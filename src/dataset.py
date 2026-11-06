# -*- coding: utf-8 -*-
"""dataset.py — 数据集合（参数化数据表）

导入的 Excel / CSV 存成 data/_datasets/<名称>.json：
    {"name": "...", "source": "xxx.xlsx", "headers": ["userName", "city"],
     "rows": [["alice", "上海"], ...], "updated_at": "2026-11-05 16:42:08"}

接口里写 ${字段名} 引用当前数据集的列，运行时按行执行：
第一行跑完整串步骤 → 第二行再跑一遍。没引用任何字段时照常只跑一次。
当前数据集记在 data/_dataset_active.json（{"name": "..."}）。
"""
import csv
import io
import json
import os
import re
import threading
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DATASET_DIR = os.path.join(DATA_DIR, "_datasets")
ACTIVE_FILE = os.path.join(DATA_DIR, "_dataset_active.json")

_LOCK = threading.Lock()
# ${...} 引用：列名可以是中文（如「姓名」「手机号」）或任意非 {} 字符，
# 不能用 [A-Za-z_] 限定英文——中文列头写的 ${姓名} 会被当成未引用，参数化不展开
_REF_RE = re.compile(r"\$\{\s*([^{}]+?)\s*\}")


# ── 基础工具 ────────────────────────────────────────────
def _ensure_dir():
    os.makedirs(DATASET_DIR, exist_ok=True)


def _safe_name(name):
    """只取文件名，去掉路径分隔与 .json 后缀，防止穿越到目录外"""
    name = os.path.basename(str(name or "").strip())
    if name.lower().endswith(".json"):
        name = name[:-5]
    name = name.strip()
    if name in ("", ".", ".."):
        return ""
    return name


def _path(name):
    safe = _safe_name(name)
    return os.path.join(DATASET_DIR, safe + ".json") if safe else ""


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _cell(v):
    """单元格统一成字符串：Excel 读出来的 1.0 写成 1，时间等类型转文本"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return str(v)


def _norm_headers(headers):
    """列名去空白；空的按「列1」补；重名的加 -2 后缀，保证能当变量名用"""
    out = []
    seen = {}
    for i, h in enumerate(headers or []):
        name = str(h or "").strip() or ("列%d" % (i + 1))
        base = name
        n = 1
        while name in seen:
            n += 1
            name = "%s-%d" % (base, n)
        seen[name] = 1
        out.append(name)
    return out


def _norm_rows(rows, width):
    out = []
    for r in rows or []:
        r = list(r or [])[:width]
        r += [""] * (width - len(r))
        out.append([_cell(v) for v in r])
    return out


# ── 读 / 写 ─────────────────────────────────────────────
def list_datasets():
    _ensure_dir()
    items = []
    for fn in sorted(os.listdir(DATASET_DIR)):
        if not fn.lower().endswith(".json"):
            continue
        try:
            with open(os.path.join(DATASET_DIR, fn), "r", encoding="utf-8") as f:
                d = json.load(f) or {}
        except Exception:
            continue
        rows = d.get("rows") or []
        headers = d.get("headers") or []
        items.append({
            "name": d.get("name") or fn[:-5],
            "source": d.get("source") or "",
            "headers": headers,
            "columns": len(headers),
            "rows_count": len(rows),
            "updated_at": d.get("updated_at") or "",
        })
    return items


def load(name):
    path = _path(name)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception:
        return None
    d["name"] = d.get("name") or _safe_name(name)
    d["headers"] = d.get("headers") or []
    d["rows"] = d.get("rows") or []
    d["source"] = d.get("source") or ""
    d["updated_at"] = d.get("updated_at") or ""
    return d


def save(name, headers, rows, source=None):
    """整表保存（导入 / 编辑 / 加行删行都走这里），返回保存后的数据集"""
    safe = _safe_name(name)
    if not safe:
        raise ValueError("数据集名称不能为空")
    headers = _norm_headers(headers)
    if not headers:
        raise ValueError("至少要有一列")
    rows = _norm_rows(rows, len(headers))
    _ensure_dir()
    path = _path(safe)
    old = load(safe) or {}
    data = {
        "name": safe,
        "source": source if source is not None else (old.get("source") or ""),
        "headers": headers,
        "rows": rows,
        "updated_at": _now(),
    }
    with _LOCK:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    return data


def delete(name):
    path = _path(name)
    removed = False
    if path and os.path.exists(path):
        with _LOCK:
            os.remove(path)
        removed = True
    if active_name() == _safe_name(name):
        set_active("")
    return removed


def active_name():
    try:
        with open(ACTIVE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        return str(d.get("name") or "")
    except Exception:
        return ""


def set_active(name):
    safe = _safe_name(name)
    if safe and not os.path.exists(_path(safe)):
        raise ValueError("数据集不存在：%s" % safe)
    with _LOCK:
        tmp = ACTIVE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"name": safe}, f, ensure_ascii=False)
        os.replace(tmp, ACTIVE_FILE)
    return safe


# ── 运行时：按行展开 ────────────────────────────────────
def rows_for_steps(ordered_steps):
    """当前数据集被这些步骤引用时，返回 [(行标签, {字段: 值}), ...]；没引用返回 None"""
    name = active_name()
    if not name:
        return None
    data = load(name)
    if not data or not data.get("headers") or not data.get("rows"):
        return None
    try:
        blob = json.dumps([s for _i, s in (ordered_steps or [])], ensure_ascii=False)
    except Exception:
        return None
    used = set()
    for m in _REF_RE.finditer(blob):
        # 引用可能是 ${姓名} 或 ${姓名.字段}：与列头比对用根名（去掉 .字段 / [下标]）
        head = re.split(r"[.\[]", m.group(1).strip(), 1)[0].strip()
        if head:
            used.add(head)
    if not used:
        return None
    cols = [h for h in data["headers"] if h in used]
    if not cols:
        return None
    out = []
    for i, row in enumerate(data["rows"], 1):
        vals = {}
        for c in cols:
            idx = data["headers"].index(c)
            vals[c] = row[idx] if idx < len(row) else ""
        out.append(("第%d行" % i, vals))
    return out


def first_row_for_steps(ordered_steps):
    """单次发送（即发即看）只能跑一行，取当前数据集第一行

    返回 {"name": 数据集名, "label": 行标签, "values": {字段: 值}}；
    当前接口没引用任何数据集字段时返回 None。
    """
    rows = rows_for_steps(ordered_steps)
    if not rows:
        return None
    label, vals = rows[0]
    return {"name": active_name(), "label": label, "values": vals}


# ── 导入 Excel / CSV ────────────────────────────────────
def read_table(filename, stream):
    """把 xlsx / xls / csv 读成 (headers, rows)，首行当表头"""
    ext = os.path.splitext(str(filename or ""))[1].lower()
    raw = stream.read()
    if ext in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.active
        table = [list(r) for r in ws.iter_rows(values_only=True)]
        try:
            wb.close()
        except Exception:
            pass
    elif ext == ".csv":
        text = None
        for enc in ("utf-8-sig", "gbk", "utf-8"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            text = raw.decode("utf-8", "replace")
        table = [r for r in csv.reader(io.StringIO(text))]
    else:
        raise ValueError("只支持 .xlsx / .xlsm / .csv 文件")
    table = [r for r in table if any(str(c).strip() for c in r if c is not None)]
    if not table:
        raise ValueError("文件里没有数据")
    headers = _norm_headers([_cell(c) for c in table[0]])
    rows = _norm_rows(table[1:], len(headers))
    return headers, rows
