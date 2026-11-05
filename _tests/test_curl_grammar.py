# -*- coding: utf-8 -*-
"""curl 命令行的“格式”测试：只验证写法，不依赖任何选项语义

curl 的选项语义永远补不完（每版都在加），但命令行的写法是有限的：
  引号 4 种、续行 3 种、长选项 3 种取值写法、短选项 3 种 + 连写、
  `--` 终止符、位置参数、未知选项的取值证据。
这里把这些写法穷举一遍——写法对了，新增选项也不会把 URL / header 吃掉。

运行：cd .qoder/skills/api-tester/_tests ; pytest -q test_curl_grammar.py
"""
import pytest

import curl2yaml as C

URL = "https://a.com/x"
ARG_SHORTS = sorted(C._SHORT_ARG)
NOARG_SHORTS = sorted(C._SHORT_NOARG)


def q(v):
    return "'" + str(v).replace("'", "'\\''") + "'"


def parse(cmd):
    return C.parse_curl_report(cmd)


def unknown_warns(r):
    return [w for w in r["warnings"] if "无法识别" in w]


# ══════════════════════════════════════════════════════
#  1. 长选项的三种取值写法
# ══════════════════════════════════════════════════════

@pytest.mark.parametrize("frag", [
    "--header 'x-probe: v1'",
    '--header="x-probe: v1"',
    "--header='x-probe: v1'",
    "-H 'x-probe: v1'",
    "-H'x-probe: v1'",
])
def test_header_write_forms_are_equivalent(frag):
    r = parse(f"curl {frag} {q(URL)}")
    assert r["url"] == URL
    assert r["headers"] == {"x-probe": "v1"}, r["headers"]


@pytest.mark.parametrize("frag", [
    "-X POST", "-XPOST", "--request POST", "--request=POST",
])
def test_method_write_forms_are_equivalent(frag):
    r = parse(f"curl {frag} {q(URL)}")
    assert r["method"] == "POST" and r["url"] == URL


def test_body_write_forms_are_equivalent():
    forms = ["-d", "--data", "--data-raw", "--data-ascii", "--data-binary"]
    expect = None
    for opt in forms:
        r = parse(f"curl {opt} '{{\"a\":1}}' {q(URL)}")
        assert r["url"] == URL, opt
        assert r["body"] == {"a": 1}, opt
        expect = r["body"]
    assert expect == {"a": 1}


def test_empty_equals_value_never_eats_url():
    r = parse(f"curl --totally-unknown= {q(URL)} -H 'app-id: 1'")
    assert r["url"] == URL and r["headers"] == {"app-id": "1"}
    assert unknown_warns(r)


# ══════════════════════════════════════════════════════
#  2. 短选项连写（curl 规则：遇到带值的，后面剩下就是值）
# ══════════════════════════════════════════════════════

@pytest.mark.parametrize("frag,method", [
    ("-sS -X POST", "POST"),
    ("-sS -XPOST", "POST"),
    ("-sSXPOST", "POST"),
    ("-svX PATCH", "PATCH"),
    ("-ksSXPUT", "PUT"),
])
def test_short_cluster_with_value(frag, method):
    r = parse(f"curl {frag} {q(URL)}")
    assert r["url"] == URL, frag
    assert r["method"] == method, (frag, r["method"])


def test_short_cluster_no_value():
    r = parse(f"curl -sSLk {q(URL)} -H 'app-id: 1'")
    assert r["url"] == URL and r["headers"] == {"app-id": "1"}
    assert not unknown_warns(r)


def test_short_cluster_last_takes_value_from_next_token():
    # -sSH 后面单独跟一个 header 值
    r = parse(f"curl -sSH 'app-id: 1' {q(URL)}")
    assert r["url"] == URL and r["headers"] == {"app-id": "1"}


# ══════════════════════════════════════════════════════
#  3. 短选项签名穷举：有值 / 无值都不能丢 URL 和 header
# ══════════════════════════════════════════════════════

@pytest.mark.parametrize("ch", ARG_SHORTS)
def test_every_arg_short_keeps_url_and_header(ch):
    # 只断言格式：不给它丢 URL、也不让它吞掉后面的 -H。
    # 语义短选项（-A/-e/-u 等）会额外产生自己的 header，所以用 get 而非全等。
    # 分开写：-x VAL URL
    r = parse(f"curl -{ch} 'VAL' {q(URL)} -H 'app-id: 1'")
    assert r["url"] == URL, f"-{ch} 吃掉了 URL"
    assert r["headers"].get("app-id") == "1", f"-{ch} 吃掉了 -H"
    # 紧凑写：-xVAL URL
    r2 = parse(f"curl -{ch}VAL {q(URL)} -H 'app-id: 1'")
    assert r2["url"] == URL, f"-{ch}VAL 吃掉了 URL"
    assert r2["headers"].get("app-id") == "1", f"-{ch}VAL 吃掉了 -H"


@pytest.mark.parametrize("ch", NOARG_SHORTS)
def test_every_noarg_short_keeps_url_and_header(ch):
    r = parse(f"curl -{ch} {q(URL)} -H 'app-id: 1'")
    assert r["url"] == URL, f"-{ch} 吃掉了 URL"
    assert r["headers"] == {"app-id": "1"}, f"-{ch} 吃掉了 -H"
    assert not unknown_warns(r), f"-{ch} 未被识别：{r['warnings']}"


# ══════════════════════════════════════════════════════
#  4. -- 终止符与位置参数
# ══════════════════════════════════════════════════════

def test_terminator_makes_rest_positional():
    r = parse(f"curl -H 'app-id: 1' -- {q(URL)}")
    assert r["url"] == URL and r["headers"] == {"app-id": "1"}
    assert r["warnings"] == []


def test_terminator_allows_dash_url():
    # -- 之后以 - 开头也算地址，不会被当成选项
    r = parse("curl -- '-https://a.com/x'")
    assert r["url"] == "-https://a.com/x", r["url"]


def test_single_dash_is_stdio_not_url():
    r = parse(f"curl - {q(URL)} -H 'app-id: 1'")
    assert r["url"] == URL, r["url"]
    assert r["headers"] == {"app-id": "1"}


def test_multiple_positional_urls_takes_first_and_warns():
    r = parse(f"curl {q(URL)} {q('https://a.com/y')}")
    assert r["url"] == URL
    assert any("多个请求地址" in w for w in r["warnings"]), r["warnings"]


def test_url_after_terminator_not_overriding_url_option():
    r = parse(f"curl --url {q('https://a.com/1')} -- {q('https://a.com/2')}")
    assert r["url"] == "https://a.com/1"
    assert any("多个请求地址" in w for w in r["warnings"])


# ══════════════════════════════════════════════════════
#  5. 未知选项：只能靠“取值证据”判断，不能抢 URL
# ══════════════════════════════════════════════════════

@pytest.mark.parametrize("frag", [
    # 值不像 URL / 不像选项 → 是它的取值
    "--upstream foo",
    "--upstream 12345",
    # 唯一一个 URL：不能被未知选项吞掉
    f"--upstream {URL}",
    # 两个 URL：前一个是取值，后一个才是地址
    f"--upstream https://b.com/y {URL}",
    # 值是 header 形态 → 仍然当取值（漏吞比多吞更危险）
    "--upstream 'a: b'",
    # 值为空串
    "--upstream ''",
])
def test_unknown_option_value_evidence(frag):
    r = parse(f"curl {frag} {q(URL)} -H 'app-id: 1'")
    assert r["url"] == URL, f"{frag} → {r['url']!r}"
    assert r["headers"].get("app-id") == "1", f"{frag} 吞掉了 -H"
    assert unknown_warns(r), f"{frag} 没有警告"


def test_unknown_option_never_eats_known_option():
    r = parse(f"curl --upstream -H 'app-id: 1' {q(URL)}")
    assert r["url"] == URL and r["headers"] == {"app-id": "1"}


# ══════════════════════════════════════════════════════
#  6. 空白与续行（引号外）
# ══════════════════════════════════════════════════════

@pytest.mark.parametrize("sep", [" ", "  ", "\t", " \t ", "\n", " \\\n  ", " ^\n  ", " `\n  "])
def test_whitespace_and_continuation_between_args(sep):
    cmd = f"curl{sep}-H 'app-id: 1'{sep}{q(URL)}"
    r = parse(cmd)
    assert r["url"] == URL, (sep, r["url"])
    assert r["headers"] == {"app-id": "1"}, (sep, r["headers"])


@pytest.mark.parametrize("cmd", [
    f"  curl   -H 'app-id: 1'   {URL}  ",
    f"\ncurl\n-H 'app-id: 1'\n{URL}\n",
])
def test_leading_trailing_whitespace(cmd):
    r = parse(cmd)
    assert r["url"] == URL and r["headers"] == {"app-id": "1"}


# ======================================================
#  7. bash ANSI-C 引用 $'...'（Postman / 浏览器复制多行 body 常见）
# ══════════════════════════════════════════════════════

@pytest.mark.parametrize("frag,expect", [
    ("$'{\"a\": 1}'", {"a": 1}),
    ("$'{\\n  \"a\": 1,\\n  \"b\": 2\\n}'", {"a": 1, "b": 2}),
])
def test_ansi_c_quoting_body(frag, expect):
    r = parse(f"curl -d {frag} {q(URL)}")
    assert r["url"] == URL, r["url"]
    assert r["body"] == expect, r["body"]


def test_ansi_c_quoting_header_and_simple_text():
    # $'X-A: b' 这种 header 写法也要识别；纯文本 body 不走 JSON 解析
    r = parse(f"curl -H $'app-id: 1' -d $'hello\\tworld' {q(URL)}")
    assert r["url"] == URL
    assert r["headers"].get("app-id") == "1"
    assert r["body"] == "hello\tworld", repr(r["body"])


def test_ansi_c_quoting_hex_escape():
    r = parse(f"curl -d $'\\x41\\x42' {q(URL)}")
    assert r["body"] == "AB", repr(r["body"])


# ======================================================
#  8. 多个 -d 拼接：不能拼出 &&
# ══════════════════════════════════════════════════════

@pytest.mark.parametrize("frags,expect", [
    (["-d a=1", "-d b=2"], "a=1&b=2"),
    (["-d a=1", "-d '&b=2'"], "a=1&b=2"),
    (["-d 'a=1&'", "-d 'b=2'"], "a=1&b=2"),
])
def test_multiple_data_never_double_ampersand(frags, expect):
    r = parse(f"curl {frags[0]} {frags[1]} {q(URL)}")
    assert r["url"] == URL
    assert r["body"] == expect, repr(r["body"])
