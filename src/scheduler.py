#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scheduler.py — 定时任务调度器（api-tester 内置模块）

支持 5 种触发方式：
  once          一次性：指定日期时间执行一次
  daily         每天固定时间：如 09:00
  random_window 每日随机区间：每天在 [start, end] 内随机时刻执行
  interval      间隔执行：每 N 分钟一次，可加 ± 随机抖动分钟
  burst         时间窗口内随机执行 N 次：如 60 分钟内随机执行 5 次

线程模型：单个守护线程每 5 秒扫描任务表，到期任务交给独立线程执行（防重入）。
持久化：
  data/_schedule_tasks.json              任务表
  data/_schedule_runs/<task_id>/<run>.json  每次执行的完整结果（每任务保留最近 50 次）
"""
import io
import json
import os
import random
import sys
import threading
import time
import contextlib
from datetime import datetime, timedelta

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.insert(0, _dir)

if getattr(sys, 'frozen', False):
    # 打包版：数据目录随 exe（sys.executable 所在目录），__file__ 指向包内虚拟路径不可用
    _app_root = os.path.dirname(os.path.abspath(sys.executable))
else:
    _app_root = os.path.dirname(_dir)
CASES_DIR = os.path.join(_app_root, "cases")
DATA_DIR = os.path.join(_app_root, "data")
TASKS_FILE = os.path.join(DATA_DIR, "_schedule_tasks.json")
RUNS_DIR = os.path.join(DATA_DIR, "_schedule_runs")
VAR_FILE = os.path.join(DATA_DIR, "_variables.json")
PARAMS_DIR = os.path.join(DATA_DIR, "_schedule_params")   # 参数化 Excel 持久目录

PARAM_STRATEGIES = ("all_rows", "round_robin", "random_row")  # 参数化行选择策略

FMT = "%Y-%m-%d %H:%M:%S"
MAX_RUNS_KEPT = 50       # 每任务保留最近执行次数
MAX_RESP_SAVED = 20000   # 保存的响应体最大字符数

_lock = threading.RLock()
_tasks = []              # 任务列表（内存态，_lock 保护）
_running = set()         # 正在执行的任务 id 集合
_worker_started = False
_tick_stop = False


# ═══════════════════════════════════════════════════
#  持久化
# ═══════════════════════════════════════════════════

def _now():
    return datetime.now()


def _load_tasks_from_disk():
    global _tasks
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _tasks = data if isinstance(data, list) else []
    except (IOError, json.JSONDecodeError):
        _tasks = []
    # 启动恢复：上次进程退出时残留的 running 状态重置为等待，重算下次执行
    now = _now()
    for t in _tasks:
        if t.get("state") == "running":
            t["state"] = "waiting"
        if t.get("enabled") and t.get("state") in ("waiting", None):
            _init_schedule(t, now)


def _save():
    """写盘（调用方需持有 _lock）"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = TASKS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_tasks, f, ensure_ascii=False, indent=2)
        os.replace(tmp, TASKS_FILE)
    except IOError as e:
        print(f"  [调度] 保存任务表失败: {e}")


def _find(tid):
    for t in _tasks:
        if t.get("id") == tid:
            return t
    return None


def _gen_id():
    import uuid
    return "sch_" + uuid.uuid4().hex[:8]


# ═══════════════════════════════════════════════════
#  时间工具
# ═══════════════════════════════════════════════════

def _parse_hhmm(s, default=(9, 0)):
    """解析 'HH:MM'，容错返回 (时, 分)"""
    try:
        parts = str(s).strip().split(":")
        h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        return max(0, min(23, h)), max(0, min(59, m))
    except (ValueError, IndexError):
        return default


def _parse_dt(s):
    for fmt in (FMT, "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(str(s).strip(), fmt)
        except ValueError:
            continue
    return None


def _fmt(dt):
    return dt.strftime(FMT) if dt else None


def _rand_between(start_dt, end_dt):
    """在 [start_dt, end_dt] 之间随机取一个时刻（闭区间秒级）"""
    span = int((end_dt - start_dt).total_seconds())
    if span <= 0:
        return start_dt
    return start_dt + timedelta(seconds=random.randint(0, span))


# ═══════════════════════════════════════════════════
#  调度计算
# ═══════════════════════════════════════════════════

def _init_schedule(task, now):
    """初始化/重算 next_run_at（以及 burst 的 pending_times）"""
    mode = task.get("mode", "daily")
    cfg = task.get("config") or {}

    if mode == "once":
        dt = _parse_dt(cfg.get("run_at"))
        task["next_run_at"] = _fmt(dt) if dt and dt > now else None
        if not task["next_run_at"]:
            task["state"] = "completed"

    elif mode == "daily":
        h, m = _parse_hhmm(cfg.get("time", "09:00"))
        cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        task["next_run_at"] = _fmt(cand)

    elif mode == "random_window":
        sh, sm = _parse_hhmm(cfg.get("start", "09:00"))
        eh, em = _parse_hhmm(cfg.get("end", "10:00"))
        start_dt = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end_dt = now.replace(hour=eh, minute=em, second=0, microsecond=0)
        if end_dt <= start_dt:
            end_dt = start_dt + timedelta(minutes=1)
        if now >= end_dt:
            # 今天窗口已结束 → 明天窗口
            start_dt += timedelta(days=1)
            end_dt += timedelta(days=1)
        elif now > start_dt:
            # 已在今天窗口内 → 从现在起延迟 5~30 秒后随机触发（避免立刻连发）
            start_dt = now + timedelta(seconds=5)
            if start_dt >= end_dt:
                end_dt = start_dt + timedelta(seconds=30)
        task["next_run_at"] = _fmt(_rand_between(start_dt, end_dt))

    elif mode == "interval":
        every = max(1, int(cfg.get("every", 30)))
        jitter = max(0, int(cfg.get("jitter", 0)))
        delta = every * 60 + (random.randint(-jitter * 60, jitter * 60) if jitter else 0)
        delta = max(30, delta)  # 至少 30 秒
        task["next_run_at"] = _fmt(now + timedelta(seconds=delta))

    elif mode == "burst":
        window_min = max(1, int(cfg.get("window", 60)))
        times = max(1, int(cfg.get("times", 5)))
        span = window_min * 60
        k = min(times, max(1, span - 10))
        offsets = sorted(random.sample(range(10, max(11, span)), k))
        now_ts = now
        task["pending_times"] = [_fmt(now_ts + timedelta(seconds=o)) for o in offsets]
        task["next_run_at"] = task["pending_times"][0]

    else:
        task["next_run_at"] = None

    if task.get("next_run_at"):
        task["state"] = "waiting"


def _advance_after_run(task, now):
    """一次执行完成后，推进到下个执行点"""
    mode = task.get("mode")
    cfg = task.get("config") or {}

    if mode == "once":
        task["state"] = "completed"
        task["next_run_at"] = None

    elif mode == "daily":
        h, m = _parse_hhmm(cfg.get("time", "09:00"))
        cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        task["next_run_at"] = _fmt(cand)

    elif mode == "random_window":
        sh, sm = _parse_hhmm(cfg.get("start", "09:00"))
        eh, em = _parse_hhmm(cfg.get("end", "10:00"))
        start_dt = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end_dt = now.replace(hour=eh, minute=em, second=0, microsecond=0)
        if end_dt <= start_dt:
            end_dt = start_dt + timedelta(minutes=1)
        if now >= end_dt:
            start_dt += timedelta(days=1)
            end_dt += timedelta(days=1)
        else:
            # 窗口内执行完了，下一个执行点放到明天
            start_dt += timedelta(days=1)
            end_dt += timedelta(days=1)
        task["next_run_at"] = _fmt(_rand_between(start_dt, end_dt))

    elif mode == "interval":
        _init_schedule(task, now)

    elif mode == "burst":
        pending = task.get("pending_times") or []
        pending = [p for p in pending if p != task.get("_executing_at")]
        # 移除刚刚执行的时刻（按时间顺序取第一个已过期的）
        task["pending_times"] = pending
        if pending:
            task["next_run_at"] = pending[0]
            task["state"] = "waiting"
        else:
            task["next_run_at"] = None
            task["state"] = "completed"


# ═══════════════════════════════════════════════════
#  执行引擎（复用 engine.py，与 app.py 的 run 逻辑一致）
# ═══════════════════════════════════════════════════

def _safe_case_path(filename):
    if not filename:
        return None
    filepath = os.path.realpath(os.path.join(CASES_DIR, filename))
    if not filepath.startswith(os.path.realpath(CASES_DIR) + os.sep):
        return None
    return filepath


def _load_variables_values():
    """加载预设变量名→值（与 app.py 的 _load_variables_values 一致）

    自增变量（带 seq 元数据）不返回：它们由 seqvars.assign 在每次请求前取号。
    """
    try:
        with open(VAR_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (IOError, json.JSONDecodeError):
        return {}
    result = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            if isinstance(v.get("seq"), dict):
                continue
            result[k] = v.get("value", "")
        else:
            result[k] = v
    return result


# ═══════════════════════════════════════════════════
#  参数化（Excel 数据驱动）
# ═══════════════════════════════════════════════════

def _safe_params_path(filename):
    """校验参数化 Excel 位于 PARAMS_DIR 内，返回绝对路径或 None"""
    if not filename:
        return None
    filepath = os.path.realpath(os.path.join(PARAMS_DIR, filename))
    if not filepath.startswith(os.path.realpath(PARAMS_DIR) + os.sep):
        return None
    return filepath


def _normalize_params(raw):
    """校验并规范化任务参数化配置，返回 (params, error)

    结构: {file, mappings: {Excel列: YAML变量}, types: {Excel列: 类型},
           strategy, row_count, cursor}
    """
    if not raw:
        return None, ""
    if not isinstance(raw, dict):
        return None, "参数化配置格式错误"
    fname = str(raw.get("file", "")).strip()
    if not fname:
        return None, "参数化 Excel 文件名缺失"
    path = _safe_params_path(fname)
    if not path or not os.path.exists(path):
        return None, "参数化 Excel 不存在，请重新上传"
    mappings = raw.get("mappings") or {}
    if not isinstance(mappings, dict):
        return None, "参数映射格式错误"
    clean_map = {}
    for col, var in mappings.items():
        col = str(col).strip()
        var = str(var).strip()
        if col and var:
            clean_map[col] = var
    if not clean_map:
        return None, "请至少映射一个参数列"
    types_raw = raw.get("types") or {}
    clean_types = {}
    if isinstance(types_raw, dict):
        for k, v in types_raw.items():
            k = str(k).strip()
            if k:
                clean_types[k] = str(v)
    strategy = str(raw.get("strategy", "all_rows")).strip()
    if strategy not in PARAM_STRATEGIES:
        strategy = "all_rows"
    try:
        row_count = int(raw.get("row_count") or 0)
    except (TypeError, ValueError):
        row_count = 0
    return {
        "file": fname,
        "mappings": clean_map,
        "types": clean_types,
        "strategy": strategy,
        "row_count": max(0, row_count),
        "cursor": 0,
    }, ""


def _load_param_rows(task):
    """按任务参数化配置读取 Excel 并选择本次执行的行集合

    返回 (row_sets, meta):
      row_sets — [(行标签, {变量名: 值}), ...]
      meta     — {file, strategy, rows: [行号], total_rows}
    """
    import engine as E
    params = dict(task.get("params") or {})
    fname = params.get("file", "")
    path = _safe_params_path(fname)
    if not path or not os.path.exists(path):
        raise FileNotFoundError("参数化 Excel 不存在: " + str(fname))
    headers, rows = E.parse_excel_rows(path)
    if not rows:
        raise ValueError("参数化 Excel 中没有数据行")

    mappings = params.get("mappings") or {}
    types = params.get("types") or {}
    col_index = {}
    for col in mappings:
        try:
            col_index[col] = headers.index(col)
        except ValueError:
            continue

    def _row_vars(row):
        out = {}
        for col, var in mappings.items():
            ci = col_index.get(col)
            if ci is None or ci >= len(row):
                continue
            out[var] = E.convert_param_value(row[ci], types.get(col, "string"))
        return out

    strategy = params.get("strategy", "all_rows")
    meta = {"file": fname, "strategy": strategy, "total_rows": len(rows)}
    if strategy == "random_row":
        pick = random.randrange(len(rows))
        meta["rows"] = [pick + 1]
        return [("第%d行" % (pick + 1), _row_vars(rows[pick]))], meta
    if strategy == "round_robin":
        with _lock:
            t = _find(task.get("id"))
            cursor = 0
            if t is not None:
                cursor = int((t.get("params") or {}).get("cursor", 0))
                t.setdefault("params", {})["cursor"] = (cursor + 1) % len(rows)
                _save()
        pick = cursor % len(rows)
        meta["rows"] = [pick + 1]
        return [("第%d行" % (pick + 1), _row_vars(rows[pick]))], meta
    # all_rows：每次触发跑完全部行
    row_sets = [("第%d行" % (i + 1), _row_vars(r)) for i, r in enumerate(rows)]
    meta["rows"] = list(range(1, len(rows) + 1))
    return row_sets, meta


def _trim_record(record):
    """裁剪保存的记录体量（响应过长时截断）"""
    resp = record.get("response") or ""
    if len(resp) > MAX_RESP_SAVED:
        record["response"] = resp[:MAX_RESP_SAVED] + "\n…（已截断）"
    return record


def execute_file(filename, step_filter="", loops=1, row_sets=None):
    """执行一个用例文件，返回 {records, all_pass, pass, fail, total, setup_vars, setup_errors}

    与 app.py /api/run-stream 的核心逻辑一致（无流式输出）。
    row_sets: 参数化行集合 [(行标签, {变量名: 值}), ...]；提供时每行独立变量上下文
              完整执行一次序列（保留同一行内跨步骤提取的变量传递），
              记录的 param_row 字段标记所属行。
    """
    import engine as E

    filepath = _safe_case_path(filename)
    if not filepath or not os.path.exists(filepath):
        raise FileNotFoundError(f"文件不存在: {filename}")

    with open(filepath, "r", encoding="utf-8") as f:
        cfg = json.load(f) or {}

    try:
        loops = min(max(1, int(loops)), 100)
    except (TypeError, ValueError):
        loops = 1

    preset_vars = _load_variables_values()
    setup_cfg = cfg.get("setup")
    setup_vars, setup_errors = E.eval_setup(setup_cfg)
    base_vars = dict(preset_vars)
    base_vars.update(setup_vars)
    variables = dict(base_vars)  # 当前行的工作变量（extract/步骤 setup 写入；行切换时重置）

    steps = cfg.get("steps", [])
    records = []

    def _exec_one(idx, step_name, req_cfg, assert_cfg, extract, extract_list, step_mock, step_setup_cfg, loop_num=None):
        if step_setup_cfg and isinstance(step_setup_cfg, dict):
            step_setup_vars, _ = E.eval_setup(step_setup_cfg)
            variables.update(step_setup_vars)

        _resolved_cfg = None
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = E.do_request(req_cfg, variables, mock=step_mock)

            # 入参快照在 do_request 之后取：自增变量在请求前才取号
            _resolved_cfg = E._replace_deep(req_cfg, variables or {})
            if result["error"]:
                E.P.print_error(result["error"])
                record = {
                    "step_index": idx, "name": step_name,
                    "method": req_cfg.get("method", "GET"),
                    "url": E._replace(req_cfg.get("url", ""), variables),
                    "status_code": "ERR", "elapsed_ms": result["elapsed_ms"],
                    "passed": False, "asserts": [],
                    "response": result["error"], "extracted": [],
                    "is_mock": False,
                    "req_body": _resolved_cfg.get("body"),
                    "req_params": _resolved_cfg.get("params"),
                    "req_headers": _resolved_cfg.get("headers") or {},
                }
            else:
                ok, msgs = E.run_assert(
                    assert_cfg, result["status_code"],
                    result["resp_text"], result["resp_json"],
                    elapsed_ms=result["elapsed_ms"],
                    resp_headers=result.get("resp_headers"))
                if result["resp_json"] is not None:
                    resp_preview = json.dumps(result["resp_json"], ensure_ascii=False, indent=2, default=str)
                else:
                    resp_preview = result["resp_text"] or ""
                extracted = []
                if result["resp_json"] is not None:
                    extracted = E.do_extract(extract, extract_list, result["resp_json"], variables)
                record = {
                    "step_index": idx, "name": step_name,
                    "method": req_cfg.get("method", "GET"),
                    "url": E._replace(req_cfg.get("url", ""), variables),
                    "status_code": result["status_code"],
                    "elapsed_ms": result["elapsed_ms"],
                    "passed": ok,
                    "asserts": [__import__("re").sub(r'\x1b\[[0-9;]*m', '', m) for m in msgs],
                    "response": resp_preview,
                    "extracted": extracted,
                    "is_mock": result.get("is_mock", False),
                    "seq_used": result.get("seq_used") or {},
                    "req_body": _resolved_cfg.get("body"),
                    "req_params": _resolved_cfg.get("params"),
                    "req_headers": _resolved_cfg.get("headers") or {},
                }
        if loop_num is not None:
            record["loop"] = loop_num
        record["console"] = buf.getvalue()
        # 未解析变量（YAML 引用顺序错误/变量名拼错时，URL 或请求体中会残留 ${xxx}）
        record["unresolved"] = E.find_unresolved_refs({
            "url": record.get("url", ""),
            "body": record.get("req_body"),
            "params": record.get("req_params"),
            "headers": record.get("req_headers"),
        })
        return record

    def _exec_sequence():
        # ── 单接口模式（无 steps）──
        if not steps and "request" in cfg:
            record = _exec_one(
                1, cfg.get("name", "接口测试"),
                cfg.get("request", {}), cfg.get("assert", {}),
                cfg.get("extract", {}), cfg.get("extract_list", {}),
                cfg.get("mock"), None)
            records.append(_trim_record(record))
        elif steps:
            filter_nums = None
            if step_filter:
                try:
                    filter_nums = [int(s.strip()) for s in str(step_filter).split(",") if s.strip()]
                except ValueError:
                    filter_nums = None
            if filter_nums:
                ordered = [(n, steps[n - 1]) for n in filter_nums if 1 <= n <= len(steps)]
            else:
                ordered = list(enumerate(steps, 1))
            for loop_num in range(1, loops + 1):
                for idx, step in ordered:
                    step_name = step.get("name", f"Step{idx}")
                    req_cfg = step.get("request", {})

                    # ── foreach 数据驱动解析（用上游提取的数组逐条驱动本步骤）──
                    fe_items, fe_item_var, fe_note, fe_error = E.parse_foreach(step.get("foreach"), variables)
                    if fe_error:
                        records.append(_trim_record({
                            "step_index": idx, "name": step_name,
                            "method": req_cfg.get("method", "GET"),
                            "url": E._replace(req_cfg.get("url", ""), variables),
                            "status_code": "ERR", "elapsed_ms": 0,
                            "passed": False, "asserts": [],
                            "response": f"foreach 配置异常：{fe_error}", "extracted": [],
                            "is_mock": False,
                            "req_body": "", "req_params": None, "req_headers": {},
                            "console": "",
                        }))
                        continue
                    fe_iter = list(enumerate(fe_items, 1)) if fe_items is not None else [(None, None)]
                    fe_scope = E.save_foreach_scope(fe_item_var, variables) if fe_items is not None else None
                    for fe_i, fe_val in fe_iter:
                        if fe_items is not None:
                            E._inject_foreach(fe_item_var, fe_val, variables)
                        record = _exec_one(
                            idx, step_name,
                            req_cfg, step.get("assert", {}),
                            step.get("extract", {}), step.get("extract_list", {}),
                            step.get("mock"), step.get("setup"),
                            loop_num if loops > 1 else None)
                        if fe_items is not None:
                            record["name"] = f"{step_name}[数据{fe_i}/{len(fe_items)}]"
                            record["foreach_index"] = fe_i
                            record["foreach_total"] = len(fe_items)
                            record["foreach_item"] = E._foreach_disp(fe_item_var, fe_val)[:200]
                        records.append(_trim_record(record))
                    # 恢复 foreach 注入变量，避免污染后续步骤（如覆盖参数化行同名变量）
                    if fe_scope is not None:
                        E.restore_foreach_scope(fe_scope, variables)
        else:
            raise ValueError("没有 steps 配置，也没有 request 配置")

    # 参数化行循环：无 row_sets 时整文件执行一次
    if row_sets:
        for row_label, row_vars in row_sets:
            variables.clear()
            variables.update(base_vars)
            if row_vars:
                variables.update(row_vars)
            before = len(records)
            _exec_sequence()
            for r in records[before:]:
                r["param_row"] = row_label
    else:
        _exec_sequence()

    total = len(records)
    passed = sum(1 for r in records if r["passed"])
    result = {
        "records": records,
        "all_pass": passed == total and total > 0,
        "pass": passed,
        "fail": total - passed,
        "total": total,
        "setup_vars": {k: str(v) for k, v in setup_vars.items()},
        "setup_errors": setup_errors,
    }
    if row_sets:
        result["param_rows"] = [label for label, _ in row_sets]
    return result


# ═══════════════════════════════════════════════════
#  任务执行与结果保存
# ═══════════════════════════════════════════════════

def _runs_dir(task_id):
    d = os.path.join(RUNS_DIR, task_id)
    os.makedirs(d, exist_ok=True)
    return d


def _save_run_result(task_id, result, started_at, finished_at, trigger, param_meta=None):
    run_id = finished_at.strftime("%Y%m%d_%H%M%S") + "_" + ("s" if trigger == "schedule" else "m")
    payload = {
        "run_id": run_id,
        "task_id": task_id,
        "trigger": trigger,               # schedule=定时触发  manual=立即执行
        "started_at": _fmt(started_at),
        "finished_at": _fmt(finished_at),
        "all_pass": result["all_pass"],
        "pass": result["pass"],
        "fail": result["fail"],
        "total": result["total"],
        "setup_vars": result.get("setup_vars", {}),
        "setup_errors": result.get("setup_errors", []),
        "records": result["records"],
    }
    if param_meta:
        payload["param"] = param_meta
    if result.get("error"):
        payload["error"] = result["error"]
    d = _runs_dir(task_id)
    with open(os.path.join(d, run_id + ".json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    # 清理旧结果（保留最近 MAX_RUNS_KEPT 个）
    try:
        files = sorted([x for x in os.listdir(d) if x.endswith(".json")])
        for old in files[:-MAX_RUNS_KEPT]:
            os.remove(os.path.join(d, old))
    except OSError:
        pass
    return run_id, payload


def _execute_task(tid, trigger="schedule"):
    """执行任务并记录结果（在独立线程中调用）"""
    with _lock:
        task = _find(tid)
        if not task:
            return
        file = task.get("file", "")
        steps = task.get("steps", "")
        loops = task.get("loops", 1)
        params_cfg = dict(task.get("params") or {}) or None
        executing_at = task.get("next_run_at") if trigger == "schedule" else None
        task["_executing_at"] = executing_at

    started_at = _now()
    param_meta = None
    try:
        row_sets = None
        if params_cfg:
            row_sets, param_meta = _load_param_rows({"id": tid, "params": params_cfg})
        result = execute_file(file, steps, loops, row_sets=row_sets)
    except Exception as e:
        result = {"records": [], "all_pass": False, "pass": 0, "fail": 0, "total": 0,
                  "setup_vars": {}, "setup_errors": [], "error": str(e)}
    finished_at = _now()

    with _lock:
        task = _find(tid)
        if task:
            run_id, payload = _save_run_result(tid, result, started_at, finished_at, trigger, param_meta)
            task["last_result"] = {
                "run_id": run_id,
                "time": payload["finished_at"],
                "all_pass": result["all_pass"],
                "pass": result["pass"],
                "fail": result["fail"],
                "total": result["total"],
                "error": result.get("error", ""),
            }
            task["run_count"] = int(task.get("run_count", 0)) + 1
            if trigger == "schedule":
                task["last_run_at"] = executing_at
                _advance_after_run(task, finished_at)
            task.pop("_executing_at", None)
            _save()
    print(f"  [调度] 任务 {tid} 执行完成: pass={result['pass']}/{result['total']}")


def _start_task_run(tid, trigger="schedule"):
    """启动一次任务执行（异步线程，防重入）"""
    with _lock:
        task = _find(tid)
        if not task or tid in _running:
            return False
        _running.add(tid)
        if trigger == "schedule":
            task["state"] = "running"
        _save()

    def _runner():
        try:
            _execute_task(tid, trigger)
        finally:
            with _lock:
                _running.discard(tid)

    threading.Thread(target=_runner, daemon=True).start()
    return True


# ═══════════════════════════════════════════════════
#  调度线程
# ═══════════════════════════════════════════════════

def _tick():
    now = _now()
    due_tids = []
    with _lock:
        for t in _tasks:
            if not t.get("enabled"):
                continue
            if t.get("state") in ("running", "completed", "paused"):
                continue
            if t.get("id") in _running:
                continue
            nxt = t.get("next_run_at")
            if not nxt:
                # 兜底补算（如创建时未初始化）
                _init_schedule(t, now)
                continue
            dt = _parse_dt(nxt)
            if dt is None:
                _init_schedule(t, now)
                continue
            if dt <= now:
                due_tids.append(t.get("id"))
    for tid in due_tids:
        _start_task_run(tid, trigger="schedule")


def _worker():
    # 1 秒粒度：任务到点后最多 1 秒即进入执行状态，列表刷新能及时看到「执行中」
    while not _tick_stop:
        try:
            _tick()
        except Exception as e:
            print(f"  [调度] tick 异常: {e}")
        time.sleep(1)


def start_worker():
    """启动调度守护线程（重复调用安全）"""
    global _worker_started
    with _lock:
        if _worker_started:
            return
        _load_tasks_from_disk()
        _worker_started = True
    threading.Thread(target=_worker, daemon=True).start()
    n = len([t for t in _tasks if t.get("enabled")])
    print(f"  [调度] 调度线程已启动，共 {len(_tasks)} 个任务（启用 {n} 个）")


# ═══════════════════════════════════════════════════
#  对外 API（供 app.py 路由调用）
# ═══════════════════════════════════════════════════

def _sanitize_task(t):
    """去掉内部字段后返回副本"""
    t2 = {k: v for k, v in t.items() if not k.startswith("_")}
    return t2


def list_tasks():
    with _lock:
        now = _now()
        out = []
        for t in _tasks:
            # 惰性修正：启用的 waiting 任务若 next 已过期超过 2 分钟（如休眠/时钟漂移），重算
            if t.get("enabled") and t.get("state") == "waiting":
                dt = _parse_dt(t.get("next_run_at"))
                if dt and (now - dt).total_seconds() > 120 and t.get("mode") in ("daily", "random_window"):
                    _init_schedule(t, now)
                    _save()
            t2 = _sanitize_task(t)
            # 手动「立即执行」期间也对外显示执行中（与定时触发的 running 展示一致）
            if t.get("id") in _running:
                t2["state"] = "running"
            out.append(t2)
        return {"tasks": out}


def create_task(payload):
    file = str(payload.get("file", "")).strip()
    if not file:
        return False, "请选择 YAML 文件"
    if not _safe_case_path(file) or not os.path.exists(_safe_case_path(file)):
        return False, "文件不存在"
    mode = str(payload.get("mode", "")).strip()
    if mode not in ("once", "daily", "random_window", "interval", "burst"):
        return False, "调度方式无效"
    try:
        loops = int(payload.get("loops", 1))
    except (TypeError, ValueError):
        loops = 1
    cfg = payload.get("config") or {}

    # 各模式参数校验
    if mode == "once":
        dt = _parse_dt(cfg.get("run_at"))
        if not dt:
            return False, "请填写执行时间"
        if dt <= _now():
            return False, "执行时间必须晚于当前时间"
    elif mode == "daily":
        if "time" not in cfg:
            return False, "请填写每天执行时间"
    elif mode == "random_window":
        if "start" not in cfg or "end" not in cfg:
            return False, "请填写随机区间起止时间"
    elif mode == "interval":
        try:
            if int(cfg.get("every", 0)) < 1:
                return False, "间隔分钟数需 ≥ 1"
        except (TypeError, ValueError):
            return False, "间隔分钟数无效"
    elif mode == "burst":
        try:
            if int(cfg.get("window", 0)) < 1:
                return False, "时间窗口（分钟）需 ≥ 1"
            if int(cfg.get("times", 0)) < 1:
                return False, "执行次数需 ≥ 1"
        except (TypeError, ValueError):
            return False, "窗口/次数参数无效"

    # 参数化配置（可选）：校验 Excel 存在与映射有效性
    params, perr = _normalize_params(payload.get("params"))
    if perr:
        return False, perr

    task = {
        "id": _gen_id(),
        "file": file,
        "steps": str(payload.get("steps", "")).strip(),
        "loops": min(max(1, loops), 100),
        "mode": mode,
        "config": cfg,
        "enabled": True,
        "state": "waiting",
        "created_at": _fmt(_now()),
        "next_run_at": None,
        "pending_times": [],
        "last_result": None,
        "run_count": 0,
        "params": params,
    }
    with _lock:
        _init_schedule(task, _now())
        if task.get("state") == "completed" and mode == "once":
            return False, "执行时间已过，任务未创建"
        _tasks.append(task)
        _save()
        return True, _sanitize_task(task)


def update_task(tid, patch):
    with _lock:
        t = _find(tid)
        if not t:
            return False, "任务不存在"
        if "enabled" in patch:
            enabled = bool(patch["enabled"])
            if enabled and not t.get("enabled"):
                # 重新启用：仅对仍有计划的任务重算调度；
                # 已完成（once 已跑完 / burst 次数用完）任务恢复时不复活，保持已完成
                if t.get("state") != "completed":
                    t["state"] = "waiting"
                    t.pop("pending_times", None)
                    _init_schedule(t, _now())
                t["enabled"] = enabled
            elif not enabled and t.get("enabled"):
                # 暂停：已完成是终态，忽略暂停请求（若覆盖 completed 状态，之后恢复时会绕过复活保护）
                if t.get("state") != "completed":
                    t["state"] = "paused"
                    t["enabled"] = enabled
        if "file" in patch:
            t["file"] = str(patch["file"]).strip()
        if "steps" in patch:
            t["steps"] = str(patch["steps"]).strip()
        if "loops" in patch:
            try:
                t["loops"] = min(max(1, int(patch["loops"])), 100)
            except (TypeError, ValueError):
                pass
        if "params" in patch:
            # 传 null / {} 表示清除参数化配置
            if patch["params"]:
                p, perr = _normalize_params(patch["params"])
                if perr:
                    return False, perr
                t["params"] = p
            else:
                t["params"] = None
        _save()
        return True, _sanitize_task(t)


def delete_task(tid):
    with _lock:
        t = _find(tid)
        if not t:
            return False, "任务不存在"
        if t.get("id") in _running:
            return False, "任务正在执行中，请稍后再删除"
        _tasks.remove(t)
        _save()
    # 删除执行历史
    try:
        import shutil
        d = os.path.join(RUNS_DIR, tid)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass
    return True, ""


def run_now(tid):
    """立即执行一次（不影响既定调度计划）"""
    with _lock:
        t = _find(tid)
        if not t or t.get("id") in _running:
            return False, "任务不存在或正在执行中"
    _start_task_run(tid, trigger="manual")
    return True, ""


def list_runs(tid):
    d = os.path.join(RUNS_DIR, tid)
    if not os.path.isdir(d):
        return []
    out = []
    try:
        for fname in sorted(os.listdir(d), reverse=True):
            if not fname.endswith(".json"):
                continue
            fp = os.path.join(d, fname)
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                out.append({
                    "run_id": data.get("run_id", fname[:-5]),
                    "trigger": data.get("trigger", "schedule"),
                    "started_at": data.get("started_at", ""),
                    "finished_at": data.get("finished_at", ""),
                    "all_pass": data.get("all_pass", False),
                    "pass": data.get("pass", 0),
                    "fail": data.get("fail", 0),
                    "total": data.get("total", 0),
                    "param": data.get("param"),
                    "error": data.get("error", ""),
                })
            except (IOError, json.JSONDecodeError):
                continue
    except OSError:
        pass
    return out


def get_run_detail(tid, run_id):
    # run_id 防路径遍历
    if not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
        return None
    fp = os.path.join(RUNS_DIR, tid, run_id + ".json")
    if not os.path.exists(fp):
        return None
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError):
        return None
