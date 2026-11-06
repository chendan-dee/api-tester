# -*- coding: utf-8 -*-
"""表单化后端 API 测试：/api/curl2request（curl 解析填表）

curl 解析全链路（tokenizer/选项表/警告）已在 test_curl_* 系列覆盖，
本文件只验证"解析报告 → 表单填充 step"这一层新 API 的契约。
"""
import pytest

import app as A


def _post(client, url, **kw):
    return client.post(url, json=kw)


def test_curl2request_basic():
    client = A.app.test_client()
    r = _post(client, "/api/curl2request",
              curl="curl -X POST 'https://a.com/x?p=1' -H 'app-id: 110066' "
                   "-H 'Content-Type: application/json' --data-raw '{\"flag\": 1}'")
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] is True
    step = body["step"]
    assert step["name"] == "接口测试"
    req = step["request"]
    assert req["url"] == "https://a.com/x?p=1"
    assert req["method"] == "POST"
    assert req["headers"]["app-id"] == "110066"
    assert req["body"] == {"flag": 1}
    assert req["timeout"] == 60  # 默认超时 60 秒


def test_curl2request_with_name():
    client = A.app.test_client()
    r = _post(client, "/api/curl2request", curl="curl 'https://a.com/x'", name="登录接口")
    body = r.get_json()
    assert body["ok"] is True and body["step"]["name"] == "登录接口"


def test_curl2request_timeout_from_max_time():
    client = A.app.test_client()
    r = _post(client, "/api/curl2request", curl="curl -m 30 'https://a.com/x'")
    body = r.get_json()
    assert body["ok"] is True
    assert body["step"]["request"]["timeout"] == 30.0


def test_curl2request_missing_url():
    client = A.app.test_client()
    r = _post(client, "/api/curl2request", curl="curl -X POST -H 'a: b'")
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] is False
    assert "URL" in body["error"]


def test_curl2request_warnings_forwarded():
    """认证类选项必须进 warnings，不能静默漏掉"""
    client = A.app.test_client()
    r = _post(client, "/api/curl2request",
              curl="curl 'https://a.com/x' --digest -u 'admin:secret'")
    body = r.get_json()
    assert body["ok"] is True
    assert any("认证" in w for w in body["warnings"]), body["warnings"]


def test_curl2request_file_reference_not_leaked():
    """-d @file 不得把文件名当 body 塞进表单"""
    client = A.app.test_client()
    r = _post(client, "/api/curl2request", curl="curl -d '@body.json' 'https://a.com/x'")
    body = r.get_json()
    assert body["ok"] is True
    assert body["step"]["request"].get("body") is None
    assert any("文件" in w for w in body["warnings"]), body["warnings"]


# ═══════════════════════════════════════════════════════
#  /api/send：Postman 式即发即看（mock 模式免网络）
# ═══════════════════════════════════════════════════════

def test_send_mock_response():
    """mock 步骤不发网络请求，完整走 请求→断言→提取 链路"""
    client = A.app.test_client()
    step = {"name": "mock接口",
            "request": {"url": "https://a.com/x", "method": "GET", "timeout": 5},
            "assert": {"status_code": 200},
            "extract": {"code": "code"},
            "mock": {"status_code": 200, "response": {"code": 0, "msg": "ok"}}}
    r = _post(client, "/api/send", step=step)
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] is True
    assert body["is_mock"] is True
    assert body["status_code"] == 200
    assert body["resp_json"] == {"code": 0, "msg": "ok"}
    assert body["assert_passed"] is True
    assert body["extracted"] == ["${code}=0"]


def test_send_assert_fail_reported():
    """断言不通过时 assert_passed=False，且给出断言详情"""
    client = A.app.test_client()
    step = {"name": "x",
            "request": {"url": "https://a.com/x"},
            "assert": {"status_code": 404},
            "mock": {"status_code": 200, "response": {"a": 1}}}
    r = _post(client, "/api/send", step=step)
    body = r.get_json()
    assert body["ok"] is True and body["assert_passed"] is False
    assert body["asserts"], "断言失败时也要返回断言明细"


def test_send_requires_url():
    """没填 URL 直接 400，不落盘不发请求"""
    client = A.app.test_client()
    r = _post(client, "/api/send", step={"name": "x", "request": {"method": "GET"}})
    body = r.get_json()
    assert r.status_code == 400 and body["ok"] is False


def test_send_group_field_tolerated():
    """step 携带 group 等扩展字段不影响发送与结构校验"""
    client = A.app.test_client()
    step = {"name": "x", "group": "登录模块",
            "request": {"url": "https://a.com/x"},
            "mock": {"status_code": 200, "response": {"a": 1}}}
    r = _post(client, "/api/send", step=step)
    body = r.get_json()
    assert body["ok"] is True and body["status_code"] == 200
