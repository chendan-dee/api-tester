# -*- coding: utf-8 -*-
"""步骤读写回归测试（JSON 存储）：删除 / 移动 / 复制 / 保存必须"少一条就真少一条"

历史缺陷：旧 YAML 实现的 `_rewrite_steps` 会把顶格列表项当成"steps 之后的
顶级内容"原样保留，再叠加一份重新生成的步骤 —— 用户看到"点删除，结果变成复制"
（5 条变 9 条）。JSON 整文档读写从根上消灭这类问题，本文件守住等价场景。
"""
import json
import os

import pytest

import app as A


STEP = {
    "name": "{name}",
    "request": {"url": "https://a.com/{slug}", "method": "POST",
                "body": {"flag": 1}, "timeout": 15},
    "assert": {"status_code": 200},
}


def build_steps(names):
    return [json.loads(json.dumps(STEP).replace("{name}", n).replace("{slug}", n))
            for n in names]


def write_case(tmp_path, doc):
    p = tmp_path / "case.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    return str(p)


def _case_file(fname, doc):
    path = os.path.join(A.CASES_DIR, fname)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def _cleanup(*paths):
    for p in paths:
        for ext in ("", ".bak"):
            fp = p + ext
            if os.path.exists(fp):
                os.remove(fp)


def test_delete_api_shrinks_list():
    """走真实 /api/step/delete，验证 5 条 → 4 条，且不重复"""
    names = [f"Step{i}" for i in range(1, 6)]
    fname = "_tmp_del_regress.json"
    path = _case_file(fname, {"name": "回归", "steps": build_steps(names)})
    try:
        client = A.app.test_client()
        r = client.post("/api/step/delete", json={"file": fname, "index": 3})
        assert r.status_code == 200 and r.get_json()["ok"] is True
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
        assert [s["name"] for s in got["steps"]] == ["Step1", "Step2", "Step4", "Step5"]
        raw = open(path, encoding="utf-8").read()
        assert raw.count('"name": "Step3"') == 0, "被删的步骤又出现了"
        assert raw.count('"name": "Step1"') == 1, "步骤被重复写入"
    finally:
        _cleanup(path)


def test_delete_keeps_setup_and_doc_fields():
    """文档级字段（name/setup）必须保留，只动 steps"""
    doc = {
        "name": "带前置",
        "setup": {"ts": "datetime.now()"},
        "steps": build_steps(["S1", "S2"]),
        "mock_rules": [{"path": "/x", "status": 500}],
    }
    fname = "_tmp_del_header.json"
    path = _case_file(fname, doc)
    try:
        client = A.app.test_client()
        r = client.post("/api/step/delete", json={"file": fname, "index": 1})
        assert r.status_code == 200 and r.get_json()["ok"] is True
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
        assert [s["name"] for s in got["steps"]] == ["S2"]
        assert got["setup"]["ts"] == "datetime.now()", "头部 setup 被丢了"
        assert got["mock_rules"][0]["status"] == 500, "文档级字段被丢了"
        assert got["name"] == "带前置"
    finally:
        _cleanup(path)


def test_repeated_deletes_are_stable():
    """连续删两次，不能出现条数回弹"""
    names = ["A", "B", "C", "D"]
    fname = "_tmp_del_stable.json"
    path = _case_file(fname, {"name": "x", "steps": build_steps(names)})
    try:
        client = A.app.test_client()
        for _ in range(3):
            r = client.post("/api/step/delete", json={"file": fname, "index": 1})
            assert r.status_code == 200 and r.get_json()["ok"] is True
            with open(path, encoding="utf-8") as f:
                got = json.load(f)
            assert isinstance(got["steps"], list), "steps 类型坏了"
        with open(path, encoding="utf-8") as f:
            assert [s["name"] for s in json.load(f)["steps"]] == ["D"]
    finally:
        _cleanup(path)


def test_move_step_does_not_duplicate():
    """上移/下移不能复制"""
    fname = "_tmp_move.json"
    path = _case_file(fname, {"name": "x", "steps": build_steps(["A", "B"])})
    try:
        client = A.app.test_client()
        r = client.post("/api/step/move", json={"file": fname, "index": 2, "direction": "up"})
        assert r.status_code == 200 and r.get_json()["ok"] is True
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
        assert [s["name"] for s in got["steps"]] == ["B", "A"]
        assert len(got["steps"]) == 2, "移动后步骤数量变了"
    finally:
        _cleanup(path)


def test_duplicate_inserts_after_original():
    """复制步骤插在原步骤后并自动改名"""
    fname = "_tmp_dup.json"
    path = _case_file(fname, {"name": "x", "steps": build_steps(["A", "B"])})
    try:
        client = A.app.test_client()
        r = client.post("/api/step/duplicate", json={"file": fname, "index": 1})
        assert r.status_code == 200 and r.get_json()["ok"] is True
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
        assert [s["name"] for s in got["steps"]] == ["A", "A_copy", "B"]
    finally:
        _cleanup(path)


def test_delete_last_step_writes_empty_list():
    """删空后 steps 必须是 []，不能悬空"""
    fname = "_tmp_del_last.json"
    path = _case_file(fname, {"name": "x", "steps": build_steps(["only"])})
    try:
        client = A.app.test_client()
        r = client.post("/api/step/delete", json={"file": fname, "index": 1})
        assert r.status_code == 200 and r.get_json()["ok"] is True
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
        assert got["steps"] == []
    finally:
        _cleanup(path)


def test_step_save_append_delete_end_to_end():
    """保存（index=0 追加）→ 再保存 → 删除，条数与内容始终正确"""
    fname = "_tmp_save_append.json"
    path = _case_file(fname, {"name": "接口测试", "steps": [json.loads(json.dumps(STEP))]})
    step1 = json.loads(json.dumps(STEP))
    step1["name"] = "保存的步骤"
    step1["request"]["url"] = "https://a.com/1"
    step2 = json.loads(json.dumps(STEP))
    step2["name"] = "第二条"
    step2["request"]["url"] = "https://a.com/2"
    try:
        client = A.app.test_client()
        # 覆盖第 1 条
        r = client.post("/api/step/save", json={"file": fname, "index": 1, "step": step1})
        assert r.status_code == 200 and r.get_json()["ok"] is True
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert len(data["steps"]) == 1
        assert data["steps"][0]["name"] == "保存的步骤"
        assert data["steps"][0]["request"]["body"] == {"flag": 1}

        # 追加第 2 条
        r = client.post("/api/step/save", json={"file": fname, "index": 0, "step": step2})
        assert r.status_code == 200 and r.get_json()["ok"] is True
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert len(data["steps"]) == 2, "追加后条数不对"

        # 删除第 1 条
        r = client.post("/api/step/delete", json={"file": fname, "index": 1})
        assert r.status_code == 200 and r.get_json()["ok"] is True
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert len(data["steps"]) == 1, "删除后条数不对"
        assert data["steps"][0]["name"] == "第二条"
    finally:
        _cleanup(path)


def test_step_save_rejects_bad_structure():
    """结构校验未通过不允许落盘（如 method 拼写错误）"""
    fname = "_tmp_save_bad.json"
    path = _case_file(fname, {"name": "x", "steps": build_steps(["A"])})
    bad = json.loads(json.dumps(STEP))
    bad["request"]["method"] = "GOT"  # 拼写错误
    try:
        client = A.app.test_client()
        r = client.post("/api/step/save", json={"file": fname, "index": 1, "step": bad})
        body = r.get_json()
        assert r.status_code == 400 and body["ok"] is False
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["steps"][0]["request"]["method"] == "POST", "校验失败却已落盘"
    finally:
        _cleanup(path)


def test_save_warns_do_not_block():
    """重复步骤名只是 [WARN] 提示，不能阻塞保存"""
    fname = "_tmp_save_warn.json"
    path = _case_file(fname, {"name": "x", "steps": build_steps(["A"])})
    step = json.loads(json.dumps(STEP))
    step["name"] = "A"  # 与第 1 条重名 → check_config 产生 [WARN]
    try:
        client = A.app.test_client()
        r = client.post("/api/step/save", json={"file": fname, "index": 1, "step": step})
        assert r.status_code == 200 and r.get_json()["ok"] is True
    finally:
        _cleanup(path)


def test_doc_api_roundtrip():
    """/api/doc 返回全量结构，/api/doc/save 落盘后读回一致"""
    fname = "_tmp_doc.json"
    doc = {
        "name": "文档级",
        "setup": {"now": "datetime.now()"},
        "steps": build_steps(["A", "B"]),
    }
    client = A.app.test_client()
    path = os.path.join(A.CASES_DIR, fname)
    try:
        r = client.post("/api/doc/save", json={"file": fname, "doc": doc})
        assert r.status_code == 200 and r.get_json()["ok"] is True

        r = client.get(f"/api/doc?file={fname}")
        assert r.status_code == 200
        got = r.get_json()["doc"]
        assert got == doc, "落盘后读回不一致"

        # 更新 setup 后再读
        doc["setup"]["seq"] = "1"
        r = client.post("/api/doc/save", json={"file": fname, "doc": doc})
        assert r.status_code == 200
        r = client.get(f"/api/doc?file={fname}")
        assert r.get_json()["doc"]["setup"]["seq"] == "1"
    finally:
        _cleanup(path)
