# -*- coding: utf-8 -*-
"""内置动态变量（${__random_mobile} 等）：生成粒度、类型、链式展开

运行：cd .qoder/skills/api-tester/tests ; pytest -q
"""
import engine


def _get(url):
    return {"url": url, "method": "GET"}


def test_random_mobile_generated_and_written_back():
    variables = {}
    used = engine._apply_dynamic_vars(_get("https://a.com/u?m=${__random_mobile}"), variables)
    assert set(used) == {"__random_mobile"}
    mobile = used["__random_mobile"]
    assert mobile.isdigit() and len(mobile) == 11 and mobile.startswith("1")
    # 原地写回：同一请求里多处引用取同一个值
    assert variables["__random_mobile"] == mobile


def test_timestamp_keeps_number_type():
    variables = {}
    used = engine._apply_dynamic_vars(
        {"url": "https://a.com", "method": "POST", "body": {"t": "${__timestamp}"}}, variables)
    assert isinstance(used["__timestamp"], int)
    # 整串引用按原始类型注入，不是字符串
    assert engine._replace("${__timestamp}", variables) == used["__timestamp"]


def test_unreferenced_dynamic_not_generated():
    variables = {}
    assert engine._apply_dynamic_vars(_get("https://a.com/u"), variables) == {}
    assert "__random_mobile" not in variables


def test_unknown_placeholder_left_alone():
    variables = {}
    assert engine._apply_dynamic_vars(_get("https://a.com/u?x=${__nope}"), variables) == {}
    assert engine._replace("${__nope}", variables) == "${__nope}"


def test_chain_alias_expanded_to_same_value():
    """列里写 ${__random_mobile}、别名列写 ${mobile}：链式也要收敛到同一个值"""
    variables = {"mobile": "${__random_mobile}", "phone": "${mobile}"}
    engine._apply_dynamic_vars(_get("https://a.com/u?m=${phone}"), variables)
    assert variables["mobile"] == variables["__random_mobile"]
    assert variables["phone"] == variables["mobile"]


def test_cycle_refs_terminate():
    variables = {"a": "${b}", "b": "${a}"}
    engine._apply_dynamic_vars(_get("https://a.com/u?x=${a}"), variables)   # 不能卡死


def test_gen_mobile_matches_dynamic_rule():
    for _ in range(20):
        m = engine.gen_mobile()
        assert len(m) == 11 and m.isdigit() and m.startswith("1") and m[1] in "3456789"
