# -*- coding: utf-8 -*-
"""数据库连接器：本地 SQLite 与远程 MySQL，统一「元数据 + 只读查询 + 写入」接口"""
import datetime
import os
import re
import sqlite3
import time

from .store import decrypt_password


class ConnError(Exception):
    """连接/执行失败，message 直接给用户看"""


# ── SQL 语句判定 ──────────────────────────────

_WRITE_WORDS = re.compile(
    r"\b(insert|update|delete|replace|merge|create|drop|alter|truncate|attach|detach|vacuum|grant|revoke)\b",
    re.I,
)


def _strip_sql_comments(sql):
    s = re.sub(r"/\*.*?\*/", " ", sql or "", flags=re.S)
    s = re.sub(r"(?:^|\s)--[^\n]*", " ", s)
    # # 是 MySQL 的行注释，不剥掉会把注释后的 select/update 判错
    s = re.sub(r"#[^\n]*", " ", s)
    return s.strip()


# CREATE TRIGGER/PROCEDURE 这类语句体内有分号，要一直切到块尾的 END
_BLOCK_HEAD_RE = re.compile(
    r"(?is)^\s*create\s+(?:temp\s+|temporary\s+)?(?:trigger|procedure|function|event)\b")


def _block_stmt_ended(text):
    """BEGIN ... END 块是否已结束（CASE ... END 要跟 CASE 配对，不能当成块尾）"""
    s = _strip_sql_comments(text)
    s = re.sub(r"'(?:''|\\.|[^'])*'", " ", s)
    s = re.sub(r'"(?:""|[^"])*"', " ", s)
    s = re.sub(r"`(?:``|[^`])*`", " ", s)
    in_body, case = False, 0
    for t in re.findall(r"[A-Za-z_]+", s):
        tl = t.lower()
        if not in_body:
            if tl == "begin":
                in_body = True
            continue
        if tl == "case":
            case += 1
        elif tl == "end":
            if case:
                case -= 1
            else:
                return True
    return False


def split_statements(sql):
    """按分号切分成多条语句：字符串、反引号标识符、注释里的分号不算分隔符"""
    s = sql or ""
    out, buf = [], []
    i, n = 0, len(s)
    quote = None
    while i < n:
        ch = s[i]
        if quote:
            buf.append(ch)
            # 反斜杠转义（MySQL 字符串）
            if ch == "\\" and quote in ("'", '"') and i + 1 < n:
                buf.append(s[i + 1])
                i += 2
                continue
            if ch == quote:
                # '' / "" / `` 双写转义，仍属于同一个字符串
                if i + 1 < n and s[i + 1] == quote:
                    buf.append(s[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "-" and s.startswith("--", i) and (i + 2 >= n or s[i + 2].isspace()):
            j = s.find("\n", i)
            j = n if j < 0 else j
            buf.append(s[i:j])
            i = j
            continue
        if ch == "#":
            j = s.find("\n", i)
            j = n if j < 0 else j
            buf.append(s[i:j])
            i = j
            continue
        if ch == "/" and s.startswith("/*", i):
            j = s.find("*/", i + 2)
            j = n if j < 0 else j + 2
            buf.append(s[i:j])
            i = j
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            # 触发器/存储过程体里的分号不能当语句分隔符
            if _BLOCK_HEAD_RE.match(_strip_sql_comments(stmt)) and not _block_stmt_ended(stmt):
                buf.append(ch)
                i += 1
                continue
            if _strip_sql_comments(stmt):
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    stmt = "".join(buf).strip()
    if _strip_sql_comments(stmt):
        out.append(stmt)
    return out


def is_readonly_sql(sql):
    s = _strip_sql_comments(sql).lower()
    if not s:
        return False
    # SELECT ... INTO OUTFILE/DUMPFILE 会写服务器文件，不算只读
    if re.search(r"\binto\s+(?:outfile|dumpfile)\b", s):
        return False
    if s.startswith("with"):
        # CTE 可能是 SELECT，也可能包着 UPDATE/DELETE，用写关键字兜底
        return not _WRITE_WORDS.search(s)
    return s.startswith(("select", "show", "desc", "describe", "explain", "pragma"))


# 这些语句不能包在显式事务里（VACUUM/ATTACH 等），也包含用户手写的事务控制
_NO_TXN_RE = re.compile(
    r"(?is)^\s*(?:vacuum|pragma|attach|detach|begin|commit|end|rollback|savepoint|release)\b")


def _friendly(e):
    msg = str(e)
    low = msg.lower()
    if "not authorized" in low:
        return "只允许 SELECT 查询，写操作已被拦截"
    if "no such table" in low:
        return "表不存在：" + msg.split(":", 1)[-1].strip() + "，请检查表名"
    if "no such column" in low:
        return "列不存在：" + msg.split(":", 1)[-1].strip() + "，请检查列名"
    if "access denied" in low:
        return "用户名或密码错误，或该账号没有访问权限（" + msg + "）"
    if "can't connect" in low or "timed out" in low or "timeout" in low:
        return "无法连接数据库服务器，请检查主机、端口和网络（" + msg + "）"
    if "unknown database" in low:
        return "数据库不存在：" + msg.split(":", 1)[-1].strip()
    if "already exists" in low:
        return "对象已存在：" + msg.split(":", 1)[-1].strip()
    if "cannot add a unique column" in low:
        return "SQLite 加列时不能带 UNIQUE：请先加列，再用 CREATE UNIQUE INDEX 建唯一索引"
    if "cannot add a not null column with default value null" in low:
        return "给已有表加 NOT NULL 列必须填默认值（SQLite 限制）"
    if "cannot add a column with non-constant default" in low:
        return "SQLite 加列只支持常量默认值"
    if "cannot add a primary key column" in low:
        return "SQLite 不支持给已有表加主键列"
    if "no such collation sequence" in low:
        return "SQLite 不支持这个排序规则（COLLATE）"
    if "duplicate column name" in low:
        return "列已存在：" + msg.split(":", 1)[-1].strip()
    if "syntax error" in low or "incomplete input" in low:
        return "SQL 语法错误：" + msg
    return msg


def _jsonable(v):
    if v is None:
        return None  # 保留 NULL，前端才能和数据里的空字符串区分开
    if isinstance(v, (int, float, str)):
        return v
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return str(v)
    return str(v)


def _safe_ident(name):
    s = re.sub(r"[^\w\u4e00-\u9fa5]+", "_", str(name or "").strip())
    if not s or not s.strip("_"):
        return ""
    if s[0].isdigit():
        return ""
    return s[:64]


def _sql_literal(v):
    s = str(v).strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return s
    if s.upper() in ("NULL", "CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME"):
        return s.upper()
    return "'" + s.replace("'", "''") + "'"


# 整表备份的行数上限：本地工具就不做流式分片了，避免一次导出把内存吃满
_BACKUP_MAX_ROWS = 200000

# 一次执行里最多回传多少个结果集：语句还是要全部执行，只是不再囤积结果
_QUERY_MAX_RESULT_SETS = 30


def _sqlite_literal(v):
    """把值写成 SQLite 字面量，用于备份文件里的 INSERT"""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, bytes):
        return "X'%s'" % v.hex()
    return "'" + str(v).replace("'", "''") + "'"


def _mysql_literal(v):
    """MySQL 字面量：反斜杠和单引号都要转义"""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, bytes):
        return "X'%s'" % v.hex()
    return "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"


def _mysql_default_literal(v):
    """库里的原默认值：表达式（CURRENT_TIMESTAMP(3)）原样，其余加引号"""
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if re.fullmatch(r"(?i)current_timestamp(?:\(\d+\))?", s):
        return s
    return "'" + s.replace("\\", "\\\\").replace("'", "''") + "'"


def _require_ident(name, label):
    ident = _safe_ident(name)
    if not ident:
        raise ConnError(f"{label}不合法：{name!r}（只能用中文、字母、数字、下划线，且不能以数字开头）")
    return ident


# MySQL 的库名 / 表名允许多种字符（如 hotel-manage），不能走 _safe_ident 归一化，否则中划线会变成下划线
def _mysql_ident(name, label):
    s = str(name or "").strip()
    if not s or len(s) > 64 or any(ord(ch) < 32 or ord(ch) == 127 for ch in s):
        raise ConnError(f"{label}不合法：{name!r}")
    return s.replace("`", "``")


def _where_clause(where):
    """数据页的筛选条件：只允许单条表达式，不允许用分号拼接新语句"""
    w = (where or "").strip()
    if not w:
        return ""
    if ";" in w:
        raise ConnError("筛选条件里不能包含分号")
    return w


_SQL_CONSTRAINT_KW = re.compile(
    r"^(?:primary|not|null|unique|check|default|collate|references|constraint|generated|autoincrement|as)\b",
    re.I,
)


def _safe_col_type(typ, fallback="TEXT"):
    """列类型会直接拼进 DDL：只留字母数字、括号、逗号、空格和成对的单引号（ENUM/SET 取值）"""
    s = str(typ or "").strip()
    if not s:
        return fallback
    s = re.sub(r"[^A-Za-z0-9_(), ']+", "", s)  # 去掉分号、注释符、反引号等危险字符
    s = re.sub(r"\s{2,}", " ", s).strip()
    if not re.match(r"[A-Za-z]", s):
        return fallback
    if s.count("(") != s.count(")") or s.count("'") % 2:
        return fallback
    return s[:120]


def _sqlite_col_type(typ, fallback="TEXT"):
    """SQLite 的类型参数只能是数字，ENUM('a','b') 这种直接报清楚"""
    t = _safe_col_type(typ, fallback)
    if re.search(r"\(\s*'", t):
        raise ConnError("SQLite 不支持 ENUM / SET 这类带字符串参数的类型，请改用 TEXT（限制取值可以加 CHECK 约束）")
    return t


def _split_coldef(seg):
    """把一列定义拆成 (列名, 类型, 其余修饰)，改定义时靠它保留主键/唯一/COLLATE"""
    s = (seg or "").strip()
    m = re.match(r'^(?:"([^"]+)"|`([^`]+)`|\[([^\]]+)\]|(\S+))', s)
    if not m:
        return "", "", ""
    name = m.group(1) or m.group(2) or m.group(3) or m.group(4) or ""
    rest = s[m.end():].strip()
    depth, cut = 0, len(rest)
    for t in re.finditer(r"\S+", rest):
        word = t.group(0)
        if depth == 0 and _SQL_CONSTRAINT_KW.match(word):
            cut = t.start()
            break
        depth += word.count("(") - word.count(")")
        cut = t.end()
    return name, rest[:cut].strip(), rest[cut:].strip()


def _strip_col_modifiers(tail, drop_default=True):
    """去掉顶层的 NOT NULL / DEFAULT；CHECK(...)、GENERATED ... AS (...) 括号里的内容原样保留"""
    s = tail or ""
    out, i, n, depth, quote = [], 0, len(s), 0, None
    while i < n:
        ch = s[i]
        if quote:
            out.append(ch)
            if ch == quote:
                if i + 1 < n and s[i + 1] == quote:
                    out.append(s[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "(":
            depth += 1
            out.append(ch)
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            out.append(ch)
            i += 1
            continue
        if depth == 0 and (i == 0 or not (s[i - 1].isalnum() or s[i - 1] == "_")):
            m = re.match(r"(?i)not\s+null\b", s[i:])
            if m:
                i += m.end()
                out.append(" ")
                continue
            if drop_default:
                m = re.match(r"(?i)default\b", s[i:])
                if m:
                    i += m.end()
                    while i < n and s[i].isspace():
                        i += 1
                    if i < n and s[i] in ("'", '"'):
                        q = s[i]
                        i += 1
                        while i < n:
                            if s[i] == "\\" and i + 1 < n:
                                i += 2
                                continue
                            if s[i] == q:
                                if i + 1 < n and s[i + 1] == q:
                                    i += 2
                                    continue
                                i += 1
                                break
                            i += 1
                    elif i < n and s[i] == "(":
                        d = 0
                        while i < n:
                            if s[i] == "(":
                                d += 1
                            elif s[i] == ")":
                                d -= 1
                                if d == 0:
                                    i += 1
                                    break
                            i += 1
                    else:
                        while i < n and not s[i].isspace() and s[i] not in ",)":
                            i += 1
                    out.append(" ")
                    continue
        out.append(ch)
        i += 1
    return re.sub(r"\s{2,}", " ", "".join(out)).strip()


def _split_sql_list(text):
    """按顶层逗号切分 CREATE TABLE 里的列定义（忽略括号与引号内的逗号）"""
    out, buf = [], []
    depth, quote, i = 0, None, 0
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                if i + 1 < len(text) and text[i + 1] == quote:
                    buf.append(text[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"', '`'):
            quote = ch
        elif ch == "/" and text.startswith("/*", i):
            # 块注释里的逗号不是列分隔符（COMMENT 会转成注释）
            j = text.find("*/", i + 2)
            j = len(text) if j < 0 else j + 2
            buf.append(text[i:j])
            i = j
            continue
        elif ch == "-" and text.startswith("--", i) and (i + 2 >= len(text) or text[i + 2].isspace()):
            j = text.find("\n", i)
            j = len(text) if j < 0 else j
            buf.append(text[i:j])
            i = j
            continue
        elif ch == "#":
            j = text.find("\n", i)
            j = len(text) if j < 0 else j
            buf.append(text[i:j])
            i = j
            continue
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if "".join(buf).strip():
        out.append("".join(buf).strip())
    return [x for x in out if x]


def _ends_with_line_comment(seg):
    """判断一段 SQL 是否以 -- / # 行注释收尾（拼接时会把后面的逗号、右括号一起注释掉）"""
    parts = _code_segments(seg or "")
    if not parts:
        return False
    is_code, text = parts[-1]
    if is_code:
        return False
    head = text.lstrip()
    return head.startswith("--") or head.startswith("#")


# ── SQLite 的 MySQL 兼容改写 ──────────────────────────
# 本地库经常直接粘贴 MySQL 导出的 DDL：COMMENT、UNSIGNED、字符集、AUTO_INCREMENT、
# 表级 KEY、ENGINE 表选项等 SQLite 都不认。这里做等价的文本改写，并把改动说明返回给前端。

_IDENT = r'(?:"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[[^\]]*\]|[A-Za-z_][A-Za-z0-9_$]*|[\u4e00-\u9fa5][\w$]*)'
_NATIVE_COLLATIONS = {"binary", "nocase", "rtrim"}


def _read_quoted(s, i):
    """从引号位置读取完整的字符串/标识符，返回 (原文, 结束位置)"""
    q = s[i]
    j, n = i + 1, len(s)
    while j < n:
        if s[j] == "\\" and q != "`":
            j += 2
            continue
        if s[j] == q:
            if j + 1 < n and s[j + 1] == q:
                j += 2
                continue
            return s[i:j + 1], j + 1
        j += 1
    return s[i:], n


def _kw_at(s, i, kw):
    """s[i] 处是否是独立关键字 kw"""
    if i > 0 and (s[i - 1].isalnum() or s[i - 1] in "_$"):
        return None
    return re.match(r"(?i)%s\b" % kw, s[i:])


def _skip_ws(s, i):
    while i < len(s) and s[i].isspace():
        i += 1
    return i


def _unquote_ident(raw):
    s = str(raw or "").strip()
    if len(s) >= 2 and s[0] in ('"', "`", "[") and s[-1] in ('"', "`", "]"):
        q = s[0]
        s = s[1:-1]
        if q == '"':
            s = s.replace('""', '"')
        elif q == "`":
            s = s.replace("``", "`")
    return s


def _quote_ident(name):
    return '"' + str(name or "").replace('"', '""') + '"'


def _code_segments(sql):
    """把 SQL 拆成 (是否代码, 文本)：改写只作用于代码段，字符串和注释原样保留"""
    s = sql or ""
    parts, i, n, start = [], 0, len(s), 0
    while i < n:
        ch = s[i]
        if ch in ("'", '"', "`"):
            if start < i:
                parts.append((True, s[start:i]))
            raw, j = _read_quoted(s, i)
            parts.append((False, raw))
            i = start = j
            continue
        if ch == "-" and s.startswith("--", i) and (i + 2 >= n or s[i + 2].isspace()):
            if start < i:
                parts.append((True, s[start:i]))
            j = s.find("\n", i)
            j = n if j < 0 else j
            parts.append((False, s[i:j]))
            i = start = j
            continue
        if ch == "#":
            if start < i:
                parts.append((True, s[start:i]))
            j = s.find("\n", i)
            j = n if j < 0 else j
            parts.append((False, s[i:j]))
            i = start = j
            continue
        if ch == "/" and s.startswith("/*", i):
            if start < i:
                parts.append((True, s[start:i]))
            j = s.find("*/", i + 2)
            j = n if j < 0 else j + 2
            parts.append((False, s[i:j]))
            i = start = j
            continue
        i += 1
    if start < n:
        parts.append((True, s[start:]))
    return parts or [(True, s)]


def _top_words(sql):
    """列出括号外的单词位置（跳过字符串与注释）"""
    s = sql or ""
    out, i, n, depth, quote = [], 0, len(s), 0, None
    while i < n:
        ch = s[i]
        if quote:
            if ch == "\\" and quote != "`":
                i += 2
                continue
            if ch == quote:
                if i + 1 < n and s[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        if ch == "-" and s.startswith("--", i) and (i + 2 >= n or s[i + 2].isspace()):
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "#":
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "/" and s.startswith("/*", i):
            j = s.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0 and (ch.isalnum() or ch == "_"):
            m = re.match(r"[\w$]+", s[i:])
            if m:
                out.append((i, i + m.end(), m.group(0).lower()))
                i += m.end()
                continue
        i += 1
    return out


def _top_has(sql, word):
    return any(w == word for _a, _b, w in _top_words(sql))


def _sqlite_token_compat(sql, ddl=False, alter=False):
    """MySQL 词法兼容：COMMENT/UNSIGNED/字符集/排序规则/ON UPDATE/AFTER/FIRST/NOW()"""
    s = sql or ""
    out, notes = [], []
    i, n, depth = 0, len(s), 0
    while i < n:
        ch = s[i]
        if ch in ("'", '"', "`"):
            raw, i = _read_quoted(s, i)
            out.append(raw)
            continue
        if ch == "-" and s.startswith("--", i) and (i + 2 >= n or s[i + 2].isspace()):
            j = s.find("\n", i)
            j = n if j < 0 else j
            out.append(s[i:j])
            i = j
            continue
        if ch == "#":
            j = s.find("\n", i)
            j = n if j < 0 else j
            out.append(s[i:j])
            i = j
            continue
        if ch == "/" and s.startswith("/*", i):
            j = s.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(s[i:j])
            i = j
            continue
        if ch == "(":
            depth += 1
            out.append(ch)
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            out.append(ch)
            i += 1
            continue
        if depth <= 1 and ddl:
            m = _kw_at(s, i, "comment")
            if m:
                k = _skip_ws(s, i + m.end())
                if k < n and s[k] == "=":
                    k = _skip_ws(s, k + 1)
                if k < n and s[k] in ("'", '"'):
                    raw, k2 = _read_quoted(s, k)
                    text = raw[1:-1].replace("''", "'").replace('""', '"')
                    out.append("/* %s */" % text.replace("*/", "* /"))
                    notes.append("COMMENT 注释已转成 SQL 注释")
                    i = k2
                    continue
            m = _kw_at(s, i, "unsigned") or _kw_at(s, i, "zerofill")
            if m:
                out.append(" ")
                notes.append("已去掉 MySQL 的 UNSIGNED / ZEROFILL 修饰")
                i += m.end()
                continue
            m = _kw_at(s, i, "character")
            if m:
                k = _skip_ws(s, i + m.end())
                m2 = _kw_at(s, k, "set") if k < n else None
                if m2:
                    k = _skip_ws(s, k + m2.end())
                    if k < n and s[k] == "=":
                        k = _skip_ws(s, k + 1)
                    m3 = re.match(_IDENT, s[k:]) if k < n else None
                    if m3:
                        out.append(" ")
                        notes.append("已去掉 MySQL 的字符集设置")
                        i = k + m3.end()
                        continue
            m = _kw_at(s, i, "charset")
            if m:
                k = _skip_ws(s, i + m.end())
                if k < n and s[k] == "=":
                    k = _skip_ws(s, k + 1)
                m3 = re.match(_IDENT, s[k:]) if k < n else None
                if m3:
                    out.append(" ")
                    notes.append("已去掉 MySQL 的字符集设置")
                    i = k + m3.end()
                    continue
            m = _kw_at(s, i, "collate")
            if m:
                k = _skip_ws(s, i + m.end())
                if k < n and s[k] == "=":
                    k = _skip_ws(s, k + 1)
                m3 = re.match(_IDENT, s[k:]) if k < n else None
                if m3:
                    name = _unquote_ident(m3.group(0))
                    if name.lower() in _NATIVE_COLLATIONS:
                        out.append(s[i:k + m3.end()])
                    else:
                        out.append(" ")
                        notes.append("已去掉 SQLite 不支持的排序规则 " + name)
                    i = k + m3.end()
                    continue
            m = _kw_at(s, i, "on")
            if m:
                k = _skip_ws(s, i + m.end())
                m2 = _kw_at(s, k, "update") if k < n else None
                if m2:
                    k = _skip_ws(s, k + m2.end())
                    m3 = _kw_at(s, k, "current_timestamp") if k < n else None
                    if m3:
                        k += m3.end()
                        mm = re.match(r"\s*\(\s*\d*\s*\)", s[k:])
                        if mm:
                            k += mm.end()
                        out.append(" ")
                        notes.append("已去掉 ON UPDATE（自动更新时间需用触发器实现）")
                        i = k
                        continue
            m = _kw_at(s, i, "using")
            if m:
                k = _skip_ws(s, i + m.end())
                m2 = (_kw_at(s, k, "btree") or _kw_at(s, k, "hash")) if k < n else None
                if m2:
                    out.append(" ")
                    notes.append("已去掉 MySQL 的索引类型 USING BTREE/HASH")
                    i = k + m2.end()
                    continue
            # BINARY 作为列属性（VARCHAR(10) BINARY）SQLite 不认；BINARY(16) 这类类型要保留
            m = _kw_at(s, i, "binary")
            if m:
                k = _skip_ws(s, i + m.end())
                if not (k < n and s[k] == "("):
                    out.append(" ")
                    notes.append("已去掉 MySQL 的 BINARY 列属性（SQLite 文本默认按二进制比较）")
                    i += m.end()
                    continue
            if alter and depth == 0:
                m = _kw_at(s, i, "after")
                if m:
                    k = _skip_ws(s, i + m.end())
                    m3 = re.match(_IDENT, s[k:]) if k < n else None
                    if m3:
                        out.append(" ")
                        notes.append("SQLite 不支持指定列位置，新列会追加到末尾")
                        i = k + m3.end()
                        continue
                m = _kw_at(s, i, "first")
                if m:
                    out.append(" ")
                    notes.append("SQLite 不支持指定列位置，新列会追加到末尾")
                    i += m.end()
                    continue
        m = _kw_at(s, i, "now") or _kw_at(s, i, "curdate") or _kw_at(s, i, "curtime")
        if m:
            k = _skip_ws(s, i + m.end())
            if k < n and s[k] == "(":
                k2 = _skip_ws(s, k + 1)
                if k2 < n and s[k2] == ")":
                    repl = {"now": "CURRENT_TIMESTAMP",
                            "curdate": "CURRENT_DATE",
                            "curtime": "CURRENT_TIME"}[m.group(0).lower()]
                    out.append(repl)
                    i = k2 + 1
                    continue
        m = _kw_at(s, i, "current_timestamp")
        if m:
            k = i + m.end()
            mm = re.match(r"\s*\(\s*\d*\s*\)", s[k:])
            if mm:
                out.append("CURRENT_TIMESTAMP")
                i = k + mm.end()
                continue
        out.append(ch)
        i += 1
    return "".join(out), notes


_MYSQL_TABLE_OPT_RE = re.compile(
    r"(?is)\b(?:engine|auto_increment|avg_row_length|default\s+(?:character\s+set|charset)|"
    r"character\s+set|charset|collate|row_format|key_block_size|stats_persistent|"
    r"stats_auto_recalc|stats_sample_pages|pack_keys|checksum|delay_key_write|"
    r"connection|password|encryption|compression|tablespace|union|insert_method|"
    r"max_rows|min_rows|data\s+directory|index\s+directory)"
    r"\s*(?:=\s*(?:'[^']*'|\"[^\"]*\"|[^\s,]+))?")


def _sqlite_clean_table_options(tail, notes):
    """去掉 MySQL 表选项，保留 SQLite 的 STRICT / WITHOUT ROWID 与注释"""
    kept, dropped, removed = [], [], []
    for is_code, text in _code_segments(tail or ""):
        if is_code:
            for mm in _MYSQL_TABLE_OPT_RE.finditer(text):
                opt = mm.group(0).strip()
                if opt:
                    removed.append(opt[:28])
            text = _MYSQL_TABLE_OPT_RE.sub(" ", text)
        for piece in _split_sql_list(text):
            p = piece.strip()
            if not p:
                continue
            if re.fullmatch(r"(?is)(?:strict|without\s+rowid)", p) or re.fullmatch(r"(?s)/\*.*?\*/", p):
                kept.append(p)
            elif re.fullmatch(r"[;\s]*", p):
                # 语句收尾的分号不是表选项
                continue
            elif re.fullmatch(r"(?i)default", p):
                # DEFAULT CHARSET 去掉字符集后的残留，不再单独提示
                continue
            else:
                dropped.append(p)
    if removed:
        notes.append("已移除 MySQL 表选项：" + "、".join(removed[:3]) + (" 等" if len(removed) > 3 else ""))
    if dropped:
        notes.append("已忽略表选项：" + "、".join(x[:24] for x in dropped[:3]))
    return (" " + " ".join(kept)) if kept else ""


def _make_index_sql(unique, name, tbl_raw, cols):
    cols = re.sub(r"(%s)\s*\(\s*\d+\s*\)" % _IDENT, r"\1", cols)
    return "CREATE %sINDEX %s ON %s (%s)" % (
        "UNIQUE " if unique else "", _quote_ident(_unquote_ident(name)), tbl_raw, cols)


def _matching_paren(s, open_at):
    depth, i, n, quote = 0, open_at, len(s), None
    while i < n:
        ch = s[i]
        if quote:
            if ch == "\\" and quote != "`":
                i += 2
                continue
            if ch == quote:
                if i + 1 < n and s[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        # 注释里的括号不参与配对（-- 行注释可能写到一半括号）
        if ch == "-" and s.startswith("--", i) and (i + 2 >= n or s[i + 2].isspace()):
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "#":
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "/" and s.startswith("/*", i):
            j = s.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


# 索引定义尾部可能跟着注释（COMMENT 转换而来），匹配时允许
_INDEX_TAIL = r"(?:\s*/\*.*?\*/)*\s*"


def _sqlite_enum_to_text(seg):
    """ENUM 转 TEXT + CHECK，SET 转 TEXT（多选值仍用逗号分隔保存）；返回 (新定义, 说明)"""
    m = re.match(r"(?is)^(\s*(?:%s)\s+)(enum|set)\s*\(" % _IDENT, seg or "")
    if not m:
        return seg, ""
    open_at = m.end() - 1
    close_at = _matching_paren(seg, open_at)
    if close_at < 0:
        return seg, ""
    nm = re.match(_IDENT, seg.strip())
    name = _unquote_ident(nm.group(0)) if nm else ""
    values = seg[open_at + 1:close_at]
    new = seg[:m.start(2)] + "TEXT" + seg[close_at + 1:]
    if m.group(2).lower() == "set":
        return new, "SET 已转成 TEXT（多选值用逗号分隔保存）"
    if name:
        new = new.rstrip() + " CHECK (%s IN (%s))" % (_quote_ident(name), values)
    return new, "ENUM 已转成 TEXT + CHECK 约束"


_CREATE_HEAD_RE = re.compile(
    r"(?is)^(\s*create\s+(?:temp\s+|temporary\s+)?table\s+(?:if\s+not\s+exists\s+)?)(%s)\s*\(" % _IDENT)
_ALTER_HEAD_RE = re.compile(
    r"(?is)^(\s*alter\s+table\s+(?:if\s+exists\s+)?)(%s)\s+(.+)$" % _IDENT)


def _sqlite_rewrite_create(sql, notes):
    """CREATE TABLE 的 MySQL 语法：AUTO_INCREMENT、表级 KEY、ENGINE 表选项"""
    m = _CREATE_HEAD_RE.match(sql)
    if not m:
        return sql, []
    head, tbl_raw = m.group(1), m.group(2)
    tbl_name = _unquote_ident(tbl_raw)
    open_at = m.end() - 1
    close_at = _matching_paren(sql, open_at)
    if close_at < 0:
        return sql, []
    out_defs, extra = [], []
    auto_col, idx_n = "", 0
    for seg in _split_sql_list(sql[open_at + 1:close_at]):
        if re.search(r"(?i)\bauto_increment\b", seg):
            name = _split_coldef(seg)[0]
            if auto_col:
                raise ConnError("一个表只能有一个 AUTO_INCREMENT 列")
            if not name or not re.search(r"(?i)\b(?:tinyint|smallint|mediumint|int|integer|bigint)\b", seg):
                raise ConnError("AUTO_INCREMENT 列必须是整数类型：" + seg[:60])
            auto_col = name
            out_defs.append('%s INTEGER PRIMARY KEY AUTOINCREMENT' % _quote_ident(name))
            continue
        mi = re.match(
            r"(?is)^(?:constraint\s+\S+\s+)?(fulltext\s+|spatial\s+)?(unique\s+)?(?:key|index)\s*(%s)?\s*\((.*)\)%s$"
            % (_IDENT, _INDEX_TAIL), seg)
        if mi:
            kind, uniq, raw_name, cols = mi.group(1), bool(mi.group(2)), mi.group(3), mi.group(4)
            if kind:
                notes.append("SQLite 没有全文/空间索引，已跳过该索引")
                continue
            idx_n += 1
            name = _unquote_ident(raw_name) if raw_name else "idx_%s_%d" % (_safe_ident(tbl_name) or "t", idx_n)
            extra.append(_make_index_sql(uniq, name, tbl_raw, cols))
            notes.append("表级 KEY 已转成 CREATE INDEX")
            continue
        seg, note = _sqlite_enum_to_text(seg)
        if note:
            notes.append(note)
        out_defs.append(seg)
    if auto_col:
        for i, seg in enumerate(out_defs):
            mp = re.match(r"(?is)^(?:constraint\s+\S+\s+)?primary\s+key\s*\((.*)\)%s$" % _INDEX_TAIL, seg)
            if not mp:
                continue
            cols = [_unquote_ident(c.split()[0]) for c in _split_sql_list(mp.group(1)) if c.strip()]
            if len(cols) == 1 and cols[0].lower() == auto_col.lower():
                out_defs[i] = ""
            elif any(c.lower() == auto_col.lower() for c in cols):
                raise ConnError("SQLite 不支持复合主键中包含自增列")
        notes.append("AUTO_INCREMENT 已转成 INTEGER PRIMARY KEY AUTOINCREMENT")
    defs = []
    for seg in out_defs:
        if not seg.strip():
            continue
        # 行注释会一路延伸到拼接点：不补换行的话，后面的逗号和右括号会被它注释掉
        defs.append(seg + ("\n" if _ends_with_line_comment(seg) else ""))
    body = ", ".join(defs)
    suffix = _sqlite_clean_table_options(sql[close_at + 1:], notes)
    return head + tbl_raw + " (" + body + ")" + suffix, extra


def _sqlite_rewrite_alter(sql, notes):
    """ALTER TABLE：多列 ADD 拆成多条、ADD INDEX 转 CREATE INDEX、去掉 MySQL 专属操作"""
    m = _ALTER_HEAD_RE.match(sql)
    if not m:
        return [sql]
    head, tbl_raw, rest = m.group(1), m.group(2), m.group(3)
    tbl_name = _unquote_ident(tbl_raw)
    out = []
    for n, op in enumerate(_split_sql_list(rest), 1):
        op = op.strip()
        if not op:
            continue
        low = op.lower()
        if re.match(r"^(?:modify|change)\b", low):
            raise ConnError("SQLite 不支持 ALTER ... MODIFY / CHANGE，请在「表定义」页签改列（会自动重建表并保留数据）")
        if re.match(r"^add\s+(?:constraint\s+\S+\s+)?(?:primary\s+key|foreign\s+key)\b", low):
            raise ConnError("SQLite 不支持给已有表添加主键/外键，只能在建表时定义")
        if re.match(r"^(?:drop\s+)?(?:primary\s+key|foreign\s+key)\b", low):
            raise ConnError("SQLite 不支持在已有表上删除主键/外键")
        if re.match(r"^alter\s+column\b", low):
            raise ConnError("SQLite 不支持 ALTER COLUMN，请在「表定义」页签改列")
        mi = re.match(r"(?is)^add\s+(?:(unique|fulltext|spatial)\s+)?(?:index|key)\s*(%s)?\s*\((.*)\)%s$" % (_IDENT, _INDEX_TAIL), op)
        if mi:
            kind, raw_name, cols = mi.group(1), mi.group(2), mi.group(3)
            if kind in ("fulltext", "spatial"):
                notes.append("SQLite 没有全文/空间索引，已跳过该索引")
                continue
            idx = _unquote_ident(raw_name) if raw_name else "idx_%s_%d" % (_safe_ident(tbl_name) or "t", n)
            out.append(_make_index_sql(kind == "unique", idx, tbl_raw, cols))
            notes.append("ADD INDEX 已转成 CREATE INDEX")
            continue
        mu = re.match(r"(?is)^add\s+unique\s*\((.*)\)%s$" % _INDEX_TAIL, op)
        if mu:
            idx = "idx_%s_%d" % (_safe_ident(tbl_name) or "t", n)
            out.append(_make_index_sql(True, idx, tbl_raw, mu.group(1)))
            notes.append("ADD UNIQUE 已转成 CREATE UNIQUE INDEX")
            continue
        mc = re.match(r"(?is)^add\s+(?:column\s+)?(.+)$", op)
        if mc and not re.match(r"^(?:constraint|index|key|unique|fulltext|spatial|primary|foreign)\b", mc.group(1).lower()):
            coldef = mc.group(1).strip()
            if re.search(r"(?i)\bauto_increment\b", coldef):
                raise ConnError("SQLite 不支持给已有表加自增列，请在「表定义」里加普通 INTEGER 列")
            coldef, note = _sqlite_enum_to_text(coldef)
            if note:
                notes.append(note)
            unique = _top_has(coldef, "unique")
            if unique:
                for a, b, w in _top_words(coldef):
                    if w == "unique":
                        coldef = (coldef[:a] + " " + coldef[b:]).strip()
                        break
                notes.append("列上的 UNIQUE 已转成 CREATE UNIQUE INDEX")
            out.append("%s%s ADD COLUMN %s" % (head, tbl_raw, coldef))
            if unique:
                cname = _split_coldef(coldef)[0] or ("c%d" % n)
                idx = "idx_%s_%s" % (_safe_ident(tbl_name) or "t", _safe_ident(cname) or ("c%d" % n))
                out.append(_make_index_sql(True, idx, tbl_raw, _quote_ident(cname)))
            continue
        if re.match(r"^drop\s+index\b", low):
            md = re.match(r"(?is)^drop\s+index\s+(%s)\s*$" % _IDENT, op)
            if md:
                out.append("DROP INDEX " + md.group(1))
                continue
        if re.match(r"^(?:engine|default\s+(?:character\s+set|charset)|character\s+set|charset|collate|row_format|auto_increment|comment|convert\s+to)\b", low):
            notes.append("已忽略 MySQL 表选项：" + op[:40])
            continue
        out.append("%s%s %s" % (head, tbl_raw, op))
    return out


def _rewrite_update_delete_limit(sql):
    """MySQL 的 UPDATE / DELETE ... LIMIT：SQLite 默认不支持，用 rowid 子查询改写"""
    m = re.match(r"(?is)^\s*delete\s+from\s+(%s)(.*)$" % _IDENT, sql)
    if m:
        tbl_raw, rest = m.group(1), m.group(2)
        ml = re.match(r"(?is)^(.*?)\s+limit\s+(\d+)\s*$", rest)
        if not ml:
            return sql, False
        cond = ml.group(1).strip()
        sub = "SELECT rowid FROM %s%s LIMIT %s" % (tbl_raw, (" " + cond) if cond else "", ml.group(2))
        return "DELETE FROM %s WHERE rowid IN (%s)" % (tbl_raw, sub), True
    m = re.match(r"(?is)^\s*update\s+(%s)\s+set\s+(.+)$" % _IDENT, sql)
    if m:
        tbl_raw, rest = m.group(1), m.group(2)
        ml = re.match(r"(?is)^(.*?)\s+limit\s+(\d+)\s*$", rest)
        if not ml:
            return sql, False
        body = ml.group(1).strip()
        where_at = None
        for a, b, w in _top_words(body):
            if w == "where":
                where_at = (a, b)
                break
        if where_at:
            sets = body[:where_at[0]].strip()
            sub = "SELECT rowid FROM %s %s LIMIT %s" % (tbl_raw, body[where_at[1]:].strip(), ml.group(2))
        else:
            sets = body
            sub = "SELECT rowid FROM %s LIMIT %s" % (tbl_raw, ml.group(2))
        return "UPDATE %s SET %s WHERE rowid IN (%s)" % (tbl_raw, sets, sub), True
    return sql, False


def _sqlite_rewrite_stmt(sql):
    """返回 (真正要执行的 SQL 列表, 说明列表)；空列表表示这条语句被忽略"""
    notes = []
    low = re.sub(r"\s+", " ", _strip_sql_comments(sql)).strip().lower()
    if not low:
        return [], notes
    if re.match(r"^set\s", low):
        return [], ["已忽略 MySQL 的 SET 会话语句"]
    if re.match(r"^(?:use\s|create\s+database\b|drop\s+database\b)", low):
        return [], ["已忽略 MySQL 的库级语句（本地库是单个文件）"]
    if re.match(r"^(?:lock\s+tables?\b|unlock\s+tables?\b)", low):
        return [], ["已忽略 MySQL 的锁表语句"]
    if low.startswith("truncate"):
        m = re.match(r"(?is)^\s*truncate\s+(?:table\s+)?(%s)\s*$" % _IDENT, sql)
        if m:
            return ["DELETE FROM " + m.group(1)], ["TRUNCATE 已转成 DELETE（自增序列不重置）"]
    m = re.match(r"(?is)^\s*drop\s+index\s+(?:if\s+exists\s+)?(%s)\s+on\s+%s\s*$" % (_IDENT, _IDENT), sql)
    if m and low.startswith("drop index"):
        return ["DROP INDEX " + m.group(1)], ["已去掉 DROP INDEX 的 ON 表名（SQLite 按索引名删除）"]
    if re.match(r"^rename\s+tables?\b", low):
        mr = re.match(r"(?is)^\s*rename\s+tables?\s+(.+)$", sql)
        out = []
        for p in _split_sql_list(mr.group(1)) if mr else []:
            mp = re.match(r"(?is)^(%s)\s+to\s+(%s)$" % (_IDENT, _IDENT), p.strip())
            if not mp:
                raise ConnError("无法解析 RENAME TABLE 语句：" + p[:60])
            out.append("ALTER TABLE %s RENAME TO %s" % (mp.group(1), mp.group(2)))
        if out:
            return out, ["RENAME TABLE 已转成 ALTER TABLE ... RENAME TO"]
    if re.match(r"(?is)^\s*create\s+(?:temp\s+|temporary\s+)?table\s+(?:if\s+not\s+exists\s+)?%s\s+like\s+%s\s*$" % (_IDENT, _IDENT), sql):
        raise ConnError("SQLite 不支持 CREATE TABLE ... LIKE，请改用 CREATE TABLE ... AS SELECT ...（只复制列和数据）")
    if re.match(r"^insert\s+ignore\b", low):
        sql = re.sub(r"(?is)^(\s*insert)\s+ignore\b", r"\1 OR IGNORE", sql)
        notes.append("INSERT IGNORE 已转成 INSERT OR IGNORE")
    if re.search(r"\bon\s+duplicate\s+key\s+update\b", low):
        raise ConnError("SQLite 不支持 ON DUPLICATE KEY UPDATE，请改用 INSERT OR REPLACE，或先查询再更新")
    if re.match(r"^(?:update|delete)\b", low):
        sql, changed = _rewrite_update_delete_limit(sql)
        if changed:
            notes.append("UPDATE/DELETE 的 LIMIT 已用 rowid 子查询改写")
    if re.match(r"^create\s+(?:unique\s+)?index\b", low):
        s2 = re.sub(r"(?is)\s+using\s+(?:btree|hash)\b", " ", sql)
        if s2 != sql:
            notes.append("已去掉 MySQL 的索引类型 USING BTREE/HASH")
            sql = s2
    if re.match(r"^create\s+(?:temp\s+|temporary\s+)?table\b", low):
        sql, n2 = _sqlite_token_compat(sql, ddl=True)
        notes += n2
        sql, extra = _sqlite_rewrite_create(sql, notes)
        return [sql] + extra, notes
    if re.match(r"^alter\s+table\b", low):
        sql, n2 = _sqlite_token_compat(sql, ddl=True, alter=True)
        notes += n2
        return _sqlite_rewrite_alter(sql, notes), notes
    sql, n2 = _sqlite_token_compat(sql, ddl=False)
    notes += n2
    return [sql], notes


def _rewrite_limit_offset(sql):
    """MySQL 的 LIMIT offset, count 转成 SQLite 的 LIMIT count OFFSET offset"""
    out = []
    for is_code, text in _code_segments(sql):
        if is_code:
            text = re.sub(r"(?i)\blimit\s+(\d+)\s*,\s*(\d+)", r"LIMIT \2 OFFSET \1", text)
        out.append(text)
    return "".join(out)


def _sqlite_query_rewrite(sql):
    """查询页的 MySQL 兼容：SHOW / DESC / LIMIT x,y / NOW()"""
    notes = []
    s, n2 = _sqlite_token_compat(sql, ddl=False)
    notes += n2
    s = _rewrite_limit_offset(s)
    if re.match(r"(?is)^\s*show\s+(?:full\s+)?tables\b", s):
        notes.append("SHOW TABLES 已转成查询 sqlite_master")
        return ("SELECT name AS \"表名\" FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"), notes
    m = re.match(r"(?is)^\s*show\s+create\s+table\s+(%s)\s*$" % _IDENT, s)
    if m:
        notes.append("SHOW CREATE TABLE 已转成查询 sqlite_master")
        return ("SELECT sql AS \"Create Table\" FROM sqlite_master "
                "WHERE type = 'table' AND name = %s" % _sql_literal(_unquote_ident(m.group(1)))), notes
    m = re.match(r"(?is)^\s*show\s+(?:full\s+)?columns\s+from\s+(%s)" % _IDENT, s)
    if m:
        notes.append("SHOW COLUMNS 已转成 PRAGMA table_info")
        return "PRAGMA table_info(%s)" % m.group(1), notes
    m = re.match(r"(?is)^\s*show\s+(?:index|indexes|keys)\s+from\s+(%s)" % _IDENT, s)
    if m:
        notes.append("SHOW INDEX 已转成 PRAGMA index_list")
        return "PRAGMA index_list(%s)" % m.group(1), notes
    m = re.match(r"(?is)^\s*(?:desc|describe)\s+(%s)\s*$" % _IDENT, s)
    if m:
        notes.append("DESC 已转成 PRAGMA table_info")
        return "PRAGMA table_info(%s)" % m.group(1), notes
    return s, notes


def _sqlite_col_comments(ddl):
    """从建表 SQL 的 /* 注释 */ 里取回列注释（COMMENT 转换后仍能展示）"""
    out = {}
    if not ddl or "(" not in ddl or ")" not in ddl:
        return out
    body = ddl[ddl.index("(") + 1: ddl.rindex(")")]
    for seg in _split_sql_list(body):
        mc = re.findall(r"/\*.*?\*/", seg, re.S)
        if not mc:
            continue
        name, _typ, _tail = _split_coldef(seg)
        if name:
            text = mc[-1][2:-2].strip()
            if text:
                out[name.lower()] = text
    return out


# ── SQLite ──────────────────────────────

_CREATE_INDEX_RE = re.compile(
    r"(?is)^\s*create\s+(unique\s+)?index\s+(%s)\s+on\s+(%s)\s*\(" % (_IDENT, _IDENT))


def _index_owner(conn, name):
    """索引名已存在时返回它所属的表，不存在返回 None"""
    row = conn.execute(
        "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=? AND sql IS NOT NULL",
        (name,)).fetchone()
    return row[0] if row else None


def _exec_sqlite_one(conn, sql, notes):
    """执行单条语句；CREATE INDEX 撞名时按 MySQL 语义处理（索引名只在表内唯一）"""
    try:
        return conn.execute(sql)
    except sqlite3.DatabaseError as e:
        m = _CREATE_INDEX_RE.match(sql)
        if not m or "already exists" not in str(e).lower():
            raise
        name, tbl = _unquote_ident(m.group(2)), _unquote_ident(m.group(3))
        owner = _index_owner(conn, name)
        if owner is not None and owner.lower() == tbl.lower():
            notes.append("索引 %s 已存在，已跳过重复创建" % name)
            return None
        base = "idx_%s_%s" % (_safe_ident(tbl) or "t", _safe_ident(name) or "i")
        cand, k = base, 1
        while _index_owner(conn, cand) is not None:
            k += 1
            cand = "%s_%d" % (base, k)
        notes.append("索引 %s 与其它表同名，已改名为 %s（SQLite 索引名全库唯一）" % (name, cand))
        return conn.execute(sql[:m.start(2)] + _quote_ident(cand) + sql[m.end(2):])


_XDB_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
}

# 只读 PRAGMA 白名单：table_info / index_list 这类只看结构；写类 PRAGMA（user_version、journal_mode 等）一律不放行
_XDB_RO_PRAGMA = {
    "table_info", "table_xinfo", "table_list", "index_list", "index_info", "index_xinfo",
    "foreign_key_list", "database_list", "collation_list", "compile_options",
    "integrity_check", "quick_check", "foreign_key_check", "encoding",
    "page_count", "freelist_count", "data_version", "pragma_list", "function_list",
    "module_list",
}

_XDB_DENY_FUNCS = {"load_extension"}


def _sqlite_authorizer(action, arg1, arg2, dbname, trigger):
    if action in _XDB_ALLOWED_ACTIONS:
        if action == sqlite3.SQLITE_FUNCTION and str(arg2 or "").lower() in _XDB_DENY_FUNCS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and str(arg1 or "").lower() in _XDB_RO_PRAGMA:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


class SQLiteConnector:
    kind = "sqlite"

    def __init__(self, src, data_dir, readonly=None):
        self.src = src
        self.data_dir = data_dir
        self.readonly = src.get("readonly", True) if readonly is None else bool(readonly)
        p = src.get("path") or ""
        self.path = p if os.path.isabs(p) else os.path.join(data_dir, p)
        if not os.path.isfile(self.path):
            raise ConnError("数据库文件不存在：" + self.path)

    def _conn(self, guard=True):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        if guard and self.readonly:
            conn.set_authorizer(_sqlite_authorizer)
        return conn

    def test(self):
        started = time.time()
        conn = self._conn(guard=False)
        try:
            ver = conn.execute("SELECT sqlite_version()").fetchone()[0]
            return {"ok": True, "server": "SQLite " + str(ver),
                    "elapsed_ms": int((time.time() - started) * 1000)}
        except sqlite3.DatabaseError as e:
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def list_schemas(self):
        return ["main"]

    def list_tables(self, schema=None):
        conn = self._conn(guard=False)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            return [r[0] for r in rows]
        except sqlite3.DatabaseError as e:
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def list_columns(self, schema, table):
        tname = _require_ident(table, "表名")
        conn = self._conn(guard=False)
        try:
            rows = conn.execute(f'PRAGMA table_info("{tname}")').fetchall()
            ddl_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (tname,)).fetchone()
            comments = _sqlite_col_comments(ddl_row[0] if ddl_row else "")
            return [{
                "name": r["name"],
                "type": r["type"] or "TEXT",
                "pk": bool(r["pk"]),
                "notnull": bool(r["notnull"]),
                "default": r["dflt_value"],
                "comment": comments.get(str(r["name"]).lower(), ""),
            } for r in rows]
        except sqlite3.DatabaseError as e:
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def table_info(self, schema, table):
        tname = _require_ident(table, "表名")
        cols = self.list_columns(schema, tname)
        conn = self._conn(guard=False)
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (tname,)).fetchone()
            ddl = (row[0] if row else "") or ""
            indexes = [r[0] for r in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                (tname,)).fetchall()]
        except sqlite3.DatabaseError as e:
            raise ConnError(_friendly(e))
        finally:
            conn.close()
        return {"table": tname, "comment": "", "columns": cols, "ddl": ddl, "indexes": indexes}

    def query(self, sql, limit=5000, schema=None):
        stmts = split_statements(sql)
        if not stmts:
            raise ConnError("请输入 SQL 语句")
        started = time.time()
        conn = self._conn()
        try:
            results = []
            capped = False
            notes = []
            # 多条语句按顺序执行，每条有结果集的都单独收好，前端按「第 N 条」分别展示
            for idx, raw in enumerate(stmts, 1):
                s, ns = _sqlite_query_rewrite(raw)
                notes += ns
                if not s:
                    continue
                cur = conn.execute(s)
                if cur.description is None:
                    continue
                if len(results) >= _QUERY_MAX_RESULT_SETS:
                    capped = True
                    continue
                cols = [d[0] for d in cur.description]
                rows = cur.fetchmany(limit + 1)
                truncated = len(rows) > limit
                if truncated:
                    rows = rows[:limit]
                results.append({
                    "index": idx,
                    "sql": raw.strip(),
                    "columns": cols,
                    "rows": [[_jsonable(v) for v in r] for r in rows],
                    "count": len(rows),
                    "truncated": truncated,
                })
            if not results:
                raise ConnError("语句已执行，但没有可展示的结果集")
            result = dict(results[-1])
            result["results"] = results
            result["capped"] = capped
            result["statements"] = len(stmts)
            result["elapsed_ms"] = int((time.time() - started) * 1000)
            if notes:
                result["notes"] = list(dict.fromkeys(notes))
            return result
        except sqlite3.DatabaseError as e:
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def execute(self, sql, schema=None):
        if self.readonly:
            raise ConnError("当前数据源是只读模式，请先打开「可写」开关")
        stmts = split_statements(sql)
        if not stmts:
            raise ConnError("请输入 SQL 语句")
        conn = self._conn(guard=False)
        # sqlite3 模块不会把 DDL 放进隐式事务，这里自己开事务才能做到多条语句整体回滚
        in_txn = not any(_NO_TXN_RE.match(s) for s in stmts)
        idx = 0
        notes = []
        try:
            total, lastrowid = 0, None
            if in_txn:
                conn.isolation_level = None
                conn.execute("BEGIN")
            for idx, s in enumerate(stmts, 1):
                # MySQL 风格语句先改写成 SQLite 能执行的（COMMENT/多列 ADD/KEY 等）
                todo, ns = _sqlite_rewrite_stmt(s)
                notes += ns
                for one in todo:
                    cur = _exec_sqlite_one(conn, one, notes)
                    if cur is None:
                        continue
                    if cur.rowcount and cur.rowcount > 0:
                        total += cur.rowcount
                    lastrowid = cur.lastrowid
            if in_txn:
                conn.execute("COMMIT")
            else:
                conn.commit()
            res = {"ok": True, "rowcount": total, "statements": len(stmts), "lastrowid": lastrowid}
            if notes:
                res["notes"] = list(dict.fromkeys(notes))
            return res
        except (ConnError, sqlite3.DatabaseError) as e:
            if in_txn:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.DatabaseError:
                    pass
            else:
                conn.rollback()
            msg = str(e) if isinstance(e, ConnError) else _friendly(e)
            if len(stmts) > 1:
                if in_txn:
                    msg += "（第 %d 条语句失败，已整体回滚）" % idx
                else:
                    msg += "（第 %d 条语句失败；含 VACUUM/PRAGMA 等语句，无法整体回滚）" % idx
            raise ConnError(msg)
        finally:
            conn.close()

    def create_table(self, table, columns):
        if self.readonly:
            raise ConnError("当前数据源是只读模式，请先打开「可写」开关")
        tname = _require_ident(table, "表名")
        defs, names = [], set()
        for c in columns or []:
            cn = _require_ident(c.get("name"), "列名")
            if cn in names:
                raise ConnError("列名重复：" + cn)
            names.add(cn)
            typ = str(c.get("type") or "TEXT").upper()
            if typ not in ("TEXT", "INTEGER", "REAL", "BLOB", "NUMERIC"):
                typ = "TEXT"
            d = f'"{cn}" {typ}'
            if c.get("pk"):
                d += " PRIMARY KEY"
            if c.get("notnull"):
                d += " NOT NULL"
            if c.get("default") not in (None, ""):
                d += " DEFAULT " + _sql_literal(c.get("default"))
            defs.append(d)
        if not defs:
            raise ConnError("至少需要一列")
        conn = self._conn(guard=False)
        try:
            conn.execute(f'CREATE TABLE "{tname}" ({", ".join(defs)})')
            conn.commit()
            return {"ok": True, "table": tname}
        except sqlite3.DatabaseError as e:
            conn.rollback()
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def alter_column(self, schema, table, data):
        """改表定义：add / modify（可改名，也能改类型、非空、默认值）/ drop"""
        if self.readonly:
            raise ConnError("当前数据源是只读模式，请先打开「可写」开关")
        tname = _require_ident(table, "表名")
        action = str(data.get("action") or "").strip().lower()
        col = data.get("column") or {}
        old_name = str(data.get("old_name") or col.get("name") or "").strip()
        conn = self._conn(guard=False)
        try:
            if action == "add":
                cname = _require_ident(col.get("name"), "列名")
                ctype = _sqlite_col_type(col.get("type"))
                if col.get("notnull") and col.get("default") in (None, ""):
                    raise ConnError("给已有表加 NOT NULL 列必须填默认值（SQLite 限制）")
                ddl = '"%s" %s' % (cname, ctype)
                if col.get("notnull"):
                    ddl += " NOT NULL"
                if col.get("default") not in (None, ""):
                    ddl += " DEFAULT " + _sql_literal(col.get("default"))
                conn.execute('ALTER TABLE "%s" ADD COLUMN %s' % (tname, ddl))
                conn.commit()
                return {"ok": True, "table": tname, "column": cname, "action": "add"}

            if action == "drop":
                cname = _require_ident(old_name, "列名")
                conn.execute('ALTER TABLE "%s" DROP COLUMN "%s"' % (tname, cname))
                conn.commit()
                return {"ok": True, "table": tname, "action": "drop"}

            if action not in ("modify", "rename"):
                raise ConnError("不支持的改表操作：" + (action or "空"))

            cname = _require_ident(old_name, "原列名")
            new_name = _require_ident(col.get("name") or cname, "列名")
            new_type = _sqlite_col_type(col.get("type"))
            want_default = col.get("default") not in (None, "")
            tmp = "__dbx_rebuild_%s" % tname
            # 改名、重建都放进同一个事务：中途失败整体回滚，不留半改状态
            conn.isolation_level = None
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("PRAGMA legacy_alter_table=ON")
            conn.execute("BEGIN")
            try:
                if new_name != cname:
                    # 先交给 SQLite 改名，视图/触发器/索引里的列引用会同步改写
                    conn.execute('ALTER TABLE "%s" RENAME COLUMN "%s" TO "%s"' % (tname, cname, new_name))
                    cname = new_name
                row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (tname,)).fetchone()
                old_sql = (row[0] if row else "") or ""
                if "(" not in old_sql or ")" not in old_sql:
                    raise ConnError("读不到建表语句，无法修改表定义")
                open_at, close_at = old_sql.index("("), old_sql.rindex(")")
                if close_at <= open_at:
                    raise ConnError("读不到建表语句，无法修改表定义")
                defs = _split_sql_list(old_sql[open_at + 1: close_at])
                suffix = old_sql[close_at + 1:].strip()   # STRICT / WITHOUT ROWID 等表选项
                hit = False
                for i, seg in enumerate(defs):
                    name, _typ, tail = _split_coldef(seg)
                    if name.lower() != cname.lower():
                        continue
                    new_def = '"%s" %s' % (cname, new_type)
                    if col.get("notnull"):
                        new_def += " NOT NULL"
                    if want_default:
                        new_def += " DEFAULT " + _sql_literal(col.get("default"))
                    # 主键、唯一、CHECK、COLLATE 等原样保留；默认值留空时也不动原来的
                    keep = _strip_col_modifiers(tail, drop_default=want_default)
                    if keep:
                        new_def += " " + keep
                    defs[i] = new_def
                    hit = True
                    break
                if not hit:
                    raise ConnError("表里没有这一列：" + cname)
                new_create = 'CREATE TABLE "%s" (%s)' % (tname, ", ".join(defs))
                if suffix:
                    new_create += " " + suffix
                # 索引/触发器先存下来，重建表后按原样建回去（改名后 SQL 已被 SQLite 改写）
                extras = [r[0] for r in conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type IN ('index','trigger') AND tbl_name=? AND sql IS NOT NULL",
                    (tname,)).fetchall()]
                # generated 列不能手动插入，迁移数据时只搬普通列（xinfo 第 7 列 2/3 为生成列）
                data_cols = [r[1] for r in conn.execute(
                    'PRAGMA table_xinfo("%s")' % tname).fetchall() if len(r) < 7 or r[6] not in (2, 3)]
                if not data_cols:
                    raise ConnError("表里没有可迁移的数据列")
                col_list = ", ".join('"%s"' % c for c in data_cols)
                conn.execute('ALTER TABLE "%s" RENAME TO "%s"' % (tname, tmp))
                conn.execute(new_create)
                conn.execute('INSERT INTO "%s" (%s) SELECT %s FROM "%s"' % (tname, col_list, col_list, tmp))
                conn.execute('DROP TABLE "%s"' % tmp)
                for sql in extras:
                    conn.execute(sql)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                try:
                    conn.execute("PRAGMA legacy_alter_table=OFF")
                    conn.execute("PRAGMA foreign_keys=ON")
                except sqlite3.DatabaseError:
                    pass
            return {"ok": True, "table": tname, "column": cname, "action": "modify"}
        except sqlite3.DatabaseError as e:
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def table_data(self, table, limit=100, offset=0, where="", schema=None, order=None):
        tname = _require_ident(table, "表名")
        cond = _where_clause(where)
        tail = (" WHERE " + cond) if cond else ""
        conn = self._conn()
        try:
            info_rows = conn.execute('PRAGMA table_xinfo("%s")' % tname).fetchall()
            # 第 7 列：2/3 是生成列（不能直接改）；第 6 列是主键序号
            gen_cols = [r[1] for r in info_rows if len(r) > 6 and r[6] in (2, 3)]
            pk_cols = [r[1] for r in info_rows if len(r) > 5 and r[5]]
            # 单列 INTEGER 主键就是 rowid 别名，新增时由数据库生成，前端不给输入框
            auto_cols = []
            if len(pk_cols) == 1:
                for r in info_rows:
                    if r[1] == pk_cols[0] and str(r[2] or "").strip().upper() == "INTEGER":
                        auto_cols.append(r[1])
                        break
            # 排序：只认可表里真实存在的列，方向只允许 ASC/DESC
            ob = ""
            if order:
                want = str(order.get("column") or "").strip()
                names = [r[1] for r in info_rows]
                ocol = want if want in names else _require_ident(want, "排序列")
                if ocol not in names:
                    raise ConnError("列不存在：" + want)
                ob = ' ORDER BY "%s" %s' % (ocol.replace('"', '""'),
                                            "DESC" if str(order.get("dir") or "").lower() == "desc" else "ASC")
            total = conn.execute('SELECT COUNT(*) FROM "%s"%s' % (tname, tail)).fetchone()[0]
            use_rowid = False
            if not pk_cols:
                # 没有主键就用 rowid 定位；WITHOUT ROWID 表不行，退回只读
                try:
                    conn.execute('SELECT rowid FROM "%s" LIMIT 1' % tname).fetchone()
                    use_rowid = True
                except sqlite3.DatabaseError:
                    use_rowid = False
            sel = 'rowid AS "__dbx_rowid", *' if use_rowid else "*"
            cur = conn.execute('SELECT %s FROM "%s"%s%s LIMIT ? OFFSET ?' % (sel, tname, tail, ob),
                               (int(limit), int(offset)))
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            keys = []
            if use_rowid:
                cols = cols[1:]
                keys = [{"__rowid__": r[0]} for r in rows]
                out_rows = [list(r[1:]) for r in rows]
            else:
                out_rows = [list(r) for r in rows]
                if pk_cols:
                    idx = {c: cols.index(c) for c in pk_cols if c in cols}
                    keys = [{c: _jsonable(r[idx[c]]) for c in pk_cols if c in idx} for r in rows]
            return {"columns": cols,
                    "rows": [[_jsonable(v) for v in r] for r in out_rows],
                    "keys": keys if keys else None,
                    # 空表时 keys 为空，不能因此判成不可编辑：有主键 / 能用 rowid 就能新增
                    "editable": bool(pk_cols) or use_rowid,
                    "readonly_cols": gen_cols,
                    "auto_cols": auto_cols,
                    "count": len(out_rows), "total": total,
                    "offset": int(offset), "limit": int(limit)}
        except sqlite3.DatabaseError as e:
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def update_cell(self, schema, table, rowkey, column, value):
        """双击改单元格：只允许按主键/rowid 精确更新一行"""
        if self.readonly:
            raise ConnError("当前数据源是只读模式，请先打开「可写」开关")
        tname = _require_ident(table, "表名")
        col = _require_ident(column, "列名")
        key = rowkey if isinstance(rowkey, dict) else {}
        conn = self._conn(guard=False)
        try:
            info_rows = conn.execute('PRAGMA table_xinfo("%s")' % tname).fetchall()
            if not info_rows:
                raise ConnError("表不存在：" + tname)
            names = {r[1] for r in info_rows}
            if col not in names:
                raise ConnError("列不存在：" + col)
            if any(r[1] == col and len(r) > 6 and r[6] in (2, 3) for r in info_rows):
                raise ConnError("生成列不能直接修改")
            pk_cols = [r[1] for r in info_rows if len(r) > 5 and r[5]]
            if "__rowid__" in key:
                if pk_cols:
                    raise ConnError("定位条件不合法，请刷新后重试")
                try:
                    conn.execute('SELECT rowid FROM "%s" LIMIT 1' % tname).fetchone()
                except sqlite3.DatabaseError:
                    raise ConnError("该表没有主键，无法定位要修改的行")
                where, params = "rowid = ?", [key["__rowid__"]]
            else:
                if not pk_cols:
                    raise ConnError("该表没有主键，无法定位要修改的行")
                missing = [c for c in pk_cols if c not in key]
                if missing:
                    raise ConnError("缺少主键定位值：" + "、".join(missing))
                extra = [c for c in key if c not in pk_cols]
                if extra:
                    raise ConnError("定位条件里含非主键列：" + "、".join(extra))
                where = " AND ".join('"%s" = ?' % _require_ident(c, "主键列") for c in pk_cols)
                params = [key[c] for c in pk_cols]
            cur = conn.execute('UPDATE "%s" SET "%s" = ? WHERE %s' % (tname, col, where),
                               [value] + params)
            if cur.rowcount > 1:
                conn.rollback()
                raise ConnError("定位到多行，已取消修改")
            conn.commit()
            return {"ok": True, "rowcount": cur.rowcount}
        except sqlite3.DatabaseError as e:
            conn.rollback()
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def save_rows(self, schema, table, updates=None, inserts=None, deletes=None):
        """数据页暂存编辑一次提交：删除 / 修改 / 新增在同一个事务里完成

        行定位与 update_cell 一致：有主键按主键，没有主键用 rowid。
        新增行只提交用户填过的列，没填的列交给库里的默认值。
        """
        if self.readonly:
            raise ConnError("当前数据源是只读模式")
        tname = _require_ident(table, "表名")
        conn = self._conn(guard=False)
        try:
            info_rows = conn.execute('PRAGMA table_xinfo("%s")' % tname).fetchall()
            if not info_rows:
                raise ConnError("表不存在：" + tname)
            names = [r[1] for r in info_rows]
            gen_cols = {r[1] for r in info_rows if len(r) > 6 and r[6] in (2, 3)}
            pk_cols = [r[1] for r in info_rows if len(r) > 5 and r[5]]

            def locator(key):
                key = key if isinstance(key, dict) else {}
                if "__rowid__" in key:
                    if pk_cols:
                        raise ConnError("定位条件不合法，请刷新后重试")
                    try:
                        conn.execute('SELECT rowid FROM "%s" LIMIT 1' % tname).fetchone()
                    except sqlite3.DatabaseError:
                        raise ConnError("该表没有主键，无法定位要修改的行")
                    return "rowid = ?", [key["__rowid__"]]
                if not pk_cols:
                    raise ConnError("该表没有主键，无法定位要修改的行")
                missing = [c for c in pk_cols if c not in key]
                if missing:
                    raise ConnError("缺少主键定位值：" + "、".join(missing))
                extra = [c for c in key if c not in pk_cols]
                if extra:
                    raise ConnError("定位条件里含非主键列：" + "、".join(extra))
                return (" AND ".join('"%s" = ?' % _require_ident(c, "主键列") for c in pk_cols),
                        [key[c] for c in pk_cols])

            counts = {"inserted": 0, "updated": 0, "deleted": 0}
            for key in (deletes or []):
                where, params = locator(key)
                cur = conn.execute('DELETE FROM "%s" WHERE %s' % (tname, where), params)
                if cur.rowcount != 1:
                    raise ConnError("删除没有定位到唯一一行，已取消本次保存")
                counts["deleted"] += cur.rowcount

            for item in (updates or []):
                item = item or {}
                vals = item.get("values") or {}
                sets, params = [], []
                for c, v in vals.items():
                    col = _require_ident(c, "列名")
                    if col not in names:
                        raise ConnError("列不存在：" + col)
                    if col in gen_cols:
                        raise ConnError("生成列不能修改：" + col)
                    sets.append('"%s" = ?' % col)
                    params.append(v)
                if not sets:
                    continue
                where, wparams = locator(item.get("key"))
                cur = conn.execute('UPDATE "%s" SET %s WHERE %s' % (tname, ", ".join(sets), where),
                                   params + wparams)
                if cur.rowcount != 1:
                    raise ConnError("修改没有定位到唯一一行，已取消本次保存")
                counts["updated"] += cur.rowcount

            for item in (inserts or []):
                item = item or {}
                vals = item.get("values") or {}
                cols, params = [], []
                for c, v in vals.items():
                    col = _require_ident(c, "列名")
                    if col not in names:
                        raise ConnError("列不存在：" + col)
                    if col in gen_cols:
                        continue
                    cols.append(col)
                    params.append(v)
                if cols:
                    conn.execute('INSERT INTO "%s" (%s) VALUES (%s)' %
                                 (tname, ", ".join('"%s"' % c for c in cols),
                                  ", ".join("?" * len(cols))), params)
                else:
                    conn.execute('INSERT INTO "%s" DEFAULT VALUES' % tname)
                counts["inserted"] += 1
            conn.commit()
            return {"ok": True, **counts}
        except sqlite3.DatabaseError as e:
            conn.rollback()
            raise ConnError(_friendly(e))
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def batch_update(self, schema, table, column, where="", mode="set",
                     value=None, find="", replace="", dry_run=True, exact=False):
        """批量改值：set 把选中列统一改成 value；replace 在列内做查找替换

        只更新真正命中的行：replace 默认按整值匹配（数字列更符合直觉），
        exact=False 时按子串替换；命中口径与 batch_preview 完全一致。
        """
        if self.readonly:
            raise ConnError("当前数据源是只读模式")
        tname = _require_ident(table, "表名")
        mode = str(mode or "set").strip().lower()
        if mode not in ("set", "replace"):
            raise ConnError("批量修改方式只支持 set / replace")
        if mode == "replace" and find == "":
            raise ConnError("查找内容不能为空，否则会在每行前插入替换文本")
        cond = _where_clause(where)
        tail = (" WHERE " + cond) if cond else ""
        conn = self._conn(guard=False)
        try:
            info_rows = conn.execute('PRAGMA table_xinfo("%s")' % tname).fetchall()
            if not info_rows:
                raise ConnError("表不存在：" + tname)
            names = [r[1] for r in info_rows]
            want = str(column or "").strip()
            col = want if want in names else _require_ident(want, "列名")
            if col not in names:
                raise ConnError("列不存在：" + want)
            if any(r[1] == col and len(r) > 6 and r[6] in (2, 3) for r in info_rows):
                raise ConnError("生成列不能直接修改")
            qcol = '"%s"' % col.replace('"', '""')
            total = int(conn.execute('SELECT COUNT(*) FROM "%s"%s' % (tname, tail)).fetchone()[0])
            # 查找内容与替换内容一样时不会有变化，直接跳过
            if mode == "replace" and find == replace:
                return {"dry_run": bool(dry_run), "total": total, "matched": 0, "rowcount": 0}
            if mode == "replace":
                if exact:
                    hit, hparams = "%s = ?" % qcol, [find]
                else:
                    hit, hparams = "instr(CAST(%s AS TEXT), ?) > 0" % qcol, [find]
            else:
                hit, hparams = "NOT (%s IS ?)" % qcol, [value]
            htail = (" WHERE " + cond + " AND " + hit) if cond else (" WHERE " + hit)
            matched = int(conn.execute('SELECT COUNT(*) FROM "%s"%s' % (tname, htail),
                                       hparams).fetchone()[0])
            if dry_run:
                return {"dry_run": True, "total": total, "matched": matched, "rowcount": 0}
            if mode == "replace" and exact:
                cur = conn.execute('UPDATE "%s" SET %s = ?%s' % (tname, qcol, htail),
                                   [replace] + hparams)
            elif mode == "replace":
                cur = conn.execute('UPDATE "%s" SET %s = REPLACE(CAST(%s AS TEXT), ?, ?)%s'
                                   % (tname, qcol, qcol, htail), [find, replace] + hparams)
            else:
                cur = conn.execute('UPDATE "%s" SET %s = ?%s' % (tname, qcol, htail),
                                   [value] + hparams)
            changed = cur.rowcount
            conn.commit()
            return {"dry_run": False, "total": total, "matched": matched, "rowcount": changed}
        except sqlite3.DatabaseError as e:
            conn.rollback()
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def batch_preview(self, schema, table, column, where="", mode="set",
                      value=None, find="", replace="", limit=50, exact=False):
        """批量修改前先看命中行：只回传真正会变化的行（改前 → 改后）"""
        tname = _require_ident(table, "表名")
        mode = str(mode or "set").strip().lower()
        if mode not in ("set", "replace"):
            raise ConnError("批量修改方式只支持 set / replace")
        if mode == "replace" and find == "":
            raise ConnError("查找内容不能为空，否则会在每行前插入替换文本")
        cond = _where_clause(where)
        conn = self._conn(guard=False)
        try:
            info_rows = conn.execute('PRAGMA table_xinfo("%s")' % tname).fetchall()
            if not info_rows:
                raise ConnError("表不存在：" + tname)
            names = [r[1] for r in info_rows]
            want = str(column or "").strip()
            col = want if want in names else _require_ident(want, "列名")
            if col not in names:
                raise ConnError("列不存在：" + want)
            if any(r[1] == col and len(r) > 6 and r[6] in (2, 3) for r in info_rows):
                raise ConnError("生成列不能直接修改")
            pk_cols = [r[1] for r in info_rows if len(r) > 5 and r[5]]
            qcol = '"%s"' % col.replace('"', '""')
            base = (" WHERE " + cond) if cond else ""
            total = int(conn.execute('SELECT COUNT(*) FROM "%s"%s' % (tname, base)).fetchone()[0])
            if mode == "replace" and find == replace:
                return {"total": total, "matched": 0, "shown": 0, "truncated": False, "rows": []}
            # 命中条件：replace 默认整值匹配，exact=False 时按子串（与 REPLACE 一样区分大小写）
            if mode == "replace":
                if exact:
                    hit, hparams = "%s = ?" % qcol, [find]
                else:
                    hit, hparams = "instr(CAST(%s AS TEXT), ?) > 0" % qcol, [find]
            else:
                hit, hparams = "NOT (%s IS ?)" % qcol, [value]
            htail = (" WHERE " + cond + " AND " + hit) if cond else (" WHERE " + hit)
            matched = int(conn.execute('SELECT COUNT(*) FROM "%s"%s' % (tname, htail), hparams).fetchone()[0])
            use_rowid = False
            if not pk_cols:
                try:
                    conn.execute('SELECT rowid FROM "%s" LIMIT 1' % tname).fetchone()
                    use_rowid = True
                except sqlite3.DatabaseError:
                    use_rowid = False
            sel = 'rowid AS "__dbx_rowid", *' if use_rowid else "*"
            limit = max(1, min(int(limit or 50), 200))
            cur = conn.execute('SELECT %s FROM "%s"%s LIMIT ?' % (sel, tname, htail),
                               hparams + [limit])
            dcols = [d[0] for d in cur.description]
            out_cols = dcols[1:] if use_rowid else dcols
            fetched = cur.fetchall()
            ci = dcols.index(col) if col in dcols else -1
            out = []
            for r in fetched:
                row_vals = list(r[1:]) if use_rowid else list(r)
                if use_rowid:
                    key = {"__rowid__": _jsonable(r[0])}
                elif pk_cols:
                    idx = {c: dcols.index(c) for c in pk_cols if c in dcols}
                    key = {c: _jsonable(r[idx[c]]) for c in idx} or None
                else:
                    key = None
                before = _jsonable(r[ci]) if ci >= 0 else None
                if mode == "replace":
                    if exact:
                        after = replace
                    else:
                        after = None if before is None else str(before).replace(find, replace)
                else:
                    after = _jsonable(value)
                out.append({"key": key, "before": before, "after": after,
                            "row": [_jsonable(v) for v in row_vals]})
            return {"total": total, "matched": matched, "shown": len(out),
                    "truncated": matched > len(out), "columns": out_cols, "rows": out}
        except sqlite3.DatabaseError as e:
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def backup_table(self, table, schema=None):
        """整表备份成 SQL：建表语句 + 索引/触发器 + INSERT，可直接粘回 SQL 页签恢复"""
        tname = _require_ident(table, "表名")
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (tname,)).fetchone()
            if not row:
                raise ConnError("表不存在：" + tname)
            ddl = (row[0] or "").strip()
            if not ddl:
                raise ConnError("读不到建表语句，无法备份：" + tname)
            extras = [r[0].strip() for r in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type IN ('index','trigger') AND tbl_name=? AND sql IS NOT NULL",
                (tname,)).fetchall() if (r[0] or "").strip()]
            info = conn.execute('PRAGMA table_xinfo("%s")' % tname).fetchall()
            # 生成列不能手动插入，备份时只搬普通列
            cols = [r[1] for r in info if not (len(r) > 6 and r[6] in (2, 3))]
            if not cols:
                raise ConnError("表里没有可导出的列")
            qcols = ", ".join('"%s"' % c.replace('"', '""') for c in cols)
            lines = ["-- %s 备份  %s" % (tname, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                     ddl + ";"]
            lines.extend(x + ";" for x in extras)
            n, truncated = 0, False
            cur = conn.execute('SELECT %s FROM "%s"' % (qcols, tname))
            for r in cur:
                lines.append('INSERT INTO "%s" (%s) VALUES (%s);' %
                             (tname.replace('"', '""'), qcols, ", ".join(_sqlite_literal(v) for v in r)))
                n += 1
                if n >= _BACKUP_MAX_ROWS:
                    truncated = True
                    break
            return {"table": tname, "rows": n, "truncated": truncated,
                    "sql": "\n".join(lines) + "\n"}
        except sqlite3.DatabaseError as e:
            raise ConnError(_friendly(e))
        finally:
            conn.close()


# ── MySQL ──────────────────────────────

class MySQLConnector:
    kind = "mysql"

    def __init__(self, src, data_dir, readonly=None):
        self.src = src
        self.data_dir = data_dir
        self.readonly = src.get("readonly", True) if readonly is None else bool(readonly)

    def _connect(self, database=None):
        try:
            import pymysql
        except ImportError:
            raise ConnError("未安装 PyMySQL，请先执行：pip install PyMySQL")
        try:
            return pymysql.connect(
                host=self.src.get("host") or "127.0.0.1",
                port=int(self.src.get("port") or 3306),
                user=self.src.get("user") or "root",
                password=decrypt_password(self.src.get("password_enc")),
                database=database if database is not None else (self.src.get("database") or None),
                charset="utf8mb4",
                connect_timeout=6,
                read_timeout=60,
                write_timeout=30,
                autocommit=True,
                cursorclass=pymysql.cursors.Cursor,
            )
        except Exception as e:
            raise ConnError(_friendly(e))

    def test(self):
        started = time.time()
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION()")
                ver = cur.fetchone()[0]
            return {"ok": True, "server": "MySQL " + str(ver),
                    "elapsed_ms": int((time.time() - started) * 1000)}
        except Exception as e:
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def list_schemas(self):
        conn = self._connect(database=None)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW DATABASES")
                names = [r[0] for r in cur.fetchall()]
            # 系统库默认不展示，减少噪声
            return [n for n in names if n.lower() not in
                    ("information_schema", "mysql", "performance_schema", "sys")]
        except Exception as e:
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def list_tables(self, schema=None):
        sc = _mysql_ident(schema or self.src.get("database") or "", "库名")
        conn = self._connect(database=None)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SHOW TABLES FROM `{sc}`")
                return [r[0] for r in cur.fetchall()]
        except Exception as e:
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def list_columns(self, schema, table):
        sc = _mysql_ident(schema or self.src.get("database") or "", "库名")
        tb = _mysql_ident(table, "表名")
        conn = self._connect(database=None)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SHOW FULL COLUMNS FROM `{sc}`.`{tb}`")
                rows = cur.fetchall()
            out = []
            for r in rows:
                # Field, Type, Collation, Null, Key, Default, Extra, Privileges, Comment
                out.append({
                    "name": r[0],
                    "type": str(r[1]),
                    "pk": str(r[4]).upper() == "PRI",
                    "notnull": str(r[3]).upper() == "NO",
                    "default": r[5],
                    "comment": (r[8] or "") if len(r) > 8 else "",
                })
            return out
        except Exception as e:
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def table_info(self, schema, table):
        """表结构 + 表注释 + 字段注释，一次连接拿全"""
        sc = _mysql_ident(schema or self.src.get("database") or "", "库名")
        tb = _mysql_ident(table, "表名")
        conn = self._connect(database=None)
        try:
            cols = []
            with conn.cursor() as cur:
                cur.execute(f"SHOW FULL COLUMNS FROM `{sc}`.`{tb}`")
                for r in cur.fetchall():
                    cols.append({
                        "name": r[0],
                        "type": str(r[1]),
                        "pk": str(r[4]).upper() == "PRI",
                        "notnull": str(r[3]).upper() == "NO",
                        "default": r[5],
                        "comment": (r[8] or "") if len(r) > 8 else "",
                    })
            comment = ""
            try:
                # 表注释在 information_schema，个别账号无权限，拿不到就留空
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT TABLE_COMMENT FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (sc, tb))
                    row = cur.fetchone()
                comment = (row[0] or "").strip() if row else ""
            except Exception:
                comment = ""
            ddl = ""
            try:
                # 「表定义」页要展示建表语句，账号无权限就不展示
                with conn.cursor() as cur:
                    cur.execute("SHOW CREATE TABLE `%s`.`%s`" % (sc, tb))
                    row = cur.fetchone()
                ddl = str(row[1]) if row and len(row) > 1 else ""
            except Exception:
                ddl = ""
            return {"table": table, "comment": comment, "columns": cols, "ddl": ddl, "indexes": []}
        except Exception as e:
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def table_data(self, table, limit=100, offset=0, where="", schema=None, order=None):
        """点表名直接看数据：默认 100 条，支持 WHERE 筛选、排序与总数统计"""
        sc = _mysql_ident(schema or self.src.get("database") or "", "库名")
        tb = _mysql_ident(table, "表名")
        cond = _where_clause(where)
        tail = (" WHERE " + cond) if cond else ""
        conn = self._connect(database=None)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW FULL COLUMNS FROM `%s`.`%s`" % (sc, tb))
                meta = cur.fetchall()
                pk_cols = [r[0] for r in meta if str(r[4]).upper() == "PRI"]
                gen_cols = [r[0] for r in meta if "generated" in str(r[6] or "").lower()]
                # 自增列新增时由数据库生成，前端不给输入框
                auto_cols = [r[0] for r in meta if "auto_increment" in str(r[6] or "").lower()]
                ob = ""
                if order:
                    want = str(order.get("column") or "").strip()
                    names = [r[0] for r in meta]
                    ocol = want if want in names else _require_ident(want, "排序列")
                    if ocol not in names:
                        raise ConnError("列不存在：" + want)
                    ob = " ORDER BY `%s` %s" % (ocol, "DESC" if str(order.get("dir") or "").lower() == "desc" else "ASC")
                cur.execute("SELECT COUNT(*) FROM `%s`.`%s`%s" % (sc, tb, tail))
                total = int(cur.fetchone()[0])
                cur.execute("SELECT * FROM `%s`.`%s`%s%s LIMIT %d OFFSET %d" %
                            (sc, tb, tail, ob, int(limit), int(offset)))
                cols = [d[0] for d in cur.description]
                rows = cur.fetchall()
            keys = []
            if pk_cols:
                idx = {c: cols.index(c) for c in pk_cols if c in cols}
                keys = [{c: _jsonable(r[idx[c]]) for c in pk_cols if c in idx} for r in rows]
            return {"columns": cols,
                    "rows": [[_jsonable(v) for v in r] for r in rows],
                    "keys": keys if keys else None,
                    "editable": bool(pk_cols),
                    "readonly_cols": gen_cols,
                    "auto_cols": auto_cols,
                    "count": len(rows), "total": total,
                    "offset": int(offset), "limit": int(limit)}
        except Exception as e:
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def update_cell(self, schema, table, rowkey, column, value):
        """双击改单元格：只允许按主键精确更新一行（没主键不猜）"""
        if self.readonly:
            raise ConnError("当前数据源是只读模式，请先打开「可写」开关")
        sc = _mysql_ident(schema or self.src.get("database") or "", "库名")
        tb = _mysql_ident(table, "表名")
        col = _require_ident(column, "列名")
        key = rowkey if isinstance(rowkey, dict) else {}
        conn = self._connect(database=None)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW FULL COLUMNS FROM `%s`.`%s`" % (sc, tb))
                meta = cur.fetchall()
            names = {r[0] for r in meta}
            if col not in names:
                raise ConnError("列不存在：" + col)
            if any(r[0] == col and "generated" in str(r[6] or "").lower() for r in meta):
                raise ConnError("生成列不能直接修改")
            pk_cols = [r[0] for r in meta if str(r[4]).upper() == "PRI"]
            if not pk_cols:
                raise ConnError("该表没有主键，无法定位要修改的行")
            missing = [c for c in pk_cols if c not in key]
            if missing:
                raise ConnError("缺少主键定位值：" + "、".join(missing))
            extra = [c for c in key if c not in pk_cols]
            if extra:
                raise ConnError("定位条件里含非主键列：" + "、".join(extra))
            where = " AND ".join("`%s` = %%s" % _require_ident(c, "主键列") for c in pk_cols)
            sql = "UPDATE `%s`.`%s` SET `%s` = %%s WHERE %s LIMIT 1" % (sc, tb, col, where)
            with conn.cursor() as cur:
                cur.execute(sql, [value] + [key[c] for c in pk_cols])
                changed = cur.rowcount
            return {"ok": True, "rowcount": changed}
        except ConnError:
            raise
        except Exception as e:
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def save_rows(self, schema, table, updates=None, inserts=None, deletes=None):
        """数据页暂存编辑一次提交：删除 / 修改 / 新增在同一个事务里完成

        行定位按主键；新增行只提交用户填过的列，没填的列交给库里的默认值。
        """
        if self.readonly:
            raise ConnError("当前数据源是只读模式")
        sc = _mysql_ident(schema or self.src.get("database") or "", "库名")
        tb = _mysql_ident(table, "表名")
        target = "`%s`.`%s`" % (sc, tb)
        conn = self._connect(database=None)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW FULL COLUMNS FROM %s" % target)
                meta = cur.fetchall()
            names = [r[0] for r in meta]
            gen_cols = {r[0] for r in meta if "generated" in str(r[6] or "").lower()}
            pk_cols = [r[0] for r in meta if str(r[4]).upper() == "PRI"]

            def locator(key):
                key = key if isinstance(key, dict) else {}
                if not pk_cols:
                    raise ConnError("该表没有主键，无法定位要修改的行")
                missing = [c for c in pk_cols if c not in key]
                if missing:
                    raise ConnError("缺少主键定位值：" + "、".join(missing))
                extra = [c for c in key if c not in pk_cols]
                if extra:
                    raise ConnError("定位条件里含非主键列：" + "、".join(extra))
                return (" AND ".join("`%s` = %%s" % _require_ident(c, "主键列") for c in pk_cols),
                        [key[c] for c in pk_cols])

            conn.begin()
            counts = {"inserted": 0, "updated": 0, "deleted": 0}
            with conn.cursor() as cur:
                for key in (deletes or []):
                    where, params = locator(key)
                    cur.execute("DELETE FROM %s WHERE %s" % (target, where), params)
                    if cur.rowcount != 1:
                        raise ConnError("删除没有定位到唯一一行，已取消本次保存")
                    counts["deleted"] += cur.rowcount

                for item in (updates or []):
                    item = item or {}
                    vals = item.get("values") or {}
                    sets, params = [], []
                    for c, v in vals.items():
                        col = _require_ident(c, "列名")
                        if col not in names:
                            raise ConnError("列不存在：" + col)
                        if col in gen_cols:
                            raise ConnError("生成列不能修改：" + col)
                        sets.append("`%s` = %%s" % col)
                        params.append(v)
                    if not sets:
                        continue
                    where, wparams = locator(item.get("key"))
                    cur.execute("UPDATE %s SET %s WHERE %s" % (target, ", ".join(sets), where),
                                params + wparams)
                    if cur.rowcount == 0:
                        # MySQL 默认返回变化行数：值与原值相同时 rowcount 为 0，先确认行是否存在
                        with conn.cursor() as chk:
                            chk.execute("SELECT COUNT(*) FROM %s WHERE %s" % (target, where), wparams)
                            exists = int(chk.fetchone()[0])
                        if not exists:
                            raise ConnError("修改没有定位到唯一一行，已取消本次保存")
                        continue
                    if cur.rowcount != 1:
                        raise ConnError("修改定位到多行，已取消本次保存")
                    counts["updated"] += cur.rowcount

                for item in (inserts or []):
                    item = item or {}
                    vals = item.get("values") or {}
                    cols, params = [], []
                    for c, v in vals.items():
                        col = _require_ident(c, "列名")
                        if col not in names:
                            raise ConnError("列不存在：" + col)
                        if col in gen_cols:
                            continue
                        cols.append(col)
                        params.append(v)
                    if cols:
                        cur.execute("INSERT INTO %s (%s) VALUES (%s)" %
                                    (target, ", ".join("`%s`" % c for c in cols),
                                     ", ".join(["%s"] * len(cols))), params)
                    else:
                        cur.execute("INSERT INTO %s () VALUES ()" % target)
                    counts["inserted"] += 1
            conn.commit()
            return {"ok": True, **counts}
        except ConnError:
            conn.rollback()
            raise
        except Exception as e:
            conn.rollback()
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def batch_update(self, schema, table, column, where="", mode="set",
                     value=None, find="", replace="", dry_run=True, exact=False):
        """批量改值：set 统一改成 value；replace 只更新命中行（exact 整值 / 否则子串）"""
        if self.readonly:
            raise ConnError("当前数据源是只读模式")
        sc = _mysql_ident(schema or self.src.get("database") or "", "库名")
        tb = _mysql_ident(table, "表名")
        mode = str(mode or "set").strip().lower()
        if mode not in ("set", "replace"):
            raise ConnError("批量修改方式只支持 set / replace")
        if mode == "replace" and find == "":
            raise ConnError("查找内容不能为空，否则会在每行前插入替换文本")
        cond = _where_clause(where)
        tail = (" WHERE " + cond) if cond else ""
        target = "`%s`.`%s`" % (sc, tb)
        conn = self._connect(database=None)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW FULL COLUMNS FROM `%s`.`%s`" % (sc, tb))
                meta = cur.fetchall()
                names = [r[0] for r in meta]
                want = str(column or "").strip()
                col = want if want in names else _require_ident(want, "列名")
                if col not in names:
                    raise ConnError("列不存在：" + want)
                if any(r[0] == col and "generated" in str(r[6] or "").lower() for r in meta):
                    raise ConnError("生成列不能直接修改")
                cur.execute("SELECT COUNT(*) FROM %s%s" % (target, tail))
                total = int(cur.fetchone()[0])
                if mode == "replace" and find == replace:
                    return {"dry_run": bool(dry_run), "total": total, "matched": 0, "rowcount": 0}
                if mode == "replace":
                    if exact:
                        hit, hparams = "`%s` = %%s" % col, [find]
                    else:
                        hit, hparams = "LOCATE(%%s, CAST(`%s` AS CHAR)) > 0" % col, [find]
                else:
                    hit, hparams = "NOT (`%s` <=> %%s)" % col, [value]
                htail = (" WHERE " + cond + " AND " + hit) if cond else (" WHERE " + hit)
                cur.execute("SELECT COUNT(*) FROM %s%s" % (target, htail), hparams)
                matched = int(cur.fetchone()[0])
                if dry_run:
                    return {"dry_run": True, "total": total, "matched": matched, "rowcount": 0}
                if mode == "replace" and exact:
                    cur.execute("UPDATE %s SET `%s` = %%s%s" % (target, col, htail),
                                [replace] + hparams)
                elif mode == "replace":
                    cur.execute("UPDATE %s SET `%s` = REPLACE(CAST(`%s` AS CHAR), %%s, %%s)%s"
                                % (target, col, col, htail), [find, replace] + hparams)
                else:
                    cur.execute("UPDATE %s SET `%s` = %%s%s" % (target, col, htail),
                                [value] + hparams)
                changed = cur.rowcount
            return {"dry_run": False, "total": total, "matched": matched, "rowcount": changed}
        except ConnError:
            raise
        except Exception as e:
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def batch_preview(self, schema, table, column, where="", mode="set",
                      value=None, find="", replace="", limit=50, exact=False):
        """批量修改前先看命中行：只回传真正会变化的行（改前 → 改后）"""
        sc = _mysql_ident(schema or self.src.get("database") or "", "库名")
        tb = _mysql_ident(table, "表名")
        mode = str(mode or "set").strip().lower()
        if mode not in ("set", "replace"):
            raise ConnError("批量修改方式只支持 set / replace")
        if mode == "replace" and find == "":
            raise ConnError("查找内容不能为空，否则会在每行前插入替换文本")
        cond = _where_clause(where)
        target = "`%s`.`%s`" % (sc, tb)
        conn = self._connect(database=None)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW FULL COLUMNS FROM `%s`.`%s`" % (sc, tb))
                meta = cur.fetchall()
                names = [r[0] for r in meta]
                want = str(column or "").strip()
                col = want if want in names else _require_ident(want, "列名")
                if col not in names:
                    raise ConnError("列不存在：" + want)
                if any(r[0] == col and "generated" in str(r[6] or "").lower() for r in meta):
                    raise ConnError("生成列不能直接修改")
                pk_cols = [r[0] for r in meta if str(r[4]).upper() == "PRI"]
                tail = (" WHERE " + cond) if cond else ""
                cur.execute("SELECT COUNT(*) FROM %s%s" % (target, tail))
                total = int(cur.fetchone()[0])
                if mode == "replace" and find == replace:
                    return {"total": total, "matched": 0, "shown": 0, "truncated": False, "rows": []}
                # 命中条件：replace 默认整值匹配，exact=False 时按子串
                if mode == "replace":
                    if exact:
                        hit, hparams = "`%s` = %%s" % col, [find]
                    else:
                        hit, hparams = "LOCATE(%%s, CAST(`%s` AS CHAR)) > 0" % col, [find]
                else:
                    hit, hparams = "NOT (`%s` <=> %%s)" % col, [value]
                htail = (" WHERE " + cond + " AND " + hit) if cond else (" WHERE " + hit)
                cur.execute("SELECT COUNT(*) FROM %s%s" % (target, htail), hparams)
                matched = int(cur.fetchone()[0])
                limit = max(1, min(int(limit or 50), 200))
                cur.execute("SELECT * FROM %s%s LIMIT %d" % (target, htail, limit), hparams)
                dcols = [d[0] for d in cur.description]
                fetched = cur.fetchall()
                ci = dcols.index(col) if col in dcols else -1
                out = []
                for r in fetched:
                    key = None
                    if pk_cols:
                        idx = {c: dcols.index(c) for c in pk_cols if c in dcols}
                        key = {c: _jsonable(r[idx[c]]) for c in idx} or None
                    before = _jsonable(r[ci]) if ci >= 0 else None
                    if mode == "replace":
                        if exact:
                            after = replace
                        else:
                            after = None if before is None else str(before).replace(find, replace)
                    else:
                        after = _jsonable(value)
                    out.append({"key": key, "before": before, "after": after,
                                "row": [_jsonable(v) for v in r]})
                return {"total": total, "matched": matched, "shown": len(out),
                        "truncated": matched > len(out), "columns": dcols, "rows": out}
        except ConnError:
            raise
        except Exception as e:
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def backup_table(self, table, schema=None):
        """整表备份成 SQL：SHOW CREATE TABLE + INSERT，可直接粘回 SQL 页签恢复"""
        sc = _mysql_ident(schema or self.src.get("database") or "", "库名")
        tb = _mysql_ident(table, "表名")
        conn = self._connect(database=None)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW CREATE TABLE `%s`.`%s`" % (sc, tb))
                row = cur.fetchone()
                ddl = (row[1] or "").strip() if row and len(row) > 1 else ""
                if not ddl:
                    raise ConnError("读不到建表语句，无法备份：" + tb)
                cur.execute("SHOW FULL COLUMNS FROM `%s`.`%s`" % (sc, tb))
                # 生成列不能手动插入，备份时只搬普通列
                cols = [r[0] for r in cur.fetchall() if "generated" not in str(r[6] or "").lower()]
                if not cols:
                    raise ConnError("表里没有可导出的列")
                qcols = ", ".join("`%s`" % c.replace("`", "``") for c in cols)
                lines = ["-- %s.%s 备份  %s" % (sc, tb, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                         ddl + ";"]
                cur.execute("SELECT %s FROM `%s`.`%s`" % (qcols, sc, tb))
                n, truncated = 0, False
                while not truncated:
                    rows = cur.fetchmany(500)
                    if not rows:
                        break
                    for r in rows:
                        lines.append("INSERT INTO `%s` (%s) VALUES (%s);" %
                                     (tb, qcols, ", ".join(_mysql_literal(v) for v in r)))
                        n += 1
                        if n >= _BACKUP_MAX_ROWS:
                            truncated = True
                            break
            return {"table": tb, "rows": n, "truncated": truncated,
                    "sql": "\n".join(lines) + "\n"}
        except ConnError:
            raise
        except Exception as e:
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def alter_column(self, schema, table, data):
        """改表定义：add / modify（可改名，也能改类型、非空、默认值）/ drop"""
        if self.readonly:
            raise ConnError("当前数据源是只读模式，请先打开「可写」开关")
        sc = _mysql_ident(schema or self.src.get("database") or "", "库名")
        tb = _mysql_ident(table, "表名")
        action = str(data.get("action") or "").strip().lower()
        col = data.get("column") or {}
        old_name = str(data.get("old_name") or col.get("name") or "").strip()
        target = "`%s`.`%s`" % (sc, tb)
        conn = self._connect(database=None)
        try:
            if action == "add":
                cname = _require_ident(col.get("name"), "列名")
                ddl = "`%s` %s" % (cname, _safe_col_type(col.get("type")))
                if col.get("notnull"):
                    ddl += " NOT NULL"
                if col.get("default") not in (None, ""):
                    ddl += " DEFAULT " + _sql_literal(col.get("default"))
                with conn.cursor() as cur:
                    cur.execute("ALTER TABLE %s ADD COLUMN %s" % (target, ddl))
                return {"ok": True, "table": tb, "column": cname, "action": "add"}

            if action == "drop":
                cname = _require_ident(old_name, "列名")
                with conn.cursor() as cur:
                    cur.execute("ALTER TABLE %s DROP COLUMN `%s`" % (target, cname))
                return {"ok": True, "table": tb, "action": "drop"}

            if action not in ("modify", "rename"):
                raise ConnError("不支持的改表操作：" + (action or "空"))

            cname = _require_ident(old_name, "原列名")
            new_name = _require_ident(col.get("name") or cname, "列名")
            # CHANGE 会整列重写，先把原定义读出来，用户没改的部分照旧
            old = None
            with conn.cursor() as cur:
                cur.execute("SHOW FULL COLUMNS FROM %s" % target)
                for r in cur.fetchall():
                    if str(r[0]).lower() == cname.lower():
                        old = r
                        break
            if old is None:
                raise ConnError("表里没有这一列：" + cname)
            # 不能 upper：ENUM/SET 的取值大小写是列定义的一部分
            old_type = str(old[1])
            old_collation = str(old[2]) if len(old) > 2 and old[2] else ""
            old_nullable = str(old[3]).upper() == "YES"
            old_default = old[5]
            extra = str(old[6] or "")
            comment = (old[8] or "") if len(old) > 8 else ""
            raw_type = str(col.get("type") or "").strip()
            if not raw_type:
                typ = old_type
            else:
                typ = _safe_col_type(raw_type, old_type)
                # 类型没改时直接用库里的原值，避免 ENUM/SET 取值大小写被改写
                if re.sub(r"\s+", "", typ).lower() == re.sub(r"\s+", "", old_type).lower():
                    typ = old_type
            notnull = (not old_nullable) if col.get("notnull") is None else bool(col.get("notnull"))
            # 默认值留空 = 保持原默认；要彻底去掉默认值可显式填 NULL
            using_old = col.get("default") in (None, "")
            default = old_default if using_old else col.get("default")
            is_auto = "auto_increment" in extra.lower()
            ddl = "`%s` `%s` %s" % (cname, new_name, typ)
            if old_collation and not re.search(r"(?i)character\s+set|charset|collate", typ):
                # CHANGE 会重建列，不带上原 COLLATE 会把列的排序规则改成表默认
                if re.search(r"(?i)(char|text|enum|set)", typ):
                    ddl += " COLLATE " + re.sub(r"[^A-Za-z0-9_]", "", old_collation)
            ddl += " NOT NULL" if notnull else " NULL"
            if is_auto:
                ddl += " AUTO_INCREMENT"
            elif default is not None:
                ddl += " DEFAULT " + (_mysql_default_literal(default) if using_old else _sql_literal(default))
            elif not notnull:
                ddl += " DEFAULT NULL"
            m = re.search(r"on update\s+[A-Za-z_()0-9 ]+", extra, re.I)
            if m:
                ddl += " " + re.sub(r"\s+", " ", m.group(0)).strip().upper()
            if comment:
                ddl += " COMMENT '" + str(comment).replace("\\", "\\\\").replace("'", "''") + "'"
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE %s CHANGE COLUMN %s" % (target, ddl))
            return {"ok": True, "table": tb, "column": new_name, "action": "modify"}
        except ConnError:
            raise
        except Exception as e:
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def query(self, sql, limit=5000, schema=None):
        stmts = split_statements(sql)
        if not stmts:
            raise ConnError("请输入 SQL 语句")
        if self.readonly:
            for s in stmts:
                if not is_readonly_sql(s):
                    raise ConnError("当前数据源是只读模式，只允许 SELECT / SHOW / EXPLAIN")
        started = time.time()
        # 必须连到左侧选中的库：MySQL 未选库时执行 from 表名会报 (1046, 'No database selected')
        conn = self._connect(database=(schema or None))
        try:
            results = []
            capped = False
            with conn.cursor() as cur:
                # 多条语句按顺序执行，每条有结果集的都单独收好，前端按「第 N 条」分别展示
                for idx, raw in enumerate(stmts, 1):
                    cur.execute(raw)
                    if cur.description is None:
                        continue
                    if len(results) >= _QUERY_MAX_RESULT_SETS:
                        capped = True
                        continue
                    cols = [d[0] for d in cur.description]
                    rows = cur.fetchmany(limit + 1)
                    truncated = len(rows) > limit
                    if truncated:
                        rows = rows[:limit]
                    results.append({
                        "index": idx,
                        "sql": raw.strip(),
                        "columns": cols,
                        "rows": [[_jsonable(v) for v in r] for r in rows],
                        "count": len(rows),
                        "truncated": truncated,
                    })
            if not results:
                raise ConnError("语句已执行，但没有可展示的结果集")
            result = dict(results[-1])
            result["results"] = results
            result["capped"] = capped
            result["statements"] = len(stmts)
            result["elapsed_ms"] = int((time.time() - started) * 1000)
            return result
        except ConnError:
            raise
        except Exception as e:
            raise ConnError(_friendly(e))
        finally:
            conn.close()

    def execute(self, sql, schema=None):
        if self.readonly:
            raise ConnError("当前数据源是只读模式，请先打开「可写」开关")
        stmts = split_statements(sql)
        if not stmts:
            raise ConnError("请输入 SQL 语句")
        conn = self._connect(database=(schema or None))
        idx = 0
        try:
            total, lastrowid = 0, None
            with conn.cursor() as cur:
                for idx, s in enumerate(stmts, 1):
                    cur.execute(s)
                    if isinstance(cur.rowcount, int) and cur.rowcount > 0:
                        total += cur.rowcount
                    lastrowid = cur.lastrowid
            return {"ok": True, "rowcount": total, "statements": len(stmts), "lastrowid": lastrowid}
        except Exception as e:
            msg = _friendly(e)
            if len(stmts) > 1:
                msg += "（第 %d 条语句失败）" % idx
            raise ConnError(msg)
        finally:
            conn.close()


def open_connector(src, data_dir, readonly=None):
    if src.get("type") == "sqlite":
        return SQLiteConnector(src, data_dir, readonly)
    if src.get("type") == "mysql":
        return MySQLConnector(src, data_dir, readonly)
    raise ConnError("不支持的数据源类型：" + str(src.get("type")))
