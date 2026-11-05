# -*- coding: utf-8 -*-
"""curl → yaml 解析回归测试

运行：cd .qoder/skills/api-tester/_tests ; pytest -q
这里每一条用例，都对应过一次（或差点发生）的“静默漏参数”。
以后改 curl2yaml.py，先跑这个目录，全绿才算完。
"""
import pytest

import curl2yaml as C


def parse(cmd):
    return C.parse_curl_report(cmd)


def hget(headers, name):
    """header 名大小写不敏感取值（YAML 里保留原样大小写）"""
    for k, v in headers.items():
        if str(k).lower() == name.lower():
            return v
    return None


# ══════════════════════════════════════════════════════
#  真实工具复制的原文样本
# ══════════════════════════════════════════════════════

# 用户报障的原样本：Chrome "Copy as cURL (bash)"
CHROME_BASH = r"""curl --url 'https://mkttest.bestwehotel.net/proxy/point-alert/getAlertRule' \
  -H 'accept: application/json, text/plain, */*' \
  -H 'accept-language: zh-CN,zh;q=0.9' \
  -H 'app-id: 110066' \
  -H 'content-type: application/json;charset=UTF-8' \
  -b 'gdp_user_id=gioenc-bgb76db1%2Ccb63; maliangtoken=GbevwdCLMlcx8+4E9pFgbo82VyXtx; SSO-AUTH-HEADER=LONG17f2f; token=LONG17f2f' \
  -H 'fizz-appid: dbebcdcd-dcd5-40be-ba62-131413148888, HYGLXTBF8266248EA54DAA9CB49D279537244F' \
  -H 'origin: https://mkttest.bestwehotel.net' \
  -H 'priority: u=1, i' \
  -H 'referer: https://mkttest.bestwehotel.net/h/admin/scoreCardWarning?index=3' \
  -H 'sec-ch-ua: "Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"' \
  -H 'sec-ch-ua-mobile: ?0' \
  -H 'sec-ch-ua-platform: "Windows"' \
  -H 'sec-fetch-dest: empty' \
  -H 'sec-fetch-mode: cors' \
  -H 'sec-fetch-site: same-origin' \
  -H 'token: LONG17f2f56aa5984723bbed29ec9aeb6e1f' \
  -H 'user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/153.0.0.0 Safari/537.36' \
  --data-raw '{"flag":1}'"""

# Chrome "Copy as cURL (cmd)"：^" 包引号 + ^ 续行
CHROME_CMD = r"""curl ^"https://a.com/api/x^" ^
  -H ^"accept: application/json^" ^
  -H ^"cookie: a=1; b=2^" ^
  --data-raw ^"{\^"flag\^":1}^\""""

# Chrome "Copy as cURL (PowerShell)"：反引号续行 + 双引号内 \" 转义
CHROME_PS = r"""curl.exe "https://a.com/api/x?p=1" `
  -H "accept: application/json" `
  -H "cookie: a=1; b=2" `
  --data-raw "{\"flag\":1}" """

# api-tester 自己「复制为 cURL」的格式：-X 打头、header 单引号转义、URL 放最后
FRONTEND_COPY = r"""curl -X POST \
  -H 'accept: application/json' \
  -H 'x-note: it'\''s ok' \
  --data-raw '{"flag":1}' \
  'https://a.com/api/x'"""

# Postman "Code snippet → cURL"
POSTMAN = r"""curl --location --request POST 'https://a.com/api/x' \
--header 'Content-Type: application/json' \
--header 'Cookie: a=1; b=2' \
--data-raw '{"flag":1}'"""

# Fiddler / Charles 的单行导出
FIDDLER = r'''curl -X POST "https://a.com/api/x" -H "Host: a.com" -H "Connection: keep-alive" -H "Cookie: a=1; b=2" -H "Content-Type: application/json" --data-raw "{\"flag\":1}"'''


# ══════════════════════════════════════════════════════
#  1. 真实样本全字段保真
# ══════════════════════════════════════════════════════

def test_user_real_curl_all_fields():
    r = parse(CHROME_BASH)
    assert r["url"] == "https://mkttest.bestwehotel.net/proxy/point-alert/getAlertRule"
    assert r["method"] == "POST"
    assert r["body"] == {"flag": 1}
    h = r["headers"]
    assert len(h) == 16 + 1                      # 16 个 -H + cookie
    assert h["cookie"].endswith("token=LONG17f2f") and "gdp_user_id" in h["cookie"]
    assert h["sec-ch-ua"] == '"Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"'
    assert h["referer"].endswith("scoreCardWarning?index=3")
    assert h["priority"] == "u=1, i"
    assert "accept-encoding" not in {k.lower() for k in h}
    assert r["warnings"] == []


def test_chrome_cmd_style():
    r = parse(CHROME_CMD)
    assert r["url"] == "https://a.com/api/x"
    assert r["body"] == {"flag": 1}
    assert r["headers"]["accept"] == "application/json"
    assert r["headers"]["cookie"] == "a=1; b=2"


def test_powershell_style():
    r = parse(CHROME_PS)
    assert r["url"] == "https://a.com/api/x?p=1"
    assert r["body"] == {"flag": 1}
    assert r["headers"]["cookie"] == "a=1; b=2"


def test_frontend_copy_style():
    r = parse(FRONTEND_COPY)
    assert r["url"] == "https://a.com/api/x"
    assert r["method"] == "POST"
    assert r["headers"]["x-note"] == "it's ok"   # '...'\''...' 转义要还原
    assert r["body"] == {"flag": 1}


def test_postman_style():
    r = parse(POSTMAN)
    assert r["url"] == "https://a.com/api/x"
    assert r["method"] == "POST"
    assert hget(r["headers"], "content-type") == "application/json"
    assert hget(r["headers"], "cookie") == "a=1; b=2"
    assert hget(r["headers"], "content-type") == "application/json"
    assert r["body"] == {"flag": 1}


def test_fiddler_single_line():
    r = parse(FIDDLER)
    assert r["url"] == "https://a.com/api/x"
    assert hget(r["headers"], "host") == "a.com"
    assert hget(r["headers"], "cookie") == "a=1; b=2"
    assert r["body"] == {"flag": 1}


# ══════════════════════════════════════════════════════
#  2. 选项写法
# ══════════════════════════════════════════════════════

def test_attached_short_opts_and_equals_form():
    r = parse("""curl --url=https://a.com/x -H'accept: json' -XPOST -d'{"a":1}'""")
    assert r["url"] == "https://a.com/x"
    assert r["method"] == "POST"
    assert r["headers"]["accept"] == "json"
    assert r["body"] == {"a": 1}


def test_merged_no_arg_flags():
    r = parse("curl -sSLk 'https://a.com/x' -H 'app-id: 1'")
    assert r["url"] == "https://a.com/x"
    assert r["headers"]["app-id"] == "1"


def test_no_curl_prefix():
    r = parse("--url 'https://a.com/x' -H 'app-id: 1'")
    assert r["url"] == "https://a.com/x"


def test_url_only():
    r = parse("curl https://a.com/x")
    assert (r["url"], r["method"], r["headers"], r["body"]) == ("https://a.com/x", "GET", {}, None)


def test_url_as_positional_last():
    r = parse("curl -X GET -H 'a: b' 'https://a.com/x?q=1'")
    assert r["url"] == "https://a.com/x?q=1"
    assert r["method"] == "GET"


def test_unknown_option_never_eats_url():
    # 真正未登记的选项：只能给警告，绝不能把后面的 URL 当成它的取值
    r = parse("curl --x-not-a-real-option a:b:c:d --url 'https://a.com/x'")
    assert r["url"] == "https://a.com/x"
    assert any("x-not-a-real-option" in w for w in r["warnings"])


def test_registered_connection_option_warns_not_silent():
    # --connect-to 会把连接目标改到别的机器：已登记、不影响报文，
    # 但转换后的请求走真实 DNS，可能失败 → 必须警告而不是静默
    r = parse("curl --connect-to a.com:443:b.com:443 --url 'https://a.com/x'")
    assert r["url"] == "https://a.com/x"
    assert any("环境" in w for w in r["warnings"]), r["warnings"]


def test_url_missing_value_falls_back():
    # 值被漏粘时，也不能让 URL 变成 "-H"
    r = parse("curl --url -H 'app-id: 1' https://a.com/x")
    assert r["url"] == "https://a.com/x"
    assert r["warnings"]


def test_no_url_reports_warning():
    r = parse("curl -H 'a: b'")
    assert r["url"] == ""
    assert r["warnings"]


# ══════════════════════════════════════════════════════
#  3. cookie / body / 编码
# ══════════════════════════════════════════════════════

def test_cookie_flag_becomes_header():
    r = parse("curl https://a.com/x -b 'a=1; b=2'")
    assert r["headers"]["cookie"] == "a=1; b=2"


def test_cookie_file_never_leaks_into_header():
    r = parse("curl https://a.com/x -b cookies.txt")
    assert "cookie" not in r["headers"]
    assert any("cookie" in w for w in r["warnings"])


def test_cookie_without_equals_is_treated_as_file():
    r = parse("curl https://a.com/x -b mycookiejar")
    assert "cookie" not in r["headers"]
    assert r["warnings"]


def test_data_file_warns():
    r = parse("curl https://a.com/x --data-binary @payload.json")
    assert any("@payload.json" in w or "文件" in w for w in r["warnings"])


def test_get_mode_urlencodes_data():
    r = parse("curl -G 'https://a.com/x' --data-urlencode 'q=中文' --data-urlencode 'p=a b'")
    assert r["url"].startswith("https://a.com/x?")
    assert "q=%E4%B8%AD%E6%96%87" in r["url"]
    assert "p=a%20b" in r["url"]
    assert r["method"] == "GET" and r["body"] is None


def test_multiline_json_body():
    cmd = """curl 'https://a.com/x' -H 'content-type: application/json' --data '{
  "a": 1,
  "b": [1, 2]
}'"""
    r = parse(cmd)
    assert r["body"] == {"a": 1, "b": [1, 2]}


def test_lenient_json_trailing_comma():
    r = parse("""curl https://a.com/x -d '{"a":1,}'""")
    assert r["body"] == {"a": 1}


def test_non_json_body_stays_raw_string():
    r = parse("""curl https://a.com/x -H 'content-type: application/json' -d '{a:1}'""")
    assert r["body"] == "{a:1}"          # 交给 engine 原样发，不能被二次编码


def test_form_urlencoded_body_stays_raw():
    r = parse("""curl https://a.com/x -H 'content-type: application/x-www-form-urlencoded' -d 'a=1&b=2'""")
    assert r["body"] == "a=1&b=2"


def test_multiple_data_parts_joined():
    r = parse("curl https://a.com/x -d 'a=1' -d 'b=2'")
    assert r["body"] == "a=1&b=2"


def test_json_flag():
    r = parse("""curl https://a.com/x --json '{"a":1}'""")
    assert r["body"] == {"a": 1}
    assert r["method"] == "POST"
    assert hget(r["headers"], "content-type") == "application/json"
    assert hget(r["headers"], "accept") == "application/json"


def test_form_flag():
    r = parse("curl https://a.com/x -F 'name=张三' -F 'age=18'")
    assert r["body"] == {"name": "张三", "age": "18"}
    assert "form-urlencoded" in hget(r["headers"], "content-type")


def test_auth_referer_user_agent():
    r = parse("curl https://a.com/x -A 'UA/1.0' -e 'https://r.com' -u 'u:p'")
    assert r["headers"]["user-agent"] == "UA/1.0"
    assert r["headers"]["referer"] == "https://r.com"
    assert r["headers"]["authorization"] == "Basic dTpw"


# ══════════════════════════════════════════════════════
#  4. 会“请求失败”的头部
# ══════════════════════════════════════════════════════

def test_unsupported_accept_encoding_removed():
    r = parse("""curl https://a.com/x -H 'accept-encoding: gzip, deflate, br, zstd'""")
    ae = r["headers"].get("accept-encoding", "")
    assert "br" not in [t.strip() for t in ae.split(",")]
    assert "zstd" not in [t.strip() for t in ae.split(",")]
    assert "gzip" in ae
    assert any("accept-encoding" in w for w in r["warnings"])


def test_pseudo_headers_and_content_length_dropped():
    cmd = ("curl https://a.com/x -H ':authority: a.com' -H ':method: GET' "
           "-H 'content-length: 99' -H 'app-id: 1'")
    r = parse(cmd)
    keys = {k.lower() for k in r["headers"]}
    assert "content-length" not in keys
    assert all(k and not k.startswith(":") for k in keys)
    assert hget(r["headers"], "app-id") == "1"


def test_duplicate_headers_merged():
    r = parse("curl https://a.com/x -H 'cookie: a=1' -H 'cookie: b=2' -H 'accept: x' -H 'accept: y'")
    assert r["headers"]["cookie"] == "a=1; b=2"
    assert r["headers"]["accept"] == "x, y"


# ══════════════════════════════════════════════════════
#  5. build_config 输出结构（表单填充与落盘用的 dict）
# ══════════════════════════════════════════════════════

def test_config_roundtrip():
    r = parse(CHROME_BASH)
    cfg = C.build_config(r["url"], r["method"], r["headers"], r["body"])
    req = cfg["request"]
    assert req["url"] == r["url"]
    assert req["method"] == r["method"]
    assert req["body"] == r["body"]
    assert req["headers"] == r["headers"]
    # 不生成 assert：curl 只描述发什么请求，避免 201/204 被误判成“转换错误”
    assert "assert" not in cfg
    assert req["timeout"] == 60


def test_config_hostile_scalars():
    headers = {"x-a": 'q"uo\\te', "x-b": "#not-a-comment", "x-c": "line1\nline2"}
    cfg = C.build_config("https://a.com/x", "POST", headers, {"k": 'a"b'})
    assert cfg["request"]["headers"] == headers
    assert cfg["request"]["body"] == {"k": 'a"b'}


def test_config_nested_list_of_dict():
    body = {"items": [{"id": 1, "tags": ["a", "b"]}], "n": None, "b": True}
    cfg = C.build_config("https://a.com/x", "POST", {}, body)
    assert cfg["request"]["body"] == body
