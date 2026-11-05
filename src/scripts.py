#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scripts.py — 接口的前置 / 后置脚本执行器

用例里的每个 step 可以带两段 Postman 风格 JavaScript：

    pre_script   — 请求发出前执行，能改 URL / 方法 / 请求头 / Body，能写变量
    post_script  — 响应回来后执行，能读响应、写变量、用 pm.test 加断言

两段脚本都跑在独立的 Node 子进程里（见 script_runtime.js），只暴露 pm / console，
拿不到 require / process / fs，同步死循环由 vm 超时掐断，整体由子进程超时兜底。

Python 侧只负责搬运：把变量表、当前请求、响应塞进去，把脚本改后的变量与请求取回来。
"""

import json
import os
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_DONE = "__PM_DONE__"
_DEFAULT_TIMEOUT = 8.0          # 单个脚本的最长执行时间（秒）
_MAX_RESP_BODY = 2 * 1024 * 1024  # 响应体超过 2MB 只把前 2MB 交给脚本，避免白白搬运几十兆
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _runtime_path():
    """script_runtime.js 的位置：源码目录、打包目录各试一次。"""
    cands = [os.path.join(_HERE, "script_runtime.js")]
    base = getattr(sys, "_MEIPASS", "")
    if base:
        cands.append(os.path.join(base, "src", "script_runtime.js"))
        cands.append(os.path.join(base, "script_runtime.js"))
    for p in cands:
        if os.path.exists(p):
            return p
    return ""


def node_bin():
    """找 Node 可执行文件：先看 PATH，再看 Windows 默认安装目录。"""
    p = shutil.which("node") or shutil.which("node.exe")
    if p:
        return p
    for c in (r"C:\Program Files\nodejs\node.exe",
              r"C:\Program Files (x86)\nodejs\node.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\nodejs\node.exe")):
        if c and os.path.exists(c):
            return c
    return ""


def available():
    return bool(node_bin() and _runtime_path())


def unavailable_reason():
    if not node_bin():
        return "未找到 Node.js，脚本无法执行；装好 Node（nodejs.org）后重启本工具即可"
    return "缺少脚本运行时 script_runtime.js，请重新安装本工具"


def _clip_body(text):
    s = "" if text is None else str(text)
    if len(s) <= _MAX_RESP_BODY:
        return s, False
    return s[:_MAX_RESP_BODY], True


def run_script(phase, code, variables, request=None, response=None,
               timeout=_DEFAULT_TIMEOUT, name=""):
    """执行一段脚本。

    参数：
      phase      — 'pre' 或 'post'
      code       — 脚本源码
      variables  — 当前变量表（脚本里 pm.variables.set 会写回返回值）
      request    — {method,url,headers,body,params,timeout}，前置脚本可改
      response   — {code,status,headers,body,elapsed_ms}，后置脚本可读

    返回：
      {ok, error, logs:[{level,text}], tests:[{name,passed,message}],
       vars, request, elapsed_ms}
    """
    node = node_bin()
    runtime = _runtime_path()
    if not node or not runtime:
        return {"ok": False, "error": unavailable_reason(),
                "logs": [], "tests": [], "vars": {}, "request": None}

    resp_payload = None
    if response is not None:
        body, clipped = _clip_body(response.get("body"))
        resp_payload = {
            "code": response.get("code"),
            "status": response.get("status") or response.get("code"),
            "headers": response.get("headers") or {},
            "body": body,
            "elapsed_ms": response.get("elapsed_ms") or 0,
            "truncated": clipped,
        }

    payload = {
        "phase": "post" if phase == "post" else "pre",
        "code": code or "",
        "vars": {str(k): ("" if v is None else str(v)) for k, v in (variables or {}).items()},
        "request": request,
        "response": resp_payload,
        "name": name,
    }

    import time
    t0 = time.time()
    try:
        proc = subprocess.run(
            [node, runtime],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=max(2.0, float(timeout)),
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "脚本执行超时（超过 %g 秒）" % float(timeout),
                "logs": [], "tests": [], "vars": {}, "request": None,
                "elapsed_ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"ok": False, "error": "脚本启动失败：%s" % e,
                "logs": [], "tests": [], "vars": {}, "request": None,
                "elapsed_ms": int((time.time() - t0) * 1000)}

    out = (proc.stdout or b"").decode("utf-8", "replace")
    elapsed = int((time.time() - t0) * 1000)
    idx = out.rfind(_DONE)
    if idx < 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip() \
                 or out.strip() or "没有输出"
        return {"ok": False, "error": "脚本运行异常：" + detail[:600],
                "logs": [], "tests": [], "vars": {}, "request": None,
                "elapsed_ms": elapsed}

    try:
        res = json.loads(out[idx + len(_DONE):].strip())
    except Exception as e:
        return {"ok": False, "error": "脚本结果解析失败：%s" % e,
                "logs": [], "tests": [], "vars": {}, "request": None,
                "elapsed_ms": elapsed}

    res.setdefault("ok", False)
    res["elapsed_ms"] = elapsed
    return res
