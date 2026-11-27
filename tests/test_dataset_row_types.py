# -*- coding: utf-8 -*-
"""数据集合行标记（normal / error）：对齐规则、保存沿用、导入清空、单发下发

运行：cd .qoder/skills/api-tester/tests ; pytest -q
"""
import pytest

import dataset
import settings_store


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """存储路径指到临时目录，别动真实的 data/_datasets 与 settings.json"""
    monkeypatch.setattr(dataset, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dataset, "DATASET_DIR", str(tmp_path / "_datasets"))
    old_data_dir = settings_store.data_dir()
    settings_store.configure(str(tmp_path))
    yield
    settings_store.configure(old_data_dir)


def test_norm_types_align_and_fallback():
    assert dataset._norm_types(["error"], 3) == ["error", "normal", "normal"]
    assert dataset._norm_types(["ERROR", " we ", None], 3) == ["error", "normal", "normal"]
    assert dataset._norm_types(None, 2) == ["normal", "normal"]


def test_save_keeps_old_types_when_not_passed():
    """编辑表格只改 rows（不传 row_types）时，旧标记不能丢"""
    dataset.save("t", ["mobile"], [["13800000000"]], row_types=["error"])
    d = dataset.save("t", ["mobile"], [["13800000000"], ["13900000000"]])
    assert d["row_types"] == ["error", "normal"]


def test_save_clears_types_when_empty_list_passed():
    """导入同名表必须清空旧标记（app.dataset_import 显式传 []）"""
    dataset.save("t", ["mobile"], [["13800000000"]], row_types=["error"])
    d = dataset.save("t", ["mobile"], [["13900000000"]], row_types=[])
    assert d["row_types"] == ["normal"]


def test_first_row_carries_expected_type():
    dataset.save("t", ["mobile"], [["13800000000"], ["13900000000"]], row_types=["error", "normal"])
    dataset.set_active("t")
    used = dataset.first_row_for_steps([(1, {"url": "https://a.com?m=${mobile}"})])
    assert used["expected_type"] == "error"
    assert used["values"] == {"mobile": "13800000000"}


def test_rows_for_steps_returns_type_per_row():
    dataset.save("t", ["mobile"], [["13800000000"], ["x"]], row_types=["normal", "error"])
    dataset.set_active("t")
    rows = dataset.rows_for_steps([(1, {"url": "https://a.com?m=${mobile}"})])
    assert [r[2] for r in rows] == ["normal", "error"]


def test_rows_for_steps_ignores_unreferenced_dataset():
    dataset.save("t", ["mobile"], [["13800000000"]])
    dataset.set_active("t")
    assert dataset.rows_for_steps([(1, {"url": "https://a.com"})]) is None
