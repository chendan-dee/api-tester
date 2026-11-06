# -*- coding: utf-8 -*-
"""engine 发送层回归测试：解析对了，发出去也不能变形

运行：cd .qoder/skills/api-tester/tests ; pytest -q
"""
import pytest

import engine


class FakeResp:
    status_code = 200
    headers = {"content-type": "application/json"}
    text = "{}"

    def json(self):
        return {}


@pytest.fixture
def captured(monkeypatch):
    box = {}

    def fake_request(method, url, **kw):
        box.update(method=method, url=url, **kw)
        return FakeResp()

    monkeypatch.setattr(engine.requests, "request", fake_request)
    return box


def test_dict_body_sent_as_json(captured):
    engine.do_request({"url": "https://a.com/x", "method": "POST",
                       "headers": {"content-type": "application/json"},
                       "body": {"flag": 1}})
    assert captured["json"] == {"flag": 1}
    assert "data" not in captured


def test_raw_string_body_sent_as_data(captured):
    # 非标准 JSON（curl 里常见）必须原样发，不能被 requests 二次 json 编码
    engine.do_request({"url": "https://a.com/x", "method": "POST",
                       "headers": {"content-type": "application/json"},
                       "body": "{a:1}"})
    assert "json" not in captured
    assert captured["data"].decode("utf-8") == "{a:1}"


def test_form_body_sent_as_data(captured):
    engine.do_request({"url": "https://a.com/x", "method": "POST",
                       "headers": {"content-type": "application/x-www-form-urlencoded"},
                       "body": "a=1&b=2"})
    assert captured["data"] == "a=1&b=2"


def test_unsupported_encoding_stripped_at_runtime(captured):
    # 环境没装 brotli/zstandard 时，带 br 会让响应解不出来
    engine.do_request({"url": "https://a.com/x", "method": "GET",
                       "headers": {"accept-encoding": "gzip, deflate, br, zstd"}})
    ae = captured["headers"].get("accept-encoding", "")
    tokens = [t.strip() for t in ae.split(",") if t.strip()]
    assert "br" not in tokens and "zstd" not in tokens
    assert "gzip" in tokens


def test_all_unsupported_encoding_drops_header(captured):
    engine.do_request({"url": "https://a.com/x", "method": "GET",
                       "headers": {"accept-encoding": "br, zstd"}})
    assert "accept-encoding" not in {k.lower() for k in captured["headers"]}


def test_content_length_removed(captured):
    engine.do_request({"url": "https://a.com/x", "method": "POST",
                       "headers": {"content-length": "999", "app-id": "1"},
                       "body": {"a": 1}})
    assert "content-length" not in {k.lower() for k in captured["headers"]}
    assert captured["headers"]["app-id"] == "1"


def test_request_config_not_mutated(captured):
    # 跑第二遍时不能因为第一次 pop 掉 header 而少东西
    cfg = {"url": "https://a.com/x", "method": "POST",
           "headers": {"content-length": "999", "accept-encoding": "gzip, br"},
           "body": {"a": 1}}
    engine.do_request(cfg)
    assert cfg["headers"]["content-length"] == "999"
    assert cfg["headers"]["accept-encoding"] == "gzip, br"
