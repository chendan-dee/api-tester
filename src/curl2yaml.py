#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
curl2yaml.py — curl 命令解析器（供表单化接口测试工具填表使用）

用法：
  import curl2yaml as C
  report = C.parse_curl_report(curl_str)   # 解析结果（url/method/headers/body/timeout/warnings）
  step   = C.build_config(...)             # 组装成 step 结构字典

支持的 curl 选项：
  --url / --url-query / --request-target / -X / -H / -b(--cookie) / -d(--data*) /
  --json / -F / -A / -e / -u / --oauth2-bearer / -G
  --opt value 与 --opt=value 两种写法都认；
  兼容 bash(\\) 、cmd(^) 、PowerShell(`) 三种续行与三种引号风格；
  HTTP/2 伪头（:authority 等）与 Content-Length 会被丢弃；
  下发给 curl 自己的选项（代理 / 证书 / 重试等）解析后不影响报文；
  无法静态还原的（@文件、-T 上传、--aws-sigv4 签名、--next 多请求）一律给警告，不静默丢弃。
"""
import base64
import importlib
import json
import re
from urllib.parse import quote, urlsplit

# ══════════════════════════════════════════════════════
#  curl 选项表
# ══════════════════════════════════════════════════════
# 带取值的选项：{选项名: 取值处理方式}
#   header / header:<头名> / data / data-urlencode / json / form
#   method / url / cookie / auth / ignore(与报文无关，取值忽略)
_ARG_OPTS = {
    # ── 会进报文：header / body / method / url ──
    '-H': 'header', '--header': 'header',
    '-d': 'data', '--data': 'data', '--data-raw': 'data',
    '--data-binary': 'data', '--data-ascii': 'data',
    '--data-urlencode': 'data-urlencode',
    '--json': 'json',
    '-F': 'form', '--form': 'form', '--form-string': 'form',
    '-X': 'method', '--request': 'method',
    '--url': 'url', '--url-query': 'url_query',
    '--request-target': 'request_target',
    '-b': 'cookie', '--cookie': 'cookie',
    '-A': 'header:user-agent', '--user-agent': 'header:user-agent',
    '-e': 'header:referer', '--referer': 'header:referer',
    '-u': 'auth', '--user': 'auth',
    '--oauth2-bearer': 'oauth2_bearer',
    # ── 无法静态还原，只能给警告 ──
    '--aws-sigv4': 'warn',
    '-T': 'upload', '--upload-file': 'upload',
    # ── 下发/连接类选项，与请求报文无关，取值直接忽略 ──
    '-o': 'ignore', '--output': 'ignore', '--output-dir': 'ignore',
    '-D': 'ignore', '--dump-header': 'ignore',
    '-c': 'ignore', '--cookie-jar': 'ignore',
    '-x': 'warn_env', '--proxy': 'warn_env', '--proxy-user': 'ignore',
    '-U': 'ignore', '--proxy-header': 'ignore', '--noproxy': 'ignore',
    '--preproxy': 'ignore', '--proxy1.0': 'ignore',
    '--socks4': 'ignore', '--socks4a': 'ignore',
    '--socks5': 'ignore', '--socks5-hostname': 'ignore',
    '--proxy-cacert': 'ignore', '--proxy-cert': 'ignore', '--proxy-key': 'ignore',
    '--proxy-ciphers': 'ignore', '--proxy-pass': 'ignore',
    '-K': 'ignore', '--config': 'ignore',
    '-m': 'timeout', '--max-time': 'timeout', '--connect-timeout': 'ignore',
    '--retry': 'ignore', '--retry-delay': 'ignore', '--retry-max-time': 'ignore',
    '--expect100-timeout': 'ignore', '--keepalive-time': 'ignore',
    '--connect-to': 'warn_env', '--happy-eyeballs-timeout-ms': 'ignore',
    '--dns-servers': 'ignore', '--dns-interface': 'ignore', '--doh-url': 'ignore',
    '--hsts': 'ignore', '--max-filesize': 'ignore',
    '--speed-limit': 'ignore', '--speed-time': 'ignore',
    '-w': 'ignore', '--write-out': 'ignore', '--resolve': 'warn_env',
    '--cacert': 'ignore', '--capath': 'ignore', '--cert': 'warn_env',
    '--key': 'warn_env', '--pass': 'ignore', '--crlfile': 'ignore',
    '--ciphers': 'ignore', '--tls-max': 'ignore', '--cert-type': 'ignore',
    '--key-type': 'ignore', '--pinnedpubkey': 'ignore',
    '--interface': 'ignore', '--limit-rate': 'ignore', '--max-redirs': 'ignore',
    '--unix-socket': 'ignore', '--local-port': 'ignore', '--rate': 'ignore',
    '-r': 'ignore', '--range': 'ignore',
    '--proto': 'ignore', '--proto-redir': 'ignore', '--proto-default': 'ignore',
    '--trace': 'ignore', '--trace-ascii': 'ignore', '--trace-config': 'ignore',
    '--stderr': 'ignore',
    # 断点续传 / 并发 / TLS 参数：不影响请求报文
    '-C': 'ignore', '--continue-at': 'ignore',
    '--parallel-max': 'ignore', '--parallel-max-host': 'ignore',
    '--keepalive-cnt': 'ignore', '--libcurl': 'ignore',
    '--sigalgs': 'ignore', '--tls13-ciphers': 'ignore', '--curves': 'ignore',
    '--tlsuser': 'ignore', '--tlspassword': 'ignore', '--tlsauthtype': 'ignore',
    '--proxy-tlsuser': 'ignore', '--proxy-tlspassword': 'ignore',
    '--proxy-tlsauthtype': 'ignore', '--proxy-tls13-ciphers': 'ignore',
}

# 无取值的选项
_NO_ARG_OPTS = {
    '-L', '--location', '--location-trusted', '--compressed', '-k', '--insecure',
    '-v', '--verbose', '-s', '--silent', '-S', '--show-error', '-i', '--include',
    '-f', '--fail', '--fail-with-body', '-g', '--globoff', '-N', '--no-buffer',
    '-#', '--progress-bar', '-q', '--disable', '--raw', '-4', '-6',
    '--http1.0', '--http1.1', '--http2', '--http2-prior-knowledge',
    '--tlsv1.0', '--tlsv1.1', '--tlsv1.2', '--tlsv1.3',
    '--no-keepalive', '--no-alpn', '--no-npn', '--no-sessionid',
    '--no-progress-meter', '--ssl-no-revoke', '--ssl-reqd', '--path-as-is',
    '-O', '--remote-name', '--remote-name-all', '-J', '--remote-header-name',
    '--basic', '--anyauth',
    '--parallel', '--parallel-immediate', '-Z',
    '--trace-time', '--tcp-nodelay', '--tcp-fastopen',
    '--http3', '--http3-only', '--http0.9',
    '--retry-all-errors', '--retry-connrefused', '--fail-early',
    '--keepalive', '--no-hsts',
    '--styled-output', '--no-styled-output', '--tr-encoding',
    '--disallow-username-in-url', '--proxy-insecure', '--no-clobber',
    # 新版别名 / 纯本地行为
    '--follow', '--show-headers',
    '--post301', '--post302', '--post303',
    '--create-dirs', '--use-ascii', '--compressed-ssh',
    '--ssl', '--sslv2', '--sslv3', '--tlsv1',
    '--proxytunnel', '--proxy-http2',
    '--out-null', '--remote-time',
    # 语义会影响请求，单独处理
    '-G', '--get', '-I', '--head', '--next', '-:',
}

# 无取值但会改变请求认证方式：curl 会做本地认证协商，转换后请求可能因缺认证头而失败
_WARN_NOARG_OPTS = {'--ntlm', '--digest', '--negotiate'}

# 已登记的全部选项名：用来判断“上一个选项是不是漏了取值”
_KNOWN_OPTS = set(_ARG_OPTS) | _NO_ARG_OPTS | _WARN_NOARG_OPTS

# ══════════════════════════════════════════════════════
#  格式签名（不是语义）：短选项是封闭集合，有值/无值可穷举
# ══════════════════════════════════════════════════════
# 来源：curl --help all（curl 8.21）；短选项新增极少，与选项语义无关，
# 只回答一个问题：“这个选项吃不吃下一个 token”。
# 带值：-A -C -D -E -F -H -K -P -Q -T -U -X -Y -b -c -d -e -m -o -r -t -u -w -x -y -z
# 无值：# 0 1 2 3 4 6 : B G I J L M N O R S V Z a f g i j k l n p q s v
# -h (--help) 的参数是可选的，按无值处理，避免 `curl -h <url>` 把 URL 吃掉
_SHORT_ARG = frozenset("ACDEFHKPQTUX" "bcde" "mortuwxyz" "Y")
_SHORT_NOARG = frozenset("#012346:BGIJLMNORSVZafghijklnpqsv")


def _is_option_token(tok):
    """这一项是否是一个已登记的选项名（而不是某个选项的取值）"""
    if tok.startswith("--"):
        return tok.split("=", 1)[0] in _KNOWN_OPTS
    return len(tok) == 2 and tok in _KNOWN_OPTS


# -F 的 mime 属性（;type= / ;filename= / ;headers=），对请求报文无意义，切掉但要提示
_FORM_ATTR_RE = re.compile(r";(?=(?:type|filename|headers)=)")

_ESCAPE_MAP = {'n': '\n', 't': '\t', 'r': '\r'}
# bash ANSI-C 引用 $'...' 的转义表（Postman / 浏览器复制多行 body 时常见）
_ANSI_MAP = {'n': '\n', 't': '\t', 'r': '\r', 'a': '\a', 'b': '\b',
             'f': '\f', 'v': '\v', 'e': '\x1b', '\\': '\\',
             "'": "'", '"': '"', '?': '?'}
_HEADER_LIKE_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+:")
_PSEUDO_HEADER_RE = re.compile(r"^:")
_CMD_INNER_QUOTE_RE = re.compile(r'\\\^"')      # 内层引号 \^" → \"
_CMD_QUOTE_RE = re.compile(r'\^\\?"')          # 包值引号 ^" / ^\" → "
_CMD_ESCAPE_RE = re.compile(r'\^([&|()<>^])')


def _importable(mod):
    try:
        importlib.import_module(mod)
        return True
    except Exception:
        return False


def _supported_encodings():
    """当前环境能解压的 accept-encoding（requests 无 brotli/zstandard 时会解不出响应）"""
    sup = {"gzip", "deflate", "identity"}
    if _importable("brotli") or _importable("brotlicffi"):
        sup.add("br")
    if _importable("zstandard"):
        sup.add("zstd")
    return sup


def strip_unsupported_encoding(headers, warnings=None):
    """移除本机解不了的 accept-encoding 项（浏览器复制的 curl 常带 br / zstd）"""
    sup = _supported_encodings()
    for name in list(headers):
        if str(name).lower() != "accept-encoding":
            continue
        tokens = [t.strip() for t in str(headers[name]).split(",") if t.strip()]
        keep = [t for t in tokens if t.split(";")[0].strip().lower() in sup]
        if len(keep) == len(tokens):
            continue
        dropped = [t for t in tokens if t not in keep]
        if keep:
            headers[name] = ", ".join(keep)
        else:
            headers.pop(name)
        if warnings is not None:
            warnings.append("accept-encoding 中的 " + "/".join(dropped)
                            + " 本机未装解码库，已移除（否则响应会乱码）")
    return headers


# ══════════════════════════════════════════════════════
#  命令切分
# ══════════════════════════════════════════════════════

def _normalize_cmd_style(text):
    """把 cmd 风格复制的 curl 归一化成 bash 风格引号

    内层引号 \\^" → \\"，包值的 ^" → "，其余 ^X 转义 → X；
    没有 ^ 时原样返回，不影响 bash / PowerShell 风格的命令。
    """
    if '^' not in text:
        return text
    text = _CMD_INNER_QUOTE_RE.sub('\\\\"', text)
    text = _CMD_QUOTE_RE.sub('"', text)
    text = _CMD_ESCAPE_RE.sub(r'\1', text)
    return text


def _ansi_escape_at(text, i):
    """text[i] 是反斜杠，按 bash 的 $'...' 规则展开；返回 (展开结果, 消耗字符数)"""
    n = len(text)
    if i + 1 >= n:
        return "\\", 1
    c = text[i + 1]
    if c in _ANSI_MAP:
        return _ANSI_MAP[c], 2
    if c == "x" and i + 4 <= n:
        h = text[i + 2:i + 4]
        if all(ch in "0123456789abcdefABCDEF" for ch in h):
            return chr(int(h, 16)), 4
    if c == "u" and i + 6 <= n:
        h = text[i + 2:i + 6]
        if all(ch in "0123456789abcdefABCDEF" for ch in h):
            return chr(int(h, 16)), 6
    if c in "01234567":
        j = i + 1
        while j < n and j < i + 4 and text[j] in "01234567":
            j += 1
        return chr(int(text[i + 1:j], 8)), j - i
    return "\\" + c, 2            # bash 对未识别的转义保留反斜杠


def _tokenize_curl(text):
    """把 curl 命令拆成 token 列表。

    - 单/双引号包裹的值整体作为一个 token，引号内的换行原样保留
    - 支持 \\、^、` 三种续行（只在引号外生效）
    - 引号外的 ^" / \" 视为字面引号，兼容 cmd 风格复制的 curl
    """
    text = _normalize_cmd_style(text)
    tokens, buf = [], []
    quote, started, escaped_quote = "", False, False
    i, n = 0, len(text)

    def skip_newline(pos):
        """pos 起（跳过空格/tab 后）若是换行，返回换行后的位置，否则返回 -1"""
        k = pos
        while k < n and text[k] in " \t":
            k += 1
        if k < n and text[k] in "\r\n":
            k += 1
            if text[k - 1] == "\r" and k < n and text[k] == "\n":
                k += 1
            return k
        return -1

    while i < n:
        c = text[i]

        if quote:
            # 双引号内按 shell 规则处理转义，单引号内一律原样
            if c == "\\" and quote == '"' and i + 1 < n:
                nc = text[i + 1]
                if nc in ('"', "\\", "/", "'") or nc in _ESCAPE_MAP:
                    buf.append(_ESCAPE_MAP.get(nc, nc))
                    i += 2
                    continue
            if c == quote:
                quote = ""
                i += 1
                continue
            buf.append(c)
            i += 1
            continue

        if c == "$" and i + 1 < n and text[i + 1] == "'":
            # bash ANSI-C 引用 $'a\nb'：转义生效，单引号不能出现在内部
            i += 2
            while i < n:
                ch = text[i]
                if ch == "\\":
                    piece, used = _ansi_escape_at(text, i)
                    buf.append(piece)
                    i += used
                    continue
                if ch == "'":
                    i += 1
                    break
                buf.append(ch)
                i += 1
            started = True
            continue

        if c in ("'", '"'):
            quote, started = c, True
            i += 1
            continue

        if c in ("\\", "^", "`"):
            nxt = skip_newline(i + 1)          # 续行
            if nxt != -1:
                i = nxt
                continue
            if i + 1 < n and text[i + 1] in ("'", '"', "^"):
                buf.append(text[i + 1])        # cmd 风格的字面引号
                started = escaped_quote = True
                i += 2
                continue
            buf.append(c)
            started = True
            i += 1
            continue

        if c in " \t\r\n":
            if started:
                tokens.append(_strip_wrapped("".join(buf), escaped_quote))
                buf, started, escaped_quote = [], False, False
            i += 1
            continue

        buf.append(c)
        started = True
        i += 1

    if started:
        tokens.append(_strip_wrapped("".join(buf), escaped_quote))
    return tokens


def _strip_wrapped(tok, escaped_quote):
    """cmd 风格的 ^"..."^" 会残留外层引号，这里去掉成对的外层引号"""
    if escaped_quote and len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ('"', "'"):
        return tok[1:-1]
    return tok


# ══════════════════════════════════════════════════════
#  格式层：只依赖“写法”的预处理（与选项语义无关）
# ══════════════════════════════════════════════════════

def _expand_short_clusters(tokens):
    """把短选项连写拆成标准形态：-sSXPOST → -s -S -X POST；-H'k: v' → -H 'k: v'

    curl 的规则：连写时逐个字符判定，遇到带值的短选项，它后面剩下的字符就是取值。
    不确定是否带值的（签名表里没有）原样保留，交给未知选项分支处理。
    """
    out = []
    after_dd = False        # -- 之后的都是位置参数，不能拆（-https://x 不是 -h -t tps://x）
    for tok in tokens:
        if tok == "--":
            after_dd = True
            out.append(tok)
            continue
        if after_dd or not (len(tok) > 2 and tok[0] == "-" and tok[1] != "-"):
            out.append(tok)
            continue
        parts, rest, ok = [], tok[1:], True
        while rest:
            name = rest[0]
            if name in _SHORT_NOARG:
                parts.append("-" + name)
                rest = rest[1:]
                continue
            if name in _SHORT_ARG:
                parts.append("-" + name)
                value = rest[1:]
                if value:
                    parts.append(value)
                rest = ""
                continue
            ok = False        # 签名表里没有：不猜，整串交给未知选项分支
            break
        out.extend(parts if ok else [tok])
    return out


def _looks_like_url(tok):
    """是否明显是一个 URL（用于判断它是 URL 而不是某个选项的取值）"""
    low = (tok or "").lower()
    return "://" in low or low.startswith(("//", "localhost", "127.0.0.1"))


def _later_has_url(tokens, start):
    """start 之后还有没有 URL 形态的 token（不止一个 URL 时，前面的更像选项取值）"""
    return any(_looks_like_url(t) and not t.startswith("-") for t in tokens[start:])



# ══════════════════════════════════════════════════════
#  解析
# ══════════════════════════════════════════════════════

def _set_header(headers, name, value):
    """写入 header，同名（大小写不敏感）时合并：cookie 用 '; '，其它用 ', '

    HTTP/2 伪头（:authority 等）与 Content-Length 直接丢弃：前者 requests 发不出去，
    后者由 requests 按实际 body 重算，保留旧值反而会让请求失败。
    """
    name = (name or "").strip()
    value = "" if value is None else str(value).strip()
    if not name or _PSEUDO_HEADER_RE.match(name) or name.lower() == "content-length":
        return
    low = name.lower()
    for exist in list(headers):
        if exist.lower() == low:
            sep = "; " if low == "cookie" else ", "
            if headers[exist] and value:
                headers[exist] = headers[exist] + sep + value
            elif value:
                headers[exist] = value
            return
    headers[name] = value


def _has_header(headers, name):
    return any(str(h).lower() == name.lower() for h in headers)


def _urlencode_piece(val, warnings):
    """--data-urlencode 的编码规则：name=content / name@file / content

    curl 用 curl_easy_escape 编码（空格 → %20，不是 +）。
    """
    if "=" in val:
        k, v = val.split("=", 1)
        return f"{k}={quote(v, safe='')}"
    if "@" in val:
        warnings.append(f"--data-urlencode {val} 引用文件，无法静态读取，已跳过该字段")
        return None
    return quote(val, safe="")


def _looks_like_option_value(tok, later_has_url=False):
    """未知选项后面的这一项，是否更像它的取值（而不是请求地址）

    格式证据只有两条：
      1. 以 - 开头的一定是选项，不是值；
      2. 像 URL 的：只有后面还有别的 URL 时，才认为它是这个选项的值。
    其它情况（普通串 / 空串 / 形如 header）一律当取值——curl 的选项绝大多数
    是带值的，漏吞比多吞更危险（漏吞会让它变成 URL）。
    """
    if tok.startswith("-"):
        return False
    if _looks_like_url(tok):
        return later_has_url
    return True


def _parse_body_text(raw):
    """把 -d / --data-raw 拼出的文本尽量解析成 JSON，失败则原样保留字符串"""
    txt = raw.strip()
    if not txt:
        return raw
    for candidate in (txt, _fix_json_text(txt)):
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            continue
    return raw


def _fix_json_text(txt):
    """修补常见伪 JSON：去掉 } / ] 前的尾逗号；整体没有双引号时把单引号换成双引号"""
    fixed = re.sub(r",\s*([}\]])", r"\1", txt)
    if '"' not in fixed:
        fixed = fixed.replace("'", '"')
    return fixed


def parse_curl(curl_str):
    """解析 curl 命令，返回 (url, method, headers, body)"""
    r = parse_curl_report(curl_str)
    return r["url"], r["method"], r["headers"], r["body"]


def parse_curl_report(curl_str):
    """解析 curl 命令，返回 {url, method, headers, body, warnings}

    warnings 记录被忽略或需人工确认的项，便于界面提示 —— 任何参数都不会再静默丢失。
    """
    tokens = _tokenize_curl(curl_str or "")
    tokens = _expand_short_clusters(tokens)
    if tokens and tokens[0].lower() in ("curl", "curl.exe"):
        tokens = tokens[1:]

    url = ""
    method = ""
    headers = {}
    data_parts, form_parts, url_queries = [], [], []
    request_target = ""
    max_time = None             # -m / --max-time 映射到请求的 timeout 字段
    json_mode = get_mode = False
    positional_only = False       # 遇到 -- 之后，全部当位置参数（不再识别选项）
    extra_urls = []
    warnings = []
    i, n = 0, len(tokens)

    while i < n:
        tok = tokens[i]
        if not positional_only and tok == "--":
            positional_only = True
            i += 1
            continue

        opt, inline = tok, None
        if tok.startswith("--") and "=" in tok:
            opt, inline = tok.split("=", 1)

        kind = _ARG_OPTS.get(opt)
        if kind:
            if inline is not None:
                val, i = inline, i + 1
            elif (i + 1 < n and not _is_option_token(tokens[i + 1])
                    and not (kind == "url" and tokens[i + 1].startswith("-"))):
                val, i = tokens[i + 1], i + 2
            else:
                # 值被漏粘：绝不能把后面的 --url / -H 当成取值吞掉
                val, i = "", i + 1
                if kind not in ("ignore", "url"):
                    warnings.append(f"{opt} 缺少取值，已跳过")

            if kind == "header":
                if val.startswith("@"):
                    warnings.append(f"{opt} {val} 从文件读 header，无法静态读取，"
                                    "请把文件内容手工填进 Headers")
                elif ":" in val:
                    k, v = val.split(":", 1)
                    _set_header(headers, k, v)
                else:
                    k, v = val.rstrip(";"), ""     # 形如 -H 'X-Empty;' 的空值头
                    _set_header(headers, k, v)
            elif kind.startswith("header:"):
                _set_header(headers, kind.split(":", 1)[1], val)
            elif kind in ("data", "data-urlencode"):
                if val.startswith("@"):
                    warnings.append(f"{opt} {val} 引用文件，无法静态读取，请手工填写 body 内容")
                elif val:
                    data_parts.append((val, kind == "data-urlencode"))
            elif kind == "json":
                if val.startswith("@"):
                    warnings.append(f"{opt} {val} 引用文件，无法静态读取，请手工填写 body 内容")
                elif val:
                    data_parts.append((val, False))
                    json_mode = True
            elif kind == "form":
                if val:
                    form_parts.append(val)
            elif kind == "method":
                method = val.upper()
            elif kind == "url":
                if val and not val.startswith("-"):
                    if url and val != url:
                        warnings.append(f"--url 出现多次（{val}），只转换了第一个：{url}")
                    elif not url:
                        url = val
                else:
                    warnings.append("--url 缺少取值，已改为从位置参数里找 URL")
            elif kind == "url_query":
                if val:
                    url_queries.append(val)
            elif kind == "request_target":
                if val:
                    request_target = val
            elif kind == "cookie":
                # -b 可能是 cookie 串（含 =），也可能是 cookie 文件；
                # 立即合并以保持与 -H 'cookie: ...' 在命令行中的先后顺序
                if val.startswith("@") or "=" not in val:
                    warnings.append(f"-b {val} 指向 cookie 文件，无法静态读取，请在 headers 里补 cookie")
                else:
                    _set_header(headers, "cookie", val)
            elif kind == "auth":
                user, _, pwd = val.partition(":")
                _set_header(headers, "authorization",
                            "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode())
            elif kind == "oauth2_bearer":
                if val:
                    if _has_header(headers, "authorization"):
                        warnings.append("--oauth2-bearer 与已有的 authorization 冲突，已按 Bearer 覆盖")
                        for exist in [h for h in headers if h.lower() == "authorization"]:
                            headers.pop(exist)
                    _set_header(headers, "authorization", "Bearer " + val)
                else:
                    warnings.append("--oauth2-bearer 缺少 token，已跳过")
            elif kind == "upload":
                if val:
                    warnings.append(f"{opt} {val} 上传的文件内容无法静态读取，请手工补写 body")
            elif kind == "timeout":
                try:
                    t = float(val)
                    if t > 0:
                        max_time = t
                except (TypeError, ValueError):
                    warnings.append(f"{opt} {val} 不是合法秒数，已忽略")
            elif kind == "warn_env":
                warnings.append(f"{opt} {val} 依赖本机环境（代理 / 证书 / DNS 覆盖），"
                                "转换后的请求可能因此失败，请确认运行环境一致")
            elif kind == "warn":
                warnings.append(f"{opt} {val} 需要本地计算签名，无法静态转换，"
                                "请在 headers 里手工补 authorization")
            continue

        if positional_only:
            if tok == "-":
                i += 1
                continue
            if not url:
                url = tok
            elif tok != url:
                extra_urls.append(tok)
            i += 1
            continue

        if tok == "-":
            # 单个 - 在 curl 里表示 stdin/stdout，不是地址
            i += 1
            continue
        if opt in _WARN_NOARG_OPTS:
            warnings.append(f"{opt} 需要 curl 的本地认证协商，转换后的请求缺少认证头可能失败，"
                            "请在 headers 里手工补认证")
            i += 1
            continue
        if opt in _NO_ARG_OPTS:
            if opt in ("-G", "--get"):
                get_mode = True
            elif opt in ("-I", "--head"):
                method = "HEAD"
            elif opt in ("--next", "-:"):
                warnings.append("命令里含 --next（多个请求），只转换了第一个请求")
            i += 1
            continue

        # ── 格式签名：短选项吃不吃取值，与语义无关 ──
        if len(opt) == 2 and opt[0] == "-" and opt[1] in _SHORT_NOARG:
            i += 1
            continue
        if len(opt) == 2 and opt[0] == "-" and opt[1] in _SHORT_ARG:
            has_value = (inline is not None or (
                i + 1 < n and not _is_option_token(tokens[i + 1])))
            i += 2 if has_value and inline is None else 1
            continue

        if tok.startswith("-") and tok != "-":
            # 未知选项：用格式证据判断它是否吃掉了下一个 token，
            # 绝不吞掉 URL / header
            warnings.append(f"已忽略无法识别的选项 {opt}")
            i += 1
            if inline is None and i < n and \
                    _looks_like_option_value(tokens[i], _later_has_url(tokens, i + 1)):
                i += 1
            continue

        if not url:
            url = tok
        elif tok != url:
            extra_urls.append(tok)
        i += 1

    if extra_urls:
        warnings.append("命令里含多个请求地址，只转换了第一个；其余："
                        + ", ".join(extra_urls[:3]))

    # ── accept-encoding：本机解不了的编码（br / zstd）必须去掉，否则响应乱码 ──
    strip_unsupported_encoding(headers, warnings)

    # ── --json 隐式补 content-type / accept ──
    if json_mode:
        if not _has_header(headers, "content-type"):
            _set_header(headers, "content-type", "application/json")
        if not _has_header(headers, "accept"):
            _set_header(headers, "accept", "application/json")

    # ── 组装 body ──
    body = None
    if form_parts:
        form = {}
        for part in form_parts:
            k, _, v = part.partition("=")
            if v.startswith(("@", "<")):
                warnings.append(f"表单字段 {k} 引用文件（{v}），无法自动填充，需手动处理文件上传")
            else:
                # -F 'f=v;type=text/plain' 里的 mime 属性对请求报文无意义，切掉但要提示
                segs = _FORM_ATTR_RE.split(v, maxsplit=1)
                v = segs[0]
                if len(segs) > 1:
                    attrs = [a.split("=", 1)[0] for a in segs[1].split(";") if "=" in a]
                    warnings.append(f"表单字段 {k} 的 {'/'.join(attrs)} 属性无法自动填充，已忽略")
                form[k] = v
        body = form
        if not _has_header(headers, "content-type"):
            _set_header(headers, "content-type", "application/x-www-form-urlencoded")
    elif data_parts and json_mode:
        # --json 可写多次：curl 会拼成同一个 body，这里按对象合并，同名字段后者覆盖
        docs, conflicts, merged_ok = [v for v, _ in data_parts], [], True
        merged = {}
        for doc in docs:
            try:
                obj = json.loads(doc)
            except (ValueError, TypeError):
                merged_ok = False
                break
            if not isinstance(obj, dict):
                merged_ok = False
                break
            conflicts.extend(k for k in obj if k in merged)
            merged.update(obj)
        if merged_ok:
            body = merged
            if conflicts:
                warnings.append("多个 --json 出现同名字段 " + ", ".join(conflicts) + "，后者已覆盖前者")
        else:
            body = _parse_body_text("".join(docs))
            if isinstance(body, str):
                warnings.append("多个 --json 拼接后不是合法 JSON，已按原样保留 body")
    elif data_parts:
        # 多个 -d 按表单拼接，但不重复补分隔符（否则 `-d a=1 -d '&b=2'` 会拼出 a=1&&b=2）
        joined = ""
        for val, is_enc in data_parts:
            piece = _urlencode_piece(val, warnings) if is_enc else val
            if piece is None:
                continue
            if joined and not joined.endswith("&") and not piece.startswith("&"):
                joined += "&"
            joined += piece
        if get_mode:
            url = f"{url}{'&' if '?' in url else '?'}{joined}"
        else:
            body = _parse_body_text(joined)

    # ── --url-query：追加到 URL 的查询串（+ 开头表示已编码，@ 引用文件）──
    if url_queries:
        pieces = []
        for item in url_queries:
            if item.startswith("+"):
                pieces.append(item[1:])
            elif item.startswith("@"):
                warnings.append(f"--url-query {item} 引用文件，无法静态读取，已跳过")
            else:
                piece = _urlencode_piece(item, warnings)
                if piece is not None:
                    pieces.append(piece)
        extra = "&".join(p for p in pieces if p)
        if extra:
            url = f"{url}{'&' if '?' in url else '?'}{extra}"

    # ── --request-target：直接覆盖请求行里的 path+query ──
    if request_target and url:
        sp = urlsplit(url)
        url = f"{sp.scheme}://{sp.netloc}{request_target}" if sp.scheme and sp.netloc \
            else request_target

    if not method:
        method = "POST" if body is not None else "GET"
    if not url:
        # 兜底：即使选项解析出了偏差，也要把命令里的 URL 抢回来，不能给出占位地址
        for cand in tokens:
            if "://" in cand and not cand.startswith("-"):
                url = cand
                warnings.append(f"未能按常规位置取到 URL，已兜底识别为 {cand}")
                break
    if not url:
        warnings.append("未识别到请求 URL，请检查 curl 命令（--url 后的地址或 URL 位置参数）")

    return {"url": url, "method": method, "headers": headers,
            "body": body, "warnings": warnings, "timeout": max_time}


def build_config(url, method, headers, body, name="接口测试", timeout=None):
    """构造 api-tester 配置字典（纯数据结构，不拼字符串）

    不生成 assert：curl 只描述“发什么请求”，Postman 导入后也只是发请求 + 看响应；
    硬塞 status_code: 200 会让返回 201 / 204 / 4xx 的接口被误判为“转换错误”。
    """
    request = {"url": url, "method": method}
    if headers:
        request["headers"] = dict(headers)
    if body is not None:
        request["body"] = body
    # 默认 60 秒（比执行器默认 15 更宽容），有 -m / --max-time 则按命令里的值
    request["timeout"] = float(timeout) if timeout and float(timeout) > 0 else 60
    return {"name": name, "request": request}
