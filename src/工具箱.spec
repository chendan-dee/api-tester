# -*- mode: python ; coding: utf-8 -*-
"""工具箱（api-tester）PyInstaller 打包配置 —— onedir 模式

为什么用 onedir 而不是单文件 onefile：
  本工具包含 mitmproxy（抓包引擎）、PyQt5 + OpenCV（录制面板）等重量级组件。
  单文件 exe 每次启动（含抓包、录制面板、录制器等子进程——它们复用 exe 本体）
  都要把全部内容解压到临时目录，启动慢、子进程开销成倍；onedir 由
  「工具箱.exe + _internal 目录」组成，所有子进程共享同一份依赖，启动快、稳定。
  分发时把整个「工具箱」文件夹拷贝给对方，双击 exe 即可用，无需安装 Python。

打包命令（在技能根目录 skills/api-tester 执行）：
  ..\\..\\..\\.venv\\Scripts\\python.exe -m PyInstaller src\\工具箱.spec --noconfirm --clean

运行期目录约定（exe 旁自动生成/持久化）：
  cases/  data/  certs/  logs/  transfer/  templates/  static/
  （templates/static 缺失时自动回退到包内内置资源，放外部可随时改 HTML 免打包）
"""

import os
from PyInstaller.utils.hooks import collect_all

_ROOT = os.path.dirname(SPECPATH)  # SPECPATH = src/，其上一级即技能根目录

datas = [
    (os.path.join(_ROOT, "templates"), "templates"),
    (os.path.join(_ROOT, "static"), "static"),
    # cases/ 只打入示例文件（首次运行复制到 exe 旁 cases/），不打包用户真实用例
    (os.path.join(_ROOT, "cases", "example.json"), "cases"),
    # 抓包引擎的 mitmproxy addon 脚本：运行时以 -s 加载，必须以实体文件存在
    (os.path.join(_ROOT, "src", "capture", "addon.py"), "capture"),
    # 前置/后置脚本的 Node 运行时：每次请求都 spawn 一次，必须是实体文件
    (os.path.join(_ROOT, "src", "script_runtime.js"), "src"),
]

binaries = []
hiddenimports = [
    # 项目内部模块（exe 入口按首参动态分派导入，静态分析可能漏掉）
    "engine", "printer", "data_generator", "yaml_checker", "curl2yaml", "scheduler",
    "scripts",
    "capture", "capture.engine",
    "recorder_tool", "recorder_tool.control_panel", "recorder_tool.recorder",
    # 数据库工作台（延迟导入，静态分析扫不到）
    "db", "db.store", "db.connectors", "pymysql",
    # 录制/抓包等第三方依赖（触发对应 hook 完整收集）
    "tkinter", "mss", "cv2", "numpy", "PyQt5", "uiautomation", "comtypes",
    "qrcode.image.pil",
    "mitmproxy.tools.dump",
]

# mitmproxy 全量收集：包含内部 addons、web 静态资源与 Rust 原生扩展
# （mitmproxy_rs / mitmproxy_windows 带平台二进制，必须随包携带）
for _pkg in ("mitmproxy", "mitmproxy_rs", "mitmproxy_windows"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

a = Analysis(
    [os.path.join(_ROOT, "app.py")],
    pathex=[os.path.join(_ROOT, "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="工具箱",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[os.path.join(_ROOT, "icon.ico")],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="工具箱",
)
