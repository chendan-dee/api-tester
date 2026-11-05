# -*- coding: utf-8 -*-
"""curl 覆盖审计：凡“无法静态还原”的写法必须产生警告，凡“可还原”的必须精确还原

fidelity（随机保真）只证一致性，本文件证两件事：
  A. 需要文件 / 本地环境 / 多请求的写法，解析后绝不能静默消失，必须出现在 warnings；
  B. 上一轮补齐的缺口（-H @file / 多次 --url / --json @file / 扩展方法）不回退。

运行：cd .qoder/skills/api-tester/_tests ; pytest -q test_curl_coverage.py
"""
import pytest

import curl2yaml as C
import yaml_checker

URL = "https://a.com/x"


def parse(cmd):
    return C.parse_curl_report(cmd)


# ==========================================================
#  A. 无法静态还原的写法：必有警告，绝不允许静默
# ==========================================================

@pytest.mark.parametrize("opt,val,keyword", [
    ("-H", "@headers.txt", "header"),           # curl 支持 -H @file 从文件读头
    ("--header", "@headers.txt", "header"),
    ("-d", "@body.json", "body"),               # 内联文件
    ("--data", "@body.json", "body"),
    ("--data-binary", "@bin.dat", "body"),
    ("--data-raw", "@body.json", "body"),
    ("--json", "@body.json", "body"),
    ("--data-urlencode", "name@words.txt", "文件"),
    ("--url-query", "@queries.txt", "文件"),
    ("-b", "cookie.txt", "cookie"),
    ("-b", "@cookies.txt", "cookie"),
    ("-T", "upload.bin", "body"),
])
def test_unrestorable_form_must_warn(opt, val, keyword):
    cmd = f"curl {opt} '{val}' '{URL}' -H 'app-id: 1'"
    r = parse(cmd)
    ctx = f"{opt} {val}\nwarnings={r['warnings']}"
    assert r["url"] == URL, ctx
    assert r["warnings"], f"{opt} {val} 必须产生警告：" + ctx
    assert any(keyword in w for w in r["warnings"]), ctx
    # 绝不能把文件名当报文发出去
    if opt.startswith("-H") or opt == "--header":
        assert all(not k.startswith("@") for k in r["headers"]), ctx
    else:
        assert r["body"] is None or "@" not in str(r["body"])[:2], ctx


def test_multi_url_option_keeps_first_and_warns():
    r = parse(f"curl --url '{URL}' --url 'https://b.com/y'")
    assert r["url"] == URL, r
    assert any("多次" in w for w in r["warnings"]), r["warnings"]


def test_multi_url_option_same_value_silent():
    r = parse(f"curl --url '{URL}' --url '{URL}'")
    assert r["url"] == URL
    assert not [w for w in r["warnings"] if "多次" in w], r["warnings"]


def test_aws_sigv4_warns():
    r = parse(f"curl --aws-sigv4 'aws:amz:cn-north-1:s3' '{URL}'")
    assert any("签名" in w for w in r["warnings"]), r["warnings"]


def test_upload_option_warns():
    r = parse(f"curl -T 'f.bin' '{URL}'")
    assert any("body" in w for w in r["warnings"]), r["warnings"]
    assert r["body"] is None


def test_form_file_field_warns():
    r = parse(f"curl -F 'f=@a.txt' -F 'k=v' '{URL}'")
    assert any("文件" in w for w in r["warnings"]), r["warnings"]
    assert r["body"] == {"k": "v"}, r["body"]


def test_next_multi_request_warns():
    r = parse(f"curl '{URL}' --next 'https://b.com/y'")
    assert any("--next" in w for w in r["warnings"]), r["warnings"]


def test_multiple_positional_urls_warns():
    r = parse(f"curl '{URL}' 'https://b.com/y'")
    assert r["url"] == URL
    assert any("多个请求地址" in w for w in r["warnings"]), r["warnings"]


# ==========================================================
#  B. 可还原的写法：精确还原（Postman 口径）
# ==========================================================

@pytest.mark.parametrize("arg,expect", [
    ("name=hello world", "name=hello%20world"),
    ("=hello world", "=hello%20world"),
    ("hello world", "hello%20world"),
    ("name=%E5%B7%B2%E7%BC%96%E7%A0%81", "name=%25E5%25B7%25B2%25E7%25BC%2596%25E7%25A0%2581"),
])
def test_data_urlencode_forms(arg, expect):
    r = parse(f"curl --data-urlencode '{arg}' '{URL}'")
    assert r["body"] == expect, (arg, r["body"])


def test_data_urlencode_space_encoding():
    # curl 用 curl_easy_escape：空格 → %20（不是 +）
    r = parse(f"curl --data-urlencode 'name=value with space' '{URL}'")
    assert r["body"] == "name=value%20with%20space", r["body"]


@pytest.mark.parametrize("method", ["PROPFIND", "REPORT", "TRACE", "MKCOL"])
def test_extended_methods_survive_check(method):
    r = parse(f"curl -X {method} '{URL}'")
    assert r["method"] == method
    cfg = C.build_config(r["url"], r["method"], r["headers"], r["body"])
    errors = yaml_checker.check_config(cfg)
    hard = [e for e in errors if "[WARN]" not in str(e[1])]
    assert not hard, hard


def test_json_at_file_never_leaks_as_body():
    r = parse(f"curl --json '@body.json' '{URL}'")
    assert r["body"] is None, r["body"]
    assert any("文件" in w for w in r["warnings"]), r["warnings"]


def test_compressed_strips_unsupported_encoding():
    r = parse(f"curl --compressed -H 'accept-encoding: gzip, deflate, br, zstd' '{URL}'")
    got = {k.lower(): v for k, v in r["headers"].items()}
    enc = got.get("accept-encoding", "")
    assert "br" not in enc or "移除" in "".join(r["warnings"])
