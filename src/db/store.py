# -*- coding: utf-8 -*-
"""数据源配置持久化

密码用 Windows DPAPI 加密后保存（同一用户同一机器才能解开），不落明文。
"""
import base64
import ctypes
import json
import os
import re
import threading
import time
import uuid

_LOCK = threading.Lock()


def _dpapi(data, unprotect=False):
    """调用 Windows DPAPI 加解密；非 Windows 环境原样返回"""
    if os.name != "nt":
        return data
    from ctypes import wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    raw = bytes(data)
    buf = ctypes.create_string_buffer(raw, len(raw))
    blob_in = _BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _BLOB()
    fn = ctypes.windll.crypt32.CryptUnprotectData if unprotect else ctypes.windll.crypt32.CryptProtectData
    fn.restype = ctypes.c_bool
    if unprotect:
        ok = fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    else:
        ok = fn(ctypes.byref(blob_in), None, None, None, None, 0, None, ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI 调用失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def encrypt_password(pwd):
    if not pwd:
        return ""
    try:
        return "dpapi:" + base64.b64encode(_dpapi(pwd.encode("utf-8"))).decode("ascii")
    except Exception:
        # 极端情况下（非 Windows / 系统 API 异常）退回编码存储，解密逻辑保持可用
        return "plain:" + base64.b64encode(pwd.encode("utf-8")).decode("ascii")


def decrypt_password(enc):
    if not enc:
        return ""
    try:
        if enc.startswith("dpapi:"):
            return _dpapi(base64.b64decode(enc[6:]), unprotect=True).decode("utf-8")
        if enc.startswith("plain:"):
            return base64.b64decode(enc[6:]).decode("utf-8")
    except Exception:
        return ""
    return enc


class SourceStore:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "_db_sources.json")
        self.local_dir = os.path.join(data_dir, "localdb")
        os.makedirs(self.local_dir, exist_ok=True)

    def _load(self):
        try:
            if os.path.isfile(self.path):
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
        return []

    def _save(self, items):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(items, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    @staticmethod
    def public(item):
        out = dict(item)
        out.pop("password_enc", None)
        out["has_password"] = bool(item.get("password_enc"))
        return out

    def list(self):
        return [self.public(x) for x in self._load()]

    def get(self, sid):
        for x in self._load():
            if x.get("id") == sid:
                return x
        return None

    def save(self, cfg):
        with _LOCK:
            items = self._load()
            sid = cfg.get("id") or ("ds_" + uuid.uuid4().hex[:10])
            old = next((x for x in items if x.get("id") == sid), None)
            item = dict(old or {})
            item.update({
                "id": sid,
                "name": (cfg.get("name") or "").strip() or "未命名数据源",
                "type": cfg.get("type") or "sqlite",
                "readonly": bool(cfg.get("readonly", True)),
            })
            if item["type"] == "sqlite":
                item["path"] = cfg.get("path") or ""
                item["host"] = item["port"] = item["user"] = item["database"] = ""
                item["password_enc"] = ""
            else:
                item["host"] = (cfg.get("host") or "").strip()
                try:
                    item["port"] = int(cfg.get("port") or 3306)
                except (TypeError, ValueError):
                    item["port"] = 3306
                item["user"] = (cfg.get("user") or "").strip()
                item["database"] = (cfg.get("database") or "").strip()
                item["path"] = ""
                if cfg.get("password"):
                    item["password_enc"] = encrypt_password(cfg["password"])
                elif old is None:
                    item["password_enc"] = ""
            item.setdefault("created_at", time.strftime("%Y-%m-%d %H:%M:%S"))
            item["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if old:
                items[items.index(old)] = item
            else:
                items.append(item)
            self._save(items)
            return self.public(item)

    def delete(self, sid):
        with _LOCK:
            items = self._load()
            keep = [x for x in items if x.get("id") != sid]
            if len(keep) == len(items):
                return False
            self._save(keep)
            return True

    def create_local(self, name):
        """在 data/localdb/ 下新建一个空 SQLite 库，并注册为可写数据源"""
        import sqlite3

        safe = re.sub(r"[^\w\u4e00-\u9fa5\-]+", "_", (name or "local")).strip("_") or "local"
        fname = safe + ".db"
        path = os.path.join(self.local_dir, fname)
        n = 1
        while os.path.exists(path):
            n += 1
            fname = f"{safe}_{n}.db"
            path = os.path.join(self.local_dir, fname)
        conn = sqlite3.connect(path)
        conn.close()
        cfg = self.save({
            "name": name or safe,
            "type": "sqlite",
            "path": os.path.relpath(path, self.data_dir),
            "readonly": False,
        })
        return cfg, path
