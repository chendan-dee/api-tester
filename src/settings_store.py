# -*- coding: utf-8 -*-
"""data/settings.json —— 工具内「主服务独占」配置的统一落点

过去每个功能各写一个 JSON（_variables.json / _environments.json / config.json …），
data/ 目录里堆了 20 多个文件，分不清哪个是配置、哪个是数据。现按「谁能写」归口：

  · 主服务独占、体积小、低频写 → 全部并进 settings.json，一项一个顶层段落；
  · 抓包引擎是 mitmproxy 子进程（经环境变量拿路径直读）、录制控制面板是 PyQt
    子进程，两者与主服务**跨进程**通信，继续用独立文件，不能并进来；
  · 互传记录（手机端高频轮询写）、笔记/随心记（打字自动保存、体积大）同样独立。

对外只有 load(name, default) / save(name, value)，整段覆盖 —— 与原「整文件覆盖」
语义一一对应，调用方照旧「先 load、改完 save」即可。

并发：所有段落共用本模块的 _LOCK，保存为「读全量 → 换单段 → 原子替换」，
避免 app.py 与 scheduler.py、dataset.py 各自持锁时互相覆盖。
"""
import copy
import json
import os
import sys
import threading

_dir = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    # 打包版：数据目录随 exe（__file__ 指向包内虚拟路径，不可用）
    _app_root = os.path.dirname(os.path.abspath(sys.executable))
else:
    _app_root = os.path.dirname(_dir)

_data_dir = os.path.join(_app_root, "data")
_settings_file = os.path.join(_data_dir, "settings.json")

# 段落名 -> 旧版独立文件名（只用于一次性迁移）
LEGACY_FILES = {
    "config": "config.json",
    "variables": "_variables.json",
    "environments": "_environments.json",
    "db_sources": "_db_sources.json",
    "mock_proxy": "_mock_proxy.json",
    "remote_proxy": "_remote_proxy.json",
    "capture": "_capture_config.json",
    "schedule": "_schedule_tasks.json",
    "dataset_active": "_dataset_active.json",
    "transfer_guard": "_transfer_guard.json",
    "todo_cats": "_workbench_todo_cats.json",
    "todos": "_workbench_todos.json",
}

_LOCK = threading.RLock()


def configure(data_dir):
    """由 app.py 启动时指定数据目录（exe 模式下以可执行文件位置为准）"""
    global _data_dir, _settings_file
    _data_dir = str(data_dir)
    _settings_file = os.path.join(_data_dir, "settings.json")


def path():
    return _settings_file


def data_dir():
    return _data_dir


def _read_all():
    """读全量配置；文件缺失返回 {}，损坏时另存 .bad 后返回 {}"""
    try:
        with open(_settings_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        try:
            os.replace(_settings_file, _settings_file + ".bad")
            print("[settings] settings.json 解析失败，已另存为 .bad：%s" % e)
        except Exception:
            pass
        return {}


def _write_all(data):
    os.makedirs(_data_dir, exist_ok=True)
    tmp = _settings_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _settings_file)


def load(name, default=None):
    """取一个段落（返回副本，调用方随便改都不会污染下一次读取）"""
    with _LOCK:
        data = _read_all()
    if name in data:
        return data[name]
    return copy.deepcopy(default)


def save(name, value):
    """整段覆盖：与原「整文件覆盖写」语义一致"""
    with _LOCK:
        data = _read_all()
        data[name] = value
        _write_all(data)


def sections():
    with _LOCK:
        return sorted(_read_all().keys())


def merged_legacy_files():
    """已被并入 settings.json 的旧文件名

    启动时用：旧版数据目录（~/.api-tester 等）里的同名文件不再重复拷进来，
    否则每次启动都会把早年的旧副本“复活”到 data/，看起来像还没合并。
    """
    with _LOCK:
        data = _read_all()
    names = {fname for name, fname in LEGACY_FILES.items() if name in data}
    if "config" in data:
        # 这两份也早就折进 config 段落（由 app.py 的 _migrate_legacy_config 处理）
        names.update(("_device_names.json", "_transfer_save_dir.txt", "_transfer_max_items.txt"))
    return names


def migrate_legacy(log=print):
    """把旧版分散文件并入 settings.json，逐个校验后删除源文件

    只写「settings.json 里还没有的段落」：万一新旧同时存在，保留旧文件不动，
    宁可多留一个文件，也不覆盖用户配置。返回 {段落: 是否已迁移}。
    """
    result = {}
    for name, fname in LEGACY_FILES.items():
        src = os.path.join(_data_dir, fname)
        if not os.path.exists(src):
            continue
        try:
            with open(src, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            log("  [settings] 跳过迁移 %s（解析失败：%s）" % (fname, e))
            result[name] = False
            continue
        with _LOCK:
            data = _read_all()
            if name in data:
                log("  [settings] %s 对应段落已存在，保留旧文件不覆盖" % fname)
                result[name] = False
                continue
            data[name] = raw
            _write_all(data)
            ok = _read_all().get(name) == raw
        if not ok:
            log("  [settings] %s 迁移校验失败，保留旧文件" % fname)
            result[name] = False
            continue
        try:
            os.remove(src)
        except Exception as e:
            log("  [settings] %s 已并入但删除失败：%s" % (fname, e))
        result[name] = True
    merged = [n for n, ok in result.items() if ok]
    if merged:
        log("  [settings] 已并入统一配置 %d 项：%s" % (len(merged), ", ".join(merged)))
    return result
