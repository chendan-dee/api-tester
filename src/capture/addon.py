#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""capture/addon.py — mitmproxy 流量捕获 addon

挂载到 mitmdump（-s addon.py），承担三件事：
1. 流量记录：每笔请求/响应的摘要与详情经后台线程批量上报
   /api/capture/report（队列满时丢弃，绝不阻塞代理事件循环）；
2. 改包 Mock：命中「启用中」的规则（独立规则库 data/_capture_mock.json）直接
   本地响应，不发真实请求；
3. 断点 / 高级弱网：请求或响应挂起等前端编辑放行（300s 超时自动放行）；
   按档位注入延迟 / 丢包 / 上下行带宽限速 / 间歇卡顿（真·逐块流式限速）。

配置来源（主服务写文件、0.5s 节流重读，改动实时生效）：
- CAPTURE_SCOPE_FILE       来源记录开关（web=本机 / device=手机）
- CAPTURE_ENGINE_CFG_FILE  功能开关（mock / weaknet / breakpoint）
- CAPTURE_MOCK_FILE        Mock 规则库

设计要点：
- 钩子内只做「组数据 + 入内存队列 / await」；断点挂起走线程池阻塞
  HTTP，不占用代理事件循环；
- 上报失败（主服务未启动等）静默丢弃，不影响抓包转发本身；
- 工具箱自身流量（回环 / 本机 IP 的 5050/5051）全程直通，
  不记录、不改包、不断点、不限速。
"""

import asyncio
import fnmatch
import gzip
import json
import os
import queue
import random
import re
import threading
import time
import urllib.request
import zlib
from datetime import datetime

from mitmproxy import http

REPORT_URL = os.environ.get("CAPTURE_REPORT_URL", "http://127.0.0.1:5050/api/capture/report")
_BREAK_URL = os.environ.get("CAPTURE_BREAK_URL", "").strip()
if not _BREAK_URL and REPORT_URL.endswith("/api/capture/report"):
    _BREAK_URL = REPORT_URL[: -len("/api/capture/report")] + "/api/capture/breakpoint/hit"

MAX_BODY = 256 * 1024           # 单个 body 上报上限（256KB，超出截断）
_Q = queue.Queue(maxsize=8000)  # 队列满时丢弃新记录，绝不阻塞代理

# 攒批上报：单条流量两次上报（pending + 终报）不再各发一次 HTTP，降低主服务压力
_BATCH_MAX = 100                # 单批最多条数
_BATCH_WAIT = 0.2               # 攒批最长等待（秒）
_SPOOL_DIR = os.environ.get("CAPTURE_SPOOL_DIR", "").strip()  # 上报失败落盘目录（主服务重启窗口不丢流量）
_SPOOL_MAX_FILES = 300          # spool 文件数上限（超出丢弃最旧）
_drop_count = 0                 # 队列满丢弃的累计条数（随批次上报给主服务）
_drop_lock = threading.Lock()

_DROP_TEXT = b"[api-tester] blocked by breakpoint"
_DROP_HEADERS = {"Content-Type": "text/plain; charset=utf-8"}

# 忽略的本机流量（访问工具箱自身、回环地址），避免噪音
_IGNORE_HOSTS = ("127.0.0.1", "localhost", "::1")
# 工具箱自身服务（本机 IP + 主服务/HTTPS 代理端口）：PC/手机浏览器访问工具箱
# 页面（轮询、静态资源）经代理时不应刷屏流量列表
_WEB_PORT = int(os.environ.get("CAPTURE_WEB_PORT", "5050"))
_TOOL_PORTS = (_WEB_PORT, _WEB_PORT + 1)
_LOCAL_HOSTS = set(_IGNORE_HOSTS)
_lip = os.environ.get("CAPTURE_LOCAL_IP", "").strip()
if _lip:
    _LOCAL_HOSTS.add(_lip)


def _should_ignore(request):
    """是否属于应忽略的本机流量（回环 / 工具箱自身）"""
    host = request.pretty_host
    if host in _IGNORE_HOSTS:
        return True
    try:
        if host in _LOCAL_HOSTS and request.port in _TOOL_PORTS:
            return True
    except Exception:
        pass
    return False


# ── 来源记录开关（web=本机回环客户端 / device=其他设备）─────
# 开关文件由工具箱主服务维护（默认全关）；0.5s 节流重读，读取失败保留旧值。
# 文件缺失时兜底全记录（保数据优先），正常路径下主服务启动即写入文件。
_SCOPE_FILE = os.environ.get("CAPTURE_SCOPE_FILE", "").strip()
_scope = {"web": True, "device": True}
_last_scope_check = 0.0


def _refresh_scope():
    """节流重读来源开关文件（最多每 0.5s 一次）"""
    global _scope, _last_scope_check
    if not _SCOPE_FILE:
        return
    now = time.monotonic()
    if now - _last_scope_check < 0.5:
        return
    _last_scope_check = now
    try:
        with open(_SCOPE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _scope = {"web": bool(data.get("web", True)), "device": bool(data.get("device", True))}
    except Exception:
        pass


# ── 通用节流文件读取（功能开关 / Mock 规则库）────────────────
def _throttled_load(path, state):
    """0.5s 节流读取 JSON 文件；读取失败保留上次值，state 为可变缓存 dict"""
    if not path:
        return state.get("value")
    now = time.monotonic()
    if now - state.get("last", 0.0) < 0.5:
        return state.get("value")
    state["last"] = now
    try:
        with open(path, "r", encoding="utf-8") as f:
            state["value"] = json.load(f)
    except Exception:
        pass  # 保留旧值（文件瞬时不可读时不影响转发）
    return state.get("value")


_ENGINE_CFG_FILE = os.environ.get("CAPTURE_ENGINE_CFG_FILE", "").strip()
_eng_state = {"value": {"mock": {"enabled": False},
                        "weaknet": {"enabled": False, "preset": "custom", "presets": {}},
                        "breakpoint": {"request": False, "response": False, "filter": ""}},
              "last": 0.0}

_MOCK_FILE = os.environ.get("CAPTURE_MOCK_FILE", "").strip()
_mock_state = {"value": {"mappings": []}, "last": 0.0}


def _eng_cfg():
    """当前功能开关（节流读取；主服务未启动时兜底全关）"""
    cfg = _throttled_load(_ENGINE_CFG_FILE, _eng_state)
    return cfg if isinstance(cfg, dict) else {}


def _mock_cfg():
    """当前 Mock 规则库（节流读取）"""
    cfg = _throttled_load(_MOCK_FILE, _mock_state)
    return cfg if isinstance(cfg, dict) else {}


# ── 改包 Mock：规则匹配与响应构造 ────────────────────────────
def _mock_path_match(pattern, path):
    """Mock 路径匹配：规则含 * 时按通配（如 /api/user/*），否则精确匹配

    与主服务 _mock_path_match（代理 Mock）保持同一语义，两库行为一致。
    """
    pattern = pattern or ""
    if "*" in pattern:
        return fnmatch.fnmatchcase(path or "", pattern)
    return pattern == (path or "")


def _match_mock(method, host, path):
    """在规则库中查找命中规则

    匹配语义（与代理 Mock 链路保持一致）：
    - enabled 缺省视为启用；
    - host 为空或 * 时匹配任意域名；method 为空时匹配任意方法；
    - path 支持 * 通配（如 /api/user/*），无 * 时精确匹配（不含 query）。
    """
    for m in _mock_cfg().get("mappings", []):
        if not isinstance(m, dict):
            continue
        if m.get("enabled", True) is False:
            continue
        if not _mock_path_match(m.get("path"), path):
            continue
        m_host = (m.get("host") or "").strip()
        if m_host and m_host != "*" and m_host != host:
            continue
        m_method = (m.get("method") or "").strip().upper()
        if m_method and m_method != (method or "").upper():
            continue
        return m
    return None


def _mock_response_parts(m, library):
    """规则 → (status, reason_message, headers, body_bytes, delay_ms, response_name)"""
    responses = m.get("responses")
    status, msg, headers, body, delay, name = 200, "", {}, "", 0, ""
    if isinstance(responses, list) and responses:
        try:
            idx = min(int(m.get("active_response") or 0), len(responses) - 1)
        except Exception:
            idx = 0
        active = responses[max(idx, 0)]
        if isinstance(active, dict):
            status = active.get("status", 200)
            msg = active.get("status_message", "") or ""
            headers = dict(active.get("headers") or {})
            body = active.get("body", "")
            delay = active.get("delay_ms", 0) or 0
            name = active.get("name", "") or ""
    else:
        # 兼容旧格式：直接取规则顶层字段
        status = m.get("status", 200)
        msg = m.get("status_message", "") or ""
        headers = dict(m.get("headers") or {})
        body = m.get("body", "")
        delay = m.get("delay_ms", 0) or 0
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False)
    if body is None:
        body = ""
    if not isinstance(body, str):
        body = str(body)
    body_bytes = body.encode("utf-8")
    # 剔除会与重算冲突的头：content-length 由 mitmproxy 自动补齐；
    # content-encoding 针对原始 body（如 gzip），改包后必然失效
    for k in list(headers.keys()):
        if str(k).lower() in ("content-length", "content-encoding", "transfer-encoding", "connection"):
            del headers[k]
    if not any(str(k).lower() == "content-type" for k in headers) and body.strip()[:1] in ("{", "["):
        headers["Content-Type"] = "application/json; charset=utf-8"
    try:
        delay = int(delay or 0)
    except Exception:
        delay = 0
    if delay <= 0:  # 与代理 Mock 一致：响应级延时优先，否则取库全局延时
        try:
            delay = int(library.get("delay_ms") or 0)
        except Exception:
            delay = 0
    try:
        status = int(status)
    except Exception:
        status = 200
    return status, msg, headers, body_bytes, delay, name


# ── 断点：挂起等前端决策 ─────────────────────────────────────
def _post_json_sync(url, payload, timeout):
    """同步 POST JSON（在事件循环之外的线程中执行），返回响应 dict 或 None"""
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _break_filter_ok(filter_text, url, method=""):
    """断点拦截匹配（升级语法，向后兼容纯关键字配置）

    - 逗号分隔多个条件；任一正条件命中且无排除条件命中 → 命中；
    - 正条件：URL 子串 / host:子串（主机名包含）/ method:GET|POST（方法精确，可 | 分隔）；
    - 排除条件：以 - 开头，命中即否决（如 -/health）；
    - 空配置 = 全部命中。
    """
    text = (filter_text or "").strip()
    if not text:
        return True
    low = (url or "").lower()
    meth = (method or "").upper()
    host = ""
    try:
        m = re.match(r"^https?://([^/?#]+)", low)
        host = m.group(1) if m else ""
    except Exception:
        pass
    has_pos = False
    pos_hit = False
    for kw in text.split(","):
        kw = kw.strip()
        if not kw:
            continue
        neg = kw.startswith("-")
        body = kw[1:].strip() if neg else kw
        if not body:
            continue
        bl = body.lower()
        if bl.startswith("method:"):
            vals = [x.strip().upper() for x in bl[len("method:"):].split("|") if x.strip()]
            hit = meth in vals
        elif bl.startswith("host:"):
            hit = bl[len("host:"):].strip() in host
        else:
            hit = bl in low
        if neg:
            if hit:
                return False  # 排除条件命中 → 整体否决
        else:
            has_pos = True
            if hit:
                pos_hit = True
    return (not has_pos) or pos_hit


def _break_flow_info(flow, phase):
    """上报给前端的断点报文快照（供编辑）"""
    req = flow.request
    info = {
        "flow_id": flow.id,  # mitmproxy 流量 id：前端 ⏸ 合成行与同一条真实记录（pending 行）去重用
        "method": req.method,
        "url": req.pretty_url,
        "host": req.pretty_host,
        "path": req.path,
        "req_headers": _headers_dict(req),
        "req_body": _decode_body(req),
    }
    if phase == "response" and flow.response is not None:
        info["status"] = flow.response.status_code
        info["resp_headers"] = _headers_dict(flow.response)
        info["resp_body"] = _decode_body(flow.response)
    return info


async def _wait_break(phase, flow):
    """挂起当前请求/响应，等前端编辑决策（超时/异常自动放行）"""
    if not _BREAK_URL:
        return {"action": "continue"}
    payload = {"phase": phase, "flow": _break_flow_info(flow, phase)}
    try:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, _post_json_sync, _BREAK_URL, payload, 320)
    except Exception:
        resp = None
    if isinstance(resp, dict) and isinstance(resp.get("decision"), dict):
        return resp["decision"]
    return {"action": "continue"}


def _apply_req_decision(flow, dec):
    """把前端编辑应用到请求（字段缺失 = 不修改）"""
    req = flow.request
    try:
        method = (dec.get("method") or "").strip().upper()
        if method:
            req.method = method
        url = (dec.get("url") or "").strip()
        if url:
            if url.startswith(("http://", "https://")):
                req.url = url
            elif url.startswith("/"):
                req.path = url  # 相对路径：仅改 path（含 query），host 不变
        headers = dec.get("headers")
        if isinstance(headers, dict):
            old_host = None
            try:
                old_host = req.headers.get("host")
            except Exception:
                pass
            req.headers.clear()
            for k, v in headers.items():
                req.headers[str(k)] = str(v)
            if old_host and not any(str(k).lower() == "host" for k in headers):
                req.headers["host"] = old_host  # 保留默认 Host，避免上游虚拟主机路由失败
        body = dec.get("body")
        if isinstance(body, str):
            req.content = body.encode("utf-8")
            if "content-encoding" in req.headers:
                del req.headers["content-encoding"]
    except Exception:
        pass


def _apply_resp_decision(flow, dec):
    """把前端编辑应用到响应（字段缺失 = 不修改）"""
    resp = flow.response
    if resp is None:
        return
    try:
        status = dec.get("status")
        if status is not None:
            try:
                si = int(status)
                if 100 <= si <= 599:
                    resp.status_code = si
            except Exception:
                pass
        headers = dec.get("headers")
        if isinstance(headers, dict):
            resp.headers.clear()
            for k, v in headers.items():
                resp.headers[str(k)] = str(v)
        body = dec.get("body")
        if isinstance(body, str):
            resp.content = body.encode("utf-8")
            if "content-encoding" in resp.headers:
                del resp.headers["content-encoding"]
    except Exception:
        pass


# ── 高级弱网模拟：延迟 / 丢包 / 上下行限速 / 间歇卡顿 ──────
# 实测基础（mitmproxy 12.x streaming 机制）：
# - 带宽限速用 flow.request/response.stream 同步回调逐块计时 sleep 实现
#   （mitmproxy 无内置限速；同步回调是唯一能真·逐块减速的点）；
# - 单流限速封顶 _THROTTLE_BUDGET_S，避免极低带宽长时间堵塞代理事件循环；
# - 流式转发时 flow.*.content 不再可用，回调内自累积原始 body（≤_BODY_CAP）
#   存入 flow.metadata 供流量详情展示；压缩体先解压再展示；
# - 间歇卡顿全局共享一个时钟（_STALL），在途流量同卡同放，模拟整网顿住。
_WN_KEYS = ("latency_ms", "bandwidth_kbps", "bandwidth_up_kbps", "loss_pct", "stall_every", "stall_ms")
_BODY_CAP = 1024 * 1024          # 流式 body 自累积上限（1MB，与主服务磁盘上限对齐）
_THROTTLE_BUDGET_S = 300.0       # 单流限速时间预算（秒，用尽后该流余下全速）
_STALL = {"until": 0.0}          # 间歇卡顿时钟：当前卡顿周期的预期结束时刻


def _weaknet_params(wn):
    """解析当前生效的弱网参数（presets[preset]；兼容旧扁平字段 delay_ms/kbps）"""
    if not isinstance(wn, dict):
        return {k: 0 for k in _WN_KEYS}
    p = wn
    presets = wn.get("presets")
    if isinstance(presets, dict):
        cand = presets.get(str(wn.get("preset") or "custom"))
        if not isinstance(cand, dict):
            cand = presets.get("custom")
        if isinstance(cand, dict):
            p = cand

    def num(key, legacy=None, cap=0.0):
        try:
            v = p.get(key)
            if v is None and legacy:
                v = p.get(legacy)
            v = float(v or 0)
        except Exception:
            v = 0.0
        if v != v or v < 0:  # NaN / 负数
            v = 0.0
        return min(v, cap)

    bandwidth = num("bandwidth_kbps", None, 102400)
    if bandwidth <= 0:
        # 旧字段 kbps 语义为 KB/s（KiB），换算为 kbps（千比特）
        bandwidth = min(num("kbps", None, 102400) * 8.0, 102400)
    # 上行带宽：0 / 缺省 = 与下行相同（兼容旧配置的上下行同速语义）
    bandwidth_up = num("bandwidth_up_kbps", None, 102400)
    if bandwidth_up <= 0:
        bandwidth_up = bandwidth

    return {
        "latency_ms": int(num("latency_ms", "delay_ms", 120000)),
        "bandwidth_kbps": bandwidth,
        "bandwidth_up_kbps": bandwidth_up,
        "loss_pct": num("loss_pct", None, 100),
        "stall_every": num("stall_every", None, 3600),
        "stall_ms": int(num("stall_ms", None, 300000)),
    }


def _stall_wait(p):
    """间歇卡顿：返回本流此刻应等待的秒数（0=不卡）

    规则：距上次卡顿结束 ≥ stall_every 秒 → 触发新卡顿（持续 stall_ms）；
    正处于卡顿中的流量等剩余时长——所有流共享同一时钟，同卡同放。

    """
    every = float(p.get("stall_every") or 0)
    ms = int(p.get("stall_ms") or 0)
    if every <= 0 or ms <= 0:
        return 0.0
    now = time.monotonic()
    end = _STALL["until"]
    if now < end:
        return min(end - now, ms / 1000.0)
    if now - end >= every:
        _STALL["until"] = now + ms / 1000.0
        return ms / 1000.0
    return 0.0


def _make_throttle_cb(flow, meta_key, over_key, bw_key="bandwidth_kbps"):
    """流式限速回调工厂（上行/下行共用；bw_key 选择限速参数：下行 bandwidth_kbps / 上行 bandwidth_up_kbps）

    每块数据到达时同步调用：按当前带宽计算该块应占传输时间并 sleep。
    由于 mitmproxy 的 stream 回调在代理事件循环内同步执行，长 sleep 会
    一并冻结其他在途流量——这正是弱网模拟想要的效果（真·堵在网络上）。
    分片 sleep 期间重读配置：关闭开关 / 调档位即时生效。
    同时把原始 body 累积进 flow.metadata（限内），补上流式转发后
    flow.*.content 不可用导致详情无法展示的空缺。
    """
    state = {"buf": bytearray(), "over": False, "spent": 0.0}

    def cb(chunk):
        if not chunk:  # 空块 = 流结束（mitmproxy 约定）
            try:
                if state["over"]:
                    flow.metadata[over_key] = True
                elif state["buf"]:
                    flow.metadata[meta_key] = bytes(state["buf"])
            except Exception:
                pass
            return chunk
        # 1) 自累积 body（限内；超限丢弃改记标记）
        if not state["over"]:
            state["buf"] += chunk
            if len(state["buf"]) > _BODY_CAP:
                state["over"] = True
                state["buf"] = bytearray()
        # 2) 重读配置（0.5s 节流缓存）：中途关闭 / 调档即时生效
        try:
            wn = _eng_cfg().get("weaknet") or {}
            p = _weaknet_params(wn) if wn.get("enabled") else None
        except Exception:
            p = None
        if not p:
            return chunk
        # 3) 间歇卡顿（全局时钟）
        stall = _stall_wait(p)
        if stall > 0:
            time.sleep(stall)
        # 4) 带宽限速：分片 sleep（≤0.25s/片），期间按新配置重算剩余
        if p[bw_key] > 0 and state["spent"] < _THROTTLE_BUDGET_S:
            kbps = p[bw_key]
            remain = min(len(chunk) * 8.0 / (kbps * 1000.0),
                         _THROTTLE_BUDGET_S - state["spent"])
            while remain > 1e-4:
                t = 0.25 if remain > 0.25 else remain
                time.sleep(t)
                state["spent"] += t
                remain -= t
                if remain <= 1e-4:
                    break
                try:
                    wn2 = _eng_cfg().get("weaknet") or {}
                    if not wn2.get("enabled"):
                        break
                    kbps2 = _weaknet_params(wn2)[bw_key]
                except Exception:
                    break
                if kbps2 <= 0:
                    break
                if abs(kbps2 - kbps) > 1e-6:  # 带宽调整：按新速率折算剩余时间
                    remain = remain * kbps / kbps2
                    kbps = kbps2
        return chunk

    return cb


def _decode_streamed(raw, msg):
    """解码流式累积的原始 body：先按 Content-Encoding 解压，再按文本展示"""
    enc = ""
    try:
        enc = (msg.headers.get("content-encoding") or "").lower()
    except Exception:
        pass
    if enc and len(raw) <= 128 * 1024:  # 压缩体：限小体积尝试解压（防解压炸弹）
        try:
            if "gzip" in enc:
                raw = gzip.decompress(raw)
            elif "deflate" in enc:
                try:
                    raw = zlib.decompress(raw)
                except Exception:
                    raw = zlib.decompress(raw, -15)  # raw deflate 流
        except Exception:
            pass  # 解压失败（流不完整等）：按原始字节降级展示
    try:
        text = raw.decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return "[二进制内容 %d 字节，未显示]" % len(raw)
    if len(text) > MAX_BODY:
        return text[:MAX_BODY] + "\n…[已截断，完整 %d 字节]" % len(raw)
    return text


def _body_text(flow, msg, meta_key, over_key):
    """流量详情 body 文本：流式自累积 → 常规 content → 降级描述"""
    try:
        over = bool(flow.metadata.get(over_key))
        buf = flow.metadata.get(meta_key)
    except Exception:
        over, buf = False, None
    if over and buf is None:
        return "[body 超过 %dMB，已省略（流式转发）]" % (_BODY_CAP // (1024 * 1024))
    if isinstance(buf, (bytes, bytearray)):
        return _decode_streamed(bytes(buf), msg)
    return _decode_body(msg)


# ── 来源辅助 ─────────────────────────────────────────────────
def _client_ip(flow):
    """客户端 IP（连接来源，mitmproxy 标准 API）"""
    try:
        if flow.client_conn and flow.client_conn.peername:
            return flow.client_conn.peername[0]
    except Exception:
        pass
    return ""


def _source_of(flow):
    """流量来源：本机回环客户端=web，其他设备（手机等）=device"""
    return "web" if _client_ip(flow) in ("127.0.0.1", "::1") else "device"


def _source_allowed(flow):
    """当前来源是否在记录开关范围内（关闭时仅不记录，代理转发不受影响）"""
    _refresh_scope()
    if flow.metadata.get("cap_replay"):  # 重放为用户主动发起，不受来源开关限制
        return True
    return _scope.get(_source_of(flow), True)


def _enqueue(record):
    """入上报队列；队列满时累计丢弃计数（绝不阻塞代理事件循环）"""
    global _drop_count
    try:
        _Q.put_nowait(record)
    except queue.Full:
        with _drop_lock:
            _drop_count += 1


# ── WebSocket 消息聚合 ─────────────────────────────────────────
# mitmproxy 12 的 WS 流量仍是 HTTPFlow（握手 101 + flow.websocket.messages），
# 消息聚合到 metadata 随记录上报（不单独建列表条目），前端在详情页展示；
# 连接存活期间按 _WS_REPORT_EVERY 节流增量上报，断连时终报。
_WS_MAX_MSGS = 100              # 单连接最多保留的消息条数（超出置截断标记）
_WS_MSG_BYTES = 1024            # 单条消息预览上限（超出截断）
_WS_REPORT_EVERY = 2.0          # 连接存活期间的增量上报间隔（秒）


def _ws_msg_text(content):
    """WebSocket 消息内容 → (展示文本, 类型)；二进制降级描述，超长截断"""
    try:
        if content is None:
            return "", "text"
        raw = content if isinstance(content, (bytes, bytearray)) else str(content).encode("utf-8")
        try:
            text = bytes(raw).decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            return "[二进制 %d 字节]" % len(raw), "bin"
    except Exception:
        return "", "bin"
    if len(text) > _WS_MSG_BYTES:
        return text[:_WS_MSG_BYTES] + "…[截断]", "text"
    return text, "text"


def _ws_payload(flow):
    """WebSocket 消息聚合快照（非 WS 流量返回 None）"""
    try:
        msgs = flow.metadata.get("cap_ws_msgs")
    except Exception:
        return None
    if msgs is None:
        return None
    try:
        trunc = bool(flow.metadata.get("cap_ws_trunc"))
    except Exception:
        trunc = False
    return {"count": len(msgs), "truncated": trunc, "messages": list(msgs)}


def _post_batch(batch):
    """批量上报一批记录（附累计丢弃计数）；成功返回 True，失败计数退回下次重报"""
    global _drop_count
    with _drop_lock:
        dropped = _drop_count
        _drop_count = 0
    payload = {"records": batch}
    if dropped:
        payload["dropped"] = dropped
    try:
        req = urllib.request.Request(
            REPORT_URL, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        with _drop_lock:
            _drop_count += dropped
        return False


def _spool_write(batch):
    """上报失败落盘（主服务重启窗口不丢流量）；spool 文件数有上限"""
    if not _SPOOL_DIR:
        return
    try:
        os.makedirs(_SPOOL_DIR, exist_ok=True)
        files = sorted(os.listdir(_SPOOL_DIR))
        if len(files) >= _SPOOL_MAX_FILES:
            for old in files[:len(files) - _SPOOL_MAX_FILES + 1]:
                try:
                    os.remove(os.path.join(_SPOOL_DIR, old))
                except Exception:
                    pass
        name = "sp_%013d_%04d.json" % (int(time.time() * 1000), random.randint(0, 9999))
        with open(os.path.join(_SPOOL_DIR, name), "w", encoding="utf-8") as f:
            json.dump(batch, f, ensure_ascii=False)
    except Exception:
        pass


def _spool_flush(max_files=20):
    """补发 spool 中的历史批次（成功即删文件；仍失败保留待下轮）"""
    if not _SPOOL_DIR or not os.path.isdir(_SPOOL_DIR):
        return
    try:
        files = sorted(os.listdir(_SPOOL_DIR))[:max_files]
    except Exception:
        return
    for fn in files:
        path = os.path.join(_SPOOL_DIR, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                batch = json.load(f)
        except Exception:
            batch = None
        if not isinstance(batch, list) or not batch:
            try:
                os.remove(path)  # 坏文件 / 空批次：直接丢弃
            except Exception:
                pass
            continue
        if _post_batch(batch):
            try:
                os.remove(path)
            except Exception:
                pass
        else:
            return  # 主服务仍不可用：保持顺序，下轮再试


def _worker():
    """后台上报线程：攒批 POST；失败落盘由空闲轮补发（主服务新旧协议均兼容）"""
    while True:
        try:
            rec = _Q.get(timeout=1)
        except queue.Empty:
            _spool_flush()  # 空闲时补发主服务重启窗口落盘的批次
            continue
        batch = [rec]
        deadline = time.monotonic() + _BATCH_WAIT
        while len(batch) < _BATCH_MAX:
            t = deadline - time.monotonic()
            if t <= 0:
                break
            try:
                batch.append(_Q.get(timeout=t))
            except queue.Empty:
                break
        if not _post_batch(batch):
            _spool_write(batch)


threading.Thread(target=_worker, daemon=True).start()


def _decode_body(msg):
    """安全提取消息 body 文本；二进制 / 超大 / 流式未缓存时降级描述"""
    try:
        content = msg.content  # 大 body 被 stream_large_bodies 流式转发时可能不可用
    except Exception:
        return "[body 未捕获（大文件流式转发）]"
    if content is None:
        return None
    size = len(content)
    if size == 0:
        return ""
    try:
        text = content.decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return "[二进制内容 %d 字节，未显示]" % size
    if len(text) > MAX_BODY:
        return text[:MAX_BODY] + "\n…[已截断，完整 %d 字节]" % size
    return text


def _headers_dict(msg):
    try:
        return {k: v for k, v in msg.headers.items()}
    except Exception:
        return {}


class CaptureAddon:
    """mitmproxy 生命周期钩子：改包 Mock / 断点 / 弱网 + 流量记录上报"""

    def __init__(self):
        # CONNECT 阶段失败流量关联映射：mitmproxy 对 CONNECT 失败不触发
        # request/response/error 钩子（直接回 502），需在 server_connect_error /
        # tls_failed_client 钩子里按连接 id 找回 flow 生成失败记录——否则
        # 「客户端未信任代理证书」「目标不可达」类流量在列表完全不可见（遗漏）。
        self._pending_connects = {}   # server_conn.id -> flow
        self._pending_clients = {}    # client_conn.id -> flow

    def _build_record(self, flow, error=None, pending=False):
        req = flow.request
        resp = flow.response
        client_ip = _client_ip(flow)
        src = "web" if client_ip in ("127.0.0.1", "::1") else "device"
        if pending:
            # 在途记录（请求头阶段上报）：仅摘要 + 请求头——不读 body（此时 body 尚未
            # 转发，读取会破坏流式限速）；响应 / body 等待终报按 id 覆盖
            replay_p = False
            try:
                replay_p = bool(req.headers.get("X-ApiTester-Replay"))
            except Exception:
                pass
            ws = _ws_payload(flow)
            return {
                "id": flow.id,
                "time": datetime.fromtimestamp(req.timestamp_start).strftime("%H:%M:%S"),
                "ts": req.timestamp_start,
                "method": req.method, "scheme": req.scheme, "host": req.pretty_host,
                "port": req.port, "path": req.path,
                "status": 101 if ws is not None else None,
                "content_type": "", "size": 0, "duration_ms": None,
                "client": client_ip, "source": src,
                "req_headers": _headers_dict(req), "req_body": None,
                "resp_headers": {}, "resp_body": None,
                "error": None, "mock": None, "replay": replay_p, "loss": False,
                "ws": ws,
                "state": "pending",
            }
        mock = None
        try:
            mock = flow.metadata.get("cap_mock")
        except Exception:
            mock = None
        try:
            replay = bool(flow.metadata.get("cap_replay"))
        except Exception:
            replay = False
        duration_ms = 0
        try:
            if resp is not None:
                duration_ms = int((resp.timestamp_end - req.timestamp_start) * 1000)
            ws_end = flow.metadata.get("cap_ws_end")
            if ws_end:  # WebSocket：耗时=连接存活时长，而非 101 握手耗时
                duration_ms = int((ws_end - req.timestamp_start) * 1000)
        except Exception:
            pass
        if duration_ms <= 0 and isinstance(mock, dict):
            duration_ms = int(mock.get("delay_ms") or 0)  # Mock 本地响应的耗时=配置延时
        size = 0
        try:
            if resp is not None:
                cl = resp.headers.get("content-length") or ""
                if cl.isdigit():
                    size = int(cl)
                elif resp.content:
                    size = len(resp.content)
        except Exception:
            pass
        loss = False
        try:
            loss = bool(flow.metadata.get("cap_loss"))
        except Exception:
            pass
        return {
            "id": flow.id,
            "time": datetime.fromtimestamp(req.timestamp_start).strftime("%H:%M:%S"),
            "ts": req.timestamp_start,
            "method": req.method,
            "scheme": req.scheme,
            "host": req.pretty_host,
            "port": req.port,
            "path": req.path,
            "status": resp.status_code if resp else None,
            "content_type": (resp.headers.get("content-type", "") if resp else ""),
            "size": size,
            "duration_ms": duration_ms,
            "client": client_ip,
            "source": src,
            "req_headers": _headers_dict(req),
            "req_body": _body_text(flow, req, "cap_up_body", "cap_up_over"),
            "resp_headers": _headers_dict(resp) if resp else {},
            "resp_body": _body_text(flow, resp, "cap_down_body", "cap_down_over") if resp else None,
            "error": error,
            "mock": mock,
            "replay": replay,
            "loss": loss,
            "ws": _ws_payload(flow),
            "state": "done",
        }

    def _report_pending(self, flow, cfg):
        """在途流量上报：请求进入引擎即上报一条 state=pending 记录，让流量列表实时
        出现该笔请求（弱网延迟 / 限速 / 长响应等待期间列表不缺项）；响应 / 错误终报
        按 id 覆盖更新。

        跳过（避免重复行 / 幽灵行）：
        - 断点命中（请求或响应任一 filter 匹配）→ 以「⏸ 合成行」形式展示；
        - 来源开关未记录该来源 → 终报同样不会记录，避免永远停留「进行中」；
          重放流量例外（用户主动发起，始终记录）。
        """
        try:
            req = flow.request
            if not _source_allowed(flow):  # 重放已提前置位 cap_replay，自动豁免来源开关
                return
            bp = cfg.get("breakpoint") or {}
            if (bp.get("request") or bp.get("response")) \
                    and _break_filter_ok(bp.get("filter"), req.pretty_url, req.method):
                return
            flow.metadata["cap_pending"] = True  # 终报通行证：已上报 pending 的流量必须完成终报
            _enqueue(self._build_record(flow, pending=True))
        except Exception:
            pass

    def requestheaders(self, flow):
        """请求头阶段：在途上报 → 丢包判定 → 上行带宽限速武装（仅真实转发流量）

        丢包必须在请求头阶段完成：此时上游连接尚未建立，kill 后请求（含 body）
        不达服务器；若等到 request 钩子（流式 body 转发完成后）才 kill，body 已
        全部到达服务器，仅客户端被断连（实测确认的事件时序）。

        与断点 / Mock 的先后关系：
        - 请求断点命中（filter 匹配）→ 不判定不武装：断点编辑请求体优先；
        - 改包 Mock 命中 → 不判定不武装：请求不会真实转发，避免无谓等待。
        """
        try:
            req = flow.request
            if _should_ignore(req):
                return
            if req.headers.get("X-ApiTester-Replay"):
                flow.metadata["cap_replay"] = True  # 提前置位：让来源豁免在 pending 上报时即生效
            cfg = _eng_cfg()
            self._report_pending(flow, cfg)
            wn = cfg.get("weaknet") or {}
            if not wn.get("enabled"):
                return
            bp = cfg.get("breakpoint") or {}
            if bp.get("request") and _break_filter_ok(bp.get("filter"), req.pretty_url, req.method):
                return
            if _match_mock(req.method, req.pretty_host, req.path.split("?")[0]) is not None:
                return
            p = _weaknet_params(wn)
            # a) 丢包：按概率在请求头阶段掐断（请求不达服务器，客户端得到网络错误）
            #    killable 前置：不可掐断的流量继续正常转发，不得打「丢包」标记（避免误标）
            if p["loss_pct"] > 0 and random.random() * 100.0 < p["loss_pct"] and flow.killable:
                flow.metadata["cap_loss"] = p["loss_pct"]
                flow.kill()
                return
            # b) 上行带宽限速：仅对有 body 的请求武装（流式逐块减速；上行带宽 0=随下行）
            if p["bandwidth_up_kbps"] <= 0:
                return
            te = (req.headers.get("transfer-encoding") or "").lower()
            cl = (req.headers.get("content-length") or "0").strip()
            has_body = "chunked" in te or (cl.isdigit() and int(cl) > 0)
            if has_body:
                flow.request.stream = _make_throttle_cb(flow, "cap_up_body", "cap_up_over",
                                                        "bandwidth_up_kbps")
        except Exception:
            pass

    async def responseheaders(self, flow):
        """响应头阶段：武装下行带宽限速 + 间歇卡顿（仅真实转发流量）

        - 响应断点命中（filter 匹配）→ 不武装：响应编辑优先（流式转发会在
          断点挂起前把 body 发出，编辑失效）；
        - Mock / 丢弃的本地合成响应不会走到本钩子，保险再跳过。
        """
        try:
            if _should_ignore(flow.request):
                return
            if flow.response is None or flow.metadata.get("cap_mock") \
                    or flow.metadata.get("cap_break") == "drop":
                return
            cfg = _eng_cfg()
            wn = cfg.get("weaknet") or {}
            if not wn.get("enabled"):
                return
            p = _weaknet_params(wn)
            bp = cfg.get("breakpoint") or {}
            if bp.get("response") and _break_filter_ok(bp.get("filter"), flow.request.pretty_url, flow.request.method):
                return
            if p["bandwidth_kbps"] > 0:
                flow.response.stream = _make_throttle_cb(flow, "cap_down_body", "cap_down_over")
            stall = _stall_wait(p)
            if stall > 0:
                await asyncio.sleep(stall)
        except Exception:
            pass

    async def request(self, flow):
        """请求阶段：断点挂起 → 改包 Mock 命中 → 弱网（丢包/卡顿/延迟）"""
        try:
            if _should_ignore(flow.request):
                return
            # 接口重放：识别标记头 → 剔除（不污染真实请求）并跳过断点，避免长时间挂起阻塞重放
            if flow.request.headers.get("X-ApiTester-Replay"):
                flow.metadata["cap_replay"] = True
                try:
                    del flow.request.headers["X-ApiTester-Replay"]
                except Exception:
                    pass
            cfg = _eng_cfg()
            bp = cfg.get("breakpoint") or {}
            wn = cfg.get("weaknet") or {}
            # 1) 请求断点：挂起等前端编辑 / 丢弃（重放流量跳过）
            if bp.get("request") and not flow.metadata.get("cap_replay") \
                    and _break_filter_ok(bp.get("filter"), flow.request.pretty_url, flow.request.method):
                dec = await _wait_break("request", flow)
                if (dec.get("action") or "continue") == "drop":
                    flow.response = http.Response.make(502, _DROP_TEXT, dict(_DROP_HEADERS))
                    flow.metadata["cap_break"] = "drop"
                    return
                _apply_req_decision(flow, dec)
            # 2) 改包 Mock：命中「启用中」的规则直接本地响应（不发真实请求）
            #    拦截与否由每条规则自身的 enabled 决定（无全局总开关）
            path = flow.request.path.split("?")[0]
            m = _match_mock(flow.request.method, flow.request.pretty_host, path)
            if m is not None:
                status, msg, headers, body, delay, name = _mock_response_parts(m, _mock_cfg())
                if delay > 0:
                    await asyncio.sleep(min(delay, 60000) / 1000.0)
                resp = http.Response.make(status, body, headers)
                if msg:
                    resp.reason = str(msg)
                flow.response = resp
                flow.metadata["cap_mock"] = {
                    "id": m.get("id"), "name": name or (m.get("path") or ""), "delay_ms": delay,
                }
                return
            # 3) 高级弱网（仅真实转发流量）：丢包已在 requestheaders 阶段完成 → 卡顿 → 延迟
            if wn.get("enabled"):
                p = _weaknet_params(wn)
                # a) 间歇卡顿：等待当前卡顿周期剩余时长
                stall = _stall_wait(p)
                if stall > 0:
                    await asyncio.sleep(stall)
                # b) 基础延迟：每个请求发出前等待
                if p["latency_ms"] > 0:
                    await asyncio.sleep(min(p["latency_ms"], 120000) / 1000.0)
        except Exception:
            pass  # 任何异常都不阻断转发

    async def response(self, flow):
        """响应阶段：响应断点 → 记录上报（下行限速在 responseheaders 流式阶段）"""
        try:
            if _should_ignore(flow.request):
                return
            cfg = _eng_cfg()
            bp = cfg.get("breakpoint") or {}
            mock_hit = bool(flow.metadata.get("cap_mock"))
            dropped = flow.metadata.get("cap_break") == "drop"
            replay = bool(flow.metadata.get("cap_replay"))
            # 1) 响应断点：挂起等前端编辑 / 丢弃（Mock / 丢弃 / 重放的响应不再干预）
            if bp.get("response") and not mock_hit and not dropped and not replay \
                    and _break_filter_ok(bp.get("filter"), flow.request.pretty_url, flow.request.method):
                dec = await _wait_break("response", flow)
                if (dec.get("action") or "continue") == "drop":
                    flow.response = http.Response.make(502, _DROP_TEXT, dict(_DROP_HEADERS))
                    flow.metadata["cap_break"] = "drop"
                else:
                    _apply_resp_decision(flow, dec)
            # 2) 记录上报：已上报 pending 的流量必须完成终报（否则残留「进行中」幽灵行），
            #    其余按来源开关（关闭时仅不记录，转发不受影响）
            if not flow.metadata.get("cap_pending") and not _source_allowed(flow):
                return
            _enqueue(self._build_record(flow))
        except Exception:
            pass

    def error(self, flow):
        try:
            if _should_ignore(flow.request):
                return
            # 已上报 pending 的流量必须完成终报（与 response 钩子同规则）
            if not flow.metadata.get("cap_pending") and not _source_allowed(flow):
                return
            err = ""
            try:
                if flow.error:
                    err = getattr(flow.error, "msg", None) or str(flow.error)
            except Exception:
                pass
            _enqueue(self._build_record(flow, error=err or "未知错误"))
        except Exception:
            pass

    # ── CONNECT 阶段失败记录（客户端未装证书 / 目标不可达等流量也必须在列表可见）──
    # mitmproxy 对 CONNECT 失败不触发 request/response/error 钩子（直接回 502 给
    # 客户端），若无以下钩子这类流量会完全遗漏：
    # - http_connect：缓存连接 id -> flow 映射（仅缓存，不生成记录，正常流程由
    #   request/response 钩子记录，此处只服务失败场景的 flow 找回）；
    # - server_connect_error：上游 DNS/端口不可达（仅 CONNECT 缓存的 flow 命中，
    #   普通 HTTP 请求的失败由 error 钩子记录，天然不重复）；
    # - tls_failed_client：客户端 TLS 握手失败（典型：设备未信任代理证书 / App
    #   证书固定）——附修复提示。

    def http_connect(self, flow):
        try:
            if _should_ignore(flow.request):
                return None, None
            if len(self._pending_connects) > 500 or len(self._pending_clients) > 500:
                # 兜底防泄漏：大量挂起连接（极罕见）时清空旧映射
                self._pending_connects.clear()
                self._pending_clients.clear()
            try:
                self._pending_connects[flow.server_conn.id] = flow
            except Exception:
                pass
            try:
                self._pending_clients[flow.client_conn.id] = flow
            except Exception:
                pass
        except Exception:
            pass
        return None, None

    def server_connected(self, data):
        """上游连接建立成功：移除缓存（此后不会再触发 server_connect_error）。

        注意不能用 server_connect（它在连接尝试开始时就触发，成功与否未知），
        否则 server_connect_error 到来前 flow 已被弹出、失败记录丢失。
        """
        try:
            self._pending_connects.pop(data.server.id, None)
        except Exception:
            pass

    def server_connect_error(self, data):
        """上游连接失败（CONNECT 场景）：生成失败记录，避免列表完全空白"""
        try:
            flow = self._pending_connects.pop(data.server.id, None)
            if flow is None or flow.request is None:
                return
            if flow.request.method != "CONNECT":
                return
            if _should_ignore(flow.request):
                return
            if not _source_allowed(flow):
                return
            err = ""
            try:
                err = str(data.server.error or "").strip()
            except Exception:
                pass
            rec = self._build_record(flow, error=err or "上游连接失败")
            rec["status"] = None  # CONNECT 未成功：不允许残留合成状态码误导
            _enqueue(rec)
        except Exception:
            pass

    def tls_established_client(self, data):
        """客户端 TLS 握手成功：移除缓存（此后不会再触发 tls_failed_client）"""
        try:
            self._pending_clients.pop(data.conn.id, None)
        except Exception:
            pass

    def tls_failed_client(self, data):
        """客户端 TLS 握手失败：生成失败记录并附修复提示（列表可见才可诊断）"""
        try:
            flow = self._pending_clients.pop(data.conn.id, None)
            if flow is None or flow.request is None:
                return
            if _should_ignore(flow.request):
                return
            if not _source_allowed(flow):
                return
            # 上游 TLS 同时失败时优先提示上游问题（否则提示「未信任代理证书」会误导）
            srv_err = ""
            try:
                srv_err = str(getattr(flow.server_conn, "error", "") or "").strip()
            except Exception:
                pass
            if srv_err:
                msg = "上游 TLS 握手失败：" + srv_err
            else:
                msg = ("客户端 TLS 握手失败：客户端可能未信任代理证书，"
                       "请在设备上安装并信任工具箱 CA；已安装仍失败则可能是 App 证书固定（SSL Pinning）")
            rec = self._build_record(flow, error=msg)
            rec["status"] = None  # CONNECT 的 200 合成响应不代表真实业务状态
            _enqueue(rec)
        except Exception:
            pass

    # ── WebSocket：消息聚合记录（不单独建列表条目，随握手记录上报）──

    def websocket_start(self, flow):
        """WebSocket 握手完成：初始化消息聚合并上报一条在途记录

        来源开关关闭的连接不聚合（与 HTTP 记录同规则），转发本身不受影响。
        """
        try:
            if _should_ignore(flow.request):
                return
            if not _source_allowed(flow):
                return
            flow.metadata["cap_ws_msgs"] = []
            flow.metadata["cap_ws_trunc"] = False
            flow.metadata["cap_ws_last_report"] = 0.0
            flow.metadata["cap_pending"] = True  # 终报通行证：websocket_end 必须完成终报
            _enqueue(self._build_record(flow, pending=True))
        except Exception:
            pass

    def websocket_message(self, flow):
        """WebSocket 消息：聚合到 metadata（单连接上限 100 条），节流增量上报"""
        try:
            msgs = flow.metadata.get("cap_ws_msgs")
            if msgs is None:  # 未初始化（自身流量 / 来源关闭）：不记录
                return
            if len(msgs) >= _WS_MAX_MSGS:
                flow.metadata["cap_ws_trunc"] = True
                return
            m = flow.websocket.messages[-1]
            text, kind = _ws_msg_text(m.content)
            msgs.append({
                "dir": "c" if m.from_client else "s",
                "t": datetime.fromtimestamp(m.timestamp).strftime("%H:%M:%S"),
                "type": kind,
                "text": text,
            })
            now = time.monotonic()
            if now - float(flow.metadata.get("cap_ws_last_report") or 0.0) >= _WS_REPORT_EVERY:
                flow.metadata["cap_ws_last_report"] = now
                _enqueue(self._build_record(flow, pending=True))
        except Exception:
            pass

    def websocket_end(self, flow):
        """WebSocket 连接结束：记录存活时长并终报（携带完整消息聚合）"""
        try:
            if flow.metadata.get("cap_ws_msgs") is None:
                return
            flow.metadata["cap_ws_end"] = time.time()
            _enqueue(self._build_record(flow))
        except Exception:
            pass


addons = [CaptureAddon()]
