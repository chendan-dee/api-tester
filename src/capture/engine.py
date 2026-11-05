#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""capture/engine.py — 抓包引擎（mitmproxy 子进程）管理

对外接口:
    start() / stop() / status() / is_running() / cleanup_port_occupants()

设计约束：
- 引擎绝不随主服务自动启动，仅在界面手动点击「启动抓包」时拉起；
- 主服务退出时由 app.py 通过 atexit 调用 stop()，不留残留进程；
- 子进程为 mitmdump（run_mitm.py + addon.py），CA 复用项目自签证书
  （合并为 mitmproxy-ca.pem 注入 confdir，已装过工具箱证书的设备免重装）。
"""

import os
import socket
import subprocess
import sys
import threading
import time


class CaptureEngine:
    """抓包引擎子进程管理器（线程安全）"""

    def __init__(self, app_root, port=8899, report_port=5050, local_ip=""):
        self.app_root = app_root
        self.port = port
        self.report_port = report_port
        self.local_ip = local_ip
        self._proc = None
        self._lock = threading.Lock()

    # ── 路径 ────────────────────────────────────────

    def _module_dir(self):
        return os.path.dirname(os.path.abspath(__file__))

    def _addon_path(self):
        p = os.path.join(self._module_dir(), "addon.py")
        if os.path.isfile(p):
            return p
        # 打包版：addon.py 以 datas 形式放在 _MEIPASS/capture/ 下
        base = getattr(sys, "_MEIPASS", "")
        if base:
            alt = os.path.join(base, "capture", "addon.py")
            if os.path.isfile(alt):
                return alt
        return p

    def _runner_path(self):
        return os.path.join(self._module_dir(), "run_mitm.py")

    def _confdir(self):
        d = os.path.join(self.app_root, "certs", "mitm")
        os.makedirs(d, exist_ok=True)
        return d

    # ── CA 注入（证书复用）─────────────────────────

    def _ensure_ca(self):
        """把项目自签 CA（证书+私钥）合并为 mitmproxy-ca.pem 注入 confdir"""
        dst = os.path.join(self._confdir(), "mitmproxy-ca.pem")
        cert = os.path.join(self.app_root, "certs", "api-tester-ca.crt")
        key = os.path.join(self.app_root, "certs", "api-tester-ca-key.pem")
        if not (os.path.exists(cert) and os.path.exists(key)):
            # 源 CA 缺失时拒绝注入并让启动失败：mitmproxy 在空 confdir 下会自行
            # 生成另一套自签 CA，与设备安装的项目 CA 不一致 → HTTPS 全量证书报错
            print("[Capture] 项目 CA 缺失，拒绝启动（避免 mitmproxy 自签另一套 CA）")
            return False
        try:
            with open(cert, "r", encoding="utf-8") as f:
                cert_pem = f.read()
            with open(key, "r", encoding="utf-8") as f:
                key_pem = f.read()
            # 始终用最新源 CA 重写，避免 CA 更新后 mitmproxy 仍用旧签名
            with open(dst, "w", encoding="utf-8") as f:
                f.write(cert_pem.rstrip("\n") + "\n" + key_pem.rstrip("\n") + "\n")
            print(f"[Capture] CA 已注入 mitmproxy confdir: {dst}")
            return True
        except Exception as e:
            print(f"[Capture] CA 注入失败: {e}")
            return False

    # ── 可用性检查 ──────────────────────────────────

    def _check_mitm_installed(self):
        """检查 mitmproxy 是否可用（子进程 import 测试）"""
        if getattr(sys, "frozen", False):
            # 打包版：exe 无法执行 python -c，改用内部命令 __checkmitm__ 子进程验证
            probe_cmd = [sys.executable, "__checkmitm__"]
            timeout = 60
        else:
            probe_cmd = [sys.executable, "-c", "import mitmproxy.tools.dump"]
            timeout = 30
        try:
            r = subprocess.run(
                probe_cmd,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout,
            )
            return r.returncode == 0
        except Exception:
            return False

    def port_in_use(self):
        """探测抓包端口是否已有进程在监听"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        try:
            s.connect(("127.0.0.1", self.port))
            return True
        except Exception:
            return False
        finally:
            s.close()

    def is_running(self):
        return self._proc is not None and self._proc.poll() is None

    def set_port(self, port):
        """修改监听端口（仅引擎未运行时允许，返回是否成功）"""
        with self._lock:
            if self.is_running():
                return False
            self.port = int(port)
            return True

    # ── 启动 / 停止 ─────────────────────────────────

    def start(self):
        with self._lock:
            if self.is_running():
                return {"ok": True, "running": True, "port": self.port}
            if self.port_in_use():
                return {
                    "ok": False,
                    "error": "端口 %d 已被占用（可能是上次遗留的抓包进程），可尝试强制清理" % self.port,
                    "port_conflict": True,
                }
            if not self._check_mitm_installed():
                if getattr(sys, "frozen", False):
                    return {
                        "ok": False,
                        "error": "抓包引擎组件加载失败：请确认使用完整的打包版目录（勿单独复制 exe 文件）",
                    }
                return {
                    "ok": False,
                    "error": "抓包引擎（mitmproxy）未安装，请先执行: pip install mitmproxy",
                }
            if not self._ensure_ca():
                return {"ok": False, "error": "项目 CA 注入失败（证书缺失或写入权限不足），请检查 certs 目录"}

            mitm_args = [
                "-s", self._addon_path(),
                "--listen-port", str(self.port),
                "--set", "confdir=" + self._confdir(),
                "--set", "stream_large_bodies=10m",
                # 禁用上游证书引用：否则生成的证书会带上游证书的 CRL 地址，
                # Windows Schannel（Edge/系统应用）开启吊销检查时会因 CRL 不可达而硬失败
                "--set", "upstream_cert=false",
                "--quiet",
            ]
            if getattr(sys, "frozen", False):
                # 打包版：exe 无独立 python，复用 exe 本体以内部命令 __mitmdump__ 分派
                cmd = [sys.executable, "__mitmdump__"] + mitm_args
            else:
                cmd = [sys.executable, self._runner_path()] + mitm_args
            env = os.environ.copy()
            env["CAPTURE_REPORT_URL"] = f"http://127.0.0.1:{self.report_port}/api/capture/report"
            env["CAPTURE_WEB_PORT"] = str(self.report_port)
            if self.local_ip:
                env["CAPTURE_LOCAL_IP"] = self.local_ip
            # 来源记录开关文件（由主服务维护：web=本机 / device=手机），addon 节流读取
            env["CAPTURE_SCOPE_FILE"] = os.path.join(self.app_root, "data", "_capture_scope.json")
            # 功能配置（改包 Mock / 弱网 / 断点开关）与抓包专用 Mock 规则库：addon 节流读取，改动实时生效
            # 规则库与「代理 Mock」的 _mock_proxy.json 完全分离（抓包改包不影响代理 Mock，反之亦然）
            env["CAPTURE_ENGINE_CFG_FILE"] = os.path.join(self.app_root, "data", "_capture_engine.json")
            env["CAPTURE_MOCK_FILE"] = os.path.join(self.app_root, "data", "_capture_mock.json")
            # 上报失败落盘目录：主服务重启窗口期的流量先落盘，恢复后空闲补报（不丢流量）
            env["CAPTURE_SPOOL_DIR"] = os.path.join(self.app_root, "data", "_capture_spool")
            env["PYTHONIOENCODING"] = "utf-8"
            creationflags = 0
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            try:
                self._proc = subprocess.Popen(
                    cmd, env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                )
            except Exception as e:
                self._proc = None
                return {"ok": False, "error": f"引擎启动失败: {e}"}

            # 等待端口就绪（最长 20 秒）
            for _ in range(200):
                if self.port_in_use():
                    print(f"[Capture] 引擎已启动，监听端口 {self.port}")
                    return {"ok": True, "running": True, "port": self.port}
                if self._proc.poll() is not None:
                    self._proc = None
                    return {"ok": False, "error": "引擎进程异常退出，请检查 mitmproxy 是否完整安装"}
                time.sleep(0.1)
            self._kill_proc()
            return {"ok": False, "error": "引擎启动超时（20 秒），请查看端口占用"}

    def _kill_proc(self):
        """杀掉子进程树（覆盖 python launcher 双进程结构）"""
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    if sys.platform == "win32":
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=10,
                        )
                    else:
                        proc.kill()
        except Exception:
            pass

    def stop(self):
        with self._lock:
            was_running = self.is_running()
            self._kill_proc()
            time.sleep(0.3)
            force_cleaned = 0
            if self.port_in_use():
                # 引用丢失或 launcher 残留：按端口兜底清理
                force_cleaned = self.cleanup_port_occupants()
            if was_running or force_cleaned:
                print("[Capture] 引擎已停止")
            return {"ok": True, "running": False, "force_cleaned": force_cleaned}

    def cleanup_port_occupants(self):
        """按端口查找并杀掉占用进程树（兜底清理），返回清理数量"""
        if sys.platform != "win32":
            return 0
        try:
            output = subprocess.check_output(
                'netstat -ano | findstr ":%d " | findstr "LISTENING"' % self.port,
                shell=True, text=True, stderr=subprocess.DEVNULL, timeout=10,
            )
        except Exception:
            return 0
        pids = set()
        for line in output.strip().splitlines():
            parts = line.split()
            if parts:
                try:
                    pid = int(parts[-1])
                    if pid != os.getpid():
                        pids.add(pid)
                except (ValueError, IndexError):
                    pass
        count = 0
        for pid in pids:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                )
                count += 1
            except Exception:
                pass
        if count:
            print(f"[Capture] 已强制清理端口 {self.port} 的占用进程 {count} 个")
        return count

    # ── 状态 ────────────────────────────────────────

    def status(self):
        return {
            "running": self.is_running(),
            "port": self.port,
            "port_in_use": self.port_in_use(),
        }
