#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
runner.py — 运行接口测试

两种用法：
  1. 改下面两行变量，直接运行
  2. 命令行：python runner.py test.json 2
"""
import os
import sys

# Windows 终端默认 GBK 编码，emoji 会崩溃，强制 UTF-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import engine as E

# ═══════════════════════════════════════════════════════════════
#  在这里配置你要跑的文件和步骤
# ═══════════════════════════════════════════════════════════════
CASE_FILE = "cases/test.json"  # ← 改成你的用例文件路径
STEP      = "1"               # ← 留空跑全部，填数字跑指定步骤，如 2 或 1,3
# ═══════════════════════════════════════════════════════════════


def load_yaml(path):
    """加载用例文档（JSON）"""
    import json as _json
    if not os.path.exists(path):
        print(f"文件不存在: {path}"); sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        cfg = _json.load(f)
    print(f"  配置文件: {path}\n")
    return cfg


def run(case_file, step=""):
    """运行测试：加载用例文档 → 按步骤过滤 → 执行"""
    cfg = load_yaml(case_file)

    # 过滤步骤（坏参数友好提示，不抛 traceback）
    if "steps" in cfg and step:
        try:
            step_filter = [int(s.strip()) for s in step.split(",") if s.strip()]
        except ValueError:
            print(f"步骤参数无效: {step!r}，应为数字或逗号分隔的数字，如 2 或 1,3")
            return False
        if not step_filter:
            print(f"步骤参数无效: {step!r}，应为数字或逗号分隔的数字，如 2 或 1,3")
            return False
        filtered = [s for i, s in enumerate(cfg["steps"], 1) if i in step_filter]
        if not filtered:
            print(f"步骤编号无效，共 {len(cfg['steps'])} 步"); return False
        cfg = dict(cfg, steps=filtered)

    return E.run_steps(cfg, "reports") if "steps" in cfg else E.run_single(cfg, "reports")


if __name__ == "__main__":
    # 命令行参数解析（兼容 PowerShell 空字符串被吞的问题）
    # 用法:
    #   python runner.py                        # 用默认配置（默认跑第1步）
    #   python runner.py test.json              # 跑指定文件全部步骤
    #   python runner.py test.json 2            # 跑指定文件的第2步
    #   python runner.py test.json 1,3          # 跑指定文件的第1、3步
    #   python runner.py --all test.json        # 强制跑全部步骤
    args = sys.argv[1:]
    run_all = "--all" in args
    args = [a for a in args if a != "--all"]

    if len(args) == 0:
        # 无参数：使用默认配置
        case_file, step = CASE_FILE, STEP
    elif len(args) == 1:
        if args[0].isdigit() or ("," in args[0] and all(s.strip().isdigit() for s in args[0].split(","))):
            # 纯数字：作为步骤过滤，用默认文件
            case_file, step = CASE_FILE, args[0]
        else:
            # 文件名：默认跑全部步骤
            case_file, step = args[0], ""
    else:
        case_file, step = args[0], args[1]

    # --all 强制跑全部
    if run_all:
        step = ""

    success = run(case_file, step)
    sys.exit(0 if success else 1)
