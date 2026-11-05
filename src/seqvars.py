#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
seqvars.py — 预设变量中的「自增变量」序号器

数据存储（复用 data/_variables.json，带 seq 元数据的变量即为自增变量）：
  "orderNo": {
      "value": "XX002",                     ← 永远是「下一个将使用的值」
      "saved_at": "2026-10-26 10:00:00",
      "seq": {"prefix": "XX", "digits": 3, "start": 1, "next": 2, "step": 1,
              "last_used": "XX001", "last_at": "2026-10-26 10:05:00", "used_count": 1}
  }

约定：
  1. value 始终等于 seq.next 渲染出的字符串，因此变量面板、_load_variables_values()
     等既有读取路径无需改动即可看到「下一个号」。
  2. 取号只发生在 engine.do_request 入口（每次真实请求取一个号），取号后立即落盘，
     进程重启不会回到起始值。
  3. 自增只更新 value/next/last_used/last_at/used_count，绝不刷 saved_at
     ——变量面板按保存时间倒序排列，若跟着刷新每跑一次行就会跳动。
  4. 序号超过指定位数时自然变长（XX999 → XX1000），不设上限、不回绕。
  5. 与接口提取/参数化/setup 同名时「提取优先」：预设加载时自增变量不写入变量字典，
     请求前若发现 variables 中该名字被写成了「非上一次发出的值」，即判定为外部写入，
     本次不取号也不消耗序号。
"""
import json
import os
import re
import sys
import threading
from datetime import datetime

_LOCK = threading.RLock()

_here = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    # 打包版：数据目录随 exe（与 app.py / scheduler.py 的推导保持一致）
    _app_root = os.path.dirname(os.path.abspath(sys.executable))
else:
    _app_root = os.path.dirname(_here)
VARS_FILE = os.path.join(_app_root, "data", "_variables.json")

OVERRIDE_KEY = "__seq_override__"   # 保留键（历史兼容，当前逻辑不再依赖该标记）
MAX_DIGITS = 12


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load_raw():
    """读取 _variables.json 原始字典（含元数据），失败时返回空字典"""
    try:
        with open(VARS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except (IOError, OSError, json.JSONDecodeError):
        return {}


def _save_raw(raw):
    """原子写回：先写 .tmp 再替换，避免并发读到半截文件"""
    tmp = VARS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    os.replace(tmp, VARS_FILE)


# ═══════════════════════════════════════════════════
#  格式解析 / 渲染
# ═══════════════════════════════════════════════════

def parse_format(value, digits=None, step=1):
    """从示例值解析自增格式

    XX001 → prefix="XX", digits=3, start=1
    XX005 → prefix="XX", digits=3, start=5（从 005 接着用）
    ORDER（无数字）→ prefix="ORDER", digits=3, start=1
    """
    text = str(value if value is not None else "").strip()
    m = re.search(r"(\d+)$", text)
    if m:
        prefix = text[:m.start()]
        start = int(m.group(1))
        auto_digits = len(m.group(1))
    else:
        prefix = text
        start = 1
        auto_digits = 3
    try:
        d = int(digits) if digits not in (None, "") else auto_digits
    except (TypeError, ValueError):
        d = auto_digits
    d = max(0, min(d, MAX_DIGITS))
    try:
        st = int(step)
    except (TypeError, ValueError):
        st = 1
    st = st if st >= 1 else 1
    return {"prefix": prefix, "digits": d, "start": start, "next": start, "step": st}


def render(prefix, digits, num):
    """把序号渲染成带前缀的定长字符串（不足位数左侧补零）"""
    try:
        n = int(num)
    except (TypeError, ValueError):
        n = 0
    try:
        d = max(0, min(int(digits), MAX_DIGITS))
    except (TypeError, ValueError):
        d = 3
    return "%s%s" % (str(prefix or ""), str(n).zfill(d))


def _render_item(seq):
    return render(seq.get("prefix", ""), seq.get("digits", 3), seq.get("next", 1))


def build_seq(sample_value, digits=None, step=1):
    """构造 seq 元数据（新建 / 编辑自增变量时使用）"""
    fmt = parse_format(sample_value, digits, step)
    return {
        "prefix": fmt["prefix"], "digits": fmt["digits"],
        "start": fmt["start"], "next": fmt["next"], "step": fmt["step"],
        "last_used": None, "last_at": None, "used_count": 0,
    }


def is_seq(item):
    """判断变量条目是否为自增变量"""
    return isinstance(item, dict) and isinstance(item.get("seq"), dict)


def describe(item):
    """把变量条目转成前端需要的自增摘要（非自增变量返回 None）"""
    if not is_seq(item):
        return None
    seq = item["seq"]
    return {
        "prefix": seq.get("prefix", ""),
        "digits": seq.get("digits", 3),
        "start": seq.get("start", 1),
        "next": seq.get("next", 1),
        "step": seq.get("step", 1),
        "next_value": _render_item(seq),
        "last_used": seq.get("last_used"),
        "last_at": seq.get("last_at") or "",
        "used_count": seq.get("used_count", 0) or 0,
    }


# ═══════════════════════════════════════════════════
#  取号 / 覆盖标记 / 重置
# ═══════════════════════════════════════════════════

def assign(variables, only=None):
    """为本次请求分配自增变量值（原地写入 variables）

    返回 {变量名: 本次使用的值}；无自增变量时返回空字典。

    参数：
      only — 只处理这些变量名（本次请求实际引用到的）；None 表示不限

    是否取号：
      · 名字不在 variables 中（预设加载会排除自增变量）→ 取号
      · 值等于「上一次发出的号」→ 取号（同一轮里多次请求各自取号）
      · 其余情况 → 视为被接口提取 / 参数化 / setup 写了真实值，按提取优先跳过
    """
    if not isinstance(variables, dict):
        return {}
    with _LOCK:
        raw = _load_raw()
        names = [k for k, v in raw.items() if is_seq(v)]
        # 请求里没引用的自增变量不取号：既不会白跑序号，也不会出现在结果卡片上
        if only is not None:
            names = [n for n in names if n in only]
        if not names:
            return {}
        assigned = {}
        for name in names:
            item = raw[name]
            seq = item["seq"]
            if name in variables and str(variables.get(name)) != str(seq.get("last_used")):
                continue
            used = _render_item(seq)
            try:
                step = int(seq.get("step", 1) or 1)
            except (TypeError, ValueError):
                step = 1
            seq["next"] = int(seq.get("next", 1) or 1) + max(1, step)
            seq["last_used"] = used
            seq["last_at"] = _now()
            seq["used_count"] = int(seq.get("used_count", 0) or 0) + 1
            item["value"] = _render_item(seq)   # value 始终保存「下一个」
            variables[name] = used
            assigned[name] = used
        if assigned:
            _save_raw(raw)
        return assigned


def mark_override(variables, names):
    """（保留接口）显式登记“被接口提取 / 参数化写入”的变量名

    当前取号逻辑已通过「值是否等于上一次发出的号」自动判断，无需再登记。
    """
    if not isinstance(variables, dict) or not names:
        return
    bucket = variables.get(OVERRIDE_KEY)
    if not isinstance(bucket, set):
        bucket = set()
        variables[OVERRIDE_KEY] = bucket
    for n in names:
        if n:
            bucket.add(n)


def reset(name):
    """重置自增变量回起始值（保留前缀/位数/步长），返回重置后的摘要"""
    with _LOCK:
        raw = _load_raw()
        item = raw.get(name)
        if not is_seq(item):
            return None
        seq = item["seq"]
        start = seq.get("start", 1)
        try:
            start = int(start)
        except (TypeError, ValueError):
            start = 1
        seq["next"] = start
        seq["last_used"] = None
        seq["last_at"] = None
        seq["used_count"] = 0
        item["value"] = _render_item(seq)
        _save_raw(raw)
        return describe(item)


def set_step(name, step):
    """只改步长：前缀/位数/起始值/当前序号进度全部保留（非自增变量返回 None）"""
    try:
        step = max(1, int(step))
    except (TypeError, ValueError):
        step = 1
    with _LOCK:
        raw = _load_raw()
        item = raw.get(name)
        if not is_seq(item):
            return None
        item["seq"]["step"] = step
        _save_raw(raw)
        return describe(item)


def set_rule(name, sample_value, digits=None, step=1, reset_counter=True):
    """新建 / 修改自增规则

    reset_counter=True 时按示例值重新开始（next=start）；
    False 时仅改前缀/位数/步长，保留当前序号进度。
    """
    with _LOCK:
        raw = _load_raw()
        item = raw.get(name)
        now_str = _now()
        if not isinstance(item, dict):
            item = {"value": "", "saved_at": now_str}
        old_seq = item.get("seq") if isinstance(item.get("seq"), dict) else None
        seq = build_seq(sample_value, digits, step)
        if old_seq and not reset_counter:
            seq["next"] = old_seq.get("next", seq["next"])
            seq["last_used"] = old_seq.get("last_used")
            seq["last_at"] = old_seq.get("last_at")
            seq["used_count"] = old_seq.get("used_count", 0)
        item["seq"] = seq
        item["value"] = _render_item(seq)
        if not reset_counter and old_seq is None:
            item["saved_at"] = now_str
        raw[name] = item
        _save_raw(raw)
        return describe(item)


def drop_seq(name):
    """把自增变量降级为普通变量（保留当前 value）"""
    with _LOCK:
        raw = _load_raw()
        item = raw.get(name)
        if isinstance(item, dict):
            item.pop("seq", None)
            item["saved_at"] = _now()
            _save_raw(raw)
            return True
        return False
