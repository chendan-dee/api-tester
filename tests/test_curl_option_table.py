# -*- coding: utf-8 -*-
"""curl 选项全覆盖测试：选项表里的每一项都要有明确归宿，表外的真实选项一个都不能漏

两类断言：
  A. 回归 —— curl2yaml 选项表 _ARG_OPTS / _NO_ARG_OPTS 里的每一项，都必须被正确定向：
     取值不吞 URL、不污染报文、不产生“无法识别”警告；
  B. 防漏 —— curl 手册 / 浏览器 / Postman / 抓包工具会出现在复制结果里的选项，
     必须已登记进选项表：有报言语义的必须映射出来，下发 / 连接类的必须进 ignore 表。
     新增选项忘了登记，本文件立刻红。

运行：cd .qoder/skills/api-tester/tests ; pytest -q test_curl_option_table.py
"""
import base64

import pytest

import curl2yaml as C
import yaml_checker

URL = "https://a.com/x"
# 这几个选项本来就会改写 URL，不能拿原 URL 做通用断言
URL_CHANGING = {"url", "url_query", "request_target"}
ARG_OPTS = sorted(C._ARG_OPTS)
NO_ARG_OPTS = sorted(C._NO_ARG_OPTS)
LONG_ARGS = sorted(k for k in C._ARG_OPTS if k.startswith("--"))
SHORT_ARGS = sorted(k for k in C._ARG_OPTS if len(k) == 2)


def q(v):
    """bash 单引号"""
    return "'" + str(v).replace("'", "'\\''") + "'"


def parse(cmd):
    return C.parse_curl_report(cmd)


def unknown_warns(r):
    """被当成未知选项而丢弃的选项名"""
    return [w for w in r["warnings"] if "无法识别" in w]


def all_warn_text(r):
    return "\n".join(r["warnings"])


# ══════════════════════════════════════════════════════
#  A1. 每个“带值选项”都必须被正确消费
# ══════════════════════════════════════════════════════

# kind → (样例值, 校验函数)
def _check_value(r, kind, val):
    if kind == "header":
        assert r["headers"].get("x-probe") == "v1", r["headers"]
    elif kind.startswith("header:"):
        name = kind.split(":", 1)[1]
        assert r["headers"].get(name) == val, r["headers"]
    elif kind in ("data", "data-urlencode"):
        assert r["body"] in ({"a": 1}, "a=1", '{"a":1}'), r["body"]
        assert r["method"] == "POST"
    elif kind == "json":
        assert r["body"] == {"a": 1}, r["body"]
        assert r["headers"].get("content-type") == "application/json"
        assert r["headers"].get("accept") == "application/json"
    elif kind == "form":
        assert r["body"] == {"f": "v"}, r["body"]
        assert r["method"] == "POST"
    elif kind == "method":
        assert r["method"] == "PUT", r["method"]
    elif kind == "url":
        assert r["url"] == URL
    elif kind == "url_query":
        assert r["url"] == URL + "?a=1", r["url"]
        assert r["body"] is None, r["body"]
    elif kind == "request_target":
        assert r["url"] == "https://a.com/y", r["url"]
    elif kind == "cookie":
        assert r["headers"].get("cookie") == "c=1; d=2"
    elif kind == "auth":
        assert r["headers"].get("authorization") == \
            "Basic " + base64.b64encode(b"u:p").decode()
    elif kind == "oauth2_bearer":
        assert r["headers"].get("authorization") == "Bearer tok", r["headers"]
    elif kind == "upload":
        assert any("body" in w for w in r["warnings"]), r["warnings"]
        assert r["body"] is None, r["body"]
    elif kind == "warn":
        assert any("签名" in w for w in r["warnings"]), r["warnings"]
    elif kind == "timeout":
        assert r["timeout"] == 30.0, r["timeout"]
    elif kind == "warn_env":
        assert any("环境" in w for w in r["warnings"]), r["warnings"]
        assert r["body"] is None, r["body"]
    elif kind == "ignore":
        assert set(r["headers"]) == {"app-id"}, r["headers"]
        assert r["body"] is None, r["body"]
    else:
        pytest.fail(f"选项表出现未覆盖的新类型 {kind!r}，请补测试")


SAMPLE = {
    "header": "x-probe: v1",
    "header:user-agent": "UA/1.0",
    "header:referer": "https://r.com/p?q=1",
    "data": '{"a":1}',
    "data-urlencode": "a=1",
    "json": '{"a":1}',
    "form": "f=v",
    "method": "PUT",
    "url": URL,
    "url_query": "a=1",
    "request_target": "/y",
    "cookie": "c=1; d=2",
    "auth": "u:p",
    "oauth2_bearer": "tok",
    "upload": "f.txt",
    "warn": "aws:amz:cn-north-1:s3",
    "timeout": "30",
    "warn_env": "proxyhost:8080",
    "ignore": "some-value",
}


@pytest.mark.parametrize("opt", ARG_OPTS)
def test_each_value_option_is_consumed(opt):
    kind = C._ARG_OPTS[opt]
    val = SAMPLE[kind]
    if kind == "url":
        cmd = f"curl {opt} {q(val)} -H 'app-id: 1'"
    else:
        cmd = f"curl {opt} {q(val)} {q(URL)} -H 'app-id: 1'"
    r = parse(cmd)
    if kind not in URL_CHANGING:
        assert r["url"] == URL, f"{opt} 把 URL 弄丢/弄脏了：{r['url']!r}"
    assert not unknown_warns(r), f"{opt} 落进了未知选项：{r['warnings']}"
    _check_value(r, kind, val)


@pytest.mark.parametrize("opt", LONG_ARGS)
def test_each_long_option_equals_form(opt):
    """--opt=value 与 --opt value 必须完全等价"""
    kind = C._ARG_OPTS[opt]
    val = SAMPLE[kind]
    spaced = parse(f"curl {opt} {q(val)} {q(URL)} -H 'app-id: 1'")
    eq = parse(f"curl {opt}={q(val)} {q(URL)} -H 'app-id: 1'")
    assert eq["url"] == spaced["url"], opt
    assert eq["headers"] == spaced["headers"], opt
    assert eq["body"] == spaced["body"], opt
    assert eq["method"] == spaced["method"], opt


@pytest.mark.parametrize("opt", SHORT_ARGS)
def test_each_short_option_compact_form(opt):
    """-XPOST / -H'k: v' 这类紧凑写法必须与分开写等价"""
    kind = C._ARG_OPTS[opt]
    val = SAMPLE[kind]
    spaced = parse(f"curl {opt} {q(val)} {q(URL)} -H 'app-id: 1'")
    compact = parse(f"curl {opt}{q(val)} {q(URL)} -H 'app-id: 1'")
    assert compact["url"] == spaced["url"], f"{opt} 紧凑写法丢 URL"
    assert compact["headers"] == spaced["headers"], opt
    assert compact["body"] == spaced["body"], opt
    assert compact["method"] == spaced["method"], opt


@pytest.mark.parametrize("opt", ARG_OPTS)
def test_missing_value_never_eats_next_option(opt):
    """值被漏粘时，后面的 --url / -H 绝不能被吞掉"""
    r = parse(f"curl {opt} --url {q(URL)} -H 'app-id: 1'")
    assert r["url"] == URL, f"{opt} 缺值时把 --url 吃掉了"
    assert r["headers"].get("app-id") == "1", f"{opt} 缺值时把 -H 吃掉了：{r['headers']}"
    assert not any(k.lower().startswith("--") for k in r["headers"]), r["headers"]
    assert r["body"] in (None, ""), f"{opt} 缺值时把选项名当成了 body：{r['body']!r}"


# ══════════════════════════════════════════════════════
#  A2. 每个“无值选项”都不能干扰报文
# ══════════════════════════════════════════════════════

@pytest.mark.parametrize("opt", NO_ARG_OPTS)
def test_each_flag_option_is_harmless(opt):
    r = parse(f"curl {opt} {q(URL)} -H 'app-id: 1'")
    assert r["url"] == URL, f"{opt} 干扰了 URL"
    assert r["headers"] == {"app-id": "1"}, f"{opt} 污染了 headers：{r['headers']}"
    assert r["body"] is None, f"{opt} 造出了 body：{r['body']!r}"
    assert not unknown_warns(r), f"{opt} 落进了未知选项"
    if opt in ("-I", "--head"):
        assert r["method"] == "HEAD"
    elif opt in ("-G", "--get"):
        assert r["method"] == "GET"


@pytest.mark.parametrize("opt", sorted(C._WARN_NOARG_OPTS))
def test_auth_flags_raise_warning_not_silent(opt):
    """--ntlm/--digest/--negotiate 会改认证方式：必须警告，不能静默忽略"""
    r = parse(f"curl {opt} {q(URL)} -H 'app-id: 1'")
    assert r["url"] == URL, f"{opt} 干扰了 URL"
    assert r["headers"] == {"app-id": "1"}, f"{opt} 污染了 headers：{r['headers']}"
    assert r["body"] is None
    assert any("认证" in w for w in r["warnings"]), f"{opt} 没有给出认证警告：{r['warnings']}"


def test_max_time_maps_to_timeout():
    r = parse(f"curl -m 30 {q(URL)}")
    assert r["timeout"] == 30.0


def test_default_timeout_is_60_in_config():
    r = parse(f"curl {q(URL)}")
    cfg = C.build_config(r["url"], r["method"], r["headers"], r["body"], timeout=r["timeout"])
    assert cfg["request"]["timeout"] == 60
    assert "assert" not in cfg


def test_max_time_carried_into_config():
    r = parse(f"curl -m 90 {q(URL)}")
    cfg = C.build_config(r["url"], r["method"], r["headers"], r["body"], timeout=r["timeout"])
    assert cfg["request"]["timeout"] == 90.0


def test_merged_flag_cluster_with_value_option():
    """无值短选项连写后紧跟带值选项：-sSLk -H / -svXPOST"""
    r = parse(f"curl -sSLk {q(URL)} -H 'app-id: 1'")
    assert r["url"] == URL and r["headers"] == {"app-id": "1"}
    r = parse(f"curl -svXPOST {q(URL)} --data-raw '{{\"a\":1}}'")
    assert r["method"] == "POST" and r["body"] == {"a": 1}


# ══════════════════════════════════════════════════════
#  B1. 有报言语义的选项：必须映射出来（漏一个就是真机丢参）
# ══════════════════════════════════════════════════════

def test_url_query_appends_to_url():
    """--url-query 是 curl 7.87+ 的写 query 方式（新版 Postman/Chrome 会导出）"""
    r = parse(f"curl {q(URL)} --url-query 'page=2' --url-query 'size=10'")
    assert r["url"] == URL + "?page=2&size=10", r["url"]
    assert r["body"] is None


def test_url_query_merges_existing_query():
    r = parse("curl 'https://a.com/x?p=1' --url-query 'q=a b'")
    assert r["url"] == "https://a.com/x?p=1&q=a%20b", r["url"]


def test_url_query_file_reference_warns():
    r = parse(f"curl {q(URL)} --url-query '@q.txt'")
    assert any("url-query" in w for w in r["warnings"]), r["warnings"]


def test_url_query_plus_prefix_is_already_encoded():
    """--url-query 以 + 开头表示内容是已编码的 query 片段，不能被二次编码"""
    r = parse(f"curl {q(URL)} --url-query '+page%3D2' --url-query 'k=v'")
    assert r["url"] == URL + "?page%3D2&k=v", r["url"]


def test_curl_8x_aliases_match_old_flags():
    """--follow 是 --location 的别名，--show-headers 是 -i 的别名"""
    a = parse(f"curl --follow {q(URL)} -H 'app-id: 1'")
    b = parse(f"curl -L {q(URL)} -H 'app-id: 1'")
    c = parse(f"curl --show-headers {q(URL)} -H 'app-id: 1'")
    d = parse(f"curl -i {q(URL)} -H 'app-id: 1'")
    assert a == b and c == d
    assert not unknown_warns(a) and not unknown_warns(c)


def test_unregistered_option_is_loud_not_silent():
    """带语义但没法静态还原的选项：可以忽略，但必须留下警告，绝不允许静默丢参"""
    cases = [
        ("--netrc-file /tmp/n", "--netrc-file 'n.txt'"),
        ("--netrc", "--netrc"),
        ("--variable", "--variable 'v=1'"),
        ("--etag-compare", "--etag-compare 'e.txt'"),
        ("--time-cond", "--time-cond 'yesterday'"),
        ("--ignore-content-length", "--ignore-content-length"),
        ("--doh-insecure", "--doh-insecure"),
        ("--proxy-digest", "--proxy-digest"),
    ]
    for name, frag in cases:
        r = parse(f"curl {frag} {q(URL)} -H 'app-id: 1'")
        assert r["warnings"], f"{name} 被静默丢弃了"
        assert r["url"] == URL, f"{name} 吞掉了 URL：{r['url']!r}"
        assert r["headers"].get("app-id") == "1", f"{name} 吞掉了 -H：{r['headers']}"



def test_oauth2_bearer_sets_authorization():
    r = parse(f"curl {q(URL)} --oauth2-bearer 'tok123'")
    assert r["headers"].get("authorization") == "Bearer tok123", r["headers"]


def test_aws_sigv4_warns_because_local_signing_impossible():
    r = parse(f"curl {q(URL)} --aws-sigv4 'aws:amz:cn-north-1:s3' -u 'k:s'")
    assert any("aws-sigv4" in w.lower() for w in r["warnings"]), r["warnings"]
    assert r["headers"].get("authorization")  # -u 仍然生效


def test_request_target_overrides_path():
    r = parse(f"curl {q(URL)} --request-target '/y?z=1'")
    assert r["url"] == "https://a.com/y?z=1", r["url"]


def test_dump_header_is_output_not_request():
    r = parse(f"curl {q(URL)} -D 'h.txt' -H 'app-id: 1'")
    assert r["headers"] == {"app-id": "1"}
    assert not unknown_warns(r), r["warnings"]


def test_upload_file_warns_about_body():
    """-T 传的是 body，不能静默丢掉"""
    r = parse(f"curl {q(URL)} -T 'f.txt'")
    assert any("上传" in all_warn_text(r) or "文件" in w for w in r["warnings"]), r["warnings"]


def test_next_warns_about_second_request():
    """--next 表示后面还有第二个请求，只解析第一个时必须提示"""
    r = parse(f"curl {q(URL)} --next {q('https://a.com/x2')}")
    assert any("--next" in w or "多个请求" in w for w in r["warnings"]), r["warnings"]


def test_multiple_json_flags_make_one_document():
    r = parse(f'''curl {q(URL)} --json '{{"a":1}}' --json '{{"b":2}}' ''')
    # curl 把多个 --json 的数据直接拼接成同一个 body，至少不能拼成 & 串再退回字符串
    assert r["body"] == {"a": 1, "b": 2} or any("json" in w.lower() for w in r["warnings"]), r["body"]


def test_unknown_option_still_warns():
    r = parse(f"curl --totally-unknown-option v {q(URL)}")
    assert unknown_warns(r), r["warnings"]
    assert r["url"] == URL


# ══════════════════════════════════════════════════════
#  B2. 下发 / 连接类选项：必须在 ignore 表里（不许报“无法识别”）
# ══════════════════════════════════════════════════════

SILENT_OPTS = [
    # 输出去向 / 日志
    "-D", "--dump-header", "--trace-time", "--trace-config",
    # 连接 / 传输控制
    "--keepalive-time", "--no-keepalive", "--tcp-nodelay", "--tcp-fastopen",
    "--connect-to", "--happy-eyeballs-timeout-ms",
    "--dns-servers", "--doh-url", "--hsts", "--http3", "--http3-only",
    "--socks4", "--socks4a", "--socks5", "--socks5-hostname", "--preproxy",
    "--proxy-header", "-U", "--proxy-user", "--noproxy", "-x", "--proxy",
    "--max-filesize", "--speed-limit", "--speed-time",
    "--retry-all-errors", "--retry-connrefused", "--fail-early", "--fail-with-body",
    "--expect100-timeout", "--keepalive",
    # 其它常见的“复制出来带一堆”
    "--styled-output", "--no-styled-output", "--raw", "--tr-encoding",
]


@pytest.mark.parametrize("opt", SILENT_OPTS)
def test_connection_opts_are_registered(opt):
    if opt in C._NO_ARG_OPTS:
        cmd = f"curl {opt} {q(URL)} -H 'app-id: 1'"
    else:
        cmd = f"curl {opt} 'v' {q(URL)} -H 'app-id: 1'"
    r = parse(cmd)
    assert r["url"] == URL, f"{opt} 干扰了 URL"
    assert r["headers"] == {"app-id": "1"}, f"{opt} 污染了 headers：{r['headers']}"
    assert not unknown_warns(r), f"{opt} 没登记进选项表：{r['warnings']}"


# ══════════════════════════════════════════════════════
#  B3. 常见有语义选项抽样（含真实工具导出形态）
# ══════════════════════════════════════════════════════

def test_header_empty_value_forms():
    r = parse(f"curl {q(URL)} -H 'X-Empty;' -H 'X-Blank:'")
    assert r["headers"] == {"X-Empty": "", "X-Blank": ""}, r["headers"]


def test_chinese_header_value_kept_verbatim():
    r = parse(f"curl {q(URL)} -H 'x-中文: 中文值' -H 'x-mix: a 中 b'")
    assert r["headers"]["x-中文"] == "中文值", r["headers"]
    assert r["headers"]["x-mix"] == "a 中 b", r["headers"]


def test_form_field_with_semicolon_attributes():
    r = parse(f"curl {q(URL)} -F 'f=v;type=text/plain' -F 'g=h'")
    assert r["body"] == {"f": "v", "g": "h"}, r["body"]


def test_cookie_forms_equivalent():
    """-b / --cookie / -H Cookie 三种写法必须得到同一份 cookie（头名大小写不参与语义）"""
    a = parse(f"curl {q(URL)} -b 'c=1; d=2'")
    b = parse(f"curl {q(URL)} --cookie 'c=1; d=2'")
    c = parse(f"curl {q(URL)} -H 'Cookie: c=1; d=2'")
    low = [{k.lower(): v for k, v in x["headers"].items()} for x in (a, b, c)]
    assert low[0] == low[1] == low[2], low
    assert low[0] == {"cookie": "c=1; d=2"}, low[0]


# ══════════════════════════════════════════════════════
#  C. YAML 端到端：解析结果必须一字不差落进 YAML
# ══════════════════════════════════════════════════════

ALL_KINDS_CMD = f"""curl -X POST {q(URL + "?p=1")} \\
  -H 'accept: application/json, text/plain, */*' \\
  -H 'x-empty:' \\
  -H 'x-utf8: 中文值' \\
  -b 'c=1; d=2' \\
  -A 'UA/2.0' \\
  -e 'https://r.com' \\
  -u 'u:p' \\
  --data-raw '{{"a":1,"b":[1,{{"c":null}}],"s":"x y"}}'"""


def test_config_carries_every_parsed_field():
    r = parse(ALL_KINDS_CMD)
    assert not unknown_warns(r) and r["warnings"] == [], r["warnings"]
    cfg = C.build_config(r["url"], r["method"], r["headers"], r["body"])
    req = cfg["request"]
    assert req["url"] == r["url"]
    assert req["method"] == r["method"]
    assert req["headers"] == r["headers"]
    assert req["body"] == r["body"]
    assert len(req["headers"]) == 7, req["headers"]
    assert req["headers"]["x-empty"] == ""
    assert req["headers"]["x-utf8"] == "中文值"


@pytest.mark.parametrize("opt", ARG_OPTS)
def test_config_roundtrip_for_every_option(opt):
    kind = C._ARG_OPTS[opt]
    val = SAMPLE[kind]
    cmd = (f"curl {opt} {q(val)} -H 'app-id: 1'" if kind == "url"
           else f"curl {opt} {q(val)} {q(URL)} -H 'app-id: 1'")
    r = parse(cmd)
    cfg = C.build_config(r["url"], r["method"], r["headers"], r["body"])
    req = cfg["request"]
    assert req["headers"] == r["headers"], opt
    assert req.get("body") == r["body"], opt
    assert req["url"] == r["url"], opt
    assert req["method"] == r["method"], opt


# ══════════════════════════════════════════════════════
#  D. 生成的 step 结构必须能过结构校验（与界面保存链路一致）
# ══════════════════════════════════════════════════════

E2E_SAMPLES = [
    # 带 --url-query 的 GET
    f"curl {q(URL)}?p=1 --url-query 'page=2' --url-query 'q=a b'",
    # 带认证的 GET
    f"curl {q(URL)} --oauth2-bearer 'tok123' -H 'app-id: 1'",
    # 一堆下发类选项 + 真实请求
    (f"curl {q(URL)} -D h.txt --connect-to a.com:443:b.com:443 --keepalive-time 60 "
     "--http3 --retry 3 --retry-all-errors -x http://127.0.0.1:8888 -U u:p "
     "--doh-url https://d/dns -H 'app-id: 1'"),
    # 表单 + mime 属性
    f"curl {q(URL)} -F 'f=v;type=text/plain' -F 'g=h'",
    # 多个 --json
    f"curl {q(URL)} --json '{{\"a\":1}}' --json '{{\"b\":2}}'",
    # 非 JSON 原文 body
    f"curl {q(URL)} -H 'content-type: text/plain' --data-raw 'line1\nline2'",
]


@pytest.mark.parametrize("cmd", E2E_SAMPLES)
def test_generated_config_passes_structure_check(cmd):
    r = parse(cmd)
    cfg = C.build_config(r["url"], r["method"], r["headers"], r["body"])
    errors = yaml_checker.check_config(cfg)
    hard = [e for e in errors if "[WARN]" not in str(e[1])]
    assert not hard, hard

