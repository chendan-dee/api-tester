#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""printer.py — 统一输出模块"""

import sys

# Windows 终端默认 GBK 编码，emoji 会崩溃，强制 UTF-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass  # reconfigure 不可用时降级

import json

try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init(autoreset=True)
    _G = Fore.GREEN
    _R = Fore.RED
    _Y = Fore.YELLOW
    _0 = Style.RESET_ALL
except ImportError:
    _G = _R = _Y = _0 = ""

SEP = "-" * 50


def ok(text):   return f"{_G}[OK] {text}{_0}"
def fail(text): return f"{_R}[FAIL] {text}{_0}"
def warn(text): return f"{_Y}[WARN] {text}{_0}"


def sep(title=""):
    print(f"\n{SEP}")
    if title:
        print(f"  {title}")
        print(SEP)


def print_start(name, method, url, loop, interval, mode="single"):
    sep(name)
    if mode == "single" and loop > 1:
        print(f"  循环 {loop} 次  间隔 {interval}s")
    elif mode != "single":
        print(f"  共 {loop} 个步骤")
    print()


def print_request_line(index, total, method, url, is_mock=False):
    prefix = f"[{index}/{total}] " if total > 1 else ""
    mock_tag = f"{_Y}[MOCK]{_0} " if is_mock else ""
    print(f"  {prefix}{mock_tag}{method.upper()}  {url}")


def print_result(status_code, elapsed_ms, assert_msgs, resp_text,
                 show_preview=True, resp_json=None):
    color = _G if (status_code and 200 <= status_code < 300) else _R
    print(f"  状态: {color}{status_code}{_0}   耗时: {elapsed_ms}ms")
    if assert_msgs:
        print(f"  断言: " + "  |  ".join(assert_msgs))
    if show_preview and resp_json is not None:
        formatted = json.dumps(resp_json, ensure_ascii=False, indent=2)
        if len(formatted) > 500:
            formatted = formatted[:500].rstrip() + "\n    …"
        indented = "\n".join("    " + line for line in formatted.split("\n"))
        print(f"  响应:\n{indented}")
    elif show_preview and resp_text:
        preview = resp_text[:150].replace("\n", " ")
        suffix = "…" if len(resp_text) > 150 else ""
        print(f"  响应: {preview}{suffix}")
    print()


def print_extract(var_name, value, is_list=False):
    if is_list:
        print(f"  → ${{{var_name}}} = [{len(value)} 项]")
    else:
        print(f"  → ${{{var_name}}} = {str(value)[:80]}")


def print_extract_fail(var_name, path):
    print(f"  {warn(f'提取失败 ${{{var_name}}}，路径: {path}')}")


def print_error(msg):
    print(f"  {fail(msg)}\n")


def print_step(index, total, name, method, url, loop=1, interval=0):
    print(f"  Step {index}/{total}  {name}")
    print(f"  {method.upper()}  {url}")
    if loop > 1:
        print(f"  循环 {loop} 次  间隔 {interval}s")


def print_step_skipped(index, total, name):
    print(f"  Step {index}/{total}  {name}  {warn('跳过')}\n")


def print_summary_single(total, passed, elapsed_list, variables=None):
    failed = total - passed
    sep()
    if failed == 0:
        print(f"  {ok(f'全部通过 ({total})')}")
    else:
        print(f"  {fail(f'失败 {failed}/{total}')}")
    if variables:
        for k, v in variables.items():
            print(f"  {k} = {str(v)[:60]}")
    print()


def print_summary_steps(records, variables):
    passed = sum(1 for r in records if r.get("断言结果") == "PASS")
    failed = sum(1 for r in records if r.get("断言结果") == "FAIL")
    sep()
    if failed == 0:
        print(f"  {ok(f'全部通过 ({passed})')}")
    else:
        print(f"  {fail(f'失败 {failed}/{len(records)}')}")
    if variables:
        for k, v in variables.items():
            print(f"  {k} = {str(v)[:60]}")
    print()


def print_saved(filepath):
    print(f"  已保存: {filepath}\n")


def print_setup_var(var_name, value, expr):
    """打印前置条件计算成功的变量"""
    val_str = str(value)
    if len(val_str) > 60:
        val_str = val_str[:60] + "…"
    print(f"  {_G}[+]{_0} ${{{var_name}}} = {val_str}  {_Y}<- {expr}{_0}")


def print_setup_error(var_name, expr, error):
    """打印前置条件计算失败的变量"""
    print(f"  {_R}[x]{_0} ${{{var_name}}} 计算失败: {error}")
    print(f"    表达式: {expr}")
