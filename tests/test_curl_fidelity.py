# -*- coding: utf-8 -*-
"""curl 保真测试：随机造 curl，解析回来必须与“意图”逐项一致

做法：先随机生成一份“意图”（url / method / 多条 header / cookie / body），
再把它渲染成不同人、不同工具会粘贴出来的写法（bash 单引号、bash 双引号、
Chrome cmd 的 ^" 风格、PowerShell 反引号风格；单行 / 多行续行；--opt value /
--opt=value；URL 在开头 / 结尾 / --url），最后解析回来逐项比对。

任何一项比对不通过，就说明真机上会丢参数。

运行：cd .qoder/skills/api-tester/tests ; pytest -q test_curl_fidelity.py
"""
import json
import random

import pytest

import curl2yaml as C


# ══════════════════════════════════════════════════════
#  渲染器：把 (选项, 值) 渲染成各种 shell 风格的 curl 文本
# ══════════════════════════════════════════════════════

def sh_single(v):
    """bash / zsh / curl.exe 单引号：' → '\\''"""
    return "'" + v.replace("'", "'\\''") + "'"


def sh_double(v):
    """bash 双引号：转义 \\ 和 " """
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def cmd_quoted(v):
    """Chrome ‘Copy as cURL (cmd)’：^"..."^"，内层 " → \\^"，^ → ^^，&|<>() → ^x"""
    out = []
    for ch in v:
        if ch == '"':
            out.append('\\^"')
        elif ch == "^":
            out.append("^^")
        elif ch in "&|<>()":
            out.append("^" + ch)
        else:
            out.append(ch)
    return '^"' + "".join(out) + '^"'


def ps_quoted(v):
    """PowerShell：双引号包裹（值里不能有 " / $ / ` 才用这种风格）"""
    return '"' + v + '"'


def ansi_c_quoted(v):
    """bash 的 $'...' ANSI-C 引用：反斜杠转义生效、单引号不能出现（Postman 复制多行 body 常见）"""
    return "$'" + v.replace("\\", "\\\\") + "'"


STYLES = ("bash", "bash_dq", "cmd", "ps", "ansi")


def can_render(v, style):
    """该风格能否无歧义地表达这个值"""
    if style == "bash":
        return True
    if style == "bash_dq":
        return "$" not in v and "`" not in v
    if style == "cmd":
        return "%" not in v and "\n" not in v
    if style == "ansi":
        return "'" not in v
    return not any(c in v for c in '"$`') and "\n" not in v


def wrap(v, style):
    return {"bash": sh_single, "bash_dq": sh_double, "cmd": cmd_quoted,
            "ps": ps_quoted, "ansi": ansi_c_quoted}[style](v)


CONT = {"bash": "\\", "bash_dq": "\\", "cmd": "^", "ps": "`", "ansi": "\\"}


def render(opts, url, style, url_mode, eq_form=False, multiline=True, exe=False):
    """opts: [(选项, 值 or None)]；url_mode: url_opt | first | last"""
    pieces = []      # 每个元素是一条 shell 片段（不含续行符）

    def opt_text(name, value):
        if value is None:
            return name
        if eq_form and name.startswith("--"):
            return f"{name}={wrap(value, style)}"
        return f"{name} {wrap(value, style)}"

    for name, value in opts:
        pieces.append(opt_text(name, value))

    if url_mode == "url_opt":
        pieces.insert(0, opt_text("--url", url))
    elif url_mode == "first":
        pieces.insert(0, wrap(url, style))
    elif url_mode == "last":
        pieces.append(wrap(url, style))

    head = "curl.exe" if exe else "curl"
    if not multiline:
        return head + " " + "  ".join(pieces)
    cont = CONT[style]
    body = (" " + cont + "\n  ").join(pieces)
    return f"{head} " + body


# ══════════════════════════════════════════════════════
#  意图生成
# ══════════════════════════════════════════════════════

HEADER_NAMES = ["accept", "app-id", "content-type", "referer", "sec-ch-ua",
                "x-trace-id", "fizz-appid", "token", "origin", "priority",
                "x-custom", "accept-language", "sec-fetch-mode", "user-agent"]

HOSTILE = ['plain', 'a b', 'a:b', 'a;b', 'a,b', 'a=b', 'q"q', "s'q", "a\\b",
           '#hash', '&amp', '|pipe', '*star', '(paren)', '<tag>', '?q=1',
           '@at', '!bang', '中文值', 'x' * 60, 'a b:c;d,e=f']

URL_HOSTS = ["https://a.com/api/x", "https://mkttest.bestwehotel.net/proxy/point-alert/getAlertRule",
             "https://a.com/x?p=1&q=2", "http://127.0.0.1:8080/api/v1/list"]


def rand_header_value(rnd):
    a, b = rnd.choice(HOSTILE), rnd.choice(HOSTILE)
    return rnd.choice([a, f"{a}, {b}", f"{a};{b}", f"Bearer {a}"])


def rand_json(rnd, depth=2):
    def val(d):
        if d <= 0:
            return rnd.choice([1, 0, -1, 1.5, True, False, None, "s",
                               "中文", "110066", "", "a:b", "x" * 20])
        kind = rnd.choice(["s", "d", "l", "d", "l"])
        if kind == "s":
            return rnd.choice([1, 2, "v", None, True, 0])
        if kind == "d":
            return {rnd.choice(["a", "b", "id", "名称", "k:x"]): val(d - 1)
                    for _ in range(rnd.randint(0, 3))}
        return [val(d - 1) for _ in range(rnd.randint(0, 3))]

    return {"flag": rnd.randint(0, 9), "list": [val(depth - 1) for _ in range(rnd.randint(0, 3))],
            "obj": val(depth - 1)}


def build_intent(rnd):
    want_style = rnd.choice(STYLES)
    url = rnd.choice(URL_HOSTS)
    if rnd.random() < 0.15:
        url += "&r=%E4%B8%AD%E6%96%87"

    headers = []
    for name in rnd.sample(HEADER_NAMES, rnd.randint(0, 6)):
        value = rand_header_value(rnd)
        if rnd.random() < 0.1:
            value = "中文头值"
        headers.append((name, value))

    cookie = None
    if rnd.random() < 0.7:
        cookie = "token=abc123; sso=LONG17f2f; gdp_user_id=gioenc%2Cb"

    method = None
    if rnd.random() < 0.6:
        method = rnd.choice(["GET", "POST", "PUT", "DELETE", "PATCH"])

    body_kind = rnd.choice(["json", "json_pretty", "form", "text", "none"])
    body_text = None
    body_expect = None
    if body_kind == "json":
        data = rand_json(rnd)
        body_text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        body_expect = data
    elif body_kind == "json_pretty":
        data = rand_json(rnd)
        body_text = json.dumps(data, ensure_ascii=False, indent=2)
        body_expect = data
    elif body_kind == "form":
        body_text = "a=1&b=2&c=%E4%B8%AD%E6%96%87"
        body_expect = body_text
    elif body_kind == "text":
        body_text = "hello world 中文"
        body_expect = body_text

    opts = []
    for name, value in headers:
        opts.append((rnd.choice(["-H", "--header"]), f"{name}: {value}"))
    if cookie:
        opts.append((rnd.choice(["-b", "--cookie"]), cookie))
    if method:
        opts.append((rnd.choice(["-X", "--request"]), method))
    if body_text is not None:
        opts.append((rnd.choice(["-d", "--data", "--data-raw", "--data-binary"]), body_text))
    if rnd.random() < 0.2:
        opts.append((rnd.choice(["--compressed", "--location", "-k", "-s", "-L"]), None))

    # 选一个能表达所有片段的风格
    all_values = [v for _, v in opts if v] + [url]
    style = next((s for s in [want_style] + list(STYLES)
                  if all(can_render(v, s) for v in all_values)), "bash")

    url_mode = rnd.choice(["url_opt", "first", "last"])
    text = render(opts, url, style,
                  url_mode=url_mode,
                  eq_form=(rnd.random() < 0.15),
                  multiline=(rnd.random() < 0.6),
                  exe=(rnd.random() < 0.3))

    return {
        "text": text, "style": style, "url_mode": url_mode,
        "url": url, "headers": {n.lower(): v for n, v in headers},
        "cookie": cookie, "method": method,
        "body_text": body_text, "body_expect": body_expect,
        "has_body": body_text is not None,
    }


# ══════════════════════════════════════════════════════
#  逐项比对
# ══════════════════════════════════════════════════════

def assert_fidelity(intent):
    r = C.parse_curl_report(intent["text"])
    ctx = f"\n--- style={intent['style']} url_mode={intent['url_mode']} ---\n{intent['text']}\n"

    assert r["url"] == intent["url"], "URL 不一致" + ctx
    assert not [w for w in r["warnings"] if "URL" in w], "URL 报了警告" + ctx

    got = {k.lower(): v for k, v in r["headers"].items()}
    for name, value in intent["headers"].items():
        assert got.get(name) == value, f"header {name} 不一致: {got.get(name)!r} != {value!r}" + ctx
    if intent["cookie"]:
        assert got.get("cookie") == intent["cookie"], "cookie 不一致" + ctx

    if intent["method"]:
        assert r["method"] == intent["method"], "method 不一致" + ctx
    else:
        assert r["method"] == ("POST" if intent["body_text"] is not None else "GET"), \
            "推断 method 不一致" + ctx

    assert r["body"] == intent["body_expect"], "body 不一致" + ctx
    return r


@pytest.mark.parametrize("seed", range(150))
def test_random_curl_fidelity(seed):
    rnd = random.Random(20261030 + seed)
    intent = build_intent(rnd)
    assert_fidelity(intent)


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("url_mode", ["url_opt", "first", "last"])
def test_style_x_urlmode_matrix(style, url_mode):
    # 用确定性种子：内置 hash() 受 PYTHONHASHSEED 随机化影响，会让用例时过时不过
    rnd = random.Random(sum(ord(c) for c in style + url_mode))
    intent = build_intent(rnd)
    values = [intent["url"]] + ([intent["cookie"]] if intent["cookie"] else [])
    values += list(intent["headers"].values())
    if not all(can_render(v, style) for v in values):
        pytest.skip("该风格无法表达这些字符")
    body = intent["body_text"]
    if body is not None and not can_render(body, style):
        pytest.skip("该风格无法表达该 body")
    text = render(
        [(rnd.choice(["-H", "--header"]), f"{n}: {v}") for n, v in intent["headers"].items()]
        + ([("-b", intent["cookie"])] if intent["cookie"] else [])
        + ([("-X", intent["method"])] if intent["method"] else [])
        + ([("--data-raw", body)] if body is not None else []),
        intent["url"], style, url_mode, multiline=True)
    intent["text"] = text
    intent["style"] = style
    intent["url_mode"] = url_mode
    assert_fidelity(intent)


# ══════════════════════════════════════════════════════
#  真实工具导出的典型形态（手写固定样本）
# ══════════════════════════════════════════════════════

REAL_SAMPLES = {
    # Chrome: Copy as cURL (bash)，URL 用 --url
    "chrome_bash": (
        r"""curl --url 'https://a.com/api/x' \
  -H 'accept: application/json, text/plain, */*' \
  -H 'cookie: a=1; b=2' \
  --data-raw '{"flag":1}'""",
        {"url": "https://a.com/api/x", "method": "POST",
         "headers": {"accept": "application/json, text/plain, */*", "cookie": "a=1; b=2"},
         "body": {"flag": 1}},
    ),
    # Chrome: Copy as cURL (cmd)，^"..."^" + ^ 续行
    "chrome_cmd": (
        'curl ^"https://a.com/api/x^" ^\n'
        '  -H ^"accept: application/json^" ^\n'
        '  -H ^"cookie: a=1; b=2^" ^\n'
        '  --data-raw ^"{\\^"flag\\^":1}^\\"',
        {"url": "https://a.com/api/x", "method": "POST",
         "headers": {"accept": "application/json", "cookie": "a=1; b=2"},
         "body": {"flag": 1}},
    ),
    # Chrome: Copy as cURL (PowerShell)
    "chrome_ps": (
        'curl.exe "https://a.com/api/x?p=1" `\n'
        '  -H "accept: application/json" `\n'
        '  --data-raw "{\\"flag\\":1}"',
        {"url": "https://a.com/api/x?p=1", "method": "POST",
         "headers": {"accept": "application/json"}, "body": {"flag": 1}},
    ),
    # Postman: Code snippet → cURL
    "postman": (
        "curl --location --request PUT 'https://a.com/api/x' \\\n"
        "--header 'Content-Type: application/json' \\\n"
        "--header 'Cookie: a=1; b=2' \\\n"
        "--header 'Authorization: Bearer xx.yy.zz' \\\n"
        "--data-raw '{\"flag\":1}'",
        {"url": "https://a.com/api/x", "method": "PUT",
         "headers": {"content-type": "application/json", "cookie": "a=1; b=2",
                     "authorization": "Bearer xx.yy.zz"}, "body": {"flag": 1}},
    ),
    # Fiddler / Charles 单行
    "fiddler": (
        'curl -X POST "https://a.com/api/x" -H "Host: a.com" -H "Connection: keep-alive" '
        '-H "Cookie: a=1; b=2" -H "Content-Type: application/json" --data-raw "{\\"flag\\":1}"',
        {"url": "https://a.com/api/x", "method": "POST",
         "headers": {"host": "a.com", "connection": "keep-alive",
                     "cookie": "a=1; b=2", "content-type": "application/json"},
         "body": {"flag": 1}},
    ),
    # Apifox / Insomnia 风格（--request + --header + --data-raw，URL 结尾）
    "apifox": (
        "curl --request POST \\\n"
        "  --url 'https://a.com/api/x?p=1' \\\n"
        "  --header 'accept: application/json' \\\n"
        "  --header 'content-type: application/json' \\\n"
        "  --data '{\"a\":[1,2],\"b\":{\"c\":null}}'",
        {"url": "https://a.com/api/x?p=1", "method": "POST",
         "headers": {"accept": "application/json", "content-type": "application/json"},
         "body": {"a": [1, 2], "b": {"c": None}}},
    ),
    # 前端自造（复制为 cURL）：-X 打头 + '\'' 转义 + URL 结尾
    "self_copy": (
        r"""curl -X POST \
  -H 'accept: application/json' \
  -H 'x-note: it'\''s ok' \
  --data-raw '{"flag":1}' \
  'https://a.com/api/x'""",
        {"url": "https://a.com/api/x", "method": "POST",
         "headers": {"accept": "application/json", "x-note": "it's ok"},
         "body": {"flag": 1}},
    ),
    # 单行无引号（值里没有空格）
    "bare": (
        "curl -X POST https://a.com/api/x -H accept:application/json -b a=1;b=2 --data-raw {\"flag\":1}",
        None,   # 这种写法本身有歧义（; 会被 shell 拆），不校验
    ),
}


@pytest.mark.parametrize("name", [k for k, v in REAL_SAMPLES.items() if v[1]])
def test_real_tool_samples(name):
    text, expect = REAL_SAMPLES[name]
    r = C.parse_curl_report(text)
    assert r["url"] == expect["url"], name
    assert r["method"] == expect["method"], name
    assert {k.lower(): v for k, v in r["headers"].items()} == expect["headers"], name
    assert r["body"] == expect["body"], name
    assert not [w for w in r["warnings"] if "URL" in w], name


# ══════════════════════════════════════════════════════
#  极限加压：长值 / 多 header / 重名 / 特殊字符共存
# ══════════════════════════════════════════════════════

def test_stress_long_cookie_and_many_headers():
    cookie = "; ".join(f"k{i}=v{i}" for i in range(120))          # 约 1.2KB
    headers = "".join(
        f"  -H 'x-h{i}: value-{i}, sub={i};q=0.{i}' \\\n" for i in range(25))
    cmd = (f"curl 'https://a.com/api/x' \\\n"
           f"  -b '{cookie}' \\\n{headers}  --data-raw '{{\"flag\":1}}'")
    r = C.parse_curl_report(cmd)
    assert r["url"] == "https://a.com/api/x"
    assert r["headers"]["cookie"] == cookie
    assert len(r["headers"]) == 26
    assert r["headers"]["x-h24"] == "value-24, sub=24;q=0.24"
    assert r["body"] == {"flag": 1}


def test_header_value_with_multiple_colons():
    r = C.parse_curl_report(
        "curl 'https://a.com/x' -H 'x-url: http://a.com:8080/p?q=1' -H 'x-time: 12:34:56'")
    assert r["headers"]["x-url"] == "http://a.com:8080/p?q=1"
    assert r["headers"]["x-time"] == "12:34:56"


def test_cookie_flag_and_cookie_header_merge():
    r = C.parse_curl_report("curl https://a.com/x -H 'Cookie: a=1' -b 'b=2' -H 'cookie: c=3'")
    assert r["headers"]["Cookie"] == "a=1; b=2; c=3"


def test_duplicate_header_merged_in_order():
    r = C.parse_curl_report("curl https://a.com/x -H 'x-a: 1' -H 'x-a: 2' -H 'X-A: 3'")
    assert r["headers"]["x-a"] == "1, 2, 3"


def test_get_with_data_urlencode():
    r = C.parse_curl_report(
        "curl -G 'https://a.com/x' --data-urlencode 'q=中文 空格' --data-urlencode 'p=1+2'")
    assert r["url"] == "https://a.com/x?q=%E4%B8%AD%E6%96%87%20%E7%A9%BA%E6%A0%BC&p=1%2B2"
    assert r["method"] == "GET" and r["body"] is None


def test_body_with_dollar_percent_ampersand():
    body = '{"price":"$100","url":"a%20b","q":"x&y=z"}'
    r = C.parse_curl_report(f"curl 'https://a.com/x' --data-raw '{body}'")
    assert r["body"] == {"price": "$100", "url": "a%20b", "q": "x&y=z"}


def test_multiline_body_exact():
    body = '{\n  "a": 1,\n\n  "b": [\n    1,\n    2\n  ]\n}'
    r = C.parse_curl_report("curl 'https://a.com/x' --data-raw '" + body + "'")
    assert r["body"] == {"a": 1, "b": [1, 2]}


def test_multiline_non_json_body_exact():
    body = "line1\n  line2\n\tline3\n\nline5"
    r = C.parse_curl_report("curl 'https://a.com/x' -H 'content-type: text/plain' -d '" + body + "'")
    assert r["body"] == body


def test_unicode_everywhere():
    cmd = ("curl 'https://a.com/api/查询?名称=张三' "
           "-H 'x-name: 中文值' -b '登录态=已登录; token=abc' "
           "--data-raw '{\"名称\":\"张三\",\"备注\":\"中文\"}'")
    r = C.parse_curl_report(cmd)
    assert r["url"] == "https://a.com/api/查询?名称=张三"
    assert r["headers"]["x-name"] == "中文值"
    assert r["headers"]["cookie"] == "登录态=已登录; token=abc"
    assert r["body"] == {"名称": "张三", "备注": "中文"}


def test_long_single_line_3000_chars():
    val = "v" * 3000
    r = C.parse_curl_report(f"curl https://a.com/x -H 'x-long: {val}'")
    assert r["headers"]["x-long"] == val
