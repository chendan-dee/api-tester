#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
yaml_checker.py — 用例文档结构校验工具

用法：
  1. 改下面 JSON_FILE 变量，直接运行
  2. 命令行：python yaml_checker.py cases/test.json
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

# ═══════════════════════════════════════════════════════════════
#  在这里配置要校验的文件
# ═══════════════════════════════════════════════════════════════
JSON_FILE = "cases/test.json"
# ═══════════════════════════════════════════════════════════════

# requests 支持任意方法；这里只挡住拼写错误的常见白名单，
# 并额外收录 curl -X 可能带出的扩展方法（WebDAV 等），避免转换后校验误报
VALID_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS",
                 "TRACE", "CONNECT", "PROPFIND", "REPORT", "MKCOL",
                 "COPY", "MOVE", "LOCK", "UNLOCK", "PURGE"}


def load_yaml(path):
    """加载用例文档（JSON），语法错误直接报错"""
    import json as _json

    if not os.path.exists(path):
        print(f"文件不存在: {path}")
        return None

    with open(path, "r", encoding="utf-8") as f:
        try:
            data = _json.load(f)
        except _json.JSONDecodeError as e:
            print(f"[ERROR] JSON 语法错误:\n")
            print(f"   位置: 第 {e.lineno} 行, 第 {e.colno} 列")
            print(f"   原因: {e.msg}")
            print()
            _suggest_syntax_fix(str(e))
            return None

    if data is None:
        print("[WARN] 文件为空，没有任何配置内容")
        return None

    return data


def _suggest_syntax_fix(err):
    """根据 JSON 语法错误给出修复建议"""
    err_lower = err.lower()
    if "expecting ','" in err_lower or "expecting ':'" in err_lower:
        print("   [TIP] 修复建议: 检查 key: value 之间是否缺逗号或冒号")
    if "expecting property name" in err_lower:
        print("   [TIP] 修复建议: 检查是否多了逗号（尾逗号），JSON 不允许")
    if "control character" in err_lower:
        print("   [TIP] 修复建议: 字符串里有未转义的控制字符，请检查换行/引号")
    print("      示例:  {\"name\": \"步骤名称\", \"steps\": [...]}")


def check_config(data, path=""):
    """递归校验配置结构，返回错误列表 [(位置, 错误信息, 修复建议)]"""
    errors = []

    if not isinstance(data, dict):
        errors.append((path or "根节点", f"应该是字典(dict)，实际是 {type(data).__name__}",
                        "顶层应该是 YAML 字典，包含 name、steps 等字段"))
        return errors

    # ── 校验 steps ──
    if "steps" in data:
        steps = data["steps"]
        steps_path = f"{path}steps" if path else "steps"

        if not isinstance(steps, list):
            errors.append((steps_path,
                            f"steps 应该是列表(list)，实际是 {type(steps).__name__}",
                            "每个步骤前加 '- ' 使其成为列表项:\n"
                            "      steps:\n"
                            "        - name: 步骤1\n"
                            "          request:\n"
                            "            url: https://..."))
        else:
            # 检测重复 step 名称（警告级别）
            seen_names = {}
            for i, step in enumerate(steps, 1):
                if isinstance(step, dict):
                    name = step.get("name", "")
                    if name:
                        seen_names.setdefault(name, []).append(i)
            for name, indices in seen_names.items():
                if len(indices) > 1:
                    step_list = ", ".join(f"第{i}步" for i in indices)
                    errors.append((f"steps",
                                   f"[WARN] 重复名称 '{name}': {step_list}",
                                   "建议给每个步骤起不同的名字，避免提取变量时混淆"))

            for i, step in enumerate(steps, 1):
                step_path = f"{steps_path}[{i}]"
                if not isinstance(step, dict):
                    errors.append((step_path,
                                    f"步骤应该是字典(dict)，实际是 {type(step).__name__}",
                                    f"第 {i} 个步骤格式错误，检查缩进和 '- ' 前缀"))
                    continue
                errors.extend(_check_step(step, step_path))

    # ── 校验单接口模式 ──
    if "request" in data:
        errors.extend(_check_request(data["request"], f"{path}request" if path else "request"))

    if "assert" in data:
        errors.extend(_check_assert(data["assert"], f"{path}assert" if path else "assert"))

    # ── 校验 setup 前置条件 ──
    if "setup" in data:
        setup = data["setup"]
        setup_path = f"{path}setup" if path else "setup"
        if not isinstance(setup, dict):
            errors.append((setup_path,
                            f"setup 应该是字典(dict)，实际是 {type(setup).__name__}",
                            "setup 格式:\n"
                            "      setup:\n"
                            '        timestamp: "datetime.now().strftime(\'%Y-%m-%d %H:%M:%S\')"'))
        else:
            for var_name, expr in setup.items():
                if not isinstance(expr, (str, int, float)):
                    errors.append((f"{setup_path}.{var_name}",
                                    f"setup 变量值应该是字符串(Python 表达式)，实际是 {type(expr).__name__}",
                                    f"用引号包裹 Python 表达式，如: {var_name}: \"random.randint(1, 100)\""))

    # ── 校验 loop/interval 类型 ──
    for key in ("loop", "interval"):
        if key in data:
            val = data[key]
            if not isinstance(val, (int, float)):
                errors.append((f"{path}{key}",
                                f"{key} 应该是数字，实际是 {type(val).__name__}: {val}",
                                f"把 {key} 的值改为数字，如 {key}: 3"))

    return errors


def _check_step(step, path):
    """校验单个步骤"""
    errors = []

    if "name" not in step:
        errors.append((path, "缺少 name 字段", f"添加 name: 步骤名称"))

    if "request" not in step and "mock" not in step:
        errors.append((path, "缺少 request 字段（或 mock）", f"添加 request 配置块"))

    if "request" in step:
        errors.extend(_check_request(step["request"], f"{path}.request"))

    if "assert" in step:
        errors.extend(_check_assert(step["assert"], f"{path}.assert"))

    if "extract" in step:
        ext = step["extract"]
        if not isinstance(ext, dict):
            errors.append((f"{path}.extract",
                            f"extract 应该是字典(dict)，实际是 {type(ext).__name__}",
                            "extract 格式:\n"
                            "      extract:\n"
                            "        变量名: \"json.path\""))

    if "extract_list" in step:
        ext = step["extract_list"]
        if not isinstance(ext, dict):
            errors.append((f"{path}.extract_list",
                            f"extract_list 应该是字典(dict)，实际是 {type(ext).__name__}",
                            "extract_list 格式同 extract"))

    return errors


def _check_request(req, path):
    """校验 request 配置"""
    errors = []

    if not isinstance(req, dict):
        errors.append((path, f"request 应该是字典(dict)，实际是 {type(req).__name__}",
                        "request 下应该包含 url、method 等字段"))
        return errors

    if "url" not in req:
        errors.append((path, "缺少 url 字段", "添加 url: https://你的接口地址"))
    elif not isinstance(req["url"], str):
        errors.append((f"{path}.url",
                        f"url 应该是字符串，实际是 {type(req['url']).__name__}",
                        "url 值要用引号包裹"))
    elif not req["url"].startswith("http"):
        errors.append((f"{path}.url",
                        f"url 不以 http 开头: {req['url'][:50]}",
                        "url 必须是完整的 http:// 或 https:// 地址"))

    method = req.get("method", "GET")
    if not isinstance(method, str) or method.upper() not in VALID_METHODS:
        errors.append((f"{path}.method",
                        f"无效的 HTTP 方法: {method}",
                        f"可选值: {', '.join(sorted(VALID_METHODS))}"))

    if "headers" in req and not isinstance(req["headers"], dict):
        errors.append((f"{path}.headers",
                        f"headers 应该是字典(dict)，实际是 {type(req['headers']).__name__}",
                        "headers 格式:\n"
                        "      headers:\n"
                        "        Content-Type: application/json"))

    if "body" in req:
        body = req["body"]
        if not isinstance(body, (dict, list, str)):
            errors.append((f"{path}.body",
                            f"body 应该是字典/列表/字符串，实际是 {type(body).__name__}",
                            "POST 请求的 body 通常是字典格式"))

    if "params" in req and not isinstance(req["params"], dict):
        errors.append((f"{path}.params",
                        f"params 应该是字典(dict)，实际是 {type(req['params']).__name__}",
                        "params 格式:\n"
                        "      params:\n"
                        "        key: value"))

    if "timeout" in req:
        t = req["timeout"]
        if not isinstance(t, (int, float)):
            errors.append((f"{path}.timeout",
                            f"timeout 应该是数字，实际是 {type(t).__name__}: {t}",
                            "timeout 值改为数字（秒），如 timeout: 15"))

    return errors


def _check_assert(assert_cfg, path):
    """校验 assert 配置（断言键清单与 engine.py run_assert 保持一致）"""
    errors = []

    if not isinstance(assert_cfg, dict):
        errors.append((path, f"assert 应该是字典(dict)，实际是 {type(assert_cfg).__name__}",
                        "assert 格式:\n"
                        "    assert:\n"
                        "      status_code: 200"))
        return errors

    # engine.py 实际支持的全部断言键
    SUPPORTED_ASSERT_KEYS = {
        "status_code", "body_contains", "body_not_contains",
        "json_path", "json_path_exists", "response_time", "header_contains",
    }

    if "status_code" in assert_cfg:
        sc = assert_cfg["status_code"]
        if not isinstance(sc, (int, str)) or not str(sc).isdigit():
            errors.append((f"{path}.status_code",
                            f"status_code 应该是整数，实际是: {sc}",
                            "改为整数，如 status_code: 200"))

    if "body_contains" in assert_cfg:
        bc = assert_cfg["body_contains"]
        if not isinstance(bc, str):
            errors.append((f"{path}.body_contains",
                            f"body_contains 应该是字符串，实际是 {type(bc).__name__}",
                            '改为字符串，如 body_contains: "success"'))

    if "body_not_contains" in assert_cfg:
        bnc = assert_cfg["body_not_contains"]
        if not isinstance(bnc, str):
            errors.append((f"{path}.body_not_contains",
                            f"body_not_contains 应该是字符串，实际是 {type(bnc).__name__}",
                            '改为字符串，如 body_not_contains: "error"'))

    if "json_path" in assert_cfg:
        jp = assert_cfg["json_path"]
        if not isinstance(jp, dict):
            errors.append((f"{path}.json_path",
                            f"json_path 应该是字典(dict)，实际是 {type(jp).__name__}",
                            "json_path 格式:\n"
                            "      json_path:\n"
                            "        data.id: \"123\""))

    if "json_path_exists" in assert_cfg:
        jpe = assert_cfg["json_path_exists"]
        if not (isinstance(jpe, str) or (isinstance(jpe, list) and all(isinstance(p, str) for p in jpe))):
            errors.append((f"{path}.json_path_exists",
                            f"json_path_exists 应该是字符串或字符串列表，实际是 {type(jpe).__name__}",
                            "json_path_exists 格式:\n"
                            "      json_path_exists:\n"
                            "      - data.token\n"
                            "      - data.list[0]"))

    if "response_time" in assert_cfg:
        rt = assert_cfg["response_time"]
        if not isinstance(rt, (int, float)) or isinstance(rt, bool) or rt < 0:
            errors.append((f"{path}.response_time",
                            f"response_time 应该是非负数字(毫秒)，实际是: {rt}",
                            "改为毫秒数，如 response_time: 1000"))

    if "header_contains" in assert_cfg:
        hc = assert_cfg["header_contains"]
        if not isinstance(hc, dict):
            errors.append((f"{path}.header_contains",
                            f"header_contains 应该是字典(dict)，实际是 {type(hc).__name__}",
                            "header_contains 格式:\n"
                            "      header_contains:\n"
                            "        Content-Type: json"))

    # 未知断言键：WARN 提示（engine 会直接忽略，写了也不会生效）
    for key in assert_cfg.keys():
        if key not in SUPPORTED_ASSERT_KEYS:
            errors.append((f"{path}.{key}",
                            f"[WARN] 未知断言键 '{key}'，engine 不会执行该断言",
                            f"支持的断言键: {', '.join(sorted(SUPPORTED_ASSERT_KEYS))}"))

    return errors


def run_check(doc_file):
    """执行完整校验"""
    print(f"  校验文件: {doc_file}\n")

    data = load_yaml(doc_file)
    if data is None:
        return False

    errors = check_config(data)

    if not errors:
        print("  [OK] 格式正确，结构校验通过！\n")

        # 显示概要信息
        name = data.get("name", "(未命名)")
        print(f"  名称: {name}")
        if "setup" in data and isinstance(data["setup"], dict):
            print(f"  前置变量: {len(data['setup'])} 个")
            for var_name, expr in data["setup"].items():
                print(f"    ${{{var_name}}} ← {str(expr)[:60]}")
        if "steps" in data and isinstance(data["steps"], list):
            print(f"  步骤数: {len(data['steps'])}")
            for i, step in enumerate(data["steps"], 1):
                if isinstance(step, dict):
                    sn = step.get("name", f"Step{i}")
                    req = step.get("request", {})
                    method = req.get("method", "GET") if isinstance(req, dict) else "?"
                    url = req.get("url", "?") if isinstance(req, dict) else "?"
                    print(f"    {i}. {sn}  {method}  {url[:60]}")
        elif "request" in data:
            req = data["request"]
            if isinstance(req, dict):
                print(f"  模式: 单接口")
                print(f"  方法: {req.get('method', 'GET')}")
                print(f"  地址: {req.get('url', '?')[:60]}")
        print()
        return True

    print(f"  [ERROR] 发现 {len(errors)} 个问题:\n")
    for i, (location, problem, fix) in enumerate(errors, 1):
        print(f"  [{i}] 位置: {location}")
        print(f"      问题: {problem}")
        print(f"      [TIP] {fix}")
        print()

    return False


if __name__ == "__main__":
    doc_file = sys.argv[1] if len(sys.argv) > 1 else JSON_FILE
    ok = run_check(doc_file)
    sys.exit(0 if ok else 1)
