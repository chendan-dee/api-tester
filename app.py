#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""app.py — API Tester Web 界面"""

import io
import json
import os
import re
import sys
import time
import socket
import sqlite3
import threading
import contextlib
import subprocess
from datetime import datetime

# ── 源码模块路径引导 ────────────────────────────────
# 入口 app.py 位于项目根目录（与 启动.bat 同级），engine/runner/capture/db/
# recorder_tool/seqvars 等业务模块都在 src/ 下，必须在导入它们之前加入 sys.path。
SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import seqvars          # 预设变量中的自增变量（序号器）

# 无控制台模式下（exe windowed）防止 print 报错
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

# ── 打包版内部命令分派 ──────────────────────────────
# 打包后录制面板 / 录制器 / mitmdump 子进程复用 exe 本体（目标机可能没有
# Python 环境），父进程以「工具箱.exe __panel__ ...」等形式拉起子进程，
# 入口在此按首参分派，避免依赖外置脚本文件与独立解释器。
if getattr(sys, 'frozen', False) and len(sys.argv) > 1:
    _dispatch = sys.argv[1]
    if _dispatch in ('__panel__', '__recorder__', '__mitmdump__', '__checkmitm__'):
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        if SRC_DIR not in sys.path:
            sys.path.insert(0, SRC_DIR)
        if _dispatch == '__mitmdump__':
            from mitmproxy.tools.main import mitmdump
            mitmdump()  # mitmdump 自行解析剩余 argv（-s/--listen-port/--set/--quiet）
            os._exit(0)
        elif _dispatch == '__checkmitm__':
            import importlib
            importlib.import_module('mitmproxy.tools.dump')
            os._exit(0)
        elif _dispatch == '__panel__':
            from recorder_tool.control_panel import main as _panel_main
            _panel_main()  # run_control_panel 内部以 sys.exit 结束，SystemExit 正常退出进程
            os._exit(0)
        else:  # __recorder__
            from recorder_tool.recorder import main as _recorder_main
            os._exit(_recorder_main())

# ── 服务端口（支持环境变量 API_TESTER_PORT 覆盖）──
try:
    PORT = int(os.environ.get("API_TESTER_PORT", "5050"))
except (TypeError, ValueError):
    PORT = 5050
if not (1 <= PORT <= 65535):
    PORT = 5050

# ═══════════════════════════════════════════════════
#  环境依赖检查与自动安装
# ═══════════════════════════════════════════════════
_REQUIRED_DEPS = [
    ("flask", "flask"),
    ("requests", "requests"),
    ("pandas", "pandas"),
    ("openpyxl", "openpyxl"),
    ("faker", "faker"),
    ("colorama", "colorama"),
    ("qrcode", "qrcode[pil]"),
]

_INSTALL_PAGE_HTML = '''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>工具箱 - 环境初始化</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family: "Segoe UI", "Microsoft YaHei", sans-serif; background:#0f172a; color:#e2e8f0;
         display:flex; align-items:center; justify-content:center; min-height:100vh; }
  .card { background:#1e293b; border:1px solid #334155; border-radius:16px; padding:48px 56px;
          max-width:560px; width:90%; text-align:center; box-shadow:0 20px 60px rgba(0,0,0,.4); }
  .spinner { width:48px; height:48px; border:4px solid #334155; border-top-color:#3b82f6;
             border-radius:50%; animation:spin 1s linear infinite; margin:0 auto 24px; }
  @keyframes spin { to { transform:rotate(360deg); } }
  h1 { font-size:20px; font-weight:600; margin-bottom:8px; }
  .sub { font-size:13px; color:#94a3b8; margin-bottom:24px; }
  .pkgs { display:flex; flex-wrap:wrap; gap:8px; justify-content:center; margin-bottom:24px; }
  .pkg { background:#0f172a; border:1px solid #334155; border-radius:6px; padding:4px 12px;
         font-size:12px; color:#93c5fd; font-family:monospace; }
  .log { background:#0f172a; border:1px solid #334155; border-radius:8px; padding:14px;
         font-family:monospace; font-size:11px; color:#64748b; text-align:left;
         max-height:180px; overflow-y:auto; line-height:1.7; white-space:pre-wrap; word-break:break-all; }
  .done-icon { font-size:48px; margin-bottom:16px; }
  .error { color:#f87171; }
</style>
</head>
<body>
<div class="card">
  <div id="state-loading">
    <div class="spinner"></div>
    <h1>正在安装必要插件…</h1>
    <p class="sub">首次运行需要安装以下依赖，安装完成后将自动启动</p>
    <div class="pkgs" id="pkg-list"></div>
    <div class="log" id="log-box">等待安装…</div>
  </div>
  <div id="state-done" style="display:none">
    <div class="done-icon">✅</div>
    <h1>插件安装完成</h1>
    <p class="sub">正在启动服务，请稍候…</p>
  </div>
  <div id="state-error" style="display:none">
    <div class="done-icon">⚠️</div>
    <h1 class="error">插件安装失败</h1>
    <p class="sub error" id="error-msg"></p>
    <div class="log" id="error-log" style="margin-top:16px"></div>
  </div>
</div>
<script>
  var pkgs = "__MISSING__".split(", ");
  var pkgList = document.getElementById("pkg-list");
  pkgs.forEach(function(p){ if(p){ var s=document.createElement("span"); s.className="pkg"; s.textContent=p; pkgList.appendChild(s); } });
  var logBox = document.getElementById("log-box");
  var timer = setInterval(function(){
    fetch("/install-status").then(r=>r.json()).then(function(st){
      if(st.lines && st.lines.length){ logBox.textContent = st.lines.slice(-30).join("\\n"); logBox.scrollTop = logBox.scrollHeight; }
      if(st.done){
        clearInterval(timer);
        document.getElementById("state-loading").style.display="none";
        document.getElementById("state-done").style.display="block";
        setTimeout(function(){ location.reload(); }, 4000);
      } else if(st.error){
        clearInterval(timer);
        document.getElementById("state-loading").style.display="none";
        document.getElementById("state-error").style.display="block";
        document.getElementById("error-msg").textContent = st.error;
        document.getElementById("error-log").textContent = st.lines.slice(-20).join("\\n");
      }
    }).catch(function(){});
  }, 1000);
</script>
</body>
</html>'''

def _check_and_install_deps():
    """检查必要依赖，缺失时启动临时页面显示安装进度，自动安装后重启"""
    if getattr(sys, 'frozen', False):
        return  # 打包版依赖已内置，跳过检查（frozen 环境无法执行 pip install）
    import importlib.util
    missing = []
    for mod_name, pkg_name in _REQUIRED_DEPS:
        try:
            if importlib.util.find_spec(mod_name) is None:
                missing.append(pkg_name)
        except (ImportError, ValueError, ModuleNotFoundError):
            missing.append(pkg_name)
    if not missing:
        return

    import http.server
    install_state = {"lines": [], "done": False, "error": ""}

    class _InstallHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/install-status':
                body = json.dumps(install_state, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                html = _INSTALL_PAGE_HTML.replace('__MISSING__', ', '.join(missing))
                body = html.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        def log_message(self, fmt, *args):
            pass

    # 启动临时 HTTP 服务显示安装进度页面
    tmp_server = None
    try:
        tmp_server = http.server.HTTPServer(('0.0.0.0', PORT), _InstallHandler)
        threading.Thread(target=tmp_server.serve_forever, daemon=True).start()
        import webbrowser
        def _open_install_page():
            time.sleep(0.8)
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
                s.close()
            except Exception:
                ip = "127.0.0.1"
            webbrowser.open(f"http://{ip}:{PORT}")
        threading.Thread(target=_open_install_page, daemon=True).start()
    except Exception:
        tmp_server = None

    # 执行 pip install
    try:
        proc = subprocess.Popen(
            [sys.executable, '-m', 'pip', 'install'] + missing,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1
        )
        for line in proc.stdout:
            install_state["lines"].append(line.rstrip())
        proc.wait()
        if proc.returncode == 0:
            install_state["done"] = True
        else:
            install_state["error"] = "安装失败，请检查网络后重试，或手动执行: pip install " + ' '.join(missing)
    except Exception as e:
        install_state["error"] = str(e)

    if install_state["error"]:
        # 安装失败：保持页面显示错误信息
        while True:
            time.sleep(60)

    # 安装成功：等待页面展示完成状态后重启应用
    time.sleep(3)
    if tmp_server:
        tmp_server.shutdown()
    time.sleep(1)
    if getattr(sys, 'frozen', False):
        subprocess.Popen([sys.executable])
    else:
        subprocess.Popen([sys.executable] + sys.argv)
    os._exit(0)

_check_and_install_deps()
from urllib.parse import urlparse
import requests as req_lib

from flask import Flask, send_from_directory, request, jsonify, make_response

# 源码模块（engine/runner/capture/db/recorder_tool 等）都在 src/ 下，顶部已加入 sys.path
_entry_dir = os.path.dirname(os.path.abspath(__file__))

# ── 项目根目录（数据持久化基址）──
# exe 模式下用 sys.executable 定位实际路径，避免 __file__ 指向临时目录
_IS_EXE = getattr(sys, 'frozen', False)
if _IS_EXE:
    _app_root = os.path.dirname(os.path.abspath(sys.executable))
else:
    _app_root = _entry_dir  # 入口 app.py 所在目录即项目根目录

# ── 静态资源解析：外部目录优先（放 exe 旁可随时改 HTML/JS），缺失回退打包内置资源 ──
def _resolve_asset(name):
    """templates/static 等资源目录：外部存在用外部，否则用包内（_MEIPASS）内置资源"""
    ext = os.path.join(_app_root, name)
    if os.path.isdir(ext):
        return ext
    _base = getattr(sys, "_MEIPASS", None)
    if _base:
        alt = os.path.join(_base, name)
        if os.path.isdir(alt):
            return alt
    return ext

app = Flask(__name__,
    template_folder=_resolve_asset("templates"),
    static_folder=_resolve_asset("static"),
    static_url_path="/static")
app.config['JSON_AS_ASCII'] = False  # jsonify 直接输出中文，不转 Unicode（Flask 2.x）
app.json.ensure_ascii = False  # Flask 3.x 中文编码配置
# jsonify 默认会按字典键名排序，会把变量列表打乱成字母序；关掉后保持插入顺序（先建在前、新增在后）
app.json.sort_keys = False
app.config['TEMPLATES_AUTO_RELOAD'] = True  # 模板改动即时生效，免重启（避免重启打断用户抓包）

# ── 用例文件存放目录（JSON 格式，随项目文件夹迁移）──
CASES_DIR = os.path.join(_app_root, "cases")
os.makedirs(CASES_DIR, exist_ok=True)

# 首次运行 exe 时：从内置的 cases/ 复制示例文件（内置资源位于 _MEIPASS）
if _IS_EXE:
    _builtin_cases = os.path.join(getattr(sys, "_MEIPASS", _entry_dir), "cases")
    if os.path.isdir(_builtin_cases):
        for _fname in os.listdir(_builtin_cases):
            if _fname.endswith(".json"):
                _old = os.path.join(_builtin_cases, _fname)
                _new = os.path.join(CASES_DIR, _fname)
                if not os.path.exists(_new):
                    try:
                        import shutil
                        shutil.copy2(_old, _new)
                    except Exception:
                        pass

# ── 持久化配置目录（随项目文件夹迁移）──
DATA_DIR = os.path.join(_app_root, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# 迁移旧数据：从 ~/.api-tester/ 和旧 cases/ 迁移到 data/（兼容旧版）
_old_data_sources = []
# 旧版 exe 的数据在 ~/.api-tester/
_home_data = os.path.join(os.path.expanduser("~"), ".api-tester")
if os.path.isdir(_home_data):
    _old_data_sources.append(_home_data)
# 旧版开发模式的数据在 cases/ 下
_old_cases_dir = os.path.join(_app_root, "cases")
if os.path.isdir(_old_cases_dir):
    _old_data_sources.append(_old_cases_dir)

for _src_dir in _old_data_sources:
    for _fname in ["_variables.json", "_workbench_todos.json", "_workbench_notes.json",
                   "_workbench_quick_notes.json",
                   "_transfer_save_dir.txt", "_device_names.json",
                   "_mock_proxy.json", "_remote_proxy.json"]:
        _old = os.path.join(_src_dir, _fname)
        _new = os.path.join(DATA_DIR, _fname)
        if os.path.exists(_old) and not os.path.exists(_new):
            try:
                import shutil
                shutil.copy2(_old, _new)
            except Exception:
                pass

# 防止路径遍历：校验文件名解析后仍在 CASES_DIR 内
def _safe_filepath(filename):
    """校验文件名安全，返回合法路径或 None"""
    if not filename:
        return None
    filepath = os.path.realpath(os.path.join(CASES_DIR, filename))
    if not filepath.startswith(os.path.realpath(CASES_DIR) + os.sep) and filepath != os.path.realpath(CASES_DIR):
        return None
    return filepath


def _load_doc(filepath):
    """加载用例文档（JSON），返回 dict；格式错误抛 json.JSONDecodeError"""
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def _dump_doc(filepath, data):
    """写回用例文档（JSON）：写前备份，ensure_ascii=False + 缩进 2"""
    _backup_file(filepath)  # 重写前备份原文件
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

# 获取本机 IP 地址
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as e:
        print(f"[IP] 获取本机 IP 失败，回退 127.0.0.1: {e}")
        return "127.0.0.1"

LOCAL_IP = get_local_ip()

# ═══════════════════════════════════════════════════
#  CA 证书管理（用于 HTTPS 拦截）
# ═══════════════════════════════════════════════════

CA_CERT_DIR = os.path.join(_app_root, "certs")
CA_CERT_FILE = os.path.join(CA_CERT_DIR, "api-tester-ca.crt")
CA_KEY_FILE = os.path.join(CA_CERT_DIR, "api-tester-ca-key.pem")

def _harden_private_path(path):
    """收紧私钥相关路径的 NTFS 权限：去掉继承，只留当前用户 / SYSTEM / Administrators。

    自签 CA 私钥是明文 PEM，默认继承程序目录的权限，同机任何账户拖走一份
    就能伪造任意网站的 HTTPS。这里只解决「本机最小暴露」：整个 certs 目录被
    拷到别的机器或网盘，仍需要靠不分享来保证。失败只记日志，不影响证书使用。
    """
    if not os.path.exists(path):
        return
    if os.name != 'nt':
        try:
            os.chmod(path, 0o700 if os.path.isdir(path) else 0o600)
        except Exception as e:
            print(f"[CA] 收紧权限失败（{path}）：{e}")
        return
    user = os.environ.get('USERNAME') or ''
    tail = ':(OI)(CI)F' if os.path.isdir(path) else ':F'
    # SYSTEM / Administrators 用 SID 写，中文系统的账户名本地化不影响匹配
    principals = ([user] if user else []) + ['*S-1-5-18', '*S-1-5-32-544']
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    # 先逐条授权，成功后再断继承：万一授权失败，至少不会把自己锁在目录外
    for p in principals:
        try:
            subprocess.run(['icacls', path, '/grant:r', p + tail],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=flags, timeout=10)
        except Exception:
            pass
    try:
        subprocess.run(['icacls', path, '/inheritance:r'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=flags, timeout=10)
    except Exception:
        pass

def _harden_certs_dir():
    """certs/ 整体加固：目录本身 + 证书 + 私钥 + mitm 注入副本目录（不存在则跳过）"""
    try:
        _harden_private_path(CA_CERT_DIR)
        for p in (CA_CERT_FILE, CA_KEY_FILE, os.path.join(CA_CERT_DIR, 'mitm')):
            _harden_private_path(p)
    except Exception as e:
        print(f"[CA] 收紧 certs 目录权限失败（不影响使用）：{e}")

def ensure_ca_cert():
    """确保 CA 证书存在，不存在则生成"""
    os.makedirs(CA_CERT_DIR, exist_ok=True)
    if not os.path.exists(CA_CERT_FILE) or not os.path.exists(CA_KEY_FILE):
        print("[CA] 生成自签名 CA 证书...")
        try:
            from cryptography import x509
            from cryptography.x509.oid import NameOID
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            import datetime
            
            # 生成私钥
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            
            # 生成 CA 证书
            subject = issuer = x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, "API Tester CA"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "API Tester"),
                x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
            ])
            
            cert = x509.CertificateBuilder().subject_name(
                subject
            ).issuer_name(
                issuer
            ).public_key(
                key.public_key()
            ).serial_number(
                x509.random_serial_number()
            ).not_valid_before(
                datetime.datetime.utcnow()
            ).not_valid_after(
                datetime.datetime.utcnow() + datetime.timedelta(days=3650)
            ).add_extension(
                x509.BasicConstraints(ca=True, path_length=None), critical=True
            ).add_extension(
                # 新版 OpenSSL 校验链时强制要求 CA 具备 keyUsage(keyCertSign)，缺失会报
                # "CA cert does not include key usage extension"
                x509.KeyUsage(
                    digital_signature=True, content_commitment=False,
                    key_encipherment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=True, crl_sign=True,
                    encipher_only=False, decipher_only=False,
                ), critical=True
            ).add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            ).sign(key, hashes.SHA256())
            
            # 保存证书和私钥
            with open(CA_CERT_FILE, "wb") as f:
                f.write(cert.public_bytes(serialization.Encoding.PEM))
            
            with open(CA_KEY_FILE, "wb") as f:
                f.write(key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.TraditionalOpenSSL,
                    encryption_algorithm=serialization.NoEncryption()
                ))
            
            print(f"[CA] CA 证书已生成：{CA_CERT_FILE}")
            _harden_certs_dir()
            return True
        except ImportError:
            print("[CA] cryptography 库未安装，请运行: pip install cryptography")
            return False
        except Exception as e:
            print(f"[CA] 生成 CA 证书失败：{e}")
            return False
    # 已存在：顺手再收紧一次（幂等）。从别的机器整目录拷过来的旧文件也在这里被修成当前用户可读
    _harden_certs_dir()
    return True

# 域名证书内存缓存：{domain: (cert_pem_bytes, key_pem_bytes)}，不写磁盘
# 上限 200 个域名（约 600KB），超出淘汰最早未命中的条目
_domain_cert_cache = {}
_DOMAIN_CERT_CACHE_MAX = 200

def generate_domain_cert(domain):
    """为指定域名生成证书（内存缓存，不落盘）
    
    返回 (cert_pem_bytes, key_pem_bytes)，调用方按需写临时文件。
    """
    # 域名合法性校验
    if not domain or not re.fullmatch(r'[A-Za-z0-9.-]+', domain):
        print(f"[CA] 非法域名，拒绝生成证书: {domain!r}")
        return None, None
    
    # 命中内存缓存，移到末尾标记为最近使用（LRU）
    if domain in _domain_cert_cache:
        entry = _domain_cert_cache.pop(domain)
        _domain_cert_cache[domain] = entry
        return entry
    
    # 懒加载：CA 不存在时先生成（仅在真正需要拦截 HTTPS 时才触发）
    if not os.path.exists(CA_CERT_FILE) or not os.path.exists(CA_KEY_FILE):
        if not ensure_ca_cert():
            return None, None
    
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime
        
        # 加载 CA 证书和私钥
        with open(CA_CERT_FILE, "rb") as f:
            ca_cert = x509.load_pem_x509_certificate(f.read())
        
        with open(CA_KEY_FILE, "rb") as f:
            ca_key = serialization.load_pem_private_key(f.read(), password=None)
        
        # 生成域名私钥
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        
        # 生成域名证书
        subject = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, domain),
        ])
        
        cert = x509.CertificateBuilder().subject_name(
            subject
        ).issuer_name(
            ca_cert.subject
        ).public_key(
            key.public_key()
        ).serial_number(
            x509.random_serial_number()
        ).not_valid_before(
            datetime.datetime.utcnow()
        ).not_valid_after(
            datetime.datetime.utcnow() + datetime.timedelta(days=365)
        ).add_extension(
            x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False
        ).add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        ).add_extension(
            # RFC 5280：由带 SKI 的 CA 签发的证书必须携带 AKI，否则 OpenSSL 严格校验报
            # "Missing Authority Key Identifier"
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        ).add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=True, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False,
            ), critical=True
        ).add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        ).sign(ca_key, hashes.SHA256())
        
        # 仅存内存，不写磁盘
        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        key_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        )
        
        _domain_cert_cache[domain] = (cert_pem, key_pem)
        # 超限淘汰最久未使用的条目（LRU）
        while len(_domain_cert_cache) > _DOMAIN_CERT_CACHE_MAX:
            _domain_cert_cache.pop(next(iter(_domain_cert_cache)))
        print(f"[CA] 域名证书已生成（内存缓存）: {domain}")
        return cert_pem, key_pem
    except Exception as e:
        print(f"[CA] 生成域名证书失败 ({domain}): {e}")
        return None, None


# ═══════════════════════════════════════════════════
#  全局错误处理
# ═══════════════════════════════════════════════════

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "接口不存在"}), 404


@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "请求方法不允许"}), 405


@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": "服务器内部错误"}), 500


# ═══════════════════════════════════════════════════
#  页面
# ═══════════════════════════════════════════════════

@app.route("/")
def index():
    resp = send_from_directory(_resolve_asset("templates"), "index.html")
    # 前端就一个 index.html，改完要立刻生效：不让浏览器缓存，免得刷新后还是旧页面
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(_resolve_asset("static"), "favicon.png", mimetype="image/png")


# ═══════════════════════════════════════════════════
#  API: 列出 cases/ 下的用例文件
# ═══════════════════════════════════════════════════

# 文件元信息缓存：{filename: (mtime, size, steps)}，文件变化后自动失效
_file_meta_cache = {}


@app.route("/api/files")
def list_files():
    """列出 cases/ 目录下的用例文件（含接口数量与最后修改时间，供文件选择器展示）

    返回:
      [{name, steps, mtime}, ...]
      steps — steps 段中的接口数量（解析失败为 0）
      mtime — 最后修改时间（Unix 秒，带亚秒小数）
    """
    items = []
    for f in sorted(f for f in os.listdir(CASES_DIR)
                    if f.endswith(".json") and not f.startswith("_")):
        path = os.path.join(CASES_DIR, f)
        try:
            st = os.stat(path)
        except OSError:
            continue
        cached = _file_meta_cache.get(f)
        if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
            steps = cached[2]
        else:
            try:
                doc = _load_doc(path)
                steps = len(doc.get("steps") or []) if isinstance(doc, dict) else 0
            except Exception:
                steps = 0
            _file_meta_cache[f] = (st.st_mtime, st.st_size, steps)
        # mtime 保留亚秒精度：前端用它判断磁盘内容是否已变（同秒内连改两次也能识别）
        items.append({"name": f, "steps": steps, "mtime": st.st_mtime})
    return jsonify(items)


# ═══════════════════════════════════════════════════
#  API: 删除用例文件
# ═══════════════════════════════════════════════════

@app.route("/api/delete-file", methods=["POST"])
def delete_file():
    """删除 cases/ 目录下的指定用例文件

    请求参数:
      filename — 要删除的文件名（如 test.json）

    返回:
      ok       — 是否成功
      filename — 已删除的文件名
    """
    filename = (request.get_json(silent=True) or {}).get("filename", "")
    if not filename:
        return jsonify({"ok": False, "error": "请输入文件名"}), 400

    filepath = _safe_filepath(filename)
    if not filepath or not os.path.exists(filepath):
        return jsonify({"ok": False, "error": "文件不存在或非法文件名"}), 404

    os.remove(filepath)
    return jsonify({"ok": True, "filename": filename})


# ═══════════════════════════════════════════════════
#  API: 新建用例文件（带模板）
# ═══════════════════════════════════════════════════

@app.route("/api/new-file", methods=["POST"])
def new_file():
    """在 cases/ 目录下新建一个带 steps 结构的用例文件

    请求参数:
      filename — 新文件名（如 myapi.json）

    返回:
      ok       — 是否成功
      filename — 新建的文件名
    """
    filename = (request.get_json(silent=True) or {}).get("filename", "")
    if not filename:
        return jsonify({"ok": False, "error": "请输入文件名"}), 400

    if not filename.endswith(".json"):
        filename += ".json"

    filepath = _safe_filepath(filename)
    if not filepath:
        return jsonify({"ok": False, "error": "非法文件名"}), 400
    if os.path.exists(filepath):
        return jsonify({"ok": False, "error": f"文件已存在: {filename}"}), 400

    template = {
        "name": "接口测试",
        "steps": [
            {
                "name": "Step1",
                "request": {
                    "url": "https://api.example.com/path",
                    "method": "GET",
                    "timeout": 15
                },
                "assert": {"status_code": 200}
            }
        ]
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(template, f, ensure_ascii=False, indent=2)
        f.write("\n")

    return jsonify({"ok": True, "filename": filename})


# ═══════════════════════════════════════════════════
#  API: 加载用例文件 → 返回步骤列表
# ═══════════════════════════════════════════════════

@app.route("/api/load")
def load_yaml_api():
    filename = request.args.get("file", "")
    filepath = _safe_filepath(filename)
    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": "文件不存在或非法文件名"}), 404

    try:
        data = _load_doc(filepath)
    except json.JSONDecodeError as e:
        return jsonify({"error": e.msg, "line": e.lineno, "col": e.colno}), 400

    if not isinstance(data, dict):
        return jsonify({"error": "JSON 格式不正确"}), 400

    name = data.get("name", "未命名")
    steps = []

    # 提取 setup 信息
    setup_info = {}
    if "setup" in data and isinstance(data["setup"], dict):
        setup_info = {k: str(v) for k, v in data["setup"].items()}

    if "steps" in data and isinstance(data["steps"], list):
        for i, step in enumerate(data["steps"], 1):
            if not isinstance(step, dict):
                continue
            req = step.get("request", {}) or {}
            steps.append({
                "index": i,
                "name": step.get("name", f"Step{i}"),
                "method": (req.get("method", "GET") if isinstance(req, dict) else "GET"),
                "url": (req.get("url", "") if isinstance(req, dict) else ""),
                "has_extract": bool(step.get("extract") or step.get("extract_list")),
                "has_mock": bool(step.get("mock")),
            })
    elif "request" in data:
        req = data["request"] or {}
        steps.append({
            "index": 1,
            "name": name,
            "method": req.get("method", "GET"),
            "url": req.get("url", ""),
            "has_extract": False,
            "has_mock": bool(data.get("mock")),
        })

    return jsonify({"name": name, "steps": steps, "total": len(steps), "setup": setup_info})


# ═══════════════════════════════════════════════════
#  API: curl → 请求表单（Postman 式解析填表）
# ═══════════════════════════════════════════════════

@app.route("/api/curl2request", methods=["POST"])
def curl_to_request_api():
    """把 curl 命令解析成 step 结构字典（不生成任何文本格式）

    请求参数:
      curl — curl 命令文本
      name — 步骤名称（可选）

    返回:
      ok       — 是否成功
      step     — 解析出的 step 字典（name/request/…）供表单填充
      warnings — 被忽略/需人工确认的选项列表
    """
    curl_text = (request.get_json(silent=True) or {}).get("curl", "")
    step_name = (request.get_json(silent=True) or {}).get("name", "").strip()
    import curl2yaml as C
    try:
        report = C.parse_curl_report(curl_text)
        if not report["url"]:
            return jsonify({"ok": False,
                            "error": "未识别到请求 URL，请检查 curl 命令（如 --url 后的地址是否完整）"})
        step = C.build_config(report["url"], report["method"], report["headers"],
                              report["body"], name=step_name or "接口测试",
                              timeout=report.get("timeout"))
        # warnings 一并回传：被忽略/需人工确认的项在界面上提示，不再静默漏参数
        return jsonify({"ok": True, "step": step, "warnings": report["warnings"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})



# ═══════════════════════════════════════════════════
#  API: 复制 step / 上下移动 step
# ═══════════════════════════════════════════════════

@app.route("/api/step/duplicate", methods=["POST"])
def duplicate_step():
    """复制指定 step 并插入到其后方"""
    filename = (request.get_json(silent=True) or {}).get("file", "")
    step_index = (request.get_json(silent=True) or {}).get("index", 0)  # 1-based

    filepath = _safe_filepath(filename)
    if not filepath or not os.path.exists(filepath):
        return jsonify({"ok": False, "error": f"文件不存在: {filename}"}), 404

    try:
        data = _load_doc(filepath)
    except json.JSONDecodeError as e:
        return jsonify({"ok": False, "error": f"JSON 格式错误: {e}"}), 400

    if not isinstance(data, dict) or "steps" not in data:
        return jsonify({"ok": False, "error": "文件没有 steps 列表"}), 400

    steps = data["steps"]
    if not (1 <= step_index <= len(steps)):
        return jsonify({"ok": False, "error": f"步骤序号越界: {step_index}"}), 400

    import copy
    original = steps[step_index - 1]
    cloned = copy.deepcopy(original)
    # 自动改名避免重名
    orig_name = cloned.get("name", f"Step{step_index}")
    cloned["name"] = orig_name + "_copy"
    steps.insert(step_index, cloned)  # 插入到原 step 后面
    data["steps"] = steps

    # 重写文件
    _dump_doc(filepath, data)
    return jsonify({"ok": True, "new_index": step_index + 1, "total": len(steps)})


@app.route("/api/step/move", methods=["POST"])
def move_step():
    """上下移动 step（direction: up / down）"""
    filename = (request.get_json(silent=True) or {}).get("file", "")
    step_index = (request.get_json(silent=True) or {}).get("index", 0)  # 1-based
    direction = (request.get_json(silent=True) or {}).get("direction", "")

    filepath = _safe_filepath(filename)
    if not filepath or not os.path.exists(filepath):
        return jsonify({"ok": False, "error": f"文件不存在: {filename}"}), 404

    try:
        data = _load_doc(filepath)
    except json.JSONDecodeError as e:
        return jsonify({"ok": False, "error": f"JSON 格式错误: {e}"}), 400

    if not isinstance(data, dict) or "steps" not in data:
        return jsonify({"ok": False, "error": "文件没有 steps 列表"}), 400

    steps = data["steps"]
    if not (1 <= step_index <= len(steps)):
        return jsonify({"ok": False, "error": f"步骤序号越界: {step_index}"}), 400

    i = step_index - 1  # 0-based
    if direction == "up" and i > 0:
        steps[i], steps[i - 1] = steps[i - 1], steps[i]
        new_index = step_index - 1
    elif direction == "down" and i < len(steps) - 1:
        steps[i], steps[i + 1] = steps[i + 1], steps[i]
        new_index = step_index + 1
    else:
        return jsonify({"ok": False, "error": f"无法{('上移' if direction == 'up' else '下移')}"}), 400

    data["steps"] = steps
    _dump_doc(filepath, data)
    return jsonify({"ok": True, "new_index": new_index, "total": len(steps)})


def _backup_file(filepath):
    """重写用例文件前备份：复制为同名 .bak（同目录，覆盖式）"""
    try:
        import shutil
        if os.path.exists(filepath):
            shutil.copy2(filepath, filepath + ".bak")
    except Exception as e:
        print(f"[Backup] 备份失败 {filepath}: {e}")


# ═══════════════════════════════════════════════════
#  API: 读取用例文件内容
# ═══════════════════════════════════════════════════

@app.route("/api/file-content")
def file_content():
    filename = request.args.get("file", "")
    filepath = _safe_filepath(filename)
    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": "文件不存在"}), 404
    with open(filepath, "r", encoding="utf-8") as f:
        return jsonify({"content": f.read(), "filename": filename})


# ═══════════════════════════════════════════════════
#  API: 返回整个用例文档结构（表单编辑器数据源）
# ═══════════════════════════════════════════════════

@app.route("/api/doc")
def api_doc():
    """返回整个用例文档的 dict（name/setup/steps 全量字段）

    返回:
      doc — 完整文档字典；/api/load 只给摘要，表单编辑用本接口
    """
    filename = request.args.get("file", "")
    filepath = _safe_filepath(filename)
    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": "文件不存在"}), 404
    try:
        data = _load_doc(filepath)
    except json.JSONDecodeError as e:
        return jsonify({"error": f"JSON 格式错误: {e}"}), 400
    if not isinstance(data, dict):
        return jsonify({"error": "文档结构不正确"}), 400
    return jsonify({"doc": data})


# ═══════════════════════════════════════════════════
#  API: 保存单个 step（JSON 字典，整文档读写）
# ═══════════════════════════════════════════════════

@app.route("/api/step/save", methods=["POST"])
def step_save():
    """把表单序列化的 step 字典保存到指定步骤（1-based）

    请求参数:
      file  — 文件名
      index — 步骤序号（1-based；0 表示追加新步骤）
      step  — 完整 step 字典（name/request/assert/extract/…）
    """
    payload = request.get_json(silent=True) or {}
    filename = payload.get("file", "")
    try:
        idx = int(payload.get("index", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "步骤序号无效，必须是整数"}), 400
    step = payload.get("step")

    if not filename:
        return jsonify({"ok": False, "error": "未指定文件"}), 400
    if not isinstance(step, dict):
        return jsonify({"ok": False, "error": "step 必须是 JSON 字典"}), 400

    filepath = _safe_filepath(filename)
    if not filepath:
        return jsonify({"ok": False, "error": "非法文件名"}), 400

    if not os.path.exists(filepath):
        # 文件不存在：直接以该 step 为唯一步骤新建
        doc = {"name": "接口测试", "steps": [step]}
        _dump_doc(filepath, doc)
        return jsonify({"ok": True, "index": 1, "total": 1, "created": True})

    try:
        data = _load_doc(filepath)
    except json.JSONDecodeError as e:
        return jsonify({"ok": False, "error": f"文件格式错误: {e}"}), 400

    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "文件格式不正确"}), 400
    if "steps" not in data or not isinstance(data.get("steps"), list):
        data["steps"] = []

    steps = data["steps"]
    if idx == 0:
        # 追加新步骤
        steps.append(step)
        new_index = len(steps)
    elif 1 <= idx <= len(steps):
        steps[idx - 1] = step
        new_index = idx
    else:
        return jsonify({"ok": False, "error": f"步骤 {idx} 不存在（共 {len(steps)} 步）"}), 404

    # 保存前结构校验（复用 yaml_checker 的结构规则，JSON 文档同样适用）
    # [WARN] 类提示（如重复步骤名）不阻塞保存，只拦真正的结构错误
    import yaml_checker as YC
    errors = [e for e in YC.check_config(data) if not e[1].startswith("[WARN]")]
    if errors:
        return jsonify({"ok": False, "error": "结构校验未通过",
                        "details": [{"location": loc, "problem": prob, "fix": fix}
                                    for loc, prob, fix in errors]}), 400

    _dump_doc(filepath, data)
    return jsonify({"ok": True, "index": new_index, "total": len(steps)})


# ═══════════════════════════════════════════════════
#  API: 保存整个用例文档（新建 / 保存 setup 等文档级字段）
# ═══════════════════════════════════════════════════

@app.route("/api/doc/save", methods=["POST"])
def doc_save():
    """把整个文档 dict 落盘（新建文件或更新文档级字段如 name/setup）

    请求参数:
      file — 文件名
      doc  — 完整文档字典
    """
    payload = request.get_json(silent=True) or {}
    filename = payload.get("file", "")
    doc = payload.get("doc")

    if not filename:
        return jsonify({"ok": False, "error": "未指定文件"}), 400
    if not isinstance(doc, dict):
        return jsonify({"ok": False, "error": "doc 必须是 JSON 字典"}), 400
    if not filename.endswith(".json"):
        filename += ".json"

    filepath = _safe_filepath(filename)
    if not filepath:
        return jsonify({"ok": False, "error": "非法文件名"}), 400

    import yaml_checker as YC
    errors = [e for e in YC.check_config(doc) if not e[1].startswith("[WARN]")]
    if errors:
        return jsonify({"ok": False, "error": "结构校验未通过",
                        "details": [{"location": loc, "problem": prob, "fix": fix}
                                    for loc, prob, fix in errors]}), 400

    _dump_doc(filepath, doc)
    return jsonify({"ok": True, "filename": filename})


# ═══════════════════════════════════════════════════
#  前置 / 后置脚本（Postman 风格 JS，跑在 Node 子进程里）
# ═══════════════════════════════════════════════════

def _copy_req(req_cfg):
    """浅拷贝请求配置（headers 也复制），避免前置脚本的改动漏到下一轮执行"""
    cfg = dict(req_cfg or {})
    if isinstance(cfg.get("headers"), dict):
        cfg["headers"] = dict(cfg["headers"])
    return cfg


def _exec_step_script(step, phase, variables, req_cfg=None, result=None):
    """执行 step 上的前置 / 后置脚本。

    参数:
      step      — 步骤字典（读 pre_script / post_script）
      phase     — 'pre' 或 'post'
      variables — 变量表，脚本里 pm.variables.set 的值就地写回
      req_cfg   — 请求配置；前置脚本改过的值通过返回值传下去
      result    — do_request 的结果；后置脚本用来读响应

    返回 (req_cfg, info)：info 为 None 表示这个接口没写脚本，
    否则是 {phase, logs, tests, error, elapsed_ms}。
    """
    key = "pre_script" if phase == "pre" else "post_script"
    code = step.get(key)
    if not isinstance(code, str) or not code.strip():
        return req_cfg, None

    import scripts as S

    request_payload = None
    if req_cfg is not None:
        request_payload = {
            "method": str(req_cfg.get("method") or "GET").upper(),
            "url": str(req_cfg.get("url") or ""),
            "headers": dict(req_cfg.get("headers") or {}),
            "params": req_cfg.get("params") or {},
            "body": req_cfg.get("body"),
            "timeout": req_cfg.get("timeout"),
        }
    response_payload = None
    if result is not None and not result.get("error"):
        response_payload = {
            "code": result.get("status_code"),
            "headers": result.get("resp_headers") or {},
            "body": result.get("resp_text") or "",
            "elapsed_ms": result.get("elapsed_ms") or 0,
        }

    r = S.run_script(phase, code, variables,
                     request=request_payload, response=response_payload,
                     name=str(step.get("name") or ""))
    info = {
        "phase": phase,
        "logs": r.get("logs") or [],
        "tests": r.get("tests") or [],
        "error": r.get("error") or "",
        "elapsed_ms": r.get("elapsed_ms") or 0,
    }
    if not r.get("ok"):
        return req_cfg, info

    # 变量回写：脚本里 set / unset 过的都生效，且保持同一个 dict 给后续步骤用
    new_vars = r.get("vars")
    if isinstance(new_vars, dict):
        variables.clear()
        variables.update({k: ("" if v is None else str(v)) for k, v in new_vars.items()})

    # 请求回写：只认脚本真的动过的字段
    new_req = r.get("request")
    if req_cfg is not None and isinstance(new_req, dict):
        cfg = _copy_req(req_cfg)
        headers = new_req.get("headers")
        if isinstance(headers, dict):
            cfg["headers"] = {str(k): v for k, v in headers.items() if k}
        if new_req.get("method"):
            cfg["method"] = str(new_req["method"]).upper()
        if isinstance(new_req.get("url"), str) and new_req["url"]:
            cfg["url"] = new_req["url"]
        if isinstance(new_req.get("params"), dict) and new_req["params"]:
            cfg["params"] = new_req["params"]
        if "body" in new_req:
            body = new_req.get("body")
            if body is not None:
                cfg["body"] = body
            else:
                cfg.pop("body", None)
        req_cfg = cfg
    return req_cfg, info


def _merge_script_tests(msgs, info, passed):
    """把后置脚本里的 pm.test 结果并进断言列表，返回新的 passed。"""
    if not info:
        return passed
    import engine as E
    for t in info.get("tests") or []:
        name = str(t.get("name") or "脚本断言")
        if t.get("passed"):
            msgs.append(E.P.ok("脚本 · " + name))
        else:
            msgs.append(E.P.fail("脚本 · " + name + " · " + str(t.get("message") or "")))
            passed = False
    if info.get("error"):
        msgs.append(E.P.fail("后置脚本出错 · " + str(info["error"])))
        passed = False
    return passed


def _script_log_lines(pre_info, post_info):
    """脚本 console 输出 → 控制台文本行（批量运行时跟着日志一起展示）"""
    lines = []
    for info in (pre_info, post_info):
        if not info:
            continue
        tag = "[前置脚本] " if info.get("phase") == "pre" else "[后置脚本] "
        for lg in info.get("logs") or []:
            lines.append(tag + str(lg.get("text") or ""))
        if info.get("error"):
            lines.append(tag + "[出错] " + str(info["error"]))
    return lines


# ═══════════════════════════════════════════════════
#  API: 发送单步请求（Postman 式即发即看，无需先保存）
# ═══════════════════════════════════════════════════

@app.route("/api/send", methods=["POST"])
def api_send():
    """把表单序列化的 step 直接发出去并返回响应详情

    请求参数:
      file — 文件名（可选；用于加载文档级 setup 与步骤级 setup）
      step — 完整 step 字典（request/assert/extract/extract_list/mock）

    返回:
      ok / status_code / elapsed_ms / resp_headers / resp_body / resp_json
      is_mock / seq_used / ds_used / asserts / assert_passed / extracted / error

    ds_used：本次单发用了哪行数据集合（{name, label, values}；没引用时为 None）
    """
    payload = request.get_json(silent=True) or {}
    step = payload.get("step")
    if not isinstance(step, dict):
        return jsonify({"ok": False, "error": "step 必须是 JSON 字典"}), 400
    req_cfg = step.get("request")
    if not isinstance(req_cfg, dict) or not req_cfg.get("url"):
        return jsonify({"ok": False, "error": "请填写请求 URL"}), 400

    import engine as E

    # 文档级 setup（文件存在时加载，不存在则跳过，不影响即发即看）
    setup_cfg = {}
    filename = payload.get("file", "")
    if filename:
        filepath = _safe_filepath(filename)
        if filepath and os.path.exists(filepath):
            try:
                doc = _load_doc(filepath)
                if isinstance(doc, dict) and isinstance(doc.get("setup"), dict):
                    setup_cfg = doc["setup"]
            except Exception:
                pass

    # 数据集合参数化：即发即看只发一次，用当前数据集的第一行（串联/批量执行才逐行展开）
    # 取值优先级与 /api/run 一致：提取 > 变量管理（环境/全局） > 数据集合，所以数据集先铺底
    variables = {}
    ds_used = None
    try:
        import dataset as DS
        ds_row = DS.first_row_for_steps([(1, step)])
    except Exception:
        ds_row = None
    if ds_row:
        ds_used = ds_row
        variables.update(ds_row.get("values") or {})

    variables.update(_load_variables_values())
    if setup_cfg:
        setup_vars, _ = E.eval_setup(setup_cfg)
        variables.update(setup_vars)
    if isinstance(step.get("setup"), dict):
        step_setup_vars, _ = E.eval_setup(step["setup"])
        variables.update(step_setup_vars)

    mock_cfg = step.get("mock")
    assert_cfg = step.get("assert") or {}
    extract = step.get("extract") or {}
    extract_list = step.get("extract_list") or {}

    # 前置脚本：能改 URL / 方法 / 请求头 / Body，也能写变量（写进 variables 供 ${} 替换）
    req_cfg, pre_info = _exec_step_script(step, "pre", variables, req_cfg=req_cfg)
    if pre_info and pre_info.get("error"):
        return jsonify({"ok": False, "error": "前置脚本出错：" + pre_info["error"],
                        "script": {"pre": pre_info, "post": None}})

    result = E.do_request(req_cfg, variables, mock=mock_cfg)
    if result["error"]:
        return jsonify({"ok": False, "error": result["error"],
                        "elapsed_ms": result["elapsed_ms"],
                        "script": {"pre": pre_info, "post": None}})

    passed, msgs = E.run_assert(assert_cfg, result["status_code"],
                                result["resp_text"], result["resp_json"],
                                elapsed_ms=result["elapsed_ms"],
                                resp_headers=result.get("resp_headers"))
    extracted = []
    extract_detail = {}
    if result["resp_json"] is not None:
        try:
            extracted = E.do_extract(extract, extract_list,
                                     result["resp_json"], variables, extract_detail)
        except Exception as e:
            extracted = [f"提取异常: {e}"]

    # 后置脚本：读响应、写变量、pm.test 的断言并进断言列表
    _, post_info = _exec_step_script(step, "post", variables, result=result)
    passed = _merge_script_tests(msgs, post_info, passed)

    return jsonify({
        "ok": True,
        "status_code": result["status_code"],
        "elapsed_ms": result["elapsed_ms"],
        "resp_headers": result.get("resp_headers") or {},
        "resp_body": result["resp_text"],
        "resp_json": result["resp_json"],
        "is_mock": result.get("is_mock", False),
        "seq_used": result.get("seq_used") or {},
        "latin1_headers": result.get("latin1_headers") or [],
        "resp_latin1_headers": result.get("resp_latin1_headers") or [],
        "binary_resp": result.get("binary_resp", False),
        "resp_ct": result.get("resp_ct") or "",
        "ds_used": ds_used,
        "asserts": msgs,
        "assert_passed": passed,
        "extracted": extracted,
        "extract_detail": extract_detail,
        "script": {"pre": pre_info, "post": post_info},
    })


# ═══════════════════════════════════════════════════
#  API: 运行测试
# ═══════════════════════════════════════════════════

@app.route("/api/run", methods=["POST"])
def run_test():
    body = (request.get_json(silent=True) or {})
    filename = body.get("file", "")
    step_filter = body.get("steps", "")
    try:
        loops = int(body.get("loops", 1))
    except (TypeError, ValueError):
        loops = 1
    loops = min(max(1, loops), 100)  # 限制上限 100，超出截断

    filepath = _safe_filepath(filename)
    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": "文件不存在或非法文件名"}), 404

    cfg = _load_doc(filepath)

    import engine as E

    steps = cfg.get("steps", [])

    # 单接口模式（无 steps）
    if not steps and "request" in cfg:
        import engine as E
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            success = E.run_single(cfg, "reports")
        req = cfg.get("request", {})
        return jsonify({
            "all_pass": success, "total": 1,
            "passed": 1 if success else 0, "failed": 0 if success else 1,
            "results": [{
                "step_index": 1, "name": cfg.get("name", "接口测试"),
                "method": req.get("method", "GET"), "url": req.get("url", ""),
                "passed": success, "status_code": "see above",
                "elapsed_ms": 0, "asserts": [], "extracted": [],
                "response": buf.getvalue(), "is_mock": False, "console": buf.getvalue(),
            }],
            "mode": "single"
        })

    if not steps:
        return jsonify({"error": "没有 steps 配置"}), 400

    # 解析步骤过滤
    filter_nums = None
    if step_filter:
        try:
            filter_nums = [int(s.strip()) for s in step_filter.split(",") if s.strip()]
        except ValueError:
            return jsonify({"error": "步骤格式错误，用数字和逗号，如 1,3"}), 400

    # 按用户输入顺序构建步骤列表（如输入 "5,3" 则先执行5再执行3）
    if filter_nums:
        ordered_steps = []
        for num in filter_nums:
            if 1 <= num <= len(steps):
                ordered_steps.append((num, steps[num - 1]))
    else:
        ordered_steps = list(enumerate(steps, 1))

    # ── 执行前置条件（setup）──
    setup_cfg = cfg.get("setup")
    setup_vars, setup_errors = E.eval_setup(setup_cfg)

    # 逐步执行，收集结果（循环在外层，步骤在内层，保证按轮次顺序）
    # 先加载预设变量（持久化存储），再合并 setup 变量（与流式模式一致）
    preset_vars = _load_variables_values()
    variables = dict(preset_vars)
    variables.update(setup_vars)
    results = []
    all_pass = True

    for loop_num in range(1, loops + 1):
        for idx, step in ordered_steps:

            step_name = step.get("name", f"Step{idx}")
            req_cfg = step.get("request", {})
            assert_cfg = step.get("assert", {})
            extract = step.get("extract", {})
            extract_list = step.get("extract_list", {})
            step_mock = step.get("mock")

            # 步骤级 setup（覆盖根级同名变量）
            step_setup_cfg = step.get("setup")
            if step_setup_cfg and isinstance(step_setup_cfg, dict):
                step_setup_vars, _ = E.eval_setup(step_setup_cfg)
                variables.update(step_setup_vars)

            # 捕获 printer 的终端输出
            buf = io.StringIO()
            passed = False
            record = {}

            with contextlib.redirect_stdout(buf):
                result = E.do_request(req_cfg, variables, mock=step_mock)

                # 入参快照在 do_request 之后取：自增变量在请求前才取号
                _resolved_cfg = E._replace_deep(req_cfg, variables or {})
                _req_body = _resolved_cfg.get("body")
                _req_params = _resolved_cfg.get("params")
                _req_headers = _resolved_cfg.get("headers") or {}

                if result["error"]:
                    E.P.print_error(result["error"])
                    record = {
                        "step_index": idx, "name": step_name,
                        "method": req_cfg.get("method", "GET"),
                        "url": E._replace(req_cfg.get("url", ""), variables),
                        "status_code": "ERR", "elapsed_ms": result["elapsed_ms"],
                        "passed": False, "asserts": [],
                        "response": result["error"], "extracted": [],
                        "is_mock": False,
                        "req_body": _req_body, "req_params": _req_params, "req_headers": _req_headers,
                    }
                else:
                    ok, msgs = E.run_assert(
                        assert_cfg, result["status_code"],
                        result["resp_text"], result["resp_json"],
                        elapsed_ms=result["elapsed_ms"],
                        resp_headers=result.get("resp_headers"))
                    passed = ok

                    resp_preview = ""
                    if result["resp_json"] is not None:
                        resp_preview = json.dumps(
                            result["resp_json"], ensure_ascii=False, indent=2, default=str)
                    else:
                        resp_preview = result["resp_text"] or ""

                    extracted = []
                    extract_detail = {}
                    if result["resp_json"] is not None:
                        extracted = E.do_extract(
                            extract, extract_list,
                            result["resp_json"], variables, extract_detail)

                    record = {
                        "step_index": idx, "name": step_name,
                        "method": req_cfg.get("method", "GET"),
                        "url": E._replace(req_cfg.get("url", ""), variables),
                        "status_code": result["status_code"],
                        "elapsed_ms": result["elapsed_ms"],
                        "passed": passed,
                        "asserts": [re.sub(r'\x1b\[[0-9;]*m', '', m) for m in msgs],
                        "response": resp_preview,
                        "extracted": extracted,
                        "extract_detail": extract_detail,
                        "is_mock": result.get("is_mock", False),
                        "seq_used": result.get("seq_used") or {},
                        "req_body": _req_body, "req_params": _req_params, "req_headers": _req_headers,
                    }

            if loops > 1:
                record["loop"] = loop_num

            if not passed:
                all_pass = False

            record["console"] = buf.getvalue()
            results.append(record)

    total_pass = sum(1 for r in results if r["passed"])
    return jsonify({
        "all_pass": all_pass,
        "total": len(results),
        "passed": total_pass,
        "failed": len(results) - total_pass,
        "results": results,
        "setup_vars": {k: str(v) for k, v in setup_vars.items()},
        "setup_errors": setup_errors,
    })




def _scan_var_refs(text):
    """扫描字符串/字典中所有 ${varName} 占位符，返回引用的变量名集合"""
    import re
    return set(re.findall(r'\$\{(\w+)\}', str(text)))


# ═══════════════════════════════════════════════════
#  API: 运行测试（SSE 流式返回）
# ═══════════════════════════════════════════════════

@app.route("/api/run-stream", methods=["POST"])
def run_test_stream():
    """流式返回每个步骤的执行结果（Server-Sent Events）

    请求参数:
      file  — YAML 文件名
      steps — 步骤过滤（如 "1,3"）
      loops — 循环次数

    返回:
      text/event-stream，每个步骤完成后立即发送 data: {json}，
      全部完成后发送 data: [DONE]
    """
    body = (request.get_json(silent=True) or {})
    filename = body.get("file", "")
    step_filter = body.get("steps", "")
    try:
        loops = int(body.get("loops", 1))
    except (TypeError, ValueError):
        loops = 1
    loops = min(max(1, loops), 100)  # 限制上限 100，超出截断

    filepath = _safe_filepath(filename)
    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": "文件不存在或非法文件名"}), 404

    cfg = _load_doc(filepath)

    import engine as E
    steps = cfg.get("steps", [])

    # 单接口模式：与 steps 分支一致的结构化记录（真实 status_code/elapsed_ms/asserts）
    if not steps and "request" in cfg:
        setup_cfg = cfg.get("setup")
        setup_vars, _setup_errors = E.eval_setup(setup_cfg)
        variables = dict(_load_variables_values())
        variables.update(setup_vars)

        req_cfg = cfg.get("request", {})
        assert_cfg = cfg.get("assert", {})
        extract = cfg.get("extract", {})
        extract_list = cfg.get("extract_list", {})
        mock_cfg = cfg.get("mock")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = E.do_request(req_cfg, variables, mock=mock_cfg)

            if result["error"]:
                E.P.print_error(result["error"])
                record = {
                    "step_index": 1, "name": cfg.get("name", "接口测试"),
                    "method": req_cfg.get("method", "GET"),
                    "url": E._replace(req_cfg.get("url", ""), variables),
                    "status_code": "ERR", "elapsed_ms": result["elapsed_ms"],
                    "passed": False, "asserts": [],
                    "response": result["error"], "extracted": [],
                    "is_mock": False,
                }
            else:
                ok, msgs = E.run_assert(
                    assert_cfg, result["status_code"],
                    result["resp_text"], result["resp_json"],
                    elapsed_ms=result["elapsed_ms"],
                    resp_headers=result.get("resp_headers"))

                resp_preview = ""
                if result["resp_json"] is not None:
                    resp_preview = json.dumps(
                        result["resp_json"], ensure_ascii=False, indent=2, default=str)
                else:
                    resp_preview = result["resp_text"] or ""

                extracted = []
                extract_detail = {}
                if result["resp_json"] is not None:
                    extracted = E.do_extract(
                        extract, extract_list,
                        result["resp_json"], variables, extract_detail)

                record = {
                    "step_index": 1, "name": cfg.get("name", "接口测试"),
                    "method": req_cfg.get("method", "GET"),
                    "url": E._replace(req_cfg.get("url", ""), variables),
                    "status_code": result["status_code"],
                    "elapsed_ms": result["elapsed_ms"],
                    "passed": ok,
                    "asserts": [re.sub(r'\x1b\[[0-9;]*m', '', m) for m in msgs],
                    "response": resp_preview,
                    "extracted": extracted,
                    "extract_detail": extract_detail,
                    "is_mock": result.get("is_mock", False),
                    "seq_used": result.get("seq_used") or {},
                    "latin1_headers": result.get("latin1_headers") or [],
                    "resp_headers": result.get("resp_headers") or {},
                    "resp_latin1_headers": result.get("resp_latin1_headers") or [],
                    "binary_resp": result.get("binary_resp", False),
                    "resp_ct": result.get("resp_ct") or "",
                }

        record["console"] = buf.getvalue()
        def gen_single():
            yield f"data: {json.dumps(record, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return app.response_class(gen_single(), mimetype="text/event-stream")

    if not steps:
        return jsonify({"error": "没有 steps 配置"}), 400

    # 解析步骤过滤
    filter_nums = None
    if step_filter:
        try:
            filter_nums = [int(s.strip()) for s in step_filter.split(",") if s.strip()]
        except ValueError:
            return jsonify({"error": "步骤格式错误，用数字和逗号，如 1,3"}), 400

    # 按用户输入顺序构建步骤列表（如输入 "5,3" 则先执行5再执行3）
    if filter_nums:
        ordered_steps = []
        for num in filter_nums:
            if 1 <= num <= len(steps):
                ordered_steps.append((num, steps[num - 1]))
    else:
        ordered_steps = list(enumerate(steps, 1))

    # 数据集合：当前数据集被这些步骤引用时，按行展开执行
    ds_rows = None
    try:
        import dataset as DS
        ds_rows = DS.rows_for_steps(ordered_steps)
    except Exception:
        ds_rows = None

    # 计算总请求数（按行参数化时每行都要跑完整串）
    total_requests = len(ordered_steps) * loops * (len(ds_rows) if ds_rows else 1)

    # 加载预设变量（持久化存储）
    preset_vars = _load_variables_values()

    # ── 执行前置条件（setup）──
    setup_cfg = cfg.get("setup")
    setup_vars, setup_errors = E.eval_setup(setup_cfg)

    def generate():
        nonlocal total_requests  # foreach 展开时动态修正总请求数
        base_vars = dict(preset_vars)  # 每行重置回这个基线
        base_vars.update(setup_vars)   # setup 变量覆盖预设同名变量
        variables = dict(base_vars)  # 当前行的工作变量（extract/步骤 setup 写入；行切换时重置）
        seq = 0  # 全局请求序号
        step_extracted_keys = set()  # 记录哪些变量是从中间步骤提取的
        fe_fixed_steps = set()  # 已修正过总请求数的 foreach 步骤
        # 行在外层、循环次数在内层：每行都从头跑完整串
        _plan_rows = [(ln, lb, rv)
                      for lb, rv in (ds_rows or [(None, None)])
                      for ln in range(1, loops + 1)]
        _last_row = object()

        # 发送 setup 信息（第一个事件）
        if setup_vars or setup_errors:
            setup_record = {
                "type": "setup",
                "setup_vars": {k: str(v) for k, v in setup_vars.items()},
                "setup_errors": setup_errors,
            }
            yield f"data: {json.dumps(setup_record, ensure_ascii=False)}\n\n"

        for loop_num, _row_label, _row_vars in _plan_rows:
            if _row_vars is not None and _row_label != _last_row:
                # 换行：先铺本行数据（优先级最低），再叠变量基线；之后步骤提取的值再覆盖
                # 取值优先级：当前接口提取 > 变量管理（环境/全局） > 数据集合
                _last_row = _row_label
                variables.clear()
                variables.update(_row_vars)
                variables.update(base_vars)
                step_extracted_keys.clear()
            for idx, step in ordered_steps:

                step_name = step.get("name", f"Step{idx}")
                req_cfg = step.get("request", {})
                assert_cfg = step.get("assert", {})
                extract = step.get("extract", {})
                extract_list = step.get("extract_list", {})
                step_mock = step.get("mock")

                # 步骤级 setup（覆盖根级同名变量）
                step_setup_cfg = step.get("setup")
                if step_setup_cfg and isinstance(step_setup_cfg, dict):
                    step_setup_vars, _ = E.eval_setup(step_setup_cfg)
                    variables.update(step_setup_vars)

                # ── foreach 数据驱动解析（用上游提取的数组逐条驱动本步骤）──
                fe_items, fe_item_var, fe_note, fe_error = E.parse_foreach(step.get("foreach"), variables)
                if fe_error:
                    seq += 1
                    err_record = {
                        "step_index": idx, "name": step_name,
                        "method": req_cfg.get("method", "GET"),
                        "url": E._replace(req_cfg.get("url", ""), variables),
                        "status_code": "ERR", "elapsed_ms": 0,
                        "passed": False, "asserts": [],
                        "response": f"foreach 配置异常：{fe_error}", "extracted": [],
                        "is_mock": False,
                        "req_body": "", "req_params": None, "req_headers": {},
                        "seq": seq, "total": total_requests,
                        "console": "", "foreach_error": fe_error,
                    }
                    yield f"data: {json.dumps(err_record, ensure_ascii=False)}\n\n"
                    continue
                if fe_items is not None and idx not in fe_fixed_steps:
                    fe_fixed_steps.add(idx)
                    # 动态修正总请求数：该步骤每轮由 1 次展开为 len(fe_items) 次
                    total_requests += (len(fe_items) - 1) * loops
                fe_iter = list(enumerate(fe_items, 1)) if fe_items is not None else [(None, None)]
                fe_scope = E.save_foreach_scope(fe_item_var, variables) if fe_items is not None else None

                for fe_i, fe_val in fe_iter:
                    if fe_items is not None:
                        E._inject_foreach(fe_item_var, fe_val, variables)

                    seq += 1

                    buf = io.StringIO()
                    passed = False
                    record = {}

                    # 本轮请求配置单独一份：前置脚本改过也只影响本次，不漏给下一条
                    cur_req = _copy_req(req_cfg)
                    pre_info = None
                    post_info = None

                    with contextlib.redirect_stdout(buf):
                        cur_req, pre_info = _exec_step_script(step, "pre", variables, req_cfg=cur_req)
                        _pre_err = (pre_info or {}).get("error") or ""
                        if _pre_err:
                            E.P.print_error("前置脚本出错：" + _pre_err)
                            result = {"ok": False, "status_code": None, "elapsed_ms": 0,
                                      "resp_text": "", "resp_json": None, "resp_headers": {},
                                      "error": "前置脚本出错：" + _pre_err,
                                      "is_mock": False, "seq_used": {}}
                        else:
                            result = E.do_request(cur_req, variables, mock=step_mock)

                        # 入参快照在 do_request 之后取：自增变量在请求前才取号
                        _resolved_cfg = E._replace_deep(cur_req, variables or {})
                        _req_body = _resolved_cfg.get("body")
                        _req_params = _resolved_cfg.get("params")
                        _req_headers = _resolved_cfg.get("headers") or {}

                        if result["error"]:
                            E.P.print_error(result["error"])
                            record = {
                                "step_index": idx, "name": step_name,
                                "method": cur_req.get("method", "GET"),
                                "url": E._replace(cur_req.get("url", ""), variables),
                                "status_code": "ERR", "elapsed_ms": result["elapsed_ms"],
                                "passed": False, "asserts": [],
                                "response": result["error"], "extracted": [],
                                "is_mock": False,
                                "req_body": _req_body, "req_params": _req_params, "req_headers": _req_headers,
                                "latin1_headers": result.get("latin1_headers") or [],
                            }
                        else:
                            ok, msgs = E.run_assert(
                                assert_cfg, result["status_code"],
                                result["resp_text"], result["resp_json"],
                                elapsed_ms=result["elapsed_ms"],
                                resp_headers=result.get("resp_headers"))
                            passed = ok

                            resp_preview = ""
                            if result["resp_json"] is not None:
                                resp_preview = json.dumps(
                                    result["resp_json"], ensure_ascii=False, indent=2, default=str)
                            else:
                                resp_preview = result["resp_text"] or ""

                            extracted = []
                            extract_detail = {}
                            if result["resp_json"] is not None:
                                extracted = E.do_extract(
                                    extract, extract_list,
                                    result["resp_json"], variables, extract_detail)
                                # 记录提取的变量名，供后续步骤区分来源
                                for var_name in list(extract.keys()) + list(extract_list.keys()):
                                    if var_name in variables:
                                        step_extracted_keys.add(var_name)

                            # 后置脚本：读响应、写变量，pm.test 的断言并进断言列表
                            _, post_info = _exec_step_script(step, "post", variables, result=result)
                            passed = _merge_script_tests(msgs, post_info, passed)

                            record = {
                                "step_index": idx, "name": step_name,
                                "method": cur_req.get("method", "GET"),
                                "url": E._replace(cur_req.get("url", ""), variables),
                                "status_code": result["status_code"],
                                "elapsed_ms": result["elapsed_ms"],
                                "passed": passed,
                                "asserts": [re.sub(r'\x1b\[[0-9;]*m', '', m) for m in msgs],
                                "response": resp_preview,
                                "extracted": extracted,
                                "extract_detail": extract_detail,
                                "is_mock": result.get("is_mock", False),
                                "seq_used": result.get("seq_used") or {},
                                "req_body": _req_body, "req_params": _req_params, "req_headers": _req_headers,
                                "latin1_headers": result.get("latin1_headers") or [],
                                "resp_headers": result.get("resp_headers") or {},
                                "resp_latin1_headers": result.get("resp_latin1_headers") or [],
                                "binary_resp": result.get("binary_resp", False),
                                "resp_ct": result.get("resp_ct") or "",
                            }

                    # 脚本产物：脚本日志与出错信息附在这条记录上（断言已并进 asserts）
                    if pre_info or post_info:
                        record["script"] = {"pre": pre_info, "post": post_info}
                        _slines = _script_log_lines(pre_info, post_info)
                        if _slines:
                            record["script_logs"] = _slines

                    # 扫的是前置脚本改过之后的请求，报出来的变量来源才准
                    refs_before = _scan_var_refs(str(cur_req))
                    used_preset = [k for k in refs_before if k in preset_vars]
                    used_from_step = [k for k in refs_before if k in step_extracted_keys and k not in preset_vars]

                    record["seq"] = seq
                    record["total"] = total_requests
                    if loops > 1:
                        record["loop"] = loop_num
                    if _row_label is not None:
                        record["param_row"] = _row_label
                        record["param_vars"] = dict(_row_vars or {})
                    if fe_items is not None:
                        record["foreach_index"] = fe_i
                        record["foreach_total"] = len(fe_items)
                        record["foreach_item"] = E._foreach_disp(fe_item_var, fe_val)[:200]

                    # 变量来源信息：仅展示当前步骤实际引用的预设变量和前步提取变量
                    record["preset_vars_used"] = used_preset
                    record["step_vars_used"] = used_from_step

                    record["console"] = buf.getvalue()
                    if record.get("script_logs"):
                        record["console"] = "\n".join(record["script_logs"]) + "\n" + record["console"]
                    # 未解析变量（YAML 引用顺序错误/变量名拼错时，URL 或请求体中会残留 ${xxx}）
                    record["unresolved"] = E.find_unresolved_refs({
                        "url": record.get("url", ""),
                        "body": _req_body, "params": _req_params, "headers": _req_headers,
                    })

                    yield f"data: {json.dumps(record, ensure_ascii=False)}\n\n"

                # 恢复 foreach 注入变量，避免污染后续步骤（如覆盖参数化行同名变量）
                if fe_scope is not None:
                    E.restore_foreach_scope(fe_scope, variables)

        yield "data: [DONE]\n\n"

    return app.response_class(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


# ═══════════════════════════════════════════════════
#  API: 删除 step
# ═══════════════════════════════════════════════════

@app.route("/api/step/delete", methods=["POST"])
def step_delete():
    filename = (request.get_json(silent=True) or {}).get("file", "")
    try:
        idx = int((request.get_json(silent=True) or {}).get("index", 0))
    except (TypeError, ValueError):
        return jsonify({"code": 1, "message": "步骤序号无效，必须是整数"}), 400

    filepath = _safe_filepath(filename)
    if not filepath or not os.path.exists(filepath):
        return jsonify({"ok": False, "error": "文件不存在"}), 404

    try:
        data = _load_doc(filepath)
    except json.JSONDecodeError as e:
        return jsonify({"ok": False, "error": f"文件格式错误: {e}"}), 400

    steps = data.get("steps", [])
    if idx < 1 or idx > len(steps):
        return jsonify({"ok": False, "error": f"步骤 {idx} 不存在"}), 404

    removed = steps.pop(idx - 1)
    removed_name = removed.get("name", f"Step{idx}") if isinstance(removed, dict) else f"Step{idx}"
    data["steps"] = steps

    _dump_doc(filepath, data)
    return jsonify({"ok": True, "removed": removed_name, "remaining": len(steps)})


# ═══════════════════════════════════════════════════
#  API: 新增 step（手动添加接口）
# ═══════════════════════════════════════════════════

@app.route("/api/step/add", methods=["POST"])
def step_add():
    """新增步骤到用例文件。
    优先接收完整 step 字典（JSON），兼容旧的 name/url/method/headers/body 字段组装。
    """
    payload = request.get_json(silent=True) or {}
    filename = payload.get("file", "")
    if not filename:
        return jsonify({"ok": False, "error": "未指定文件"}), 400

    filepath = _safe_filepath(filename)
    if not filepath:
        return jsonify({"ok": False, "error": "非法文件名"}), 400

    # ── 构造新 step ──
    step_dict = payload.get("step")
    if isinstance(step_dict, dict):
        new_step = step_dict
    else:
        # 从字段组装（兼容旧调用）
        step_name = payload.get("name", "").strip() or "新接口"
        url = payload.get("url", "").strip()
        method = payload.get("method", "GET").strip().upper()
        headers_raw = payload.get("headers", "").strip()
        body_raw = payload.get("body", "").strip()

        if not url:
            return jsonify({"ok": False, "error": "URL 不能为空"}), 400

        new_step = {"name": step_name, "request": {"url": url, "method": method}}

        # 解析 headers（支持多行 key: value 格式）
        if headers_raw:
            headers = {}
            for line in headers_raw.split("\n"):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip()] = v.strip()
            if headers:
                new_step["request"]["headers"] = headers

        # 解析 body（尝试 JSON，失败则作为字符串）
        if body_raw:
            try:
                new_step["request"]["body"] = json.loads(body_raw)
            except (json.JSONDecodeError, ValueError):
                new_step["request"]["body"] = body_raw

        new_step.setdefault("request", {})["timeout"] = 15
        new_step["assert"] = {"status_code": 200}

    # ── 写入文件 ──
    if not os.path.exists(filepath):
        doc = {"name": "接口测试", "steps": [new_step]}
        _dump_doc(filepath, doc)
        return jsonify({"ok": True, "step_num": 1, "total": 1, "filename": filename})

    try:
        data = _load_doc(filepath)
    except json.JSONDecodeError as e:
        return jsonify({"ok": False, "error": f"文件格式错误: {e}"}), 400

    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "文件格式不正确"}), 400

    # 确保有 steps 列表
    if "steps" not in data or not isinstance(data.get("steps"), list):
        data["steps"] = []

    data["steps"].append(new_step)
    _dump_doc(filepath, data)

    step_num = len(data["steps"])
    return jsonify({"ok": True, "step_num": step_num, "total": step_num, "filename": filename})


def find_key_paths(obj, target_key, prefix="", max_depth=15, results=None):
    """递归搜索 JSON 结构，返回匹配 key 的所有路径。
    遍历数组的每一个元素，保证所有兄弟节点上的匹配 key 都能被发现。"""
    if results is None:
        results = []
    if max_depth <= 0:
        return results
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            if k.lower() == target_key.lower():
                results.append(path)
            find_key_paths(v, target_key, path, max_depth - 1, results)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            find_key_paths(item, target_key, f"{prefix}[{i}]", max_depth - 1, results)
    return results


# ═══════════════════════════════════════════════════
#  API: 验证 extract 路径（运行请求后检查路径是否可取值）
# ═══════════════════════════════════════════════════

@app.route("/api/validate-extract", methods=["POST"])
def validate_extract():
    filename = (request.get_json(silent=True) or {}).get("file", "")
    try:
        idx = int((request.get_json(silent=True) or {}).get("index", 0))
    except (TypeError, ValueError):
        return jsonify({"code": 1, "message": "步骤序号无效，必须是整数"}), 400
    fields = (request.get_json(silent=True) or {}).get("fields", [])

    filepath = _safe_filepath(filename)
    if not filepath:
        return jsonify({"ok": False, "error": "非法文件名"}), 400
    try:
        data = _load_doc(filepath)
    except json.JSONDecodeError as e:
        return jsonify({"ok": False, "error": f"文件格式错误: {e}"}), 400

    steps = data.get("steps", [])
    if idx < 1 or idx > len(steps):
        return jsonify({"ok": False, "error": f"步骤 {idx} 不存在"}), 404

    step = steps[idx - 1]
    req_cfg = step.get("request", {})
    step_mock = step.get("mock")

    import engine as E
    # 传入预设变量（替代空 {}），保证 ${var} 占位符可被替换
    _vars = _load_variables_values()
    with contextlib.redirect_stdout(io.StringIO()):
        result = E.do_request(req_cfg, _vars, mock=step_mock)

    if result["error"]:
        return jsonify({"ok": False, "error": f"请求失败: {result['error']}"}), 400

    resp_json = result.get("resp_json")
    if resp_json is None:
        return jsonify({"ok": False, "error": "响应不是 JSON 格式"}), 400

    results = []
    for field_path in fields:
        val = E.get_by_path(resp_json, field_path)
        if val is not None:
            results.append({
                "path": field_path, "ok": True,
                "value": str(val)[:200]
            })
        else:
            last_key = field_path.split(".")[-1]
            suggestions = find_key_paths(resp_json, last_key)
            results.append({
                "path": field_path, "ok": False,
                "suggestions": suggestions[:10]
            })

    return jsonify({"ok": True, "results": results})


# ═══════════════════════════════════════════════════
#  API: 添加 extract 字段到 step
# ═══════════════════════════════════════════════════

@app.route("/api/add-extract", methods=["POST"])
def add_extract():
    filename = (request.get_json(silent=True) or {}).get("file", "")
    try:
        idx = int((request.get_json(silent=True) or {}).get("index", 0))
    except (TypeError, ValueError):
        return jsonify({"code": 1, "message": "步骤序号无效，必须是整数"}), 400
    fields = (request.get_json(silent=True) or {}).get("fields", [])

    filepath = _safe_filepath(filename)
    if not filepath:
        return jsonify({"ok": False, "error": "非法文件名"}), 400
    try:
        data = _load_doc(filepath)
    except json.JSONDecodeError as e:
        return jsonify({"ok": False, "error": f"文件格式错误: {e}"}), 400

    steps = data.get("steps", [])
    if idx < 1 or idx > len(steps):
        return jsonify({"ok": False, "error": f"步骤 {idx} 不存在"}), 404

    step = steps[idx - 1]
    if not isinstance(step, dict):
        return jsonify({"ok": False, "error": "step 格式异常"}), 400

    if "extract" not in step or not isinstance(step.get("extract"), dict):
        step["extract"] = {}

    for field_path in fields:
        # 用路径最后一段作为变量名
        var_name = field_path.split(".")[-1] if "." in field_path else field_path
        var_name = var_name.replace("[", "").replace("]", "")
        step["extract"][var_name] = field_path

    steps[idx - 1] = step
    _dump_doc(filepath, data)

    return jsonify({"ok": True, "fields_added": fields})


# ═══════════════════════════════════════════════════
#  API: 数组批量提取预览（运行请求后展示关联表格）
# ═══════════════════════════════════════════════════

@app.route("/api/preview-extract", methods=["POST"])
def preview_extract():
    """运行指定 step 的请求，从响应中按数组路径提取多字段，返回表格数据

    请求参数:
      file       — YAML 文件名
      index      — step 序号（从 1 开始）
      array_path — 数组的 JSON 路径，如 "result.hotelList"
      fields     — 要从每个数组元素中提取的字段名列表，如 ["sid", "hotelCode", "hotelName"]

    返回:
      ok      — 是否成功
      headers — 表头（即 fields）
      rows    — 二维数组，每行对应一个数组元素，每列对应一个字段
      total   — 数组总长度
    """
    filename = (request.get_json(silent=True) or {}).get("file", "")
    try:
        idx = int((request.get_json(silent=True) or {}).get("index", 0))
    except (TypeError, ValueError):
        return jsonify({"code": 1, "message": "步骤序号无效，必须是整数"}), 400
    array_path = (request.get_json(silent=True) or {}).get("array_path", "")
    fields = (request.get_json(silent=True) or {}).get("fields", [])

    filepath = _safe_filepath(filename)
    if not filepath:
        return jsonify({"ok": False, "error": "非法文件名"}), 400
    try:
        data = _load_doc(filepath)
    except json.JSONDecodeError as e:
        return jsonify({"ok": False, "error": f"文件格式错误: {e}"}), 400

    steps = data.get("steps", [])
    if idx < 1 or idx > len(steps):
        return jsonify({"ok": False, "error": f"步骤 {idx} 不存在"}), 404

    step = steps[idx - 1]
    req_cfg = step.get("request", {})
    step_mock = step.get("mock")

    import engine as E
    # 传入预设变量（替代空 {}），保证 ${var} 占位符可被替换
    _vars = _load_variables_values()
    with contextlib.redirect_stdout(io.StringIO()):
        result = E.do_request(req_cfg, _vars, mock=step_mock)

    if result["error"]:
        return jsonify({"ok": False, "error": f"请求失败: {result['error']}"}), 400

    resp_json = result.get("resp_json")
    if resp_json is None:
        return jsonify({"ok": False, "error": "响应不是 JSON 格式"}), 400

    # 取出数组（若路径含 [*] 通配符，使用合并模式遍历所有匹配数组）
    if array_path and '[*]' in array_path:
        arr = E.get_by_path_with_merge(resp_json, array_path)
    else:
        arr = E.get_by_path(resp_json, array_path) if array_path else resp_json
    if not isinstance(arr, list):
        # 给出建议：搜索响应中哪些路径是数组
        arr_suggestions = _find_array_paths(resp_json)
        return jsonify({
            "ok": False,
            "error": f"路径 '{array_path}' 不是数组（实际类型: {type(arr).__name__}）",
            "array_suggestions": [s[0] for s in arr_suggestions]  # 只返回结构路径
        }), 400

    # 逐行提取字段值
    rows = []
    for item in arr:
        if isinstance(item, dict):
            row = []
            for f in fields:
                val = item.get(f) if "." not in f else E.get_by_path(item, f)
                row.append(str(val) if val is not None else "")
            rows.append(row)
        else:
            rows.append([str(item)])

    return jsonify({
        "ok": True,
        "headers": fields,
        "rows": rows,
        "total": len(arr)
    })


def _match_array_name(struct_path, key, lname):
    """数组名匹配（lname 已小写）。两种写法：
      - 纯名称（address_list）：按 key 匹配，大小写不敏感
      - 带点路径（user_info.address_list 或 [*].user_info.address_list）：
        按结构路径匹配（[N] 归一为 [*]，取后缀）
    """
    if "." not in lname and "[" not in lname:
        return key.lower() == lname
    return struct_path == lname or struct_path.endswith("." + lname)


def _find_array_by_name(obj, name, prefix="", max_depth=30, results=None):
    """按 key 名称查找数组（默认深度 30，遍历所有数组元素）。
    对每个数组都完整遍历其所有元素，保证同名数组在所有兄弟节点上都能被发现。
    名称支持纯名称与带点路径（见 _match_array_name）。"""
    if results is None:
        results = []
    if max_depth <= 0:
        return results
    import re
    lname = (name or "").strip().lower()
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, list):
                struct = re.sub(r'\[\d+\]', '[*]', path).lower()
                if _match_array_name(struct, k, lname):
                    results.append(path)
            _find_array_by_name(v, name, path, max_depth - 1, results)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _find_array_by_name(item, name, f"{prefix}[{i}]", max_depth - 1, results)
    return results


def _find_array_by_name_deep(obj, name, prefix="", max_depth=50, results=None):
    """极端深度搜索按名称查找数组（仅用于检测浅层默认深度之外是否有遗漏）"""
    if results is None:
        results = []
    if max_depth <= 0:
        return results
    import re
    lname = (name or "").strip().lower()
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, list):
                struct = re.sub(r'\[\d+\]', '[*]', path).lower()
                if _match_array_name(struct, k, lname):
                    results.append(path)
            _find_array_by_name_deep(v, name, path, max_depth - 1, results)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _find_array_by_name_deep(item, name, f"{prefix}[{i}]", max_depth - 1, results)
    return results


def _find_array_paths(obj, prefix="", max_depth=30, results=None):
    """递归搜索 JSON 中所有数组类型的路径。
    遍历所有元素以发现完整路径并统计 count，然后按结构路径聚合：
    相同 schema 的兄弟数组合并为一条（如 data[*].orders），count 为元素总数。
    返回格式：[(结构路径, 总count, 示例具体路径, 示例路径列表), ...]"""
    if results is None:
        results = []
    if max_depth <= 0:
        return results
    # 第一步：收集所有原始 (path, count) 对
    raw = []
    _collect_arrays_raw(obj, prefix, max_depth, raw)
    # 第二步：按结构路径聚合
    return _aggregate_by_structure(raw)


def _collect_arrays_raw(obj, prefix, max_depth, raw):
    """收集所有数组的 (path, count) 原始数据，不做聚合。

    数组无论作为 dict 值还是数组元素（多维数组）都会收集，空数组也保留
    （count=0）：只以 dict 值为入口会把数组套数组的层级丢掉，例如
    {"a": [[1,2],[3,4]]} 的内层 a[0]、a[1] 永远发现不了。
    """
    if max_depth <= 0 or obj is None:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, list):
                raw.append((path, len(v)))
            _collect_arrays_raw(v, path, max_depth - 1, raw)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            item_path = f"{prefix}[{i}]"
            if isinstance(item, list):
                raw.append((item_path, len(item)))  # 多维数组的内层也收集
            _collect_arrays_raw(item, item_path, max_depth - 1, raw)


def _aggregate_by_structure(raw_pairs):
    """按结构路径聚合：将 data[0].orders、data[1].orders、… 合并为 data[*].orders。
    保留第一个具体路径作为示例，count 为所有兄弟数组的元素总数，
    examples 收集具体路径（最多 8 个，多维数组展开后避免示例标签爆炸）。"""
    import re
    aggregated = {}  # struct_path -> (total_count, example_path, examples_list)
    for path, count in raw_pairs:
        # 将所有 [N] 替换为 [*] 得到结构路径
        struct = re.sub(r'\[\d+\]', '[*]', path)
        if struct not in aggregated:
            aggregated[struct] = (count, path, [path])
        else:
            old_count, example, examples = aggregated[struct]
            if len(examples) < 8:
                examples.append(path)
            aggregated[struct] = (old_count + count, example, examples)
    return [(struct, total, example, examples) for struct, (total, example, examples) in aggregated.items()]


def _type_name(v):
    """JSON 值的人类可读类型名：bool/number/string/null/array，其余退回原生类型名"""
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if v is None:
        return "null"
    if isinstance(v, list):
        return "array"
    return type(v).__name__


def _clip_sample(v, limit=30):
    s = str(v)
    return s if len(s) <= limit else s[:limit] + "…"


def _summarize_structure(item, max_depth=8):
    """数组元素（dict）的嵌套结构摘要：递归展开嵌套对象/数组，生成层级列表。

    返回扁平列表 [(路径, 描述), ...]，描述形如：
      'object'           — 对象节点，路径可继续展开
      'array[3]'         — 数组节点，3 为元素数
      'array[0]·empty'   — 空数组节点
      'string=张三'       — 标量，示例值截断到 30 字符
    数组节点仅深入第一个元素（结构探针），避免大报文展开过深。
    """
    out = []

    def walk(obj, prefix, depth):
        if depth <= 0 or obj is None:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                p = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    out.append((p, "object"))
                    walk(v, p, depth - 1)
                elif isinstance(v, list):
                    n = len(v)
                    if n and isinstance(v[0], dict):
                        out.append((p, f"array[{n}]"))
                        walk(v[0], p + "[0]", depth - 1)
                    else:
                        out.append((p, f"array[{n}]" + (f"·{_type_name(v[0])}" if n else "·empty")))
                else:
                    out.append((p, f"{_type_name(v)}={_clip_sample(v)}"))
        elif isinstance(obj, list):
            for i, it in enumerate(obj):
                walk(it, f"{prefix}[{i}]", depth - 1)

    if isinstance(item, dict):
        walk(item, "", max_depth)
    return out


def _find_array_paths_deep(obj, prefix="", max_depth=50, results=None):
    """极端深度搜索数组路径（用于检测浅层默认深度之外是否仍有遗漏）。
    逻辑与 _find_array_paths 一致：收集所有原始路径后按结构聚合。"""
    if results is None:
        results = []
    raw = []
    _collect_arrays_raw(obj, prefix, max_depth, raw)
    return _aggregate_by_structure(raw)


# ═══════════════════════════════════════════════════
#  API: 探测响应中的数组路径（运行请求后返回所有数组路径 + 字段列表）
# ═══════════════════════════════════════════════════

@app.route("/api/detect-arrays", methods=["POST"])
def detect_arrays():
    """运行指定 step 的请求，探测响应中所有数组路径，返回路径 + 每项的字段名列表

    请求参数:
      file  — YAML 文件名
      index — step 序号（从 1 开始）

    返回:
      ok    — 是否成功
      arrays — 数组列表，每项含:
        path   — 数组路径（如 "result.hotelList"）
        count  — 数组长度
        fields — 第一个元素的字段名列表（若元素是 dict）
    """
    filename = (request.get_json(silent=True) or {}).get("file", "")
    try:
        idx = int((request.get_json(silent=True) or {}).get("index", 0))
    except (TypeError, ValueError):
        return jsonify({"code": 1, "message": "步骤序号无效，必须是整数"}), 400

    filepath = _safe_filepath(filename)
    if not filepath:
        return jsonify({"ok": False, "error": "非法文件名"}), 400
    try:
        data = _load_doc(filepath)
    except json.JSONDecodeError as e:
        return jsonify({"ok": False, "error": f"文件格式错误: {e}"}), 400

    steps = data.get("steps", [])
    if idx < 1 or idx > len(steps):
        return jsonify({"ok": False, "error": f"步骤 {idx} 不存在"}), 404

    step = steps[idx - 1]
    req_cfg = step.get("request", {})
    step_mock = step.get("mock")

    import engine as E
    # 传入预设变量（替代空 {}），保证 ${var} 占位符可被替换
    _vars = _load_variables_values()
    with contextlib.redirect_stdout(io.StringIO()):
        result = E.do_request(req_cfg, _vars, mock=step_mock)

    if result["error"]:
        return jsonify({"ok": False, "error": f"请求失败: {result['error']}"}), 400

    resp_json = result.get("resp_json")
    if resp_json is None:
        return jsonify({"ok": False, "error": "响应不是 JSON 格式"}), 400

    # 查找所有数组路径（按结构聚合）
    arr_paths = _find_array_paths(resp_json)  # [(struct_path, total_count, example_path, examples), ...]

    # 深度检查：是否还有更深层的数组被遗漏
    deep_arr_paths = _find_array_paths_deep(resp_json)
    deep_extra_structs = set(p[0] for p in deep_arr_paths) - set(p[0] for p in arr_paths)

    arrays = []
    for struct_path, total_count, example_path, examples in arr_paths:
        arr = E.get_by_path(resp_json, example_path)
        if not isinstance(arr, list):
            continue
        info = {"path": struct_path, "count": total_count, "fields": [], "example_path": example_path, "examples": examples}
        if arr and isinstance(arr[0], dict):
            info["fields"] = list(arr[0].keys())
            info["structure"] = _summarize_structure(arr[0])
        arrays.append(info)

    result_data = {"ok": True, "arrays": arrays, "total": len(arrays)}
    if deep_extra_structs:
        result_data["depth_exceeded"] = len(deep_extra_structs)
        result_data["deep_paths"] = list(deep_extra_structs)[:5]
    return jsonify(result_data)


# ═══════════════════════════════════════════════════
#  API: 直接从 JSON 文本探测数组路径（不依赖 YAML step）
# ═══════════════════════════════════════════════════

@app.route("/api/detect-arrays-json", methods=["POST"])
def detect_arrays_json():
    """接收原始 JSON 文本，探测其中所有数组路径，返回路径 + 字段列表

    请求参数:
      json_text  — 原始 JSON 字符串
      array_name — 可选，按名称搜索指定数组（最多3层，超过3层提示手动输入）

    返回:
      ok     — 是否成功
      arrays — 数组列表，每项含:
        path   — 数组路径（如 "data.hotelList"）
        count  — 数组长度
        fields — 第一个元素的字段名列表（若元素是 dict）
    """
    json_text = (request.get_json(silent=True) or {}).get("json_text", "").strip()
    array_name = (request.get_json(silent=True) or {}).get("array_name", "").strip()
    if not json_text:
        return jsonify({"ok": False, "error": "请输入 JSON 内容"}), 400

    # 智能解析：支持标准 JSON、裸数组、多对象报文片段
    resp_json = None
    parse_mode = "standard"
    try:
        resp_json = json.loads(json_text)
    except json.JSONDecodeError:
        # 尝试1：包成数组（处理 {…},{…} 裸多对象报文）
        try:
            resp_json = json.loads("[" + json_text.rstrip().rstrip(",") + "]")
            parse_mode = "wrapped_array"
        except json.JSONDecodeError:
            # 尝试2：最外层可能缺逗号，按 },{ 边界修复
            import re as _re
            fixed = _re.sub(r'}\s*\{', '},{', json_text)
            try:
                resp_json = json.loads("[" + fixed.rstrip().rstrip(",") + "]")
                parse_mode = "wrapped_array"
            except json.JSONDecodeError as e:
                return jsonify({"ok": False, "error": f"JSON 语法错误: {e}"}), 400

    import engine as E

    # ── 按名称搜索模式（遍历所有数组元素，深度 30）──
    if array_name:
        search_root = resp_json
        found_paths = _find_array_by_name(search_root, array_name)
        result_data = {"ok": True, "arrays": [], "total": 0,
                       "mode": parse_mode, "search_name": array_name}
        if found_paths:
            # 与全量模式一致：按结构路径聚合（[0].a → [*].a），
            # 预览时可一次取出该层级全部实例的数据
            raw_pairs = []
            for p in found_paths:
                arr = E.get_by_path(search_root, p)
                if isinstance(arr, list):
                    raw_pairs.append((p, len(arr)))
            arrays = []
            for struct_path, total_count, example_path, examples in _aggregate_by_structure(raw_pairs):
                arr = E.get_by_path(search_root, example_path)
                if isinstance(arr, list):
                    info = {"path": struct_path, "count": total_count, "fields": [],
                            "example_path": example_path, "examples": examples}
                    if arr and isinstance(arr[0], dict):
                        info["fields"] = list(arr[0].keys())
                        info["structure"] = _summarize_structure(arr[0])
                    arrays.append(info)
            result_data["arrays"] = arrays
            result_data["total"] = len(arrays)
        # 深层检测：是否还有更深层的匹配
        deep_found = _find_array_by_name_deep(search_root, array_name)
        deep_extra = [p for p in deep_found if p not in (found_paths if found_paths else [])]
        if deep_extra:
            result_data["depth_exceeded"] = len(deep_extra)
            result_data["deep_paths"] = deep_extra[:5]
        return jsonify(result_data)

    # ── 默认全量探测模式 ──
    # 顶层就是数组时，直接以根路径返回 + 探测元素内嵌套数组
    deep_extra = []
    if isinstance(resp_json, list):
        info = {"path": "[根数组]", "count": len(resp_json), "fields": []}
        if resp_json and isinstance(resp_json[0], dict):
            info["fields"] = list(resp_json[0].keys())
            info["structure"] = _summarize_structure(resp_json[0])
        arrays = [info]
        # 探测列表元素内部的嵌套数组：逐元素收集并加 [idx] 前缀，
        # 保证示例路径对应真实索引（收集到一起再统一加前缀会让示例路径
        # 全部变成最后一个索引的路径，出现 [2].xxx 重复展示的问题）
        prefixed_raw = []
        for idx, elem in enumerate(resp_json):
            if not isinstance(elem, dict):
                continue
            elem_raw = []
            _collect_arrays_raw(elem, '', 30, elem_raw)
            for (p, c) in elem_raw:
                prefixed_raw.append((f"[{idx}].{p}" if p else f"[{idx}]", c))
        aggregated = _aggregate_by_structure(prefixed_raw)
        for struct_path, total_count, example_path, examples in aggregated:
            sub_arr = E.get_by_path(resp_json, example_path)
            if isinstance(sub_arr, list):
                a_info = {"path": struct_path, "count": total_count, "fields": [], "example_path": example_path, "examples": examples}
                if sub_arr and isinstance(sub_arr[0], dict):
                    a_info["fields"] = list(sub_arr[0].keys())
                    a_info["structure"] = _summarize_structure(sub_arr[0])
                arrays.append(a_info)
        result_data = {"ok": True, "arrays": arrays, "total": len(arrays), "mode": parse_mode}
        return jsonify(result_data)

    # 标准对象：探测内部所有数组路径（已按结构聚合）
    arr_paths = _find_array_paths(resp_json)
    # 深度检查
    deep_arr_paths = _find_array_paths_deep(resp_json)
    deep_extra_structs = set(p[0] for p in deep_arr_paths) - set(p[0] for p in arr_paths)

    arrays = []
    for struct_path, total_count, example_path, examples in arr_paths:
        arr = E.get_by_path(resp_json, example_path)
        if not isinstance(arr, list):
            continue
        info = {"path": struct_path, "count": total_count, "fields": [], "example_path": example_path, "examples": examples}
        if arr and isinstance(arr[0], dict):
            info["fields"] = list(arr[0].keys())
            info["structure"] = _summarize_structure(arr[0])
        arrays.append(info)

    result_data = {"ok": True, "arrays": arrays, "total": len(arrays), "mode": parse_mode}
    if deep_extra_structs:
        result_data["depth_exceeded"] = len(deep_extra_structs)
        result_data["deep_paths"] = list(deep_extra_structs)[:5]
    return jsonify(result_data)


# ═══════════════════════════════════════════════════
#  API: 数组批量提取 — 写入 YAML
# ═══════════════════════════════════════════════════

@app.route("/api/add-extract-list", methods=["POST"])
def add_extract_list():
    """把数组批量提取配置写入 step 的 extract_list 字段

    请求参数:
      file       — YAML 文件名
      index      — step 序号
      array_path — 数组路径
      fields     — 提取字段列表
      var_name   — 变量名（存储整个数组）

    写入 YAML 格式:
      extract_list:
        变量名: array_path
      extract:
        字段名: array_path[0].字段名
    """
    filename = (request.get_json(silent=True) or {}).get("file", "")
    try:
        idx = int((request.get_json(silent=True) or {}).get("index", 0))
    except (TypeError, ValueError):
        return jsonify({"code": 1, "message": "步骤序号无效，必须是整数"}), 400
    array_path = (request.get_json(silent=True) or {}).get("array_path", "")
    fields = (request.get_json(silent=True) or {}).get("fields", [])
    var_name = (request.get_json(silent=True) or {}).get("var_name", "")

    filepath = _safe_filepath(filename)
    if not filepath:
        return jsonify({"ok": False, "error": "非法文件名"}), 400
    try:
        data = _load_doc(filepath)
    except json.JSONDecodeError as e:
        return jsonify({"ok": False, "error": f"文件格式错误: {e}"}), 400

    steps = data.get("steps", [])
    if idx < 1 or idx > len(steps):
        return jsonify({"ok": False, "error": f"步骤 {idx} 不存在"}), 404

    step = steps[idx - 1]
    if not isinstance(step, dict):
        return jsonify({"ok": False, "error": "step 格式异常"}), 400

    # 写入 extract_list（存整个数组）
    if var_name and array_path:
        if "extract_list" not in step or not isinstance(step.get("extract_list"), dict):
            step["extract_list"] = {}
        step["extract_list"][var_name] = array_path

    # 写入 extract（存每个字段的第 0 项路径，方便引用）
    if fields:
        if "extract" not in step or not isinstance(step.get("extract"), dict):
            step["extract"] = {}
        for f in fields:
            step["extract"][f] = f"{array_path}[0].{f}"

    steps[idx - 1] = step
    _dump_doc(filepath, data)

    saved_info = []
    if var_name:
        saved_info.append(f"extract_list.{var_name} = {array_path}")
    for f in fields:
        saved_info.append(f"extract.{f} = {array_path}[0].{f}")

    return jsonify({"ok": True, "saved": saved_info})


# ═══════════════════════════════════════════════════
#  API: Mock 数据生成
# ═══════════════════════════════════════════════════

@app.route("/api/gen-mock", methods=["POST"])
def gen_mock():
    """根据字段定义生成 mock 数据

    请求参数:
      fields — 字段列表，每项含 name(字段名)、type(类型)、custom(自定义值)
      count  — 生成条数

    返回:
      ok      — 是否成功
      records — 生成的记录列表
      count   — 记录数
    """
    import random
    import uuid as _uuid
    from datetime import datetime, timedelta

    fields = (request.get_json(silent=True) or {}).get("fields", [])
    try:
        count = min(int((request.get_json(silent=True) or {}).get("count", 5)), 100)  # 最多100条
    except (TypeError, ValueError):
        return jsonify({"code": 1, "message": "count 必须是整数"}), 400

    if not fields:
        return jsonify({"ok": False, "error": "请至少定义一个字段"}), 400

    # 数据生成器
    _SURNAMES = list("王李张刘陈杨黄赵周吴徐孙马胡朱郭何罗梁宋")
    _NAMES = ["伟", "芳", "娜", "敏", "静", "强", "磊", "洋", "艳", "勇",
              "军", "杰", "涛", "明", "超", "秀英", "丽", "婷", "雪", "建华"]
    _DOMAINS = ["qq.com", "163.com", "gmail.com", "outlook.com", "foxmail.com"]
    _CITIES = ["北京", "上海", "广州", "深圳", "成都", "杭州", "武汉", "南京", "重庆", "西安"]
    _STREETS = ["中山路", "人民路", "解放路", "建设路", "和平路", "文化路", "胜利路", "新华路"]
    _COMPANIES = ["科技有限公司", "信息技术有限公司", "网络科技公司", "软件开发公司",
                  "数据服务公司", "云计算公司", "智能科技公司", "互联网科技公司"]
    _COMPANY_PREFIX = ["华", "中", "新", "金", "天", "万", "恒", "达", "信", "瑞"]

    def gen_one(ftype, custom):
        """根据类型生成单个值"""
        if custom:
            return custom
        if ftype == "name":
            return random.choice(_SURNAMES) + random.choice(_NAMES)
        elif ftype == "phone":
            prefixes = ["138", "139", "158", "159", "186", "187", "135", "136", "152", "155"]
            return random.choice(prefixes) + "".join([str(random.randint(0, 9)) for _ in range(8)])
        elif ftype == "idcard":
            area = f"{random.randint(11, 65):02d}{random.randint(0, 20):02d}{random.randint(0, 10):02d}"
            year = random.randint(1970, 2005)
            month = random.randint(1, 12)
            day = random.randint(1, 28)
            seq = random.randint(100, 999)
            base = f"{area}{year}{month:02d}{day:02d}{seq}"
            return base + str(random.randint(0, 9))
        elif ftype == "email":
            name = "".join(random.choices("abcdefghijklmnopqrstuvwxyz", k=random.randint(5, 10)))
            return f"{name}@{random.choice(_DOMAINS)}"
        elif ftype == "address":
            return f"{random.choice(_CITIES)}市{random.choice(_STREETS)}{random.randint(1, 999)}号"
        elif ftype == "date":
            base = datetime(2023, 1, 1)
            delta = timedelta(days=random.randint(0, 900))
            return (base + delta).strftime("%Y-%m-%d")
        elif ftype == "number":
            return random.randint(1, 10000)
        elif ftype == "boolean":
            return random.choice([True, False])
        elif ftype == "uuid":
            return str(_uuid.uuid4())
        elif ftype == "company":
            return random.choice(_COMPANY_PREFIX) + random.choice(_COMPANY_PREFIX) + random.choice(_COMPANIES)
        else:  # text
            return f"文本{random.randint(1, 100)}"

    records = []
    for _ in range(count):
        record = {}
        for f in fields:
            record[f["name"]] = gen_one(f.get("type", "text"), f.get("custom", ""))
        records.append(record)

    return jsonify({"ok": True, "records": records, "count": count})


# ═══════════════════════════════════════════════════
#  API: 常用测试数据生成（造数据）
# ═══════════════════════════════════════════════════
# 数据池与算法内联固化（移植自 data-generator 技能的仿真规则）：身份证号带
# GB11643 校验位、银行卡号通过 Luhn、信用代码带 31 进制校验，生成结果可直接
# 用于校验类测试。本区块所需模块在此补齐（文件顶层未引入）。
import random
import string
import uuid
from datetime import timedelta

_GD_SURNAMES = [
    "王", "李", "张", "刘", "陈", "杨", "赵", "黄", "周", "吴",
    "徐", "孙", "胡", "朱", "高", "林", "何", "郭", "马", "罗",
    "梁", "宋", "郑", "谢", "韩", "唐", "冯", "于", "董", "萧",
    "程", "曹", "袁", "邓", "许", "傅", "沈", "曾", "彭", "吕",
    "苏", "卢", "蒋", "蔡", "贾", "丁", "魏", "薛", "叶", "阎",
    "余", "潘", "杜", "戴", "夏", "钟", "汪", "田", "任", "姜",
    "范", "方", "石", "姚", "谭", "廖", "邹", "熊", "金", "陆",
    "郝", "孔", "白", "崔", "康", "毛", "邱", "秦", "江", "史",
]

_GD_GIVEN_NAMES = [
    "伟", "芳", "娜", "秀英", "敏", "静", "丽", "强", "磊", "洋",
    "艳", "勇", "军", "杰", "娟", "涛", "明", "超", "秀兰", "霞",
    "平", "刚", "桂英", "文", "华", "慧", "建华", "玉兰", "建国", "建军",
    "丹", "志强", "秀珍", "建平", "美玲", "雪梅", "海燕", "晓明", "小红", "志伟",
    "雅琴", "佳", "俊", "婷", "鑫", "浩", "宇", "轩", "晨", "瑞",
    "思远", "子涵", "梓涵", "欣怡", "子轩", "浩然", "皓轩", "宇轩", "梓萱", "语桐",
    "雨泽", "博远", "博文", "天佑", "思源", "若曦", "诗涵", "梦琪", "一诺", "依诺",
]

_GD_PROVINCES_CITIES = {
    "北京市": ["东城区", "西城区", "朝阳区", "海淀区", "丰台区", "石景山区", "通州区", "大兴区"],
    "上海市": ["黄浦区", "徐汇区", "长宁区", "静安区", "普陀区", "虹口区", "杨浦区", "浦东新区"],
    "广东省": ["广州市", "深圳市", "珠海市", "汕头市", "佛山市", "东莞市", "中山市", "惠州市"],
    "浙江省": ["杭州市", "宁波市", "温州市", "嘉兴市", "湖州市", "绍兴市", "金华市", "台州市"],
    "江苏省": ["南京市", "苏州市", "无锡市", "常州市", "南通市", "扬州市", "徐州市", "盐城市"],
    "四川省": ["成都市", "绵阳市", "德阳市", "宜宾市", "泸州市", "达州市", "南充市", "乐山市"],
    "湖北省": ["武汉市", "宜昌市", "襄阳市", "荆州市", "黄冈市", "十堰市", "孝感市", "咸宁市"],
    "湖南省": ["长沙市", "株洲市", "湘潭市", "衡阳市", "岳阳市", "常德市", "郴州市", "邵阳市"],
    "山东省": ["济南市", "青岛市", "烟台市", "潍坊市", "临沂市", "济宁市", "淄博市", "威海市"],
    "河南省": ["郑州市", "洛阳市", "开封市", "南阳市", "新乡市", "安阳市", "许昌市", "焦作市"],
    "福建省": ["福州市", "厦门市", "泉州市", "漳州市", "莆田市", "宁德市", "龙岩市", "三明市"],
    "陕西省": ["西安市", "咸阳市", "宝鸡市", "渭南市", "汉中市", "延安市", "安康市", "商洛市"],
    "重庆市": ["渝中区", "江北区", "南岸区", "沙坪坝区", "九龙坡区", "渝北区", "巴南区", "北碚区"],
    "天津市": ["和平区", "河东区", "河西区", "南开区", "河北区", "红桥区", "滨海新区", "东丽区"],
}

_GD_COMPANY_PREFIXES = [
    "华", "中", "国", "新", "大", "天", "金", "恒", "鑫", "万",
    "龙", "盛", "宏", "博", "瑞", "信", "诚", "德", "泰", "安",
]
_GD_COMPANY_WORDS = [
    "达", "通", "创", "联", "远", "科", "和", "嘉", "源", "辉",
]
_GD_COMPANY_TYPES = ["科技有限公司", "信息技术有限公司", "网络科技有限公司", "贸易有限公司",
                    "实业有限公司", "投资有限公司", "咨询有限公司", "传媒有限公司",
                    "电子商务有限公司", "数据服务有限公司", "智能科技有限公司", "生物科技有限公司"]

_GD_POSITIONS = [
    "软件工程师", "产品经理", "项目经理", "UI设计师", "数据分析师",
    "运营专员", "市场经理", "销售总监", "人事主管", "财务经理",
    "前端开发", "后端开发", "测试工程师", "运维工程师", "架构师",
    "技术总监", "CTO", "CEO", "COO", "行政助理",
]

_GD_INDUSTRIES = [
    "互联网", "金融", "教育", "医疗", "电商", "游戏", "人工智能",
    "新能源", "房地产", "餐饮", "物流", "汽车", "制造业", "传媒",
    "旅游", "农业", "环保", "法律", "咨询", "广告",
]

_GD_STREETS = ["中山路", "解放路", "人民路", "建设路", "和平路", "长江路",
               "文化路", "新华路", "光明路", "幸福路", "学院路", "科技路"]

_GD_PHONE_PREFIXES = [
    "130", "131", "132", "133", "134", "135", "136", "137", "138", "139",
    "150", "151", "152", "153", "155", "156", "157", "158", "159",
    "170", "171", "172", "173", "175", "176", "177", "178",
    "180", "181", "182", "183", "184", "185", "186", "187", "188", "189",
    "191", "198", "199",
]

_GD_ID_AREAS = [
    "110101", "110102", "310101", "310104", "440103", "440305",
    "330102", "330106", "320102", "320505", "510104", "510107",
    "420102", "420106", "430102", "430104", "370102", "370202",
]

_GD_BANK_PREFIXES = [
    "622202", "622848", "622700", "621661", "622588", "621286",
    "622622", "622521", "622908", "621483", "622576", "621626",
]

_GD_EMAIL_DOMAINS = [
    "qq.com", "163.com", "126.com", "sina.com", "gmail.com",
    "outlook.com", "foxmail.com", "hotmail.com", "yeah.net", "139.com",
]


def _gd_name():
    return random.choice(_GD_SURNAMES) + random.choice(_GD_GIVEN_NAMES)


def _gd_gender():
    return random.choice(["男", "女"])


def _gd_age():
    return random.randint(18, 65)


def _gd_birthday(min_age=18, max_age=65):
    """生日：按年龄区间反推（按天数近似，避免 replace(year=) 在闰日抛异常）"""
    today = datetime.now()
    start = today - timedelta(days=365 * max_age + max_age // 4)  # 最早
    end = today - timedelta(days=365 * min_age)                   # 最晚
    span = max(0, (end - start).days)
    return (start + timedelta(days=random.randint(0, span))).strftime("%Y-%m-%d")


def _gd_phone():
    return random.choice(_GD_PHONE_PREFIXES) + "".join(str(random.randint(0, 9)) for _ in range(8))


def _gd_id_number():
    """身份证号：18 位，含 GB11643 校验位（出生日期 18-70 岁）"""
    area = random.choice(_GD_ID_AREAS)
    birthday = _gd_birthday(min_age=18, max_age=70).replace("-", "")
    seq = "".join(str(random.randint(0, 9)) for _ in range(3))
    base = area + birthday + seq
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    check = "10X98765432"[sum(int(base[i]) * weights[i] for i in range(17)) % 11]
    return base + check


def _gd_email():
    name = "".join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(6, 12)))
    return name + "@" + random.choice(_GD_EMAIL_DOMAINS)


def _gd_address():
    """完整地址：省+市(区)+街道门牌（数据池中直辖市的"城市"项即区）"""
    province = random.choice(list(_GD_PROVINCES_CITIES.keys()))
    city = random.choice(_GD_PROVINCES_CITIES[province])
    street = random.choice(_GD_STREETS)
    return (province + city + street + str(random.randint(1, 200)) + "号"
            + str(random.randint(1, 30)) + "栋" + str(random.randint(101, 2503)) + "室")


def _gd_province():
    return random.choice(list(_GD_PROVINCES_CITIES.keys()))


def _gd_city():
    province = random.choice(list(_GD_PROVINCES_CITIES.keys()))
    return random.choice(_GD_PROVINCES_CITIES[province])


def _gd_postcode():
    return str(random.randint(100000, 699999))


def _gd_company():
    return (random.choice(_GD_COMPANY_PREFIXES) + random.choice(_GD_COMPANY_WORDS)
            + random.choice(_GD_COMPANY_TYPES))


def _gd_position():
    return random.choice(_GD_POSITIONS)


def _gd_industry():
    return random.choice(_GD_INDUSTRIES)


def _gd_credit_code():
    """统一社会信用代码：18 位，含 31 进制校验位"""
    chars = "0123456789ABCDEFGHJKLMNPQRTUWXY"
    base = "".join(random.choices(chars, k=17))
    weights = [1, 3, 9, 27, 19, 3, 15, 19, 27, 19, 15, 3, 13, 17, 9, 7, 3]
    total = sum(chars.index(base[i]) * weights[i] for i in range(17))
    return base + chars[(31 - total % 31) % 31]


def _gd_bank_card():
    """银行卡号：16 位，含 Luhn 校验位"""
    prefix = random.choice(_GD_BANK_PREFIXES)
    base = prefix + "".join(str(random.randint(0, 9)) for _ in range(16 - len(prefix) - 1))
    digits = [int(d) for d in base]
    for i in range(len(digits) - 1, -1, -2):
        digits[i] *= 2
        if digits[i] > 9:
            digits[i] -= 9
    check = (10 - sum(digits) % 10) % 10
    return base + str(check)


def _gd_username():
    return random.choice(["user", "test", "admin", "demo", "dev"]) + str(random.randint(100, 99999))


def _gd_password():
    return "".join(random.choices(string.ascii_letters + string.digits + "!@#$%", k=random.randint(8, 16)))


def _gd_ip():
    return "%d.%d.%d.%d" % (random.randint(1, 223), random.randint(0, 255),
                            random.randint(0, 255), random.randint(1, 254))


def _gd_mac():
    return ":".join("%02X" % random.randint(0, 255) for _ in range(6))


def _gd_url():
    return "https://www." + "".join(random.choices(string.ascii_lowercase,
                                                      k=random.randint(5, 10))) \
        + "." + random.choice(["com", "cn", "net", "org", "io"])


def _gd_date():
    return (datetime.now() - timedelta(days=random.randint(0, 365 * 3))).strftime("%Y-%m-%d")


def _gd_datetime():
    dt = datetime.now() - timedelta(days=random.randint(0, 365 * 3), seconds=random.randint(0, 86400))
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _gd_timestamp():
    dt = datetime.now() - timedelta(days=random.randint(0, 365 * 3), seconds=random.randint(0, 86400))
    return int(dt.timestamp())


def _gd_uuid():
    return str(uuid.uuid4())


def _gd_order_id():
    return "DD" + datetime.now().strftime("%Y%m%d%H%M") + "".join(random.choices(string.digits, k=6))


def _gd_amount():
    return round(random.uniform(1, 99999), 2)


_GD_GENERATORS = {
    "姓名": _gd_name, "性别": _gd_gender, "年龄": _gd_age, "生日": _gd_birthday,
    "手机号": _gd_phone, "身份证号": _gd_id_number, "邮箱": _gd_email,
    "地址": _gd_address, "省份": _gd_province, "城市": _gd_city, "邮编": _gd_postcode,
    "公司名称": _gd_company, "职位": _gd_position, "行业": _gd_industry,
    "统一社会信用代码": _gd_credit_code, "银行卡号": _gd_bank_card,
    "用户名": _gd_username, "密码": _gd_password, "IP地址": _gd_ip,
    "MAC地址": _gd_mac, "URL": _gd_url,
    "日期": _gd_date, "时间": _gd_datetime, "时间戳": _gd_timestamp,
    "UUID": _gd_uuid, "订单号": _gd_order_id, "金额": _gd_amount,
}


@app.route("/api/gen-data", methods=["POST"])
def gen_data():
    """常用测试数据生成（造数据）

    请求参数:
      types — 数据类型列表（中文名，取值见 _GD_GENERATORS，按传入顺序作为列）
      count — 生成条数（1-500，默认 10）

    返回:
      ok / columns（列顺序）/ records / count
    """
    data = request.get_json(silent=True) or {}
    types = data.get("types") or []
    if not isinstance(types, list) or not types:
        return jsonify({"ok": False, "error": "请至少选择一种数据类型"}), 400
    cols, seen = [], set()
    for t in types:
        t = str(t)
        if t in seen:
            continue
        if t not in _GD_GENERATORS:
            return jsonify({"ok": False, "error": "不支持的数据类型: %s" % t}), 400
        seen.add(t)
        cols.append(t)
    try:
        count = max(1, min(int(data.get("count", 10)), 500))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "生成条数必须是整数"}), 400
    records = [{c: _GD_GENERATORS[c]() for c in cols} for _ in range(count)]
    return jsonify({"ok": True, "columns": cols, "records": records, "count": count})


# ═══════════════════════════════════════════════════
#  API: 将 mock 数据写入指定 step
# ═══════════════════════════════════════════════════

@app.route("/api/add-mock-to-step", methods=["POST"])
def add_mock_to_step():
    """把生成的 mock 数据写入指定 YAML 文件的指定 step

    请求参数:
      file   — YAML 文件名
      index  — step 序号（从 1 开始）
      mock   — mock 配置字典，含 status_code 和 response

    写入位置:
      step 的 mock 字段，与 request / assert 同级
    """
    filename = (request.get_json(silent=True) or {}).get("file", "")
    try:
        idx = int((request.get_json(silent=True) or {}).get("index", 0))
    except (TypeError, ValueError):
        return jsonify({"code": 1, "message": "步骤序号无效，必须是整数"}), 400
    mock_data = (request.get_json(silent=True) or {}).get("mock")

    if not filename:
        return jsonify({"ok": False, "error": "请选择文件"}), 400
    if not mock_data:
        return jsonify({"ok": False, "error": "没有 mock 数据"}), 400

    filepath = _safe_filepath(filename)
    if not filepath or not os.path.exists(filepath):
        return jsonify({"ok": False, "error": f"文件不存在: {filename}"}), 404

    try:
        data = _load_doc(filepath)
    except json.JSONDecodeError as e:
        return jsonify({"ok": False, "error": f"文件格式错误: {e}"}), 400

    steps = data.get("steps", [])
    if idx < 1 or idx > len(steps):
        return jsonify({"ok": False, "error": f"步骤 {idx} 不存在"}), 404

    step = steps[idx - 1]
    if not isinstance(step, dict):
        return jsonify({"ok": False, "error": "step 格式异常"}), 400

    step["mock"] = mock_data
    steps[idx - 1] = step
    _dump_doc(filepath, data)

    step_name = step.get("name", f"Step{idx}")
    return jsonify({"ok": True, "filename": filename, "step_index": idx, "step_name": step_name})


# ═══════════════════════════════════════════════════
#  API: 报文仿写 Mock —— 根据原始 JSON 智能生成同类数据
# ═══════════════════════════════════════════════════

@app.route("/api/imitate-mock", methods=["POST"])
def imitate_mock():
    """根据用户粘贴的原始 JSON 报文，分析结构并仿写生成同类 mock 数据

    请求参数:
      template — 原始 JSON 报文（对象或数组）
      count    — 若顶层是数组，生成多少条（默认与原数组等长）

    智能类型推断规则（按优先级）:
      1. 字段名关键词: name→姓名, phone/mobile→手机, email→邮箱, address/addr→地址, idcard/id_card→身份证, date/time→日期, uuid→UUID, company→公司
      2. 值模式: 纯数字→同范围随机数, 日期格式→随机日期, UUID→随机UUID, bool→随机bool
      3. 字段值原样保留无法推断时
    """
    import random
    import uuid as _uuid
    from datetime import datetime, timedelta
    import re as _re

    template = (request.get_json(silent=True) or {}).get("template")
    count = (request.get_json(silent=True) or {}).get("count", 0)  # 0 = 保持原样

    if template is None:
        return jsonify({"ok": False, "error": "请粘贴 JSON 报文"}), 400

    # ── 数据生成器（同 gen-mock） ──
    _SURNAMES = list("王李张刘陈杨黄赵周吴徐孙马胡朱郭何罗梁宋")
    _NAMES = ["伟", "芳", "娜", "敏", "静", "强", "磊", "洋", "艳", "勇",
              "军", "杰", "涛", "明", "超", "秀英", "丽", "婷", "雪", "建华"]
    _DOMAINS = ["qq.com", "163.com", "gmail.com", "outlook.com", "foxmail.com"]
    _CITIES = ["北京", "上海", "广州", "深圳", "成都", "杭州", "武汉", "南京", "重庆", "西安"]
    _STREETS = ["中山路", "人民路", "解放路", "建设路", "和平路", "文化路", "胜利路", "新华路"]
    _COMPANIES = ["科技有限公司", "信息技术有限公司", "网络科技公司", "软件开发公司",
                  "数据服务公司", "云计算公司", "智能科技公司", "互联网科技公司"]
    _COMPANY_PREFIX = ["华", "中", "新", "金", "天", "万", "恒", "达", "信", "瑞"]

    # ── 字段名关键词 → 类型 ──
    _NAME_KEYWORDS = {
        "name": "name", "姓名": "name", "realname": "name", "real_name": "name",
        "username": "name", "user_name": "name", "nick": "name", "nickname": "name",
        "phone": "phone", "mobile": "phone", "手机": "phone", "电话": "phone",
        "tel": "phone", "telephone": "phone", "contact_phone": "phone",
        "email": "email", "邮箱": "email", "mail": "email",
        "address": "address", "addr": "address", "地址": "address",
        "idcard": "idcard", "id_card": "idcard", "身份证": "idcard",
        "company": "company", "公司": "company", "corp": "company",
        "uuid": "uuid",
    }

    _DATE_RE = _re.compile(r'^\d{4}[-/]\d{1,2}[-/]\d{1,2}')
    _UUID_RE = _re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')

    def infer_type(key, value):
        """根据字段名和值推断生成类型"""
        k = key.lower().strip()
        orig = key
        # 字段名匹配（多种边界检测避免误匹配）
        for kw, tp in _NAME_KEYWORDS.items():
            # 1. 完全匹配: "name", "phone"
            if k == kw:
                return tp
            # 2. _ 边界: "user_name", "phone_number", "contact_phone"
            if f"_{kw}" in k or f"{kw}_" in k:
                return tp
            # 3. 驼峰边界: "hotelName"→Name, "contactPhone"→Phone
            cap_kw = kw[0].upper() + kw[1:]
            if cap_kw in orig:
                return tp
            # 4. 后缀匹配: "userName"→name, "hotelEmail"→email
            if len(kw) >= 3 and k.endswith(kw) and len(k) > len(kw):
                return tp
        # 值模式匹配
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, (int, float)):
            return "number"
        if isinstance(value, str):
            if _UUID_RE.match(value):
                return "uuid"
            if _DATE_RE.match(value):
                return "date"
            # 纯数字字符串
            if value.isdigit() and len(value) >= 6:
                return "phone" if len(value) >= 11 else "number_str"
        return None  # 无法推断

    def gen_value(key, orig_value, vtype=None):
        """根据类型生成新值"""
        if vtype is None:
            vtype = infer_type(key, orig_value)

        if vtype == "name":
            return random.choice(_SURNAMES) + random.choice(_NAMES)
        elif vtype == "phone":
            prefixes = ["138", "139", "158", "159", "186", "187", "135", "136", "152", "155"]
            return random.choice(prefixes) + "".join([str(random.randint(0, 9)) for _ in range(8)])
        elif vtype == "email":
            n = "".join(random.choices("abcdefghijklmnopqrstuvwxyz", k=random.randint(5, 10)))
            return f"{n}@{random.choice(_DOMAINS)}"
        elif vtype == "address":
            return f"{random.choice(_CITIES)}市{random.choice(_STREETS)}{random.randint(1, 999)}号"
        elif vtype == "idcard":
            area = f"{random.randint(11, 65):02d}{random.randint(0, 20):02d}{random.randint(0, 10):02d}"
            year = random.randint(1970, 2005)
            month = random.randint(1, 12)
            day = random.randint(1, 28)
            seq = random.randint(100, 999)
            base = f"{area}{year}{month:02d}{day:02d}{seq}"
            return base + str(random.randint(0, 9))
        elif vtype == "company":
            return random.choice(_COMPANY_PREFIX) + random.choice(_COMPANY_PREFIX) + random.choice(_COMPANIES)
        elif vtype == "uuid":
            return str(_uuid.uuid4())
        elif vtype == "date":
            base = datetime(2023, 1, 1)
            delta = timedelta(days=random.randint(0, 900))
            # 保持原始格式
            fmt = "%Y-%m-%d %H:%M:%S" if " " in str(orig_value) else "%Y-%m-%d"
            if "/" in str(orig_value):
                fmt = fmt.replace("-", "/")
            return (base + delta).strftime(fmt)
        elif vtype == "boolean":
            return random.choice([True, False])
        elif vtype == "number":
            # 保持同类数值范围
            if isinstance(orig_value, int):
                mag = max(1, abs(orig_value))
                return random.randint(1, mag * 3)
            return round(random.uniform(0.01, float(orig_value) * 3), 2)
        elif vtype == "number_str":
            # 保持字符串数字的位数
            length = len(str(orig_value))
            return "".join([str(random.randint(0, 9)) for _ in range(length)])
        # 无法推断 → 原样返回
        return orig_value

    def imitate(obj, parent_key=""):
        """递归仿写 JSON 结构"""
        if isinstance(obj, dict):
            result = {}
            for k, v in obj.items():
                result[k] = imitate(v, parent_key=k)
            return result
        elif isinstance(obj, list):
            if not obj:
                return []
            # 对数组中的每个元素仿写
            n = count if count > 0 else len(obj)
            return [imitate(obj[i % len(obj)], parent_key=parent_key) for i in range(n)]
        else:
            # 原子值
            return gen_value(parent_key, obj)

    try:
        result = imitate(template)
    except Exception as e:
        return jsonify({"ok": False, "error": f"仿写失败: {e}"}), 400

    return jsonify({"ok": True, "result": result})


# ═══════════════════════════════════════════════════
#  API: 批量参数化 — 解析 Excel 预览
# ═══════════════════════════════════════════════════

# 批量参数化临时缓存（session_id -> {headers, rows, filename, ts}）
_batch_sessions = {}
_batch_sessions_lock = threading.Lock()
_BATCH_SESSION_TTL = 1800   # 会话有效期：30 分钟
_BATCH_SESSION_MAX = 50     # 会话总数上限


def _put_batch_session(session_id, data):
    """写入批量参数会话：惰性清理超时会话，超出上限时淘汰最旧"""
    with _batch_sessions_lock:
        now = time.time()
        expired = [k for k, v in _batch_sessions.items()
                   if now - v.get("ts", 0) > _BATCH_SESSION_TTL]
        for k in expired:
            del _batch_sessions[k]
        while len(_batch_sessions) >= _BATCH_SESSION_MAX and _batch_sessions:
            oldest = min(_batch_sessions.items(), key=lambda kv: kv[1].get("ts", 0))[0]
            del _batch_sessions[oldest]
        data["ts"] = now
        _batch_sessions[session_id] = data


def _get_batch_session(session_id):
    """读取批量参数会话，过期自动失效"""
    with _batch_sessions_lock:
        s = _batch_sessions.get(session_id)
        if s is None:
            return None
        if time.time() - s.get("ts", 0) > _BATCH_SESSION_TTL:
            del _batch_sessions[session_id]
            return None
        return s

def _cell_to_display_str(cell):
    """openpyxl 单元格 → Excel 显示一致的字符串（保留给旧调用方，实现统一在 engine）"""
    import engine as E
    return E._excel_cell_display(cell)


def _parse_excel_file(file):
    """解析 Excel 文件（支持 .xlsx 和 .xls），返回 (headers, all_rows) 或抛出异常
    单元格值按 Excel 显示效果转为字符串；实现统一委托 engine.parse_excel_rows，
    与定时任务参数化的读取行为保持一致。
    """
    import engine as E
    return E.parse_excel_rows(file, file.filename or "")


# ── 入参类型智能识别（上传后自动预选，仅高置信度场景） ──

_DATE_STR_RE = re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$|^\d{4}年\d{1,2}月\d{1,2}日$")
_DATETIME_STR_RE = re.compile(
    r"^(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日)[ T]\d{1,2}:\d{1,2}(?::\d{1,2})?$")
_DATE_WORD_RE = re.compile(r"date|日期|time|时间|day|(?<![a-z])dt(?![a-z])", re.IGNORECASE)


def _detect_param_type(col_name, values):
    """根据整列数据智能识别建议的入参类型（仅高置信度时返回，其余返回 None 表示保持字符串原样传递）：
    - 整列值均为日期写法（2024-01-01 / 2024/1/1 / 2024年1月1日）→ "date"；含时间部分 → "datetime"
    - 列名含日期/时间线索且整列值为日期序列号数字（367~73415 ≈ 1901~2100 年）→ "date"/"datetime"
    - 整列值为 true/false → "boolean"
    """
    vals = [s for s in (str(v).strip() for v in values) if s]
    if not vals:
        return None
    # 1) 日期样式的字符串（与 Excel 显示一致）
    if all(_DATE_STR_RE.match(v) or _DATETIME_STR_RE.match(v) for v in vals):
        return "datetime" if any(_DATETIME_STR_RE.match(v) for v in vals) else "date"
    # 2) 列名含日期线索 + 值全部是日期序列号（Excel 1900 系统：1901-01-01=367，2100-12-31=73415）
    if _DATE_WORD_RE.search(col_name or ""):
        try:
            nums = [float(v) for v in vals]
        except ValueError:
            nums = []
        if nums and all(367 <= n <= 73415 for n in nums):
            return "datetime" if any(n != int(n) for n in nums) else "date"
    # 3) 布尔
    if all(v.lower() in ("true", "false") for v in vals):
        return "boolean"
    return None


@app.route("/api/batch/preview", methods=["POST"])
def batch_preview():
    """解析上传的 Excel 文件，返回参数名和预览数据"""
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "请上传 Excel 文件"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"ok": False, "error": "文件名为空"}), 400

    try:
        headers, all_rows = _parse_excel_file(file)
        if not headers or not any(headers):
            return jsonify({"ok": False, "error": "Excel 第一行必须是参数名（表头）"}), 400

        total = len(all_rows)
        preview = all_rows[:20]

        # 存入临时缓存，生成 session_id
        import uuid as _uuid
        session_id = _uuid.uuid4().hex[:12]
        _put_batch_session(session_id, {
            "headers": headers,
            "rows": all_rows,
            "filename": file.filename or "",
        })

        # 智能识别各列建议的入参类型（仅高置信度场景；未识别的列保持"字符串"原样传递）
        suggested = {}
        for ci, h in enumerate(headers):
            if not h:
                continue
            col_vals = [row[ci] for row in all_rows[:500] if ci < len(row)]
            guess = _detect_param_type(h, col_vals)
            if guess:
                suggested[h] = guess

        return jsonify({"ok": True, "headers": headers, "rows": preview, "total": total,
                        "session_id": session_id, "suggested_types": suggested})

    except Exception as e:
        return jsonify({"ok": False, "error": f"Excel 解析失败: {str(e)}"}), 400


# ═══════════════════════════════════════════════════
#  API: 数据库工作台（本地 SQLite / 远程 MySQL）
# ═══════════════════════════════════════════════════
_DB_STORE = None


def _db_store():
    global _DB_STORE
    if _DB_STORE is None:
        from db.store import SourceStore
        _DB_STORE = SourceStore(DATA_DIR)
    return _DB_STORE


def _db_get_source(sid):
    from db.connectors import ConnError
    if not sid:
        raise ConnError("请先选择一个数据源")
    src = _db_store().get(str(sid))
    if not src:
        raise ConnError("数据源不存在，请刷新左侧列表")
    return src


def _db_open(src, readonly=True):
    from db.connectors import open_connector
    return open_connector(src, DATA_DIR, readonly=readonly)


def _db_err(e):
    return jsonify({"ok": False, "error": str(e) or "操作失败"})


@app.route("/api/db/sources", methods=["GET"])
def db_sources_list():
    return jsonify({"ok": True, "sources": _db_store().list()})


@app.route("/api/db/sources/save", methods=["POST"])
def db_sources_save():
    data = request.get_json(silent=True) or {}
    try:
        if data.get("type") == "sqlite" and data.get("path"):
            p = str(data["path"])
            full = p if os.path.isabs(p) else os.path.join(DATA_DIR, p)
            if not os.path.isfile(full):
                return jsonify({"ok": False, "error": "数据库文件不存在：" + p})
        item = _db_store().save(data)
        return jsonify({"ok": True, "source": item, "sources": _db_store().list()})
    except Exception as e:
        return _db_err(e)


@app.route("/api/db/sources/delete", methods=["POST"])
def db_sources_delete():
    data = request.get_json(silent=True) or {}
    sid = str(data.get("id", ""))
    ok = _db_store().delete(sid)
    return jsonify({"ok": ok, "sources": _db_store().list()})


@app.route("/api/db/create-local", methods=["POST"])
def db_create_local():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "请输入库名称"})
    try:
        item, path = _db_store().create_local(name)
        return jsonify({"ok": True, "source": item, "path": path, "sources": _db_store().list()})
    except Exception as e:
        return _db_err(e)


@app.route("/api/db/test", methods=["POST"])
def db_test():
    data = request.get_json(silent=True) or {}
    try:
        from db.store import encrypt_password
        if data.get("id"):
            # 编辑已有数据源：表单里的新值覆盖旧配置，密码留空则沿用已存密码
            src = dict(_db_get_source(data["id"]) or {})
            for k in ("type", "path", "host", "user", "database"):
                if data.get(k) not in (None, ""):
                    src[k] = data[k]
            if data.get("port"):
                src["port"] = data["port"]
            if data.get("password"):
                src["password_enc"] = encrypt_password(data["password"])
        else:
            src = {
                "type": data.get("type") or "mysql",
                "path": data.get("path") or "",
                "host": data.get("host") or "",
                "port": data.get("port") or 3306,
                "user": data.get("user") or "",
                "database": data.get("database") or "",
                "password_enc": encrypt_password(data.get("password") or ""),
            }
        conn = _db_open(src, readonly=True)
        return jsonify({"ok": True, **conn.test()})
    except Exception as e:
        return _db_err(e)


@app.route("/api/db/schemas", methods=["GET"])
def db_schemas():
    try:
        src = _db_get_source(request.args.get("source_id"))
        conn = _db_open(src, readonly=True)
        return jsonify({"ok": True, "schemas": conn.list_schemas(), "type": src.get("type")})
    except Exception as e:
        return _db_err(e)


@app.route("/api/db/tables", methods=["GET"])
def db_tables():
    try:
        src = _db_get_source(request.args.get("source_id"))
        conn = _db_open(src, readonly=True)
        return jsonify({"ok": True, "tables": conn.list_tables(request.args.get("schema"))})
    except Exception as e:
        return _db_err(e)


@app.route("/api/db/columns", methods=["GET"])
def db_columns():
    try:
        src = _db_get_source(request.args.get("source_id"))
        conn = _db_open(src, readonly=True)
        return jsonify({"ok": True, "columns": conn.list_columns(
            request.args.get("schema"), request.args.get("table"))})
    except Exception as e:
        return _db_err(e)


@app.route("/api/db/table-info", methods=["GET"])
def db_table_info():
    """表结构 + 表注释 + 字段注释，用于右侧表详情面板"""
    try:
        src = _db_get_source(request.args.get("source_id"))
        conn = _db_open(src, readonly=True)
        return jsonify({"ok": True, **conn.table_info(
            request.args.get("schema"), request.args.get("table"))})
    except Exception as e:
        return _db_err(e)


@app.route("/api/db/query", methods=["POST"])
def db_query():
    data = request.get_json(silent=True) or {}
    sql = str(data.get("sql") or "").strip()
    if not sql:
        return jsonify({"ok": False, "error": "请输入 SQL 语句"})
    try:
        src = _db_get_source(data.get("source_id"))
        conn = _db_open(src, readonly=True)
        # schema：左侧当前选中的库，MySQL 必须连到这个库才能执行不带头名的表名
        return jsonify({"ok": True, **conn.query(sql, schema=str(data.get("schema") or ""))})
    except Exception as e:
        return _db_err(e)


@app.route("/api/db/execute", methods=["POST"])
def db_execute():
    data = request.get_json(silent=True) or {}
    sid = str(data.get("source_id") or "")
    sql = str(data.get("sql") or "").strip()
    if not sql:
        return jsonify({"ok": False, "error": "请输入 SQL 语句"})
    try:
        src = _db_get_source(sid)
        conn = _db_open(src, readonly=False)
        return jsonify({"ok": True, **conn.execute(sql, schema=str(data.get("schema") or ""))})
    except Exception as e:
        return _db_err(e)


@app.route("/api/db/create-table", methods=["POST"])
def db_create_table():
    data = request.get_json(silent=True) or {}
    try:
        src = _db_get_source(data.get("source_id"))
        if src.get("type") != "sqlite":
            return jsonify({"ok": False, "error": "可视化建表目前只支持本地库，远程库请直接执行 CREATE TABLE"})
        conn = _db_open(src, readonly=False)
        return jsonify({"ok": True, **conn.create_table(data.get("table"), data.get("columns"))})
    except Exception as e:
        return _db_err(e)


@app.route("/api/db/table-data", methods=["GET"])
def db_table_data():
    try:
        src = _db_get_source(request.args.get("source_id"))
        conn = _db_open(src, readonly=True)
        try:
            limit = max(1, min(int(request.args.get("limit") or 100), 1000))
            offset = max(int(request.args.get("offset") or 0), 0)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "limit / offset 必须是整数"})
        order = (request.args.get("order") or "").strip()
        direction = (request.args.get("dir") or "").strip().lower()
        return jsonify({"ok": True, **conn.table_data(
            request.args.get("table"), limit, offset,
            request.args.get("where") or "", request.args.get("schema") or "",
            {"column": order, "dir": direction} if order else None)})
    except Exception as e:
        return _db_err(e)


@app.route("/api/db/backup-table", methods=["GET"])
def db_backup_table():
    """整表备份：返回建表语句 + INSERT，前端直接存成 .sql 文件"""
    try:
        src = _db_get_source(request.args.get("source_id"))
        conn = _db_open(src, readonly=True)
        return jsonify({"ok": True, **conn.backup_table(
            request.args.get("table"), request.args.get("schema") or "")})
    except Exception as e:
        return _db_err(e)


@app.route("/api/db/update-cell", methods=["POST"])
def db_update_cell():
    """数据页双击改单元格：按主键/rowid 精确更新一行"""
    data = request.get_json(silent=True) or {}
    if "value" not in data:
        return jsonify({"ok": False, "error": "缺少要写入的值"})
    try:
        src = _db_get_source(data.get("source_id"))
        conn = _db_open(src, readonly=False)
        return jsonify(conn.update_cell(
            data.get("schema") or src.get("database"), data.get("table"),
            data.get("key") or {}, data.get("column"), data.get("value")))
    except Exception as e:
        return _db_err(e)


@app.route("/api/db/save-rows", methods=["POST"])
def db_save_rows():
    """数据页暂存编辑：新增 / 修改 / 删除一次性提交，共用一个事务"""
    data = request.get_json(silent=True) or {}
    try:
        src = _db_get_source(data.get("source_id"))
        conn = _db_open(src, readonly=False)
        return jsonify(conn.save_rows(
            data.get("schema") or src.get("database"), data.get("table"),
            data.get("updates") or [], data.get("inserts") or [], data.get("deletes") or []))
    except Exception as e:
        return _db_err(e)


@app.route("/api/db/alter-table", methods=["POST"])
def db_alter_table():
    """改表定义：加列 / 改列（可改名）/ 删列"""
    data = request.get_json(silent=True) or {}
    try:
        src = _db_get_source(data.get("source_id"))
        conn = _db_open(src, readonly=False)
        return jsonify({"ok": True, **conn.alter_column(
            data.get("schema") or src.get("database"), data.get("table"), data)})
    except Exception as e:
        return _db_err(e)


@app.route("/api/db/batch-update", methods=["POST"])
def db_batch_update():
    """数据页批量改值：把某列按筛选条件统一改成固定值，或做查找替换"""
    data = request.get_json(silent=True) or {}
    try:
        src = _db_get_source(data.get("source_id"))
        conn = _db_open(src, readonly=False)
        return jsonify({"ok": True, **conn.batch_update(
            data.get("schema") or src.get("database"), data.get("table"),
            data.get("column"), data.get("where") or "", data.get("mode") or "set",
            data.get("value"), data.get("find") or "", data.get("replace") or "",
            dry_run=bool(data.get("dry_run")), exact=bool(data.get("exact")))})
    except Exception as e:
        return _db_err(e)


@app.route("/api/db/batch-preview", methods=["POST"])
def db_batch_preview():
    """批量改值前预览：只回传真正会变化的行（改前 → 改后），用于命中统计与结果核对"""
    data = request.get_json(silent=True) or {}
    try:
        src = _db_get_source(data.get("source_id"))
        conn = _db_open(src, readonly=True)
        return jsonify({"ok": True, **conn.batch_preview(
            data.get("schema") or src.get("database"), data.get("table"),
            data.get("column"), data.get("where") or "", data.get("mode") or "set",
            data.get("value"), data.get("find") or "", data.get("replace") or "",
            limit=data.get("limit") or 50, exact=bool(data.get("exact")))})
    except Exception as e:
        return _db_err(e)


# ═══════════════════════════════════════════════════
#  API: SQL 文件（通用 SQL 查询，多文件保存在 data/sql/*.sql）
# ═══════════════════════════════════════════════════

SQL_FILES_DIR = os.path.join(DATA_DIR, "sql")
SQL_FILES_META = os.path.join(SQL_FILES_DIR, "_meta.json")


def _sql_files_dir():
    os.makedirs(SQL_FILES_DIR, exist_ok=True)
    return SQL_FILES_DIR


def _sql_clean_name(name):
    """文件名清洗：去掉路径分隔符与系统非法字符，避免跳出 sql 目录"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(name or "")).strip().strip(".")
    return s[:80]


def _sql_meta_load():
    try:
        with open(SQL_FILES_META, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _sql_meta_save(meta):
    try:
        with open(SQL_FILES_META, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


@app.route("/api/sql/files", methods=["GET"])
def sql_files_list():
    d = _sql_files_dir()
    meta = _sql_meta_load()
    files = []
    for fn in os.listdir(d):
        if not fn.lower().endswith(".sql"):
            continue
        name = fn[:-4]
        try:
            st = os.stat(os.path.join(d, fn))
        except OSError:
            continue
        m = meta.get(name) or {}
        files.append({
            "name": name,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "updated": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "source_id": m.get("source_id") or "",
            "schema": m.get("schema") or "",
        })
    # 最近改过的排前面，常用文件不用往下翻
    files.sort(key=lambda x: x.get("mtime") or 0, reverse=True)
    return jsonify({"ok": True, "files": files})


@app.route("/api/sql/file", methods=["GET"])
def sql_file_get():
    name = _sql_clean_name(request.args.get("name"))
    p = os.path.join(_sql_files_dir(), name + ".sql")
    if not name or not os.path.isfile(p):
        return jsonify({"ok": False, "error": "SQL 文件不存在"})
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            content = f.read()
    except Exception as e:
        return jsonify({"ok": False, "error": "读取失败：%s" % e})
    m = _sql_meta_load().get(name) or {}
    return jsonify({"ok": True, "name": name, "sql": content,
                    "source_id": m.get("source_id") or "", "schema": m.get("schema") or ""})


@app.route("/api/sql/save", methods=["POST"])
def sql_file_save():
    data = request.get_json(silent=True) or {}
    name = _sql_clean_name(data.get("name"))
    if not name:
        return jsonify({"ok": False, "error": "请填写 SQL 文件名"})
    old = _sql_clean_name(data.get("old_name"))
    sql = str(data.get("sql") or "")
    d = _sql_files_dir()
    try:
        with open(os.path.join(d, name + ".sql"), "w", encoding="utf-8", newline="\n") as f:
            f.write(sql)
    except Exception as e:
        return jsonify({"ok": False, "error": "保存失败：%s" % e})
    meta = _sql_meta_load()
    # old_name 与 name 不同 = 重命名：旧文件与旧元信息一并清掉
    if old and old != name:
        try:
            os.remove(os.path.join(d, old + ".sql"))
        except OSError:
            pass
        meta.pop(old, None)
    meta[name] = {
        "source_id": str(data.get("source_id") or ""),
        "schema": str(data.get("schema") or ""),
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _sql_meta_save(meta)
    return jsonify({"ok": True, "name": name})


@app.route("/api/sql/delete", methods=["POST"])
def sql_file_delete():
    data = request.get_json(silent=True) or {}
    name = _sql_clean_name(data.get("name"))
    p = os.path.join(_sql_files_dir(), name + ".sql")
    if not name or not os.path.isfile(p):
        return jsonify({"ok": False, "error": "SQL 文件不存在"})
    try:
        os.remove(p)
    except Exception as e:
        return jsonify({"ok": False, "error": "删除失败：%s" % e})
    meta = _sql_meta_load()
    meta.pop(name, None)
    _sql_meta_save(meta)
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════
#  API: 批量参数化 — 执行（SSE 流式）
# ═══════════════════════════════════════════════════

def _normalize_date_value(value, type_str):
    """日期/日期时间规范化（委托 engine，保持既有调用方兼容）"""
    import engine as E
    return E.normalize_date_value(value, type_str)


def _convert_param_value(value, type_str):
    """Excel 参数值类型转换（委托 engine，与定时任务参数化共用同一实现）"""
    import engine as E
    return E.convert_param_value(value, type_str)


@app.route("/api/batch/run-stream", methods=["POST"])
def batch_run_stream():
    """批量参数化执行 step，SSE 流式返回"""
    filename = request.form.get("yaml_file", "")
    step_index_str = request.form.get("step_index", "0")
    loops_str = request.form.get("loops", "1")
    param_map_str = request.form.get("param_map", "{}")

    try:
        step_index = int(step_index_str)
    except ValueError:
        return jsonify({"error": "步骤序号无效"}), 400

    loops = min(max(1, int(loops_str) if loops_str.isdigit() else 1), 100)

    try:
        param_map = json.loads(param_map_str)
    except json.JSONDecodeError:
        return jsonify({"error": "参数映射格式错误"}), 400

    # 参数类型映射（Excel列名 -> string/number/boolean/json）
    param_types_str = request.form.get("param_types", "{}")
    try:
        param_types = json.loads(param_types_str)
    except json.JSONDecodeError:
        param_types = {}

    # 从临时缓存读取参数数据
    session_id = request.form.get("session_id", "").strip()
    session = _get_batch_session(session_id) if session_id else None
    if not session_id or session is None:
        return jsonify({"error": "参数会话已过期，请重新上传 Excel"}), 400

    headers = session["headers"]
    param_rows = session["rows"]

    filepath = _safe_filepath(filename)
    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": f"YAML 文件不存在: {filename}"}), 404

    import engine as E

    cfg = _load_doc(filepath)

    steps = cfg.get("steps", [])
    if step_index < 1 or step_index > len(steps):
        return jsonify({"error": f"步骤 {step_index} 不存在"}), 404

    step = steps[step_index - 1]
    step_name = step.get("name", f"Step{step_index}")
    req_cfg = step.get("request", {})
    assert_cfg = step.get("assert", {})
    step_mock = step.get("mock")

    if not param_rows:
        return jsonify({"error": "Excel 中没有数据行"}), 400

    total_rows = len(param_rows)
    total_requests = total_rows * loops

    # 加载预设变量和 setup
    preset_vars = _load_variables_values()
    setup_cfg = cfg.get("setup")
    setup_vars, setup_errors = E.eval_setup(setup_cfg)

    def generate():
        import re
        import traceback

        yield f"data: {json.dumps({'type': 'preview', 'total_rows': total_rows, 'loops': loops, 'total_requests': total_requests, 'step_name': step_name}, ensure_ascii=False)}\n\n"

        seq = 0
        try:
          for loop_num in range(1, loops + 1):
            for row_idx, row in enumerate(param_rows):
                seq += 1
                row_num = row_idx + 1

                variables = dict(preset_vars)
                variables.update(setup_vars)

                for excel_col_name, yaml_var_name in param_map.items():
                    try:
                        col_idx = headers.index(excel_col_name)
                        if col_idx < len(row):
                            variables[yaml_var_name] = _convert_param_value(
                                row[col_idx], param_types.get(excel_col_name, "string"))
                    except ValueError:
                        pass

                refs = set(re.findall(r'\$\{(\w+)\}', str(req_cfg)))
                used_preset = [k for k in refs if k in preset_vars]
                used_param = [k for k in refs if k in variables and k not in preset_vars and k not in (setup_vars or {})]

                buf = io.StringIO()
                passed = False
                record = {}

                try:
                    with contextlib.redirect_stdout(buf):
                        result = E.do_request(req_cfg, variables, mock=step_mock)

                        # 入参快照在 do_request 之后取：自增变量在请求前才取号
                        _resolved_cfg = E._replace_deep(req_cfg, variables or {})
                        _req_body = _resolved_cfg.get("body")
                        _req_params = _resolved_cfg.get("params")
                        _req_headers = _resolved_cfg.get("headers") or {}

                        if result["error"]:
                            E.P.print_error(result["error"])
                            record = {
                                "step_index": step_index, "name": step_name,
                                "method": req_cfg.get("method", "GET"),
                                "url": E._replace(req_cfg.get("url", ""), variables),
                                "status_code": "ERR", "elapsed_ms": result["elapsed_ms"],
                                "passed": False, "asserts": [],
                                "response": result["error"], "extracted": [],
                                "is_mock": False,
                                "req_body": _req_body, "req_params": _req_params, "req_headers": _req_headers,
                            }
                        else:
                            ok, msgs = E.run_assert(
                                assert_cfg, result["status_code"],
                                result["resp_text"], result["resp_json"],
                                elapsed_ms=result["elapsed_ms"],
                                resp_headers=result.get("resp_headers"))
                            passed = ok

                            resp_preview = ""
                            if result["resp_json"] is not None:
                                resp_preview = json.dumps(
                                    result["resp_json"], ensure_ascii=False, indent=2, default=str)
                            else:
                                resp_preview = result["resp_text"] or ""

                            record = {
                                "step_index": step_index, "name": step_name,
                                "method": req_cfg.get("method", "GET"),
                                "url": E._replace(req_cfg.get("url", ""), variables),
                                "status_code": result["status_code"],
                                "elapsed_ms": result["elapsed_ms"],
                                "passed": passed,
                                "asserts": [re.sub(r'\x1b\[[0-9;]*m', '', m) for m in msgs],
                                "response": resp_preview,
                                "extracted": [],
                                "is_mock": result.get("is_mock", False),
                                "seq_used": result.get("seq_used") or {},
                                "req_body": _req_body, "req_params": _req_params, "req_headers": _req_headers,
                            }
                except Exception as ex:
                    record = {
                        "step_index": step_index, "name": step_name,
                        "method": req_cfg.get("method", "GET"),
                        "url": E._replace(req_cfg.get("url", ""), variables),
                        "status_code": "ERR", "elapsed_ms": 0,
                        "passed": False, "asserts": [],
                        "response": f"执行异常: {type(ex).__name__}: {ex}",
                        "extracted": [], "is_mock": False, "req_body": "",
                        "req_params": None, "req_headers": None,
                    }

                record["seq"] = seq
                record["total"] = total_requests
                record["loop"] = loop_num
                record["row_index"] = row_idx
                record["row_num"] = row_num
                record["preset_vars_used"] = used_preset
                record["param_vars_used"] = used_param
                record["is_batch"] = True
                record["console"] = buf.getvalue()

                yield f"data: {json.dumps(record, ensure_ascii=False, default=str)}\n\n"
        except Exception as ex:
            err_record = {"type": "error", "error": f"批量执行异常: {type(ex).__name__}: {ex}"}
            yield f"data: {json.dumps(err_record, ensure_ascii=False)}\n\n"

        yield "data: [DONE]\n\n"

    return app.response_class(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@app.route("/api/batch/run-chain-stream", methods=["POST"])
def batch_run_chain_stream():
    """参数化执行：选中多个步骤按顺序串联执行，
    用 Excel 行数据对第一个步骤参数化，每一行数据为一组，
    每组依次执行所有选中步骤，组内步骤间变量可通过 extract 传递。
    SSE 流式返回。

    请求参数（form data）:
      yaml_file  — YAML 文件名
      steps      — 步骤序号（逗号分隔，按输入顺序执行，如 "1,2"）
      loops      — 整体循环次数
      param_map  — Excel列名 → YAML变量名 的 JSON 映射
      session_id — Excel 上传会话 ID
    """
    filename = request.form.get("yaml_file", "")
    steps_str = request.form.get("steps", "")
    loops_str = request.form.get("loops", "1")
    param_map_str = request.form.get("param_map", "{}")

    loops = min(max(1, int(loops_str) if loops_str.isdigit() else 1), 100)

    try:
        param_map = json.loads(param_map_str)
    except json.JSONDecodeError:
        return jsonify({"error": "参数映射格式错误"}), 400

    # 参数类型映射（Excel列名 -> string/number/boolean/json）
    param_types_str = request.form.get("param_types", "{}")
    try:
        param_types = json.loads(param_types_str)
    except json.JSONDecodeError:
        param_types = {}

    # 从临时缓存读取参数数据
    session_id = request.form.get("session_id", "").strip()
    session = _get_batch_session(session_id) if session_id else None
    if not session_id or session is None:
        return jsonify({"error": "参数会话已过期，请重新上传 Excel"}), 400

    headers = session["headers"]
    param_rows = session["rows"]

    filepath = _safe_filepath(filename)
    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": f"YAML 文件不存在: {filename}"}), 404

    import engine as E

    cfg = _load_doc(filepath)

    steps = cfg.get("steps", [])
    if not steps:
        return jsonify({"error": "没有 steps 配置"}), 400

    # 解析步骤过滤（按用户输入顺序执行）
    filter_nums = None
    if steps_str:
        try:
            filter_nums = [int(s.strip()) for s in steps_str.split(",") if s.strip()]
        except ValueError:
            return jsonify({"error": "步骤格式错误，用数字和逗号，如 1,2"}), 400

    if filter_nums:
        ordered_steps = []
        for num in filter_nums:
            if 1 <= num <= len(steps):
                ordered_steps.append((num, steps[num - 1]))
    else:
        ordered_steps = list(enumerate(steps, 1))

    if not ordered_steps:
        return jsonify({"error": "没有有效的步骤"}), 400

    if not param_rows:
        return jsonify({"error": "Excel 中没有数据行"}), 400

    total_rows = len(param_rows)
    total_groups = total_rows * loops
    total_requests = total_groups * len(ordered_steps)

    # 加载预设变量和 setup
    preset_vars = _load_variables_values()
    setup_cfg = cfg.get("setup")
    setup_vars, setup_errors = E.eval_setup(setup_cfg)

    def generate():
        import re
        seq = 0
        group_num = 0
        # Excel 注入的 YAML 变量名集合（param_map 的值）
        param_var_names = set(param_map.values())

        yield f"data: {json.dumps({'type': 'preview', 'total_rows': total_rows, 'loops': loops, 'total_groups': total_groups, 'total_requests': total_requests, 'steps': [n for n, _ in ordered_steps]}, ensure_ascii=False)}\n\n"

        try:
          for loop_num in range(1, loops + 1):
            for row_idx, row in enumerate(param_rows):
                group_num += 1
                row_num = row_idx + 1

                # 每组数据以全新变量开始：预设 + setup + 本行参数
                variables = dict(preset_vars)
                variables.update(setup_vars)
                for excel_col_name, yaml_var_name in param_map.items():
                    try:
                        col_idx = headers.index(excel_col_name)
                        if col_idx < len(row):
                            variables[yaml_var_name] = _convert_param_value(
                                row[col_idx], param_types.get(excel_col_name, "string"))
                    except ValueError:
                        pass

                step_extracted_keys = set()
                group_label = f"第 {group_num} 组数据（行 {row_num}）"

                # 组内按顺序执行所有选中步骤，变量在步骤间传递
                for idx, step in ordered_steps:
                    seq += 1
                    step_name = step.get("name", f"Step{idx}")
                    req_cfg = step.get("request", {})
                    assert_cfg = step.get("assert", {})
                    extract = step.get("extract", {})
                    extract_list = step.get("extract_list", {})
                    step_mock = step.get("mock")

                    # 步骤级 setup（覆盖同名变量）
                    step_setup_cfg = step.get("setup")
                    if step_setup_cfg and isinstance(step_setup_cfg, dict):
                        step_setup_vars, _ = E.eval_setup(step_setup_cfg)
                        variables.update(step_setup_vars)

                    buf = io.StringIO()
                    passed = False
                    record = {}

                    # 本轮请求配置单独一份：前置脚本改过也只影响本次
                    cur_req = _copy_req(req_cfg)
                    pre_info = None
                    post_info = None

                    try:
                        with contextlib.redirect_stdout(buf):
                            cur_req, pre_info = _exec_step_script(step, "pre", variables, req_cfg=cur_req)
                            _pre_err = (pre_info or {}).get("error") or ""
                            if _pre_err:
                                E.P.print_error("前置脚本出错：" + _pre_err)
                                result = {"ok": False, "status_code": None, "elapsed_ms": 0,
                                          "resp_text": "", "resp_json": None, "resp_headers": {},
                                          "error": "前置脚本出错：" + _pre_err,
                                          "is_mock": False, "seq_used": {}}
                            else:
                                result = E.do_request(cur_req, variables, mock=step_mock)

                            # 入参快照在 do_request 之后取：自增变量在请求前才取号
                            _resolved_cfg = E._replace_deep(cur_req, variables or {})
                            _req_body = _resolved_cfg.get("body")
                            _req_params = _resolved_cfg.get("params")
                            _req_headers = _resolved_cfg.get("headers") or {}

                            if result["error"]:
                                E.P.print_error(result["error"])
                                record = {
                                    "step_index": idx, "name": step_name,
                                    "method": cur_req.get("method", "GET"),
                                    "url": E._replace(cur_req.get("url", ""), variables),
                                    "status_code": "ERR", "elapsed_ms": result["elapsed_ms"],
                                    "passed": False, "asserts": [],
                                    "response": result["error"], "extracted": [],
                                    "is_mock": False,
                                    "req_body": _req_body, "req_params": _req_params, "req_headers": _req_headers,
                                }
                            else:
                                ok, msgs = E.run_assert(
                                    assert_cfg, result["status_code"],
                                    result["resp_text"], result["resp_json"],
                                    elapsed_ms=result["elapsed_ms"],
                                    resp_headers=result.get("resp_headers"))
                                passed = ok

                                resp_preview = ""
                                if result["resp_json"] is not None:
                                    resp_preview = json.dumps(
                                        result["resp_json"], ensure_ascii=False, indent=2, default=str)
                                else:
                                    resp_preview = result["resp_text"] or ""

                                extracted = []
                                extract_detail = {}
                                if result["resp_json"] is not None:
                                    extracted = E.do_extract(
                                        extract, extract_list,
                                        result["resp_json"], variables, extract_detail)
                                    for var_name in list(extract.keys()) + list(extract_list.keys()):
                                        if var_name in variables:
                                            step_extracted_keys.add(var_name)

                                # 后置脚本：读响应、写变量，pm.test 的断言并进断言列表
                                _, post_info = _exec_step_script(step, "post", variables, result=result)
                                passed = _merge_script_tests(msgs, post_info, passed)

                                record = {
                                    "step_index": idx, "name": step_name,
                                    "method": cur_req.get("method", "GET"),
                                    "url": E._replace(cur_req.get("url", ""), variables),
                                    "status_code": result["status_code"],
                                    "elapsed_ms": result["elapsed_ms"],
                                    "passed": passed,
                                    "asserts": [re.sub(r'\x1b\[[0-9;]*m', '', m) for m in msgs],
                                    "response": resp_preview,
                                    "extracted": extracted,
                                    "extract_detail": extract_detail,
                                    "is_mock": result.get("is_mock", False),
                                    "seq_used": result.get("seq_used") or {},
                                    "req_body": _req_body, "req_params": _req_params, "req_headers": _req_headers,
                                }
                    except Exception as ex:
                        record = {
                            "step_index": idx, "name": step_name,
                            "method": req_cfg.get("method", "GET"),
                            "url": E._replace(req_cfg.get("url", ""), variables),
                            "status_code": "ERR", "elapsed_ms": 0,
                            "passed": False, "asserts": [],
                            "response": f"执行异常: {type(ex).__name__}: {ex}",
                            "extracted": [], "is_mock": False, "req_body": "",
                            "req_params": None, "req_headers": None,
                        }

                    # 扫的是前置脚本改过之后的请求，变量来源才报得准
                    refs_before = _scan_var_refs(str(cur_req))
                    used_preset = [k for k in refs_before if k in preset_vars]
                    used_from_step = [k for k in refs_before if k in step_extracted_keys and k not in preset_vars]
                    used_param = [k for k in refs_before if k in param_var_names and k not in preset_vars]
                    if pre_info or post_info:
                        record["script"] = {"pre": pre_info, "post": post_info}
                        _slines = _script_log_lines(pre_info, post_info)
                        if _slines:
                            record["script_logs"] = _slines

                    record["seq"] = seq
                    record["total"] = total_requests
                    record["loop"] = group_num  # 以组号分组渲染
                    record["group_label"] = group_label
                    record["row_index"] = row_idx
                    record["row_num"] = row_num
                    record["preset_vars_used"] = used_preset
                    record["step_vars_used"] = used_from_step
                    record["param_vars_used"] = used_param
                    record["is_batch_chain"] = True
                    record["console"] = buf.getvalue()
                    if record.get("script_logs"):
                        record["console"] = "\n".join(record["script_logs"]) + "\n" + record["console"]

                    yield f"data: {json.dumps(record, ensure_ascii=False, default=str)}\n\n"
        except Exception as ex:
            err_record = {"type": "error", "error": f"参数化执行异常: {type(ex).__name__}: {ex}"}
            yield f"data: {json.dumps(err_record, ensure_ascii=False)}\n\n"

        yield "data: [DONE]\n\n"

    return app.response_class(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


# ═══════════════════════════════════════════════════
#  JSON 数据文件并发锁（按文件粒度，防止多线程读-改-写冲突）
# ═══════════════════════════════════════════════════

_FILE_LOCKS = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _file_lock(path):
    """获取指定文件的模块级锁（同一文件始终返回同一把锁）"""
    key = os.path.realpath(path)
    with _FILE_LOCKS_GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _FILE_LOCKS[key] = lock
        return lock


# ═══════════════════════════════════════════════════
#  变量持久化存储（预设值）
# ═══════════════════════════════════════════════════

VARS_FILE = os.path.join(DATA_DIR, "_variables.json")


def _load_variables():
    """加载持久化的变量字典（含元数据）
    新格式: {name: {value: ..., saved_at: ...}}
    旧格式: {name: value} 会自动迁移
    混合格式: 逐个 key 迁移
    """
    if not os.path.exists(VARS_FILE):
        return {}
    with _file_lock(VARS_FILE):
        try:
            with open(VARS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not raw:
                return {}
            # 逐个 key 检测并迁移旧格式
            needs_save = False
            for k, v in list(raw.items()):
                if isinstance(v, str):
                    from datetime import datetime as _dt
                    raw[k] = {"value": v, "saved_at": _dt.now().strftime("%Y-%m-%d %H:%M:%S")}
                    needs_save = True
            if needs_save:
                with open(VARS_FILE, "w", encoding="utf-8") as fw:
                    json.dump(raw, fw, ensure_ascii=False, indent=2)
            return raw
        except (json.JSONDecodeError, IOError):
            return {}


def _load_variables_values():
    """加载变量名→值字典（供执行使用，不含元数据）

    自增变量（带 seq 元数据）不在这里返回：它们由 seqvars.assign 在每次请求前取号
    写入 variables。若预设里带初值，取号时会被当成「外部写入」而跳过。
    """
    raw = _load_variables()
    result = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            if isinstance(v.get("seq"), dict):
                continue
            result[k] = v.get("value", "")
        else:
            result[k] = v
    # 当前环境的变量覆盖同名预设：环境代表具体上下文，优先级高于全局预设
    result.update(_env_values())
    return result


def _save_variables(variables):
    """保存变量字典到文件"""
    with _file_lock(VARS_FILE):
        with open(VARS_FILE, "w", encoding="utf-8") as f:
            json.dump(variables, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════
#  环境变量（多环境切换：变量取值 环境 > 预设）
# ═══════════════════════════════════════════════════

ENVS_FILE = os.path.join(DATA_DIR, "_environments.json")


def _load_environments():
    """加载环境配置：{"active": 环境 id, "envs": [{"id", "name", "vars": [{"key", "value", "enabled"}]}]}"""
    if not os.path.exists(ENVS_FILE):
        return {"active": "", "envs": []}
    with _file_lock(ENVS_FILE):
        try:
            with open(ENVS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, IOError):
            return {"active": "", "envs": []}
    if not isinstance(raw, dict):
        return {"active": "", "envs": []}
    envs = raw.get("envs")
    if not isinstance(envs, list):
        envs = []
    return {
        "active": str(raw.get("active") or ""),
        "envs": [e for e in envs if isinstance(e, dict) and e.get("id")],
    }


def _save_environments(data):
    """写入环境配置文件"""
    with _file_lock(ENVS_FILE):
        with open(ENVS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def _active_env():
    """当前激活的环境（未选或已被删除时返回 None）"""
    data = _load_environments()
    for env in data["envs"]:
        if env.get("id") == data.get("active"):
            return env
    return None


def _env_values(env=None):
    """取环境里启用的变量（env 为空时取当前激活环境）；空 key 与停用项跳过"""
    if env is None:
        env = _active_env()
    if not env:
        return {}
    result = {}
    for row in env.get("vars") or []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").strip()
        if not key or row.get("enabled", True) is False:
            continue
        result[key] = row.get("value", "")
    return result


def _clean_env_vars(rows):
    """校验并规整环境变量行（丢掉空 key，值统一转字符串，保留启用开关）"""
    clean = []
    if not isinstance(rows, list):
        return clean
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").strip()
        if not key:
            continue
        value = row.get("value")
        clean.append({
            "key": key,
            "value": "" if value is None else str(value),
            "enabled": row.get("enabled", True) is not False,
        })
    return clean


@app.route("/api/environments")
def get_environments():
    """环境列表 + 当前激活环境 + 当前环境生效的变量值"""
    data = _load_environments()
    return jsonify({
        "ok": True,
        "active": data["active"],
        "envs": data["envs"],
        "values": _env_values(),
    })


@app.route("/api/environments/save", methods=["POST"])
def save_environment():
    """新建/更新环境：{"id": 可选, "name": 环境名, "vars": [{"key", "value", "enabled"}]}"""
    from datetime import datetime as _dt
    body = request.get_json(silent=True) or {}
    env_id = str(body.get("id") or "").strip()
    name = str(body.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "请输入环境名称"}), 400

    data = _load_environments()
    for env in data["envs"]:
        if env.get("name") == name and env.get("id") != env_id:
            return jsonify({"ok": False, "error": "已存在同名环境"}), 400

    vars_rows = _clean_env_vars(body.get("vars"))
    if env_id:
        target = next((e for e in data["envs"] if e.get("id") == env_id), None)
        if not target:
            return jsonify({"ok": False, "error": "环境不存在（可能已被删除）"}), 404
        target["name"] = name
        target["vars"] = vars_rows
    else:
        env_id = "env_" + os.urandom(4).hex()
        data["envs"].append({
            "id": env_id,
            "name": name,
            "vars": vars_rows,
            "created_at": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        # 第一个环境自动设为当前，避免建完还要再点一次
        if not data.get("active"):
            data["active"] = env_id
    _save_environments(data)
    return jsonify({"ok": True, "id": env_id, "active": data["active"], "count": len(vars_rows)})


@app.route("/api/environments/activate", methods=["POST"])
def activate_environment():
    """切换当前环境：{"id": 环境 id}，id 为空表示不使用环境"""
    env_id = str((request.get_json(silent=True) or {}).get("id") or "").strip()
    data = _load_environments()
    if env_id and not any(e.get("id") == env_id for e in data["envs"]):
        return jsonify({"ok": False, "error": "环境不存在"}), 404
    data["active"] = env_id
    _save_environments(data)
    return jsonify({"ok": True, "active": env_id, "values": _env_values()})


@app.route("/api/environments/delete", methods=["POST"])
def delete_environment():
    """删除环境：{"id": 环境 id}；删的是当前环境时自动退回不使用环境"""
    env_id = str((request.get_json(silent=True) or {}).get("id") or "").strip()
    data = _load_environments()
    before = len(data["envs"])
    data["envs"] = [e for e in data["envs"] if e.get("id") != env_id]
    if len(data["envs"]) == before:
        return jsonify({"ok": False, "error": "环境不存在"}), 404
    if data.get("active") == env_id:
        data["active"] = ""
    _save_environments(data)
    return jsonify({"ok": True, "active": data["active"]})


@app.route("/api/variables")
def get_variables():
    """获取所有预设变量（含保存时间）"""
    raw = _load_variables()
    # 返回简单值字典（向后兼容）+ 元数据
    values = {}
    meta = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            values[k] = v.get("value", "")
            meta[k] = {"saved_at": v.get("saved_at", "")}
            _seq_desc = seqvars.describe(v)
            if _seq_desc:
                meta[k]["seq"] = _seq_desc
        else:
            values[k] = v
            meta[k] = {"saved_at": ""}
    return jsonify({"variables": values, "variables_meta": meta})


@app.route("/api/variables/set", methods=["POST"])
def set_variable():
    """设置/更新一个或多个变量
    请求: {"name": "token", "value": "abc"} 或 {"bulk": {"token": "abc", "userId": "42"}}
    重命名: {"old_name": "token", "name": "newToken", "value": "abc"}
    自增变量: {"name": "orderNo", "seq": {"sample": "XX001", "digits": 3, "step": 1, "reset": true}}
             {"name": "orderNo", "seq": null} 取消自增，降级为普通变量
    """
    from datetime import datetime as _dt
    body = request.get_json(silent=True) or {}
    variables = _load_variables()
    now_str = _dt.now().strftime("%Y-%m-%d %H:%M:%S")

    bulk = body.get("bulk")
    if bulk and isinstance(bulk, dict):
        # 自增变量不接受批量回填（接口提取值自动保存时会走到这里），否则计数器会被冲成普通值
        skipped = [k for k in bulk if seqvars.is_seq(variables.get(k))]
        for k, v in bulk.items():
            if k in skipped:
                continue
            variables[k] = {"value": v, "saved_at": now_str}
        _save_variables(variables)
        return jsonify({"ok": True, "saved": [k for k in bulk if k not in skipped],
                        "skipped": skipped, "count": len(bulk) - len(skipped)})

    name = str(body.get("name", "")).strip()
    value = body.get("value", "")
    old_name = str(body.get("old_name", "")).strip()
    if not name:
        return jsonify({"ok": False, "error": "请输入变量名"}), 400

    # 重命名：整条迁移（自增变量要连 seq 计数器一起搬走，只搬 value 会让计数器归零）
    moved = False
    if old_name and old_name != name and old_name in variables:
        variables[name] = variables.pop(old_name)
        _save_variables(variables)
        moved = True

    # 自增变量：seq 为规则字典 = 新建/改写；seq 为 null = 降级为普通变量
    if "seq" in body:
        if body.get("seq") is None:
            seqvars.drop_seq(name)
            return jsonify({"ok": True, "name": name, "seq": None})
        seq_cfg = body.get("seq") or {}
        desc = seqvars.set_rule(
            name, seq_cfg.get("sample", value), seq_cfg.get("digits"),
            seq_cfg.get("step", 1), seq_cfg.get("reset", True))
        return jsonify({"ok": True, "name": name, "seq": desc})

    if seqvars.is_seq(variables.get(name)):
        if moved:
            # 只改名不动值：自增计数器原样保留
            return jsonify({"ok": True, "name": name})
        return jsonify({"ok": False,
                        "error": "该变量是自增变量，请用自增规则编辑（或在变量表里取消自增）"}), 400

    variables[name] = {"value": value, "saved_at": now_str}
    _save_variables(variables)
    return jsonify({"ok": True, "name": name})


@app.route("/api/variables/delete", methods=["POST"])
def delete_variable():
    """删除一个变量"""
    name = (request.get_json(silent=True) or {}).get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "请输入变量名"}), 400
    variables = _load_variables()
    if name in variables:
        del variables[name]
        _save_variables(variables)
    return jsonify({"ok": True, "name": name})


@app.route("/api/variables/clear", methods=["POST"])
def clear_variables():
    """清空所有变量"""
    _save_variables({})
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════
#  数据集合：导入 Excel/CSV 当参数化数据表，接口里用 ${字段名} 引用
# ═══════════════════════════════════════════════════
@app.route("/api/dataset/list", methods=["GET"])
def dataset_list():
    """数据集列表 + 当前数据集名"""
    import dataset as DS
    try:
        return jsonify({"ok": True, "items": DS.list_datasets(), "active": DS.active_name()})
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500


@app.route("/api/dataset", methods=["GET"])
def dataset_get():
    """取一个数据集的完整内容（表头 + 行）"""
    import dataset as DS
    name = str(request.args.get("name") or "").strip()
    data = DS.load(name)
    if not data:
        return jsonify({"ok": False, "error": "数据集不存在"}), 404
    return jsonify({"ok": True, "dataset": data})


@app.route("/api/dataset/import", methods=["POST"])
def dataset_import():
    """导入 xlsx / csv：整张表存成数据集（同名覆盖）"""
    import dataset as DS
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "请选择要导入的文件"}), 400
    name = str(request.form.get("name") or "").strip()
    if not name:
        name = os.path.splitext(os.path.basename(f.filename))[0]
    try:
        headers, rows = DS.read_table(f.filename, f.stream)
        data = DS.save(name, headers, rows, source=os.path.basename(f.filename))
        return jsonify({"ok": True, "dataset": data, "active": DS.active_name()})
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 400


@app.route("/api/dataset/save", methods=["POST"])
def dataset_save():
    """整表保存：改单元格、加行删行、改列名都走这里"""
    import dataset as DS
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()
    headers = body.get("headers") or []
    rows = body.get("rows") or []
    source = str(body.get("source") or "").strip() or None
    try:
        data = DS.save(name, headers, rows, source=source)
        return jsonify({"ok": True, "dataset": data})
    except ValueError as ex:
        return jsonify({"ok": False, "error": str(ex)}), 400
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500


@app.route("/api/dataset/active", methods=["POST"])
def dataset_set_active():
    """设为当前数据集 / 取消（name 传空）"""
    import dataset as DS
    name = str((request.get_json(silent=True) or {}).get("name") or "").strip()
    try:
        return jsonify({"ok": True, "active": DS.set_active(name)})
    except ValueError as ex:
        return jsonify({"ok": False, "error": str(ex)}), 400


@app.route("/api/dataset/delete", methods=["POST"])
def dataset_delete():
    """删除数据集（当前数据集会被一并取消）"""
    import dataset as DS
    name = str((request.get_json(silent=True) or {}).get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "请指定要删除的数据集"}), 400
    try:
        DS.delete(name)
        return jsonify({"ok": True, "active": DS.active_name()})
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500


@app.route("/api/variables/seq/reset", methods=["POST"])
def reset_seq_variable():
    """把自增变量重置回起始值（前缀/位数/步长不变，已用次数清零）"""
    name = str((request.get_json(silent=True) or {}).get("name", "")).strip()
    if not name:
        return jsonify({"ok": False, "error": "请输入变量名"}), 400
    desc = seqvars.reset(name)
    if desc is None:
        return jsonify({"ok": False, "error": "该变量不是自增变量"}), 400
    return jsonify({"ok": True, "name": name, "seq": desc})


@app.route("/api/variables/seq/step", methods=["POST"])
def set_seq_variable_step():
    """只改自增变量的步长：不动前缀/位数，也不动当前序号进度"""
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()
    if not name:
        return jsonify({"ok": False, "error": "请输入变量名"}), 400
    try:
        step = int(body.get("step"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "步长只能是整数"}), 400
    if step < 1:
        return jsonify({"ok": False, "error": "步长至少为 1"}), 400
    desc = seqvars.set_step(name, step)
    if desc is None:
        return jsonify({"ok": False, "error": "该变量不是自增变量"}), 400
    return jsonify({"ok": True, "name": name, "seq": desc})


# ═══════════════════════════════════════════════════
#  工作台：待办事项 + 笔记
# ═══════════════════════════════════════════════════

TODOS_FILE = os.path.join(DATA_DIR, "_workbench_todos.json")
NOTES_FILE = os.path.join(DATA_DIR, "_workbench_notes.json")
# 计划分类集合单独存：todos 是数组结构（旧数据），不能塞对象进去，
# 因此分类表另存一个文件，端点也只管分类的增删改
TODO_CATS_FILE = os.path.join(DATA_DIR, "_workbench_todo_cats.json")
# 随心记：右栏速记（与「记事本」的 notes 分开存，不混进笔记列表）
QUICK_NOTES_FILE = os.path.join(DATA_DIR, "_workbench_quick_notes.json")
DEFAULT_TODO_CATS = ["测试", "开发", "日常", "学习", "会议"]


def _load_todos():
    """加载待办列表"""
    if os.path.exists(TODOS_FILE):
        with _file_lock(TODOS_FILE):
            try:
                with open(TODOS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
    return []


def _save_todos(todos):
    """保存待办列表"""
    with _file_lock(TODOS_FILE):
        with open(TODOS_FILE, "w", encoding="utf-8") as f:
            json.dump(todos, f, ensure_ascii=False, indent=2)


def _load_todo_cats():
    """加载计划分类集合（文件缺失时返回内置默认值，不写盘）"""
    if os.path.exists(TODO_CATS_FILE):
        with _file_lock(TODO_CATS_FILE):
            try:
                with open(TODO_CATS_FILE, "r", encoding="utf-8") as f:
                    cats = json.load(f)
                if isinstance(cats, list):
                    return [str(c) for c in cats if str(c).strip()]
            except (json.JSONDecodeError, IOError):
                pass
    return list(DEFAULT_TODO_CATS)


def _save_todo_cats(cats):
    """保存分类集合（去空、去重、保持前端传入的顺序）"""
    clean, seen = [], set()
    for c in cats or []:
        name = str(c).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        clean.append(name)
    with _file_lock(TODO_CATS_FILE):
        with open(TODO_CATS_FILE, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
    return clean


def _load_notes():
    """加载笔记列表"""
    if os.path.exists(NOTES_FILE):
        with _file_lock(NOTES_FILE):
            try:
                with open(NOTES_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
    return []


def _save_notes(notes):
    """保存笔记列表"""
    with _file_lock(NOTES_FILE):
        with open(NOTES_FILE, "w", encoding="utf-8") as f:
            json.dump(notes, f, ensure_ascii=False, indent=2)


@app.route("/api/workbench/todos", methods=["GET"])
def get_todos():
    """获取待办列表"""
    return jsonify({"ok": True, "todos": _load_todos()})


@app.route("/api/workbench/todos", methods=["POST"])
def save_todos():
    """保存待办列表（覆盖写）"""
    todos = (request.get_json(silent=True) or {}).get("todos", [])
    _save_todos(todos)
    return jsonify({"ok": True})


@app.route("/api/workbench/todo-cats", methods=["GET"])
def get_todo_cats():
    """获取计划分类集合"""
    return jsonify({"ok": True, "categories": _load_todo_cats()})


@app.route("/api/workbench/todo-cats", methods=["POST"])
def save_todo_cats():
    """保存分类集合（整体覆盖写；改名/删除对计划的影响由前端一并处理）"""
    cats = (request.get_json(silent=True) or {}).get("categories", [])
    return jsonify({"ok": True, "categories": _save_todo_cats(cats)})


@app.route("/api/workbench/notes", methods=["GET"])
def list_notes():
    """获取笔记列表（摘要）"""
    notes = _load_notes()
    # 返回列表摘要（不含完整 content）
    summary = [{"id": n["id"], "title": n["title"], "updated_at": n["updated_at"]} for n in notes]
    return jsonify({"ok": True, "notes": summary})


@app.route("/api/workbench/notes/<note_id>", methods=["GET"])
def get_note(note_id):
    """获取单条笔记完整内容"""
    notes = _load_notes()
    for n in notes:
        if n["id"] == note_id:
            return jsonify({"ok": True, "note": n})
    return jsonify({"ok": False, "error": "笔记不存在"}), 404


@app.route("/api/workbench/notes", methods=["POST"])
def create_or_update_note():
    """创建或更新笔记
    传入 id 则更新，不传则新建。
    body: {id?, title, content}
    """
    from datetime import datetime as _dt
    data = (request.get_json(silent=True) or {})
    note_id = data.get("id")
    title = data.get("title", "").strip() or "无标题"
    content = data.get("content", "")
    now = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
    notes = _load_notes()

    if note_id:
        # 更新已有笔记
        for n in notes:
            if n["id"] == note_id:
                n["title"] = title
                n["content"] = content
                n["updated_at"] = now
                _save_notes(notes)
                return jsonify({"ok": True, "id": note_id, "updated_at": now})
        # id 不存在则新建
        note_id = None

    # 新建笔记
    import uuid
    new_id = uuid.uuid4().hex[:12]
    new_note = {"id": new_id, "title": title, "content": content, "created_at": now, "updated_at": now}
    notes.insert(0, new_note)
    _save_notes(notes)
    return jsonify({"ok": True, "id": new_id, "updated_at": now})


@app.route("/api/workbench/notes/<note_id>", methods=["DELETE"])
def delete_note(note_id):
    """删除笔记"""
    notes = _load_notes()
    notes = [n for n in notes if n["id"] != note_id]
    _save_notes(notes)
    return jsonify({"ok": True})


def _load_quick_notes():
    """加载随心记（文件缺失 / 格式损坏时返回空列表）"""
    if os.path.exists(QUICK_NOTES_FILE):
        with _file_lock(QUICK_NOTES_FILE):
            try:
                with open(QUICK_NOTES_FILE, "r", encoding="utf-8") as f:
                    notes = json.load(f)
                if isinstance(notes, list):
                    return notes
            except (json.JSONDecodeError, IOError):
                pass
    return []


def _save_quick_notes(notes):
    """保存随心记：只收 {id, text, created_at}，去空文本、保留前端顺序、上限 200 条"""
    import uuid
    from datetime import datetime as _dt
    now = _dt.now().strftime("%Y-%m-%d %H:%M")
    clean, seen = [], set()
    for n in notes or []:
        if not isinstance(n, dict):
            continue
        text = str(n.get("text") or "").strip()
        if not text:
            continue
        nid = str(n.get("id") or "").strip() or ("qn_" + uuid.uuid4().hex[:10])
        if nid in seen:
            continue
        seen.add(nid)
        clean.append({"id": nid, "text": text, "created_at": str(n.get("created_at") or now)})
        if len(clean) >= 200:
            break
    with _file_lock(QUICK_NOTES_FILE):
        with open(QUICK_NOTES_FILE, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
    return clean


@app.route("/api/workbench/quick-notes", methods=["GET"])
def get_quick_notes():
    """获取随心记列表（最新的在前）"""
    return jsonify({"ok": True, "notes": _load_quick_notes()})


@app.route("/api/workbench/quick-notes", methods=["POST"])
def save_quick_notes():
    """保存随心记（整体覆盖写；单条增删由前端重组数组后提交）"""
    notes = (request.get_json(silent=True) or {}).get("notes", [])
    return jsonify({"ok": True, "notes": _save_quick_notes(notes)})


# ═══════════════════════════════════════════════════
#  代理 Mock：接收抓包工具转发的请求，返回配置的 mock 数据
# ═══════════════════════════════════════════════════

MOCK_PROXY_FILE = os.path.join(DATA_DIR, "_mock_proxy.json")

# 代理请求日志（写入文件，按天分割）
LOGS_DIR = os.path.join(_app_root, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

def _cleanup_old_logs():
    """启动时删除 7 天前的日志文件"""
    try:
        now = time.time()
        for f in os.listdir(LOGS_DIR):
            if f.startswith("proxy_") and f.endswith(".json"):
                fp = os.path.join(LOGS_DIR, f)
                if os.path.isfile(fp) and (now - os.path.getmtime(fp)) > 7 * 86400:
                    os.remove(fp)
    except Exception as e:
        print(f"[Logs] 清理旧日志失败: {e}")

def _save_proxy_log(log_entry):
    """将日志追加到当天的日志文件"""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = os.path.join(LOGS_DIR, f"proxy_{today}.json")
        with _file_lock(log_file):
            logs = []
            if os.path.exists(log_file):
                with open(log_file, "r", encoding="utf-8") as f:
                    try:
                        logs = json.load(f)
                    except (json.JSONDecodeError, ValueError):
                        logs = []
            logs.insert(0, log_entry)
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(logs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Logs] 写入代理日志失败: {e}")

def _load_proxy_logs(date=None):
    """加载指定日期的日志，默认今天"""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    log_file = os.path.join(LOGS_DIR, f"proxy_{date}.json")
    if os.path.exists(log_file):
        with _file_lock(log_file):
            with open(log_file, "r", encoding="utf-8") as f:
                try:
                    return json.load(f)
                except (json.JSONDecodeError, ValueError):
                    return []
    return []

def _get_log_dates():
    """获取所有日志日期列表"""
    dates = []
    for f in os.listdir(LOGS_DIR):
        if f.startswith("proxy_") and f.endswith(".json"):
            date = f[6:-5]  # proxy_YYYY-MM-DD.json
            dates.append(date)
    return sorted(dates, reverse=True)

# 启动时清理旧日志
_cleanup_old_logs()


from fnmatch import fnmatchcase as _fnmatchcase


def _mock_path_match(pattern, path):
    """Mock 路径匹配：规则含 * 时按通配（如 /api/user/*），否则精确匹配

    代理 Mock 与抓包改包 Mock 共用同一语义（addon.py 的 _mock_path_match 与本函数保持一致）。
    """
    pattern = pattern or ""
    if "*" in pattern:
        return _fnmatchcase(path or "", pattern)
    return pattern == (path or "")


def _load_mock_proxy():
    if os.path.exists(MOCK_PROXY_FILE):
        with _file_lock(MOCK_PROXY_FILE):
            try:
                with open(MOCK_PROXY_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return {"mappings": []}
    return {"mappings": []}


def _save_mock_proxy(data):
    with _file_lock(MOCK_PROXY_FILE):
        with open(MOCK_PROXY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


@app.route("/api/mock-proxy", methods=["GET"])
def api_get_mock_proxy():
    """获取代理 Mock 配置"""
    return jsonify(_load_mock_proxy())


@app.route("/api/mock-proxy", methods=["POST"])
def api_save_mock_proxy():
    """保存代理 Mock 配置"""
    data = (request.get_json(silent=True) or {})
    _save_mock_proxy(data)
    return jsonify({"ok": True})


@app.route("/api/mock-proxy/delay", methods=["GET", "POST"])
def api_mock_proxy_delay():
    """获取/设置 Mock 响应延时（毫秒）"""
    config = _load_mock_proxy()
    if request.method == "GET":
        return jsonify({"delay_ms": config.get("delay_ms", 0)})
    else:
        data = (request.get_json(silent=True) or {})
        delay_ms = data.get("delay_ms", 0)
        if not isinstance(delay_ms, (int, float)) or delay_ms < 0:
            return jsonify({"error": "delay_ms 必须是非负数字"}), 400
        config["delay_ms"] = int(delay_ms)
        _save_mock_proxy(config)
        return jsonify({"ok": True, "delay_ms": config["delay_ms"]})


@app.route("/proxy/", defaults={"subpath": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
@app.route("/proxy/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
def mock_proxy(subpath):
    """代理 Mock 入口：匹配配置的路径，返回对应的 mock 响应（也支持远程映射）"""
    config = _load_mock_proxy()
    # 构造完整路径（以 / 开头）
    path = "/" + subpath if subpath else "/"
    method = request.method.upper()
    
    # 记录日志：收到转发请求
    client_ip = request.remote_addr
    print(f"[Mock Proxy] 收到请求: {method} {path} (来自: {client_ip})")

    # 查找匹配的 mapping（按路径匹配）
    matched = None
    matched_id = None
    for m in config.get("mappings", []):
        if _mock_path_match(m.get("path"), path):
            matched = m
            matched_id = m.get("id")
            break
    
    # 如果路径映射未匹配，尝试匹配远程映射的 local_path
    if not matched:
        remote_config = _load_remote_proxy()
        for rm in remote_config.get("mappings", []):
            if not rm.get("enabled", True):
                continue  # 禁用映射不参与兜底匹配（与其他代理链路一致）
            local_path = rm.get("local_path", "")
            # 支持 /proxy/test1 和 /test1 两种格式
            if local_path == path or local_path == "/" + subpath or "/proxy" + local_path == path:
                # 查找对应的 mock 配置
                for m in config.get("mappings", []):
                    if _mock_path_match(m.get("path"), local_path):
                        matched = m
                        matched_id = m.get("id")
                        break
                if matched:
                    break

    # 记录匹配结果
    if matched:
        print(f"[Mock Proxy] 匹配成功: {matched.get('path')} -> 返回 Mock")
    else:
        print(f"[Mock Proxy] 未匹配到任何映射")

    # 获取当前激活的响应（支持多响应报文）
    resp_status = 200
    resp_headers = {}
    resp_body = ""
    resp_name = ""
    resp_delay_ms = 0  # 响应级别的延时配置
    active = {}  # 当前激活的响应对象
    status_message = ""  # 自定义状态消息

    if matched:
        responses = matched.get("responses")
        active_idx = matched.get("active_response", 0)
        if responses and isinstance(responses, list) and len(responses) > 0:
            idx = min(active_idx, len(responses) - 1)
            active = responses[idx]
            resp_name = active.get("name", "")
            resp_status = active.get("status", 200)
            resp_delay_ms = active.get("delay_ms", 0)  # 状态码后面的延时配置
            resp_headers = active.get("headers") or {}
            resp_body = active.get("body", "")
            status_message = active.get("status_message", "")  # 自定义状态消息
        else:
            # 兼容旧格式：直接取顶层字段
            resp_status = matched.get("status", 200)
            resp_delay_ms = matched.get("delay_ms", 0)
            resp_headers = matched.get("headers") or {}
            resp_body = matched.get("body", "")
            status_message = matched.get("status_message", "")

    # 记录日志（存完整响应体）
    log_body_str = resp_body
    if isinstance(log_body_str, (dict, list)):
        log_body_str = json.dumps(log_body_str, ensure_ascii=False)
    log_entry = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "method": method,
        "path": path,
        "matched": matched is not None,
        "mapping_path": matched.get("path") if matched else None,
        "mapping_id": matched_id,
        "response_name": resp_name,
        "status": resp_status if matched else 404,
        "status_message": status_message if matched else "",
        "response_headers": resp_headers if matched else {},
        "response_body": log_body_str if matched else None,
    }
    _save_proxy_log(log_entry)

    if not matched:
        return jsonify({"error": "No mock mapping found for " + method + " " + path}), 404

    # 应用延时配置（优先使用响应级别的延时，否则使用全局延时）
    delay_ms = resp_delay_ms if resp_delay_ms > 0 else config.get("delay_ms", 0)
    if delay_ms > 0:
        time.sleep(delay_ms / 1000.0)

    # body 如果是 dict/list 则序列化为 JSON 字符串
    if isinstance(resp_body, (dict, list)):
        resp_body = json.dumps(resp_body, ensure_ascii=False)

    resp = make_response(resp_body, resp_status)
    # 支持自定义状态消息（如 "200 自定义成功消息"）
    if status_message:
        resp.status = f"{resp_status} {status_message}"
    for k, v in resp_headers.items():
        resp.headers[k] = v
    # 如果 body 是 JSON，强制设置 Content-Type
    if isinstance(resp_body, str) and resp_body.strip().startswith(('{', '[')):
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
    elif "Content-Type" not in resp.headers and "content-type" not in resp.headers:
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
    return resp


@app.route("/api/mock-proxy/switch-response", methods=["POST"])
def api_switch_proxy_response():
    """切换映射的激活响应报文"""
    data = (request.get_json(silent=True) or {})
    mapping_id = data.get("mapping_id")
    response_index = data.get("index", 0)
    if not mapping_id:
        return jsonify({"error": "missing mapping_id"}), 400
    config = _load_mock_proxy()
    for m in config.get("mappings", []):
        if m.get("id") == mapping_id:
            m["active_response"] = response_index
            _save_mock_proxy(config)
            return jsonify({"ok": True})
    return jsonify({"error": "mapping not found"}), 404


@app.route("/api/mock-proxy/logs", methods=["GET"])
def api_get_proxy_logs():
    """获取代理请求日志（支持按日期查询）"""
    date = request.args.get("date")
    logs = _load_proxy_logs(date)
    return jsonify({"logs": logs, "dates": _get_log_dates()})


@app.route("/api/mock-proxy/logs", methods=["DELETE"])
def api_clear_proxy_logs():
    """清空代理请求日志（删除指定日期或全部）"""
    date = request.args.get("date")
    if date:
        log_file = os.path.join(LOGS_DIR, f"proxy_{date}.json")
        if os.path.exists(log_file):
            os.remove(log_file)
    else:
        for f in os.listdir(LOGS_DIR):
            if f.startswith("proxy_") and f.endswith(".json"):
                os.remove(os.path.join(LOGS_DIR, f))
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════
#  远程映射：类似 Charles 远程映射，反向代理 + mock
# ═══════════════════════════════════════════════════

REMOTE_PROXY_FILE = os.path.join(DATA_DIR, "_remote_proxy.json")


def _load_remote_proxy():
    if os.path.exists(REMOTE_PROXY_FILE):
        with _file_lock(REMOTE_PROXY_FILE):
            try:
                with open(REMOTE_PROXY_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return {"mappings": []}
    return {"mappings": []}


def _save_remote_proxy(data):
    with _file_lock(REMOTE_PROXY_FILE):
        with open(REMOTE_PROXY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


@app.route("/api/remote-proxy", methods=["GET"])
def api_get_remote_proxy():
    """获取远程映射配置"""
    return jsonify(_load_remote_proxy())


@app.route("/api/remote-proxy", methods=["POST"])
def api_save_remote_proxy():
    """保存远程映射配置"""
    data = (request.get_json(silent=True) or {})
    mappings = data.get("mappings", [])
    
    # 确保所有映射都有 enabled 字段（默认启用）
    for m in mappings:
        if "enabled" not in m:
            m["enabled"] = True
    
    _save_remote_proxy(data)
    
    # 检查是否有任何启用的映射
    has_enabled = any(m.get("enabled", True) for m in mappings)
    print(f"[Debug] 保存配置: {len(mappings)} 个映射, has_enabled={has_enabled}")
    ok = _set_system_proxy(has_enabled)
    
    resp = {"ok": True}
    if has_enabled and not ok:
        resp["proxy_warning"] = "抓包系统代理使用中，Mock 分流代理暂未启用；请先在抓包工具中关闭系统代理"
    return jsonify(resp)


@app.route("/api/ca-cert")
def api_get_ca_cert():
    """下载 CA 证书（不存在时自动生成）"""
    if not os.path.exists(CA_CERT_FILE):
        ensure_ca_cert()
    if os.path.exists(CA_CERT_FILE):
        from flask import make_response
        with open(CA_CERT_FILE, "rb") as f:
            cert_data = f.read()
        resp = make_response(cert_data)
        resp.headers["Content-Type"] = "application/x-x509-ca-cert"
        resp.headers["Content-Disposition"] = 'attachment; filename="api-tester-ca.crt"'
        return resp
    return jsonify({"error": "CA 证书生成失败"}), 500


@app.route("/api/ca-cert-status")
def api_ca_cert_status():
    """检查 CA 证书状态"""
    return jsonify({"exists": os.path.exists(CA_CERT_FILE)})


@app.route("/api/ca-cert/install", methods=["POST"])
def api_ca_cert_install():
    """本机一键安装 CA 证书：拉起 Windows 证书安装向导界面，由用户手动完成安装"""
    if not os.path.exists(CA_CERT_FILE):
        ensure_ca_cert()
    if not os.path.exists(CA_CERT_FILE):
        return jsonify({"ok": False, "error": "CA 证书不存在"}), 404
    import subprocess
    try:
        # 用 start 命令打开 .cer 文件，Windows 会弹出证书查看器，用户点击"安装证书"即可完成
        subprocess.Popen(
            ['start', '', os.path.abspath(CA_CERT_FILE)],
            shell=True
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/ca-cert/qrcode")
def api_ca_cert_qrcode():
    """证书下载二维码：手机扫码直接下载 CA 证书（HTTPS 抓包必需）"""
    return _qr_png(f"http://{LOCAL_IP}:{PORT}/api/ca-cert")


@app.route("/api/pac")
def api_get_pac():
    """生成 PAC 代理自动配置文件（只包含已启用的映射）"""
    config = _load_remote_proxy()
    mappings = config.get("mappings", [])
    
    # 提取已启用的远程主机域名
    domains = set()
    for m in mappings:
        if not m.get("enabled", True):
            continue
        remote_host = m.get("remote_host", "")
        if remote_host.startswith("http://") or remote_host.startswith("https://"):
            from urllib.parse import urlparse
            p = urlparse(remote_host)
            domain = p.hostname or p.netloc or p.path.split("/")[0]
            if domain:
                domains.add(domain)
        elif remote_host:
            domains.add(remote_host)
    
    # 生成 PAC 文件内容（区分 HTTP 和 HTTPS）
    pac_content = '''function FindProxyForURL(url, host) {
    // API Tester 自动生成的 PAC 文件
    // 以下域名走本地代理，其他直连
    
'''
    for domain in sorted(domains):
        pac_content += f'''    if (dnsDomainIs(host, "{domain}")) {{\n        if (url.substring(0, 5) == "https") return "PROXY {LOCAL_IP}:5051";\n        return "PROXY {LOCAL_IP}:{PORT}";\n    }}\n'''
    
    pac_content += '''    
    // 其他所有请求直连
    return "DIRECT";
}
'''
    
    response = make_response(pac_content)
    response.headers["Content-Type"] = "application/x-ns-proxy-autoconfig"
    return response


# 本进程是否曾启用 PAC（用于进程退出时回滚）
_SYSTEM_PROXY_ENABLED_BY_US = False


def _set_system_proxy(enable):
    """自动配置/取消 Windows 系统代理（仅操作 PAC，不动手动代理）

    返回 True=操作成功；False=失败，或因抓包系统代理正在使用而被拒绝。
    """
    global _SYSTEM_PROXY_ENABLED_BY_US
    import winreg
    import time
    # 与抓包系统代理互斥：手动代理指向抓包端口时不能用 PAC 覆盖
    if enable and _capture_proxy_is_ours():
        print("[Proxy] 抓包系统代理正在使用，跳过 PAC 启用（两者互斥）")
        return False
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0, winreg.KEY_ALL_ACCESS
        )
        if enable:
            pac_url = f"http://{LOCAL_IP}:{PORT}/api/pac?t={int(time.time())}"
            winreg.SetValueEx(key, "AutoConfigURL", 0, winreg.REG_SZ, pac_url)
            _SYSTEM_PROXY_ENABLED_BY_US = True
            print(f"[Proxy] PAC 已开启: {pac_url}")
        else:
            try:
                winreg.DeleteValue(key, "AutoConfigURL")
            except FileNotFoundError:
                pass
            _SYSTEM_PROXY_ENABLED_BY_US = False
            print("[Proxy] PAC 已关闭")
        winreg.CloseKey(key)
        return True
    except Exception as e:
        print(f"[Proxy] 配置 PAC 失败: {e}")
        return False


def _get_system_proxy_pac_url():
    """读取注册表当前的 AutoConfigURL（不存在返回空字符串）"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0, winreg.KEY_READ
        )
        try:
            pac_url, _ = winreg.QueryValueEx(key, "AutoConfigURL")
        except FileNotFoundError:
            pac_url = ""
        winreg.CloseKey(key)
        return pac_url or ""
    except Exception as e:
        print(f"[Proxy] 读取 PAC 配置失败: {e}")
        return ""


def _clear_system_proxy():
    """清除注册表 AutoConfigURL；ProxyEnable 置 0（抓包代理使用中则跳过）

    返回 (ok, message)。与抓包系统代理互斥：不误关指向抓包端口的手动代理。
    """
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0, winreg.KEY_ALL_ACCESS
        )
        try:
            winreg.DeleteValue(key, "AutoConfigURL")
        except FileNotFoundError:
            pass
        # 手动代理正指向抓包端口时不要置 0（由抓包工具自身管理其开关与恢复）
        if not _capture_proxy_is_ours():
            try:
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
            except Exception:
                pass
        winreg.CloseKey(key)
        return True, ""
    except Exception as e:
        return False, str(e)


@app.route("/api/system-proxy/status")
def api_system_proxy_status():
    """查询系统代理（PAC）状态"""
    pac_url = _get_system_proxy_pac_url()
    return jsonify({"code": 0, "enabled": bool(pac_url), "pac_url": pac_url})


@app.route("/api/system-proxy/enable", methods=["POST"])
def api_system_proxy_enable():
    """启用系统代理（PAC）"""
    ok = _set_system_proxy(True)
    pac_url = _get_system_proxy_pac_url()
    if not ok or not pac_url:
        if _capture_proxy_is_ours():
            return jsonify({"code": 1, "message": "抓包系统代理正在使用中，请先在抓包工具中关闭「系统代理」开关，再启用 Mock 分流代理（两者互斥）"}), 400
        return jsonify({"code": 1, "message": "启用系统代理失败，无法写入注册表"}), 500
    return jsonify({"code": 0, "enabled": True, "pac_url": pac_url})


@app.route("/api/system-proxy/disable", methods=["POST"])
def api_system_proxy_disable():
    """禁用系统代理：删除 AutoConfigURL 并将 ProxyEnable 置 0"""
    global _SYSTEM_PROXY_ENABLED_BY_US
    ok, msg = _clear_system_proxy()
    if not ok:
        return jsonify({"code": 1, "message": f"禁用系统代理失败：{msg}"}), 500
    _SYSTEM_PROXY_ENABLED_BY_US = False
    return jsonify({"code": 0, "enabled": False})


@app.route("/api/system-proxy/refresh", methods=["POST"])
def api_system_proxy_refresh():
    """强制刷新 PAC：先关闭再开启系统代理，确保 PAC 文件按当前映射重新生成"""
    config = _load_remote_proxy()
    mappings = config.get("mappings", [])
    has_enabled = any(m.get("enabled", True) for m in mappings)
    
    if not has_enabled:
        return jsonify({"code": 1, "message": "没有已启用的映射，无法刷新 PAC"}), 400
    
    # 先关闭再开启，强制 Windows 重新加载 PAC
    _set_system_proxy(False)
    time.sleep(0.5)
    ok = _set_system_proxy(True)
    
    if not ok:
        if _capture_proxy_is_ours():
            return jsonify({"code": 1, "message": "抓包系统代理正在使用中，无法刷新 Mock 分流代理（两者互斥）"}), 400
        return jsonify({"code": 1, "message": "刷新系统代理失败"}), 500

    pac_url = _get_system_proxy_pac_url()
    enabled_count = sum(1 for m in mappings if m.get("enabled", True))
    return jsonify({"code": 0, "enabled": True, "pac_url": pac_url, "enabled_mappings": enabled_count})


# 进程退出时清理 PAC（仅当本进程曾启用时）
def _cleanup_system_proxy_on_exit():
    if _SYSTEM_PROXY_ENABLED_BY_US:
        _clear_system_proxy()
        print("[Proxy] 进程退出，已自动清理 PAC 配置")

import atexit
atexit.register(_cleanup_system_proxy_on_exit)


@app.route("/api/remote-proxy/toggle", methods=["POST"])
def api_toggle_remote_mapping():
    """切换远程映射的启用/禁用状态"""
    data = (request.get_json(silent=True) or {})
    mapping_id = data.get("id")
    enabled = data.get("enabled", True)
    
    config = _load_remote_proxy()
    mappings = config.get("mappings", [])
    
    for m in mappings:
        if m.get("id") == mapping_id:
            m["enabled"] = enabled
            break
    
    _save_remote_proxy(config)
    
    # 检查是否有任何启用的映射
    has_enabled = any(m.get("enabled", True) for m in mappings)
    ok = _set_system_proxy(has_enabled)
    
    resp = {"ok": True, "has_enabled": has_enabled}
    if has_enabled and not ok:
        resp["proxy_warning"] = "抓包系统代理使用中，Mock 分流代理暂未启用；请先在抓包工具中关闭系统代理"
    return jsonify(resp)


@app.route("/remote/", defaults={"subpath": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
@app.route("/remote/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
def remote_proxy(subpath):
    """远程映射反向代理：检查映射配置，匹配则返回 mock，否则转发到真实服务器"""
    config = _load_remote_proxy()
    mock_config = _load_mock_proxy()

    # 解析 subpath: 格式为 <protocol>/<host>/<original_path>
    # 例如: https/hotel-manage-test.jinjianghotels.com.cn/api/core-service/role
    if not subpath:
        return jsonify({"error": "请指定协议、主机和路径，如 /remote/https/example.com/api/test"}), 400

    # 分离协议、主机和路径
    parts = subpath.split("/", 2)
    if len(parts) < 2:
        return jsonify({"error": "格式错误，应为 /remote/<protocol>/<host>/<path>"}), 400
    
    protocol = parts[0].lower()  # http 或 https
    host = parts[1]
    original_path = "/" + parts[2] if len(parts) > 2 else "/"
    qs = request.query_string.decode('utf-8', errors='ignore')  # 原始 query 参数（转发时保留）
    
    if protocol not in ("http", "https"):
        return jsonify({"error": "协议必须是 http 或 https"}), 400

    print(f"[Remote Proxy] 收到请求: protocol={protocol}, host={host}, path={original_path}, method={request.method}")
    print(f"[Remote Proxy] 远程映射配置: {config.get('mappings', [])}")

    # 查找远程映射配置
    matched_remote = None
    for m in config.get("mappings", []):
        if not m.get("enabled", True):
            continue  # 禁用映射不参与匹配（与其他代理链路一致）
        remote_host = m.get("remote_host", "")
        remote_path = m.get("remote_path", "")
        
        # 自动解析 remote_host：如果包含 http:// 或 https://，提取协议、主机和路径
        actual_protocol = None  # None 表示匹配任意协议
        actual_host = remote_host
        actual_path = remote_path
        if remote_host.startswith("http://"):
            actual_protocol = "http"
            from urllib.parse import urlparse
            parsed = urlparse(remote_host)
            actual_host = parsed.hostname or parsed.netloc or parsed.path.split("/")[0]
            if parsed.path and parsed.path != "/":
                actual_path = parsed.path + (remote_path or "")
        elif remote_host.startswith("https://"):
            actual_protocol = "https"
            from urllib.parse import urlparse
            parsed = urlparse(remote_host)
            actual_host = parsed.hostname or parsed.netloc or parsed.path.split("/")[0]
            if parsed.path and parsed.path != "/":
                actual_path = parsed.path + (remote_path or "")
        
        print(f"[Remote Proxy] 检查映射: protocol={actual_protocol}, actual_host={actual_host}, actual_path={actual_path}")
        # 匹配协议（如果配置了协议，必须匹配；否则匹配任意协议）
        protocol_match = actual_protocol is None or actual_protocol == protocol
        # 匹配主机和路径（域名按完整段匹配，避免 example.com 误命中 example.com.other）
        host_cmp = host.lower()
        actual_host_cmp = (actual_host or "").lower()
        host_match = bool(actual_host_cmp) and (host_cmp == actual_host_cmp or host_cmp.endswith("." + actual_host_cmp))
        if protocol_match and host_match:
            if not actual_path or original_path.rstrip('/') == actual_path.rstrip('/'):
                matched_remote = m
                print(f"[Remote Proxy] 匹配成功: {m}")
                break

    if matched_remote:
        # 有映射配置，返回本地 mock
        local_path = matched_remote.get("local_path", "/")
        method = request.method.upper()
        print(f"[Remote Proxy] 查找本地 mock: local_path={local_path}, method={method}")
        print(f"[Remote Proxy] Mock 配置: {mock_config.get('mappings', [])}")

        # 在 mock 配置中查找（先精确匹配，再前缀匹配）
        matched_mock = None
        for m in mock_config.get("mappings", []):
            if m.get("path", "") == local_path:
                matched_mock = m
                print(f"[Remote Proxy] 精确匹配成功: {m}")
                break
        if not matched_mock:
            for m in mock_config.get("mappings", []):
                mock_path = m.get("path", "")
                if mock_path and local_path.startswith(mock_path):
                    matched_mock = m
                    print(f"[Remote Proxy] 前缀匹配成功: {m}")
                    break
        
        print(f"[Remote Proxy] 最终 matched_mock: {matched_mock is not None}")

        if matched_mock:
            responses = matched_mock.get("responses")
            active_idx = matched_mock.get("active_response", 0)
            resp_delay_ms = 0
            status_message = ""
            resp_name = ""
            if responses and isinstance(responses, list) and len(responses) > 0:
                idx = min(active_idx, len(responses) - 1)
                active = responses[idx]
                resp_name = active.get("name", "")
                resp_status = active.get("status", 200)
                resp_delay_ms = active.get("delay_ms", 0)
                resp_headers = active.get("headers") or {}
                resp_body = active.get("body", "")
                status_message = active.get("status_message", "")
            else:
                resp_status = matched_mock.get("status", 200)
                resp_delay_ms = matched_mock.get("delay_ms", 0)
                resp_headers = matched_mock.get("headers") or {}
                resp_body = matched_mock.get("body", "")
                status_message = matched_mock.get("status_message", "")

            if isinstance(resp_body, (dict, list)):
                resp_body = json.dumps(resp_body, ensure_ascii=False)

            # 应用延时配置（优先响应级别，否则全局）
            delay_ms = resp_delay_ms if resp_delay_ms > 0 else mock_config.get("delay_ms", 0)
            if delay_ms > 0:
                time.sleep(delay_ms / 1000.0)

            resp = make_response(resp_body, resp_status)
            # 支持自定义状态消息（如 "200 自定义成功消息"）
            if status_message:
                resp.status = f"{resp_status} {status_message}"
            for k, v in resp_headers.items():
                resp.headers[k] = v
            # 如果 body 是 JSON，强制设置 Content-Type
            if isinstance(resp_body, str) and resp_body.strip().startswith(('{', '[')):
                resp.headers["Content-Type"] = "application/json; charset=utf-8"
            elif "Content-Type" not in resp.headers and "content-type" not in resp.headers:
                resp.headers["Content-Type"] = "application/json; charset=utf-8"
            resp.headers["X-Mock-Source"] = "remote-mapping"
            print(f"[Remote Proxy] 返回 Mock 数据")
            # 记录请求日志，便于在「代理 Mock」页面查看命中记录
            _save_proxy_log({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "method": method,
                "path": host + original_path + (("?" + qs) if qs else ""),
                "matched": True,
                "mapping_path": matched_mock.get("path"),
                "mapping_id": matched_mock.get("id"),
                "response_name": resp_name,
                "status": resp_status,
                "status_message": status_message,
                "response_headers": resp_headers,
                "response_body": resp_body if isinstance(resp_body, str) else None,
            })
            return resp

        # 映射命中但本地未配置对应 mock：记录未命中日志，便于排查"为什么没 mock"
        _save_proxy_log({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "method": method,
            "path": host + original_path + (("?" + qs) if qs else ""),
            "matched": False,
            "mapping_path": matched_remote.get("remote_path"),
            "mapping_id": matched_remote.get("id"),
            "response_name": None,
            "status": 404,
            "status_message": "",
            "response_headers": {},
            "response_body": None,
        })

    # 没有映射配置或 mock 未匹配，转发到真实服务器（保留原始 query 参数）
    target_url = f"{protocol}://{host}{original_path}" + (("?" + qs) if qs else "")
    print(f"[Remote Proxy] 转发到真实服务器: {target_url}")
    try:
        # 转发请求
        headers = {k: v for k, v in request.headers if k.lower() != 'host'}
        resp = req_lib.request(
            method=request.method,
            url=target_url,
            headers=headers,
            data=request.get_data(),
            timeout=10,
            allow_redirects=True,
            verify=False
        )
        # 返回真实响应
        response = make_response(resp.content, resp.status_code)
        for k, v in resp.headers.items():
            if k.lower() not in ('transfer-encoding', 'connection', 'content-encoding'):
                response.headers[k] = v
        response.headers["X-Mock-Source"] = "real-server"
        return response
    except Exception as e:
        return jsonify({"error": "转发失败: " + str(e)}), 502


# ═══════════════════════════════════════════════════
#  HTTP 代理拦截（Charles 风格）
# ═══════════════════════════════════════════════════

# ── 公网访问面收敛：外网只放行手机互传页与其接口 ─────────────
# 必须注册在下面的代理转发钩子之前：经外网隧道访问时 Host 是公网域名，
# 否则会被 handle_proxy_request 当成"浏览器代理请求"转发出去（表现为 502）。
@app.before_request
def _guard_public_exposure():
    """经公网地址访问时只允许手机互传页 / 静态资源 / 互传接口，其余一律拒绝"""
    if not _is_external_request():
        return None
    path = request.path or ""
    if path.startswith("/api/transfer/") or path.startswith("/static/") \
            or path in ("/mobile", "/favicon.ico"):
        return None
    return jsonify({"ok": False, "error": "该功能仅限局域网或本机访问"}), 403


@app.before_request
def handle_proxy_request():
    """处理 HTTP 代理请求：浏览器配置代理后，请求会带完整 URL"""
    # 代理请求判定：请求行为绝对 URI 形式（如 POST http://host/path HTTP/1.1）
    # 不能用 request.url 兜底判断——本地请求的 request.url 同样带域名前缀
    raw_uri = request.environ.get('REQUEST_URI', '') or request.environ.get('RAW_URI', '') or ''
    is_absolute_proxy = raw_uri.startswith('http://') or raw_uri.startswith('https://')

    # 本地 API 路由不走代理逻辑，直接放行
    # 绝对 URI 的代理请求除外：外部接口路径也可能以 /api/ 开头（如 /api/core-service/...），
    # 若按本地接口放行，会因匹配不到 Flask 路由而返回 404「接口不存在」
    # /mobile 与 / 是本应用自有页面：经外网隧道访问时 Host 是公网域名，
    # 若按代理请求转发会被转发回公网域名（自环 → 502）
    local_path = request.path
    if not is_absolute_proxy and (local_path.startswith(('/api/', '/static/', '/favicon.ico'))
                                  or local_path in ('/mobile', '/')):
        return None  # 本地接口/页面，跳过代理检查
    
    # CONNECT 请求应该由 HTTPS 代理（端口 5051）处理，这里拒绝
    if request.method == 'CONNECT':
        return jsonify({"error": "CONNECT should go to HTTPS proxy port 5051"}), 400
    
    # 完整 URL：代理请求使用请求行中的绝对 URI，其余使用 request.url
    full_url = raw_uri if is_absolute_proxy else request.url
    
    if not full_url.startswith(f"http://{LOCAL_IP}:{PORT}") and not full_url.startswith("http://127.0.0.1:{0}".format(PORT)) and not full_url.startswith("http://localhost:{0}".format(PORT)):
        # 这是代理请求，解析目标主机
        from urllib.parse import urlparse
        parsed = urlparse(full_url)
        target_host = parsed.hostname
        if not target_host:
            return None  # 畸形 URL（无主机名），交给本地路由处理
        target_path = parsed.path or "/"
        target_protocol = parsed.scheme or "http"
        req_uri = target_path + (("?" + parsed.query) if parsed.query else "")
        
        # 查找远程映射配置
        config = _load_remote_proxy()
        mock_config = _load_mock_proxy()
        
        matched_remote = None
        host_matched_remote = None  # 域名命中但路径不符的映射（用于未命中日志）
        for m in config.get("mappings", []):
            if not m.get("enabled", True):
                continue  # 禁用映射不参与匹配（与 HTTPS 隧道逻辑保持一致）
            remote_host = m.get("remote_host", "")
            remote_path = m.get("remote_path", "")
            
            # 解析 remote_host（hostname 会剥离端口，避免 http://host:8080 配置失效）
            actual_protocol = None
            actual_host = remote_host
            actual_path = remote_path
            if remote_host.startswith("http://"):
                actual_protocol = "http"
                from urllib.parse import urlparse as up2
                p = up2(remote_host)
                actual_host = p.hostname or p.netloc or p.path.split("/")[0]
                if p.path and p.path != "/":
                    actual_path = p.path + (remote_path or "")
            elif remote_host.startswith("https://"):
                actual_protocol = "https"
                from urllib.parse import urlparse as up2
                p = up2(remote_host)
                actual_host = p.hostname or p.netloc or p.path.split("/")[0]
                if p.path and p.path != "/":
                    actual_path = p.path + (remote_path or "")
            
            # 匹配协议和主机（域名按完整段匹配，避免 example.com 误命中 example.com.other）
            protocol_match = actual_protocol is None or actual_protocol == target_protocol
            host_match = bool(actual_host) and (target_host == actual_host or target_host.endswith("." + actual_host))
            if protocol_match and host_match:
                # 匹配主机和路径（精确匹配，避免同一域名下多个接口被意外 mock）
                if not actual_path or target_path.rstrip('/') == actual_path.rstrip('/'):
                    matched_remote = m
                    print(f"[HTTP Proxy] 匹配成功: {m}")
                    break
                if host_matched_remote is None:
                    host_matched_remote = m
        
        matched_mock = None
        if matched_remote:
            # 返回本地 Mock
            local_path = matched_remote.get("local_path", "/")
            method = request.method.upper()
            
            # 先精确匹配，再前缀匹配（避免短前缀 path 抢先命中）
            for m in mock_config.get("mappings", []):
                if m.get("path", "") == local_path:
                    matched_mock = m
                    break
            if not matched_mock:
                for m in mock_config.get("mappings", []):
                    mock_path = m.get("path", "")
                    if mock_path and local_path.startswith(mock_path):
                        matched_mock = m
                        break
            
            if matched_mock:
                responses = matched_mock.get("responses")
                active_idx = matched_mock.get("active_response", 0)
                resp_delay_ms = 0
                status_message = ""
                resp_name = ""
                if responses and isinstance(responses, list) and len(responses) > 0:
                    idx = min(active_idx, len(responses) - 1)
                    active = responses[idx]
                    resp_name = active.get("name", "")
                    resp_status = active.get("status", 200)
                    resp_delay_ms = active.get("delay_ms", 0)
                    resp_headers = active.get("headers") or {}
                    resp_body = active.get("body", "")
                    status_message = active.get("status_message", "")
                else:
                    resp_status = matched_mock.get("status", 200)
                    resp_delay_ms = matched_mock.get("delay_ms", 0)
                    resp_headers = matched_mock.get("headers") or {}
                    resp_body = matched_mock.get("body", "")
                    status_message = matched_mock.get("status_message", "")
                
                if isinstance(resp_body, (dict, list)):
                    resp_body = json.dumps(resp_body, ensure_ascii=False)
                
                # 应用延时配置（优先响应级别，否则全局）
                delay_ms = resp_delay_ms if resp_delay_ms > 0 else mock_config.get("delay_ms", 0)
                if delay_ms > 0:
                    time.sleep(delay_ms / 1000.0)
                
                resp = make_response(resp_body, resp_status)
                # 支持自定义状态消息（如 "200 自定义成功消息"）
                if status_message:
                    resp.status = f"{resp_status} {status_message}"
                for k, v in resp_headers.items():
                    resp.headers[k] = v
                # 如果 body 是 JSON，强制设置 Content-Type（与其他 mock 入口一致）
                if isinstance(resp_body, str) and resp_body.strip().startswith(('{', '[')):
                    resp.headers["Content-Type"] = "application/json; charset=utf-8"
                elif "Content-Type" not in resp.headers and "content-type" not in resp.headers:
                    resp.headers["Content-Type"] = "application/json; charset=utf-8"
                resp.headers["X-Mock-Source"] = "http-proxy-mapping"
                print(f"[HTTP Proxy] 返回 Mock 数据")
                # 记录请求日志（与 /proxy/ 入口格式一致），便于在「代理 Mock」页面查看命中记录
                _save_proxy_log({
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "method": method,
                    "path": target_host + req_uri,
                    "matched": True,
                    "mapping_path": matched_mock.get("path"),
                    "mapping_id": matched_mock.get("id"),
                    "response_name": resp_name,
                    "status": resp_status,
                    "status_message": status_message,
                    "response_headers": resp_headers,
                    "response_body": resp_body if isinstance(resp_body, str) else None,
                })
                return resp
        
        # 未命中（域名未中/路径不符/未配对应本地 mock）：记录日志便于排查"为什么没 mock"
        miss_remote = matched_remote or host_matched_remote
        if not matched_mock and miss_remote:
            _save_proxy_log({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "method": request.method.upper(),
                "path": target_host + req_uri,
                "matched": False,
                "mapping_path": miss_remote.get("remote_path"),
                "mapping_id": miss_remote.get("id"),
                "response_name": None,
                "status": 404,
                "status_message": "",
                "response_headers": {},
                "response_body": None,
            })
        
        # 未匹配，转发到真实服务器
        print(f"[HTTP Proxy] 转发到真实服务器: {full_url}")
        try:
            headers = {k: v for k, v in request.headers if k.lower() not in ('host', 'proxy-connection')}
            resp = req_lib.request(
                method=request.method,
                url=full_url,
                headers=headers,
                data=request.get_data(),
                timeout=10,
                allow_redirects=True,
                verify=False
            )
            response = make_response(resp.content, resp.status_code)
            for k, v in resp.headers.items():
                if k.lower() not in ('transfer-encoding', 'connection', 'content-encoding'):
                    response.headers[k] = v
            response.headers["X-Mock-Source"] = "real-server"
            return response
        except Exception as e:
            return jsonify({"error": "转发失败: " + str(e)}), 502
    
    return None  # 正常请求，继续处理


# ═══════════════════════════════════════════════════
#  HTTPS CONNECT 隧道处理
# ═══════════════════════════════════════════════════

def handle_connect_tunnel(client_socket, target_host, target_port, client_addr=None):
    """处理 HTTPS CONNECT 隧道：匹配域名后 MITM 拦截，按路径决定是否返回 mock"""
    client_info = f"{client_addr[0]}:{client_addr[1]}" if client_addr else "unknown"
    config = _load_remote_proxy()
    mock_config = _load_mock_proxy()

    # 1. 检查域名是否匹配任何远程映射
    matched_remote = None
    matched_actual_path = ""
    for m in config.get("mappings", []):
        if not m.get("enabled", True):
            continue
        remote_host = m.get("remote_host", "")
        remote_path = m.get("remote_path", "")
        actual_protocol = None
        actual_host = remote_host
        actual_path = remote_path
        if remote_host.startswith("https://"):
            actual_protocol = "https"
            from urllib.parse import urlparse
            p = urlparse(remote_host)
            actual_host = p.hostname or p.netloc or p.path.split("/")[0]
            if p.path and p.path != "/":
                actual_path = p.path + (remote_path or "")
        elif remote_host.startswith("http://"):
            actual_protocol = "http"
            from urllib.parse import urlparse
            p = urlparse(remote_host)
            actual_host = p.hostname or p.netloc or p.path.split("/")[0]
            if p.path and p.path != "/":
                actual_path = p.path + (remote_path or "")

        protocol_match = actual_protocol is None or actual_protocol == "https"
        host_match = bool(actual_host) and (target_host == actual_host or target_host.endswith("." + actual_host))
        if protocol_match and host_match:
            matched_remote = m
            matched_actual_path = actual_path
            break

    # 2. 域名没匹配 → 纯隧道转发
    if not matched_remote:
        try:
            target_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            target_socket.settimeout(10)
            target_socket.connect((target_host, target_port))
            client_socket.send(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            import threading
            def forward(src, dst):
                try:
                    while True:
                        data = src.recv(4096)
                        if not data:
                            break
                        dst.send(data)
                except Exception:
                    pass  # 隧道转发中连接关闭属正常情况
                finally:
                    src.close()
                    dst.close()
            threading.Thread(target=forward, args=(client_socket, target_socket)).start()
            threading.Thread(target=forward, args=(target_socket, client_socket)).start()
        except Exception as e:
            try:
                client_socket.send(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except Exception as _e:
                print(f"[CONNECT] 发送 502 失败: {_e}")
            finally:
                client_socket.close()
        return

    # 3. 域名匹配了 → MITM 拦截，查看路径
    try:
        # 生成域名证书（内存缓存）
        cert_pem, key_pem = generate_domain_cert(target_host)
        if not cert_pem:
            raise Exception(f"无法生成证书: {target_host}")

        # 告诉客户端连接已建立
        client_socket.send(b"HTTP/1.1 200 Connection Established\r\n\r\n")

        # 用域名证书包装客户端 socket（写临时文件供 load_cert_chain 使用，用完即删）
        import ssl as _ssl
        import tempfile
        ssl_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".pem", prefix="mitm_")
        try:
            os.write(tmp_fd, cert_pem + b"\n" + key_pem)
            os.close(tmp_fd)
            ssl_ctx.load_cert_chain(certfile=tmp_path)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        ssl_client = ssl_ctx.wrap_socket(client_socket, server_side=True)
        ssl_client.settimeout(10)

        # 读取解密后的 HTTP 请求（先收头部）
        decrypted = b""
        while b"\r\n\r\n" not in decrypted:
            chunk = ssl_client.recv(4096)
            if not chunk:
                break
            decrypted += chunk
            if len(decrypted) > 65536:
                break

        if not decrypted:
            ssl_client.close()
            return

        # 解析 Content-Length，确保收完请求体
        header_end = decrypted.find(b"\r\n\r\n")
        headers_raw = decrypted[:header_end].decode('utf-8', errors='ignore')
        body_start = decrypted[header_end + 4:]
        content_length = 0
        for line in headers_raw.split('\r\n'):
            if line.lower().startswith('content-length:'):
                content_length = int(line.split(':', 1)[1].strip())
                break
        # 继续接收请求体
        while len(body_start) < content_length:
            chunk = ssl_client.recv(4096)
            if not chunk:
                break
            body_start += chunk
            decrypted += chunk

        request_body = body_start

        # 解析请求行
        request_line = headers_raw.split('\r\n')[0]
        req_parts = request_line.split(' ')
        method = req_parts[0]
        request_path = req_parts[1] if len(req_parts) > 1 else "/"
        # 路径匹配需剥离 query 参数（request_path 来自请求行，可能带 ?xxx）
        from urllib.parse import urlsplit
        request_path_only = urlsplit(request_path).path or "/"

        # 4. 检查路径是否匹配（query 参数不参与比较）
        path_match = not matched_actual_path or request_path_only.rstrip('/') == matched_actual_path.rstrip('/')

        # 5. 查找本地 Mock（仅路径匹配时；先精确匹配，再前缀匹配）
        matched_mock = None
        if path_match:
            local_path = matched_remote.get("local_path", "/")
            for m in mock_config.get("mappings", []):
                if m.get("path", "") == local_path:
                    matched_mock = m
                    break
            if not matched_mock:
                for m in mock_config.get("mappings", []):
                    mock_path = m.get("path", "")
                    if mock_path and local_path.startswith(mock_path):
                        matched_mock = m
                        break

        # 记录日志（仅匹配时）
        if matched_mock:
            log_entry = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "method": method,
                "path": target_host + request_path,
                "matched": True,
                "mapping_path": matched_actual_path,
                "mapping_id": matched_remote.get("id"),
                "response_name": None,
                "status": None,
                "status_message": "",
                "response_headers": {},
                "response_body": None,
            }

            responses = matched_mock.get("responses")
            active_idx = matched_mock.get("active_response", 0)
            resp_delay_ms = 0
            resp_status = 200
            resp_headers = {}
            resp_body = ""
            status_message = ""
            if responses and isinstance(responses, list) and len(responses) > 0:
                idx = min(active_idx, len(responses) - 1)
                active = responses[idx]
                resp_status = active.get("status", 200)
                resp_delay_ms = active.get("delay_ms", 0)
                resp_headers = active.get("headers") or {}
                resp_body = active.get("body", "")
                status_message = active.get("status_message", "")
            else:
                resp_status = matched_mock.get("status", 200)
                resp_delay_ms = matched_mock.get("delay_ms", 0)
                resp_headers = matched_mock.get("headers") or {}
                resp_body = matched_mock.get("body", "")
                status_message = matched_mock.get("status_message", "")

            if isinstance(resp_body, (dict, list)):
                resp_body = json.dumps(resp_body, ensure_ascii=False)
            if isinstance(resp_body, str):
                resp_body = resp_body.encode('utf-8')

            # 应用延时配置（优先响应级别，否则全局）
            delay_ms = resp_delay_ms if resp_delay_ms > 0 else mock_config.get("delay_ms", 0)
            if delay_ms > 0:
                time.sleep(delay_ms / 1000.0)

            # 构建 HTTP 响应
            resp_headers.setdefault("Content-Type", "application/json; charset=utf-8")
            resp_headers["X-Mock-Source"] = "https-proxy-mock"
            # 使用自定义状态消息（如果有）
            status_line = f"HTTP/1.1 {resp_status} {status_message}" if status_message else f"HTTP/1.1 {resp_status} OK"
            http_resp = status_line + "\r\n"
            for k, v in resp_headers.items():
                http_resp += f"{k}: {v}\r\n"
            http_resp += f"Content-Length: {len(resp_body)}\r\n"
            http_resp += "Connection: close\r\n\r\n"
            ssl_client.send(http_resp.encode('utf-8') + resp_body)

            log_entry["status"] = resp_status
            log_entry["status_message"] = status_message
            log_entry["response_headers"] = resp_headers
            log_entry["response_body"] = resp_body.decode('utf-8', errors='ignore')[:5000]

            _save_proxy_log(log_entry)
        else:
            # 路径不匹配或未配置本地 mock：记录未命中日志，便于排查"为什么没 mock"
            _save_proxy_log({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "method": method,
                "path": target_host + request_path,
                "matched": False,
                "mapping_path": matched_actual_path,
                "mapping_id": matched_remote.get("id"),
                "response_name": None,
                "status": 404,
                "status_message": "",
                "response_headers": {},
                "response_body": None,
            })
            # 6b. 路径不匹配 → 用 requests 库转发到真实服务器
            try:
                fwd_headers = {}
                for line in headers_raw.split('\r\n')[1:]:
                    if ':' in line:
                        k, v = line.split(':', 1)
                        k, v = k.strip(), v.strip()
                        if k.lower() not in ('host', 'proxy-connection', 'content-length'):
                            fwd_headers[k] = v

                real_resp = req_lib.request(
                    method=method,
                    url=f"https://{target_host}{request_path}",
                    headers=fwd_headers,
                    data=request_body if request_body else None,
                    timeout=10,
                    allow_redirects=True,
                    verify=False
                )

                # 构建 HTTP 响应
                http_resp = f"HTTP/1.1 {real_resp.status_code} OK\r\n"
                for k, v in real_resp.headers.items():
                    if k.lower() not in ('transfer-encoding', 'connection', 'content-encoding'):
                        http_resp += f"{k}: {v}\r\n"
                http_resp += f"Content-Length: {len(real_resp.content)}\r\n"
                http_resp += "Connection: close\r\n\r\n"
                ssl_client.send(http_resp.encode('utf-8') + real_resp.content)

                pass
            except Exception as fwd_err:
                pass
                err_body = f"Forward failed: {fwd_err}"
                err_resp = f"HTTP/1.1 502 Bad Gateway\r\nContent-Length: {len(err_body)}\r\nConnection: close\r\n\r\n{err_body}"
                ssl_client.send(err_resp.encode('utf-8'))

        try:
            ssl_client.close()
        except Exception:
            pass

    except Exception as e:
        print(f"[CONNECT] 隧道处理异常: {type(e).__name__}: {e}")
        try:
            client_socket.send(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        except Exception:
            pass
        finally:
            try:
                client_socket.close()
            except Exception:
                pass


# ═══════════════════════════════════════════════════
#  文件互传
# ═══════════════════════════════════════════════════


def _open_dir_in_explorer(path):
    """在资源管理器中打开目录，并尝试置顶窗口"""
    import subprocess
    import ctypes
    target = os.path.abspath(path)
    if not os.path.isdir(target):
        return False
    try:
        os.startfile(target)
    except AttributeError:
        subprocess.Popen(['explorer', target])
    # 尝试置顶：短暂等待后查找 Explorer 窗口并置前
    import time
    time.sleep(0.3)
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, os.path.basename(target) if os.path.basename(target) else target)
        if not hwnd:
            # 尝试用完整路径作为窗口标题查找
            hwnd = user32.FindWindowW(None, target)
        if hwnd:
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    return True


TRANSFER_DIR = os.path.join(_app_root, "transfer")
os.makedirs(TRANSFER_DIR, exist_ok=True)

# ═══ 统一配置文件：data/config.json ═══
# 所有设置类配置（互传保存目录、记录条数上限、设备名称记忆等）统一保存在这一个文件里
DATA_DIR = os.path.join(_app_root, "data")
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")


def _load_config():
    """读取统一配置文件 data/config.json"""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                return cfg
    except Exception as e:
        print(f"  [配置读取失败] {e}")
    return {}


def _save_config(cfg):
    """保存统一配置文件 data/config.json"""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [配置保存失败] {e}")


def _migrate_legacy_config(cfg):
    """迁移旧版分散配置文件到 config.json，迁移后删除旧文件"""
    changed = False
    # 1. 保存目录：data/_transfer_save_dir.txt → transfer.save_dir
    old = os.path.join(DATA_DIR, '_transfer_save_dir.txt')
    if os.path.exists(old):
        try:
            with open(old, 'r', encoding='utf-8') as f:
                d = f.read().strip()
            if d and not cfg.get('transfer', {}).get('save_dir'):
                cfg.setdefault('transfer', {})['save_dir'] = d
                changed = True
        except Exception:
            pass
        try:
            os.remove(old)
        except Exception:
            pass
    # 2. 记录条数上限：data/_transfer_max_items.txt → transfer.max_items
    old = os.path.join(DATA_DIR, '_transfer_max_items.txt')
    if os.path.exists(old):
        try:
            with open(old, 'r', encoding='utf-8') as f:
                v = int(f.read().strip())
            if 10 <= v <= 10000 and not cfg.get('transfer', {}).get('max_items'):
                cfg.setdefault('transfer', {})['max_items'] = v
                changed = True
        except Exception:
            pass
        try:
            os.remove(old)
        except Exception:
            pass
    # 3. 设备名称记忆：data/_device_names.json → device_names
    old = os.path.join(DATA_DIR, '_device_names.json')
    if os.path.exists(old):
        try:
            with open(old, 'r', encoding='utf-8') as f:
                names = json.load(f)
            if isinstance(names, dict) and not cfg.get('device_names'):
                cfg['device_names'] = names
                changed = True
        except Exception:
            pass
        try:
            os.remove(old)
        except Exception:
            pass
    if changed:
        _save_config(cfg)
    return cfg


_cfg = _migrate_legacy_config(_load_config())

# 互传保存目录（配置于 config.json 的 transfer.save_dir，默认下载目录）
_transfer_save_dir = os.path.expanduser("~/Downloads")
_d = _cfg.get('transfer', {}).get('save_dir', '')
if _d and os.path.isdir(_d):
    _transfer_save_dir = _d

# 内存存储：设备列表和消息/文件记录
_transfer_devices = {}  # device_id -> {name, connected_at, last_seen}
_transfer_devices_lock = threading.Lock()  # 设备字典读写锁，仅包字典操作与保存调用，不包网络/文件 IO
_transfer_items = []    # [{id, type, device_id, device_name, filename, filepath, text, size, time}]
_TRANSFER_MAX_SIZE = 200 * 1024 * 1024  # 整包上传单文件上限 200MB

# SSE 事件推送：device_id -> list of queue
_sse_queues = {}
_sse_queues_lock = threading.Lock()  # SSE 队列字典并发访问锁
import queue as _queue_module

# 分片上传（断点续传）：临时分片目录 + 总大小上限
_TRANSFER_CHUNK_DIR = os.path.join(TRANSFER_DIR, 'parts')
_TRANSFER_MAX_CHUNK_TOTAL = 2 * 1024 * 1024 * 1024  # 分片上传单文件上限 2GB
CHUNK_SIZE = 4 * 1024 * 1024  # 分片大小 4MB（与前端一致）
# 超时未完成分片清理（30 分钟无 mtime 更新 → 放弃，避免占着服务器盘 + 将 uploading 记为 failed）
_TRANSFER_CHUNK_TIMEOUT = 30 * 60
# merge 并发互斥：进程内维护"正在合并"的 file_id 集合，防止同一 file_id 并发 merge 产生重复文件/记录
# 锁序约定：先占 merge 锁，再按既有锁序 items(_transfer_items_lock) → io(_transfer_io_lock)，避免死锁
_transfer_merging_guard = threading.Lock()
_transfer_merging_ids = set()
# 源文件默认保留 7 天（超出后记录保留、文件删，避免转移目录腾空）
_TRANSFER_RETENTION_DAYS = 7


def _get_chunk_dir(file_id):
    """单个上传任务的分片存放目录：transfer/parts/<file_id>/<index>"""
    return os.path.join(_TRANSFER_CHUNK_DIR, file_id)


def _validate_file_id(file_id):
    """校验 file_id 是否非法：返回 True 表示非法（长度限制 + 禁止路径分隔符/穿越字符）"""
    return len(file_id) > 128 or '/' in file_id or '\\' in file_id or '..' in file_id or '\x00' in file_id


# 同名设备合并窗口（秒）：手机端设备号是浏览器指纹算出来的，换浏览器（微信内置 / 系统浏览器）
# 或指纹里的特征变动都会算出新 ID，旧记录超过这么久没心跳（已离线）就让新 ID 接管，
# 否则设备列表会攒出一串同名条目。可通过 config.json 的 transfer.same_name_merge_seconds 调整
_TRANSFER_SAME_NAME_OFFLINE = 120
try:
    _v = int(_cfg.get('transfer', {}).get('same_name_merge_seconds', 120))
    if 5 <= _v <= 86400:
        _TRANSFER_SAME_NAME_OFFLINE = _v
except Exception:
    pass


def _adopt_same_name_offline(device_id, device_name, now):
    """同名且已离线的旧记录让位给新设备号（调用方必须已持有 _transfer_devices_lock）
    返回被接管的旧 ID 列表；同名设备还在线时不动它（可能是真的两台同名手机）"""
    if not device_name or device_name == device_id or device_name == '电脑':
        return []
    adopted = []
    for old_id, info in list(_transfer_devices.items()):
        if old_id == device_id or old_id == 'desktop':
            continue
        if str(info.get('name') or '') != device_name:
            continue
        try:
            last_seen = float(info.get('last_seen') or 0)
        except (TypeError, ValueError):
            last_seen = 0
        if now - last_seen <= _TRANSFER_SAME_NAME_OFFLINE:
            continue
        del _transfer_devices[old_id]
        adopted.append(old_id)
    return adopted


def _adopt_same_fingerprint_offline(device_id, fp, now):
    """同一部手机换了浏览器 / 换了访问域名（localStorage 不互通）会算出新 ID，
    指纹相同就把旧记录收拢过来（调用方必须已持有 _transfer_devices_lock）
    返回被接管的旧 ID 列表；同指纹但还有连接在活跃（多标签页）时不动"""
    if not fp:
        return []
    adopted = []
    for old_id, info in list(_transfer_devices.items()):
        if old_id == device_id or old_id == 'desktop':
            continue
        if str(info.get('fp') or '') != fp:
            continue
        try:
            last_seen = float(info.get('last_seen') or 0)
        except (TypeError, ValueError):
            last_seen = 0
        if now - last_seen <= _TRANSFER_SAME_NAME_OFFLINE:
            continue
        del _transfer_devices[old_id]
        adopted.append(old_id)
    return adopted


def _reassign_transfer_items(old_id, new_id):
    """把旧设备号的历史互传记录改挂到新设备号上（保留条目，不丢消息）"""
    if not old_id or old_id == new_id:
        return
    changed = False
    with _transfer_items_lock:
        for it in _transfer_items:
            if it.get('device_id') == old_id:
                it['device_id'] = new_id
                changed = True
            if it.get('target_device_id') == old_id:
                it['target_device_id'] = new_id
                changed = True
        if changed:
            _persist_transfer_items()


def _touch_transfer_device(device_id, device_name, connected_time):
    """更新/注册设备信息（新设备或名称变化时持久化并通知 PC 端刷新；心跳类更新不写盘）"""
    device_name = str(device_name or '')[:64]  # 统一入口：类型归一 + 限长 64
    now = time.time()
    changed = False
    adopted = []
    with _transfer_devices_lock:
        if device_id not in _transfer_devices:
            adopted = _adopt_same_name_offline(device_id, device_name, now)
            _transfer_devices[device_id] = {
                'name': device_name,
                'connected_at': connected_time,
                'last_seen': now,
            }
            _save_transfer_devices()
            changed = True
        else:
            _transfer_devices[device_id]['last_seen'] = now
            if device_name and device_name != _transfer_devices[device_id].get('name'):
                _transfer_devices[device_id]['name'] = device_name
                _save_transfer_devices()  # 名称变化时持久化
                changed = True
            # 本机在线时顺手收拾同名离线记录（不等到下次算出新 ID 才合并，否则旧条目会永久残留）
            adopted = _adopt_same_name_offline(device_id, _transfer_devices[device_id].get('name'), now)
            if adopted:
                _save_transfer_devices()
                changed = True
    for old_id in adopted:  # 被接管的旧记录：历史互传记录改挂到新设备，列表里只留一条
        _reassign_transfer_items(old_id, device_id)
    if changed:
        _notify_device_changed()
    return now


def _clean_stale_chunks():
    """清理超时未完成的分片上传（启动时调一次，后动上传时也会调）
    超时阈值 = _TRANSFER_CHUNK_TIMEOUT (30 分钟) — 超过该时间未更新的分片视为放弃，
    同时将同 file_id 的 uploading 记录转为 failed"""
    try:
        now = time.time()
        cleaned = 0
        if os.path.isdir(_TRANSFER_CHUNK_DIR):
            for name in os.listdir(_TRANSFER_CHUNK_DIR):
                p = os.path.join(_TRANSFER_CHUNK_DIR, name)
                try:
                    if now - os.path.getmtime(p) > _TRANSFER_CHUNK_TIMEOUT:
                        shutil.rmtree(p, ignore_errors=True)
                        # 同 file_id 的 uploading 记录标记为 failed
                        _mark_uploading_as_failed(name, '上传超时（30分钟未完成）')
                        cleaned += 1
                except Exception:
                    pass
        # 无分片的 uploading 记录：用户在首个分片到达前取消/关页，parts/<file_id> 目录从未创建，
        # 上面的分片扫描扫不到 → 记录永久滞留。这里按记录创建时间兜底标记为 failed
        try:
            stale_ids = []
            with _transfer_items_lock:
                for it in list(_transfer_items):
                    if it.get('type') == 'uploading' and now - (it.get('created_at') or 0) > _TRANSFER_CHUNK_TIMEOUT:
                        stale_ids.append(it.get('file_id', ''))
            # 锁外检查目录并标记（_mark_uploading_as_failed 内部会再取 items 锁）
            for fid in stale_ids:
                if fid and not os.path.isdir(_get_chunk_dir(fid)):
                    _mark_uploading_as_failed(fid, '上传超时（30分钟未完成）')
                    cleaned += 1
        except Exception:
            pass
        if cleaned:
            print(f"  [分片清理] 清理 {cleaned} 个超时分片任务")
    except Exception:
        pass


def _mark_uploading_as_failed(file_id, error_msg):
    """将同 file_id 的 uploading 记录转为 failed (供超时清理调用)"""
    try:
        with _transfer_items_lock:
            for it in _transfer_items:
                if it.get('file_id') == file_id and it.get('type') == 'uploading':
                    it['type'] = 'failed'
                    it['error'] = error_msg
                    it['time'] = datetime.now().strftime('%H:%M:%S')
                    _persist_transfer_items()
                    return
    except Exception:
        pass


def _create_transfer_item(device_id, device_name, target_device_id, original_name, filepath, actual_size, file_id):
    """创建互传记录（含视频编码检测/自动转码），整包上传与分片合并共用"""
    ext = os.path.splitext(original_name)[1]
    # 获取磁盘上实际保存的文件名
    saved_name = os.path.basename(filepath)
    item = {
        'id': file_id,
        'file_id': file_id,  # 暴露原 file_id 给前端做重复检测/断点续传/重试匹配
        'type': _get_file_icon(original_name),
        'source': 'phone' if device_id != 'desktop' else 'desktop',
        'device_id': device_id,
        'device_name': device_name,
        'target_device_id': target_device_id,
        'filename': original_name,  # 原始文件名（用于显示和下载）
        'saved_name': saved_name,  # 磁盘上实际保存的文件名
        'filepath': filepath,
        'size': actual_size,
        'time': datetime.now().strftime('%H:%M:%S'),
        'created_at': time.time(),
    }
    # 视频编码检测：H.265 浏览器不能直接播放，若安装了 ffmpeg 则自动后台转码为 H.264
    if item['type'] == 'video':
        try:
            codec = _detect_video_codec(filepath)
            item['codec'] = codec
            if codec == 'h265' and _ffmpeg_available():
                item['transcode_status'] = 'processing'
                threading.Thread(target=_transcode_to_h264, args=(item,), daemon=True).start()
            # 生成视频缩略图（用于列表预览）
            if _ffmpeg_available():
                threading.Thread(target=_generate_video_thumbnail, args=(item,), daemon=True).start()
        except Exception:
            pass
    with _transfer_items_lock:
        _transfer_items.insert(0, item)
        _persist_transfer_items()
    _notify_new_item(item)  # SSE 推送：两端即时刷新并提示（替代轮询）
    return item

# 设备列表持久化：data/_transfer_devices.json
# 设备不自动清理（离线也不清理），重启服务后恢复展示，由用户在界面手动单个删除
_transfer_devices_file = os.path.join(DATA_DIR, '_transfer_devices.json')


def _save_transfer_devices():
    """持久化设备字典。注意：调用方必须已持有 _transfer_devices_lock（锁内调保存，避免重入）"""
    try:
        with open(_transfer_devices_file, 'w', encoding='utf-8') as f:
            json.dump(_transfer_devices, f, ensure_ascii=False)
    except Exception as e:
        print(f"  [设备持久化失败] {e}")


def _load_transfer_devices():
    try:
        if os.path.exists(_transfer_devices_file):
            with open(_transfer_devices_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                with _transfer_devices_lock:
                    _transfer_devices.update(data)
                    _count = len(_transfer_devices)
                print(f"  [设备恢复] {_count} 台设备")
    except Exception as e:
        print(f"  [设备恢复失败] {e}")

# ═══ 互传记录持久化：data/_transfer_items.json ═══
_transfer_items_file = os.path.join(DATA_DIR, '_transfer_items.json')
_transfer_io_lock = threading.Lock()  # 持久化写锁，防止多线程并发写文件
_transfer_items_lock = threading.Lock()  # 列表增删改锁，保护 _transfer_items 并发安全
# 记录条数上限（配置于 config.json 的 transfer.max_items），超出自动清理最旧的记录及文件
_TRANSFER_MAX_ITEMS = 500
try:
    _v = int(_cfg.get('transfer', {}).get('max_items', 500))
    if 10 <= _v <= 10000:
        _TRANSFER_MAX_ITEMS = _v
except Exception:
    pass


def _load_transfer_max_items():
    """从统一配置文件读取记录条数上限"""
    global _TRANSFER_MAX_ITEMS
    try:
        v = int(_load_config().get('transfer', {}).get('max_items', _TRANSFER_MAX_ITEMS))
        if 10 <= v <= 10000:
            _TRANSFER_MAX_ITEMS = v
    except Exception:
        pass


def _save_transfer_max_items(v):
    cfg = _load_config()
    cfg.setdefault('transfer', {})['max_items'] = v
    _save_config(cfg)


def _load_transfer_retention_days():
    """从统一配置文件读取文件保留天数（默认 7 天，范围 1~365）"""
    global _TRANSFER_RETENTION_DAYS
    try:
        v = int(_load_config().get('transfer', {}).get('retention_days', _TRANSFER_RETENTION_DAYS))
        if 1 <= v <= 365:
            _TRANSFER_RETENTION_DAYS = v
    except Exception:
        pass


def _save_transfer_retention_days(v):
    cfg = _load_config()
    cfg.setdefault('transfer', {})['retention_days'] = v
    _save_config(cfg)
_thumb_dir = os.path.join(DATA_DIR, 'thumbs')  # 图片缩略图缓存目录


def _get_thumb_path(file_id):
    """图片缩略图缓存路径"""
    return os.path.join(_thumb_dir, file_id + '_thumb.jpg')


def _remove_item_files(item):
    """删除记录关联的文件（源文件/转码产物/缩略图），文件不存在时忽略（仅自动清理超限记录时使用）"""
    paths = set()
    if item.get('filepath'):
        paths.add(item['filepath'])
    if item.get('h264_path'):
        paths.add(item['h264_path'])
    paths.add(_get_thumb_path(item['id']))
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def _remove_item_thumb(item):
    """清除聊天记录时只删除缩略图缓存，不删除源文件"""
    try:
        tp = _get_thumb_path(item['id'])
        if tp and os.path.exists(tp):
            os.remove(tp)
    except Exception:
        pass


def _cleanup_item_chunks(item):
    """清理未完成上传的分片目录（记录有 file_id 且有未合并分片时）"""
    fid = item.get('file_id', '')
    if not fid:
        return
    # 只清理 uploading/failed 状态的记录（正常文件已合并过，不会留有分片）
    if item.get('type') not in ('uploading', 'failed'):
        return
    d = _get_chunk_dir(fid)
    if os.path.isdir(d):
        for _attempt in range(3):
            try:
                shutil.rmtree(d)
                print(f"  [分片清理] 清理未完成分片: {fid}")
                return
            except Exception as _e:
                time.sleep(0.3)


def _trim_transfer_items():
    """记录条数超限时，清理最旧的记录及其文件（保留最近 N 条）"""
    if len(_transfer_items) <= _TRANSFER_MAX_ITEMS:
        return
    removed = _transfer_items[_TRANSFER_MAX_ITEMS:]
    del _transfer_items[_TRANSFER_MAX_ITEMS:]
    for it in removed:
        _remove_item_files(it)
    print(f"  [互传清理] 超出上限，清理 {len(removed)} 条旧记录及文件")


def _cleanup_expired_files(retention_days=None):
    """清理超过保留天数的源文件（记录保留为“源文件已过期”状态）
    - 默认保留天数 = _TRANSFER_RETENTION_DAYS (7天)
    - 记录项加 'expired': true 标记供前端展示区别
    - 为避免死锁：独立写文件，不调用 _persist_transfer_items()（那个需要 _transfer_io_lock）
    """
    if retention_days is None:
        retention_days = _TRANSFER_RETENTION_DAYS
    if not os.path.isdir(TRANSFER_DIR):
        return 0
    threshold = time.time() - retention_days * 86400
    n = 0
    dirty = False
    try:
        with _transfer_items_lock:
            for it in _transfer_items:
                if it.get('type') not in ('file', 'image', 'video'):
                    continue
                if it.get('expired'):
                    continue
                fp = it.get('filepath')
                if not fp or not os.path.exists(fp):
                    it['expired'] = True
                    dirty = True
                    continue
                try:
                    if os.path.getmtime(fp) < threshold:
                        try:
                            os.remove(fp)
                            n += 1
                        except Exception:
                            pass
                        it['expired'] = True
                        # 转码产物也一起清
                        if it.get('h264_path') and os.path.exists(it['h264_path']):
                            try: os.remove(it['h264_path'])
                            except Exception: pass
                        dirty = True
                except Exception:
                    pass
            if dirty:
                # 独立写文件，避免重入锁
                try:
                    with _transfer_io_lock:
                        with open(_transfer_items_file, 'w', encoding='utf-8') as f:
                            json.dump(_transfer_items, f, ensure_ascii=False)
                except Exception:
                    pass
    except Exception:
        pass
    if n:
        print(f"  [过期清理] 清理 {n} 个过期源文件（保留 {retention_days} 天）")
    return n


def _persist_transfer_items():
    """将互传记录保存到 data/_transfer_items.json（重启后恢复）
    注：不在此调过期清理（会与调用者的 _transfer_items_lock 死锁），由调用方在锁外显式调"""
    try:
        with _transfer_io_lock:
            _trim_transfer_items()  # 超限时先清理最旧记录
            with open(_transfer_items_file, 'w', encoding='utf-8') as f:
                json.dump(_transfer_items, f, ensure_ascii=False)
    except Exception as e:
        print(f"  [互传持久化失败] {e}")

def _load_transfer_items():
    """启动时恢复互传记录：文件已丢失的记录跳过，转码产物存在则补全状态"""
    try:
        if not os.path.exists(_transfer_items_file):
            return
        with open(_transfer_items_file, 'r', encoding='utf-8') as f:
            items = json.load(f)
        restored = []
        for it in items:
            if it.get('type') in ('image', 'video', 'file'):
                fp = it.get('filepath', '')
                h264 = it.get('h264_path', '')
                if not fp or not os.path.exists(fp):
                    # 原始文件已删（转码后清理），但转码产物还在 → 用 H.264 恢复
                    if it.get('type') == 'video' and h264 and os.path.exists(h264):
                        it['filepath'] = h264
                        it['h264_path'] = h264
                        it['transcode_status'] = 'done'
                    else:
                        continue  # 文件已被清理，跳过该记录
                elif it.get('type') == 'video' and h264 and os.path.exists(h264):
                    # 转码产物存在：只保留转码文件，源文件不保存（启动时清理历史遗留双份）
                    it['transcode_status'] = 'done'
                    if os.path.abspath(fp) != os.path.abspath(h264):
                        try:
                            if os.path.exists(fp):
                                os.remove(fp)
                                print(f"  [转码清理] 已删除源文件 {os.path.basename(fp)}")
                        except Exception:
                            pass
                        it['filepath'] = h264
                        it['h264_path'] = h264
            restored.append(it)
        _transfer_items.extend(restored)
        if restored:
            print(f"  [互传恢复] {len(restored)} 条记录")
        # 恢复/清理可能改变了记录内容（filepath 指向转码产物、删除源文件），同步写回
        _persist_transfer_items()
    except Exception as e:
        print(f"  [互传恢复失败] {e}")

_load_transfer_items()
_load_transfer_devices()
_load_transfer_max_items()
_load_transfer_retention_days()  # 从 config 读保留天数（与 max_items 一致）
_clean_stale_chunks()
_cleanup_expired_files()  # 启动时也清一次过期文件


def _periodic_cleanup_worker():
    """后台定时清理：每 5 分钟跑一次超时分片 + 过期文件清理
    解决 30 分钟超时后需要重启服务才生效的问题
    """
    while True:
        try:
            time.sleep(300)  # 5 分钟
            _clean_stale_chunks()
            _safe_cleanup_expired()
        except Exception:
            pass


def _on_exit_cleanup():
    """进程退出时清理（保证下次启动时是干净状态）"""
    try: _clean_stale_chunks()
    except: pass
    try: _safe_cleanup_expired()
    except: pass
atexit.register(_on_exit_cleanup)

# 启动后台定时清理线程（daemon=True 不随主进程退出，仅 atexit 钩子做最后清理）
threading.Thread(target=_periodic_cleanup_worker, daemon=True).start()

# 设备名称记忆持久化（按 device_id 记住名称，存于 config.json 的 device_names）
_device_names_cache = {}

def _load_device_names():
    global _device_names_cache
    cfg = _load_config()
    names = cfg.get('device_names', {})
    if isinstance(names, dict):
        # 卫生清理：key 超长（>128）的条目静默丢弃，有丢弃时回写一次清洗后的文件
        cleaned = {k: v for k, v in names.items() if len(k) <= 128}
        if len(cleaned) != len(names):
            _device_names_cache = cleaned
            _save_device_names()
        else:
            _device_names_cache = names

def _save_device_names():
    cfg = _load_config()
    cfg['device_names'] = _device_names_cache
    _save_config(cfg)

_load_device_names()


def _gen_transfer_id():
    import random as _r
    return 'tf_' + str(int(time.time())) + '_' + ''.join(_r.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=6))


_WINDOWS_RESERVED_NAMES = {'con', 'prn', 'aux', 'nul'} | {f'com{i}' for i in range(1, 10)} | {f'lpt{i}' for i in range(1, 10)}


def _get_unique_filepath(directory, original_name):
    """生成唯一的文件路径，使用原始文件名，重名时自动添加序号（仅影响磁盘保存名）"""
    safe_name = original_name.replace('\\', '_').replace('/', '_').replace('\x00', '')
    # 替换 Windows 非法字符
    for ch in ':*?"<>|':
        safe_name = safe_name.replace(ch, '_')
    # 去除首尾空格与尾随点
    safe_name = safe_name.strip().rstrip('.')
    name, ext = os.path.splitext(safe_name)
    # 主名超过 200 字符时截断（扩展名保留）
    if len(name) > 200:
        name = name[:200]
    # 主名去除尾随空格与点（避免 Win32 静默归一化导致覆盖既有文件）
    name = name.rstrip(' .')
    # Windows 保留名按第一个点之前的段判定（修复 con.tar.gz / nul.tar.gz 绕过），不区分大小写
    first_seg = name.split('.', 1)[0]
    if first_seg.lower() in _WINDOWS_RESERVED_NAMES:
        name = '_' + name
    safe_name = name + ext
    if not safe_name:
        safe_name = 'unnamed'
        name, ext = 'unnamed', ''  # 同步重置，保证重名序号退化为 unnamed_1 而非 _1
    filepath = os.path.join(directory, safe_name)
    if not os.path.exists(filepath):
        return filepath, safe_name
    # 重名时添加序号：file.txt -> file_1.txt, file_2.txt...
    counter = 1
    while True:
        new_name = f"{name}_{counter}{ext}"
        filepath = os.path.join(directory, new_name)
        if not os.path.exists(filepath):
            return filepath, new_name
        counter += 1


def _get_file_icon(filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext in ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg'):
        return 'image'
    elif ext in ('.mp4', '.avi', '.mov', '.mkv', '.wmv', '.flv', '.webm'):
        return 'video'
    else:
        return 'file'


def _find_ffmpeg():
    """查找 ffmpeg 可执行文件：优先 PATH，再查常见安装位置（winget 装后需重启 shell 才生效，这里主动探测避免依赖 PATH 传播）"""
    import shutil
    p = shutil.which('ffmpeg')
    if p:
        return p
    import glob
    candidates = []
    # winget 安装位置
    localappdata = os.environ.get('LOCALAPPDATA', '')
    if localappdata:
        pattern = os.path.join(localappdata, 'Microsoft', 'WinGet', 'Packages', 'Gyan.FFmpeg*', '*', 'bin', 'ffmpeg.exe')
        candidates.extend(glob.glob(pattern))
    # 常见手动安装位置
    candidates.append('C:\\ffmpeg\\bin\\ffmpeg.exe')
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def _ffmpeg_available():
    """检测系统是否安装 ffmpeg（用于 H.265转H.264）"""
    return _find_ffmpeg() is not None


def _detect_video_codec(filepath):
    """检测视频编码：h264 / h265 / other
    优先用 ffprobe 精确识别；不可用时回退到二进制扫描 FourCC 标识符"""
    # 优先 ffprobe（最准确，直接读 codec_id）
    try:
        ffmpeg = _find_ffmpeg()
        if ffmpeg:
            # ffprobe 与 ffmpeg 在同一目录下
            ffprobe = os.path.join(os.path.dirname(ffmpeg), 'ffprobe.exe' if sys.platform == 'win32' else 'ffprobe')
            if not os.path.exists(ffprobe):
                ffprobe = 'ffprobe'  # 回退到 PATH 查找
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            r = subprocess.run([ffprobe, '-v', 'quiet', '-select_streams', 'v:0',
                                '-show_entries', 'stream=codec_name', '-of', 'csv=p=0', filepath],
                               capture_output=True, text=True, timeout=30, startupinfo=si,
                               creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            if r.returncode == 0 and r.stdout.strip():
                cn = r.stdout.strip().lower()
                if 'hevc' in cn or 'h265' in cn:
                    return 'h265'
                if 'h264' in cn or 'avc' in cn:
                    return 'h264'
                # ffprobe 可用但编码未知 → 视为可播放，避免误转码
                return 'other'
    except Exception:
        pass
    # 回退：二进制扫描 FourCC
    try:
        prev = b''
        with open(filepath, 'rb') as f:
            while True:
                chunk = f.read(4 * 1024 * 1024)
                if not chunk:
                    break
                buf = prev + chunk
                if b'avc1' in buf or b'avc3' in buf:
                    return 'h264'
                if b'hvc1' in buf or b'hev1' in buf:
                    return 'h265'
                prev = buf[-3:]
        return 'other'
    except Exception:
        return 'other'


def _transcode_to_h264(item):
    """后台线程：将 H.265 视频转码为 H.264（浏览器不支持 H.265 直接播放）"""
    try:
        import subprocess
        ffmpeg = _find_ffmpeg()
        if not ffmpeg:
            item['transcode_status'] = 'failed'
            return
        src = item['filepath']
        dst = os.path.splitext(src)[0] + '_h264.mp4'
        # CRF 15 视觉无损级别，保证转码后画质与原片一致；preset medium 在画质与速度间取得最佳平衡
        cmd = [ffmpeg, '-y', '-i', src,
               '-c:v', 'libx264', '-preset', 'medium', '-crf', '15',
               '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', dst]
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        result = subprocess.run(cmd, capture_output=True, timeout=7200,
            startupinfo=startupinfo,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
            # 转码成功：删除原始 H.265 文件，记录指向 H.264（避免双份占用磁盘）
            try:
                if os.path.exists(item['filepath']) and os.path.abspath(item['filepath']) != os.path.abspath(dst):
                    os.remove(item['filepath'])
            except Exception:
                pass
            item['filepath'] = dst
            item['h264_path'] = dst
            item['transcode_status'] = 'done'
            item.pop('transcode_retries', None)
            _persist_transfer_items()
            print(f"  [转码完成] {item.get('filename', '')} -> H.264（已删除原始文件）")
        else:
            # 失败：自动重试一次，仍失败则标记 failed（可手动重新转码）
            retries = item.get('transcode_retries', 0) + 1
            item['transcode_retries'] = retries
            if retries < 2:
                item['transcode_status'] = 'processing'
                _persist_transfer_items()
                print(f"  [转码重试] {item.get('filename', '')} 第 {retries} 次失败，自动重试")
                threading.Thread(target=_transcode_to_h264, args=(item,), daemon=True).start()
                return
            item['transcode_status'] = 'failed'
            _persist_transfer_items()
            print(f"  [转码失败] {item.get('filename', '')}（已自动重试 1 次）")
    except Exception as e:
        item['transcode_status'] = 'failed'
        print(f"  [转码异常] {e}")


def _generate_video_thumbnail(item):
    """后台线程：为视频生成缩略图（用于列表预览）"""
    try:
        import subprocess
        ffmpeg = _find_ffmpeg()
        if not ffmpeg:
            return
        src = item.get('h264_path') or item.get('filepath')
        if not src or not os.path.exists(src):
            return
        thumb_path = _get_thumb_path(item['id'])
        # 截取第 1 秒的画面作为缩略图，输出为 JPEG
        cmd = [ffmpeg, '-y', '-i', src, '-ss', '1', '-vframes', '1',
               '-vf', 'scale=320:-1', '-f', 'image2', thumb_path]
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        result = subprocess.run(cmd, capture_output=True, timeout=60,
            startupinfo=startupinfo,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode == 0 and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            item['thumb_path'] = thumb_path
            _persist_transfer_items()
            print(f"  [缩略图生成] {item.get('filename', '')}")
            # 通知前端缩略图已就绪
            _notify_thumbnail_ready(item.get('device_id', ''), item['id'])
        else:
            # 如果第 1 秒失败，尝试第 0.5 秒
            cmd2 = [ffmpeg, '-y', '-i', src, '-ss', '0.5', '-vframes', '1',
                    '-vf', 'scale=320:-1', '-f', 'image2', thumb_path]
            result2 = subprocess.run(cmd2, capture_output=True, timeout=60,
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NO_WINDOW)
            if result2.returncode == 0 and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
                item['thumb_path'] = thumb_path
                _persist_transfer_items()
                _notify_thumbnail_ready(item.get('device_id', ''), item['id'])
    except Exception as e:
        print(f"  [缩略图异常] {e}")


@app.route("/api/transfer/retranscode/<file_id>", methods=["POST"])
def api_transfer_retranscode(file_id):
    """手动重新转码（自动重试仍失败后，前端点击重试）"""
    with _transfer_items_lock:
        items_snapshot = list(_transfer_items)
    for item in items_snapshot:
        if item['id'] != file_id:
            continue
        if item.get('type') != 'video':
            return jsonify({'error': '不是视频'}), 400
        if item.get('transcode_status') == 'processing':
            return jsonify({'error': '正在转码中'}), 400
        if item.get('transcode_status') == 'done':
            return jsonify({'error': '已完成转码，无需重试'}), 400
        fp = item.get('filepath', '')
        if not fp or not os.path.exists(fp):
            return jsonify({'error': '文件不存在，无法转码'}), 400
        if item.get('codec') != 'h265':
            return jsonify({'error': '非 H.265 视频，无需转码'}), 400
        if not _ffmpeg_available():
            return jsonify({'error': '未安装 ffmpeg'}), 400
        item['transcode_status'] = 'processing'
        item.pop('transcode_retries', None)
        _persist_transfer_items()
        threading.Thread(target=_transcode_to_h264, args=(item,), daemon=True).start()
        return jsonify({'ok': True})
    return jsonify({'error': 'not found'}), 404


def _qr_png(data):
    """生成二维码 PNG 响应（内容为 data 字符串；依赖 qrcode 库）"""
    try:
        import qrcode
    except ImportError:
        return jsonify({"error": "未安装 qrcode 库，请 pip install qrcode"}), 500
    import io as _io
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = _io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    from flask import Response
    return Response(buf.read(), mimetype='image/png', headers={'Cache-Control': 'no-cache, no-store'})


@app.route("/api/transfer/qrcode")
def api_transfer_qrcode():
    """互传二维码：只有一个码，地址自动选——有公网地址就用它，否则用局域网地址"""
    public_url = ((_load_config().get("transfer", {}) or {}).get("public_url", "") or "").rstrip("/")
    if public_url:
        return _qr_png(public_url + "/mobile")
    return _qr_png(f"http://{LOCAL_IP}:{PORT}/mobile")


@app.route("/api/transfer/config", methods=["GET", "POST"])
def api_transfer_config():
    """互传配置：记录条数上限 (max_items) + 源文件保留天数 (retention_days)"""
    global _TRANSFER_MAX_ITEMS, _TRANSFER_RETENTION_DAYS
    if request.method == "GET":
        return jsonify({'max_items': _TRANSFER_MAX_ITEMS, 'retention_days': _TRANSFER_RETENTION_DAYS})
    data = request.json or {}
    errors = []
    # max_items (可选，可单独设 retention_days)
    if 'max_items' in data:
        try:
            v = int(data.get('max_items', 0))
        except Exception:
            errors.append('max_items 需为整数')
            v = None
        if v is not None and not (10 <= v <= 10000):
            errors.append('max_items 范围 10~10000')
            v = None
        if v is not None:
            _TRANSFER_MAX_ITEMS = v
            _save_transfer_max_items(v)
            with _transfer_items_lock:
                _trim_transfer_items()  # 立即按新上限清理一次（与持久化路径一致：持 items 锁）
            _persist_transfer_items()
            print(f"[Transfer] 记录条数上限已设置为 {v}")
    # retention_days (可选)
    if 'retention_days' in data:
        try:
            v = int(data.get('retention_days', 0))
        except Exception:
            errors.append('retention_days 需为整数')
            v = None
        if v is not None and not (1 <= v <= 365):
            errors.append('retention_days 范围 1~365')
            v = None
        if v is not None:
            _TRANSFER_RETENTION_DAYS = v
            _save_transfer_retention_days(v)
            # 立即按新保留天数清一次
            _safe_cleanup_expired()
            print(f"[Transfer] 源文件保留天数已设置为 {v}")
    if errors and ('max_items' in data or 'retention_days' in data):
        # 只有都失败才返错
        if (('max_items' in data and 'max_items' in str(errors)) and 'retention_days' not in data) or \
           (('retention_days' in data and 'retention_days' in str(errors)) and 'max_items' not in data) or \
           (len(errors) >= 2):
            return jsonify({'error': '; '.join(errors)}), 400
    return jsonify({'ok': True, 'max_items': _TRANSFER_MAX_ITEMS, 'retention_days': _TRANSFER_RETENTION_DAYS})


@app.route("/api/transfer/devices")
def api_transfer_devices():
    # 设备不自动清理（离线也不清理），由用户在界面手动单个删除
    result = []
    with _transfer_devices_lock:
        devices_snapshot = list(_transfer_devices.items())
    with _sse_queues_lock:
        sse_alive = {did for did, qs in _sse_queues.items() if qs}  # 页面长连接还挂着 = 在线
    with _transfer_items_lock:
        items_snapshot = list(_transfer_items)
    for did, info in devices_snapshot:
        # 过滤掉桌面设备（PC 是服务端，不作为设备展示）
        if did == 'desktop' or info.get('name', '') == '电脑':
            continue
        items_count = sum(1 for i in items_snapshot if i['device_id'] == did)
        result.append({
            'device_id': did,
            'name': info.get('name', did),
            'connected_at': info.get('connected_at', ''),
            'last_seen': info.get('last_seen', 0),
            'items_count': items_count,
            # 在线 = 页面长连接还挂着（上线/下线都有 SSE 事件通知刷新），不再靠注册时间猜
            'online': did in sse_alive,
        })
    return jsonify({'devices': result})


@app.route("/api/transfer/device/<device_id>", methods=["DELETE"])
def api_transfer_delete_device(device_id):
    """手动删除单个设备（不影响其互传记录）"""
    if device_id == 'desktop':
        return jsonify({'error': '不能删除桌面设备'}), 400
    with _transfer_devices_lock:
        if device_id not in _transfer_devices:
            return jsonify({'error': '设备不存在'}), 404
        name = _transfer_devices[device_id].get('name', device_id)
        del _transfer_devices[device_id]
        _save_transfer_devices()
    _notify_device_kicked(device_id)  # SSE 推送：被移除的设备立即显示断开（替代轮询检测）
    print(f"[Transfer] 已删除设备 {name}（{device_id}）")
    return jsonify({'ok': True})


@app.route("/api/transfer/register", methods=["POST"])
def api_transfer_register():
    data = request.json or {}
    device_id = data.get('device_id', '')
    device_name = str(data.get('device_name', '') or '')[:64]  # 限长 64 字符
    # 设备指纹（同一台手机换浏览器/换域名也不变）：只用来收拢旧条目，不当身份主键
    fp = str(data.get('fp', '') or '')[:64]
    if fp and not re.match(r'^[A-Za-z0-9_-]{4,64}$', fp):
        fp = ''
    if not device_id:
        return jsonify({'error': 'missing device_id'}), 400
    # device_id 长度限制 + 禁止路径分隔符（防越权/防持久化文件被撑爆）
    if len(device_id) > 64 or '/' in device_id or '\\' in device_id or '\x00' in device_id:
        return jsonify({'error': 'invalid device_id'}), 400
    now = time.time()
    # 如果客户端没传名称，使用之前记住的名称
    if not device_name and device_id in _device_names_cache:
        device_name = _device_names_cache[device_id]
    # 如果客户端传了名称，保存记住它
    if device_name and device_name != device_id:
        _device_names_cache[device_id] = device_name
        _save_device_names()
    with _transfer_devices_lock:
        is_new = device_id not in _transfer_devices
        name_changed = False
        adopted = []
        if device_id in _transfer_devices:
            _transfer_devices[device_id]['last_seen'] = now
            if fp and _transfer_devices[device_id].get('fp') != fp:
                _transfer_devices[device_id]['fp'] = fp
                _save_transfer_devices()  # 指纹变了（换浏览器等）记下来，下次靠它收拢旧条目
            if device_name and device_name != _transfer_devices[device_id].get('name'):
                _transfer_devices[device_id]['name'] = device_name
                _save_transfer_devices()  # 名称变化时持久化
                name_changed = True
            # 本机在线时顺手收拢旧条目：先按指纹（同一台手机换了浏览器/域名），再按名字
            adopted = _adopt_same_fingerprint_offline(device_id, _transfer_devices[device_id].get('fp') or fp, now)
            adopted += _adopt_same_name_offline(device_id, _transfer_devices[device_id].get('name'), now)
            if adopted:
                _save_transfer_devices()
                name_changed = True
        else:
            display_name = device_name or _device_names_cache.get(device_id, device_id)
            adopted = _adopt_same_fingerprint_offline(device_id, fp, now)
            adopted += _adopt_same_name_offline(device_id, display_name, now)
            _transfer_devices[device_id] = {
                'name': display_name,
                'connected_at': datetime.now().strftime('%H:%M'),
                'last_seen': now,
                'fp': fp,
            }
            _save_transfer_devices()
    for old_id in adopted:  # 同名旧记录的历史互传记录改挂到新设备，列表里只留一条
        _reassign_transfer_items(old_id, device_id)
    # 新设备注册或名称变更时通知桌面端刷新设备列表
    if is_new or name_changed:
        _notify_device_changed()
    return jsonify({'ok': True})


@app.route("/api/transfer/reconnect", methods=["POST"])
def api_transfer_reconnect():
    data = request.json or {}
    device_id = data.get('device_id', '')
    with _transfer_devices_lock:
        if not device_id or device_id not in _transfer_devices:
            return jsonify({'error': 'device not found'}), 404
        _transfer_devices[device_id]['last_seen'] = time.time()
        _transfer_devices[device_id]['connected_at'] = datetime.now().strftime('%H:%M')
    return jsonify({'ok': True})


@app.route("/api/transfer/disconnect", methods=["POST"])
def api_transfer_disconnect():
    """PC端主动断开某设备连接，手机端会检测到并显示已断开"""
    data = request.json or {}
    device_id = data.get('device_id', '')
    if not device_id:
        return jsonify({'error': 'missing device_id'}), 400
    removed = False
    with _transfer_devices_lock:
        if device_id in _transfer_devices:
            del _transfer_devices[device_id]
            _save_transfer_devices()
            removed = True
    if removed:
        _notify_device_kicked(device_id)  # SSE 推送：手机端立即显示断开（替代轮询检测）
    return jsonify({'ok': True})


@app.route("/api/transfer/clear-devices", methods=["POST"])
def api_transfer_clear_devices():
    """清除所有已连接设备"""
    with _transfer_devices_lock:
        count = len(_transfer_devices)
        _transfer_devices.clear()
        _save_transfer_devices()
    _notify_device_kicked(all_devices=True)  # SSE 推送：所有手机端立即显示断开（替代轮询检测）
    print(f'[Transfer] 已清除 {count} 台设备')
    return jsonify({'ok': True, 'cleared': count})


@app.route("/api/transfer/check-connection")
def api_transfer_check_connection():
    """手机端检测当前设备是否仍处于连接状态"""
    device_id = request.args.get('device_id', '')
    if not device_id:
        return jsonify({'connected': False, 'error': 'missing device_id'}), 400
    with _transfer_devices_lock:
        connected = device_id in _transfer_devices
    return jsonify({'connected': connected})


@app.route("/api/transfer/save-dir", methods=["GET", "POST"])
def api_transfer_save_dir():
    global _transfer_save_dir
    if request.method == "GET":
        return jsonify({'dir': _transfer_save_dir})
    data = request.json or {}
    new_dir = data.get('dir', '').strip()
    if not new_dir:
        return jsonify({'error': 'dir is required'}), 400
    new_dir = os.path.expanduser(new_dir)
    if not os.path.isdir(new_dir):
        try:
            os.makedirs(new_dir, exist_ok=True)
        except Exception:
            return jsonify({'error': 'directory not found and cannot create'}), 400
    _transfer_save_dir = new_dir
    cfg = _load_config()
    cfg.setdefault('transfer', {})['save_dir'] = new_dir
    _save_config(cfg)
    return jsonify({'ok': True, 'dir': new_dir})


@app.route("/api/transfer/open-source-dir", methods=["POST"])
def api_transfer_open_source_dir():
    """打开视频/图片源文件目录（transfer/）"""
    if _open_dir_in_explorer(TRANSFER_DIR):
        return jsonify({'ok': True, 'dir': os.path.abspath(TRANSFER_DIR)})
    return jsonify({'error': 'directory not found'}), 404


@app.route("/api/transfer/open-save-dir", methods=["POST"])
def api_transfer_open_save_dir():
    if _open_dir_in_explorer(_transfer_save_dir):
        return jsonify({'ok': True, 'dir': os.path.abspath(_transfer_save_dir)})
    return jsonify({'error': 'directory not found'}), 404


@app.route("/api/transfer/pick-dir", methods=["POST"])
def api_transfer_pick_dir():
    global _transfer_save_dir
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        path = filedialog.askdirectory(title="选择保存文件夹", initialdir=_transfer_save_dir if os.path.isdir(_transfer_save_dir) else None)
        root.destroy()
        if path:
            _transfer_save_dir = path
            cfg = _load_config()
            cfg.setdefault('transfer', {})['save_dir'] = path
            _save_config(cfg)
            return jsonify({'ok': True, 'dir': path})
        return jsonify({'ok': False, 'cancelled': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route("/api/transfer/save-file/<file_id>", methods=["POST"])
def api_transfer_save_file(file_id):
    import shutil
    with _transfer_items_lock:
        items_snapshot = list(_transfer_items)
    for item in items_snapshot:
        if item['id'] == file_id and item.get('filepath') and os.path.exists(item['filepath']):
            dest_dir = _transfer_save_dir
            if not os.path.isdir(dest_dir):
                return jsonify({'error': 'save directory not found'}), 400
            # 优先保存 H.264 转码版本（浏览器/播放器都能播），保留原文件名
            src = item['filepath']
            if item.get('h264_path') and os.path.exists(item['h264_path']):
                src = item['h264_path']
            safe_name = _sanitize_filename(item['filename'])  # 防路径穿越
            if not safe_name:
                return jsonify({'error': 'invalid filename'}), 400
            dest_path = os.path.realpath(os.path.join(dest_dir, safe_name))
            if not dest_path.startswith(os.path.realpath(dest_dir) + os.sep):
                return jsonify({'error': 'invalid filename'}), 400
            # avoid overwrite
            if os.path.exists(dest_path):
                base, ext = os.path.splitext(safe_name)
                i = 1
                while os.path.exists(dest_path):
                    dest_path = os.path.join(dest_dir, f"{base}_{i}{ext}")
                    i += 1
            shutil.copy2(src, dest_path)
            return jsonify({'ok': True, 'path': dest_path, 'filename': os.path.basename(dest_path)})
    return jsonify({'error': 'file not found'}), 404


def _sanitize_filename(name):
    """净化文件名：去除路径分隔符、..、控制字符与冒号，保留中文与空格"""
    if not name or not isinstance(name, str):
        return ""
    name = name.replace("..", "")
    name = re.sub(r'[/\\:]', '', name)
    name = re.sub(r'[\x00-\x1f\x7f]', '', name)
    return name.strip()


def _copy_file_to_clipboard(file_path):
    """跨平台复制文件到系统剪贴板（纯系统自带命令，无需额外依赖）"""
    import subprocess
    import platform
    abs_path = os.path.abspath(file_path)
    system = platform.system()

    if system == 'Windows':
        # 使用 PowerShell Set-Clipboard 命令（Windows 10+ / PowerShell 5.0+）
        # 相比 ctypes 直接调用 Win32 API，PowerShell 能正确操作交互用户的剪贴板站
        import subprocess
        ps_cmd = f"Set-Clipboard -LiteralPath '{abs_path.replace(chr(39), chr(39)*2)}'"
        try:
            subprocess.run(
                ['powershell', '-NoProfile', '-WindowStyle', 'Hidden', '-Command', ps_cmd],
                check=True, capture_output=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            return  # 成功
        except Exception as e:
            # PowerShell 方式失败，可能是无 PowerShell 或无权限，降级到 ctypes 直接 API
            pass

        # 降级方案：使用 ctypes 直接调用 Windows API
        import ctypes
        from ctypes import wintypes

        CF_HDROP = 0xF

        class DROPFILES(ctypes.Structure):
            _fields_ = [
                ('pFiles', wintypes.DWORD),
                ('pt', wintypes.POINT),
                ('fNC', wintypes.BOOL),
                ('fWide', wintypes.BOOL),
            ]

        # 文件路径以双 null 结尾（UTF-16）
        path_utf16 = (abs_path + '\0\0').encode('utf-16-le')

        df = DROPFILES()
        df.pFiles = ctypes.sizeof(DROPFILES)
        df.fWide = True

        data = bytes(df) + path_utf16

        if not ctypes.windll.user32.OpenClipboard(None):
            raise RuntimeError('无法打开剪贴板')

        try:
            ctypes.windll.user32.EmptyClipboard()

            hMem = ctypes.windll.kernel32.GlobalAlloc(0x42, len(data))  # GMEM_MOVEABLE | GMEM_ZEROINIT
            if not hMem:
                raise RuntimeError('GlobalAlloc 失败')

            pMem = ctypes.windll.kernel32.GlobalLock(hMem)
            if pMem:
                ctypes.memmove(pMem, data, len(data))
                ctypes.windll.kernel32.GlobalUnlock(hMem)

            if not ctypes.windll.user32.SetClipboardData(CF_HDROP, hMem):
                raise RuntimeError('SetClipboardData 失败')
        finally:
            ctypes.windll.user32.CloseClipboard()

    elif system == 'Darwin':
        # macOS: AppleScript 复制文件到剪贴板
        osa_script = 'set the clipboard to (POSIX file "' + abs_path + '")'
        subprocess.run(['osascript', '-e', osa_script], check=True, capture_output=True, timeout=15)

    elif system == 'Linux':
        # Linux: 优先 wl-copy (Wayland)，降级 xclip (X11)
        file_uri = 'file://' + abs_path.replace('\\', '/')
        try:
            subprocess.run(['wl-copy', '-t', 'text/uri-list', file_uri], check=True, capture_output=True, timeout=10)
        except Exception:
            subprocess.run(
                ['xclip', '-t', 'text/uri-list', '-selection', 'clipboard'],
                input=file_uri, check=True, capture_output=True, timeout=10, text=True
            )


@app.route("/api/transfer/copy-video/<file_id>", methods=["POST"])
def api_transfer_copy_video(file_id):
    """保存视频到目录 + 复制到系统剪贴板，支持跨平台"""
    import shutil
    with _transfer_items_lock:
        items_snapshot = list(_transfer_items)
    for item in items_snapshot:
        if item['id'] == file_id and item.get('filepath') and os.path.exists(item['filepath']):
            dest_dir = _transfer_save_dir
            if not os.path.isdir(dest_dir):
                return jsonify({'error': 'save directory not found'}), 400
            # 优先 H.264 转码版
            src = item['filepath']
            if item.get('h264_path') and os.path.exists(item['h264_path']):
                src = item['h264_path']
            safe_name = _sanitize_filename(item['filename'])  # 防路径穿越
            if not safe_name:
                return jsonify({'error': 'invalid filename'}), 400
            dest_path = os.path.realpath(os.path.join(dest_dir, safe_name))
            if not dest_path.startswith(os.path.realpath(dest_dir) + os.sep):
                return jsonify({'error': 'invalid filename'}), 400
            if os.path.exists(dest_path):
                base, ext = os.path.splitext(safe_name)
                i = 1
                while os.path.exists(dest_path):
                    dest_path = os.path.join(dest_dir, f"{base}_{i}{ext}")
                    i += 1
            shutil.copy2(src, dest_path)
            # 复制到系统剪贴板
            try:
                _copy_file_to_clipboard(dest_path)
                return jsonify({'ok': True, 'path': dest_path, 'filename': os.path.basename(dest_path), 'clipboard': True})
            except Exception as e:
                # 剪贴板失败不阻断，文件已保存
                return jsonify({'ok': True, 'path': dest_path, 'filename': os.path.basename(dest_path), 'clipboard': False, 'clipboard_error': str(e)})
    return jsonify({'error': 'file not found'}), 404


@app.route("/api/transfer/upload", methods=["POST"])
def api_transfer_upload():
    device_id = request.form.get('device_id', 'unknown')
    device_name = request.form.get('device_name', device_id)
    target_device_id = request.form.get('target_device_id', '')
    _touch_transfer_device(device_id, device_name, datetime.now().strftime('%H:%M'))

    files = request.files.getlist('files')
    if not files:
        return jsonify({'error': 'no files'}), 400

    uploaded = []
    skipped = []
    for f in files:
        if not f.filename:
            continue
        # 检查文件大小
        f.seek(0, 2)
        size = f.tell()
        f.seek(0)
        if size > _TRANSFER_MAX_SIZE:
            skipped.append({'filename': f.filename, 'reason': 'file too large'})
            continue
        # 保存文件
        original_name = f.filename  # 保留原始文件名
        file_id = _gen_transfer_id()
        # 使用原始文件名保存，重名时自动添加序号（存到用户设置的保存目录）
        filepath, saved_name = _get_unique_filepath(_transfer_save_dir, original_name)
        f.save(filepath)
        actual_size = os.path.getsize(filepath)
        item = _create_transfer_item(device_id, device_name, target_device_id, original_name, filepath, actual_size, file_id)
        uploaded.append({'id': file_id, 'filename': original_name})
    return jsonify({'ok': True, 'uploaded': uploaded, 'skipped': skipped})


@app.route("/api/transfer/chunk-upload", methods=["GET", "POST"])
def api_transfer_chunk_upload():
    """分片上传：单次接收一个分片（断点续传）；GET 请求返回已上传分片列表（续传检测）"""
    if request.method == "GET":
        # 续传检测：返回已上传的分片索引列表（与 /api/transfer/chunk-status 逻辑一致）
        file_id = request.args.get('file_id', '')
        device_id = request.args.get('device_id', '')
        if not file_id:
            return jsonify({'error': 'missing file_id'}), 400
        if not device_id:
            return jsonify({'error': 'missing device_id'}), 400
        if _validate_file_id(file_id):
            return jsonify({'error': 'invalid file_id'}), 400
        d = _get_chunk_dir(file_id)
        if os.path.isdir(d):
            meta_path = os.path.join(d, '_meta.json')
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, 'r', encoding='utf-8') as _mf:
                        _owner = json.load(_mf).get('device_id', '')
                    if _owner and _owner != device_id:
                        return jsonify({'error': 'forbidden: file_id owned by another device'}), 403
                except Exception:
                    pass
        chunks = []
        if os.path.isdir(d):
            for name in os.listdir(d):
                try:
                    chunks.append(int(name))
                except Exception:
                    pass
        return jsonify({'ok': True, 'chunks': sorted(chunks), 'uploaded': sorted(chunks)})
    # ──── POST：接收分片 ────
    device_id = request.form.get('device_id', 'unknown')
    device_name = request.form.get('device_name', device_id)
    _touch_transfer_device(device_id, device_name, datetime.now().strftime('%H:%M'))
    file_id = request.form.get('file_id', '')
    try:
        chunk_index = int(request.form.get('chunk_index', -1))
        chunk_total = int(request.form.get('chunk_total', 0))
    except Exception:
        return jsonify({'error': 'invalid chunk params'}), 400
    f = request.files.get('file')
    # 注：0 字节文件 f 为空但有 filename，必须检查 filename 是否存在
    if not file_id or not f or not f.filename or chunk_index < 0 or chunk_total <= 0 or chunk_index >= chunk_total:
        return jsonify({'error': 'invalid chunk params'}), 400
    # 分片索引只允许纯数字，防止路径穿越
    if not str(chunk_index).isdigit():
        return jsonify({'error': 'invalid chunk index'}), 400
    # file_id 长度限制 + 禁止路径分隔符（防越权/防文件系统事故）
    if _validate_file_id(file_id):
        return jsonify({'error': 'invalid file_id'}), 400
    try:
        d = _get_chunk_dir(file_id)
        os.makedirs(d, exist_ok=True)
        # 防越权：file_id 已绑定其他设备时拒绝写入（不同设备不能合并同一 file_id 的分片）
        meta_path = os.path.join(d, '_meta.json')
        if os.path.exists(meta_path):
            try:
                with open(meta_path, 'r', encoding='utf-8') as _mf:
                    _meta = json.load(_mf)
                _owner = _meta.get('device_id', '')
                if _owner and _owner != device_id:
                    return jsonify({'error': 'forbidden: file_id owned by another device'}), 403
                # chunk_total 一致性校验：不一致说明是不同文件混用同一 file_id，拒绝写入
                _meta_total = _meta.get('chunk_total')
                if _meta_total is not None and int(_meta_total) != chunk_total:
                    return jsonify({'error': 'chunk_total mismatch'}), 400
            except Exception:
                pass  # meta 读取失败（损坏）时放行，避免影响正常续传（保持既有行为）
        else:
            # 首次上传时绑定设备号
            try:
                with open(meta_path, 'w', encoding='utf-8') as _mf:
                    json.dump({'device_id': device_id, 'chunk_total': chunk_total}, _mf)
            except Exception:
                pass
        # 原子落盘：先写同目录 .tmp 临时文件，校验大小后 os.replace 原子替换，
        # 防止网络中断写一半时 chunk-status 将残缺分片误判为已传
        blob = f.read()
        expected_size = len(blob)
        tmp_path = os.path.join(d, str(chunk_index) + '.tmp')
        final_path = os.path.join(d, str(chunk_index))
        with open(tmp_path, 'wb') as _tf:
            _tf.write(blob)
        if os.path.getsize(tmp_path) != expected_size:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            return jsonify({'error': 'save failed'}), 500
        try:
            os.replace(tmp_path, final_path)
        except Exception as _e:
            # 替换失败时删除 tmp，避免 N.tmp 残留被 chunk-status 之外的逻辑误读
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            print(f"  [分片保存失败] {_e}")
            return jsonify({'error': 'save failed'}), 500
    except Exception as e:
        print(f"  [分片保存失败] {e}")
        return jsonify({'error': 'save failed'}), 500
    return jsonify({'ok': True})


@app.route("/api/transfer/chunk-status")
def api_transfer_chunk_status():
    """查询已接收的分片索引（断点续传：前端跳过已传分片）
    防越权：仅允许分片所有者查进度，否则返 403 避免泄露其他设备的上传状态"""
    file_id = request.args.get('file_id', '')
    device_id = request.args.get('device_id', '')
    if not file_id:
        return jsonify({'error': 'missing file_id'}), 400
    if not device_id:
        return jsonify({'error': 'missing device_id'}), 400
    # file_id 长度限制 + 禁止路径分隔符（与 chunk-upload 对齐）
    if _validate_file_id(file_id):
        return jsonify({'error': 'invalid file_id'}), 400
    d = _get_chunk_dir(file_id)
    # 验证分片所有者是请求的设备
    if os.path.isdir(d):
        meta_path = os.path.join(d, '_meta.json')
        if os.path.exists(meta_path):
            try:
                with open(meta_path, 'r', encoding='utf-8') as _mf:
                    _owner = json.load(_mf).get('device_id', '')
                if _owner and _owner != device_id:
                    return jsonify({'error': 'forbidden: file_id owned by another device'}), 403
            except Exception:
                pass
    chunks = []
    if os.path.isdir(d):
        for name in os.listdir(d):
            try:
                chunks.append(int(name))
            except Exception:
                pass
    # 同时返回 chunks 和 uploaded，兼容前端老代码 (d.chunks) + simple-uploader.js 库 (r.uploaded)
    return jsonify({'ok': True, 'chunks': sorted(chunks), 'uploaded': sorted(chunks)})


@app.route("/api/transfer/chunk-merge", methods=["POST"])
def api_transfer_chunk_merge():
    """分片合并：校验所有分片完整后拼接为完整文件，创建互传记录"""
    data = request.json or {}
    file_id = data.get('file_id', '')
    filename = data.get('filename', '')
    device_id = data.get('device_id', 'unknown')
    device_name = data.get('device_name', device_id)
    device_name = str(device_name or device_id)[:64]  # 类型归一 + 限长 64
    target_device_id = data.get('target_device_id', '')
    try:
        chunk_total = int(data.get('chunk_total', 0))
    except Exception:
        chunk_total = 0
    if not file_id or not filename or chunk_total <= 0:
        return jsonify({'error': 'invalid params'}), 400
    # file_id 长度限制 + 禁止路径分隔符（与 chunk-upload 对齐，防止分片上传后 merge 被绕过）
    if _validate_file_id(file_id):
        return jsonify({'error': 'invalid file_id'}), 400
    # 总大小比对：前端传文件总大小 size（可选字段，缺失/非法则跳过比对）
    expected_size = None
    try:
        expected_size = int(data.get('size'))
    except Exception:
        expected_size = None
    # 并发互斥：同一 file_id 正在合并时直接 409（双击重试/双标签页场景）；finally 中释放
    with _transfer_merging_guard:
        if file_id in _transfer_merging_ids:
            return jsonify({'error': 'merging in progress'}), 409
        _transfer_merging_ids.add(file_id)
    try:
        return _do_chunk_merge(file_id, filename, device_id, device_name, target_device_id, chunk_total, expected_size)
    finally:
        with _transfer_merging_guard:
            _transfer_merging_ids.discard(file_id)


def _do_chunk_merge(file_id, filename, device_id, device_name, target_device_id, chunk_total, expected_size):
    """分片合并实际执行逻辑（调用方必须已持有该 file_id 的 merge 互斥位）"""
    d = _get_chunk_dir(file_id)
    if not os.path.isdir(d):
        return jsonify({'error': 'parts not found'}), 404
    # 防越权：merge 时验证分片目录的所有者设备号与请求 device_id 一致
    meta_path = os.path.join(d, '_meta.json')
    if os.path.exists(meta_path):
        try:
            with open(meta_path, 'r', encoding='utf-8') as _mf:
                _meta = json.load(_mf)
            _owner = _meta.get('device_id', '')
            if _owner and _owner != device_id:
                return jsonify({'error': 'forbidden: file_id owned by another device'}), 403
            # chunk_total 一致性校验：与分片写入时绑定的值不一致则拒绝合并
            _meta_total = _meta.get('chunk_total')
            if _meta_total is not None and int(_meta_total) != chunk_total:
                return jsonify({'error': 'chunk_total mismatch'}), 400
        except Exception:
            pass  # meta 读取失败（损坏）时放行，避免影响正常续传（保持既有行为）
    # 完整性校验：所有分片必须齐全
    parts = set()
    for name in os.listdir(d):
        try:
            parts.add(int(name))
        except Exception:
            pass
    missing = [i for i in range(chunk_total) if i not in parts]
    if missing:
        return jsonify({'error': 'missing chunks', 'missing': missing}), 400
    # 合并前预检：累加各分片实际大小，超限直接拒绝（避免先写盘再删除）
    pre_total_size = 0
    for i in range(chunk_total):
        try:
            pre_total_size += os.path.getsize(os.path.join(d, str(i)))
        except Exception:
            pass
    if pre_total_size > _TRANSFER_MAX_CHUNK_TOTAL:
        return jsonify({'error': 'file too large'}), 400
    # 合并分片
    safe_name = filename.replace('\\', '_').replace('/', '_')  # 仅用于磁盘路径
    ext = os.path.splitext(safe_name)[1]
    new_id = _gen_transfer_id()
    # 使用原始文件名保存，重名时自动添加序号（存到用户设置的保存目录）
    filepath, saved_name = _get_unique_filepath(_transfer_save_dir, filename)
    total_size = 0
    try:
        with open(filepath, 'wb') as out:
            for i in range(chunk_total):
                part = os.path.join(d, str(i))
                with open(part, 'rb') as inp:
                    shutil.copyfileobj(inp, out)
                total_size += os.path.getsize(part)
    except Exception as e:
        import traceback
        print(f"  [分片合并失败] {e}\n{traceback.format_exc()}")
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass
        return jsonify({'error': 'merge failed', 'detail': str(e)[:200]}), 500
    # 总大小比对：写盘完成后立即比对，不符则删除已合并文件并在转存记录/清理分片之前拦截
    if expected_size is not None and total_size != expected_size:
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass
        return jsonify({'error': 'size mismatch'}), 400
    # 合并成功后清理分片目录（Windows 文件句柄释放有延迟，失败则重试几次）
    for _attempt in range(3):
        try:
            shutil.rmtree(d)
            break
        except Exception as _e:
            print(f"  [分片清理重试 {_attempt + 1}] {_e}")
            time.sleep(0.5)
    # 合并成功：若存在同 file_id 的上传中/失败记录 → 原地转为正常记录（保持原位置，重试成功不产生新条目）
    with _transfer_items_lock:
        exist = next((it for it in _transfer_items
                      if it.get('type') in ('uploading', 'failed') and it.get('file_id') == file_id), None)
        if exist is not None:
            exist['type'] = _get_file_icon(filename)
            exist.pop('error', None)
            exist['source'] = 'phone' if device_id != 'desktop' else 'desktop'
            exist['device_id'] = device_id
            exist['device_name'] = device_name
            exist['target_device_id'] = target_device_id
            exist['filename'] = filename  # 保留原始文件名（用于显示和下载）
            exist['size'] = total_size
            exist['filepath'] = filepath
            # 视频类型：触发编码检测 + 缩略图生成（与 _create_transfer_item 对齐）
            if exist['type'] == 'video':
                try:
                    codec = _detect_video_codec(filepath)
                    exist['codec'] = codec
                    if codec == 'h265' and _ffmpeg_available():
                        exist['transcode_status'] = 'processing'
                        threading.Thread(target=_transcode_to_h264, args=(exist,), daemon=True).start()
                    if _ffmpeg_available():
                        threading.Thread(target=_generate_video_thumbnail, args=(exist,), daemon=True).start()
                except Exception:
                    pass
            # time/created_at 保持原值，列表位置不变
        _persist_transfer_items()
    # 锁外调过期清理（避免重入锁死锁）
    _safe_cleanup_expired()
    print(f"  [分片合并完成] {safe_name} ({total_size} 字节)")
    if exist is not None:
        _notify_new_item(exist)  # SSE 推送：续传完成转正，两端即时刷新
        return jsonify({'ok': True, 'id': exist['id'], 'resumed': True})
    item = _create_transfer_item(device_id, device_name, target_device_id, filename, filepath, total_size, new_id)
    return jsonify({'ok': True, 'id': item['id']})


@app.route("/api/transfer/uploading", methods=["POST"])
def api_transfer_uploading():
    """记录一条"上传中"占位记录：上传开始时调用，聊天列表中实时可见；
    同一 file_id 已有上传中/失败记录时不重复插入；成功后自动转为正常记录"""
    data = request.json or {}
    file_id = data.get('file_id', '')
    filename = data.get('filename', '')  # 保留原始文件名
    device_id = data.get('device_id', 'unknown')
    device_name = data.get('device_name', device_id)
    device_name = str(device_name or device_id)[:64]  # 类型归一 + 限长 64
    target_device_id = data.get('target_device_id', '')
    if not file_id or not filename:
        return jsonify({'error': 'missing params'}), 400
    with _transfer_items_lock:
        for it in _transfer_items:
            if it.get('file_id') == file_id and it.get('type') == 'uploading':
                return jsonify({'ok': True, 'id': it['id']})
            if it.get('file_id') == file_id and it.get('type') == 'failed':
                # 重传场景：将已有的失败记录转为上传中（保持同 id 位置不变，避免列表跳变）
                it['type'] = 'uploading'
                it.pop('error', None)
                it['device_id'] = device_id
                it['device_name'] = device_name
                it['target_device_id'] = target_device_id
                it['size'] = data.get('size', 0) or 0
                it['time'] = datetime.now().strftime('%H:%M:%S')
                _persist_transfer_items()
                return jsonify({'ok': True, 'id': it['id'], 'reused': True})
        item = {
            'id': _gen_transfer_id(),
            'type': 'uploading',
            'source': 'phone' if device_id != 'desktop' else 'desktop',
            'device_id': device_id,
            'device_name': device_name,
            'target_device_id': target_device_id,
            'file_id': file_id,
            'filename': filename,
            'size': data.get('size', 0) or 0,
            'time': datetime.now().strftime('%H:%M:%S'),
            'created_at': time.time(),
        }
        _transfer_items.insert(0, item)
        _persist_transfer_items()
    return jsonify({'ok': True, 'id': item['id']})


_IMAGE_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'heic', 'heif', 'svg', 'ico', 'avif'}
_VIDEO_EXTS = {'mp4', 'mov', 'avi', 'mkv', 'webm', '3gp', '3g2', 'm4v', 'flv', 'ts', 'wmv', 'mpeg', 'mpg'}


def _infer_file_type(filename, mime=''):
    """推断文件类型（image/video/file），重试时前端据此选择对应的文件选择器（相册/相册视频/文件管理）"""
    if mime:
        if mime.startswith('image/'):
            return 'image'
        if mime.startswith('video/'):
            return 'video'
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext in _IMAGE_EXTS:
        return 'image'
    if ext in _VIDEO_EXTS:
        return 'video'
    return 'file'


@app.route("/api/transfer/upload-fail", methods=["POST"])
def api_transfer_upload_fail():
    """记录一条上传失败记录（前端上传失败/中断时调用，展示在聊天列表中可点击重试）。
    同一 file_id 只保留最新一条失败记录（替换上传中/失败记录）；重传成功（chunk-merge）后自动清除"""
    data = request.json or {}
    file_id = data.get('file_id', '')
    filename = data.get('filename', '')  # 保留原始文件名
    device_id = data.get('device_id', 'unknown')
    device_name = data.get('device_name', device_id)
    device_name = str(device_name or device_id)[:64]  # 类型归一 + 限长 64
    target_device_id = data.get('target_device_id', '')
    if not file_id or not filename:
        return jsonify({'error': 'missing params'}), 400
    item = {
        'id': _gen_transfer_id(),
        'type': 'failed',
        'source': 'phone' if device_id != 'desktop' else 'desktop',
        'device_id': device_id,
        'device_name': device_name,
        'target_device_id': target_device_id,
        'file_id': file_id,
        'filename': filename,
        'size': data.get('size', 0) or 0,
        'file_type': _infer_file_type(filename, data.get('file_type_mime', '') or ''),
        'error': data.get('error', '') or '上传失败',
        'time': datetime.now().strftime('%H:%M:%S'),
        'created_at': time.time(),
    }
    # 同一文件已存在上传中/失败记录时替换，避免重复堆积
    with _transfer_items_lock:
        _transfer_items[:] = [i for i in _transfer_items
                              if not (i.get('type') in ('uploading', 'failed') and i.get('file_id') == file_id)]
        _transfer_items.insert(0, item)
        _persist_transfer_items()
    return jsonify({'ok': True, 'id': item['id']})


@app.route("/api/transfer/text", methods=["POST"])
def api_transfer_text():
    data = request.json or {}
    text = data.get('text', '').strip()
    device_id = data.get('device_id', 'unknown')
    device_name = str(data.get('device_name', device_id) or device_id)[:64]  # 限长 64 字符
    if not text:
        return jsonify({'error': 'empty text'}), 400
    # 限制文字长度（与前端 maxLength=5000 对齐，防止异常脚本发送巨文撑爆存储/列表渲染）
    if len(text) > 5000:
        return jsonify({'error': 'text too long (max 5000)'}), 400
    # 更新设备信息
    now = time.time()
    with _transfer_devices_lock:
        if device_id not in _transfer_devices:
            _transfer_devices[device_id] = {
                'name': device_name,
                'connected_at': datetime.now().strftime('%H:%M'),
                'last_seen': now,
            }
            _save_transfer_devices()
        else:
            _transfer_devices[device_id]['last_seen'] = now
            if device_name:
                _transfer_devices[device_id]['name'] = device_name
                _save_transfer_devices()  # 名称变化时持久化
    item = {
        'id': _gen_transfer_id(),
        'type': 'text',
        'source': 'phone' if device_id != 'desktop' else 'desktop',
        'device_id': device_id,
        'device_name': device_name,
        'target_device_id': data.get('target_device_id', ''),
        'text': text,
        'size': len(text.encode('utf-8')),
        'time': datetime.now().strftime('%H:%M:%S'),
        'created_at': now,
    }
    with _transfer_items_lock:
        _transfer_items.insert(0, item)
        _persist_transfer_items()
    _notify_new_item(item)  # SSE 推送：两端即时刷新并提示（替代轮询）
    return jsonify({'ok': True, 'id': item['id']})


def _match_transfer_item(item, device_id):
    """互传记录与设备的匹配规则（列表过滤/按设备清空共用）"""
    # 上传中/失败记录：只对发送方可见（接收方不应看到“正在上传…”占位或“上传失败”提示）
    if item.get('type') in ('uploading', 'failed'):
        if not device_id:
            # 桌面端“全部设备”视图：仅保留 desktop 自己的上传中/失败记录（否则续传入口不可达），
            # 仍隐藏其他设备的上传中/失败记录
            return item.get('device_id') == 'desktop'
        return item['device_id'] == device_id
    # 无过滤：匹配全部（已完成的文件/文本/图片/视频）
    if not device_id:
        return True
    # 文本/文件/图片/视频：当前设备发的
    if item['device_id'] == device_id:
        return True
    # 双向检查：接收方也能看到（PC 与手机互传时另一端需要看到接收记录）
    target = item.get('target_device_id', '')
    if target and target == device_id:
        return True
    return False


@app.route("/api/transfer/list")
def api_transfer_list():
    device_id = request.args.get('device_id', '')
    after_id = request.args.get('after', '')

    def _match(item):
        return _match_transfer_item(item, device_id)

    def _with_status(item):
        # 附加文件存在性标记，前端据此显示"文件已删除"而非报错（失败/上传中记录无文件，不检查）
        out = dict(item)
        out['file_exists'] = True
        if item.get('type') not in ('text', 'failed', 'uploading'):
            fp = item.get('filepath', '')
            ok = bool(fp) and os.path.exists(fp)
            # 原始文件缺失但转码产物还在 → 仍视为可用（用 H.264 播放）
            if not ok and item.get('type') == 'video':
                h264 = item.get('h264_path', '')
                ok = bool(h264) and os.path.exists(h264)
                if ok:
                    out['filepath'] = h264
                    out['h264_path'] = h264
                    out['transcode_status'] = 'done'
            out['file_exists'] = ok
        return out

    if after_id:
        result = []
        with _transfer_items_lock:
            items_snapshot = list(_transfer_items)
        for item in items_snapshot:
            if item['id'] == after_id:
                break
            if _match(item):
                result.append(_with_status(item))
        return jsonify({'items': result, 'has_new': len(result) > 0})
    with _transfer_items_lock:
        items_snapshot = list(_transfer_items[:100])
    items = [_with_status(i) for i in items_snapshot if _match(i)]
    return jsonify({'items': items})


@app.route("/api/transfer/download/<file_id>")
def api_transfer_download(file_id):
    with _transfer_items_lock:
        items_snapshot = list(_transfer_items)
    for item in items_snapshot:
        if item['id'] == file_id and item.get('filepath') and os.path.exists(item['filepath']):
            from flask import send_file
            # 优先提供 H.264 转码版本（可播放）
            src = item['filepath']
            if item.get('h264_path') and os.path.exists(item['h264_path']):
                src = item['h264_path']
            return send_file(src, as_attachment=True, download_name=item['filename'])
    return jsonify({'error': 'file not found'}), 404


@app.route("/api/transfer/preview/<file_id>")
def api_transfer_preview(file_id):
    with _transfer_items_lock:
        items_snapshot = list(_transfer_items)
    for item in items_snapshot:
        if item['id'] == file_id and item.get('filepath') and os.path.exists(item['filepath']):
            from flask import send_file
            # 优先提供 H.264 转码版本（浏览器不支持 H.265 直接播放）
            h264 = item.get('h264_path')
            if h264 and os.path.exists(h264):
                return send_file(h264)
            return send_file(item['filepath'])
    return jsonify({'error': 'file not found'}), 404


@app.route("/api/transfer/thumb/<file_id>")
def api_transfer_thumb(file_id):
    """缩略图（列表加载快）：图片用 Pillow 生成，视频用 ffmpeg 生成，缓存到 data/thumbs/"""
    with _transfer_items_lock:
        item = next((i for i in _transfer_items if i['id'] == file_id), None)
    if not item or item.get('type') not in ('image', 'video'):
        return jsonify({'error': 'not found'}), 404
    fp = item.get('filepath', '')
    if not fp or not os.path.exists(fp):
        return jsonify({'error': 'file not found'}), 404
    try:
        if not os.path.isdir(_thumb_dir):
            os.makedirs(_thumb_dir, exist_ok=True)
        tp = _get_thumb_path(file_id)
        if not os.path.exists(tp):
            if item.get('type') == 'image':
                from PIL import Image
                with Image.open(fp) as im:
                    im.thumbnail((320, 320))
                    im.convert('RGB').save(tp, 'JPEG', quality=80)
            elif item.get('type') == 'video':
                # 视频缩略图由后台线程生成，如果还没有则返回 404
                return jsonify({'error': 'thumbnail not ready'}), 404
        from flask import send_file
        return send_file(tp, mimetype='image/jpeg')
    except Exception as e:
        print(f"  [缩略图失败] {e}")
        return jsonify({'error': 'thumbnail failed'}), 500


def _notify_thumbnail_ready(device_id, file_id):
    """通知所有已订阅 SSE 队列：某个视频的缩略图已生成（广播，使桌面端也能收到手机上传的通知）"""
    with _sse_queues_lock:
        all_queues = [q for queues in _sse_queues.values() for q in queues]
    for q in all_queues:
        try:
            q.put_nowait({'event': 'thumbnail_ready', 'file_id': file_id})
        except Exception:
            pass


def _notify_device_changed():
    """通知桌面端刷新设备列表（设备注册/断开时调用）"""
    with _sse_queues_lock:
        all_queues = [q for queues in _sse_queues.values() for q in queues]
    for q in all_queues:
        try:
            q.put_nowait({'event': 'device_changed'})
        except Exception:
            pass


def _notify_new_item(item):
    """互传记录新增/转正时广播：两端页面即时刷新并弹出提示（替代轮询）"""
    evt = {
        'event': 'new_item',
        'id': item.get('id', ''),
        'item_type': item.get('type', ''),
        'source': item.get('source', ''),
        'device_id': item.get('device_id', ''),
        'device_name': item.get('device_name', ''),
        'target_device_id': item.get('target_device_id', ''),
    }
    with _sse_queues_lock:
        all_queues = [q for queues in _sse_queues.values() for q in queues]
    for q in all_queues:
        try:
            q.put_nowait(evt)
        except Exception:
            pass


def _notify_device_kicked(device_id='', all_devices=False):
    """设备被电脑端断开/移除（或全部清除）时广播：手机端立即显示断开（替代轮询检测）"""
    evt = {'event': 'kicked', 'device_id': device_id, 'all': bool(all_devices)}
    with _sse_queues_lock:
        all_queues = [q for queues in _sse_queues.values() for q in queues]
    for q in all_queues:
        try:
            q.put_nowait(evt)
        except Exception:
            pass


@app.route("/api/transfer/events")
def api_transfer_events():
    """SSE 事件推送：前端连接后等待后端推送通知"""
    from flask import Response
    device_id = request.args.get('device_id', '')
    if not device_id:
        return jsonify({'error': 'device_id required'}), 400

    def generate():
        q = _queue_module.Queue()
        with _sse_queues_lock:
            if device_id not in _sse_queues:
                _sse_queues[device_id] = []
            _sse_queues[device_id].append(q)
        # 设备上线：通知 PC 端重算在线状态（与下线同一套通知，全事件驱动）
        _notify_device_changed()
        try:
            # 发送初始连接确认
            yield "data: {\"event\": \"connected\"}\n\n"
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except _queue_module.Empty:
                    # 心跳
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            pass
        finally:
            went_offline = False
            with _sse_queues_lock:
                if device_id in _sse_queues:
                    try:
                        _sse_queues[device_id].remove(q)
                    except ValueError:
                        pass
                    if not _sse_queues[device_id]:
                        del _sse_queues[device_id]
                        went_offline = True
            # 设备下线：让 PC 端立即重算在线状态（事件驱动，不靠轮询）
            if went_offline and device_id != 'desktop':
                _notify_device_changed()

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route("/api/transfer/delete/<file_id>", methods=["DELETE"])
def api_transfer_delete(file_id):
    """删除单条聊天记录（仅删除记录与缩略图缓存，不删除源文件）"""
    with _transfer_items_lock:
        for i, item in enumerate(_transfer_items):
            if item['id'] == file_id:
                _remove_item_thumb(item)
                # 顺带清理未完成上传的分片目录（uploading/failed 状态可能遗留分片）
                _cleanup_item_chunks(item)
                _transfer_items.pop(i)
                _persist_transfer_items()
                return jsonify({'ok': True})
    return jsonify({'error': 'not found'}), 404


@app.route("/api/transfer/cleanup", methods=["POST"])
def api_transfer_cleanup():
    """手动触发清理：清理超时分片 + 按保留天数删除过期源文件（记录保留）
    请求体可传 {"retention_days": N} 覆盖默认 7 天"""
    data = request.json or {}
    retention_days = data.get('retention_days') if isinstance(data, dict) else None
    expired = _cleanup_expired_files(retention_days)
    # 同时清理超时分片（与启动时一致）
    _clean_stale_chunks()
    return jsonify({'ok': True, 'expired_files': expired, 'retention_days': retention_days or _TRANSFER_RETENTION_DAYS})


def _safe_cleanup_expired():
    """安全包装：调 _persist_transfer_items 后释放 _transfer_items_lock 再调清理（避免死锁）
    供 chunk-merge / clear 等需要持久化的接口在锁外调用"""
    try:
        _cleanup_expired_files()
    except Exception:
        pass


@app.route("/api/transfer/clear", methods=["DELETE"])
def api_transfer_clear():
    """清除聊天记录：传 device_id 只清除该设备的记录，否则清除全部。
    仅清除记录与缩略图缓存，不删除源文件（源文件保留在 transfer/ 目录）"""
    device_id = request.args.get('device_id', '')
    with _transfer_items_lock:
        if device_id:
            removed = [i for i in _transfer_items if _match_transfer_item(i, device_id)]
            _transfer_items[:] = [i for i in _transfer_items if not _match_transfer_item(i, device_id)]
        else:
            removed = list(_transfer_items)
            _transfer_items.clear()
        for item in removed:
            _remove_item_thumb(item)
            _cleanup_item_chunks(item)
    _persist_transfer_items()
    print(f"[Transfer] 已清除 {len(removed)} 条记录（device_id={device_id or 'all'}，保留源文件）")
    return jsonify({'ok': True, 'cleared': len(removed)})



@app.route("/mobile")
def mobile_page():
    return send_from_directory(_resolve_asset("templates"), "mobile.html")



# ═══════════════════════════════════════════════════
#  屏幕录制 API
# ═══════════════════════════════════════════════════

# 打包版录屏输出放 exe 旁的 transfer/recordings（exe 内部目录不可写/易被清理）
RECORD_DIR = os.path.join(_app_root if _IS_EXE else _entry_dir, 'transfer', 'recordings')
os.makedirs(RECORD_DIR, exist_ok=True)

# 录制状态文件（控制面板进程和 Web 进程共享）
_DATA_DIR_REC = DATA_DIR
RECORD_STATUS_FILE = os.path.join(_DATA_DIR_REC, '_record_status.json')

import threading as _threading
_record_status_lock = _threading.Lock()

_DEFAULT_REC_STATUS = {
    "recording": False, "paused": False, "click_count": 0,
    "start_time": 0, "last_finished_at": 0, "last_file": "",
    "panel_visible": False, "panel_pid": 0, "panel_pos": None,
    "panel_wake_ts": 0, "pending_fps": None, "pending_save_dir": None,
}


def _read_record_status():
    """读取录制状态（控制面板进程写入的）"""
    try:
        if os.path.exists(RECORD_STATUS_FILE):
            with open(RECORD_STATUS_FILE, 'r', encoding='utf-8') as f:
                s = json.load(f)
                merged = dict(_DEFAULT_REC_STATUS)
                merged.update(s)
                return merged
    except Exception:
        pass
    return dict(_DEFAULT_REC_STATUS)


def _write_record_status(**updates):
    """原子合并写入状态文件（Web 向面板下发信号使用）"""
    with _record_status_lock:
        try:
            status = _read_record_status()
            status.update(updates)
            os.makedirs(os.path.dirname(RECORD_STATUS_FILE), exist_ok=True)
            tmp = RECORD_STATUS_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(status, f, ensure_ascii=False)
            os.replace(tmp, RECORD_STATUS_FILE)
        except Exception as e:
            print(f"[录制状态] 写入失败: {e}")


# 启动时从状态文件恢复录制保存目录
try:
    _rec_st = _read_record_status()
    _rec_saved_dir = _rec_st.get('pending_save_dir')
    if _rec_saved_dir and os.path.isdir(_rec_saved_dir):
        RECORD_DIR = _rec_saved_dir
except Exception:
    pass


# D1: SSE 推送 —— 录制完成/状态变更实时下发前端
_record_event_subscribers = []
_record_event_lock = _threading.Lock()


def _publish_record_event(evt):
    with _record_event_lock:
        dead = []
        for q in _record_event_subscribers:
            try:
                q.put_nowait(evt)
            except Exception:
                dead.append(q)
        for q in dead:
            try:
                _record_event_subscribers.remove(q)
            except Exception:
                pass


_record_process = None  # 当前录制进程（CLI 模式）
_record_panel_process = None  # 当前录制控制面板进程

# ── 录制工具解释器解析 ──
# 面板/录制器需要 mss / cv2 / PyQt5。若服务是用依赖不全的 Python 启动的（如 PATH 里的裸
# python），子进程会秒退导致"工具栏打不开"，Web 端却仍提示已打开。这里启动前探测解释器：
# 优先 sys.executable，其次向上查找项目内 .venv / venv，找不到则明确报错。
_RECORD_PANEL_LOG = os.path.join(LOGS_DIR, "_record_panel.log")
_recorder_python_cache = None  # None=未探测；''=探测无结果；否则为可用解释器路径


def _python_has_recorder_deps(exe):
    """探测解释器是否可找到 mss/cv2/PyQt5（find_spec 只查元数据，不真正加载重库）"""
    probe = ('import importlib.util as u, sys; sys.exit(0 if '
             'all(u.find_spec(m) for m in ("mss", "cv2", "PyQt5")) else 1)')
    try:
        r = subprocess.run(
            [exe, '-c', probe],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if sys.platform == 'win32' else 0,
        )
        return r.returncode == 0
    except Exception:
        return False


def _resolve_recorder_python():
    """返回含 mss/cv2/PyQt5 的 Python 解释器路径；找不到返回 None（结果缓存）"""
    global _recorder_python_cache
    if _IS_EXE:
        return sys.executable  # 打包版使用自带运行环境，不做探测
    if _recorder_python_cache is not None:
        return _recorder_python_cache or None
    candidates = [sys.executable]
    base = _app_root
    for _ in range(5):
        for sub in ('.venv', 'venv'):
            p = os.path.join(base, sub, 'Scripts', 'python.exe') if sys.platform == 'win32' \
                else os.path.join(base, sub, 'bin', 'python')
            if os.path.isfile(p):
                candidates.append(p)
        parent = os.path.dirname(base)
        if parent == base:
            break
        base = parent
    seen = set()
    for exe in candidates:
        exe = os.path.normpath(exe)
        if exe in seen or not os.path.isfile(exe):
            continue
        seen.add(exe)
        if _python_has_recorder_deps(exe):
            _recorder_python_cache = exe
            return exe
    _recorder_python_cache = ''
    return None


@app.route("/api/record/panel/open", methods=["POST"])
def api_record_panel_open():
    """打开录制控制面板

    A1 优化：面板进程已存活时仅写 panel_wake_ts 唤醒隐藏面板，避免重复冷启动
    A3：同时下发 pending_fps，面板下次 start 生效
    """
    global _record_panel_process
    body = (request.get_json(silent=True) or {})
    fps = body.get('fps', 15)
    try:
        fps = min(max(5, int(fps)), 30)
    except Exception:
        fps = 15

    # 面板进程存活：写唤醒信号 + pending_fps，不重启
    if _record_panel_process and _record_panel_process.poll() is None:
        _write_record_status(panel_wake_ts=time.time(), pending_fps=fps)
        return jsonify({
            "ok": True, "message": "面板已唤醒", "reused": True,
            "pid": _record_panel_process.pid, "fps": fps
        })

    # 启动新面板（使用含 mss/cv2/PyQt5 的解释器，且 stderr 落盘便于排障）
    py_exe = _resolve_recorder_python()
    if not py_exe:
        return jsonify({"ok": False, "error": (
            "未找到包含 mss / opencv-python / PyQt5 的 Python 环境。"
            "请用项目 .venv 的 python 启动服务（启动.bat 已自动优先使用），"
            "或在缺少依赖的解释器中执行: pip install mss opencv-python PyQt5 numpy"
        )}), 500
    try:
        panel_args = ['--fps', str(fps),
                      '--save-dir', RECORD_DIR, '--api-port', str(PORT)]
        if _IS_EXE:
            # 打包版：复用 exe 本体以内部命令分派启动面板（见文件顶部 __panel__ 分派）
            cmd = [sys.executable, '__panel__'] + panel_args
        else:
            panel_script = os.path.join(SRC_DIR, 'recorder_tool', 'control_panel.py')
            cmd = [py_exe, panel_script] + panel_args
        log_fp = open(_RECORD_PANEL_LOG, 'wb')
        try:
            _record_panel_process = subprocess.Popen(
                cmd,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0,
                stdout=log_fp, stderr=subprocess.STDOUT
            )
        finally:
            log_fp.close()
        # 崩溃检测：等待 2 秒，若子进程已退出则回传日志尾部（不再谎报"已打开"）
        for _ in range(20):
            time.sleep(0.1)
            if _record_panel_process.poll() is not None:
                try:
                    with open(_RECORD_PANEL_LOG, 'rb') as f:
                        tail = f.read()[-600:].decode('utf-8', errors='ignore').strip()
                except Exception:
                    tail = ''
                _record_panel_process = None
                return jsonify({"ok": False, "error": (
                    "工具栏进程启动即退出。详情: " + (tail or "无输出")
                )}), 500
        _write_record_status(pending_fps=fps)
        return jsonify({"ok": True, "pid": _record_panel_process.pid, "fps": fps, "reused": False})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/record/panel/status")
def api_record_panel_status():
    """F4: 面板存活 + 可见状态"""
    global _record_panel_process
    alive = bool(_record_panel_process and _record_panel_process.poll() is None)
    if not alive:
        _record_panel_process = None
    st = _read_record_status()
    return jsonify({
        "ok": True,
        "open": alive and bool(st.get("panel_visible")),
        "alive": alive,
        "pid": st.get("panel_pid", 0)
    })


@app.route("/api/record/settings", methods=["GET", "POST"])
def api_record_settings():
    """获取/设置录制配置

    F5: POST 时同时写 pending_save_dir，面板轮询后切换（无需重启）
    A3: 也支持写 pending_fps
    """
    global RECORD_DIR
    if request.method == 'GET':
        st = _read_record_status()
        return jsonify({
            "ok": True, "save_dir": RECORD_DIR,
            "fps": st.get("pending_fps") or 15
        })
    data = (request.get_json(silent=True) or {})
    updates = {}
    if 'save_dir' in data:
        new_dir = (data.get('save_dir') or '').strip()
        if not new_dir:
            return jsonify({"ok": False, "error": "路径不能为空"}), 400
        if not os.path.isabs(new_dir):
            return jsonify({"ok": False, "error": "请使用绝对路径"}), 400
        try:
            os.makedirs(new_dir, exist_ok=True)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        RECORD_DIR = new_dir
        updates['pending_save_dir'] = new_dir
    if 'fps' in data:
        try:
            updates['pending_fps'] = min(max(5, int(data['fps'])), 30)
        except Exception:
            pass
    if updates:
        _write_record_status(**updates)
    # F5: applied 表示面板已存活（pending_fps/pending_save_dir 将被面板轮询拿到）
    panel_alive = bool(_record_panel_process and _record_panel_process.poll() is None)
    return jsonify({"ok": True, "save_dir": RECORD_DIR, "applied": panel_alive})


@app.route("/api/record/open-dir", methods=["POST"])
def api_record_open_dir():
    """在资源管理器中打开录制文件保存目录"""
    if _open_dir_in_explorer(RECORD_DIR):
        return jsonify({'ok': True, 'dir': os.path.abspath(RECORD_DIR)})
    return jsonify({'error': 'directory not found'}), 404


@app.route("/api/record/browse-dir", methods=["POST"])
def api_record_browse_dir():
    """打开系统文件夹选择对话框"""
    try:
        # 使用 tkinter 原生文件夹选择器（无需额外依赖）
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()  # 隐藏主窗口
        root.attributes('-topmost', True)  # 置顶
        path = filedialog.askdirectory(
            title='选择录制保存目录',
            initialdir=RECORD_DIR
        )
        root.destroy()
        if path:
            return jsonify({"ok": True, "path": path})
        else:
            return jsonify({"ok": True, "cancelled": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/record/start", methods=["POST"])
def api_record_start():
    """开始屏幕录制"""
    global _record_process
    if _record_process and _record_process.poll() is None:
        return jsonify({"ok": False, "error": "录制已在进行中"}), 409

    body = (request.get_json(silent=True) or {})
    fps = body.get('fps', 15)
    fps = min(max(5, int(fps)), 30)
    mode = body.get('mode', 'full')
    if mode not in ('full', 'region'):
        mode = 'full'

    try:
        python_exe = _resolve_recorder_python()
        if not python_exe:
            return jsonify({"ok": False, "error": (
                "未找到包含 mss / opencv-python / PyQt5 的 Python 环境，无法启动录制。"
                "请用项目 .venv 的 python 启动服务（启动.bat 已自动优先使用）"
            )}), 500
        rec_args = ['--fps', str(fps), '--save-dir', RECORD_DIR, '--mode', mode]
        if _IS_EXE:
            # 打包版：复用 exe 本体以内部命令分派启动录制（见文件顶部 __recorder__ 分派）
            cmd = [sys.executable, '__recorder__'] + rec_args
        else:
            recorder_script = os.path.join(SRC_DIR, 'recorder_tool', 'recorder.py')
            cmd = [python_exe, recorder_script] + rec_args
        _record_process = subprocess.Popen(
            cmd,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        return jsonify({"ok": True, "pid": _record_process.pid, "fps": fps, "mode": mode})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/record/stop", methods=["POST"])
def api_record_stop():
    """停止屏幕录制"""
    global _record_process
    if not _record_process or _record_process.poll() is not None:
        _record_process = None
        return jsonify({"ok": False, "error": "没有正在进行的录制"}), 404

    try:
        _record_process.terminate()
        try:
            _record_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _record_process.kill()
            _record_process.wait(timeout=3)
        _record_process = None
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/record/status")
def api_record_status():
    """获取录制状态（F4: 同时包含面板存活信息）"""
    global _record_panel_process
    running = bool(_record_process and _record_process.poll() is None)
    panel_alive = bool(_record_panel_process and _record_panel_process.poll() is None)
    if not panel_alive:
        _record_panel_process = None
    status = _read_record_status()
    status["recording"] = running or status.get("recording", False)
    status["panel_alive"] = panel_alive
    status["panel_open"] = panel_alive and bool(status.get("panel_visible"))
    return jsonify(status)


@app.route("/api/record/notify-finished", methods=["POST"])
def api_record_notify_finished():
    """D1: 控制面板 stop 后主动回调，Web 立即推送 SSE，前端无需等 2s 轮询"""
    data = (request.get_json(silent=True) or {})
    fn = data.get('file', '')
    clicks = data.get('clicks', 0)
    _publish_record_event({'type': 'finished', 'file': fn, 'clicks': clicks, 'ts': time.time()})
    # 自动触发异步转码，前端点播时大概率已就绪
    if fn:
        _async_transcode(fn)
    return jsonify({"ok": True})


@app.route("/api/record/events")
def api_record_events():
    """D1: SSE 实时推送录制事件"""
    q = _queue_module.Queue(maxsize=50)
    with _record_event_lock:
        _record_event_subscribers.append(q)

    def gen():
        try:
            yield f"data: {json.dumps({'type':'hello','ts':time.time()})}\n\n"
            while True:
                try:
                    evt = q.get(timeout=25)
                    yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                except _queue_module.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with _record_event_lock:
                if q in _record_event_subscribers:
                    try:
                        _record_event_subscribers.remove(q)
                    except Exception:
                        pass

    return app.response_class(
        gen(), mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no',
                 'Connection': 'keep-alive'}
    )


@app.route("/api/record/transcode", methods=["POST"])
def api_record_transcode():
    """手动触发转码（异步）"""
    data = (request.get_json(silent=True) or {})
    filename = data.get("name", "")
    if not filename or '..' in filename or '/' in filename or '\\' in filename:
        return jsonify({"ok": False, "error": "非法文件名"}), 400
    _async_transcode(filename)
    return jsonify({"ok": True, "message": "转码已启动"})


def _async_transcode(filename):
    """异步转码录制文件（不阻塞主线程）

    E3: 针对屏幕录制内容优化 ffmpeg 参数（veryfast + crf 26 + tune stillimage）
    去重：_transcode_inflight 避免同一文件重复启动转码
    """
    if filename in _transcode_inflight:
        return
    _transcode_inflight.add(filename)

    def _run():
        try:
            filepath = os.path.join(RECORD_DIR, filename)
            if not os.path.exists(filepath):
                return
            transcode_dir = os.path.join(RECORD_DIR, '_transcoded')
            os.makedirs(transcode_dir, exist_ok=True)
            transcode_path = os.path.join(transcode_dir, filename)
            if os.path.exists(transcode_path) and os.path.getmtime(transcode_path) >= os.path.getmtime(filepath):
                return
            ffmpeg_exe = _find_ffmpeg()
            if not ffmpeg_exe:
                venv_ffmpeg = os.path.join(os.path.dirname(sys.executable),
                                            'ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg')
                if os.path.exists(venv_ffmpeg):
                    ffmpeg_exe = venv_ffmpeg
                else:
                    print("[录制] ffmpeg 不可用，跳过转码")
                    return
            # E3: veryfast + crf 26 + tune stillimage（屏幕内容体积更小、画质更好）
            cmd = [
                ffmpeg_exe, '-y', '-i', filepath,
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '26',
                '-tune', 'stillimage', '-g', '60',
                '-movflags', '+faststart',
                '-pix_fmt', 'yuv420p',
                transcode_path
            ]
            print(f"[录制] 异步转码: {filename}")
            result = subprocess.run(cmd, capture_output=True, timeout=300)
            if result.returncode == 0 and os.path.exists(transcode_path):
                print(f"[录制] 转码完成: {transcode_path} ({os.path.getsize(transcode_path)} bytes)")
                _publish_record_event({'type': 'transcoded', 'file': filename, 'ts': time.time()})
            else:
                err = result.stderr.decode('utf-8', errors='ignore')[-500:] if result.stderr else 'unknown'
                print(f"[录制] 转码失败: {err}")
                _publish_record_event({'type': 'transcode_failed', 'file': filename, 'ts': time.time()})
        except Exception as e:
            print(f"[录制] 转码异常: {e}")
        finally:
            _transcode_inflight.discard(filename)

    import threading
    threading.Thread(target=_run, daemon=True).start()


# E1: 正在转码中的文件（避免重复启动）
_transcode_inflight = set()


@app.route("/api/record/list")
def api_record_list():
    """获取已保存的录制文件列表（包含转码状态）"""
    files = []
    transcode_dir = os.path.join(RECORD_DIR, '_transcoded')
    if os.path.exists(RECORD_DIR):
        for f in sorted(os.listdir(RECORD_DIR), reverse=True):
            if f.lower().endswith(('.mp4', '.avi', '.mkv', '.mov')):
                fp = os.path.join(RECORD_DIR, f)
                tp = os.path.join(transcode_dir, f)
                if os.path.exists(tp) and os.path.getmtime(tp) >= os.path.getmtime(fp):
                    tstatus = 'done'
                elif f in _transcode_inflight:
                    tstatus = 'processing'
                else:
                    tstatus = 'pending'
                files.append({
                    "name": f,
                    "size": os.path.getsize(fp),
                    "time": datetime.fromtimestamp(os.path.getmtime(fp)).strftime('%Y-%m-%d %H:%M:%S'),
                    "transcode_status": tstatus
                })
    return jsonify({"ok": True, "recordings": files})


def _mp4_has_faststart(filepath):
    """E2: 扫描 MP4 box 结构，判断 moov 是否在 mdat 之前（faststart）

    旧的“读 32 字节找 moov”对大文件会误判，本实现逐个 box 遍历。
    """
    try:
        size = os.path.getsize(filepath)
        with open(filepath, 'rb') as f:
            pos = 0
            moov_pos = -1
            mdat_pos = -1
            while pos < size:
                f.seek(pos)
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                box_size = int.from_bytes(hdr[:4], 'big')
                box_type = hdr[4:8]
                if box_size == 1:
                    ext = f.read(8)
                    if len(ext) < 8:
                        break
                    box_size = int.from_bytes(ext, 'big')
                elif box_size == 0:
                    box_size = size - pos
                if box_size < 8:
                    break
                if box_type == b'moov' and moov_pos < 0:
                    moov_pos = pos
                elif box_type == b'mdat' and mdat_pos < 0:
                    mdat_pos = pos
                if moov_pos >= 0 and mdat_pos >= 0:
                    break
                pos += box_size
            return moov_pos >= 0 and (mdat_pos < 0 or moov_pos < mdat_pos)
    except Exception:
        return False


@app.route("/api/record/file/<filename>")
def api_record_file(filename):
    """获取录制文件

    E1 优化：不再同步阻塞转码（旧逻辑最坏卡 120s）：
      1) 如果转码文件已就绪 → 直接返回（支持 Range）
      2) 如果原文件已是 faststart → 直接返回原文件
      3) 否则启动异步转码并返回 202，前端展示 loading、轮询或监听 SSE
    """
    if '..' in filename or '/' in filename or '\\' in filename:
        return jsonify({"ok": False, "error": "非法文件名"}), 400
    filepath = os.path.join(RECORD_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"ok": False, "error": "文件不存在"}), 404

    transcode_dir = os.path.join(RECORD_DIR, '_transcoded')
    os.makedirs(transcode_dir, exist_ok=True)
    transcode_path = os.path.join(transcode_dir, filename)

    # 1) 已转码 → 直接返回
    if os.path.exists(transcode_path) and os.path.getmtime(transcode_path) >= os.path.getmtime(filepath):
        return send_from_directory(transcode_dir, filename, conditional=True)

    # 2) E2: 原文件已是 faststart，无需转码
    if _mp4_has_faststart(filepath):
        return send_from_directory(RECORD_DIR, filename, conditional=True)

    # 3) E1: 启动异步转码，返回 202 + 进度信息
    _async_transcode(filename)
    resp = jsonify({
        "ok": False, "status": "processing", "file": filename,
        "message": "视频正在转码为浏览器兼容格式，请稍候…"
    })
    resp.status_code = 202
    resp.headers['Retry-After'] = '2'
    return resp


@app.route("/api/record/delete", methods=["POST"])
def api_record_delete():
    """删除录制文件"""
    data = (request.get_json(silent=True) or {})
    filename = data.get("name", "")
    if not filename or '..' in filename or '/' in filename or '\\' in filename:
        return jsonify({"ok": False, "error": "非法文件名"}), 400
    filepath = os.path.join(RECORD_DIR, filename)
    if os.path.exists(filepath):
        os.remove(filepath)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "文件不存在"}), 404


@app.route("/api/record/copy", methods=["POST"])
def api_record_copy():
    """把录制文件复制到系统剪贴板（复用文件互传的 _copy_file_to_clipboard）

    复制后可在微信/QQ 聊天窗口、资源管理器等直接 Ctrl+V 粘贴文件。
    优先复制转码优化版（与播放器展示一致，体积更小、兼容性更好）。
    """
    data = (request.get_json(silent=True) or {})
    filename = data.get("name", "")
    if not filename or '..' in filename or '/' in filename or '\\' in filename:
        return jsonify({"ok": False, "error": "非法文件名"}), 400
    filepath = os.path.join(RECORD_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    # 优先转码优化版（与文件互传 copy-video 的策略一致）
    transcode_path = os.path.join(RECORD_DIR, '_transcoded', filename)
    if os.path.exists(transcode_path) and os.path.getmtime(transcode_path) >= os.path.getmtime(filepath):
        target, version = transcode_path, 'optimized'
    else:
        target, version = filepath, 'original'
    try:
        _copy_file_to_clipboard(target)
    except Exception as e:
        return jsonify({"ok": False, "error": "复制到剪贴板失败: " + str(e)}), 500
    return jsonify({
        "ok": True, "name": filename, "version": version,
        "size": os.path.getsize(target),
    })


def _port_has_listener(port):
    """检测指定端口是否有进程处于 LISTENING（用于判断抓包引擎是否仍存活）"""
    try:
        output = subprocess.check_output(
            f'netstat -ano | findstr ":{port} " | findstr "LISTENING"',
            shell=True, text=True, stderr=subprocess.DEVNULL
        )
        return bool(output.strip())
    except Exception:
        return False


def _kill_port_occupants(port):
    """启动前清理占用指定端口的所有进程（Windows）"""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin():
            pass  # 非管理员也能杀自己的进程
    except Exception:
        pass
    try:
        output = subprocess.check_output(
            f'netstat -ano | findstr ":{port} " | findstr "LISTENING"',
            shell=True, text=True, stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        return  # 没有进程占用该端口
    except Exception:
        return
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
    if not pids:
        return
    print(f"  [端口清理] 端口 {port} 被 {len(pids)} 个进程占用 (PID: {', '.join(map(str, pids))})，正在清理...")
    for pid in pids:
        try:
            subprocess.run(f'taskkill /F /PID {pid}', shell=True,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    import time
    time.sleep(0.5)  # 等待端口释放
    print(f"  [端口清理] 已清理 {len(pids)} 个进程")


# ═══════════════════════════════════════════════════
#  API: 定时任务（调度逻辑见 scheduler.py）
# ═══════════════════════════════════════════════════
import scheduler as _scheduler


@app.route("/api/schedule/tasks", methods=["GET"])
def schedule_tasks_list():
    """任务列表（含下次执行时间、最近一次结果摘要）"""
    # 列表状态需要实时：禁止浏览器缓存，避免刷新按钮拿到旧快照
    resp = jsonify(_scheduler.list_tasks())
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/schedule/tasks", methods=["POST"])
def schedule_task_create():
    """创建定时任务

    请求: {name, file, steps, loops, mode, config}
      name: 任务名（可选，留空按所选接口名自动生成；列表里用它区分任务）
      mode: once | daily | random_window | interval | burst
      config 按模式取值:
        once          {run_at: '2026-10-10 15:30'}
        daily         {time: '09:00'}
        random_window {start: '10:00', end: '11:00'}
        interval      {every: 30, jitter: 10}
        burst         {window: 60, times: 5}
    """
    body = request.get_json(silent=True) or {}
    ok, res = _scheduler.create_task(body)
    if not ok:
        return jsonify({"error": res}), 400
    return jsonify({"ok": True, "task": res})


@app.route("/api/schedule/tasks/<tid>", methods=["POST"])
def schedule_task_update(tid):
    """更新任务（启停 / 重命名 / 修改文件、步骤、次数）"""
    body = request.get_json(silent=True) or {}
    ok, res = _scheduler.update_task(tid, body)
    if not ok:
        return jsonify({"error": res}), 400
    return jsonify({"ok": True, "task": res})


@app.route("/api/schedule/tasks/<tid>", methods=["DELETE"])
def schedule_task_delete(tid):
    """删除任务（连同执行历史）"""
    ok, err = _scheduler.delete_task(tid)
    if not ok:
        return jsonify({"error": err}), 400
    return jsonify({"ok": True})


@app.route("/api/schedule/tasks/<tid>/run-now", methods=["POST"])
def schedule_task_run_now(tid):
    """立即执行一次（不影响既定调度计划）"""
    ok, err = _scheduler.run_now(tid)
    if not ok:
        return jsonify({"error": err}), 400
    return jsonify({"ok": True})


@app.route("/api/schedule/tasks/<tid>/runs", methods=["GET"])
def schedule_task_runs(tid):
    """执行历史列表（最近 50 次）"""
    resp = jsonify({"runs": _scheduler.list_runs(tid)})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/schedule/tasks/<tid>/runs/<run_id>", methods=["GET"])
def schedule_task_run_detail(tid, run_id):
    """单次执行详情（含每一步骤的结果与响应）"""
    data = _scheduler.get_run_detail(tid, run_id)
    if not data:
        return jsonify({"error": "执行记录不存在"}), 404
    resp = jsonify(data)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# 定时任务参数化 Excel 持久化目录（任务长期引用，不能依赖内存 session）
SCHED_PARAMS_DIR = os.path.join(DATA_DIR, "_schedule_params")


@app.route("/api/schedule/param-file", methods=["POST"])
def schedule_param_file_upload():
    """上传定时任务参数化 Excel（持久保存，供调度执行时逐行读取）

    返回解析预览与各列建议类型；同名文件覆盖保存。
    """
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "请上传 Excel 文件"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"ok": False, "error": "文件名为空"}), 400

    raw_name = os.path.basename((file.filename or "").replace("\\", "/"))
    ext = os.path.splitext(raw_name)[1].lower()
    if ext not in (".xlsx", ".xls"):
        return jsonify({"ok": False, "error": "仅支持 .xlsx / .xls 文件"}), 400

    # 文件名安全化：保留中英文、数字与 ._-，其余替换为下划线
    stem = os.path.splitext(raw_name)[0] or "params"
    stem = "".join(c if (c.isalnum() or c in "._-") else "_" for c in stem)
    safe_name = stem + ext

    try:
        import engine as E
        headers, all_rows = E.parse_excel_rows(file, file.filename)
        if not headers or not any(headers):
            return jsonify({"ok": False, "error": "Excel 第一行必须是参数名（表头）"}), 400
        if not all_rows:
            return jsonify({"ok": False, "error": "Excel 中没有数据行"}), 400

        os.makedirs(SCHED_PARAMS_DIR, exist_ok=True)
        dest = os.path.join(SCHED_PARAMS_DIR, safe_name)
        file.stream.seek(0)
        file.save(dest)

        # 智能识别各列建议的入参类型（仅高置信度场景）
        suggested = {}
        for ci, h in enumerate(headers):
            if not h:
                continue
            col_vals = [row[ci] for row in all_rows[:500] if ci < len(row)]
            guess = _detect_param_type(h, col_vals)
            if guess:
                suggested[h] = guess

        return jsonify({"ok": True, "saved_name": safe_name, "headers": headers,
                        "rows": all_rows[:20], "total": len(all_rows),
                        "suggested_types": suggested})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Excel 解析失败: {str(e)}"}), 400


# 启动调度守护线程（每 5 秒扫描到期任务）
_scheduler.start_worker()


# ═══════════════════════════════════════════════════
#  抓包引擎（mitmproxy）与流量缓冲
#  设计约束：引擎绝不随主服务自动启动，仅界面手动启停；
#  主服务退出时自动停止，不留残留进程。
#  端口可在界面修改并持久化（data/_capture_config.json），
#  优先级：界面保存的配置 > 环境变量 CAPTURE_PORT > 默认 8899。
# ═══════════════════════════════════════════════════

_CAPTURE_CONFIG_FILE = os.path.join(DATA_DIR, "_capture_config.json")


def _load_capture_config():
    """读取抓包配置（端口等），文件缺失/损坏时返回空字典"""
    try:
        with open(_CAPTURE_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _save_capture_config(cfg):
    """保存抓包配置（文件锁保护），返回是否成功"""
    try:
        with _file_lock(_CAPTURE_CONFIG_FILE):
            with open(_CAPTURE_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[Capture] 配置保存失败: {e}")
        return False


# ── 流量来源记录开关（web=本机回环客户端 / device=其他设备）───────
# 默认全部关闭，界面勾选后才记录对应来源；addon（mitmproxy 钩子）
# 每笔流量读取此文件决定是否上报，读取带 0.5s 节流。
# 服务每次启动重置为全关（不持久化上次选择）。
_CAPTURE_SCOPE_FILE = os.path.join(DATA_DIR, "_capture_scope.json")
_CAPTURE_SCOPE = {"web": False, "device": False}


def _save_capture_scope():
    """把来源开关写入文件供 addon 读取，返回是否成功"""
    try:
        with _file_lock(_CAPTURE_SCOPE_FILE):
            with open(_CAPTURE_SCOPE_FILE, "w", encoding="utf-8") as f:
                json.dump(_CAPTURE_SCOPE, f)
        return True
    except Exception as e:
        print(f"[Capture] scope 保存失败: {e}")
        return False


_save_capture_scope()  # 启动即写入全关状态，保证 addon 首次读取即为默认值


# ── 抓包引擎功能配置（改包 Mock / 弱网模拟 / 断点调试）──────────────
# 主服务写文件、addon 每 0.5s 节流读取——开关实时生效，无需重启引擎。
# 功能开关持久保留（用户下次打开工具箱仍是上次的选择）。
_CAPTURE_ENGINE_CFG_FILE = os.path.join(DATA_DIR, "_capture_engine.json")
# 弱网预设档位（参考 Chrome DevTools 典型网络限速预设，上下行带宽分开）——参数含义：
# latency_ms 请求延迟 / bandwidth_kbps 下行带宽上限 kbps（千比特/秒，0=不限速）/
# bandwidth_up_kbps 上行带宽上限 kbps（0=与下行相同）/ loss_pct 丢包率（按概率掐断连接）/
# stall_every+stall_ms 每 N 秒卡顿 M 毫秒。
_CAPTURE_WEAKNET_PRESETS_DEFAULT = {
    "custom": {"label": "自定义", "latency_ms": 0, "bandwidth_kbps": 0, "bandwidth_up_kbps": 0,
               "loss_pct": 0, "stall_every": 0, "stall_ms": 0},
    "gprs": {"label": "GPRS", "latency_ms": 500, "bandwidth_kbps": 50, "bandwidth_up_kbps": 20,
             "loss_pct": 0, "stall_every": 0, "stall_ms": 0},
    "2g": {"label": "2G", "latency_ms": 300, "bandwidth_kbps": 250, "bandwidth_up_kbps": 50,
           "loss_pct": 0, "stall_every": 0, "stall_ms": 0},
    "3g": {"label": "3G", "latency_ms": 100, "bandwidth_kbps": 750, "bandwidth_up_kbps": 250,
           "loss_pct": 0, "stall_every": 0, "stall_ms": 0},
    "4g": {"label": "4G", "latency_ms": 20, "bandwidth_kbps": 4000, "bandwidth_up_kbps": 3000,
           "loss_pct": 0, "stall_every": 0, "stall_ms": 0},
    "wifi": {"label": "Wi-Fi", "latency_ms": 2, "bandwidth_kbps": 30000, "bandwidth_up_kbps": 15000,
             "loss_pct": 0, "stall_every": 0, "stall_ms": 0},
    "flaky": {"label": "不稳定网络", "latency_ms": 200, "bandwidth_kbps": 1000, "bandwidth_up_kbps": 0,
              "loss_pct": 15, "stall_every": 8, "stall_ms": 5000},
}
# 旧版内置档位名 → 新档位名迁移映射（弱 Wi-Fi→2G、3G 蜂窝→3G，参数取新档位默认值）
_CAPTURE_WEAKNET_PRESET_ALIASES = {"wifi_bad": "2g", "cell_3g": "3g"}
_CAPTURE_ENGINE_CFG_DEFAULT = {
    "mock": {"enabled": False},
    "weaknet": {"enabled": False, "preset": "custom",
                "presets": {k: dict(v) for k, v in _CAPTURE_WEAKNET_PRESETS_DEFAULT.items()},
                "delay_ms": 0, "kbps": 0},
    "breakpoint": {"request": False, "response": False, "filter": ""},
}
_WEAKNET_RANGES = {
    "latency_ms": (0, 120000, "延迟须为 0-120000 毫秒"),
    "bandwidth_kbps": (0, 102400, "下行带宽须为 0-102400 kbps"),
    "bandwidth_up_kbps": (0, 102400, "上行带宽须为 0-102400 kbps"),
    "loss_pct": (0, 100, "丢包率须为 0-100 %"),
    "stall_every": (0, 3600, "卡顿间隔须为 0-3600 秒"),
    "stall_ms": (0, 300000, "卡顿时长须为 0-300000 毫秒"),
}


def _weaknet_preset_merge(cur, patch):
    """校验并合并一个弱网档位的参数补丁，失败返回错误文本（None=成功）"""
    for k, (lo, hi, msg) in _WEAKNET_RANGES.items():
        if k in patch:
            v = patch[k]
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not (lo <= v <= hi):
                return msg
            cur[k] = int(v)
    if "label" in patch:
        cur["label"] = str(patch["label"] or "").strip()[:20] or str(cur.get("label") or "")
    return None


def _load_capture_engine_cfg():
    """读取引擎功能配置（缺失字段用默认值补齐；weaknet 档位逐档合并 + 旧字段迁移）"""
    cfg = json.loads(json.dumps(_CAPTURE_ENGINE_CFG_DEFAULT))  # 深拷贝，防嵌套引用共享
    try:
        with open(_CAPTURE_ENGINE_CFG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for section in cfg:
            if isinstance(data.get(section), dict):
                cfg[section].update(data[section])
    except Exception:
        pass
    sn = cfg.get("weaknet")
    if not isinstance(sn, dict):
        sn = {"enabled": False, "preset": "custom", "presets": {}, "delay_ms": 0, "kbps": 0}
    # 档位合并：内置档位（GPRS / 2G / 3G / 4G / Wi-Fi / 不稳定网络）固定为默认参考值，
    # 不允许修改——即使持久化文件里有历史改动（旧版前端保存过）也一律忽略、回落默认；
    # 仅「自定义」档位保留用户编辑；旧档位名迁移（弱 Wi-Fi→2G、3G 蜂窝→3G），
    # 其余未知档位名一律丢弃
    merged = {k: dict(v) for k, v in _CAPTURE_WEAKNET_PRESETS_DEFAULT.items()}
    user_presets = sn.get("presets") if isinstance(sn.get("presets"), dict) else {}
    cu_old = user_presets.get("custom")
    if isinstance(cu_old, dict):
        merged["custom"].update({kk: vv for kk, vv in cu_old.items()
                                 if kk in _WEAKNET_RANGES or kk == "label"})
    sn["presets"] = merged
    preset = str(sn.get("preset") or "custom")
    preset = _CAPTURE_WEAKNET_PRESET_ALIASES.get(preset, preset)
    if preset not in merged:
        preset = "custom"
    sn["preset"] = preset
    # 旧字段迁移：历史扁平 delay_ms/kbps → 自定义档（仅当自定义档为空时生效）
    cu = merged.setdefault("custom", {"label": "自定义"})
    if not any(float(cu.get(k) or 0) > 0
               for k in ("latency_ms", "bandwidth_kbps", "loss_pct", "stall_ms")):
        try:
            old_delay = int(sn.get("delay_ms") or 0)
            old_kbps = int(sn.get("kbps") or 0)
        except Exception:
            old_delay, old_kbps = 0, 0
        if old_delay > 0 or old_kbps > 0:
            cu["latency_ms"] = old_delay
            # 旧字段 kbps 语义为 KB/s（KiB），×8 换算为 kbps（千比特）
            cu["bandwidth_kbps"] = min(old_kbps * 8, 102400)
    cfg["weaknet"] = sn
    return cfg


def _save_capture_engine_cfg(cfg):
    """保存引擎功能配置（文件锁保护），返回是否成功"""
    try:
        with _file_lock(_CAPTURE_ENGINE_CFG_FILE):
            with open(_CAPTURE_ENGINE_CFG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[Capture] 引擎功能配置保存失败: {e}")
        return False


_save_capture_engine_cfg(_load_capture_engine_cfg())  # 启动即确保文件存在（addon 读取失败时兜底全关）


try:
    _CAPTURE_PORT = int(_load_capture_config().get("port") or os.environ.get("CAPTURE_PORT", "8899"))
except Exception:
    _CAPTURE_PORT = 8899

# HTTPS 代理端口（start_https_proxy 中绑定），端口校验时需排除
_HTTPS_PROXY_PORT = 5051

try:
    from capture.engine import CaptureEngine
    _capture_engine = CaptureEngine(_app_root, port=_CAPTURE_PORT, report_port=PORT, local_ip=LOCAL_IP)
except Exception as _cap_e:
    print(f"[Capture] 引擎模块加载失败: {_cap_e}")
    _capture_engine = None

# 流量内存缓冲：deque 最新在前，seq 自增用于前端增量拉取
from collections import deque
_capture_flows = deque()          # 只存摘要+headers，不存 body
_capture_flow_index = {}          # id -> 条目对象（O(1) 查找；同 id 在途→终报原地更新）
_capture_seq = 0
_capture_lock = threading.Lock()
_capture_dropped = 0              # addon 上报队列满时累计丢弃的条数（状态接口展示）
_CAPTURE_MAX = int(os.environ.get("CAPTURE_MAX", "2000"))

# body 二级存储：内存 LRU 热缓存 + 磁盘持久化（所有 body 落盘，内存只保留最近访问的）
# 目录放 data/ 下：系统临时目录会被磁盘清理/重启清掉，历史 body 随之永久丢失；
# 启动时清空——流量列表本身随重启清空，旧 body 无主，顺带清掉上次异常退出的残留。
import tempfile as _tempfile_mod
_capture_body_dir = os.path.join(_app_root, "data", "capture_bodies")
try:
    import shutil as _shutil_mod
    if os.path.isdir(_capture_body_dir):
        _shutil_mod.rmtree(_capture_body_dir, ignore_errors=True)
    _old_body_dir = os.path.join(_tempfile_mod.gettempdir(), "api-tester-bodies")
    if os.path.isdir(_old_body_dir):
        _shutil_mod.rmtree(_old_body_dir, ignore_errors=True)  # 旧版临时目录，迁走后清理
    os.makedirs(_capture_body_dir, exist_ok=True)
except Exception:
    pass
_capture_body_cache = {}          # 内存热缓存：{flow_id: body_dict}
_capture_body_lock = threading.Lock()  # 独立锁：磁盘写/缓存淘汰不占用 _capture_lock
_BODY_CACHE_MAX = 100             # 内存最多缓存 100 条，超出淘汰但磁盘仍在
_BODY_SIZE_LIMIT = 1 * 1024 * 1024  # 单个 body 超 1MB 截断（磁盘也不存完整版）


def _extract_body_fields(data):
    """从记录中拆出 body 字段（就地 pop），返回 body dict"""
    body_data = {}
    for bk in ("req_headers", "req_body", "resp_headers", "resp_body"):
        val = data.pop(bk, None)
        if val is not None:
            if isinstance(val, str) and len(val) > _BODY_SIZE_LIMIT:
                body_data[bk] = val[:_BODY_SIZE_LIMIT] + "\n…[已截断，完整 %d 字节]" % len(val)
            else:
                body_data[bk] = val
    return body_data


def _body_cache_put(flow_id, body_data):
    """写内存热缓存（LRU 淘汰，独立锁）"""
    with _capture_body_lock:
        _capture_body_cache[flow_id] = body_data
        while len(_capture_body_cache) > _BODY_CACHE_MAX:
            _capture_body_cache.pop(next(iter(_capture_body_cache)))


def _persist_body(flow_id, body_data):
    """body 落盘 + 入内存热缓存（在 _capture_lock 之外调用，磁盘 IO 不阻塞流量查询）"""
    if not body_data:
        return
    try:
        import json as _json
        with open(os.path.join(_capture_body_dir, flow_id + ".json"), "w", encoding="utf-8") as f:
            _json.dump(body_data, f, ensure_ascii=False)
    except Exception:
        pass
    _body_cache_put(flow_id, body_data)


def _body_cache_remove(flow_id):
    """联动删除内存缓存 + 磁盘文件"""
    with _capture_body_lock:
        _capture_body_cache.pop(flow_id, None)
    _remove_body_disk(flow_id)


def _load_body_disk(flow_id):
    """从磁盘加载 body（内存未命中时兜底）"""
    path = os.path.join(_capture_body_dir, flow_id + ".json")
    if not os.path.exists(path):
        return None
    try:
        import json as _json
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        # 回填内存热缓存
        _body_cache_put(flow_id, data)
        return data
    except Exception:
        return None

def _remove_body_disk(flow_id):
    """删除磁盘上的 body 文件"""
    try:
        os.remove(os.path.join(_capture_body_dir, flow_id + ".json"))
    except OSError:
        pass


# ── 抓包数据接口本机访问限制 ────────────────────────────────
# 主服务监听 0.0.0.0（手机互传/移动端页面需要），但抓包记录属于敏感数据：
# 仅允许运行工具的这台电脑访问 /api/capture/*；addon 上报、断点回传、重放均来自回环，不受影响。
_CAPTURE_LOCAL_ADDRS = {"127.0.0.1", "::1", LOCAL_IP}


@app.before_request
def _guard_capture_api_local_only():
    """抓包数据类接口只服务本机（同网段其他设备即使打开页面也读不到抓包数据）"""
    path = request.path or ""
    if not path.startswith("/api/capture/"):
        return None
    if request.remote_addr in _CAPTURE_LOCAL_ADDRS:
        return None
    return jsonify({"ok": False, "error": "抓包接口仅允许在运行工具的电脑上访问"}), 403


# ═══════════════════════════════════════════════════
#  互传外网门禁：4 位校验码 + 访问令牌
# ═══════════════════════════════════════════════════
# 局域网扫码维持原样（直接连接）；经公网地址（frp / cloudflared / 端口映射）访问时，
# 手机端必须先输入电脑端显示的 4 位校验码，验证通过后签发令牌（Cookie），
# 之后的互传接口凭令牌放行——避免互传入口暴露到公网后被陌生人随意上传下载。
_TF_GUARD_LOCK = threading.RLock()
_TF_GUARD = {"code": "", "epoch": 0}
_TF_TOKENS = {}             # token -> 过期时间戳
_TF_FAIL = {}               # 来源 IP -> [连续错码次数, 锁定到期时间戳]
_TF_COOKIE = "apit_tf_token"
_TF_TOKEN_TTL = 12 * 3600   # 令牌有效期（小时级，避免传输中途失效）
_TF_MAX_FAIL = 5            # 同一来源连续错码上限
_TF_LOCK_SEC = 300          # 达到上限后锁定 5 分钟
# 校验码与令牌落盘：工具重启后手机端不必重新输码（否则每次重启都要回到输码页，容易被误认为"收不到消息"）
_TF_STATE_FILE = os.path.join(DATA_DIR, "_transfer_guard.json")
# 门禁自身接口：不要求令牌，否则手机端无法完成验证（各自内部再做本机限制）
_TF_GUARD_PATHS = (
    "/api/transfer/verify",
    "/api/transfer/auth-probe",
    "/api/transfer/guard",
    "/api/transfer/guard/refresh",
    "/api/transfer/public-url",
)


def _tf_state_load():
    """启动时恢复校验码与未过期令牌"""
    try:
        if not os.path.exists(_TF_STATE_FILE):
            return
        with open(_TF_STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        code = str(d.get("code") or "")
        now = time.time()
        with _TF_GUARD_LOCK:
            if len(code) == 4 and code.isdigit():
                _TF_GUARD["code"] = code
                _TF_GUARD["epoch"] = int(d.get("epoch") or 1)
            for k, v in (d.get("tokens") or {}).items():
                try:
                    exp = float(v)
                except Exception:
                    continue
                if exp > now:
                    _TF_TOKENS[k] = exp
    except Exception:
        pass


def _tf_state_save():
    """保存校验码与有效令牌（调用方通常已持锁，故用可重入锁）"""
    try:
        with _TF_GUARD_LOCK:
            now = time.time()
            data = {
                "code": _TF_GUARD.get("code", ""),
                "epoch": _TF_GUARD.get("epoch", 0),
                "tokens": {k: v for k, v in _TF_TOKENS.items() if v > now},
            }
        tmp = _TF_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, _TF_STATE_FILE)
    except Exception:
        pass


_tf_state_load()


def _ip_is_lan(host):
    """判断主机名/地址是否属于局域网或回环（零依赖实现，避免引入 ipaddress 分支差异）"""
    h = str(host or "").strip().strip("[]").lower()
    if not h:
        return False
    if h in ("localhost", "::1"):
        return True
    parts = h.split(".")
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        a, b = int(parts[0]), int(parts[1])
        return a == 127 or a == 10 or (a == 172 and 16 <= b <= 31) \
            or (a == 192 and b == 168) or (a == 169 and b == 254)
    if ":" in h:  # IPv6：回环 / 链路本地 / ULA
        return h.startswith(("fe80", "fc", "fd"))
    return False


def _is_external_request():
    """外网访问判定：来源地址 / X-Forwarded-For / Host 任一不是内网，即按外网处理。

    - 隧道场景（cloudflared / frp）：请求由本机回连，Host 与 X-Forwarded-Host 是公网域名 → 外网
    - 端口映射直连：来源地址本身就是公网 IP → 外网
    - 局域网手机扫码：全部是 192.168.x.x → 内网，免校验
    """
    remote = str(request.remote_addr or "").strip()
    if remote and not _ip_is_lan(remote):
        return True
    # 反代/隧道会把原始来源写进 X-Forwarded-For（取第一段为客户端地址）
    xff = str(request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    if xff and not _ip_is_lan(xff):
        return True
    host = str(request.headers.get("X-Forwarded-Host") or request.host or "").split(":")[0]
    if not host:
        return False
    return not _ip_is_lan(host)


def _tf_guard_code(force_new=False):
    """当前 4 位校验码；force_new=True 时换新码并作废所有已签发令牌"""
    with _TF_GUARD_LOCK:
        if force_new or not _TF_GUARD["code"]:
            _TF_GUARD["code"] = str(int.from_bytes(os.urandom(2), "big") % 9000 + 1000)
            _TF_GUARD["epoch"] += 1
            _TF_TOKENS.clear()
            if force_new:
                _TF_FAIL.clear()
            _tf_state_save()
        return _TF_GUARD["code"]


def _tf_issue_token():
    token = os.urandom(18).hex()
    with _TF_GUARD_LOCK:
        _TF_TOKENS[token] = time.time() + _TF_TOKEN_TTL
        _tf_state_save()
    return token


def _tf_token_valid():
    """请求携带的令牌是否有效（Cookie / 请求头 / 查询参数，SSE 只能走查询参数）"""
    token = (request.cookies.get(_TF_COOKIE)
             or request.headers.get("X-Transfer-Token")
             or request.args.get("tf_token") or "").strip()
    if not token:
        return False
    with _TF_GUARD_LOCK:
        exp = _TF_TOKENS.get(token)
        if not exp:
            return False
        if exp < time.time():
            _TF_TOKENS.pop(token, None)
            return False
    return True


@app.before_request
def _guard_transfer_external_access():
    """外网互传门禁：公网访问 /api/transfer/* 必须持有效令牌（局域网与本机不受影响）"""
    path = request.path or ""
    if not path.startswith("/api/transfer/") or path in _TF_GUARD_PATHS:
        return None
    if not _is_external_request() or _tf_token_valid():
        return None
    return jsonify({"ok": False, "error": "需要校验码", "need_code": True}), 401


@app.route("/api/transfer/auth-probe")
def api_transfer_auth_probe():
    """手机端探测：是否需要校验码、手头令牌是否有效（不泄露校验码本身）"""
    external = _is_external_request()
    return jsonify({"need": external, "verified": (not external) or _tf_token_valid()})


@app.route("/api/transfer/verify", methods=["POST"])
def api_transfer_verify():
    """手机端提交 4 位校验码换取令牌；局域网访问直接放行"""
    if not _is_external_request():
        return jsonify({"ok": True, "need_code": False})
    ip = str(request.remote_addr or "?")
    now = time.time()
    with _TF_GUARD_LOCK:
        _fails, until = _TF_FAIL.get(ip, [0, 0])
        if until and now < until:
            left_min = int((until - now) // 60) + 1
            return jsonify({"ok": False, "error": f"错误次数过多，请 {left_min} 分钟后再试"}), 429
    code = str((request.json or {}).get("code", "")).strip()
    if len(code) != 4 or not code.isdigit():
        return jsonify({"ok": False, "error": "请输入 4 位数字校验码"}), 400
    if code != _tf_guard_code():
        with _TF_GUARD_LOCK:
            rec = list(_TF_FAIL.get(ip) or [0, 0])
            rec[0] += 1
            if rec[0] >= _TF_MAX_FAIL:
                rec = [0, now + _TF_LOCK_SEC]
            _TF_FAIL[ip] = rec
            left = _TF_MAX_FAIL - rec[0] if not rec[1] else 0
        msg = "校验码不正确" + (f"，还可尝试 {left} 次" if left > 0 else "，已锁定 5 分钟")
        return jsonify({"ok": False, "error": msg}), 401
    with _TF_GUARD_LOCK:
        _TF_FAIL.pop(ip, None)
    resp = jsonify({"ok": True})
    resp.set_cookie(_TF_COOKIE, _tf_issue_token(), max_age=_TF_TOKEN_TTL,
                    path="/", samesite="Lax")
    print(f"[Transfer] 外网设备校验通过：{ip}")
    return resp


@app.route("/api/transfer/guard")
def api_transfer_guard():
    """电脑端读取校验码与外网地址；外网请求一律拒绝，避免校验码被远端读到"""
    if _is_external_request():
        return jsonify({"ok": False, "error": "校验码仅显示在运行工具的电脑上"}), 403
    public_url = ((_load_config().get("transfer", {}) or {}).get("public_url", "") or "")
    return jsonify({
        "ok": True,
        "code": _tf_guard_code(),
        "lan_url": f"http://{LOCAL_IP}:{PORT}/mobile",
        "public_url": public_url,
        "wan_ready": bool(public_url),
    })


@app.route("/api/transfer/guard/refresh", methods=["POST"])
def api_transfer_guard_refresh():
    """换新校验码：旧令牌全部作废，已联网的外网设备需重新输入"""
    if _is_external_request():
        return jsonify({"ok": False, "error": "仅限本机操作"}), 403
    code = _tf_guard_code(force_new=True)
    print(f"[Transfer] 外网校验码已刷新：{code}（已发令牌全部作废）")
    return jsonify({"ok": True, "code": code})


@app.route("/api/transfer/public-url", methods=["GET", "POST"])
def api_transfer_public_url():
    """互传外网访问地址：https://xxx.trycloudflare.com 或 http://公网IP:端口"""
    if request.method == "GET":
        url = ((_load_config().get("transfer", {}) or {}).get("public_url", "") or "")
        return jsonify({"ok": True, "url": url})
    if _is_external_request():
        return jsonify({"ok": False, "error": "仅限本机修改"}), 403
    url = str((request.json or {}).get("url", "") or "").strip().rstrip("/")
    if url:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or " " in url:
            return jsonify({"ok": False, "error": "请填写形如 https://abc.example.com 或 http://1.2.3.4:5050"}), 400
    cfg = _load_config()
    cfg.setdefault("transfer", {})["public_url"] = url
    _save_config(cfg)
    print(f"[Transfer] 外网访问地址已{'设置' if url else '清空'}：{url or '（空）'}")
    return jsonify({"ok": True, "url": url})


# ── 公网通道：一键拉起 cloudflared 快速隧道，给手机借一个公网地址 ──
# 场景：手机用流量（不在同一 WiFi）扫码传文件。隧道靠本机主动出站建立，
# 因此不需要公网 IP、不用在路由器做端口映射；进程关闭即失效、域名作废。
_TF_TUN = {"proc": None, "url": "", "starting": False, "log": []}
_TF_TUN_LOCK = threading.Lock()
_TF_TUN_WAIT = 25  # 等待 cloudflared 打印公网地址的最长秒数
_TF_TUN_URL_RE = r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com"


def _tf_tunnel_exe():
    """定位 cloudflared：工具目录 → 项目根 → tools/ → 常见安装位置 → PATH"""
    import shutil
    for d in (_entry_dir, _app_root, os.path.join(_app_root, "tools")):
        for name in ("cloudflared.exe", "cloudflared"):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    # winget / MSI 安装后当前进程的 PATH 不会刷新，按固定位置兜底
    for p in (r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
              r"C:\Program Files\cloudflared\cloudflared.exe",
              os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Microsoft\WinGet\Links\cloudflared.exe")):
        if p and os.path.isfile(p):
            return p
    return shutil.which("cloudflared")


def _tf_tunnel_running():
    proc = _TF_TUN.get("proc")
    return bool(proc is not None and proc.poll() is None)


def _tf_set_public_url(url):
    """公网地址与设置面板里手填的「互传外网地址」是同一字段，双向自动同步"""
    cfg = _load_config()
    cfg.setdefault("transfer", {})["public_url"] = url or ""
    _save_config(cfg)


def _tf_tunnel_reader(proc):
    """后台读 cloudflared 输出，解析出分配的公网域名后自动回填配置"""
    import re
    pat = re.compile(_TF_TUN_URL_RE)
    try:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "ignore").strip()
            if not line:
                continue
            _TF_TUN["log"].append(line)
            if len(_TF_TUN["log"]) > 80:
                del _TF_TUN["log"][:40]
            m = pat.search(line)
            if m and not _TF_TUN.get("url"):
                _TF_TUN["url"] = m.group(0)
                _TF_TUN["starting"] = False
                _tf_set_public_url(_TF_TUN["url"])
                print(f"[Transfer] 公网通道已建立：{_TF_TUN['url']}")
    except Exception:
        pass
    finally:
        _TF_TUN["starting"] = False


def _tf_tunnel_forget_url():
    """清空临时隧道域名：trycloudflare 快速隧道的域名随进程退出即刻作废，
    若继续留在配置里，二维码会指向一个打不开的地址（手机扫码直接失败）"""
    import re
    try:
        url = str(((_load_config().get("transfer", {}) or {}).get("public_url", "")) or "").strip()
        if url and re.match(_TF_TUN_URL_RE + r"/?$", url):
            _tf_set_public_url("")
            print(f"[Transfer] 已清空失效的公网地址：{url}（隧道未运行，二维码回退局域网）")
            return True
    except Exception:
        pass
    return False


def _tf_tunnel_cleanup():
    """退出时结束隧道进程，避免留下后台 cloudflared，并清掉已作废的公网地址"""
    proc = _TF_TUN.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
    _tf_tunnel_forget_url()


atexit.register(_tf_tunnel_cleanup)
# 上次运行异常退出（关窗/断电）时地址会残留，启动先做一次清理
_tf_tunnel_forget_url()


@app.route("/api/transfer/tunnel/start", methods=["POST"])
def api_transfer_tunnel_start():
    """开启公网通道（仅本机）：拉起 cloudflared 快速隧道并自动回填公网地址"""
    if _is_external_request():
        return jsonify({"ok": False, "error": "仅限本机操作"}), 403
    exe = _tf_tunnel_exe()
    if not exe:
        return jsonify({"ok": False, "available": False,
                        "error": "未找到 cloudflared.exe，请下载后放到工具目录"}), 400
    with _TF_TUN_LOCK:
        if _tf_tunnel_running() and _TF_TUN.get("url"):
            return jsonify({"ok": True, "url": _TF_TUN["url"], "running": True})
        _TF_TUN.update({"proc": None, "url": "", "starting": True})
        del _TF_TUN["log"][:]
        cmd = [exe, "tunnel", "--url", f"http://127.0.0.1:{PORT}", "--no-autoupdate"]
        kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, **kwargs)
        except Exception as e:
            _TF_TUN["starting"] = False
            return jsonify({"ok": False, "error": f"启动失败：{e}"}), 500
        _TF_TUN["proc"] = proc
    threading.Thread(target=_tf_tunnel_reader, args=(proc,), daemon=True).start()
    deadline = time.time() + _TF_TUN_WAIT
    while time.time() < deadline:
        if _TF_TUN.get("url"):
            print(f"[Transfer] 手机可用流量扫码：{_TF_TUN['url']}/mobile")
            return jsonify({"ok": True, "url": _TF_TUN["url"], "running": True})
        if proc.poll() is not None:
            _TF_TUN["starting"] = False
            detail = " | ".join(_TF_TUN["log"][-4:]) or f"退出码 {proc.returncode}"
            return jsonify({"ok": False, "error": f"通道未能建立：{detail}"}), 500
        time.sleep(0.4)
    return jsonify({"ok": False, "error": f"等待公网地址超时（{_TF_TUN_WAIT} 秒），请检查网络后重试"}), 504


@app.route("/api/transfer/tunnel/stop", methods=["POST"])
def api_transfer_tunnel_stop():
    """关闭公网通道：结束隧道、清空公网地址、作废已签发的令牌"""
    if _is_external_request():
        return jsonify({"ok": False, "error": "仅限本机操作"}), 403
    with _TF_TUN_LOCK:
        proc = _TF_TUN.get("proc")
        _TF_TUN.update({"proc": None, "url": "", "starting": False})
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    _tf_set_public_url("")
    with _TF_GUARD_LOCK:
        _TF_TOKENS.clear()  # 通道关闭后旧令牌立即失效
    _tf_state_save()
    print("[Transfer] 公网通道已关闭")
    return jsonify({"ok": True})


@app.route("/api/transfer/tunnel")
def api_transfer_tunnel_status():
    """公网通道状态：本机面板使用（外网请求由互传门禁拦截，不泄露隧道日志）"""
    running = _tf_tunnel_running()
    if not running and _TF_TUN.get("proc") is not None:
        # 进程意外退出：清掉残留地址，避免面板上挂着一个打不开的二维码
        _TF_TUN.update({"proc": None, "url": "", "starting": False})
        _tf_set_public_url("")
    if _is_external_request():
        return jsonify({"ok": True, "running": running})
    cfg_url = ((_load_config().get("transfer", {}) or {}).get("public_url", "") or "")
    exe = _tf_tunnel_exe()
    return jsonify({
        "ok": True,
        "running": running,
        "starting": bool(_TF_TUN.get("starting")),
        "available": bool(exe),
        # 已安装返回真实路径；未安装返回"该放哪"，供前端提示
        "exe_path": exe or os.path.join(_entry_dir, "cloudflared.exe"),
        "url": _TF_TUN.get("url", "") or (cfg_url if running else ""),
        "public_url": cfg_url,
        "lan_url": f"http://{LOCAL_IP}:{PORT}/mobile",
        "log": _TF_TUN["log"][-4:],
    })


# ── 本机系统代理开关（PC 接入：把本机流量导入抓包引擎）────────────
# 开启前的注册表原始设置备份（进程内保存；进程被强杀时由下次启动预检兜底清理）
_capture_sysproxy_backup = None
_capture_sysproxy_lock = threading.Lock()


def _local_ca_installed():
    """检测本机是否已安装项目自签 CA（Windows 当前用户 + 本地计算机「受信任的根证书颁发机构」）。

    返回 True/False；非 Windows 或 certutil 不可用时返回 None（前端对 None 不提示，避免误报）。
    """
    if os.name != "nt":
        return None
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        with open(CA_CERT_FILE, "rb") as f:
            fp = x509.load_pem_x509_certificate(f.read()).fingerprint(hashes.SHA1()).hex().upper()
        outs = []
        for args in (["certutil", "-user", "-store", "Root"], ["certutil", "-store", "Root"]):
            try:
                r = subprocess.run(args, capture_output=True, timeout=15, creationflags=0x08000000)
                outs.append((r.stdout or b"").decode("gbk", errors="ignore"))
            except Exception:
                pass
        if not outs:
            return None  # certutil 不可用，无法检测
        for out in outs:
            for raw in re.findall(r"(?:证书哈希\(sha1\)|Cert Hash\(sha1\))[:：]?\s*([0-9a-fA-F ]{30,})", out):
                if re.sub(r"[^0-9A-Fa-f]", "", raw).upper() == fp:
                    return True
        return False
    except Exception:
        return None


def _capture_proxy_is_ours():
    """系统手动代理是否正指向抓包端口（且已启用）"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0, winreg.KEY_READ,
        )
        try:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
        except FileNotFoundError:
            enable = 0
        try:
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        except FileNotFoundError:
            server = ""
        winreg.CloseKey(key)
        if not enable or not server:
            return False
        return f":{_CAPTURE_PORT}" in str(server)
    except Exception:
        return False


def _refresh_wininet():
    """通知 WinINET 代理设置已变更，让新启动的程序尽快感知"""
    try:
        import ctypes
        wininet = ctypes.windll.wininet
        wininet.InternetSetOptionW(0, 39, 0, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
        wininet.InternetSetOptionW(0, 37, 0, 0)  # INTERNET_OPTION_REFRESH
    except Exception:
        pass


def _set_capture_system_proxy(enable):
    """开/关本机系统代理（手动代理模式，指向抓包端口）

    开启前备份原手动代理设置，关闭时精确恢复；
    系统 PAC 开启时拒绝操作（避免与「代理 Mock」的分流互相干扰）。
    """
    global _capture_sysproxy_backup
    import winreg
    with _capture_sysproxy_lock:
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
                0, winreg.KEY_ALL_ACCESS,
            )
        except Exception as e:
            return {"ok": False, "error": f"无法访问系统代理设置：{e}"}
        try:
            if enable:
                try:
                    pac_url, _ = winreg.QueryValueEx(key, "AutoConfigURL")
                except FileNotFoundError:
                    pac_url = ""
                if pac_url:
                    return {"ok": False, "error": "系统已启用 PAC 代理（来自「代理 Mock」），请先关闭后再开启抓包系统代理"}
                backup = {}
                for name in ("ProxyEnable", "ProxyServer", "ProxyOverride"):
                    try:
                        v, t = winreg.QueryValueEx(key, name)
                        backup[name] = [v, t]
                    except FileNotFoundError:
                        pass
                _capture_sysproxy_backup = backup
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, f"127.0.0.1:{_CAPTURE_PORT}")
                # 绕过工具箱自身页面（本机 IP 访问），避免页面请求经代理产生自噪音
                winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, f"127.0.0.1;localhost;{LOCAL_IP};<local>")
                print(f"[Capture] 系统代理已开启 -> 127.0.0.1:{_CAPTURE_PORT}")
            else:
                backup = _capture_sysproxy_backup
                if backup is not None:
                    for name in ("ProxyEnable", "ProxyServer", "ProxyOverride"):
                        if name in backup:
                            v, t = backup[name]
                            winreg.SetValueEx(key, name, 0, t, v)
                        else:
                            try:
                                winreg.DeleteValue(key, name)
                            except FileNotFoundError:
                                pass
                    _capture_sysproxy_backup = None
                    print("[Capture] 系统代理已恢复为原设置")
                elif _capture_proxy_is_ours():
                    # 无备份（跨进程残留场景）：仅当指向抓包端口时才关闭
                    winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
                    print("[Capture] 发现指向抓包端口的系统代理，已关闭")
        finally:
            winreg.CloseKey(key)
        _refresh_wininet()
        return {"ok": True, "enabled": enable}


@app.route("/api/capture/sys-proxy", methods=["GET", "POST"])
def api_capture_sys_proxy():
    """本机系统代理开关：GET 查状态 / POST {enable: bool}"""
    if request.method == "GET":
        return jsonify({"enabled": _capture_proxy_is_ours()})
    data = request.get_json(silent=True) or {}
    enable = bool(data.get("enable"))
    if enable and (_capture_engine is None or not _capture_engine.is_running()):
        return jsonify({"ok": False, "error": "请先启动抓包引擎，再开启系统代理（否则本机会断网）"}), 400
    r = _set_capture_system_proxy(enable)
    if not r.get("ok"):
        return jsonify(r), 400
    r["enabled"] = _capture_proxy_is_ours()
    if enable and r["enabled"]:
        # 本机 CA 是否已安装（前端仅在对 False 时提示装证书，True/None 不打扰）
        r["ca_installed"] = _local_ca_installed()
    # 联动 Web 来源记录开关（Web 代理关闭 = 不记录 Web 流量）
    _CAPTURE_SCOPE["web"] = bool(enable and r["enabled"])
    _save_capture_scope()
    return jsonify(r)


@app.route("/api/capture/scope", methods=["GET", "POST"])
def api_capture_scope():
    """流量来源记录开关：GET 读取 / POST {source: web|device, enabled: bool}"""
    if request.method == "GET":
        return jsonify(dict(_CAPTURE_SCOPE))
    data = request.get_json(silent=True) or {}
    src = data.get("source")
    if src not in ("web", "device"):
        return jsonify({"ok": False, "error": "source 须为 web 或 device"}), 400
    if src == "device" and data.get("enabled") and not (_capture_engine is not None and _capture_engine.is_running()):
        return jsonify({"ok": False, "error": "请先启动抓包引擎，再开启手机代理（引擎未运行时不会记录任何流量）"}), 400
    _CAPTURE_SCOPE[src] = bool(data.get("enabled"))
    if not _save_capture_scope():
        return jsonify({"ok": False, "error": "开关保存失败，请检查 data 目录写入权限"}), 500
    return jsonify({"ok": True, "scope": dict(_CAPTURE_SCOPE)})


@app.route("/api/capture/config", methods=["GET", "POST"])
def api_capture_config():
    """抓包引擎配置：GET 读取 / POST 保存（目前仅端口）"""
    global _CAPTURE_PORT
    if request.method == "GET":
        return jsonify({"port": _CAPTURE_PORT, "web_port": PORT})

    data = request.get_json(silent=True) or {}
    try:
        port = int(data.get("port"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "端口必须是数字"}), 400
    if not (1 <= port <= 65535):
        return jsonify({"ok": False, "error": "端口范围须为 1-65535"}), 400
    if port in (PORT, _HTTPS_PROXY_PORT):
        return jsonify({"ok": False, "error": f"端口 {port} 已被工具箱自身占用（主服务 / HTTPS 代理），请换一个"}), 400
    if _capture_engine is not None and _capture_engine.is_running():
        return jsonify({"ok": False, "error": "抓包运行中，请先停止抓包再修改端口"}), 400

    # 残留系统代理兜底：代理仍指向旧抓包端口（引擎异常退出后残留）时先恢复——
    # 否则改端口后该代理既无法按新端口识别、也无法通过界面关闭，导致本机断网
    if _capture_proxy_is_ours():
        _set_capture_system_proxy(False)

    cfg = _load_capture_config()
    cfg["port"] = port
    if not _save_capture_config(cfg):
        return jsonify({"ok": False, "error": "配置保存失败，请检查 data 目录写入权限"}), 500
    _CAPTURE_PORT = port
    busy = False
    if _capture_engine is not None:
        _capture_engine.set_port(port)
        busy = _capture_engine.port_in_use()
    return jsonify({"ok": True, "port": port, "port_in_use": busy})


@app.route("/api/capture/status")
def api_capture_status():
    """抓包引擎状态"""
    if _capture_engine is None:
        return jsonify({"available": False, "running": False, "error": "引擎模块未加载"})
    st = _capture_engine.status()
    st["available"] = True
    st["local_ip"] = LOCAL_IP
    st["cert_ready"] = os.path.exists(CA_CERT_FILE)
    with _capture_lock:
        st["flow_count"] = len(_capture_flows)
        st["dropped"] = _capture_dropped
    st["sys_proxy"] = _capture_proxy_is_ours()
    st["scope"] = dict(_CAPTURE_SCOPE)
    return jsonify(st)


@app.route("/api/capture/start", methods=["POST"])
def api_capture_start():
    """启动抓包引擎（手动按需）"""
    if _capture_engine is None:
        return jsonify({"ok": False, "error": "引擎模块未加载"}), 500
    # 引擎启动前必须确保项目 CA 已生成：否则 mitmproxy 在空 confdir 下会自行
    # 生成另一套自签 CA，与设备安装的项目 CA 不一致 → HTTPS 流量全量证书报错
    if not ensure_ca_cert():
        return jsonify({"ok": False, "error": "CA 证书生成失败，无法启动抓包（请检查 certs 目录写入权限）"}), 500
    return jsonify(_capture_engine.start())


@app.route("/api/capture/stop", methods=["POST"])
def api_capture_stop():
    """停止抓包引擎（系统代理指向抓包端口时自动恢复，防断网）"""
    if _capture_engine is None:
        return jsonify({"ok": False, "error": "引擎模块未加载"}), 500
    r = _capture_engine.stop()
    if _capture_proxy_is_ours():
        _set_capture_system_proxy(False)
        r["sys_proxy_restored"] = True
    if _CAPTURE_SCOPE.get("web") or _CAPTURE_SCOPE.get("device"):
        # 引擎已停：Web 通道语义上已关闭；手机代理同理（引擎停止后无法记录任何流量）
        _CAPTURE_SCOPE["web"] = False
        _CAPTURE_SCOPE["device"] = False
        _save_capture_scope()
    return jsonify(r)


@app.route("/api/capture/cleanup", methods=["POST"])
def api_capture_cleanup():
    """强制清理抓包端口占用（遗留进程兜底）"""
    if _capture_engine is None:
        return jsonify({"ok": False, "error": "引擎模块未加载"}), 500
    n = _capture_engine.cleanup_port_occupants()
    return jsonify({"ok": True, "killed": n})


@app.route("/api/capture/engine-cfg", methods=["GET", "POST"])
def api_capture_engine_cfg():
    """引擎功能配置（改包 Mock / 弱网模拟 / 断点调试）：GET 读取 / POST 局部更新

    POST 后写入 _capture_engine.json，addon 0.5s 节流读取——开关实时生效。
    """
    if request.method == "GET":
        return jsonify(_load_capture_engine_cfg())
    data = request.get_json(silent=True) or {}
    cfg = _load_capture_engine_cfg()
    if isinstance(data.get("mock"), dict):
        cfg["mock"]["enabled"] = bool(data["mock"].get("enabled", cfg["mock"]["enabled"]))
    if isinstance(data.get("weaknet"), dict):
        w = data["weaknet"]
        sn = cfg["weaknet"]
        if "enabled" in w:
            sn["enabled"] = bool(w["enabled"])
        # 切换档位（preset 须为已存在的档位名）
        if "preset" in w:
            name = str(w["preset"] or "").strip()[:32]
            if name not in (sn.get("presets") or {}):
                return jsonify({"ok": False, "error": "未知的弱网档位: %s" % name}), 400
            sn["preset"] = name
        # 更新档位参数（可单档/多档 patch；内置档位参数固定不可修改，仅「自定义」档位可编辑）
        patch_map = w.get("presets")
        if isinstance(patch_map, dict):
            for name, patch in patch_map.items():
                name = str(name)[:32]
                if not isinstance(patch, dict):
                    continue
                if name != "custom":
                    return jsonify({"ok": False, "error": "内置档位参数固定不可修改（仅「自定义」档位支持编辑保存）"}), 400
                cur = sn["presets"].get(name)
                if not isinstance(cur, dict):
                    cur = {"label": name[:20]}
                err = _weaknet_preset_merge(cur, patch)
                if err:
                    return jsonify({"ok": False, "error": "档位 %s: %s" % (name, err)}), 400
                sn["presets"][name] = cur
        # 兼容旧前端字段（delay_ms/kbps）：写入自定义档并切到自定义
        if "delay_ms" in w or "kbps" in w:
            cu = sn["presets"].setdefault("custom", {"label": "自定义"})
            if "delay_ms" in w:
                v = w["delay_ms"]
                if not isinstance(v, (int, float)) or isinstance(v, bool) or not (0 <= v <= 120000):
                    return jsonify({"ok": False, "error": "delay_ms 须为 0-120000 的数字"}), 400
                cu["latency_ms"] = int(v)
            if "kbps" in w:
                v = w["kbps"]
                if not isinstance(v, (int, float)) or isinstance(v, bool) or not (0 <= v <= 102400):
                    return jsonify({"ok": False, "error": "kbps 须为 0-102400 的数字"}), 400
                # 旧字段 kbps 语义为 KB/s（KiB），×8 换算为 kbps（千比特）
                cu["bandwidth_kbps"] = min(int(v) * 8, 102400)
            sn["preset"] = "custom"
        # 同步派生旧字段（过渡期旧版 addon / 旧版前端仍读 delay_ms/kbps，KB/s 语义）
        act = (sn["presets"].get(sn.get("preset") or "custom") or {})
        try:
            sn["delay_ms"] = int(act.get("latency_ms") or 0)
            bw = int(act.get("bandwidth_kbps") or 0)
            # kbps（千比特）→ 旧字段 KB/s（KiB）：÷8；不足 1KB/s 时保底 1（避免旧前端误显示"不限速"）
            sn["kbps"] = max(1, round(bw / 8)) if bw > 0 else 0
        except Exception:
            pass
    if isinstance(data.get("breakpoint"), dict):
        b = data["breakpoint"]
        for k in ("request", "response"):
            if k in b:
                cfg["breakpoint"][k] = bool(b[k])
        if "filter" in b:
            cfg["breakpoint"]["filter"] = str(b["filter"] or "").strip()[:500]
    if not _save_capture_engine_cfg(cfg):
        return jsonify({"ok": False, "error": "配置保存失败，请检查 data 目录写入权限"}), 500
    return jsonify({"ok": True, "cfg": cfg})


# 改包 Mock 命中统计（内存计数，服务重启清零）：mapping_id -> {count, last}
_capture_mock_stats = {}


@app.route("/api/capture/mock/stats")
def api_capture_mock_stats():
    """改包 Mock 命中统计（按规则 id 累计次数与最近命中时间）"""
    with _capture_lock:
        return jsonify({"stats": dict(_capture_mock_stats)})


# ── 抓包改包 Mock：独立规则库（与「代理 Mock」的 _mock_proxy.json 完全分离，互不影响）──
# 规则结构：{id, method, host, path, enabled, active_response, responses:[{name,status,status_message,headers,body,delay_ms}]}
# （结构沿用引擎侧同构格式，engine.py 将 CAPTURE_MOCK_FILE 指向本文件，addon 无需改动）
_CAPTURE_MOCK_RULES_FILE = os.path.join(DATA_DIR, "_capture_mock.json")
_CAPTURE_MOCK_SKIP_HEADERS = ("content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive")

if not os.path.exists(_CAPTURE_MOCK_RULES_FILE):
    try:
        with open(_CAPTURE_MOCK_RULES_FILE, "w", encoding="utf-8") as _f:
            json.dump({"delay_ms": 0, "mappings": []}, _f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _load_capture_mock_rules():
    """读取抓包改包 Mock 规则库（文件缺失/损坏时返回空库）"""
    try:
        with _file_lock(_CAPTURE_MOCK_RULES_FILE):
            with open(_CAPTURE_MOCK_RULES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("mappings"), list):
            return data
    except Exception:
        pass
    return {"delay_ms": 0, "mappings": []}


def _save_capture_mock_rules(data):
    with _file_lock(_CAPTURE_MOCK_RULES_FILE):
        with open(_CAPTURE_MOCK_RULES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def _capture_mock_match(mappings, method, host, path):
    """按接口（method+host+path）匹配规则（与引擎侧语义一致：host 空/*、method 空=任意）"""
    for m in mappings:
        if not isinstance(m, dict):
            continue
        if (m.get("path") or "") != path:
            continue
        m_host = (m.get("host") or "").strip()
        if m_host and m_host != "*" and m_host != host:
            continue
        m_method = (m.get("method") or "").strip().upper()
        if m_method and m_method != method:
            continue
        return m
    return None


@app.route("/api/capture/mock/rules", methods=["GET", "POST"])
def api_capture_mock_rules():
    """抓包改包 Mock 规则库：GET 读取 / POST 全量保存（改包 Mock 视图列表用）"""
    if request.method == "GET":
        return jsonify(_load_capture_mock_rules())
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get("mappings"), list):
        return jsonify({"ok": False, "error": "mappings 须为数组"}), 400
    try:
        delay_ms = int(data.get("delay_ms") or 0)
    except Exception:
        delay_ms = 0
    _save_capture_mock_rules({"delay_ms": delay_ms, "mappings": data["mappings"]})
    return jsonify({"ok": True})


@app.route("/api/capture/mock/lookup")
def api_capture_mock_lookup():
    """查询某接口（method+host+path）当前的改包 Mock 配置（流量详情页状态行用）"""
    method = (request.args.get("method") or "").strip().upper()
    host = (request.args.get("host") or "").strip()
    path = (request.args.get("path") or "/").split("?")[0]
    cfg = _load_capture_mock_rules()
    m = _capture_mock_match(cfg.get("mappings") or [], method, host, path)
    if m is None:
        return jsonify({"ok": True, "exists": False})
    responses = m.get("responses") if isinstance(m.get("responses"), list) else []
    try:
        active = min(int(m.get("active_response") or 0), max(len(responses) - 1, 0))
    except Exception:
        active = 0
    cur = responses[active] if responses and isinstance(responses[active], dict) else {}
    return jsonify({
        "ok": True, "exists": True,
        "rule": {
            "id": m.get("id"),
            "enabled": m.get("enabled", True) is not False,
            "response_count": len(responses),
            "active": active,
            "response_name": cur.get("name") or "",
            "status": cur.get("status") if cur.get("status") is not None else 200,
            "status_message": cur.get("status_message") or "",
            "headers": cur.get("headers") if isinstance(cur.get("headers"), dict) else {},
            "body": cur.get("body") if cur.get("body") is not None else "",
        },
    })


@app.route("/api/capture/mock/save", methods=["POST"])
def api_capture_mock_save():
    """详情页「设置 Mock 返回」：为指定接口创建/更新改包 Mock 规则

    - 已存在同接口规则时更新其激活响应报文（保留响应名与延时），否则新建规则；
    - 响应头剔除 content-length / content-encoding / transfer-encoding 等（随 body 变化须重算）；
    - body 可解析为合法 JSON 时存对象（与规则库格式一致）；
    - 保存后自动开启「改包 Mock 拦截」总开关，配置 0.5s 内对引擎生效。
    """
    data = request.get_json(silent=True) or {}
    method = (data.get("method") or "").strip().upper()
    host = (data.get("host") or "").strip()
    path = (data.get("path") or "/").split("?")[0]
    if not host or not path.startswith("/"):
        return jsonify({"ok": False, "error": "接口信息不完整"}), 400
    try:
        status = int(data.get("status") or 200)
    except Exception:
        status = 200
    if not (100 <= status <= 599):
        return jsonify({"ok": False, "error": "状态码须为 100-599"}), 400
    status_message = str(data.get("status_message") or "").strip()[:200]
    headers = data.get("headers") if isinstance(data.get("headers"), dict) else {}
    headers = {str(k): str(v) for k, v in headers.items() if str(k).lower() not in _CAPTURE_MOCK_SKIP_HEADERS}
    body = data.get("body")
    if body is None:
        body = ""
    if isinstance(body, str) and body[:1] in ("{", "[") and len(body) < 512 * 1024:
        try:
            body = json.loads(body)
        except Exception:
            pass

    cfg = _load_capture_mock_rules()
    mappings = cfg.setdefault("mappings", [])
    m = _capture_mock_match(mappings, method, host, path)
    if m is not None:
        responses = m.get("responses") if isinstance(m.get("responses"), list) else []
        if not responses:
            responses = [{}]
            m["responses"] = responses
            m["active_response"] = 0
        try:
            idx = min(int(m.get("active_response") or 0), len(responses) - 1)
        except Exception:
            idx = 0
        entry = responses[idx] if isinstance(responses[idx], dict) else {}
        entry["status"] = status
        entry["status_message"] = status_message
        entry["headers"] = headers
        entry["body"] = body
        responses[idx] = entry
        m["enabled"] = True
        if not (m.get("host") or "").strip():
            m["host"] = host  # 旧规则补上 host，避免同路径误伤其他站点
        rule_id = m.get("id")
        created = False
    else:
        rule_id = "cm_%d_%s" % (int(time.time() * 1000), os.urandom(3).hex())
        mappings.append({
            "id": rule_id, "method": method, "host": host, "path": path, "enabled": True,
            "active_response": 0,
            "responses": [{
                "name": "详情设置 " + datetime.now().strftime("%m-%d %H:%M:%S"),
                "status": status, "status_message": status_message, "headers": headers,
                "body": body, "delay_ms": 0,
            }],
        })
        created = True
    _save_capture_mock_rules(cfg)

    # 兼容层：旧版 addon（未随引擎重启更新）仍读取 mock.enabled 总闸，保存时恒置开；
    # 新版 addon 已不再读取该字段，拦截与否完全由每条规则自身的 enabled 决定
    eng = _load_capture_engine_cfg()
    if not eng["mock"].get("enabled"):
        eng["mock"]["enabled"] = True
        _save_capture_engine_cfg(eng)
    return jsonify({"ok": True, "rule_id": rule_id, "created": created})


@app.route("/api/capture/mock/remove", methods=["POST"])
def api_capture_mock_remove():
    """删除一条改包 Mock 规则（详情页「移除该接口的 Mock」用）"""
    data = request.get_json(silent=True) or {}
    rule_id = data.get("rule_id")
    cfg = _load_capture_mock_rules()
    mappings = cfg.get("mappings") or []
    rest = [m for m in mappings if not (isinstance(m, dict) and m.get("id") == rule_id)]
    if len(rest) == len(mappings):
        return jsonify({"ok": False, "error": "规则不存在（可能已删除）"}), 404
    cfg["mappings"] = rest
    _save_capture_mock_rules(cfg)
    return jsonify({"ok": True})


# ── 断点调试：addon 命中后挂起，等前端编辑决策（超时 300s 自动放行）──
_capture_bp_lock = threading.Lock()
_capture_bp_pending = {}  # id -> {"event": Event, "decision": dict|None, "info": dict}
_CAPTURE_BP_TIMEOUT = 300


@app.route("/api/capture/breakpoint/hit", methods=["POST"])
def api_capture_bp_hit():
    """内部接口：addon 断点挂起（仅本机来源），阻塞等待前端决策后返回

    Flask 多线程下每个请求独立线程，挂起不影响页面轮询等其他请求。
    """
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    bp_id = "bp_" + os.urandom(6).hex()
    event = threading.Event()
    now = time.time()
    info = {
        "id": bp_id,
        "phase": data.get("phase") or "request",
        "flow": data.get("flow") or {},
        "hit_time": datetime.now().strftime("%H:%M:%S"),
        "deadline": now + _CAPTURE_BP_TIMEOUT,
    }
    with _capture_bp_lock:
        _capture_bp_pending[bp_id] = {"event": event, "decision": None, "info": info}
    event.wait(timeout=_CAPTURE_BP_TIMEOUT)
    with _capture_bp_lock:
        entry = _capture_bp_pending.pop(bp_id, None)
    decision = (entry or {}).get("decision") or {"action": "continue"}  # 超时/异常默认放行
    return jsonify({"decision": decision})


@app.route("/api/capture/breakpoint/pending")
def api_capture_bp_pending():
    """待处理断点列表（前端轮询）"""
    now = time.time()
    with _capture_bp_lock:
        items = []
        for e in _capture_bp_pending.values():
            info = dict(e["info"])
            info["remain"] = max(0, int(info["deadline"] - now))
            info.pop("deadline", None)
            items.append(info)
    items.sort(key=lambda x: x.get("hit_time") or "")
    return jsonify({"pending": items})


@app.route("/api/capture/breakpoint/decide", methods=["POST"])
def api_capture_bp_decide():
    """前端提交断点决策：action=continue|drop（可附编辑字段 method/url/status/headers/body）"""
    data = request.get_json(silent=True) or {}
    bp_id = data.get("id")
    action = data.get("action") or "continue"
    if action not in ("continue", "drop"):
        return jsonify({"ok": False, "error": "action 须为 continue 或 drop"}), 400
    with _capture_bp_lock:
        entry = _capture_bp_pending.get(bp_id)
    if not entry:
        return jsonify({"ok": False, "error": "该断点已超时或已被处理"}), 410
    decision = {"action": action}
    for k in ("method", "url", "status", "headers", "body"):
        if k in data and data[k] is not None:
            decision[k] = data[k]
    entry["decision"] = decision
    entry["event"].set()
    return jsonify({"ok": True})


# ── 设备档案：按客户端 IP 记忆设备名 / 系统（App 的 UA 为 libcurl 等无型号串时也能区分设备）──
CAPTURE_DEVICES_FILE = os.path.join(DATA_DIR, "_capture_device_profiles.json")
_capture_dev_lock = threading.Lock()
_capture_dev_cache = None


def _load_capture_devices():
    """读取设备档案（内存缓存，POST 保存时同步刷新）"""
    global _capture_dev_cache
    if _capture_dev_cache is not None:
        return _capture_dev_cache
    d = {}
    try:
        with open(CAPTURE_DEVICES_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            d = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    except FileNotFoundError:
        pass
    except Exception as e:
        print("[capture] 读取设备档案失败: %s" % e)
    _capture_dev_cache = d
    return d


@app.route("/api/capture/device-profiles", methods=["GET", "POST"])
def api_capture_device_profiles():
    """设备档案：GET 全量 / POST 按 IP 设置（name 与 os 均为空则删除）"""
    global _capture_dev_cache
    if request.method == "GET":
        return jsonify({"profiles": _load_capture_devices()})
    data = request.get_json(silent=True) or {}
    ip = str(data.get("ip") or "").strip()
    if not ip:
        return jsonify({"ok": False, "error": "缺少客户端 IP"}), 400
    name = str(data.get("name") or "").strip()[:40]
    os_name = str(data.get("os") or "").strip()[:40]
    with _capture_dev_lock:
        d = dict(_load_capture_devices())
        if not name and not os_name:
            d.pop(ip, None)
        else:
            d[ip] = {"name": name, "os": os_name}
        try:
            os.makedirs(os.path.dirname(CAPTURE_DEVICES_FILE), exist_ok=True)
            tmp = CAPTURE_DEVICES_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            os.replace(tmp, CAPTURE_DEVICES_FILE)
        except Exception as e:
            return jsonify({"ok": False, "error": "保存失败: %s" % e}), 500
        _capture_dev_cache = d
    return jsonify({"ok": True, "ip": ip, "profile": d.get(ip) or {}})


# ── 局域网设备探测：反向 DNS 主机名 + mDNS 设备名 + MAC 厂商（仅私网 IP）──
# 常见手机厂商 OUI 前缀（粗匹配，仅作提示，不自动作为设备名）
_CAPTURE_OUI_VENDORS = {
    "00:1B:63": "Apple", "3C:07:54": "Apple", "8C:85:90": "Apple", "F0:18:98": "Apple",
    "A4:83:E7": "Apple", "DC:A9:04": "Apple", "F4:5C:89": "Apple",
    "00:E0:FC": "华为", "48:7B:6B": "华为", "78:F5:FD": "华为", "AC:E2:15": "华为",
    "B0:5B:67": "华为", "E0:24:7F": "华为",
    "28:6C:07": "小米", "64:B4:73": "小米", "8C:BE:BE": "小米",
    "5C:0A:5B": "三星", "8C:77:12": "三星", "34:23:BA": "三星",
    "2C:5B:B8": "OPPO", "5C:02:14": "OPPO", "94:65:2D": "OPPO",
    "3C:0D:5A": "vivo", "84:38:35": "vivo", "60:8F:5C": "vivo",
    "B4:6D:83": "荣耀", "F4:9F:F3": "荣耀",
    "C0:EE:FB": "一加", "94:98:69": "一加",
}
_CAPTURE_BRAND_WORDS = (
    ("iphone", "iPhone"), ("ipad", "iPad"), ("huawei", "华为"), ("honor", "荣耀"),
    ("xiaomi", "小米"), ("redmi", "Redmi"), ("oppo", "OPPO"), ("vivo", "vivo"),
    ("samsung", "三星"), ("galaxy", "三星"), ("oneplus", "一加"), ("pixel", "Pixel"),
    ("realme", "realme"), ("meizu", "魅族"),
)


def _probe_reverse_dns(ip, timeout=2.5):
    """反向 DNS：带超时的 gethostbyaddr（失败返回空串）"""
    out = {"host": ""}

    def _run():
        try:
            out["host"] = str(socket.gethostbyaddr(ip)[0] or "")
        except Exception:
            pass

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(timeout)
    return out["host"]


def _probe_arp_mac(ip):
    """ARP 表查询 MAC（Windows arp -a <ip>；跨网段/离线时为空）"""
    try:
        r = subprocess.run(["arp", "-a", ip], capture_output=True, timeout=3)
        text = (r.stdout or b"").decode("gbk", errors="ignore") + (r.stderr or b"").decode("gbk", errors="ignore")
        m = re.search(r"([0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5})", text)
        return m.group(1).replace("-", ":").upper() if m else ""
    except Exception:
        return ""


def _dns_read_name(data, off):
    """解析 DNS 名称（支持压缩指针），返回 (name, next_offset)"""
    labels = []
    jumped = False
    next_off = off
    for _ in range(16):
        if off >= len(data):
            break
        ln = data[off]
        if ln & 0xC0 == 0xC0:
            if off + 1 >= len(data):
                break
            ptr = ((ln & 0x3F) << 8) | data[off + 1]
            if not jumped:
                next_off = off + 2
            jumped = True
            off = ptr
            continue
        off += 1
        if ln == 0:
            if not jumped:
                next_off = off
            break
        labels.append(data[off:off + ln].decode("utf-8", errors="ignore"))
        off += ln
        if not jumped:
            next_off = off
    return ".".join(labels), next_off


def _probe_mdns(ip, timeout=1.5):
    """单播 mDNS 查询设备名（legacy unicast：事务 ID 非 0 + QU 位，响应回临时端口）

    主要命中 Apple 设备（_device-info / _airplay）；Android 多数不响应，返回空。
    """
    names = []
    model = ""
    for svc in ("_device-info._tcp.local", "_airplay._tcp.local"):
        try:
            qname = b"".join(bytes([len(p)]) + p.encode() for p in svc.split(".")) + b"\x00"
            pkt = b"\x12\x34\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00" + qname + b"\x00\x0c\x80\x01"
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(timeout)
            s.sendto(pkt, (ip, 5353))
            data, _ = s.recvfrom(4096)
            s.close()
            if len(data) < 12:
                continue
            qd = (data[4] << 8) | data[5]
            an = (data[6] << 8) | data[7]
            off = 12
            for _q in range(qd):
                _, off = _dns_read_name(data, off)
                off += 4
            for _a in range(an):
                _, off = _dns_read_name(data, off)
                if off + 10 > len(data):
                    break
                rtype = (data[off] << 8) | data[off + 1]
                rdlen = (data[off + 8] << 8) | data[off + 9]
                rdoff = off + 10
                if rtype == 12:
                    name, _n = _dns_read_name(data, rdoff)
                    inst = name.split(".")[0].replace("\\032", " ").strip()
                    if inst and inst not in names:
                        names.append(inst)
                elif rtype == 16:
                    end = rdoff + rdlen
                    p = rdoff
                    while p < end:
                        ln = data[p]
                        p += 1
                        txt = data[p:p + ln].decode("utf-8", errors="ignore")
                        p += ln
                        if txt.lower().startswith("model=") and not model:
                            model = txt.split("=", 1)[1].strip()
                off = rdoff + rdlen
        except Exception:
            continue
    return (names[0] if names else ""), model


def _probe_host_hint(host):
    """主机名 → (品牌, 疑似型号)；如 HUAWEI-PGT-AN20 → (华为, PGT-AN20)"""
    if not host:
        return "", ""
    low = host.lower()
    brand = ""
    for w, b in _CAPTURE_BRAND_WORDS:
        if w in low:
            brand = b
            break
    cleaned = re.sub(
        r"(?i)(iphone|ipad|huawei|honor|xiaomi|redmi|oppo|vivo|samsung|galaxy|oneplus|pixel|realme|meizu|android|phone|tablet)",
        "", host,
    )
    cleaned = re.sub(r"^[-_.\s]+|[-_.\s]+$", "", cleaned)
    # 仅在主机名带品牌词时才推断型号（避免把电脑名 DESKTOP-xxx 误当型号）
    model = ""
    if brand and 3 <= len(cleaned) <= 20 and re.search(r"[A-Za-z]", cleaned) and re.search(r"\d", cleaned):
        model = cleaned
    return brand, model


@app.route("/api/capture/device-probe", methods=["POST"])
def api_capture_device_probe():
    """局域网设备探测：反向 DNS 主机名 + mDNS 设备名 + MAC 厂商（仅私网 IP）

    返回候选设备信息供「标注设备」弹窗填入，不自动保存。
    """
    import ipaddress
    data = request.get_json(silent=True) or {}
    ip = str(data.get("ip") or "").strip()
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return jsonify({"ok": False, "error": "IP 格式不正确"}), 400
    if not (addr.is_private or addr.is_loopback):
        return jsonify({"ok": False, "error": "仅支持探测局域网内网 IP"}), 400
    host = _probe_reverse_dns(ip)
    mac = _probe_arp_mac(ip)
    vendor = _CAPTURE_OUI_VENDORS.get(mac[:8].upper(), "") if mac else ""
    mdns_name, mdns_model = _probe_mdns(ip)
    brand, model = _probe_host_hint(host)
    if mdns_model:
        model = model or mdns_model
    candidates = []
    if mdns_name:
        candidates.append({"name": mdns_name, "source": "mDNS 设备名"})
    if host and host.lower() != "localhost":
        candidates.append({"name": host, "source": "反向 DNS 主机名"})
    if not candidates and vendor:
        candidates.append({"name": vendor + "设备", "source": "MAC 厂商（粗略）"})
    os_hint = ""
    low = (mdns_name or "").lower()
    if "ipad" in low:
        os_hint = "iPadOS"
    elif "iphone" in low:
        os_hint = "iOS"
    return jsonify({
        "ok": True, "ip": ip, "host": host, "mac": mac, "vendor": vendor,
        "brand": brand, "model": model, "os_hint": os_hint, "candidates": candidates,
    })


# ── User-Agent 解析：设备型号 / 操作系统（多台手机并存时用于区分设备）──
def _parse_ua_env(ua):
    """User-Agent → (设备名, 系统名)；解析失败返回空串

    设备名取 UA 中的型号（如 PGT-AN20 / SM-G991B / iPhone / iPad）；
    系统名带版本（Android 14 / iOS 17.4 / HarmonyOS 4.0 / Windows 10/11 / macOS 13.5）。
    代理层拿不到用户在手机设置里自定义的设备名称，型号 + 客户端 IP 组合用于区分设备。
    """
    ua = str(ua or "").strip()
    if not ua:
        return "", ""
    low = ua.lower()
    # iOS / iPadOS：Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) …
    m = re.search(r"\((iphone|ipad|ipod)\b", low)
    if m:
        device = {"iphone": "iPhone", "ipad": "iPad", "ipod": "iPod"}[m.group(1)]
        vm = re.search(r"\bos (\d+)[._](\d+)(?:[._](\d+))?", low)
        ver = ".".join([x for x in (vm.groups() if vm else ()) if x])
        os_name = ("iPadOS" if device == "iPad" else "iOS") + ((" " + ver) if ver else "")
        return device, os_name
    # HarmonyOS / OpenHarmony：Mozilla/5.0 (Phone; OpenHarmony 4.1) / (…; HarmonyOS 4.0; …)
    m = re.search(r"(harmonyos|openharmony)[ /]?(\d+(?:\.\d+)*)?", low)
    if m:
        brand = "HarmonyOS" if m.group(1) == "harmonyos" else "OpenHarmony"
        os_name = brand + ((" " + m.group(2)) if m.group(2) else "")
        dm = re.search(r"\(([^;()]+);\s*(?:harmonyos|openharmony)", low)
        device = dm.group(1).strip() if dm else ""
        if device.lower() in ("phone", "tablet", "linux", "android", "mobile"):
            device = ""
        return device, os_name
    # Android：Mozilla/5.0 (Linux; Android 14; PGT-AN20 Build/HUAWEIPGT-AN20; wv) …
    m = re.search(r"android\s+(\d+(?:\.\d+)*)", low)
    if m:
        os_name = "Android " + m.group(1)
        dm = re.search(r"android\s+[\d.]+;\s*([^;)]+?)(?:\s+build[/ ]|;|\))", ua, re.I)
        device = dm.group(1).strip() if dm else ""
        # Chrome 缩减 UA 用 "K" 占位；通用词不当作型号
        if device.lower() in ("k", "wv", "mobile", "linux"):
            device = ""
        return device, os_name
    # 桌面系统
    if "windows nt" in low:
        vm = re.search(r"windows nt (\d+\.\d+)", low)
        if vm and vm.group(1) == "10.0":
            os_name = "Windows 10/11"  # NT 10.0 同时覆盖 Win10 / Win11，无法从 UA 区分
        else:
            os_name = "Windows" + ((" NT " + vm.group(1)) if vm else "")
        return "", os_name
    if "macintosh" in low or "mac os x" in low:
        vm = re.search(r"mac os x (\d+)[._](\d+)(?:[._](\d+))?", low)
        ver = ".".join([x for x in (vm.groups() if vm else ()) if x])
        return "", "macOS" + ((" " + ver) if ver else "")
    if "cros" in low:
        return "", "ChromeOS"
    if "linux" in low:
        return "", "Linux"
    return "", ""


def _ingest_capture_record(data):
    """单条流量入库（report 单条/批量共用）

    - id 索引 O(1)：同 id 的在途（pending）→ 终报原地更新对象，位置保持请求开始顺序；
    - body 落盘在 _capture_lock 之外（磁盘 IO 不阻塞流量查询/轮询）；
    - 淘汰时联动删除 body 缓存与磁盘文件（锁外执行）。
    """
    global _capture_seq
    flow_id = data.get("id")
    if not flow_id:
        return False
    # 设备与系统：从请求头 UA 解析（addon 不传时兜底）；型号 + 客户端 IP 区分多台设备
    if not data.get("device") or not data.get("os"):
        ua = ""
        hs = data.get("req_headers")
        if isinstance(hs, dict):
            for hk, hv in hs.items():
                if str(hk).lower() == "user-agent":
                    ua = str(hv)
                    break
        dev, os_name = _parse_ua_env(ua)
        if dev and not data.get("device"):
            data["device"] = dev
        if os_name and not data.get("os"):
            data["os"] = os_name
    # 设备档案覆盖：按 IP 手动标注的型号 / 系统优先（App 的 UA 无型号时也能补全）
    try:
        prof = _load_capture_devices().get(str(data.get("client") or ""))
        if isinstance(prof, dict):
            if prof.get("name"):
                data["device"] = prof["name"]
            if prof.get("os"):
                data["os"] = prof["os"]
    except Exception:
        pass
    mk = data.get("mock")
    if isinstance(mk, dict) and mk.get("id"):
        with _capture_lock:
            st = _capture_mock_stats.get(mk["id"]) or {"count": 0, "last": ""}
            st["count"] += 1
            st["last"] = data.get("time") or ""
            _capture_mock_stats[mk["id"]] = st
    # 先落 body（锁外），再更新列表：列表一旦可见，详情所需的 body 已就绪
    body_data = _extract_body_fields(data)
    _persist_body(flow_id, body_data)
    # WebSocket 消息聚合兜底限长（addon 侧已限 100 条；防止旧版本 / 异常数据撑爆内存）
    ws = data.get("ws")
    if isinstance(ws, dict):
        msgs = ws.get("messages")
        if isinstance(msgs, list) and len(msgs) > 100:
            ws = dict(ws)
            ws["messages"] = msgs[:100]
            ws["truncated"] = True
            data["ws"] = ws
    evicted_ids = []
    with _capture_lock:
        _capture_seq += 1
        data["seq"] = _capture_seq
        old = _capture_flow_index.get(flow_id)
        if old is not None:
            old.clear()
            old.update(data)  # 原地更新：位置不变，前端按 id 合并后行不跳动
        else:
            while len(_capture_flows) >= _CAPTURE_MAX:
                evicted = _capture_flows.pop()
                eid = evicted.get("id", "")
                _capture_flow_index.pop(eid, None)
                evicted_ids.append(eid)
            _capture_flow_index[flow_id] = data
            _capture_flows.appendleft(data)
    for eid in evicted_ids:
        _body_cache_remove(eid)
    return True


@app.route("/api/capture/report", methods=["POST"])
def api_capture_report():
    """内部接口：addon 上报流量（仅允许本机来源）

    兼容两种协议：单条 {id: ...}（旧 addon）与批量 {"records": [...], "dropped": n}
    （新版 addon 攒批上报；dropped 为 addon 队列满丢弃的累计条数）。
    """
    global _capture_dropped
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    dropped = data.get("dropped")
    if isinstance(dropped, int) and dropped > 0:
        _capture_dropped += dropped
    records = data.get("records")
    if isinstance(records, list):
        n = 0
        for rec in records:
            if isinstance(rec, dict) and _ingest_capture_record(rec):
                n += 1
        return jsonify({"ok": True, "n": n})
    if not data.get("id"):
        return jsonify({"error": "missing id"}), 400
    _ingest_capture_record(data)
    return jsonify({"ok": True, "n": 1})


@app.route("/api/capture/flows")
def api_capture_flows():
    """流量列表（since=已见最大 seq 增量拉取；只回摘要字段）"""
    since = request.args.get("since", type=int, default=0)
    with _capture_lock:
        total = len(_capture_flows)
        flows = [f for f in _capture_flows if f.get("seq", 0) > since] if since > 0 else list(_capture_flows)
    keys = ("seq", "id", "time", "method", "scheme", "host", "port", "path", "status",
            "size", "duration_ms", "content_type", "client", "source", "device", "os",
            "error", "mock", "replay", "state", "loss")
    items = []
    for f in flows:
        item = {k: f.get(k) for k in keys}
        ws = f.get("ws")
        if isinstance(ws, dict):
            # 列表只回聚合摘要：消息明细体积大，留在详情接口按需返回
            item["ws"] = {"count": ws.get("count") or 0, "truncated": bool(ws.get("truncated"))}
        items.append(item)
    return jsonify({"flows": items, "total": total})


@app.route("/api/capture/flows/<flow_id>")
def api_capture_flow_detail(flow_id):
    """单条流量详情（含请求/响应 headers 与 body，内存→磁盘二级查找）"""
    with _capture_lock:
        f = _capture_flow_index.get(flow_id)
        result = dict(f) if f is not None else None
    if result is None:
        return jsonify({"error": "流量记录不存在"}), 404
    # 设备档案实时合并：标注后历史流量详情也立即显示（列表摘要不含设备信息）
    prof = _load_capture_devices().get(str(result.get("client") or ""))
    if isinstance(prof, dict):
        if prof.get("name"):
            result["device"] = prof["name"]
        if prof.get("os"):
            result["os"] = prof["os"]
    with _capture_body_lock:
        cached = _capture_body_cache.get(flow_id)
    if not cached:
        cached = _load_body_disk(flow_id)  # 磁盘读在锁外，不阻塞其它流量查询
    if cached:
        result.update(cached)
    else:
        for bk in ("req_headers", "req_body", "resp_headers", "resp_body"):
            result.setdefault(bk, None)
        result["_body_expired"] = True
    return jsonify(result)


# ── 接口重放（详情页「重放」：原始请求经引擎代理原样重发 N 次）─────
# 状态仅存内存（同一时刻单任务串行）；重放流量由引擎正常记录进实时流量列表
import random as _random_mod

_replay_lock = threading.Lock()
_replay_state = {
    "running": False, "total": 0, "current": 0,
    "results": [], "cancel": False, "flow_id": "",
    "summary": None,
}

# 重放请求注入的标记头：addon 识别后跳过断点、标记流量为「重放」，并从真实请求中剔除
_CAPTURE_REPLAY_FLAG_HEADER = "X-ApiTester-Replay"
# 重放时剔除的请求头（长度/主机等由 requests 按实际报文重算；逐跳头不转发）
_CAPTURE_REPLAY_SKIP_HEADERS = {
    "content-length", "host", "connection", "keep-alive", "proxy-connection",
    "te", "trailer", "transfer-encoding", "accept-encoding",
}


def _capture_replay_fetch_request(flow_id):
    """按 flow_id 取原始请求（内存→磁盘二级查找；找不到返回 None）"""
    with _capture_lock:
        f = _capture_flow_index.get(flow_id)
        result = dict(f) if f is not None else None
    if result is None:
        return None
    with _capture_body_lock:
        cached = _capture_body_cache.get(flow_id)
    if not cached:
        cached = _load_body_disk(flow_id)
    if cached:
        result.update(cached)
    return result


def _capture_replay_worker(state, rd, count, mode, interval_ms, interval_min_ms, interval_max_ms):
    """重放后台线程：串行逐次原样重发；间隔 sleep 分片以便即时响应取消"""
    scheme = (rd.get("scheme") or "http").strip().lower()
    host = (rd.get("host") or "").strip()
    path = rd.get("path") or "/"
    try:
        port = int(rd.get("port") or 0)
    except Exception:
        port = 0
    if port <= 0:
        port = 443 if scheme == "https" else 80
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        url = "%s://%s%s" % (scheme, host, path)
    else:
        url = "%s://%s:%d%s" % (scheme, host, port, path)
    method = (rd.get("method") or "GET").strip().upper()
    headers = {}
    raw_headers = rd.get("req_headers") or {}
    if isinstance(raw_headers, dict):
        for k, v in raw_headers.items():
            if str(k).lower() in _CAPTURE_REPLAY_SKIP_HEADERS:
                continue
            headers[str(k)] = str(v)
    headers[_CAPTURE_REPLAY_FLAG_HEADER] = "1"
    body = rd.get("req_body")
    payload = body.encode("utf-8", "replace") if isinstance(body, str) and body else None
    proxy_url = "http://127.0.0.1:%d" % _CAPTURE_PORT
    proxies = {"http": proxy_url, "https": proxy_url}
    verify = CA_CERT_FILE if os.path.exists(CA_CERT_FILE) else False

    def _sleep_cancellable(seconds):
        """分片 sleep（0.2s），期间检测取消；返回 False 表示被取消"""
        end = time.time() + max(0.0, seconds)
        while True:
            remain = end - time.time()
            if remain <= 0:
                return True
            with _replay_lock:
                if state["cancel"]:
                    return False
            time.sleep(min(0.2, remain))

    try:
        for i in range(1, count + 1):
            with _replay_lock:
                if state["cancel"]:
                    state["summary"]["stopped"] = True
                    break
                state["current"] = i
            if i > 1:
                if mode == "fixed":
                    wait_s = interval_ms / 1000.0
                elif mode == "random":
                    wait_s = _random_mod.randint(interval_min_ms, interval_max_ms) / 1000.0
                else:
                    wait_s = 0.0
                if wait_s > 0 and not _sleep_cancellable(wait_s):
                    with _replay_lock:
                        state["summary"]["stopped"] = True
                    break
            t0 = time.time()
            status = None
            error = None
            size = None
            try:
                # trust_env 为 Session 级属性（禁用环境代理/CA 干扰），非请求参数
                with req_lib.Session() as sess:
                    sess.trust_env = False
                    resp = sess.request(
                        method, url, headers=headers, data=payload,
                        proxies=proxies, verify=verify, timeout=(10, 60),
                    )
                    status = resp.status_code
                    # 响应大小：优先 content-length 头，缺失时读 body 计长（供过程展示）
                    cl = resp.headers.get("content-length")
                    try:
                        size = int(cl) if cl is not None else None
                    except Exception:
                        size = None
                    if size is None:
                        try:
                            size = len(resp.content)
                        except Exception:
                            size = None
                    resp.close()
            except Exception as e:
                error = (str(e) or e.__class__.__name__)[:200]
            duration_ms = int((time.time() - t0) * 1000)
            with _replay_lock:
                state["results"].append({
                    "index": i, "status": status, "duration_ms": duration_ms,
                    "size": size, "at": time.strftime("%H:%M:%S"), "error": error,
                })
                s = state["summary"]
                if status is not None and 200 <= status < 400:
                    s["success"] += 1
                    s["_sum_ms"] += duration_ms
                else:
                    s["fail"] += 1
                s["avg_ms"] = int(s["_sum_ms"] / s["success"]) if s["success"] else 0
    finally:
        with _replay_lock:
            state["running"] = False
            state["cancel"] = False


@app.route("/api/capture/replay", methods=["POST"])
def api_capture_replay_start():
    """启动接口重放（详情页「重放」）：原样请求经引擎代理重发

    入参：{flow_id, count(1-100), interval_mode: none|fixed|random,
          interval_ms, interval_min_ms, interval_max_ms}
    """
    data = request.get_json(silent=True) or {}
    flow_id = str(data.get("flow_id") or "").strip()
    if not flow_id:
        return jsonify({"ok": False, "error": "缺少流量 ID"}), 400
    try:
        count = int(data.get("count") if data.get("count") is not None else 1)
    except Exception:
        count = 0
    if not (1 <= count <= 100):
        return jsonify({"ok": False, "error": "重放次数须为 1-100"}), 400
    mode = str(data.get("interval_mode") or "none").strip().lower()
    if mode not in ("none", "fixed", "random"):
        mode = "none"

    def _clamp_ms(key, default):
        try:
            val = int(data.get(key) if data.get(key) is not None else default)
        except Exception:
            val = default
        return max(0, min(60000, val))

    interval_ms = _clamp_ms("interval_ms", 500)
    interval_min_ms = _clamp_ms("interval_min_ms", 200)
    interval_max_ms = _clamp_ms("interval_max_ms", 1000)
    if mode == "random" and interval_min_ms > interval_max_ms:
        return jsonify({"ok": False, "error": "随机间隔的最小值不能大于最大值"}), 400
    if _capture_engine is None or not _capture_engine.is_running():
        return jsonify({"ok": False, "error": "抓包引擎未运行，请先启动抓包"}), 400
    rd = _capture_replay_fetch_request(flow_id)
    if rd is None:
        return jsonify({"ok": False, "error": "流量记录不存在（可能已被清空）"}), 404
    rb = rd.get("req_body")
    if isinstance(rb, str) and any(mark in rb for mark in ("[已截断", "[二进制内容", "[body 未捕获")):
        return jsonify({"ok": False, "error": "该请求体未被完整捕获，无法重放"}), 400
    with _replay_lock:
        if _replay_state["running"]:
            return jsonify({"ok": False, "error": "已有重放任务进行中，请等待完成或先停止"}), 409
        _replay_state.update({
            "running": True, "total": count, "current": 0,
            "results": [], "cancel": False, "flow_id": flow_id,
            "summary": {"success": 0, "fail": 0, "avg_ms": 0, "stopped": False, "_sum_ms": 0},
        })
        state = _replay_state
    threading.Thread(
        target=_capture_replay_worker,
        args=(state, rd, count, mode, interval_ms, interval_min_ms, interval_max_ms),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "total": count})


@app.route("/api/capture/replay/status")
def api_capture_replay_status():
    """重放任务进度与结果（前端 1s 轮询）"""
    with _replay_lock:
        summary = None
        if _replay_state["summary"]:
            summary = {k: v for k, v in _replay_state["summary"].items() if not k.startswith("_")}
        return jsonify({
            "ok": True,
            "running": _replay_state["running"],
            "total": _replay_state["total"],
            "current": _replay_state["current"],
            "results": list(_replay_state["results"]),
            "summary": summary,
        })


@app.route("/api/capture/replay/cancel", methods=["POST"])
def api_capture_replay_cancel():
    """停止重放任务（剩余次数不再执行）"""
    with _replay_lock:
        running = _replay_state["running"]
        if running:
            _replay_state["cancel"] = True
    return jsonify({"ok": True, "running": running})


@app.route("/api/capture/flows", methods=["DELETE"])
def api_capture_clear():
    """清空流量记录"""
    global _capture_dropped
    with _capture_lock:
        ids = [f.get("id", "") for f in _capture_flows]
        _capture_flows.clear()
        _capture_flow_index.clear()
        _capture_dropped = 0
        # 注意：不重置 _capture_seq（保持单调递增）——否则其他已打开页面的
        # 增量游标 since=旧seq 将永远拉不到清空后的新流量，列表“卡死”
    for fid in ids:
        _body_cache_remove(fid)
    return jsonify({"ok": True})


@app.route("/api/capture/export/har")
def api_capture_export_har():
    """导出流量为 HAR 1.2（可导入 Chrome DevTools / Charles 复盘）；带 ?id= 时仅导出单条"""
    from datetime import datetime as _dt
    fid_filter = (request.args.get("id") or "").strip()
    with _capture_lock:
        flows = list(_capture_flows)
    entries = []
    for f in reversed(flows):  # deque 新在前 → 导出按时间升序
        if fid_filter and f.get("id") != fid_filter:
            continue
        fid = f.get("id", "")
        with _capture_body_lock:
            body = _capture_body_cache.get(fid)
        if not body:
            body = _load_body_disk(fid)
        body = body or {}
        scheme = f.get("scheme") or "http"
        host = f.get("host") or ""
        try:
            port = int(f.get("port") or 0)
        except Exception:
            port = 0
        if port and port != (443 if scheme == "https" else 80):
            url = "%s://%s:%d%s" % (scheme, host, port, f.get("path") or "/")
        else:
            url = "%s://%s%s" % (scheme, host, f.get("path") or "/")
        req_headers = body.get("req_headers") or {}
        resp_headers = body.get("resp_headers") or {}
        req_body = body.get("req_body")
        resp_body = body.get("resp_body")
        # 起始时间：新版 addon 带 ts（epoch 秒）；旧数据只有 HH:MM:SS，用当天日期拼接
        ts = f.get("ts")
        try:
            if ts:
                started = _dt.fromtimestamp(float(ts)).astimezone().isoformat(timespec="milliseconds")
            else:
                started = _dt.now().strftime("%Y-%m-%dT") + (f.get("time") or "00:00:00") + ".000"
        except Exception:
            started = _dt.now().isoformat(timespec="milliseconds")
        path = f.get("path") or "/"
        query = []
        if "?" in path:
            from urllib.parse import parse_qsl
            query = [{"name": k, "value": v}
                     for k, v in parse_qsl(path.split("?", 1)[1], keep_blank_values=True)]
        req_mime = ""
        for k, v in req_headers.items():
            if str(k).lower() == "content-type":
                req_mime = str(v)
                break
        duration = f.get("duration_ms") or 0
        entry = {
            "startedDateTime": started,
            "time": duration,
            "request": {
                "method": f.get("method") or "GET",
                "url": url,
                "httpVersion": "HTTP/1.1",
                "cookies": [],
                "headers": [{"name": str(k), "value": str(v)} for k, v in req_headers.items()],
                "queryString": query,
                "headersSize": -1,
                "bodySize": len(req_body.encode("utf-8")) if isinstance(req_body, str) and req_body else 0,
            },
            "response": {
                "status": f.get("status") if f.get("status") is not None else 0,
                "statusText": "",
                "httpVersion": "HTTP/1.1",
                "cookies": [],
                "headers": [{"name": str(k), "value": str(v)} for k, v in resp_headers.items()],
                "content": {
                    "size": f.get("size") or 0,
                    "mimeType": f.get("content_type") or "",
                    "text": resp_body if isinstance(resp_body, str) else "",
                },
                "redirectURL": "",
                "headersSize": -1,
                "bodySize": f.get("size") or 0,
            },
            "cache": {},
            "timings": {"send": 0, "wait": duration, "receive": 0},
            "_apiTester": {
                "mock": f.get("mock"), "loss": f.get("loss"), "replay": f.get("replay"),
                "source": f.get("source"), "client": f.get("client"),
            },
        }
        if isinstance(req_body, str) and req_body:
            entry["request"]["postData"] = {"mimeType": req_mime or "text/plain", "text": req_body}
        entries.append(entry)
    if fid_filter and not entries:
        return jsonify({"error": "流量不存在或已清除"}), 404
    har = {"log": {"version": "1.2", "creator": {"name": "api-tester", "version": "1.0"}, "entries": entries}}
    if fid_filter:
        fname = "capture_%s.har" % re.sub(r"[^0-9A-Za-z_-]", "_", fid_filter)[:48]
    else:
        fname = "capture_%s.har" % _dt.now().strftime("%Y%m%d_%H%M%S")
    resp = make_response(json.dumps(har, ensure_ascii=False, indent=1))
    resp.mimetype = "application/json"
    resp.headers["Content-Disposition"] = "attachment; filename=%s" % fname
    return resp


def _cleanup_capture_on_exit():
    """主服务退出时自动停止抓包引擎、恢复系统代理、清理临时 body 文件"""
    try:
        if _capture_engine is not None and _capture_engine.is_running():
            _capture_engine.stop()
            print("[Capture] 进程退出，已自动停止抓包引擎")
    except Exception:
        pass
    try:
        if _capture_proxy_is_ours():
            _set_capture_system_proxy(False)
            print("[Capture] 进程退出，已恢复系统代理")
    except Exception:
        pass
    # 清理磁盘 body 临时文件
    try:
        import shutil
        if os.path.isdir(_capture_body_dir):
            shutil.rmtree(_capture_body_dir, ignore_errors=True)
    except Exception:
        pass


atexit.register(_cleanup_capture_on_exit)


if __name__ == "__main__":
    import webbrowser
    print(f"\n  工具箱 Web 界面")
    print(f"  打开浏览器访问: http://{LOCAL_IP}:{PORT}\n")
    
    # 启动前清理端口占用
    _kill_port_occupants(PORT)
    
    # CA 证书改为懒加载：仅在 Mock 拦截 HTTPS 时按需生成，不再启动时无条件创建
    # ensure_ca_cert() 已移入 generate_domain_cert() 内部按需调用
    # 但权限收紧要每次启动都做：私钥是明文 PEM，默认跟着程序目录继承权限，同机其他账户能直读
    try:
        _harden_certs_dir()
    except Exception as _e:
        print(f"[CA] 启动时收紧 certs 权限失败（不影响使用）：{_e}")
    
    # 预检：清理上次异常退出遗留的抓包系统代理（引擎此刻未运行，代理指向死端口会断网）
    try:
        if _capture_proxy_is_ours():
            # 抓包端口仍有监听（引擎孤儿存活、流量照常上报）→ 保留代理并恢复 Web 记录开关，不打断用户抓包；否则恢复代理防断网
            if _port_has_listener(_CAPTURE_PORT):
                print("[Capture] 系统代理指向本工具且抓包端口仍在监听（引擎存活），保留代理不恢复")
                _CAPTURE_SCOPE["web"] = True
                _save_capture_scope()
            else:
                _set_capture_system_proxy(False)
                print("[Capture] 检测到遗留的抓包系统代理，已自动恢复")
    except Exception:
        pass
    
    # 延迟打开浏览器（等待服务启动）
    import threading
    def open_browser():
        import time
        time.sleep(1.5)
        url = f"http://{LOCAL_IP}:{PORT}"
        # Windows 下 os.startfile 比 webbrowser.open 更可靠
        if sys.platform == 'win32':
            try:
                os.startfile(url)
                return
            except Exception:
                pass
        try:
            webbrowser.open(url)
        except Exception:
            print(f"  [提示] 浏览器未自动打开，请手动访问: {url}")
    threading.Thread(target=open_browser, daemon=True).start()
    
    # 启动 HTTPS 代理服务器（监听 CONNECT 请求）
    def start_https_proxy():
        proxy_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        proxy_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        proxy_socket.bind(('0.0.0.0', 5051))  # HTTPS 代理端口，监听所有网卡
        proxy_socket.listen(100)
        
        while True:
            client_socket, addr = proxy_socket.accept()
            # 读取 CONNECT 请求
            request_data = client_socket.recv(4096).decode('utf-8', errors='ignore')
            
            if request_data.startswith("CONNECT"):
                # 解析目标主机和端口
                lines = request_data.split('\r\n')
                connect_line = lines[0]  # CONNECT host:port HTTP/1.1
                parts = connect_line.split(" ")
                if len(parts) >= 2:
                    host_port = parts[1]
                    host_parts = host_port.split(":")
                    target_host = host_parts[0]
                    target_port = int(host_parts[1]) if len(host_parts) > 1 else 443
                    
                    # 在新线程中处理隧道
                    threading.Thread(
                        target=handle_connect_tunnel,
                        args=(client_socket, target_host, target_port, addr)
                    ).start()
            else:
                client_socket.close()
    
    # 在后台启动 HTTPS 代理
    threading.Thread(target=start_https_proxy, daemon=True).start()
    
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)  # 监听所有网卡；多线程：断点挂起/页面轮询需并发
