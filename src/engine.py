#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
engine.py — 核心业务逻辑

职责：HTTP 请求、断言、变量提取、测试执行、结果保存
所有终端输出委托给 printer.py

调用总览 ──────────────────────────────────────────────────────────
  公开接口（由 runner.py / api_tester.py 调用）
    run_single(cfg, output_dir)    — 单接口循环测试
    run_steps(cfg, output_dir)     — 多步骤串联测试

  内部流程
    do_request(req_cfg, variables) — 发送 HTTP 请求，返回结果字典
    run_assert(assert_cfg, ...)    — 执行断言，返回 (passed, messages)
    do_extract(extract, ...)       — 从响应提取变量，写入 variables
    _run_step_once(...)            — 单步骤单次执行（请求→断言→提取→记录）
    save_result(name, records, output_dir) — 保存结果到 xlsx/csv

  内部工具
    _replace(text, variables)      — 字符串变量替换：${var} → 实际值
    _replace_deep(obj, variables)  — 对 dict/list/str 递归执行变量替换
    get_by_path(obj, path)         — JSON 路径取值，支持数组下标和混合路径
──────────────────────────────────────────────────────────────────
"""
import csv
import importlib
import json
import math
import os
import random
import re
import time
import uuid
from datetime import datetime, timedelta, date

import requests
import urllib3
import printer as P
try:
    import seqvars as _seqvars      # 预设变量中的自增序号器（取号 / 落盘）
except Exception:                    # 精简或打包环境缺失时降级为「无自增变量」
    _seqvars = None
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ═══════════════════════════════════════════════════════════════
# 前置条件（setup）：通过 Python 表达式计算变量值
# ═══════════════════════════════════════════════════════════════

# eval_setup 允许使用的安全命名空间
# 说明：Python 3.13+ 中 datetime.strftime 内部会隐式 import time，
# 因此 builtins 需提供受限的 __import__（仅白名单模块），否则正常表达式会报 KeyError/ImportError
def _setup_safe_import(name, *args, **kwargs):
    import importlib
    top = str(name).split(".")[0]
    if top in ("time", "_strptime"):
        return importlib.import_module(name)
    raise ImportError(f"禁止在 setup 表达式中导入模块: {name}")

_SETUP_SAFE_GLOBALS = {
    "__builtins__": {
        # 安全说明：不提供 exec/eval/open/compile 等危险 builtins，
        # __import__ 仅提供白名单受限版本，datetime/random/uuid 等模块已直接放入 globals 白名单
        "True": True, "False": False, "None": None,
        "__import__": _setup_safe_import,
    },
    "datetime": datetime,
    "timedelta": timedelta,
    "date": date,
    "random": random,
    "uuid": uuid,
    "math": math,
    "time": time,
    "json": json,
    "re": re,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "list": list,
    "dict": dict,
    "len": len,
    "range": range,
    "round": round,
    "abs": abs,
    "max": max,
    "min": min,
    "sum": sum,
    "sorted": sorted,
    "enumerate": enumerate,
    "zip": zip,
    "print": lambda *a, **kw: None,  # 静默，避免干扰输出
}


def eval_setup(setup_cfg):
    """执行前置条件，计算变量值

    参数：
      setup_cfg — YAML 中 setup 段的字典，键为变量名，值为 Python 表达式字符串
                  例：{"timestamp": "datetime.now().strftime('%Y-%m-%d %H:%M:%S')",
                       "random_id": "random.randint(1000, 9999)"}

    返回：(变量字典, 错误列表)
      变量字典 — 计算成功的变量，如 {"timestamp": "2026-06-29 15:30:00", "random_id": 5432}
      错误列表 — 计算失败的变量信息，如 [{"name": "xxx", "expr": "...", "error": "NameError: ..."}]

    安全限制：
      - __builtins__ 仅保留 True/False/None，禁用 __import__/exec/eval/open/compile
      - 仅开放白名单模块和内置函数
      - 每个表达式独立执行，一个失败不影响其他
    """
    if not setup_cfg or not isinstance(setup_cfg, dict):
        return {}, []

    variables = {}
    errors = []

    for var_name, expr in setup_cfg.items():
        expr_str = str(expr)
        try:
            value = eval(expr_str, dict(_SETUP_SAFE_GLOBALS), {})
            variables[var_name] = value
            P.print_setup_var(var_name, value, expr_str)
        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}"
            errors.append({"name": var_name, "expr": expr_str, "error": err_msg})
            P.print_setup_error(var_name, expr_str, err_msg)

    return variables, errors


# ═══════════════════════════════════════════════════════════════
# 变量替换
# ═══════════════════════════════════════════════════════════════

def _var_to_text(v):
    """内嵌替换时的变量序列化：布尔/对象/数组按 JSON 标准格式输出，其余用 str()
    （保证注入 JSON body 后仍是合法 JSON，如 True→true、{'a':1}→{"a": 1}；
     None → 空字符串）"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if v is None:
        return ""
    return str(v)


_VAR_REF_RE = re.compile(r'\$\{([^{}]+)\}')   # 变量引用：${名称} / ${名称.字段} / ${名称.items[0].深层}


def _lookup_var(name, variables):
    """解析 ${...} 中的引用名并取值。

    先按完整名称精确匹配（兼容含特殊字符的变量名）；
    未命中且名称带 "." 时，按“变量名.字段路径”取字段——
    如 user.id、user.items[0].name（路径解析复用 get_by_path）。
    返回 (是否找到, 值)；路径不存在或值为 null 时视为未找到。
    """
    if name in variables:
        return True, variables[name]
    head, _, rest = name.partition(".")
    if rest and head in variables:
        val = get_by_path(variables[head], rest)
        if val is not None:
            return True, val
    return False, None


def _replace(text, variables):
    """字符串变量替换：将 ${var_name} 替换为 variables 中对应的值
    例：输入 "${postId}" 且 variables={"postId": 42} → 返回 "42"
    若 text 不是字符串，原样返回不做替换

    特殊写法：字段引用 ${变量.字段}，如 "${user.id}"、"${user.items[0].name}"
    用于数据驱动（foreach 注入对象）时同一轮取多个字段分别替换
    特殊情况：当整个字符串恰好是一个 ${var} 引用时，直接返回变量的原始类型值
    （支持将数字/布尔/JSON对象等非字符串类型注入到请求体中）
    内嵌替换时布尔/对象/数组按 JSON 标准格式序列化（true / {"a": 1}）
    找不到的引用原样保留
    """
    if not isinstance(text, str):
        return text
    if not variables or '${' not in text:
        return text
    # 整个字符串恰好是单个变量引用 → 直接返回原始类型值（数字/布尔/JSON对象等）
    m = _VAR_REF_RE.fullmatch(text)
    if m:
        found, val = _lookup_var(m.group(1), variables)
        return val if found else text
    def _sub(mm):
        found, val = _lookup_var(mm.group(1), variables)
        return _var_to_text(val) if found else mm.group(0)
    return _VAR_REF_RE.sub(_sub, text)


def _replace_deep(obj, variables):
    """对 dict / list / str 递归执行变量替换
    逐层遍历 obj，遇到字符串就调用 _replace 替换其中的 ${var}
    dict → 对每个 value 递归；list → 对每个元素递归；str → 替换；其他 → 原样返回
    """
    if isinstance(obj, dict):
        return {k: _replace_deep(v, variables) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_replace_deep(i, variables) for i in obj]
    if isinstance(obj, str):
        return _replace(obj, variables)
    return obj


def _referenced_var_names(obj):
    """收集配置里出现过的 ${变量名}（只取根名，忽略 .字段 / [下标]）

    用于判断某个自增变量是否真的被本次请求引用：
    没在 url / headers / params / body 里出现的变量不取号，也不返回给前端展示。
    """
    if isinstance(obj, str):
        text = obj
    else:
        try:
            text = json.dumps(obj, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(obj)
    names = set()
    for m in _VAR_REF_RE.finditer(text):
        head = re.split(r"[.\[]", m.group(1).strip(), 1)[0].strip()
        if head:
            names.add(head)
    return names


# ═══════════════════════════════════════════════════════════════
# JSON 路径取值
# ═══════════════════════════════════════════════════════════════

def get_by_path(obj, path):
    """按 path 从 JSON 对象 obj 中取值

    对应 YAML 配置中的 extract / json_path 里写的路径，例如：
      YAML写 "[0].id"       → 匹配顶层数组第0项的 id 字段
      YAML写 "data.token"   → 匹配 data 对象下的 token 字段
      YAML写 "items[0].name"→ 匹配 items 数组第0项的 name 字段

    返回值：找到则返回对应值，路径不存在或类型不匹配则返回 None
    """
    # ── 第一步：将 path 字符串按 "." 拆解为 token 序列 ──
    tokens = []
    for part in path.split("."):
        if not part:
            continue
        # 情况1：纯数组下标，如 YAML 中的 "[0].id" 拆出 "[0]" 这段
        m = re.match(r'^\[(\d+)\]$', part)
        if m:
            tokens.append(int(m.group(1))); continue
        # 情况2：键名+数组下标，如 YAML 中的 "items[0].name" 拆出 "items[0]" 这段
        m = re.match(r'^(\w+)\[(\d+)\]$', part)
        if m:
            tokens.append(m.group(1)); tokens.append(int(m.group(2))); continue
        try:
            tokens.append(int(part))                  # 纯数字下标（无方括号）
        except ValueError:
            tokens.append(part)                       # 普通键名

    # ── 第二步：沿 token 序列逐层取值 ──
    for token in tokens:
        if obj is None:
            return None
        if isinstance(token, int):
            obj = obj[token] if isinstance(obj, list) and len(obj) > token else None
        elif isinstance(obj, dict):
            obj = obj.get(token)
        else:
            return None
    return obj


def get_by_path_with_merge(obj, path):
    """按 path 从 JSON 对象 obj 中取值，支持 [*] 通配符自动合并所有匹配数组。

    当路径包含 [*] 时，遍历所有匹配的元素并合并结果数组。
    例如：data[*].orders 会返回所有用户的 orders 合并后的数组。

    返回值：找到则返回值（可能是数组或单个值），路径不存在返回 None。
    """
    if not path or obj is None:
        return None

    # 检查是否包含 [*] 通配符
    if '[*]' not in path:
        return get_by_path(obj, path)

    # 按 "." 拆解路径，处理 [*] 通配符
    parts = path.split('.')
    return _resolve_with_wildcard(obj, parts)


def _resolve_with_wildcard(obj, parts):
    """递归解析路径，遇到 [*] 时遍历所有元素并合并结果。"""
    if not parts:
        return obj

    part = parts[0]
    remaining = parts[1:]

    if not part:
        return _resolve_with_wildcard(obj, remaining)

    # 情况1：纯 [*] 通配符
    if part == '[*]':
        if not isinstance(obj, list):
            return None
        # 遍历所有元素，递归解析剩余路径，合并结果
        results = []
        for item in obj:
            sub_result = _resolve_with_wildcard(item, remaining)
            if sub_result is not None:
                if isinstance(sub_result, list):
                    results.extend(sub_result)
                else:
                    results.append(sub_result)
        return results if results else None

    # 情况2：键名+[*]，如 "orders[*]"
    m = re.match(r'^(\w+)\[\*\]$', part)
    if m:
        key = m.group(1)
        if not isinstance(obj, dict):
            return None
        arr = obj.get(key)
        if not isinstance(arr, list):
            return None
        # 遍历数组所有元素，递归解析剩余路径，合并结果
        results = []
        for item in arr:
            sub_result = _resolve_with_wildcard(item, remaining)
            if sub_result is not None:
                if isinstance(sub_result, list):
                    results.extend(sub_result)
                else:
                    results.append(sub_result)
        return results if results else None

    # 情况3：普通键名
    if isinstance(obj, dict):
        sub_obj = obj.get(part)
        return _resolve_with_wildcard(sub_obj, remaining)

    # 情况4：数组下标
    m = re.match(r'^(\w+)\[(\d+)\]$', part)
    if m:
        key = m.group(1)
        idx = int(m.group(2))
        if not isinstance(obj, dict):
            return None
        arr = obj.get(key)
        if not isinstance(arr, list) or len(arr) <= idx:
            return None
        return _resolve_with_wildcard(arr[idx], remaining)

    # 情况5：纯数字下标
    m = re.match(r'^\[(\d+)\]$', part)
    if m:
        idx = int(m.group(1))
        if not isinstance(obj, list) or len(obj) <= idx:
            return None
        return _resolve_with_wildcard(obj[idx], remaining)

    return None


# ═══════════════════════════════════════════════════════════════
# HTTP 请求
# ═══════════════════════════════════════════════════════════════

def _supported_encodings():
    """当前环境能解压的 accept-encoding

    浏览器复制的 curl 常带 br / zstd，未装对应库时服务端真回 br，
    requests 解不出来会得到乱码响应，断言与提取全部失败。
    """
    sup = {"gzip", "deflate", "identity"}
    for mod, enc in (("brotli", "br"), ("brotlicffi", "br"), ("zstandard", "zstd")):
        try:
            importlib.import_module(mod)
            sup.add(enc)
        except Exception:
            continue
    return sup


def _strip_unsupported_encoding(headers):
    """移除本机解不了的 accept-encoding 项（原地修改传入的 headers 副本）"""
    sup = _supported_encodings()
    for name in list(headers):
        if str(name).lower() != "accept-encoding":
            continue
        tokens = [t.strip() for t in str(headers[name]).split(",") if t.strip()]
        keep = [t for t in tokens if t.split(";")[0].strip().lower() in sup]
        if len(keep) == len(tokens):
            continue
        if keep:
            headers[name] = ", ".join(keep)
        else:
            headers.pop(name)
    return headers


def _latin1_safe_headers(headers):
    """requests 按 latin-1 发送 header，含中文时直接抛 UnicodeEncodeError

    把非 latin-1 的值按 UTF-8 字节表面化（与服务端实际收到的字节一致），
    避免 curl 里带中文头就整个请求发不出去。
    """
    for name in list(headers):
        value = headers[name]
        if not isinstance(value, str):
            headers[name] = str(value)
            continue
        try:
            value.encode("latin-1")
        except UnicodeEncodeError:
            headers[name] = value.encode("utf-8").decode("latin-1")
    return headers


def do_request(req_cfg, variables=None, mock=None):
    """发送 HTTP 请求，返回结果字典

    参数：
      req_cfg    — 请求配置字典，含 url / method / headers / body / params / timeout
      variables  — 已提取的变量字典，用于替换请求配置中的 ${var_name}
      mock       — 模拟响应配置（来自 YAML 中的 mock 段），有则跳过真实请求
                   mock.response   — 模拟的响应 JSON 对象
                   mock.status_code — 模拟状态码（默认 200）
                   mock.delay      — 模拟延迟毫秒数（默认 0）

    返回字典字段：
      ok          — 请求是否成功
      status_code — HTTP 状态码
      elapsed_ms  — 请求耗时毫秒数
      resp_text   — 响应体原始文本
      resp_json   — 响应体解析后的 JSON 对象
      error       — 错误信息
      is_mock     — 是否为模拟响应
      seq_used    — 本次请求取用的自增变量 {变量名: 值}

    自增变量取号：带 seq 元数据的预设变量在每次请求前取一个号，结果原地写回
    variables，保证 ${var} 替换与调用方展示的入参快照是同一个值。
    """
    # ── 自增变量取号（每次请求一次，取号后立即落盘）──
    # 只给本次请求真正引用到的自增变量取号：入参 / mock 里没出现的变量既不消耗序号，
    # 也不会在结果卡片里冒出「自增: xxx=xxx」这种跟本次请求无关的提示
    seq_used = {}
    if _seqvars is not None and variables is not None:
        try:
            refs = _referenced_var_names(req_cfg)
            if mock:
                refs |= _referenced_var_names(mock)
            seq_used = _seqvars.assign(variables, only=refs)
        except Exception:
            seq_used = {}

    # ── mock 模式：直接返回模拟数据，不发真实请求 ──
    if mock:
        # 先替换变量，支持 ${var} 引用之前提取的值
        mock = _replace_deep(mock, variables or {})
        delay_ms = mock.get("delay", 0)
        if delay_ms > 0:
            time.sleep(delay_ms / 1000)
        resp_json   = mock.get("response")
        status_code = mock.get("status_code", 200)
        resp_text   = json.dumps(resp_json, ensure_ascii=False) if resp_json is not None else ""
        return dict(ok=True, status_code=status_code,
                    elapsed_ms=delay_ms, resp_text=resp_text,
                    resp_json=resp_json, resp_headers={},
                    error=None, is_mock=True, seq_used=seq_used)

    # ── 真实请求 ──
    req_cfg = _replace_deep(req_cfg, variables or {})
    url     = req_cfg.get("url", "")
    method  = req_cfg.get("method", "GET").upper()
    headers = dict(req_cfg.get("headers") or {})
    body    = req_cfg.get("body")
    params  = req_cfg.get("params")
    timeout = req_cfg.get("timeout", 15)

    # timeout 显式校验为数字，非法时回退默认值（不抛异常）
    try:
        timeout = float(timeout)
        if timeout <= 0:
            timeout = 15
    except (TypeError, ValueError):
        timeout = 15

    kwargs = {"headers": headers, "timeout": timeout, "verify": False}
    if params:
        kwargs["params"] = params
    if body:
        # 大小写不敏感查找 Content-Type
        ct = ""
        for _hk, _hv in headers.items():
            if str(_hk).lower() == "content-type":
                ct = str(_hv).lower()
                break
        if "form" in ct:
            kwargs["data"] = body
        elif isinstance(body, (dict, list)):
            kwargs["json"] = body
        else:
            # 字符串 body 是用户 / curl 给的原始报文，必须原样发送：
            # 走 json= 会被 requests 再 json.dumps 一次，外层多一对引号导致请求失败
            if not ct:
                guess = str(body).lstrip()[:1]
                headers["content-type"] = ("application/json" if guess in ("{", "[")
                                          else "application/x-www-form-urlencoded")
            kwargs["data"] = str(body).encode("utf-8")
    elif method in ("POST", "PUT", "PATCH") and body is None:
        # POST/PUT/PATCH 无 body 时传空字符串，避免部分服务器拒绝空 body 请求
        kwargs["data"] = ""

    # 移除本机解不了的 accept-encoding（br / zstd），否则响应会乱码
    _strip_unsupported_encoding(headers)
    # 含中文的 header 做 latin-1 表面化，否则 requests 直接报错
    _latin1_safe_headers(headers)

    # 移除 Content-Length，让 requests 库根据实际 body 自动计算
    # 保留原始值可能导致不匹配（如 curl 转换后 body 被剥离但 Content-Length 仍为原值）
    headers.pop("Content-Length", None)
    headers.pop("content-length", None)

    t0 = time.time()
    try:
        resp       = requests.request(method, url, **kwargs)
        elapsed_ms = int((time.time() - t0) * 1000)
        resp_json  = None
        try:
            resp_json = resp.json()
        except Exception:
            pass
        # 提取响应头（供断言使用）
        resp_hdrs  = dict(resp.headers) if resp.headers else {}
        return dict(ok=True, status_code=resp.status_code,
                    elapsed_ms=elapsed_ms, resp_text=resp.text,
                    resp_json=resp_json, resp_headers=resp_hdrs,
                    error=None, is_mock=False, seq_used=seq_used)
    except requests.exceptions.Timeout:
        return dict(ok=False, status_code=None,
                    elapsed_ms=int((time.time() - t0) * 1000),
                    resp_text="", resp_json=None, resp_headers={},
                    error="请求超时", is_mock=False, seq_used=seq_used)
    except Exception as e:
        return dict(ok=False, status_code=None,
                    elapsed_ms=int((time.time() - t0) * 1000),
                    resp_text="", resp_json=None, resp_headers={},
                    error=str(e), is_mock=False, seq_used=seq_used)


# ═══════════════════════════════════════════════════════════════
# 断言
# ═══════════════════════════════════════════════════════════════

def run_assert(assert_cfg, status_code, resp_text, resp_json,
              elapsed_ms=0, resp_headers=None):
    """执行断言，返回 (passed: bool, messages: list[str])

    断言规则（全部可选，不写则跳过）：
      status_code      — 期望的 HTTP 状态码，如 200、201
      body_contains    — 响应体必须包含的字符串
      body_not_contains — 响应体不得包含的字符串
      json_path        — JSON 路径断言，键为路径，值为期望值
                         例：{"data.id": "42"} 表示响应 data.id 字段应等于 "42"
      json_path_exists — JSON 路径必须存在（值不为 None），列表形式
                         例：["data.token", "data.list[0]"]
      response_time    — 响应时间上限（毫秒），如 1000 表示 ≤1s
      header_contains  — 响应头断言，键为 header 名，值为期望包含的字符串
                         例：{"Content-Type": "json"}

    每条断言产生一条彩色标签（ok 或 fail），存入 messages 列表
    """
    if not assert_cfg:
        return True, []

    msgs     = []
    all_pass = True

    # 状态码断言
    expected = assert_cfg.get("status_code")
    if expected is not None:
        if status_code == int(expected):
            msgs.append(P.ok(f"status={status_code}"))
        else:
            msgs.append(P.fail(f"status 期望={expected} 实际={status_code}"))
            all_pass = False

    # 响应体包含断言
    contains = assert_cfg.get("body_contains")
    if contains:
        if str(contains) in resp_text:
            msgs.append(P.ok(f'含="{contains}"'))
        else:
            msgs.append(P.fail(f'含="{contains}" 未找到'))
            all_pass = False

    # 响应体不包含断言
    not_contains = assert_cfg.get("body_not_contains")
    if not_contains:
        if str(not_contains) not in resp_text:
            msgs.append(P.ok(f'不含="{not_contains}"'))
        else:
            msgs.append(P.fail(f'不含="{not_contains}" 但实际包含'))
            all_pass = False

    # JSON 路径断言
    for path, expected_val in (assert_cfg.get("json_path") or {}).items():
        actual = get_by_path(resp_json, path)
        if str(actual) == str(expected_val):
            msgs.append(P.ok(f"{path}={expected_val}"))
        else:
            msgs.append(P.fail(f"{path} 期望={expected_val} 实际={actual}"))
            all_pass = False

    # JSON 路径存在断言（值不为 None 即可）
    exists_paths = assert_cfg.get("json_path_exists")
    if exists_paths:
        if isinstance(exists_paths, str):
            exists_paths = [exists_paths]
        for path in exists_paths:
            actual = get_by_path(resp_json, path)
            if actual is not None:
                msgs.append(P.ok(f"{path} 存在"))
            else:
                msgs.append(P.fail(f"{path} 不存在"))
                all_pass = False

    # 响应时间断言（毫秒）
    time_limit = assert_cfg.get("response_time")
    if time_limit is not None:
        limit_ms = int(time_limit)
        if elapsed_ms <= limit_ms:
            msgs.append(P.ok(f"耗时 {elapsed_ms}ms ≤ {limit_ms}ms"))
        else:
            msgs.append(P.fail(f"耗时 {elapsed_ms}ms > {limit_ms}ms"))
            all_pass = False

    # 响应头断言
    header_asserts = assert_cfg.get("header_contains")
    if header_asserts and isinstance(header_asserts, dict) and resp_headers:
        for hname, hval in header_asserts.items():
            # 不区分大小写查找 header
            actual_hval = None
            for k, v in resp_headers.items():
                if k.lower() == hname.lower():
                    actual_hval = v
                    break
            if actual_hval is not None and str(hval) in str(actual_hval):
                msgs.append(P.ok(f'header[{hname}] 含 "{hval}"'))
            else:
                msgs.append(P.fail(f'header[{hname}] 期望含 "{hval}" 实际={actual_hval}'))
                all_pass = False

    return all_pass, msgs


# ═══════════════════════════════════════════════════════════════
# 变量提取
# ═══════════════════════════════════════════════════════════════

def do_extract(extract, extract_list, resp_json, variables):
    """从响应 JSON 中提取变量，写入 variables 字典

    参数：
      extract       — 单值提取字典，键为变量名，值为 JSON 路径
                      例：{"postId": "[0].id"} → 从响应第0项取 id，存为变量 postId
      extract_list  — 数组提取字典，键为变量名，值为 JSON 路径
                      例：{"allIds": "data.ids"} → 取整个数组存为变量 allIds
      resp_json     — 响应解析后的 JSON 对象
      variables     — 已提取变量字典，新值追加写入

    返回：提取信息列表，如 ["${postId}=42", "${allIds}=[5项]"]
    """
    info_parts = []

    for var_name, path in (extract or {}).items():
        # 路径含 [*] 通配符时自动合并所有匹配元素（无通配时行为与 get_by_path 一致）
        val = get_by_path_with_merge(resp_json, path)
        if val is not None:
            variables[var_name] = val
            info_parts.append(f"${{{var_name}}}={str(val)[:50]}")
            P.print_extract(var_name, val)
        else:
            P.print_extract_fail(var_name, path)

    for var_name, path in (extract_list or {}).items():
        # 路径含 [*] 通配符时自动合并所有匹配元素（无通配时行为与 get_by_path 一致）
        val = get_by_path_with_merge(resp_json, path)
        if isinstance(val, list):
            variables[var_name] = val
            info_parts.append(f"${{{var_name}}}=[{len(val)}项]")
            P.print_extract(var_name, val, is_list=True)
        else:
            P.print_extract_fail(var_name, path)

    return info_parts


# ═══════════════════════════════════════════════════════════════
# foreach 数据驱动循环解析
# ═══════════════════════════════════════════════════════════════

FOREACH_MAX_ITEMS = 200   # 数据驱动循环的安全上限（防误跑海量数据）


def parse_foreach(foreach_cfg, variables):
    """解析步骤的 foreach 数据驱动配置

    YAML 写法：
      foreach: ids                                    # 简写：数据源与注入变量同名
      foreach: {from: ids, item: itemId, limit: 10}
      foreach: {from: uids, ids, item: uid, id}       # 多数据源：逗号分隔，按位置配对

    多数据源说明：多个平行数组逐轮按位置配对（第 i 轮取每个数组的第 i 条），
    长度不一致时按最短的执行；from 与 item 的数量须一一对应（item 省略则与源同名）。

    参数：
      foreach_cfg — 步骤中的 foreach 配置（字符串 / dict / None）
      variables   — 当前变量字典

    返回 (items, item_var, note, error)：
      未配置 foreach     → (None, None, None, None)
      配置或数据源有问题 → (None, None, None, '错误说明')
      单源               → (元素列表, 注入变量名字符串, 提示文本或 None, None)
      多源               → (元组列表, 注入变量名列表, 提示文本或 None, None)
    """
    if not foreach_cfg:
        return None, None, None, None

    if isinstance(foreach_cfg, str):
        src_names = [s.strip() for s in foreach_cfg.split(",") if s.strip()]
        item_names = list(src_names)
        limit = None
    elif isinstance(foreach_cfg, dict):
        src_names = [s.strip() for s in str(foreach_cfg.get("from", "") or "").split(",") if s.strip()]
        item_names = [s.strip() for s in str(foreach_cfg.get("item", "") or "").split(",") if s.strip()]
        limit = foreach_cfg.get("limit")
        if not src_names:
            return None, None, None, "foreach 缺少 from 数据源变量名"
        if not item_names:
            item_names = list(src_names)
    else:
        return None, None, None, "foreach 配置格式错误（应为变量名或 {from, item, limit}）"

    if len(item_names) != len(src_names):
        return None, None, None, (f"foreach 的 item 有 {len(item_names)} 个变量名，"
                                  f"与 from 的 {len(src_names)} 个数据源数量不一致，须一一对应")

    multi = len(src_names) > 1

    # 各数据源取值（任一源不存在直接报错）
    src_lists = []
    for nm in src_names:
        if nm not in variables:
            return None, None, None, f"数据源变量 {nm} 不存在（可能上游提取失败或未执行）"
        v = variables[nm]
        src_lists.append(list(v) if isinstance(v, list) else [v])

    note_parts = []
    lens = [len(l) for l in src_lists]
    if multi:
        if len(set(lens)) > 1:
            detail = "、".join(f"{nm} {len(l)} 条" for nm, l in zip(src_names, src_lists))
            note_parts.append(f"（数据源长度不一致：{detail}，按最短 {min(lens)} 条配对执行）")
        items = [tuple(t) for t in zip(*src_lists)]
    else:
        if not isinstance(variables[src_names[0]], list):
            note_parts.append(f"（{src_names[0]} 为单条数据，按 1 次执行）")
        items = src_lists[0]

    if not items:
        if multi:
            return None, None, None, "数据源为空，没有可执行的数据（" + "、".join(src_names) + "）"
        return None, None, None, f"数据源变量 {src_names[0]} 为空，没有可执行的数据"

    # limit：只取前 N 条
    if limit is not None:
        try:
            limit_n = int(limit)
        except (TypeError, ValueError):
            limit_n = 0
        if limit_n > 0 and len(items) > limit_n:
            note_parts.append(f"（共 {len(items)} 条，按 limit 只取前 {limit_n} 条）")
            items = items[:limit_n]

    # 安全上限
    if len(items) > FOREACH_MAX_ITEMS:
        note_parts.append(f"（共 {len(items)} 条，超过安全上限 {FOREACH_MAX_ITEMS}，本次只跑前 {FOREACH_MAX_ITEMS} 条）")
        items = items[:FOREACH_MAX_ITEMS]

    item_ret = item_names if multi else item_names[0]
    return items, item_ret, (" ".join(note_parts) or None), None


def _inject_foreach(item_var, fe_val, variables):
    """把当前轮数据注入变量字典：单源直接赋值；多源（item_var 为列表）按名逐个注入"""
    if isinstance(item_var, (list, tuple)):
        for var, val in zip(item_var, fe_val):
            variables[var] = val
    else:
        variables[item_var] = fe_val


_FOREACH_MISSING = object()


def save_foreach_scope(item_var, variables):
    """保存 foreach 注入变量在执行前的原值，供步骤结束后恢复。

    foreach 的 item 变量只在“当前步骤”内有意义；若不恢复，会残留在行级
    变量字典中，污染后续步骤的同名变量（例如覆盖参数化行传入的同名值）。
    返回 {变量名: 原值或 _FOREACH_MISSING}。
    """
    names = list(item_var) if isinstance(item_var, (list, tuple)) else [item_var]
    return {n: variables.get(n, _FOREACH_MISSING) for n in names}


def restore_foreach_scope(snapshot, variables):
    """把 foreach 注入变量恢复到执行前的状态：原先不存在则删除，原先存在则还原"""
    for n, v in snapshot.items():
        if v is _FOREACH_MISSING:
            variables.pop(n, None)
        else:
            variables[n] = v


def find_unresolved_refs(obj):
    """递归收集对象中仍未被替换的 ${var} 变量名（按出现顺序去重）。

    用于提示 YAML 里引用顺序错误/变量名拼错的情况：引擎对找不到的变量
    会原样保留，若直接发出，URL/请求体里就会出现 ${xxx} 字面量。
    """
    found = []
    def _walk(x):
        if isinstance(x, dict):
            for v in x.values():
                _walk(v)
        elif isinstance(x, list):
            for v in x:
                _walk(v)
        elif isinstance(x, str):
            for m in _VAR_REF_RE.finditer(x):
                name = m.group(1)
                if name not in found:
                    found.append(name)
    _walk(obj)
    return found


def _foreach_disp(item_var, fe_val):
    """foreach 当前轮的展示文本：单源 'uid = 7'；多源 'uid = 1, id = 10'"""
    if isinstance(item_var, (list, tuple)):
        return ", ".join(f"{var} = {val}" for var, val in zip(item_var, fe_val))
    return f"{item_var} = {fe_val}"


def _foreach_var_label(item_var):
    """变量名标签：单源返回名字；多源拼成 'uid, id'"""
    if isinstance(item_var, (list, tuple)):
        return ", ".join(str(x) for x in item_var)
    return str(item_var)


# ═══════════════════════════════════════════════════════════════
# 单步骤单次执行（供 run_steps 调用）
# ═══════════════════════════════════════════════════════════════

def _run_step_once(step_name, loop_i, step_loop,
                   req_cfg, assert_cfg, extract, extract_list, variables, mock=None,
                   foreach_info=None):
    """单步骤单次执行的完整流程：请求 → 断言 → 提取变量 → 构造记录

    参数：
      step_name     — 步骤名称（如 “获取帖子列表”）
      loop_i        — 当前循环轮次（如第2轮）
      step_loop     — 步骤总循环次数
      req_cfg       — 本步骤的请求配置
      assert_cfg    — 本步骤的断言配置
      extract       — 单值提取配置
      extract_list  — 数组提取配置
      variables     — 全局变量字典（提取的新值追加写入）
      mock          — 模拟响应配置（来自步骤中的 mock 段）
      foreach_info  — 数据驱动上下文 (数据序号, 数据总数, 当前元素值)，非 foreach 步骤为 None

    返回 (passed, record_dict)：
      passed   — 断言是否全部通过（请求失败也为 False）
      record   — 结果记录字典，用于后续汇总和保存
    """
    # ── 记录标签：含数据驱动轮次与循环轮次 ──
    if foreach_info:
        fe_i, fe_total, _fe_val = foreach_info
        label = f"{step_name}[数据{fe_i}/{fe_total}]"
        if step_loop > 1:
            label += f"[{loop_i}/{step_loop}]"
    else:
        label = f"{step_name}[{loop_i}]" if step_loop > 1 else step_name

    result = do_request(req_cfg, variables, mock=mock)

    # ── 请求失败 ──
    if result["error"]:
        P.print_error(result["error"])
        return False, {"步骤": label, "状态码": "ERR",
                        "耗时(ms)": result["elapsed_ms"],
                        "断言结果": "FAIL", "提取变量": "",
                        "响应体": result["error"]}

    # ── 请求成功 → 断言 + 提取 ──
    passed, msgs = run_assert(assert_cfg, result["status_code"],
                              result["resp_text"], result["resp_json"],
                              elapsed_ms=result["elapsed_ms"],
                              resp_headers=result.get("resp_headers"))
    P.print_result(result["status_code"], result["elapsed_ms"],
                   msgs, result["resp_text"], show_preview=False,
                   resp_json=result["resp_json"])

    info_parts = []
    if result["resp_json"] is not None:
        info_parts = do_extract(extract, extract_list,
                                result["resp_json"], variables)

    record = {"步骤": label, "状态码": result["status_code"],
              "耗时(ms)": result["elapsed_ms"],
              "断言结果": "PASS" if passed else "FAIL",
              "提取变量": " | ".join(info_parts),
              "响应体": result["resp_text"][:500]}
    return passed, record


# ═══════════════════════════════════════════════════════════════
# 单接口循环测试（公开接口）
# ═══════════════════════════════════════════════════════════════

def run_single(cfg, output_dir):
    """单接口循环测试

    流程：
      1. 执行 setup 前置条件，计算变量
      2. 打印测试标题
      3. 循环 N 次发送同一请求
      4. 每轮：请求 → 断言 → 打印结果 → 记录
      5. 汇总统计（通过/失败数）
      6. 保存结果到 xlsx/csv

    参数：
      cfg        — YAML 配置字典，含 name / loop / interval / request / assert / setup
      output_dir — 结果文件保存目录

    返回 True 表示全部通过
    """
    name         = cfg.get("name", "接口测试")
    loop         = int(cfg.get("loop", 1))
    interval     = float(cfg.get("interval", 0))
    stop_on_fail = cfg.get("stop_on_fail", False)
    req_cfg      = cfg.get("request", {})
    assert_cfg   = cfg.get("assert", {})
    mock_cfg     = cfg.get("mock")
    setup_cfg    = cfg.get("setup")
    extract      = cfg.get("extract", {})
    extract_list = cfg.get("extract_list", {})

    # ── 执行前置条件 ──
    setup_vars, setup_errors = eval_setup(setup_cfg)
    variables = dict(setup_vars)  # 变量跨轮次累积

    P.print_start(name, req_cfg.get("method", "GET"), req_cfg.get("url", ""),
                  loop, interval, mode="single")

    records    = []
    pass_count = 0

    for i in range(1, loop + 1):
        P.print_request_line(i, loop, req_cfg.get("method", "GET"), req_cfg.get("url", ""),
                              is_mock=bool(mock_cfg))
        result = do_request(req_cfg, variables=variables, mock=mock_cfg)

        if result["error"]:
            # ── 请求失败 ──
            P.print_error(result["error"])
            records.append({"轮次": i, "状态码": "ERR",
                             "耗时(ms)": result["elapsed_ms"],
                             "断言结果": "FAIL", "提取变量": "", "响应体": result["error"]})
        else:
            # ── 请求成功 → 断言 + 提取 ──
            passed, msgs = run_assert(assert_cfg, result["status_code"],
                                      result["resp_text"], result["resp_json"],
                                      elapsed_ms=result["elapsed_ms"],
                                      resp_headers=result.get("resp_headers"))
            P.print_result(result["status_code"], result["elapsed_ms"],
                           msgs, result["resp_text"], show_preview=True,
                           resp_json=result["resp_json"])

            info_parts = []
            if result["resp_json"] is not None:
                info_parts = do_extract(extract, extract_list,
                                        result["resp_json"], variables)

            if passed:
                pass_count += 1
            records.append({"轮次": i, "状态码": result["status_code"],
                             "耗时(ms)": result["elapsed_ms"],
                             "断言结果": "PASS" if passed else "FAIL",
                             "提取变量": " | ".join(info_parts),
                             "响应体": result["resp_text"][:500]})
            if stop_on_fail and not passed:
                break

        print()
        if i < loop and interval > 0:
            time.sleep(interval)

    elapsed_list = [r["耗时(ms)"] for r in records if isinstance(r["耗时(ms)"], int)]
    P.print_summary_single(len(records), pass_count, elapsed_list, variables)
    save_result(name, records, output_dir)
    return pass_count == len(records)


# ═══════════════════════════════════════════════════════════════
# 多步骤串联测试（公开接口）
# ═══════════════════════════════════════════════════════════════

def run_steps(cfg, output_dir):
    """多步骤串联测试

    流程：
      1. 执行 setup 前置条件，计算变量
      2. 打印测试标题
      3. 遍历每个步骤：
         - skip_on_fail=True 且前面有失败 → 跳过此步骤
         - 否则执行请求 → 断言 → 提取变量 → 记录
      4. 汇总统计（通过/失败数 + 变量列表）
      5. 保存结果到 xlsx/csv

    参数：
      cfg        — YAML 配置字典，含 name / steps / setup（每步含 request/assert/extract）
      output_dir — 结果文件保存目录

    返回 True 表示全部通过
    """
    name      = cfg.get("name", "串联测试")
    steps     = cfg.get("steps", [])
    setup_cfg = cfg.get("setup")

    # ── 执行前置条件 ──
    setup_vars, setup_errors = eval_setup(setup_cfg)
    variables = dict(setup_vars)  # setup 变量作为初始值
    records   = []
    all_pass  = True

    P.print_start(name, "", "", len(steps), 0, mode="steps")

    for idx, step in enumerate(steps, 1):
        step_name     = step.get("name", f"Step{idx}")
        req_cfg       = step.get("request", {})
        assert_cfg    = step.get("assert", {})
        extract       = step.get("extract", {})
        extract_list  = step.get("extract_list", {})
        step_loop     = int(step.get("loop", 1))
        step_interval = float(step.get("interval", 0))
        skip_on_fail  = step.get("skip_on_fail", False)
        step_mock     = step.get("mock")

        # ── 步骤级 setup（覆盖根级同名变量）──
        step_setup_cfg = step.get("setup")
        if step_setup_cfg and isinstance(step_setup_cfg, dict):
            step_setup_vars, step_setup_errors = eval_setup(step_setup_cfg)
            variables.update(step_setup_vars)

        # ── 跳过判断 ──
        if skip_on_fail and not all_pass:
            P.print_step_skipped(idx, len(steps), step_name)
            records.append({"步骤": step_name, "状态码": "SKIP",
                             "耗时(ms)": 0, "断言结果": "SKIP",
                             "提取变量": "", "响应体": ""})
            continue

        # ── foreach 数据驱动解析（用上游提取的数组逐条驱动本步骤）──
        fe_items, fe_item_var, fe_note, fe_error = parse_foreach(step.get("foreach"), variables)
        if fe_error:
            P.print_error(f"foreach 配置异常：{fe_error}")
            records.append({"步骤": step_name, "状态码": "ERR",
                             "耗时(ms)": 0, "断言结果": "FAIL",
                             "提取变量": "", "响应体": f"foreach 配置异常：{fe_error}"})
            all_pass = False
            continue
        if fe_note:
            print(f"  {fe_note}")

        # ── 打印步骤标题 ──
        if fe_items is not None:
            # foreach：URL 预展示用第 1 条数据替换，避免打印整个数组
            preview_vars = dict(variables)
            _inject_foreach(fe_item_var, fe_items[0], preview_vars)
            url_display = _replace(req_cfg.get("url", ""), preview_vars)
            P.print_step(idx, len(steps), step_name,
                         req_cfg.get("method", "GET"), url_display,
                         step_loop, step_interval)
            print(f"  数据驱动 {len(fe_items)} 条（变量 {_foreach_var_label(fe_item_var)}，上方 URL 用第 1 条预展示）")
        else:
            url_display = _replace(req_cfg.get("url", ""), variables)
            P.print_step(idx, len(steps), step_name,
                         req_cfg.get("method", "GET"), url_display,
                         step_loop, step_interval)

        # ── 执行步骤（foreach 逐条驱动 → loop 重复；无 foreach 时仅 loop）──
        fe_scope = save_foreach_scope(fe_item_var, variables) if fe_items is not None else None
        fe_iter = list(enumerate(fe_items, 1)) if fe_items is not None else [(None, None)]
        for fe_i, fe_val in fe_iter:
            if fe_items is not None:
                _inject_foreach(fe_item_var, fe_val, variables)
                fe_disp = _foreach_disp(fe_item_var, fe_val)
                if len(fe_disp) > 80:
                    fe_disp = fe_disp[:80] + "…"
                print(f"  [数据 {fe_i}/{len(fe_items)}]  {fe_disp}")

            for loop_i in range(1, step_loop + 1):
                if step_loop > 1:
                    print(f"  [{loop_i}/{step_loop}]")

                passed, record = _run_step_once(
                    step_name, loop_i, step_loop,
                    req_cfg, assert_cfg, extract, extract_list, variables,
                    mock=step_mock,
                    foreach_info=(fe_i, len(fe_items), fe_val) if fe_items is not None else None)

                if not passed:
                    all_pass = False
                records.append(record)

                if loop_i < step_loop and step_interval > 0:
                    time.sleep(step_interval)

            if fe_items is not None and fe_i < len(fe_items) and step_interval > 0:
                time.sleep(step_interval)

        # 恢复 foreach 注入变量，避免污染后续步骤（如覆盖同名提取变量/参数化变量）
        if fe_scope is not None:
            restore_foreach_scope(fe_scope, variables)

        print()

    P.print_summary_steps(records, variables)
    save_result(name, records, output_dir)
    return all_pass


# ═══════════════════════════════════════════════════════════════
# 保存结果
# ═══════════════════════════════════════════════════════════════

def save_result(name, records, output_dir):
    """保存测试结果到文件

    保存策略：
      优先保存为 .xlsx（需 pandas 库）
      pandas 不可用时回退保存为 .csv

    文件命名：{safe_name}_{YYYYMMDD_HHMMSS}.xlsx

    参数：
      name       — 测试名称
      records    — 结果记录列表（每条是一个字典）
      output_dir — 保存目录
    """
    if not output_dir or not records:
        return
    os.makedirs(output_dir, exist_ok=True)
    today     = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in name)

    try:
        import pandas as pd
        filepath = os.path.join(output_dir, f"{safe_name}_{today}.xlsx")
        pd.DataFrame(records).to_excel(filepath, index=False)
    except ImportError:
        filepath = os.path.join(output_dir, f"{safe_name}_{today}.csv")
        with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=records[0].keys())
            writer.writeheader()
            writer.writerows(records)

    P.print_saved(filepath)


# ═══════════════════════════════════════════════════════════════
# Excel 参数文件解析（批量参数化与定时任务参数化共用同一实现）
# ═══════════════════════════════════════════════════════════════

def _excel_cell_display(cell):
    """openpyxl 单元格 → 与 Excel 显示一致的字符串
    - datetime（纯日期）→ "YYYY-MM-DD"；含时间 → "YYYY-MM-DD HH:MM:SS"
    - time → "HH:MM:SS"；百分比格式 → "85%"；整数值浮点 → "1"
    - None → ""
    """
    import datetime as _dt
    v = cell.value
    if v is None:
        return ""
    if isinstance(v, _dt.datetime):
        if v.hour == 0 and v.minute == 0 and v.second == 0 and v.microsecond == 0:
            return f"{v.year}-{v.month:02d}-{v.day:02d}"
        return f"{v.year}-{v.month:02d}-{v.day:02d} {v.hour:02d}:{v.minute:02d}:{v.second:02d}"
    if isinstance(v, _dt.time):
        return f"{v.hour:02d}:{v.minute:02d}:{v.second:02d}"
    if isinstance(v, _dt.date):
        return f"{v.year}-{v.month:02d}-{v.day:02d}"
    if isinstance(v, (int, float)):
        nf = (cell.number_format or "").lower()
        if "%" in nf:
            return f"{v * 100:g}%"
        if isinstance(v, float) and v == int(v) and abs(v) < 1e15:
            return str(int(v))
    return str(v)


def parse_excel_rows(source, filename=""):
    """解析 Excel 参数文件（.xlsx / .xls），返回 (headers, all_rows)

    source 支持两种形态：
      - 文件路径（str）：定时任务从 data/_schedule_params/ 读取已保存的 Excel
      - 文件对象：Flask 上传文件（request.files["file"]，具备 .read()）
    单元格值按 Excel 显示效果转为字符串（日期、百分比、整数浮点等），空行自动过滤。
    """
    if isinstance(source, str):
        ext = os.path.splitext(source)[1].lower()
        path = source
    else:
        ext = os.path.splitext(filename or getattr(source, "filename", "") or "")[1].lower()
        path = None

    if ext == ".xls":
        import xlrd
        raw = open(path, "rb").read() if path else source.read()
        wb = xlrd.open_workbook(file_contents=raw)
        ws = wb.sheet_by_index(0)
        if ws.nrows < 1:
            return [], []

        def _xls_cell_str(r, c):
            cell = ws.cell(r, c)
            if cell.ctype == xlrd.XL_CELL_DATE:
                dt = xlrd.xldate_as_tuple(cell.value, wb.datemode)
                if dt[3] == 0 and dt[4] == 0 and dt[5] == 0:
                    return f"{dt[0]:04d}-{dt[1]:02d}-{dt[2]:02d}"
                return f"{dt[0]:04d}-{dt[1]:02d}-{dt[2]:02d} {dt[3]:02d}:{dt[4]:02d}:{dt[5]:02d}"
            if cell.ctype == xlrd.XL_CELL_NUMBER:
                v = cell.value
                if isinstance(v, float) and v == int(v) and abs(v) < 1e15:
                    return str(int(v))
            return str(cell.value).strip() if cell.value is not None else ""

        headers = [_xls_cell_str(0, c) for c in range(ws.ncols)]
        all_rows = []
        for r in range(1, ws.nrows):
            row = [_xls_cell_str(r, c) for c in range(ws.ncols)]
            if any(cell.strip() != "" for cell in row):
                all_rows.append(row)
        return headers, all_rows

    # .xlsx（openpyxl 非 read_only 以获取 number_format）
    import openpyxl
    target = path if path else source
    wb = openpyxl.load_workbook(target, read_only=False, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows())
    if not rows:
        wb.close()
        return [], []
    headers = [_excel_cell_display(c) for c in rows[0]]
    all_rows = []
    for row in rows[1:]:
        str_row = [_excel_cell_display(c) for c in row]
        if any(s.strip() != "" for s in str_row):
            all_rows.append(str_row)
    wb.close()
    return headers, all_rows


# ── 参数值类型转换（批量参数化 / 定时任务参数化共用） ──

def normalize_date_value(value, type_str):
    """日期/日期时间类型转换：
    - Excel 日期序列号（如 45292 / 45292.5215，1900 日期系统）→ 标准日期
    - 常见日期字符串写法（2024-01-01、2024/1/1、2024年1月1日、ISO 等）→ 规范化
    - 无法识别时原样返回
    date → "YYYY-MM-DD"；datetime → "YYYY-MM-DD HH:MM:SS"
    """
    from datetime import datetime as _dt, timedelta as _td
    fmt = "%Y-%m-%d" if type_str == "date" else "%Y-%m-%d %H:%M:%S"
    s = str(value).strip()
    if not s:
        return value
    # 1) Excel 日期序列号（数字）：合理范围 1900-01-01 ~ 9999-12-31
    try:
        num = float(s)
        if 1 <= num <= 2958465:
            # Excel 1900 日期系统含虚构的 1900-02-29（闰年 bug）：
            # 序列号 <60 用 1899-12-31 起算，>=60 用 1899-12-30 起算
            epoch = _dt(1899, 12, 31) if num < 60 else _dt(1899, 12, 30)
            return (epoch + _td(days=num)).strftime(fmt)
        return value  # 超范围数字不当序列号处理，原样返回
    except ValueError:
        pass
    # 2) 常见日期字符串格式
    for pat in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
                "%Y.%m.%d", "%Y年%m月%d日 %H:%M:%S", "%Y年%m月%d日"):
        try:
            return _dt.strptime(s, pat).strftime(fmt)
        except ValueError:
            continue
    try:  # ISO 形式（如 2024-01-01T10:00:00）
        return _dt.fromisoformat(s).strftime(fmt)
    except ValueError:
        pass
    return value


def convert_param_value(value, type_str):
    """将 Excel 读取的原始字符串按用户选择的类型转换。
    读取阶段始终保持原始字符串，仅在注入变量时根据用户
    在界面明确选择的类型进行转换（默认 string 不转换）。"""
    if type_str == "number":
        s = str(value).strip()
        try:
            # 保留用户原始书写形式：带小数点/科学计数法转 float，否则转 int
            if "." in s or "e" in s.lower():
                return float(s)
            return int(s)
        except ValueError:
            return value
    if type_str == "boolean":
        return str(value).strip().lower() in ("true", "1", "yes", "y", "是")
    if type_str == "json":
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return value
    if type_str in ("date", "datetime"):
        return normalize_date_value(value, type_str)
    return value  # string（默认）不转换
