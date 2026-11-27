# -*- coding: utf-8 -*-
"""屏幕录制控制面板 - 常驻浮动工具栏（优化版）

优化项：
- A1 面板常驻：— 按钮 hide 而非退出，Web 通过 panel_wake_ts 唤醒；× 才真正退出
- A2 记忆位置：拖动结束写入 panel_pos，下次启动恢复
- A3 FPS 热更新：pending_fps 由 Web 写入，非录制态即时生效
- B1 首帧不丢：writer 未就绪前用 deque 缓存前 N 帧，就绪后 flush
- B2 抓屏后端：优先 dxcam(DXGI Desktop Duplication, GPU) / d3dshot，均无则回退 GDI
- B3 边框置顶频率降为 2s；闪烁保持 500ms
- C1 桌面 ClickOverlay：30Hz 立即绘制点击脉冲（不等下一视频帧）
- C2 perf_counter 调度，落后过多时重置基线避免忙等
- C3 点击过期从左侧 popleft，遍历不拷贝
- C4 窗口 rect 每 5 帧才查询一次
- C6 窗口边界改用 DWM EXTENDED_FRAME_BOUNDS（排除隐形阴影），避免多录周边内容
- C7 “网页区域”模式：UIA 检测浏览器 Document 边界，只录网页内容（排除标签栏/地址栏外壳）
- C8 抓屏后端不含鼠标指针：逐帧补画系统光标，点击时叠加水波纹
- C9 启动准备：抓屏/编码就绪后才从 00:00 计时，避免视频开头缺时间
- C5 暂停/继续按钮，暂停时段不计入时长也不写帧
- D1 stop 后 POST /api/record/notify-finished（配合 Web SSE 推送）
- D2 优雅关闭：先 recorder.stop() 等 writer.release()，再退出，不用 os._exit
- D3 stopped_event 通知最后一帧已 flush
- F5 pending_save_dir 热更新
"""

import sys
import os
import json
import time
import math
import argparse
import shutil
import threading
import subprocess
import urllib.request
from datetime import datetime
from collections import deque

# mss / cv2 / numpy 属重库（冷启动合计约 2.5s），若在主线程导入会让工具栏窗口迟迟不出现。
# 这里改为后台线程并行加载（与 PyQt5 导入重叠），窗口先弹出；
# 录制等实际使用点通过 _ensure_heavy_deps() 同步等待加载完成。
mss = cv2 = np = None
_heavy_ready = threading.Event()

def _load_heavy_deps():
    global mss, cv2, np
    try:
        import mss as _mss
        import cv2 as _cv2
        import numpy as _np
        mss, cv2, np = _mss, _cv2, _np
    except Exception as e:
        print(f"[启动] 依赖加载失败: {e}（请使用含 mss/opencv-python/PyQt5 的 Python，如项目 .venv）")
    finally:
        _heavy_ready.set()

def _ensure_heavy_deps():
    """使用 mss/cv2/numpy 前调用：等待后台加载完成；依赖缺失时明确报错"""
    if not _heavy_ready.is_set():
        _heavy_ready.wait()
    if mss is None or cv2 is None or np is None:
        raise RuntimeError("缺少录制依赖 mss/opencv-python/numpy，请用项目 .venv 的 python 启动")

threading.Thread(target=_load_heavy_deps, daemon=True, name='heavy-deps').start()

from PyQt5.QtWidgets import (QApplication, QWidget, QLabel, QPushButton, QToolButton,
                             QVBoxLayout, QHBoxLayout, QLineEdit, QFrame, QAbstractButton, QMenu)
from PyQt5 import sip
from PyQt5.QtCore import Qt, QTimer, QPoint, QPointF, QEventLoop, QRectF, QSize
from PyQt5.QtGui import (QFont, QCursor, QColor, QPainter, QPen, QImage, QPolygonF,
                         QPixmap, QFontMetrics, QIcon, QPalette)

if getattr(sys, 'frozen', False):
    # 打包版：状态文件与主服务（exe 本体）共享，固定放在 exe 旁的 data/
    _APP_BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    _APP_BASE = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', '..'))
RECORD_STATUS_FILE = os.path.join(_APP_BASE, 'data', '_record_status.json')

_STATUS_LOCK = threading.Lock()


def _read_status():
    try:
        if os.path.exists(RECORD_STATUS_FILE):
            with open(RECORD_STATUS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _write_status(**updates):
    """原子合并写入状态文件"""
    with _STATUS_LOCK:
        try:
            status = _read_status()
            status.update(updates)
            os.makedirs(os.path.dirname(RECORD_STATUS_FILE), exist_ok=True)
            tmp = RECORD_STATUS_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(status, f, ensure_ascii=False)
            os.replace(tmp, RECORD_STATUS_FILE)
        except Exception as e:
            print(f"[状态] 写入失败: {e}")


def _script_mtime():
    """本文件修改时间：Web 端唤醒常驻面板前比对，发现代码已更新就重启面板"""
    try:
        return os.path.getmtime(os.path.abspath(__file__))
    except Exception:
        return 0


def _install_crash_guard():
    """崩溃兑底：先落盘 recording=False，再退出

    PyQt5 遇到槽函数里的未捕获异常会调 qFatal 直接 abort 进程，来不及写状态文件，
    Web 就会永远停在“录制中”，而这种情况下 /api/record/stop 也找不到进程，
    用户没有任何停止入口（曾因鼠标释放时 KeyError 复现）。
    这里保证任何未捕获异常都先把状态复位，页面 2 秒内自动回到“就绪”。
    """
    try:
        import faulthandler
        faulthandler.enable()      # 原生层崩溃（qFatal / 段错误）也能打印调用栈
    except Exception:
        pass

    def _hook(exc_type, exc, tb):
        try:
            _write_status(recording=False, paused=False, panel_visible=False,
                          panel_pid=0, last_finished_at=time.time())
        except Exception:
            pass
        import traceback
        traceback.print_exception(exc_type, exc, tb)
        try:
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(1)                # 不留给 qFatal，进程立即消失让 Web 能察觉

    sys.excepthook = _hook


# ═══════════════════════════════════════════════════
#  窗口枚举 / 选择
# ═══════════════════════════════════════════════════

def _get_window_frame_rect(hwnd):
    """获取窗口的真实视觉边界（不含 DWM 隐形阴影/扩展边框）。

    GetWindowRect 会把 Win10/11 窗口四周不可见的阴影扩展边框算进去
    （左右底各约 7-8px，最大化窗口还会超出屏幕），导致录制区域比
    可见窗口大、把后方其他窗口内容录进来。这里优先用 DWM 的
    DWMWA_EXTENDED_FRAME_BOUNDS 取真实边界，失败时回退 GetWindowRect。
    返回 (x, y, w, h)；无法获取时返回 None。
    """
    # 1) DWM 扩展边框（Vista+，排除阴影，最大化/贴边窗口同样准确）
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        fn = ctypes.windll.dwmapi.DwmGetWindowAttribute
        fn.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        fn.restype = ctypes.c_long
        hr = fn(wintypes.HWND(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect))
        if hr == 0:
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w > 0 and h > 0:
                return (rect.left, rect.top, w, h)
    except Exception:
        pass
    # 2) 回退：GetWindowRect（可能含阴影，但保证可用）
    try:
        import win32gui
        r = win32gui.GetWindowRect(hwnd)
        w, h = r[2] - r[0], r[3] - r[1]
        if w > 0 and h > 0:
            return (r[0], r[1], w, h)
    except Exception:
        pass
    return None


def _query_web_doc_rect(hwnd):
    """UIA 查询浏览器窗口内网页 Document 的屏幕矩形；非网页窗口返回 None。"""
    try:
        import uiautomation as auto
    except Exception:
        return None
    try:
        win = auto.ControlFromHandle(hwnd)
        if win is None:
            return None
        doc = win.DocumentControl(searchDepth=20)
        if doc and doc.Exists(0, 0):
            r = doc.BoundingRectangle
            if r and r.width() > 50 and r.height() > 50:
                return (r.left, r.top, r.width(), r.height())
    except Exception:
        pass
    return None


_web_inset_cache = {}  # hwnd -> {'inset': (dl,dt,dr,db)|None, 'frame': tuple, 'ts': float}


def _get_web_inset(hwnd, frame_rect):
    """网页内容相对窗口 frame 的四边内缩 (dl, dt, dr, db)；非网页窗口返回 None。

    带缓存：窗口 rect 未变时直接复用历史结果；窗口变化后最快 1.5s 才重新
    查询 UIA（变化瞬间沿用旧 inset 合成），避免拖动窗口时高频 UIA 查询拖慢抓帧。
    """
    if not frame_rect:
        return None
    now = time.time()
    ent = _web_inset_cache.get(hwnd)
    if ent:
        if ent['frame'] == tuple(frame_rect) and now - ent['ts'] < 15.0:
            return ent['inset']
        if now - ent['ts'] < 1.5:
            return ent['inset']
    inset = None
    try:
        r = _query_web_doc_rect(hwnd)
        if r:
            dl = r[0] - frame_rect[0]
            dt = r[1] - frame_rect[1]
            dr = frame_rect[0] + frame_rect[2] - (r[0] + r[2])
            db = frame_rect[1] + frame_rect[3] - (r[1] + r[3])
            # 合理性校验：内缩非负且单边不超过半窗，异常值视为检测失败
            if (0 <= dl <= frame_rect[2] // 2 and 0 <= dt <= frame_rect[3] // 2 and
                    0 <= dr <= frame_rect[2] // 2 and 0 <= db <= frame_rect[3] // 2):
                inset = (dl, dt, dr, db)
    except Exception:
        inset = ent['inset'] if ent else None
    _web_inset_cache[hwnd] = {'inset': inset, 'frame': tuple(frame_rect), 'ts': now}
    return inset


def _get_web_content_rect(hwnd, frame_rect=None):
    """窗口内网页内容区域（自动排除浏览器标签栏/地址栏等外壳）；无网页返回 None。"""
    fr = frame_rect or _get_window_frame_rect(hwnd)
    if not fr:
        return None
    ins = _get_web_inset(hwnd, fr)
    if not ins:
        return None
    x = fr[0] + ins[0]
    y = fr[1] + ins[1]
    w = fr[2] - ins[0] - ins[2]
    h = fr[3] - ins[1] - ins[3]
    if w <= 50 or h <= 50:
        return None
    return (x, y, w, h)


def _enum_windows():
    if sys.platform != 'win32':
        return []
    import win32gui
    results = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            return True
        # 排除本进程窗口（控制台/面板/遮罩），避免选到标题为 python 的宿主控制台
        try:
            import win32process
            _, pid = win32gui.GetWindowThreadProcessId(hwnd)
            if pid == os.getpid():
                return True
        except Exception:
            pass
        title = win32gui.GetWindowText(hwnd)
        if not title or not title.strip():
            return True
        if '屏幕录制' in title or 'Recorder' in title or 'ControlPanel' in title:
            return True
        fr = _get_window_frame_rect(hwnd)
        if fr and fr[2] > 100 and fr[3] > 100:
            results.append((hwnd, title.strip(), fr[0], fr[1], fr[2], fr[3]))
        return True

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass
    return results


def _exclude_from_capture(widget):
    """Win10 2004+：让窗口在屏幕上可见但不出现在屏幕录制/截屏中"""
    try:
        import ctypes
        hwnd = int(widget.winId())
        WDA_EXCLUDEFROMCAPTURE = 0x00000011
        ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    except Exception:
        pass


def _selectable_class(hwnd):
    """排除桌面/任务栏等 shell 窗口，避免误选整个桌面"""
    try:
        import win32gui
        cls = win32gui.GetClassName(hwnd)
        if cls in ('Progman', 'WorkerW', 'Shell_TrayWnd',
                   'Shell_SecondaryTrayWnd', 'ForegroundStaging'):
            return False
    except Exception:
        pass
    return True


def _build_pick_bar(on_ok, on_cancel):
    """构建"顶部一体式操作栏"：信息文本 + 确认 + 取消聚合为一条深色圆角栏。

    原先信息条与确认/取消按钮是三个分散的置顶小窗，确认/取消固定在屏幕
    右上角 (y=16~60)——该区域常被输入法状态条、会议"正在讲话"条、聊天
    悬浮窗等同类置顶元素插队遮挡。合并为一条居中悬浮于屏幕顶部后，
    位置落在占用最少的屏幕中上部，且只需对一个窗口做置顶保活。
    返回 (bar, hint_label, ok_btn, no_btn)。
    """
    # 用 QLabel 作根容器且不设 WA_TranslucentBackground：实测该属性会导致样式表
    # 背景完全不绘制（黑条只剩文字），不设时深色圆角背景渲染正常（同旧版提示条配方）
    bar = QLabel(None)
    bar.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
    bar.setAttribute(Qt.WA_ShowWithoutActivating, True)
    bar.setObjectName('pickBar')
    bar.setStyleSheet(
        "QLabel#pickBar { background:rgba(17,24,39,238);"
        "border:1px solid rgba(255,255,255,42); border-radius:14px; }"
        "QLabel#pickInfo { color:#fff; font-size:14px; font-weight:bold;"
        "background:transparent; border:none; }")
    lay = QHBoxLayout(bar)
    lay.setContentsMargins(18, 10, 10, 10)
    lay.setSpacing(10)
    hint = QLabel('识别中...')
    hint.setObjectName('pickInfo')
    lay.addWidget(hint)
    ok = QPushButton('✓ 确认录制')
    ok.setCursor(Qt.PointingHandCursor)
    ok.setFixedSize(120, 44)
    ok.setStyleSheet("QPushButton { background:#16a34a;color:#fff;font-size:15px;"
                     "font-weight:bold;border-radius:8px; }"
                     "QPushButton:hover { background:#15803d; }")
    ok.clicked.connect(on_ok)
    lay.addWidget(ok)
    no = QPushButton('✕ 取消')
    no.setCursor(Qt.PointingHandCursor)
    no.setFixedSize(80, 44)
    no.setStyleSheet("QPushButton { background:#374151;color:#fff;font-size:15px;"
                     "font-weight:bold;border-radius:8px; }"
                     "QPushButton:hover { background:#4b5563; }")
    no.clicked.connect(on_cancel)
    lay.addWidget(no)
    return bar, hint, ok, no


class _HighlightBorder(QWidget):
    """被动高亮边框：WA_TransparentForMouseEvents 鼠标穿透，纯视觉不拦截任何输入"""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._rect = None
        self._title = ''
        self.setGeometry(0, 0, 1, 1)

    def set_target(self, rect, title):
        # 仅在目标变化时更新几何/重绘，避免 30Hz 无条件 setGeometry 造成闪烁
        if rect == self._rect and title == self._title:
            return
        self._rect = rect
        self._title = title
        if rect:
            x, y, w, h = rect
            self.setGeometry(x - 5, y - 5, w + 10, h + 10)
        else:
            self.setGeometry(0, 0, 1, 1)
        self.update()

    def paintEvent(self, event):
        if not self._rect:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # 红色高亮边框（对齐参考工具的窗口选择样式）
        p.setPen(QPen(QColor(239, 68, 68, 235), 4))
        p.setBrush(QColor(239, 68, 68, 22))
        p.drawRect(2, 2, self.width() - 4, self.height() - 4)
        x, y, w, h = self._rect
        label = f"{self._title}   {w} × {h}"
        fm = p.fontMetrics()
        lw = (fm.horizontalAdvance(label) if hasattr(fm, 'horizontalAdvance') else fm.width(label)) + 12
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 210))
        p.drawRect(2, 2, min(self.width() - 4, lw), 24)
        p.setPen(QColor(255, 255, 255))
        p.drawText(8, 19, label)


def select_target_window():
    """FSCapture 风格：悬停实时高亮 + 左键选中（被动高亮，鼠标穿透不拦截）。

    - 高亮边框鼠标穿透，真实窗口照常响应，不存在遮罩拦截导致点击/取点失效
    - 30Hz QTimer 用 QCursor.pos() + _enum_windows() 包含检测找光标下顶层窗口
    - 左键用 GetAsyncKeyState 边沿检测确认；Esc / 右键取消；30s 超时取消
    """
    app = QApplication.instance() or QApplication(sys.argv)
    import win32api
    import win32con
    result = {'value': None}
    done = {'flag': False}
    hb = _HighlightBorder()
    hb.show()
    st = {
        'prev_l': bool(win32api.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000),
        'hwnd': None, 'title': '', 'rect': None,
    }
    timer = QTimer()
    loop = QEventLoop()

    def finish(value):
        if done['flag']:
            return
        done['flag'] = True
        timer.stop()
        hb.hide()
        hb.deleteLater()
        result['value'] = value
        if loop.isRunning():
            loop.quit()

    def tick():
        if (win32api.GetAsyncKeyState(win32con.VK_ESCAPE) & 0x8000 or
                win32api.GetAsyncKeyState(win32con.VK_RBUTTON) & 0x8000):
            finish(None)
            return
        pos = QCursor.pos()
        px, py = pos.x(), pos.y()
        pick = None
        for (hwnd, title, x, y, w, h) in _enum_windows():
            if not _selectable_class(hwnd):
                continue
            if x <= px < x + w and y <= py < y + h:
                pick = (hwnd, title, x, y, w, h)
                break
        if pick:
            st['none'] = 0
            st['hwnd'], st['title'] = pick[0], pick[1]
            st['rect'] = (pick[2], pick[3], pick[4], pick[5])
            hb.set_target(st['rect'], st['title'])
        else:
            # 滞回：连续多帧无目标才清除，避免在全屏/无之间来回跳变闪烁
            st['none'] = st.get('none', 0) + 1
            if st['none'] >= 5:
                st['hwnd'], st['title'], st['rect'] = None, '', None
                hb.set_target(None, '')
        l = bool(win32api.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000)
        if l and not st['prev_l']:
            if st['hwnd'] and st['rect']:
                x, y, w, h = st['rect']
                finish((st['hwnd'], st['title'], x, y, w, h))
        st['prev_l'] = l

    timer.timeout.connect(tick)
    timer.start(33)
    to = QTimer()
    to.setSingleShot(True)
    to.timeout.connect(lambda: finish(None))
    to.start(30000)
    loop.exec_()
    to.stop()
    return result['value']


def _legacy_select_target_window():
    """FSCapture 风格窗口选择：鼠标悬停实时高亮窗口边框，左键确认。

    - 全屏置顶半透明控件捕获鼠标（确认点击被吞掉，不会误触目标窗口）
    - 30Hz 轮询 QCursor.pos()，用 _enum_windows()（z-order 顶层优先，
      自动排除本控件[无标题]与控制面板[标题过滤]）找出光标下的顶层窗口
    - 实时绘制蓝色高亮边框 + 标题/尺寸标签
    - 左键确认，右键 / Esc 取消
    """
    app = QApplication.instance() or QApplication(sys.argv)
    result = {'value': None}
    HINT_OK = ("color:#fff;font-size:15px;font-weight:bold;"
               "background:rgba(0,0,0,190);border:2px solid #3b82f6;"
               "border-radius:10px;padding:12px 24px;")
    HINT_WARN = ("color:#fff;font-size:15px;font-weight:bold;"
                 "background:rgba(127,29,29,220);border:2px solid #ef4444;"
                 "border-radius:10px;padding:12px 24px;")

    class WindowHighlight(QWidget):
        def __init__(self):
            super().__init__()
            scr = QApplication.primaryScreen()
            geo = scr.virtualGeometry() if hasattr(scr, 'virtualGeometry') else scr.geometry()
            self.setGeometry(geo)
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setCursor(Qt.CrossCursor)
            self._hwnd = None
            self._title = ''
            self._rect = None  # (x, y, w, h)
            self._hint = QLabel("移动鼠标到应用窗口上\n左键确认 · 右键 / Esc 取消", self)
            self._hint.setAlignment(Qt.AlignCenter)
            self._hint.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self._hint.setStyleSheet(HINT_OK)
            self._center_hint()
            self._poll_timer = QTimer(self)
            self._poll_timer.timeout.connect(self._poll)
            self._poll_timer.start(33)
            self._esc_timer = QTimer(self)
            self._esc_timer.timeout.connect(self._check_esc)
            self._esc_timer.start(80)
            self._timeout = QTimer(self)
            self._timeout.setSingleShot(True)
            self._timeout.timeout.connect(self._cancel)
            self._timeout.start(30000)

        def _center_hint(self):
            self._hint.adjustSize()
            self._hint.move((self.width() - self._hint.width()) // 2,
                            (self.height() - self._hint.height()) // 2)

        def _set_hint(self, text, warn=False):
            self._hint.setText(text)
            self._hint.setStyleSheet(HINT_WARN if warn else HINT_OK)
            self._center_hint()

        def _stop_timers(self):
            self._poll_timer.stop()
            self._esc_timer.stop()
            self._timeout.stop()

        def _check_esc(self):
            try:
                import win32api
                import win32con
                if win32api.GetAsyncKeyState(win32con.VK_ESCAPE) & 0x8000:
                    self._cancel()
            except Exception:
                pass

        def _cancel(self):
            result['value'] = None
            self._stop_timers()
            self.close()

        @staticmethod
        def _selectable(hwnd):
            # 排除桌面/任务栏等 shell 窗口，避免误选整个桌面
            try:
                import win32gui
                cls = win32gui.GetClassName(hwnd)
                if cls in ('Progman', 'WorkerW', 'Shell_TrayWnd',
                           'Shell_SecondaryTrayWnd', 'ForegroundStaging'):
                    return False
            except Exception:
                pass
            return True

        def _poll(self):
            pos = QCursor.pos()
            px, py = pos.x(), pos.y()
            pick = None
            for (hwnd, title, x, y, w, h) in _enum_windows():
                if not self._selectable(hwnd):
                    continue
                if x <= px < x + w and y <= py < y + h:
                    pick = (hwnd, title, x, y, w, h)
                    break
            if pick:
                hwnd, title, x, y, w, h = pick
                changed = (hwnd != self._hwnd or (x, y, w, h) != self._rect)
                self._hwnd, self._title = hwnd, title
                self._rect = (x, y, w, h)
                if changed:
                    self.update()
                # 实时告知“将选中哪个窗口”，避免确认后不知道选了什么
                self._set_hint(f"将录制：{title}\n{w} × {h}　左键确认 · 右键/Esc 取消")
            else:
                if self._rect is not None:
                    self._hwnd, self._title, self._rect = None, '', None
                    self.update()
                self._set_hint("移动鼠标到应用窗口上\n左键确认 · 右键 / Esc 取消")

        def paintEvent(self, event):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            if not self._rect:
                return
            x, y, w, h = self._rect
            # 高亮边框 + 轻微填充
            p.setPen(QPen(QColor(59, 130, 246, 235), 3))
            p.setBrush(QColor(59, 130, 246, 28))
            p.drawRect(x, y, w, h)
            # 标题 + 尺寸标签
            label = f"{self._title}   {w} × {h}"
            fm = p.fontMetrics()
            lw = (fm.horizontalAdvance(label) if hasattr(fm, 'horizontalAdvance') else fm.width(label)) + 12
            ly = y - 26 if y - 26 > 0 else y + 4
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 200))
            p.drawRect(x, ly, min(w, lw), 22)
            p.setPen(QColor(255, 255, 255))
            p.drawText(x + 6, ly + 16, label)

        def mousePressEvent(self, event):
            if event.button() == Qt.LeftButton:
                if self._hwnd and self._rect:
                    x, y, w, h = self._rect
                    result['value'] = (self._hwnd, self._title, x, y, w, h)
                    self._stop_timers()
                    self.close()
                else:
                    # 点在无窗口区域：警告但保持打开，不直接取消
                    self._set_hint("⚠ 此处没有可选窗口\n请移到应用窗口上再左键", warn=True)
            else:
                self._cancel()

    sel = WindowHighlight()
    sel.show()
    sel.raise_()
    sel.activateWindow()
    loop = QEventLoop()
    sel.destroyed.connect(loop.quit)
    loop.exec_()
    return result['value']


# ═══════════════════════════════════════════════════
#  B2: 抓屏后端（优先 DXGI；默认 GDI 直抓）
# ═══════════════════════════════════════════════════

class _GdiGrabber:
    """GDI 桌面抓取（BitBlt 仅 SRCCOPY，不带 CAPTUREBLT）。

    必须避开 CAPTUREBLT：该标志会在部分 Windows 上干扰系统光标合成，
    导致录制过程中鼠标光标持续闪烁（mss 10.x 的 BitBlt 固定携带它）。
    本项目自绘窗口均以 WDA_EXCLUDEFROMCAPTURE 排除在录制外，
    去掉 CAPTUREBLT 后录制内容不变。"""

    def __init__(self):
        import win32gui
        import win32ui
        self._win32gui = win32gui
        self._win32ui = win32ui
        self._hwin = None
        self._hwindc = None
        self._srcdc = None
        self._memdc = None
        self._bmp = None
        self._size = None
        self._hotspot_cache = {}      # hCursor -> (hotspot_x, hotspot_y)

    def _ensure(self, w, h):
        if self._size == (w, h) and self._memdc is not None:
            return
        self._release()
        self._hwin = self._win32gui.GetDesktopWindow()
        self._hwindc = self._win32gui.GetWindowDC(self._hwin)
        self._srcdc = self._win32ui.CreateDCFromHandle(self._hwindc)
        self._memdc = self._srcdc.CreateCompatibleDC()
        self._bmp = self._win32ui.CreateBitmap()
        self._bmp.CreateCompatibleBitmap(self._srcdc, w, h)
        self._memdc.SelectObject(self._bmp)
        self._size = (w, h)

    def grab(self, rect):
        """rect=(x, y, w, h) 屏幕坐标；返回 BGR ndarray"""
        import ctypes
        import win32con
        x, y, w, h = (int(v) for v in rect)
        if w <= 0 or h <= 0:
            return None
        self._ensure(w, h)
        # SRCCOPY-only：绝不加 CAPTUREBLT（光标闪烁源）
        self._memdc.BitBlt((0, 0), (w, h), self._srcdc, (x, y), win32con.SRCCOPY)
        self._draw_cursor(x, y, w, h)
        ctypes.windll.gdi32.GdiFlush()
        raw = self._bmp.GetBitmapBits(True)          # 内存字节序 BGRA
        return np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4)[:, :, :3].copy()

    def _draw_cursor(self, ox, oy, w, h):
        """在采集帧上手动叠加系统光标（BitBlt 不含光标，且不能用 CAPTUREBLT）。

        原理：GetCursorInfo 取当前光标形状与屏幕坐标，DrawIconEx 画进内存位图；
        纯内存合成，不触碰窗口合成层，不会重新引入光标闪烁。
        DrawIconEx 的 x/y 是左上角锚点（实测验证），需减去光标自身 hotspot。"""
        try:
            import win32con
            flags, hcur, (cx, cy) = self._win32gui.GetCursorInfo()
            if not (flags & win32con.CURSOR_SHOWING) or not hcur:
                return
            spot = self._hotspot_cache.get(hcur)
            if spot is None:
                info = self._win32gui.GetIconInfo(hcur)
                spot = (int(info[1]), int(info[2]))
                self._hotspot_cache[hcur] = spot
                for hbm in (info[3], info[4]):
                    if hbm:
                        try:
                            self._win32gui.DeleteObject(hbm)
                        except Exception:
                            pass
                if len(self._hotspot_cache) > 64:
                    self._hotspot_cache.clear()
            dx = int(cx - ox - spot[0])
            dy = int(cy - oy - spot[1])
            if dx < -64 or dy < -64 or dx > w + 64 or dy > h + 64:
                return
            self._win32gui.DrawIconEx(self._memdc.GetSafeHdc(), dx, dy, hcur, 0, 0, 0, None, win32con.DI_NORMAL)
        except Exception:
            pass

    def _release(self):
        try:
            if self._memdc:
                self._memdc.DeleteDC()
        except Exception:
            pass
        try:
            if self._bmp:
                self._win32gui.DeleteObject(self._bmp.GetHandle())
        except Exception:
            pass
        try:
            if self._hwindc:
                self._win32gui.ReleaseDC(self._hwin, self._hwindc)
        except Exception:
            pass
        self._hwin = self._hwindc = self._srcdc = self._memdc = self._bmp = None
        self._size = None

    def close(self):
        self._release()


class CaptureBackend:
    """封装 dxcam / d3dshot / GDI 三种后端，对外统一接口

    GDI 的 BitBlt 在 1920x1080 下只能抓 ~20fps，直接决定了录制上限；
    dxcam 走 DXGI 桌面复制（GPU 侧取帧），实测 1080p 可达 50+ fps，仅在
    录制区域完全落在主显示器内时使用，副屏/越屏录制自动回退 GDI。
    """

    def __init__(self):
        self._kind = None
        self._impl = None
        self._gdi = None          # dxcam 模式下的兜底抓屏（区域越出主屏 / DXGI 异常时用）
        self._frame_origin = (0, 0)   # dxcam 帧覆盖的屏幕原点
        self._last_frame = None   # dxcam 最近一帧（静态画面下 grab 返回 None 时复用）
        self._dx_err = 0          # dxcam 连续异常次数（超过阈值永久降级 GDI）
        self._dx_cam = None       # 保留 DXCamera 引用（降级后 close 仍需释放它）

    def open(self):
        _ensure_heavy_deps()
        # 1) dxcam：DXGI 桌面复制，GPU 抓帧，1080p 实测 50+fps
        try:
            import dxcam
            cam = dxcam.create(output_color='BGR')
            if cam is not None:
                self._dx_cam = cam
                self._impl = cam
                self._kind = 'dxcam'
                # 尽力预热首帧（≤0.5s）：create 成功即代表 DXGI 可用，
                # 静态画面下 grab 会返回 None，不代表不可用
                deadline = time.time() + 0.5
                while time.time() < deadline:
                    f = cam.grab()
                    if f is not None:
                        self._last_frame = f
                        break
                    time.sleep(0.01)
                size = self._last_frame.shape[1::-1] if self._last_frame is not None else (0, 0)
                print(f"[录制] 抓屏后端: dxcam (DXGI {size[0]}x{size[1]})")
                return
        except Exception as e:
            print(f"[录制] dxcam 不可用，尝试其他后端: {e}")
        # 2) d3dshot（旧方案，Python 3.14 下依赖 pillow 装不上，保留兼容）
        try:
            import d3dshot
            self._impl = d3dshot.create(capture_output="numpy", frame_buffer_size=8)
            self._kind = 'd3dshot'
            print("[录制] 抓屏后端: d3dshot (DXGI)")
            return
        except Exception:
            pass
        # 3) 默认兜底：GDI（BitBlt SRCCOPY，无 CAPTUREBLT 光标闪烁问题）
        self._impl = _GdiGrabber()
        self._kind = 'gdi'
        print("[录制] 抓屏后端: GDI（SRCCOPY，无 CAPTUREBLT）")

    def _crop(self, frame, x, y, w, h):
        """从整屏帧裁出录制区域；必须 copy——dxcam 帧是内部缓冲，
        直接切片会与后续抓帧共享内存，导致点击标记/标注被回写污染"""
        H, W = frame.shape[:2]
        if x < 0 or y < 0 or x + w > W or y + h > H:
            return None
        return np.array(frame[y:y + h, x:x + w], copy=True)

    def grab(self, rect):
        """rect = (left, top, width, height)，返回 BGR ndarray"""
        _ensure_heavy_deps()
        rx, ry, rw, rh = rect
        if self._kind == 'dxcam':
            frame = None
            try:
                # 必须用 grab 而不是 get_latest_frame：后者在静态画面下会阻塞等新帧，
                # 直接拖垮录制节奏（实测帧率被拉到 5fps）
                frame = self._impl.grab()
                self._dx_err = 0
            except Exception:
                self._dx_err += 1
                if self._dx_err > 20:
                    print("[录制] dxcam 连续异常，永久降级 GDI")
                    if self._gdi is None:
                        self._gdi = _GdiGrabber()
                    self._impl = self._gdi
                    self._kind = 'gdi'
            if frame is not None:
                self._last_frame = frame
            # 画面无变化时 DXGI 不出新帧：复用上一帧，由外层按目标 fps 重复写入
            frame = self._last_frame
            if frame is not None:
                ox, oy = self._frame_origin
                out = self._crop(frame, rx - ox, ry - oy, rw, rh)
                if out is not None:
                    return out
            # 首次未拿到帧或区域越出主屏（副屏/跨屏窗口）：GDI 兜底
            if self._gdi is None:
                self._gdi = _GdiGrabber()
            return self._gdi.grab((rx, ry, rw, rh))
        if self._kind == 'd3dshot':
            frame = self._impl.capture(region=(rx, ry, rx + rw, ry + rh))
            if frame is None:
                return None
            return cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        return self._impl.grab((rx, ry, rw, rh))

    def close(self):
        # DXCamera 有 stop/release，_GdiGrabber 有 close，d3dshot 只有 stop
        if self._dx_cam is not None:
            try:
                self._dx_cam.stop()
                self._dx_cam.release()
            except Exception:
                pass
        gdi = self._gdi if self._gdi is not None else (
            self._impl if self._kind == 'gdi' else None)
        if gdi is not None:
            try:
                gdi.close()
            except Exception:
                pass
        if self._kind == 'd3dshot' and self._impl is not None:
            try:
                self._impl.stop()
            except Exception:
                pass
        self._impl = None
        self._gdi = None
        self._dx_cam = None
        self._last_frame = None
        self._kind = None


# ═══════════════════════════════════════════════════
#  编码器（ffmpeg / libx264）
# ═══════════════════════════════════════════════════

# 画质档：crf 越小越清晰、体积越大。
# preset 用 fast 而不是 veryfast：veryfast 关掉了 trellis 与 8x8dct、subme 只有 2，
# 桌面文字这种“细笔画 + 大面积纯色”的画面会被量化成糊块（本机 1080p30 实测
# fast/medium 都有 5x 以上编码余量，veryfast 的清晰度损失没有换来任何收益）
QUALITY_PRESETS = {
    # label 就是输出分辨率档（比 fps 直观：清晰度看的是像素，不是帧率）
    # height=输出高度；720p/1080p 主动降采样，0=屏幕原生 1:1（不做无意义的上采样）
    # CRF 阶梯 24/20/15（差 4~5 点）：旧版 21/17/15 只差 2 点，1080p 屏上
    # “1080p”与“原生”码率几乎一样，用户反馈三档录出来看不出区别。
    'standard': {'label': '720p', 'crf': 24, 'preset': 'fast', 'height': 720},
    'hd':       {'label': '1080p', 'crf': 20, 'preset': 'fast', 'height': 1080},
    'uhd':      {'label': '原生', 'crf': 15, 'preset': 'fast', 'height': 0},
}
QUALITY_DEFAULT = 'hd'


def _snap_quality(value):
    """把历史值/非法值归到三档；兼容中文标签、分辨率写法与 720/1080 这类旧值"""
    key = str(value or '').strip().lower()
    if key in QUALITY_PRESETS:
        return key
    alias = {'标准': 'standard', '高清': 'hd', '超清': 'uhd',
             '720p': 'standard', '720': 'standard', '1280x720': 'standard',
             '1080p': 'hd', '1080': 'hd', '1920x1080': 'hd',
             '原生': 'uhd', 'native': 'uhd', 'source': 'uhd', 'max': 'uhd',
             'high': 'hd', 'ultra': 'uhd', 'best': 'uhd'}
    return alias.get(key, QUALITY_DEFAULT)


def _output_size(src_w, src_h, quality):
    """按画质档换算输出尺寸：目标高度不小于源高度时保持原生，绝不放大失真"""
    th = QUALITY_PRESETS[_snap_quality(quality)].get('height') or 0
    src_w, src_h = max(2, int(src_w)), max(2, int(src_h))
    if th <= 0 or th >= src_h:
        return src_w // 2 * 2, src_h // 2 * 2
    ow = max(2, int(round(src_w * th / float(src_h))) // 2 * 2)
    return ow, th // 2 * 2


_FFMPEG_CACHE = None   # None=未探测；False=没有；str=可执行文件路径
_FFMPEG_LOCK = threading.Lock()


def _find_ffmpeg():
    """定位 ffmpeg：PATH 优先，其次 imageio-ffmpeg 自带二进制（打包/离线环境兜底）"""
    global _FFMPEG_CACHE
    with _FFMPEG_LOCK:
        if _FFMPEG_CACHE is not None:
            return _FFMPEG_CACHE or None
        exe = shutil.which('ffmpeg')
        if not exe:
            try:
                import imageio_ffmpeg
                exe = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                exe = None
        _FFMPEG_CACHE = exe or False
        return _FFMPEG_CACHE or None


class FfmpegWriter:
    """libx264 编码器，接口与 cv2.VideoWriter 对齐（isOpened / write / release）。

    cv2.VideoWriter 无法指定码率：1920x1080 下 avc1 只有 2-4Mbps，桌面文字边缘
    压缩成糊块。这里用 ffmpeg 子进程按 CRF 编码，同一分辨率下清晰度提升 2-4 倍。
    找不到 ffmpeg 时由调用方回退 cv2（画质变差但不影响可用）。
    """

    def __init__(self, path, w, h, fps, crf, preset='fast'):
        exe = _find_ffmpeg()
        if not exe:
            raise RuntimeError('未找到 ffmpeg')
        gop = max(1, int(round(fps * 2)))   # 2 秒一关键帧：拖动进度条 / 网页预览响应更快
        cmd = [
            exe, '-hide_banner', '-loglevel', 'error', '-y',
            '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{int(w)}x{int(h)}',
            '-r', f'{fps:g}', '-i', '-',
            # 色彩管线显式化：raw bgr24 没有色彩信息，swscale 默认按 BT.601 转 YUV
            # 且不写元数据，而播放器对 >=720p 视频默认按 BT.709 解释 ->
            # 红/蓝偏色、整体发灰，就是“录出来的画面和屏幕看到的不是一个颜色”。
            # 这里统一按 BT.709 + tv 范围转换并写入元数据，播放端按同一套参数解码。
            '-vf', 'scale=out_color_matrix=bt709:out_range=tv',
            '-c:v', 'libx264', '-preset', preset, '-crf', str(crf),
            '-pix_fmt', 'yuv420p', '-profile:v', 'high', '-g', str(gop),
            '-colorspace', 'bt709', '-color_primaries', 'bt709',
            '-color_trc', 'bt709', '-color_range', 'tv',
            '-movflags', '+faststart', path,
        ]
        flags = 0x08000000 if os.name == 'nt' else 0   # CREATE_NO_WINDOW：不弹黑框
        # 偶发 WinError 5（杀毒/索引器刚创建目标目录时的瞬时占用）-> 短重试，
        # 不能因为一次拒绝访问就回退到低码率编码器
        last = None
        for attempt in range(3):
            try:
                self._proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, creationflags=flags)
                break
            except OSError as e:
                last = e
                self._proc = None
                time.sleep(0.25 * (attempt + 1))
        if self._proc is None:
            raise last or RuntimeError('ffmpeg 启动失败')
        self._size = (int(w), int(h))
        self._err = b''

    def isOpened(self):
        return self._proc is not None and self._proc.poll() is None

    def write(self, frame):
        if not self.isOpened():
            return
        try:
            # 管道写入要求 C 连续 buffer；切片出来的帧不一定连续
            self._proc.stdin.write(np.ascontiguousarray(frame).data)
        except Exception as e:
            print(f"[录制] 编码写入失败: {e}")
            self._proc = None

    def release(self):
        p = self._proc
        if p is None:
            return
        self._proc = None
        try:
            p.stdin.close()
        except Exception:
            pass
        try:
            _, err = p.communicate(timeout=60)
            self._err = err or b''
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        if self._err:
            tail = self._err.decode('utf-8', 'ignore').strip().splitlines()[-3:]
            print("[录制] ffmpeg: " + ' | '.join(tail))


# ═══════════════════════════════════════════════════
#  录制器
# ═══════════════════════════════════════════════════

class ScreenRecorder:
    MAX_BUFFER_FRAMES = 30  # writer 未就绪时最多缓存 30 帧（≈1.5s @20fps）
    MAX_CATCHUP_FRAMES = 6  # 单次最多补 6 个重复帧（0.3s @20fps），防止卡顿后疯狂追赶

    def __init__(self, fps, save_dir, quality=QUALITY_DEFAULT):
        self._fps = fps
        # 画质档：决定编码 CRF（清晰度），与帧率/分辨率无关
        self._quality = _snap_quality(quality)
        # 实际落盘帧率：录制开始时按抓屏能力校准。成片时长由它决定，
        # 若沿用「设定值」而抓屏达不到，成片会被加速播放（30fps 声明、18 帧/秒实写 -> 1.7 倍速）
        self._effective_fps = fps
        self._pause_accum = 0.0
        self._pause_start = 0.0
        self._timeline_t0 = 0.0   # 时间轴基准：writer 就绪后才开始计时（不含校准开销）
        self._save_dir = save_dir
        self._running = False
        self._paused = False
        self._writer = None
        self._filepath = None
        self._record_rect = None
        self._clicks = deque(maxlen=32)
        self._click_lock = threading.Lock()
        self._total_clicks = 0
        self._start_ts = 0
        self._frame_count = 0
        self._backend = None
        self._mon_cache = None
        self._target_hwnd = None
        self._target_web = False
        self._pending_frames = deque(maxlen=self.MAX_BUFFER_FRAMES)
        # 录制标注图元（画笔/箭头/框选/荧光/文字），屏幕坐标；overlay 预览与帧渲染共用
        self._annotations = []
        self._ann_lock = threading.Lock()
        self._writer_ready = threading.Event()
        self._stopped_event = threading.Event()
        # 抓帧时间轴真正开始后才置位：面板/网页必须等它再启动 00:00 计时
        self._started_event = threading.Event()
        self._start_time = 0

    # ─── public ────────────────────────────

    def add_click(self, x, y):
        with self._click_lock:
            self._total_clicks += 1
            self._clicks.append({'x': x, 'y': y, 't': time.time()})

    def get_clicks_snapshot(self, window=0.8):
        now = time.time()
        with self._click_lock:
            return [(c['x'], c['y'], now - c['t']) for c in self._clicks
                    if 0 <= now - c['t'] < window]

    def add_annotation(self, ann):
        with self._ann_lock:
            self._annotations.append(ann)

    def undo_annotation(self):
        with self._ann_lock:
            if self._annotations:
                self._annotations.pop()

    def clear_annotations(self):
        with self._ann_lock:
            self._annotations.clear()

    def get_annotations(self):
        with self._ann_lock:
            return list(self._annotations)

    def is_running(self):
        return self._running

    def is_started(self):
        """抓帧时间轴是否已建立；未建立前只算“准备中”，不能开始计时"""
        return self._started_event.is_set()

    def get_start_time(self):
        """视频 00:00 对应的墙钟时间；准备中返回 0"""
        return self._timeline_t0 if self._started_event.is_set() else 0.0

    def wait_started(self, timeout=None):
        """等待抓帧真正开始；CLI 写状态前用它对齐 start_time"""
        return self._started_event.wait(timeout)

    def is_paused(self):
        return self._paused

    def pause(self):
        self._paused = True
        self._pause_start = time.time()

    def resume(self):
        if self._paused and self._pause_start:
            self._pause_accum += time.time() - self._pause_start
        self._paused = False
        self._pause_start = 0.0

    def get_effective_fps(self):
        """实际落盘帧率：录制中为校准后的值，空闲时为设定值"""
        return self._effective_fps if self._running else self._fps

    def _active_elapsed(self):
        """有效录制时长（扣除暂停时段），成片帧数必须与它成正比"""
        now = time.time()
        base = self._timeline_t0 or self._start_time
        paused = self._pause_accum + (now - self._pause_start if self._pause_start else 0)
        return max(0.0, now - base - paused)

    def get_record_rect(self):
        return self._record_rect

    def set_fps(self, fps):
        self._fps = max(5, min(30, int(fps)))

    def set_quality(self, quality):
        self._quality = _snap_quality(quality)

    def get_quality(self):
        return self._quality

    def set_save_dir(self, d):
        self._save_dir = d

    def start_fullscreen(self):
        _ensure_heavy_deps()
        if not self._mon_cache:
            with mss.mss() as sct:
                self._mon_cache = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        mon = self._mon_cache
        self._record_rect = (mon['left'], mon['top'], mon['width'], mon['height'])
        self._target_hwnd = None
        self._target_web = False
        return self._start()

    def start_window(self, hwnd, title, x, y, w, h, web=False):
        self._record_rect = (x, y, w, h)
        self._target_hwnd = hwnd
        self._target_web = web
        return self._start()

    def stop(self):
        """D3: 等 capture 线程 flush 完最后一帧"""
        if not self._running:
            return None, 0
        self._running = False
        self._stopped_event.wait(timeout=5.0)
        return self._filepath, self._total_clicks

    # ─── internal ──────────────────────────

    def _start(self):
        if self._running:
            return False
        os.makedirs(self._save_dir, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        self._filepath = os.path.join(self._save_dir, f'recording_{ts}.mp4')
        rx, ry, rw, rh = self._record_rect
        # H.264/mp4 要求偶数宽高，奇数尺寸会导致文件无法播放
        rw = max(2, int(rw) // 2 * 2)
        rh = max(2, int(rh) // 2 * 2)
        self._record_rect = (rx, ry, rw, rh)
        # 虚拟屏幕边界，用于窗口部分出屏时 clamp 抓帧区域
        try:
            import win32api
            import win32con
            self._vscreen = (win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN),
                             win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN),
                             win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN),
                             win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN))
        except Exception:
            self._vscreen = (0, 0, 100000, 100000)
        print(f"[录制] 开始: {rw}x{rh} @ ({rx},{ry}) fps={self._fps} -> {self._filepath}")

        self._running = True
        self._paused = False
        self._effective_fps = self._fps
        self._pause_accum = 0.0
        self._pause_start = 0.0
        self._timeline_t0 = 0.0
        self._start_time = time.time()
        self._start_ts = self._start_time
        self._frame_count = 0
        self._total_clicks = 0
        self._pending_frames.clear()
        self._writer_ready.clear()
        self._stopped_event.clear()
        self._started_event.clear()
        with self._click_lock:
            self._clicks.clear()

        # 校准抓屏能力 -> 建 writer -> 抓帧，三步必须串行：writer 的帧率要取校准后的真实值
        threading.Thread(target=self._capture_loop, args=(rw, rh), daemon=True).start()
        return True

    def _init_writer(self, src_w, src_h):
        _ensure_heavy_deps()
        q = QUALITY_PRESETS[self._quality]
        # 输出分辨率由画质档决定：720p/1080p 降采样，原生档与抓帧 1:1
        rw, rh = _output_size(src_w, src_h, self._quality)
        self._writer_size = (rw, rh)
        self._writer = None
        # 1) ffmpeg + libx264：可按 CRF 控质量，这是清晰度的关键
        try:
            w = FfmpegWriter(self._filepath, rw, rh, self._effective_fps,
                             q['crf'], q['preset'])
            if w.isOpened():
                self._writer = w
                src = f"（源 {src_w}x{src_h}）" if (rw, rh) != (src_w, src_h) else ''
                print(f"[录制] 编码器: libx264 CRF {q['crf']}（{q['label']}）"
                      f" @ {self._effective_fps}fps {rw}x{rh}{src}")
        except Exception as e:
            print(f"[录制] ffmpeg 不可用，回退 OpenCV 编码: {e}")
        # 2) 兜底：OpenCV（码率不可控，画质明显变差，但保证能录）
        if self._writer is None:
            for codec in ('avc1', 'mp4v'):
                fourcc = cv2.VideoWriter_fourcc(*codec)
                w = cv2.VideoWriter(self._filepath, fourcc, self._effective_fps, (rw, rh))
                if w.isOpened():
                    self._writer = w
                    print(f"[录制] 编码器: {codec} @ {self._effective_fps}fps（低码率，画质受限）")
                    break
                w.release()
        self._writer_ready.set()

    def _clamp_to_screen(self, x, y, w, h):
        """把抓帧区域限制在虚拟屏幕内，窗口部分出屏时只录可见部分"""
        vx, vy, vw, vh = getattr(self, '_vscreen', (0, 0, 100000, 100000))
        x = max(x, vx)
        y = max(y, vy)
        x2 = min(x + w, vx + vw)
        y2 = min(y + h, vy + vh)
        return x, y, max(2, x2 - x), max(2, y2 - y)

    def _fit_canvas(self, frame, cw, ch):
        """等比缩放到固定画布并黑边居中，避免窗口缩放导致画面畸变"""
        _ensure_heavy_deps()
        fh, fw = frame.shape[:2]
        if fw <= 0 or fh <= 0:
            return frame
        scale = min(cw / fw, ch / fh)
        nw = max(2, int(fw * scale) // 2 * 2)
        nh = max(2, int(fh * scale) // 2 * 2)
        resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
        canvas = np.zeros((ch, cw, 3), dtype=np.uint8)
        x0 = (cw - nw) // 2
        y0 = (ch - nh) // 2
        canvas[y0:y0 + nh, x0:x0 + nw] = resized
        return canvas

    def _calibrate_fps(self):
        """实测抓屏耗时，得出当前屏幕尺寸下真正能达到的帧率上限。

        GDI(BitBlt) 在 1920x1080 下只能抓 ~20fps，若硬按 30fps 声明写文件，
        成片会被加速播放且与 20fps 档产物几乎相同——档位差异必须落在真实帧率上。
        """
        rect = self._record_rect
        samples = []
        for _ in range(3):
            try:
                t = time.perf_counter()
                if self._backend.grab(rect) is not None:
                    samples.append(time.perf_counter() - t)
            except Exception:
                break
        if not samples:
            return self._fps
        median = sorted(samples)[len(samples) // 2]
        # 留 30% 余量：不只是抓屏，每帧还要叠加点击标记/标注绘制 + libx264 编码
        # （实测 1080p 仅按抓屏耗时估算 30fps，真实吞吐只有 ~27fps，成片会轻微加速）
        cap = int(1.0 / max(median * 1.30, 1e-6))
        return max(5, min(self._fps, cap))

    def _emit_frame(self, frame):
        """按输出分辨率落一帧：尺寸不符时等比缩放（720p/1080p 档 + 窗口缩放兜底）"""
        ws_w, ws_h = getattr(self, '_writer_size', (0, 0))
        if ws_w and (frame.shape[1] != ws_w or frame.shape[0] != ws_h):
            frame = self._fit_canvas(frame, ws_w, ws_h)
        self._writer.write(frame)
        self._frame_count += 1

    def _write_frame(self, frame):
        """按时间轴写帧：抓帧偶发落后时补重复帧，保证成片时长 == 真实录制时长"""
        if not (self._writer_ready.is_set() and self._writer and self._writer.isOpened()):
            self._pending_frames.append(frame)
            return
        while self._pending_frames:
            self._emit_frame(self._pending_frames.popleft())
        self._emit_frame(frame)
        gap = int(self._active_elapsed() * self._effective_fps) - self._frame_count
        # 卡顿/抓帧阻塞导致的欠帧用重复帧补齐（重复帧编码几乎不增体积）；
        # 单帧最多补 MAX_CATCHUP_FRAMES，剩余下一帧继续追，避免长时间阻塞和成片变短
        for _ in range(min(max(gap, 0), self.MAX_CATCHUP_FRAMES)):
            self._emit_frame(frame)

    def _capture_loop(self, default_w, default_h):
        self._backend = CaptureBackend()
        self._backend.open()
        self._effective_fps = self._calibrate_fps()
        if self._effective_fps != self._fps:
            print(f"[录制] 抓屏能力上限 {self._effective_fps}fps（设定 {self._fps}fps），按实际值落盘保证时长准确")
        # 预热：dxcam 首次取帧可能阻塞等新帧（静止画面下可达 1s），
        # 先拿到首帧再定时间轴基准，避免把等待期算进片长
        warm = None
        try:
            warm = self._backend.grab(self._record_rect)
        except Exception:
            pass
        # 编码尺寸必须等于真实抓帧尺寸经画质档换算后的值：若用录制矩形声明尺寸而
        # 实际抓到的帧尺寸不同（窗口出屏被 clamp / 缩放），整片会被等比缩小，1080p 也会被缩糊
        if warm is not None and warm.size:
            src_w, src_h = int(warm.shape[1]), int(warm.shape[0])
        else:
            src_w, src_h = default_w, default_h
        init_w, init_h = _output_size(src_w, src_h, self._quality)
        self._writer_size = (init_w, init_h)
        # 编码器初始化需 ~0.5-1s：仍后台建，但时间轴要等它就绪。
        # 若先起时间轴，这 1 秒的帧只能进 pending（上限 30 帧，@30fps 刚好 1 秒），
        # 装不下就从左侧丢掉最早的帧，再被补帧逻辑用新画面填满 ——
        # 表现就是“计时从 00:00 走，但成片开头 1 秒没有画面”。等待期面板显示“准备中”。
        threading.Thread(target=self._init_writer, args=(src_w, src_h), daemon=True).start()
        self._writer_ready.wait(timeout=10.0)
        # 时间轴第一帧必须现抓：warm 是 init 之前的画面，已经过期
        first = None
        try:
            first = self._backend.grab(self._record_rect)
        except Exception:
            pass
        if first is None or not first.size:
            first = warm
        self._timeline_t0 = time.time()
        if first is not None and first.size:
            self._write_frame(first)
        # 首帧已进时间轴，才开始对外计时：面板/网页的 00:00 与成片第一帧对齐
        self._started_event.set()

        rx, ry, rw, rh = self._record_rect
        interval = 1.0 / self._effective_fps
        next_t = time.perf_counter()
        frame_idx = 0

        while self._running:
            # C2: perf_counter 调度，落后过多时重置基线
            now = time.perf_counter()
            if now < next_t:
                time.sleep(next_t - now)
            elif now - next_t > interval * 3:
                next_t = now
            next_t += interval

            if self._paused:
                continue

            try:
                # C4/C6/C7: 窗口位置每 5 帧查一次（DWM 真实边界，不含隐形阴影）
                if self._target_hwnd and frame_idx % 5 == 0:
                    try:
                        fr = _get_window_frame_rect(self._target_hwnd)
                        if fr:
                            new = fr
                            if self._target_web:
                                wr = _get_web_content_rect(self._target_hwnd, frame_rect=fr)
                                if wr:
                                    new = wr
                            if new[2] > 50 and new[3] > 50 and new != (rx, ry, rw, rh):
                                rx, ry, rw, rh = new
                                self._record_rect = new
                    except Exception:
                        pass

                # 先 clamp 到虚拟屏幕，避免窗口部分出屏时抓帧失败
                gx, gy, gw, gh = self._clamp_to_screen(rx, ry, rw, rh)
                frame = self._backend.grab((gx, gy, gw, gh))
                if frame is None or frame.size == 0:
                    frame_idx += 1
                    continue
                self._draw_clicks(frame, gx, gy)
                self._draw_annotations(frame, gx, gy)
                # 抓屏 API 本身不包含光标：最后补画，保证“鼠标 + 水波纹”一起出现
                self._draw_cursor(frame, gx, gy)
                ws_w, ws_h = getattr(self, '_writer_size', (gw, gh))
                if frame.shape[1] != ws_w or frame.shape[0] != ws_h:
                    # 等比缩放+黑边居中：窗口移动/缩放/最大化都不变形
                    frame = self._fit_canvas(frame, ws_w, ws_h)

                # B1: writer 未就绪时缓存前若干帧；写帧时按时间轴补帧，保证成片时长准确
                self._write_frame(frame)

                frame_idx += 1
            except Exception as e:
                print(f"[录制] 帧错误: {e}")

        # 释放
        self._writer_ready.wait(timeout=5.0)
        if self._writer:
            while self._pending_frames:
                try:
                    self._emit_frame(self._pending_frames.popleft())
                except Exception:
                    break
            self._writer.release()
            self._writer = None
        if self._backend:
            self._backend.close()
            self._backend = None
        elapsed = time.time() - (self._timeline_t0 or self._start_time)
        ws = getattr(self, '_writer_size', (0, 0))
        print(f"[录制] 停止: {self._frame_count} 帧 @ {self._effective_fps}fps, "
              f"{elapsed:.1f} 秒, 输出 {ws[0]}x{ws[1]} -> {self._filepath}")
        self._stopped_event.set()

    def _draw_annotations(self, frame, ox, oy):
        """把标注图元渲染进录制帧（与 overlay 实时预览共用绘制函数，所见即所录）"""
        anns = self.get_annotations()
        if not anns:
            return
        try:
            h, w = frame.shape[:2]
            # 注意：必须用 sip.voidptr 包装零拷贝 QImage；直接传 numpy data 会因
            # PyQt5 内部复制导致绘制不回写（实测帧全黑）
            qimg = QImage(sip.voidptr(frame.ctypes.data), w, h,
                          frame.strides[0], QImage.Format_BGR888)
            painter = QPainter(qimg)
            _paint_annotations(painter, anns, ox, oy)
            painter.end()
        except Exception as e:
            print(f"[录制] 标注渲染错误: {e}")

    def _draw_clicks(self, frame, rx, ry):
        """C3: 从左侧 popleft 过期点击，遍历不拷贝

        蓝白水波纹与桌面 ClickOverlay 使用同一套 _click_ripples，保证预览即成品。"""
        _ensure_heavy_deps()
        now = time.time()
        H, W = frame.shape[:2]
        edge_bgr = CLICK_EDGE_RGB[::-1]
        ring_bgr = CLICK_RING_RGB[::-1]
        core_bgr = CLICK_CORE_RGB[::-1]
        with self._click_lock:
            while self._clicks and now - self._clicks[0]['t'] >= CLICK_LIFE:
                self._clicks.popleft()
            for c in self._clicks:
                age = now - c['t']
                if age < 0 or age >= CLICK_LIFE:
                    continue
                progress = age / CLICK_LIFE
                lx, ly = c['x'] - rx, c['y'] - ry
                if not (0 <= lx < W and 0 <= ly < H):
                    continue
                ripples = _click_ripples(progress)
                max_r = max(r for r, _ in ripples)
                pad = int(max_r + 5)
                x0, x1 = max(0, lx - pad), min(W, lx + pad + 1)
                y0, y1 = max(0, ly - pad), min(H, ly + pad + 1)
                if x1 <= x0 or y1 <= y0:
                    continue
                roi = frame[y0:y1, x0:x1]
                cx, cy = lx - x0, ly - y0

                # 双层细环：外环像回声一样更淡，整体半径控制在 25px 内
                for rr, fade in ripples:
                    layer = roi.copy()
                    cv2.circle(layer, (cx, cy), int(round(rr + 0.8)),
                               edge_bgr, 1, cv2.LINE_AA)
                    cv2.circle(layer, (cx, cy), int(round(rr)),
                               ring_bgr, 2, cv2.LINE_AA)
                    alpha = max(0.0, min(1.0, fade))
                    if alpha > 0:
                        frame[y0:y1, x0:x1] = cv2.addWeighted(
                            layer, alpha, roi, 1 - alpha, 0)

                # 中心只留一个小白点标定落点，不做放射线或大圆点
                core_fade = (1 - progress) ** 2.2
                if core_fade > 0:
                    layer = roi.copy()
                    cv2.circle(layer, (cx, cy), 2, edge_bgr, 1, cv2.LINE_AA)
                    cv2.circle(layer, (cx, cy), 1, core_bgr, -1, cv2.LINE_AA)
                    frame[y0:y1, x0:x1] = cv2.addWeighted(
                        layer, core_fade, roi, 1 - core_fade, 0)

    def _draw_cursor(self, frame, rx, ry):
        """把系统光标画进录制帧：抓屏后端不含指针，录出来会“只见波纹不见鼠标”。

        光标按屏幕物理坐标 + 热点对齐，与点击水波纹同时叠加，
        突出一次完整点击；命中本工具面板/工具条时跳过，避免录出悬空指针。
        """
        cur = _cursor_state()
        if not cur:
            return
        sx, sy, bgr, alpha, hx, hy = cur
        if _point_in_own_window(sx, sy):
            return
        H, W = frame.shape[:2]
        ch, cw = bgr.shape[:2]
        x, y = sx - hx - rx, sy - hy - ry
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(W, x + cw), min(H, y + ch)
        if x1 <= x0 or y1 <= y0:
            return
        ix0, iy0 = x0 - x, y0 - y
        ix1, iy1 = ix0 + (x1 - x0), iy0 + (y1 - y0)
        src = bgr[iy0:iy1, ix0:ix1].astype(np.float32)
        a = alpha[iy0:iy1, ix0:ix1, None]
        roi = frame[y0:y1, x0:x1].astype(np.float32)
        frame[y0:y1, x0:x1] = np.clip(
            src * a + roi * (1.0 - a), 0, 255).astype(np.uint8)


# ═══════════════════════════════════════════════════
#  鼠标监听
# ═══════════════════════════════════════════════════

# 点击标记：蓝白小水波纹（与视频帧绘制共用，预览与成片一致）
CLICK_LIFE = 0.8
CLICK_RING_RGB = (125, 211, 252)   # #7DD3FC 天蓝
CLICK_EDGE_RGB = (15, 23, 42)      # #0F172A 深蓝灰描边
CLICK_CORE_RGB = (248, 250, 252)   # #F8FAFC 中心白点
CLICK_MAX_R = 20.0                 # 主环最大半径，比旧版 35 更克制


def _click_ripples(progress):
    """返回 [(半径, 透明度), ...]：内环实、外环淡，像一圈小水波。"""
    p = max(0.0, min(1.0, progress))
    ease = 1 - (1 - p) ** 2.0
    r = 5.0 + (CLICK_MAX_R - 5.0) * ease
    fade = (1 - p) ** 1.35
    return [(r, fade), (r + 4.5, fade * 0.45)]


def _point_in_own_window(x, y, pid=None):
    """鼠标落点是否命中本录制工具自己的窗口（面板/工具条/菜单/输入框/边框）。

    WindowFromPoint 是快路径；Qt 弹出菜单打开时会拿走鼠标捕获，
    WindowFromPoint 可能报出下层应用，因此再用“可见、非穿透的自身顶层窗口矩形”兜底。
    """
    pid = int(os.getpid() if pid is None else pid)
    try:
        import win32gui
        import win32con
        import win32process
        x, y = int(x), int(y)
        hwnd = win32gui.WindowFromPoint((x, y))
        if hwnd:
            root = win32gui.GetAncestor(hwnd, 2)  # GA_ROOT
            if root:
                hwnd = root
            _, win_pid = win32process.GetWindowThreadProcessId(hwnd)
            if int(win_pid) == pid:
                return True

        hits = []

        def _enum(h, _):
            if not win32gui.IsWindowVisible(h) or win32gui.IsIconic(h):
                return True
            ex = win32gui.GetWindowLong(h, win32con.GWL_EXSTYLE)
            if ex & win32con.WS_EX_TRANSPARENT:
                return True           # 点击层/手模式标注层是故意穿透的
            _, wp = win32process.GetWindowThreadProcessId(h)
            if int(wp) != pid:
                return True
            l, t, r, b = win32gui.GetWindowRect(h)
            if l <= x < r and t <= y < b:
                hits.append(h)
                return False
            return True

        win32gui.EnumWindows(_enum, None)
        return bool(hits)
    except Exception:
        return False


# 系统光标读取：抓屏 API 不包含指针，录制时按帧叠加真实光标位图
_CURSOR_API = None
_CURSOR_CACHE = {}
_CURSOR_CACHE_LOCK = threading.Lock()
_CURSOR_CACHE_MAX = 24


def _cursor_api():
    """惰性初始化光标读取所需的 Win32 结构与函数（不增加启动期依赖）。"""
    global _CURSOR_API
    if _CURSOR_API is not None:
        return _CURSOR_API
    import ctypes
    from ctypes import wintypes

    class CURSORINFO(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('flags', wintypes.DWORD),
                    ('hCursor', wintypes.HANDLE), ('ptScreenPos', wintypes.POINT)]

    class ICONINFO(ctypes.Structure):
        _fields_ = [('fIcon', wintypes.BOOL), ('xHotspot', wintypes.DWORD),
                    ('yHotspot', wintypes.DWORD), ('hbmMask', wintypes.HBITMAP),
                    ('hbmColor', wintypes.HBITMAP)]

    class BITMAP(ctypes.Structure):
        _fields_ = [('bmType', wintypes.LONG), ('bmWidth', wintypes.LONG),
                    ('bmHeight', wintypes.LONG), ('bmWidthBytes', wintypes.LONG),
                    ('bmPlanes', wintypes.WORD), ('bmBitsPixel', wintypes.WORD),
                    ('bmBits', wintypes.LPVOID)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [('biSize', wintypes.DWORD), ('biWidth', wintypes.LONG),
                    ('biHeight', wintypes.LONG), ('biPlanes', wintypes.WORD),
                    ('biBitCount', wintypes.WORD), ('biCompression', wintypes.DWORD),
                    ('biSizeImage', wintypes.DWORD),
                    ('biXPelsPerMeter', wintypes.LONG),
                    ('biYPelsPerMeter', wintypes.LONG),
                    ('biClrUsed', wintypes.DWORD),
                    ('biClrImportant', wintypes.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [('bmiHeader', BITMAPINFOHEADER),
                    ('bmiColors', wintypes.DWORD * 3)]

    u = ctypes.windll.user32
    g = ctypes.windll.gdi32
    u.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
    u.GetCursorInfo.restype = wintypes.BOOL
    u.GetIconInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(ICONINFO)]
    u.GetIconInfo.restype = wintypes.BOOL
    u.GetDC.argtypes = [wintypes.HWND]
    u.GetDC.restype = wintypes.HDC
    u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    u.ReleaseDC.restype = ctypes.c_int
    g.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
    g.GetObjectW.restype = ctypes.c_int
    g.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                            wintypes.UINT, ctypes.c_void_p,
                            ctypes.POINTER(BITMAPINFO), wintypes.UINT]
    g.GetDIBits.restype = ctypes.c_int
    g.DeleteObject.argtypes = [wintypes.HANDLE]
    g.DeleteObject.restype = wintypes.BOOL
    _CURSOR_API = {
        'ctypes': ctypes, 'wintypes': wintypes, 'u': u, 'g': g,
        'CURSORINFO': CURSORINFO, 'ICONINFO': ICONINFO, 'BITMAP': BITMAP,
        'BITMAPINFO': BITMAPINFO, 'BITMAPINFOHEADER': BITMAPINFOHEADER,
    }
    return _CURSOR_API


def _read_dib(hbm, w, h, bits, api):
    """读取 GDI 位图像素：32bpp 返回 BGRA，1bpp 返回布尔掩码。"""
    ctypes = api['ctypes']
    bi = api['BITMAPINFO']()
    bi.bmiHeader.biSize = ctypes.sizeof(api['BITMAPINFOHEADER'])
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h          # 负高度 = 自上而下，避免再翻转
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = bits
    bi.bmiHeader.biCompression = 0      # BI_RGB
    stride = ((w * bits + 31) // 32) * 4
    buf = (ctypes.c_ubyte * (stride * h))()
    hdc = api['u'].GetDC(0)
    if not hdc:
        return None
    try:
        rows = api['g'].GetDIBits(hdc, hbm, 0, h, buf,
                                  ctypes.byref(bi), 0)
    finally:
        api['u'].ReleaseDC(0, hdc)
    if not rows:
        return None
    arr = np.frombuffer(bytes(buf), dtype=np.uint8).reshape(h, stride)
    if bits == 32:
        return arr[:, :w * 4].reshape(h, w, 4)
    return np.unpackbits(arr, axis=1)[:, :w].astype(bool)


def _read_cursor_bitmap(hcursor, api):
    """提取当前光标位图：返回 (BGR, alpha, hotspot_x, hotspot_y)。

    彩色光标优先用自带 alpha；老式单色光标（hbmColor 为空）
    用 AND/XOR 双掩码合成，否则会得到一块全黑方块。
    """
    ctypes = api['ctypes']
    ii = api['ICONINFO']()
    if not api['u'].GetIconInfo(hcursor, ctypes.byref(ii)):
        return None
    try:
        bm = api['BITMAP']()
        src = ii.hbmColor or ii.hbmMask
        if not src or not api['g'].GetObjectW(
                src, ctypes.sizeof(api['BITMAP']), ctypes.byref(bm)):
            return None
        w, h = int(bm.bmWidth), int(bm.bmHeight)
        if w <= 0 or h <= 0:
            return None
        if ii.hbmColor:
            color = _read_dib(ii.hbmColor, w, h, 32, api)
            if color is None:
                return None
            bgr = color[:, :, :3].copy()
            alpha = color[:, :, 3].astype(np.float32) / 255.0
            if alpha.max() <= 0 and ii.hbmMask:
                mask = _read_dib(ii.hbmMask, w, h, 1, api)
                if mask is not None:
                    alpha = (~mask).astype(np.float32)
        else:
            raw = _read_dib(ii.hbmMask, w, h, 1, api)
            if raw is None:
                return None
            hh = h // 2
            if hh <= 0 or raw.shape[0] < hh * 2:
                return None
            and_mask = raw[:hh]
            xor_mask = raw[hh:hh * 2]
            bgr = np.where(xor_mask[:, :, None], 255, 0).astype(np.uint8)
            bgr = np.repeat(bgr, 3, axis=2)
            alpha = (~and_mask).astype(np.float32)
        return bgr, alpha, int(ii.xHotspot), int(ii.yHotspot)
    finally:
        for hbm in (ii.hbmColor, ii.hbmMask):
            if hbm:
                api['g'].DeleteObject(hbm)


def _cursor_bitmap(hcursor):
    """按光标句柄缓存位图：同一形状每帧不重复做 GetDIBits。"""
    try:
        key = int(hcursor or 0)
    except Exception:
        return None
    if key <= 0:
        return None
    now = time.time()
    with _CURSOR_CACHE_LOCK:
        ent = _CURSOR_CACHE.get(key)
        if ent and now - ent[0] < 10.0:
            return ent[1:]
    got = _read_cursor_bitmap(key, _cursor_api())
    if got:
        with _CURSOR_CACHE_LOCK:
            _CURSOR_CACHE[key] = (now,) + got
            while len(_CURSOR_CACHE) > _CURSOR_CACHE_MAX:
                _CURSOR_CACHE.pop(next(iter(_CURSOR_CACHE)))
    return got


def _cursor_state():
    """当前可见光标：(屏幕 x, 屏幕 y, BGR, alpha, 热点 x, 热点 y)。"""
    if sys.platform != 'win32':
        return None
    try:
        api = _cursor_api()
        ci = api['CURSORINFO']()
        ci.cbSize = api['ctypes'].sizeof(api['CURSORINFO'])
        if not api['u'].GetCursorInfo(api['ctypes'].byref(ci)):
            return None
        if not (int(ci.flags) & 0x00000001) or not ci.hCursor:   # CURSOR_SHOWING
            return None
        bmp = _cursor_bitmap(ci.hCursor)
        if not bmp:
            return None
        return (int(ci.ptScreenPos.x), int(ci.ptScreenPos.y)) + bmp
    except Exception:
        return None


class MouseClickListener:
    """点击监听：轮询 GetAsyncKeyState 边沿检测（替代 pynput 全局钩子）。

    低级鼠标钩子（WH_MOUSE_LL）会让每个鼠标事件都经过回调线程，
    会造成录制中系统光标闪烁；轮询方案零钩子、零系统影响，
    坐标取按压瞬间的 GetCursorPos（物理像素，与抓帧坐标一致）。
    点击本工具自己的面板/工具条时跳过，不计数也不画标记。"""

    def __init__(self, on_click, skip_own_process=True):
        self._on_click = on_click
        self._skip_own_process = bool(skip_own_process)
        self._stop_evt = threading.Event()
        self._thread = None

    def start(self):
        self.stop()
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name='mouse-click')
        self._thread.start()

    def stop(self):
        self._stop_evt.set()
        self._thread = None

    def _handle_press(self, x, y):
        """命中本工具自己的 UI 时不计数：面板/标注条/文字框/迷你条都算。"""
        if self._skip_own_process and _point_in_own_window(x, y):
            return
        self._on_click(x, y)

    def _loop(self):
        try:
            import win32api
            import win32con
        except Exception:
            return
        prev_l = prev_r = False
        while not self._stop_evt.is_set():
            down_l = bool(win32api.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000)
            down_r = bool(win32api.GetAsyncKeyState(win32con.VK_RBUTTON) & 0x8000)
            if (down_l and not prev_l) or (down_r and not prev_r):
                try:
                    self._handle_press(*win32api.GetCursorPos())
                except Exception:
                    pass
            prev_l, prev_r = down_l, down_r
            time.sleep(0.01)


# ═══════════════════════════════════════════════════
#  C1: 桌面点击 Overlay（零延迟反馈）
# ═══════════════════════════════════════════════════

class ClickOverlay(QWidget):
    """全屏透明 overlay：点击后立刻绘制脉冲（不等下一视频帧）"""

    def __init__(self, recorder):
        super().__init__()
        self._recorder = recorder
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            | Qt.BypassWindowManagerHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setStyleSheet("background: transparent;")
        geo = QApplication.primaryScreen().virtualGeometry()
        self.setGeometry(geo)
        self.hide()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.setInterval(33)  # ~30Hz
        self._enabled = False
        self._was_active = False   # 上一 tick 是否有活跃点击脉冲（用于清屏时机）

    def showEvent(self, e):
        super().showEvent(e)
        # 原生层显式鼠标穿透：避免全屏透明层接管光标查询（WM_SETCURSOR），
        # 否则覆盖区域的应用光标（手型/文本）被断言成默认箭头，滑过时闪烁
        try:
            import win32gui
            import win32con
            hwnd = int(self.winId())
            ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE,
                                   ex | win32con.WS_EX_TRANSPARENT | 0x08000000)
        except Exception:
            pass

    def set_enabled(self, on):
        if on and not self._enabled:
            self._enabled = True
            self.show()
            self.raise_()
            self._timer.start()
        elif not on and self._enabled:
            self._enabled = False
            self._timer.stop()
            self.hide()

    def _tick(self):
        if not (self._recorder.is_running() and not self._recorder.is_paused()):
            return
        # 仅当脉冲生命窗口内还有活跃点击时才重绘（含收尾清屏帧）：
        # 平时零重绘，避免全屏 layered 窗口 30Hz 空转干扰系统光标合成（鼠标闪烁）
        has_clicks = bool(self._recorder.get_clicks_snapshot(window=CLICK_LIFE))
        if has_clicks or self._was_active:
            self.update()
        self._was_active = has_clicks

    def paintEvent(self, event):
        if not self._enabled:
            return
        clicks = self._recorder.get_clicks_snapshot(window=CLICK_LIFE)
        if not clicks:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        ox, oy = self.geometry().x(), self.geometry().y()
        for (sx, sy, age) in clicks:
            progress = age / CLICK_LIFE
            if progress >= 1.0:
                continue
            lx, ly = sx - ox, sy - oy
            # 双层小水波：外环更淡，与视频帧 _draw_clicks 共用 _click_ripples
            painter.setBrush(Qt.NoBrush)
            for r, fade in _click_ripples(progress):
                edge = QColor(*CLICK_EDGE_RGB, max(0, min(255, int(190 * fade))))
                ring = QColor(*CLICK_RING_RGB, max(0, min(255, int(235 * fade))))
                painter.setPen(QPen(edge, 1.6))
                painter.drawEllipse(QPointF(lx, ly), r + 0.8, r + 0.8)
                painter.setPen(QPen(ring, 1.7))
                painter.drawEllipse(QPointF(lx, ly), r, r)

            # 中心只留一个小白点标定落点
            core_fade = (1 - progress) ** 2.2
            a_core = max(0, min(255, int(255 * core_fade)))
            painter.setPen(QPen(QColor(*CLICK_EDGE_RGB, a_core), 1.2))
            painter.setBrush(QColor(*CLICK_CORE_RGB, a_core))
            painter.drawEllipse(QPointF(lx, ly), 1.8, 1.8)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(Qt.NoPen)


# ═══════════════════════════════════════════════════
#  录制标注（画笔 / 箭头 / 框选 / 荧光 / 文字）
# ═══════════════════════════════════════════════════

# 标注调色板：10 色，覆盖三类底——
#   亮色（红橙黄绿青蓝紫玫红）用于深色截图/彩色画面
#   白色给全黑底（全屏录制桌面暗色主题、终端、视频）
#   黑色给白底页面（后台管理系统、文档），否则白字完全看不见
ANN_COLORS = {
    '红色': (255, 59, 48),
    '橙色': (255, 149, 0),
    '黄色': (255, 214, 10),
    '绿色': (52, 199, 89),
    '青色': (34, 211, 238),
    '蓝色': (10, 132, 255),
    '紫色': (191, 90, 242),
    '玫红': (255, 45, 146),
    '白色': (255, 255, 255),
    '黑色': (17, 24, 39),
}
ANN_WIDTHS = {'细': 3, '中': 5, '粗': 8}
# 字号直接给像素档（不再分小/中/大）：默认取最小号 16px，按录制分辨率与内容密度自选
ANN_TEXT_SIZES = (16, 18, 20, 22, 24, 28, 32, 36, 40, 48, 56, 64)
ANN_TEXT_DEFAULT = ANN_TEXT_SIZES[0]
ANN_MARKER_FACTOR = 4       # 荧光笔宽度 = 细/中/粗 ×4（半透明粗线）


def _lum(rgb):
    """感知亮度（0~255）：用来判断标注色该配深底还是浅底"""
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


def _readable_on_dark(r, g, b, min_lum=160):
    """把标注色换成深色底上看得清的版本：亮度不够时向白色混合（保留色相）。
    紫色/黄色/蓝色这类浅色裸字压在白页面上几乎看不清，输入期间必须衬底。"""
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    if lum >= min_lum:
        return r, g, b
    k = (min_lum - lum) / (255.0 - lum + 1e-6)
    return (int(r + (255 - r) * k), int(g + (255 - g) * k), int(b + (255 - b) * k))


def _tool_icon(key, color):
    """工具按钮矢量图标（简单线条风格，随选中态换色）"""
    dpr = QApplication.primaryScreen().devicePixelRatio()
    size = 20
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.fill(Qt.transparent)
    pm.setDevicePixelRatio(dpr)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(size / 18.0, size / 18.0)   # 图标坐标按 18 基准设计，整体缩放到目标尺寸
    c = QColor(color)
    if key == 'hand':
        # 鼠标指针（实心箭头形：不绘制、正常操作）
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawPolygon(QPolygonF([
            QPointF(6.0, 2.6), QPointF(6.0, 15.2), QPointF(9.1, 12.1),
            QPointF(11.2, 16.4), QPointF(13.3, 15.3), QPointF(11.2, 11.1),
            QPointF(15.0, 11.0)]))
    elif key == 'pen':
        # 铅笔：斜杆 + 笔尖
        p.setPen(QPen(c, 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawPolygon(QPolygonF([
            QPointF(11.9, 3.5), QPointF(14.5, 6.1), QPointF(7.7, 12.9), QPointF(5.1, 10.3)]))
        p.drawLine(QPointF(10.6, 4.8), QPointF(13.2, 7.4))
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawPolygon(QPolygonF([
            QPointF(5.1, 10.3), QPointF(7.7, 12.9), QPointF(3.1, 14.7)]))
    elif key == 'arrow':
        # 箭头：斜线 + 实心箭头头部
        p.setPen(QPen(c, 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawLine(QPointF(3.6, 14.4), QPointF(12.4, 5.6))
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawPolygon(QPolygonF([
            QPointF(14.4, 3.6), QPointF(13.6, 8.2), QPointF(9.8, 4.4)]))
    elif key in ('rect', 'dashrect'):
        if key == 'dashrect':
            pen = QPen(c, 1.8, Qt.DashLine)
            pen.setDashPattern([2.0, 1.4])
        else:
            pen = QPen(c, 1.8)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(3.3, 5.3, 11.4, 7.4), 1.8, 1.8)
    elif key == 'ellipse':
        p.setPen(QPen(c, 1.8))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(9.0, 9.0), 5.6, 4.6)
    elif key == 'marker':
        # 荧光笔：粗斜笔画
        p.setPen(QPen(c, 4.6, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(4.8, 13.2), QPointF(13.2, 4.8))
    elif key == 'text':
        # 字母 T（几何线条）
        p.setPen(QPen(c, 2.0, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(4.6, 5.2), QPointF(13.4, 5.2))
        p.drawLine(QPointF(9.0, 5.2), QPointF(9.0, 14.2))
    p.end()
    return QIcon(pm)


def _paint_annotations(painter, anns, ox, oy, live=None):
    """绘制标注图元：屏幕坐标减 (ox, oy) 换算到目标画布。

    实时预览层（AnnotationOverlay）与录制帧渲染（ScreenRecorder）共用本函数，
    保证“屏幕上看到的”与“视频里录下的”完全一致。
    """
    painter.setRenderHint(QPainter.Antialiasing, True)
    items = list(anns)
    if live:
        items.append(live)
    for a in items:
        _paint_one_annotation(painter, a, ox, oy)


def _paint_one_annotation(painter, a, ox, oy):
    t = a.get('type')
    r, g, b = a['color']
    col = QColor(r, g, b, int(a.get('alpha', 255)))
    w = a.get('width', 5)
    if t == 'pen':
        pts = a.get('pts') or []
        if not pts:
            return
        if len(pts) == 1:
            painter.setPen(Qt.NoPen)
            painter.setBrush(col)
            painter.drawEllipse(QPointF(pts[0][0] - ox, pts[0][1] - oy), w / 2.0, w / 2.0)
            painter.setBrush(Qt.NoBrush)
            return
        painter.setPen(QPen(col, w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPolyline(QPolygonF([QPointF(x - ox, y - oy) for (x, y) in pts]))
    elif t in ('arrow', 'line'):
        x1, y1 = a['x1'] - ox, a['y1'] - oy
        x2, y2 = a['x2'] - ox, a['y2'] - oy
        painter.setPen(QPen(col, w, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        if t == 'arrow':
            ang = math.atan2(y2 - y1, x2 - x1)
            hl = max(14.0, w * 3.2)          # 箭头头部长度
            spread = math.radians(152)
            for sgn in (1, -1):
                painter.drawLine(
                    QPointF(x2, y2),
                    QPointF(x2 + hl * math.cos(ang + sgn * spread),
                            y2 + hl * math.sin(ang + sgn * spread)))
    elif t in ('rect', 'ellipse'):
        x1, y1 = a['x1'] - ox, a['y1'] - oy
        x2, y2 = a['x2'] - ox, a['y2'] - oy
        pen = QPen(col, w)
        if a.get('dash'):
            # 虚线框：段长/间隔按线宽等比，细线时不至于糊成实线，粗线时也不过分稀疏
            pen.setStyle(Qt.DashLine)
            pen.setDashPattern([2.2, 1.4])
            pen.setCapStyle(Qt.FlatCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        rect = QRectF(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
        if t == 'rect':
            painter.drawRect(rect)
        else:
            painter.drawEllipse(rect)
    elif t == 'text':
        size = a.get('size', 22)
        # 用像素字号而非磅值：与输入框 font-size:22px 完全同尺寸，保证“预览=成片”
        f = QFont('Microsoft YaHei')
        f.setPixelSize(int(size))
        f.setBold(True)
        f.setHintingPreference(QFont.PreferFullHinting)   # 小字号不糊
        painter.setFont(f)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        typo_x, typo_y = a['x'] - ox, a['y'] - oy + int(size * 0.85)
        # 只画一层：旧版的 1.5px 黑色投影在压缩后就是“重影”
        painter.setPen(col)
        painter.drawText(QPointF(typo_x, typo_y), a.get('text', ''))


class AnnotationOverlay(QWidget):
    """全屏标注层：实时预览标注；手模式整体鼠标穿透，不挡下层操作。

    与 ClickOverlay 同构：WDA_EXCLUDEFROMCAPTURE 后屏幕可见但绝不进录屏/截图；
    视频中的标注由录制帧逐帧渲染（与预览共用 _paint_annotations），所见即所录。
    """

    def __init__(self, recorder):
        super().__init__()
        self._recorder = recorder
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            | Qt.BypassWindowManagerHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent;")
        self.setGeometry(QApplication.primaryScreen().virtualGeometry())
        self._tool = 'hand'
        self._exclude_rects = []     # 鼠标命中排除区（工具条/输入框，点击穿透）
        self._color = ANN_COLORS['红色']
        self._width = ANN_WIDTHS['中']
        self._live = None            # 正在绘制中的图元（未提交）
        self._moving = None          # 正在拖动的文字图元（文字工具按住已有文字）
        self._on_text_click = None   # 文字工具点击回调（gx, gy）
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)  # 悬停检测（文字上方切拖动光标）
        self.hide()

    # ── 外部接口 ──────────────────────

    def enable(self, on):
        if on:
            self._live = None
            self._moving = None
            self.show()
            self.raise_()
            self._sync_click_through()
        else:
            self._live = None
            self._moving = None
            self.hide()

    def set_tool(self, tool):
        self._tool = tool
        self._live = None
        self._moving = None
        self.setCursor(Qt.CrossCursor)
        self._sync_click_through()
        if tool != 'hand':
            self.raise_()    # 绘制模式提至最顶，避免被其它置顶窗口局部遮挡
        self.update()

    def set_style(self, color=None, width=None):
        if color is not None:
            self._color = color
        if width is not None:
            self._width = width

    def set_exclude(self, rects):
        """设置鼠标命中排除区（全局坐标 QRect 列表）：区域内清空 alpha，点击穿透到
        工具条/文字输入框（点击画面会把 overlay 提到最顶，不能依赖 z 顺序）"""
        self._exclude_rects = [r for r in (rects or []) if r]
        self.update()

    def set_temp_text(self, ann):
        """文字输入期间把“未提交”的文字实时画到画面上（边打字边预览）；
        传 None 清除。图元不进 recorder，确认后才成为正式标注。"""
        self._live = ann
        self.update()

    def _sync_click_through(self):
        """手模式=窗口整体鼠标穿透；绘制模式=接收鼠标；全程不抢焦点"""
        try:
            import win32gui
            import win32con
            hwnd = int(self.winId())
            ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            ex |= 0x08000000  # WS_EX_NOACTIVATE：绘制时不抢下层窗口焦点
            if self._tool == 'hand':
                ex |= win32con.WS_EX_TRANSPARENT
            else:
                ex &= ~win32con.WS_EX_TRANSPARENT
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex)
        except Exception:
            pass

    # ── 绘制交互（仅绘制模式能收到鼠标事件）──

    def _hit_text(self, gx, gy):
        """返回命中的文字图元（文字工具下可拖动）；命中盒=文字包围盒略外扩"""
        for a in reversed(self._recorder.get_annotations()):
            if a.get('type') != 'text':
                continue
            font = QFont('Microsoft YaHei', a.get('size', 22), QFont.Bold)
            fm = QFontMetrics(font)
            w = fm.width(a.get('text', ''))
            h = fm.height()
            if QRectF(a['x'] - 4, a['y'] - 4, w + 8, h + 8).contains(gx, gy):
                return a
        return None

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            self._live = None    # 右键取消当前绘制
            self.update()
            return
        if e.button() != Qt.LeftButton or self._tool == 'hand':
            return
        g = e.globalPos()
        if self._tool == 'text':
            hit = self._hit_text(g.x(), g.y())
            if hit is not None:
                # 按住已有文字 -> 拖动调整位置（实时更新，帧渲染同步生效）
                self._moving = {'target': hit, 'dx': g.x() - hit['x'], 'dy': g.y() - hit['y']}
                self.setCursor(Qt.SizeAllCursor)
            elif self._on_text_click:
                self._on_text_click(g.x(), g.y())
            return
        if self._tool == 'pen':
            self._live = {'type': 'pen', 'color': self._color,
                          'width': self._width, 'pts': [(g.x(), g.y())]}
        elif self._tool == 'marker':
            # 荧光笔宽度跟随"细/中/粗"（×4 放大成荧光笔的粗笔画）
            self._live = {'type': 'pen', 'color': self._color,
                          'width': self._width * ANN_MARKER_FACTOR, 'alpha': 90,
                          'pts': [(g.x(), g.y())]}
        elif self._tool in ('arrow', 'rect', 'ellipse', 'dashrect'):
            is_dash_rect = self._tool == 'dashrect'
            self._live = {'type': 'rect' if is_dash_rect else self._tool,
                          'color': self._color, 'width': self._width,
                          'x1': g.x(), 'y1': g.y(), 'x2': g.x(), 'y2': g.y()}
            if is_dash_rect:
                self._live['dash'] = True
        self.update()

    def mouseMoveEvent(self, e):
        g = e.globalPos()
        if self._moving:
            t = self._moving['target']
            t['x'] = g.x() - self._moving['dx']
            t['y'] = g.y() - self._moving['dy']
            self.update()
            return
        if self._tool == 'text' and not self._live:
            # 悬停在已有文字上显示"可拖动"光标
            self.setCursor(Qt.SizeAllCursor if self._hit_text(g.x(), g.y()) is not None
                           else Qt.CrossCursor)
        if not self._live or self._live.get('type') not in ('pen', 'arrow', 'line', 'rect', 'ellipse'):
            return      # 文字预览不是鼠标绘制的图元，不能在这里当形状改坐标
        if not (e.buttons() & Qt.LeftButton):
            return
        if self._live['type'] == 'pen':
            pts = self._live['pts']
            last = pts[-1]
            if abs(g.x() - last[0]) + abs(g.y() - last[1]) >= 2:   # 过滤细微抖动
                pts.append((g.x(), g.y()))
        else:
            self._live['x2'] = g.x()
            self._live['y2'] = g.y()
        self.update()

    def mouseReleaseEvent(self, e):
        if self._moving:
            self._moving = None
            self.setCursor(Qt.CrossCursor)
        if e.button() != Qt.LeftButton or not self._live:
            return
        live = self._live
        # 只提交鼠标绘制的图元：文字预览（_live 由 set_temp_text 写入）没有 x1/y1，
        # 旧版在这里 live['x1'] 直接 KeyError，PyQt5 会瞬间杀掉整个面板进程
        if live.get('type') not in ('pen', 'arrow', 'line', 'rect', 'ellipse'):
            return
        self._live = None
        g = e.globalPos()
        if live['type'] == 'pen':
            pts = live['pts']   # 快速拖拽可能没有中间 move，用释放点补全终点
            if abs(g.x() - pts[-1][0]) + abs(g.y() - pts[-1][1]) >= 2:
                pts.append((g.x(), g.y()))
            if len(pts) >= 2:
                self._recorder.add_annotation(live)
        else:
            live['x2'], live['y2'] = g.x(), g.y()   # 同上：以释放点为准
            if abs(live['x2'] - live['x1']) + abs(live['y2'] - live['y1']) >= 8:
                self._recorder.add_annotation(live)   # 尺寸过小视为误触，不提交
        self.update()

    def paintEvent(self, e):
        painter = QPainter(self)
        if self._tool != 'hand':
            # Windows 对 per-pixel alpha 窗口按像素 alpha 做鼠标命中：alpha=0 的
            # 空白区域收不到任何鼠标事件（实测点击直接穿透），绘制模式必须铺一层
            # 肉眼不可见的 alpha=1 底，否则在空白处根本画不了。该层随窗口被
            # WDA_EXCLUDEFROMCAPTURE 排除，不会进入录屏。
            painter.fillRect(self.rect(), QColor(0, 0, 0, 1))
            if self._exclude_rects:
                # 工具条/输入框矩形清空 alpha：点击穿透 overlay 直达控件
                g = self.geometry()
                painter.setCompositionMode(QPainter.CompositionMode_Clear)
                for r in self._exclude_rects:
                    painter.fillRect(r.translated(-g.x(), -g.y()), Qt.transparent)
                painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        anns = self._recorder.get_annotations()
        if not anns and not self._live:
            return
        ox, oy = self.geometry().x(), self.geometry().y()
        _paint_annotations(painter, anns, ox, oy, live=self._live)


class _SwatchBtn(QAbstractButton):
    """色板色块：浅色工具条上的 18px 圆点，选中画外环而不是粗边框。

    旧版用 22px 方块 + 2px 白边表示选中：白边向内吃尺寸，选中/未选中都看不出
    颜色，且黄色/白色在浅底上没有边界。这里固定 18px 色块 + 1px 深色描边，
    选中改画外侧 2px 主色环（留 2px 空隙）：尺寸不变、位置不跳，一眼能看出选中。
    """

    def __init__(self, rgb, tip, parent=None):
        super().__init__(parent)
        self._rgb = rgb
        self._sel = False
        self._hover = False
        self.setFixedSize(22, 22)
        self.setToolTip(tip)
        self.setCursor(QCursor(Qt.PointingHandCursor))

    def set_selected(self, sel):
        if self._sel != sel:
            self._sel = sel
            self.update()

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cx, cy = self.width() / 2.0, self.height() / 2.0
        if self._sel or self._hover:
            # 浅色工具条上白环看不见：选中用主色环、悬浮用灰环
            p.setPen(QPen(QColor(79, 110, 247) if self._sel
                          else QColor(203, 213, 225), 2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(cx, cy), 9.5, 9.5)
        # 白/黄这类浅色圆点在浅底上没有边界，统一压一圈深色细边；
        # 黑/深蓝本身有反差，细边几乎看不出，也不影响
        edge = QColor(15, 23, 42, 90)
        p.setPen(QPen(edge, 1))
        p.setBrush(QColor(*self._rgb))
        p.drawEllipse(QPointF(cx, cy), 7.0, 7.0)
        p.end()


class _LineWidthBtn(QAbstractButton):
    """线宽按钮：直接画一条对应像素粗细的线。

    旧版按钮只写“细/中/粗”，看不出 3/5/8px 到底差多少；现在按钮里就是实际
    笔画粗细（选中态蓝底白线、悬浮态灰底），换颜色也不影响判断线宽。
    """

    def __init__(self, width, tip, parent=None):
        super().__init__(parent)
        self._width = int(width)
        self._sel = False
        self._hover = False
        self.setFixedSize(38, 24)
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self.setCursor(QCursor(Qt.PointingHandCursor))

    def set_selected(self, sel):
        if self._sel != sel:
            self._sel = sel
            self.update()

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self._sel or self._hover:
            p.setPen(QPen(QColor(199, 210, 254), 1) if self._sel else Qt.NoPen)
            p.setBrush(QColor(79, 110, 247) if self._sel else QColor(238, 241, 246))
            p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 6, 6)
        p.setPen(QPen(QColor(255, 255, 255) if self._sel else QColor(100, 116, 139),
                      self._width, Qt.SolidLine, Qt.RoundCap))
        y = self.height() / 2.0
        p.drawLine(QPointF(8, y), QPointF(self.width() - 8, y))
        p.end()


class _GrowLineEdit(QLineEdit):
    """会随内容变宽的单行输入框：额外跟踪输入法组字（拼音未上屏）的预编辑文本。

    QLineEdit 在组字期间不触发 textChanged，text() 也不含拼音；若只看 text()，
    框宽会停在旧值、拼音被压在框里滚动。这里把 preeditString 一起计入宽度。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.preedit = ''        # 输入法组字中的拼音（尚未上屏）

    def inputMethodEvent(self, e):
        self.preedit = e.preeditString() or ''
        super().inputMethodEvent(e)


class _TextInputPopup(QWidget):
    """文字标注输入框：输入框本身就直接长在画面上的文字位置（单层，所见即所得）

    旧版是“深色输入框 + 画面上另画一份同样的预览文字”，同一句话看两遍，位置还对不齐；
    现在只保留输入框这一层：透明底、与标注同色同字号、宽度随内容增长，
    确认后原地变成正式标注（基线对齐 _paint_one_annotation，文字不会跳位置）。

    Enter / 点击输入框外面 = 确认（空文本则静默关闭）；Esc = 取消。
    """

    def __init__(self, on_ok, on_cancel, on_vis=None):
        super().__init__()
        self._on_ok = on_ok
        self._on_cancel = on_cancel
        self._on_vis = on_vis       # 显隐/尺寸变化回调（通知标注层同步命中排除区）
        self._anchor = (0, 0)
        self._color = ANN_COLORS['红色']
        self._size = ANN_TEXT_DEFAULT
        self._closed_at = 0.0        # 最近一次关闭时刻（抑制同一下点击又弹出新框）
        self._cur_w = 0              # 本次输入已到达的宽度（只增不减，避免长句输入时窗口来回变）
        self._border = QColor(226, 232, 240, 110)   # 虚线边框色：_apply_style 按衬底深浅换
        self._armed = False          # 输入框是否已真正拿到焦点（激活过程中的抖动不算“点外面”）
        self._finishing = False      # 正在关闭：hide() 引发的焦点迁移不能再提交一次
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        # 透明底：窗口上只留一行字和一圈淡虚线（虚线在 paintEvent 里自绘）
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._edit = _GrowLineEdit()
        self._edit.setPlaceholderText('输入文字')
        self._edit.returnPressed.connect(self._commit)
        self._edit.textChanged.connect(lambda _t: self._sync_size())
        # 轮询兑底：输入法组字期间 textChanged 不触发，靠它让宽度跟手（80ms 足够跟眼）
        self._poll = QTimer(self)
        self._poll.setInterval(80)
        self._poll.timeout.connect(self._sync_size)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 1, 2, 1)   # 只留给虚线框，文字尽量贴锚点
        lay.setSpacing(0)
        lay.addWidget(self._edit)
        self._apply_style()
        self.hide()
        # 焦点离开整个输入框窗口 = 点了外面。QWidget.focusOutEvent 只在“弹窗自己”
        # 持有焦点时才触发，而焦点始终在内部 QLineEdit 上，必须监听全局焦点信号。
        _app = QApplication.instance()
        if _app is not None:
            _app.focusChanged.connect(self._on_focus_changed)

    def _font(self):
        """与画面标注完全同一套字体（_paint_one_annotation：像素字号 + 粗体 + 全 hinting）"""
        f = QFont('Microsoft YaHei')
        f.setPixelSize(int(self._size))
        f.setBold(True)
        f.setHintingPreference(QFont.PreferFullHinting)
        return f

    def _apply_style(self):
        """输入期间衬一块底色：透明底 + 裸字压在画面上时标注色会看不清。

        按标注色亮度选衬底：亮色（红/黄/紫/白…）用深底 + 同色提亮版；
        深色（黑/深蓝）用浅底 + 原色——白底页面上的黑字才看得清，
        确认后仍按原色写进画面。"""
        r, g, b = self._color
        if _lum(self._color) < 100:
            tr, tg, tb = r, g, b
            bg, ph = 'rgba(248,250,252,232)', QColor(71, 85, 105, 190)
            sel_bg, sel_fg = '#93c5fd', '#0f172a'
            self._border = QColor(15, 23, 42, 120)
        else:
            tr, tg, tb = _readable_on_dark(r, g, b)
            bg, ph = 'rgba(15,23,42,200)', QColor(226, 232, 240, 140)
            sel_bg, sel_fg = '#2563eb', '#ffffff'
            self._border = QColor(226, 232, 240, 110)
        self._edit.setFont(self._font())
        # 占位文字色走调色板：QLineEdit 的 QSS 不认 placeholder-text-color（Qt5 报 Unknown property）
        if hasattr(QPalette, 'PlaceholderText'):
            pal = self._edit.palette()
            pal.setColor(QPalette.PlaceholderText, ph)
            self._edit.setPalette(pal)
        self._edit.setStyleSheet(
            "QLineEdit { background:%s; border:none; border-radius:4px;"
            " padding:0 5px; color:rgb(%d,%d,%d);"
            " selection-background-color:%s; selection-color:%s; }" % (bg, tr, tg, tb, sel_bg, sel_fg))
        self._sync_size()

    def _sync_size(self):
        """宽度：默认就够长（240px），超过后按 96px 大块只增不减，到屏幕宽为止。

        旧版每输入一个字就 setFixedSize 一次：框宽逐像素刷新 + 光标回边触发
        QLineEdit 内部滚动，输入长句时看起来“一直在跳”。改成按 96px（约 4 个汉字）
        的大块跳跃后，一整句话只 resize 三五次，肉眼几乎看不到跳；到上限后宽度
        锁死，由输入框自己水平滚动。
        """
        fm = QFontMetrics(self._font())
        # 组字中的拼音也要占宽，否则中文输入时框不动、拼音被压在旧宽度里滚动
        text = self._edit.text() + getattr(self._edit, 'preedit', '')
        need = fm.horizontalAdvance(text)
        # 默认宽度：能一次装下十来个汉字，短词/拼音不会先宽后缩
        base = max(240, fm.horizontalAdvance('输入文字') + 28)
        vg = QApplication.primaryScreen().virtualGeometry()
        # 宽度上界跟着锚点走：右边界到屏幕边就停。旧版按“整屏宽-24”算，靠近右边缘
        # 标注一变宽就会被 _place 往左推回去（文字跟着横移）；按锚点算则永不横移
        left = max(vg.x(), self._anchor[0] - 9)
        cap = max(base, vg.x() + vg.width() - left)
        if need + 28 <= base:
            tw = base
        else:
            # 从 base 起步按 96px 整块向上取整：宽度只会跳几大档，不会逐字微调
            step = 96
            blocks = (need + 40 - base + step - 1) // step
            tw = min(cap, base + blocks * step)
        tw = max(self._cur_w, tw)                 # 本次输入内只增不减
        th = max(22, fm.height() + 2)
        # 尺寸真变了才动窗口：80ms 轮询每次都 resize/move 会让透明窗口整块重绘（输入时抖动）
        if self.width() == tw and self.height() == th:
            return
        self._cur_w = tw
        self.setFixedSize(tw, th)
        if self.isVisible():
            self._place()
            if self._on_vis:
                self._on_vis()      # 尺寸变了，鼠标穿透排除区要跟着更新

    def _place(self):
        """把框摆到锚点上：框内文字基线与 _paint_one_annotation 的画法对齐，确认时不位移"""
        gx, gy = self._anchor
        fm = QFontMetrics(self._font())
        # 标注基线在 gy + size*0.85；框内文字基线 = 上边距(1) + ascent + QLineEdit 内部偏移
        off_y = int(self._size * 0.85) - fm.ascent() - 2
        vg = QApplication.primaryScreen().virtualGeometry()
        # x 同样补上文字起点偏移（衬底左内边距 5px + QLineEdit 内部 4px），确认瞬间文字不会横移
        x = max(vg.x(), min(gx - 9, vg.x() + vg.width() - self.width()))
        y = max(vg.y(), min(gy + off_y, vg.y() + vg.height() - self.height()))
        if self.x() != x or self.y() != y:   # 位置没变不动窗口，避免重复 move 触发重绘
            self.move(x, y)

    def paintEvent(self, e):
        """只画一圈淡虚线：alpha=1 的底让整块都能接鼠标（per-pixel alpha 窗口空白处会穿透）"""
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(0, 0, 0, 1))
        # 衬底颜色变了，虚线边框要跟着换（深底用淡白、浅底用深灰，任何颜色下都能辨认边界）
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(self._border, 1, Qt.DashLine))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 4, 4)

    def anchor(self):
        return self._anchor

    def last_closed_at(self):
        return self._closed_at

    def open_at(self, gx, gy, color=None, size=None):
        if color is not None:
            self._color = color
        if size is not None:
            self._size = size
        self._anchor = (gx, gy)
        self._cur_w = 0              # 新一次输入：宽度重新从默认值算
        self._edit.blockSignals(True)
        self._edit.clear()
        self._edit.blockSignals(False)
        self._apply_style()
        self._place()               # 先摆到锚点再 show，避免先闪一下再跳位
        self._armed = False
        self.show()
        self.raise_()
        self.activateWindow()
        self._edit.setFocus()
        self._poll.start()

    def _finish(self, submit):
        """关闭输入框：submit=True 提交非空文本，否则只清预览

        hide() 会把焦点还给系统，全局 focusChanged 会因此再次回调到这里，
        必须重入保护，否则同一条文字会被提交两次（画面上叠两层字）。
        """
        if self._finishing:
            return
        self._finishing = True
        try:
            text = self._edit.text().strip()
            self._closed_at = time.time()
            self._armed = False
            self.hide()
            if submit and text:
                self._on_ok(text)
            else:
                self._on_cancel()
        finally:
            self._finishing = False

    def cancel(self):
        """外部强制关闭（隐藏工具条时），不提交"""
        self._finish(False)

    def submit(self):
        """外部触发提交（点到画面时等同回车确认）"""
        self._finish(True)

    def _commit(self):
        self._finish(True)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self._finish(False)
        else:
            super().keyPressEvent(e)

    def _on_focus_changed(self, _old, now):
        """焦点从输入框移到别处 = 点外面，默认确认（不用回车）"""
        if not self.isVisible():
            return
        if now is not None and now.window() is self:
            self._armed = True      # 焦点已落在输入框内，之后再离开才算“点外面”
            return
        if self._armed:
            self._finish(True)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        if self.isVisible() and self._armed:
            # 兜底：焦点直接从弹窗离开（内部控件未接管时）
            self._finish(True)

    def showEvent(self, e):
        super().showEvent(e)
        if self._on_vis:
            self._on_vis()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._poll.stop()
        if self._on_vis:
            self._on_vis()


class AnnotationToolbar(QWidget):
    """录制中标注工具条：工具 / 颜色 / 线宽 / 字号 / 撤销 / 清空（贴靠主面板显示）"""

    # 按钮直接用中文标注用途（图标 emoji 在不同字体下显示差异大、难以辨认）
    TOOLS = [
        ('hand',    '手',   '手：不绘制，正常操作（鼠标穿透）'),
        ('pen',     '画笔', '画笔：自由手绘'),
        ('arrow',   '箭头', '箭头：拖拽画直线箭头'),
        ('rect',    '矩形', '矩形：拖拽画矩形框'),
        ('dashrect', '虚线框', '虚线框：拖拽画虚线矩形（默认最细，可调线宽）'),
        ('ellipse', '椭圆', '椭圆：拖拽画椭圆框'),
        ('marker',  '荧光', '荧光笔：半透明粗线划重点'),
        ('text',    '文字', '文字：点击画面直接输入（输入框就在文字位置，所见即所得）；字号/颜色见下方；按住已有文字可拖动'),
    ]

    def __init__(self, recorder, ann_overlay):
        super().__init__()
        self._recorder = recorder
        self._overlay = ann_overlay
        self._tool = 'hand'
        self._color_key = '红色'
        self._width_key = '中'
        self._size = ANN_TEXT_DEFAULT
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setObjectName('annRoot')
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # 宽度随工具变化（文字工具只显示字号、绘图工具只显示线宽），不再固定 352
        self._panel = None      # 贴靠的主面板引用（显隐变宽后重新贴靠用）
        # 不激活窗口：避免 Windows 首次点击被“激活窗口”吞掉（需点两次才生效），
        # 同时点击工具条不会抢走浏览器/应用的键盘焦点
        try:
            import win32gui
            import win32con
            hwnd = int(self.winId())
            ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            ex |= 0x08000000  # WS_EX_NOACTIVATE
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex)
        except Exception:
            pass
        self.setStyleSheet("""
            QWidget { color:#1e293b; font-family:'Microsoft YaHei'; }
            QLabel { background:transparent; }
            /* 工具按钮：未选中透明、悬浮浮出浅灰底、选中填主色（与面板分段控件同一套语义）*/
            QPushButton, QToolButton { border:none; border-radius:7px; padding:0;
                                      background:transparent; color:#475569;
                                      font-size:12px; font-weight:600; font-family:'Microsoft YaHei'; }
            QPushButton:hover, QToolButton:hover { background:#eef1f6; color:#1e293b; }
            /* 选中态要一眼可见：主色填充 + 白字加粗 + 淡蓝描边（与全站主按钮同一套） */
            QPushButton#annOn, QToolButton#annOn { background:#4f6ef7; color:#ffffff;
                                                   border:1px solid #c7d2fe; font-weight:800; }
            QPushButton#annOn:hover, QToolButton#annOn:hover { background:#3b5de7; border-color:#93c5fd; }
            QPushButton#annOff, QToolButton#annOff { background:transparent; color:#64748b; }
            QPushButton#annDanger { color:#dc2626; }
            QPushButton#annDanger:hover { background:#fef2f2; color:#b91c1c; }
            QMenu { background:#ffffff; color:#1e293b; border:1px solid #e2e8f0;
                    border-radius:8px; padding:4px; font-size:12px; }
            QMenu::item { padding:5px 24px 5px 10px; border-radius:4px; }
            QMenu::item:selected { background:#eef1fe; color:#4f6ef7; }
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self._tool_btns = {}
        for key, label, tip in self.TOOLS:
            b = QToolButton()               # 纯图标按钮（用途说明放悬停提示）
            b.setFixedSize(42, 30)
            b.setIconSize(QSize(20, 20))
            b.setIcon(_tool_icon(key, '#475569'))
            b.setToolTip(tip)
            b.setCursor(QCursor(Qt.PointingHandCursor))
            b.clicked.connect(lambda _=False, k=key: self._pick_tool(k))
            row1.addWidget(b)
            self._tool_btns[key] = b
        row1.addStretch()
        root.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(5)
        # 色板：10 色装进一个浅色圆角槽里（比一排裸圆点更像“取色器”，视线一扫就能比色）
        self._color_btns = {}
        pal_box = QWidget()
        pal_box.setObjectName('annPal')
        pal_box.setStyleSheet('QWidget#annPal { background:#f1f4fa; border-radius:10px; }')
        pl = QHBoxLayout(pal_box)
        pl.setContentsMargins(5, 1, 5, 1)
        pl.setSpacing(1)
        for key, rgb in ANN_COLORS.items():
            b = _SwatchBtn(rgb, f'颜色：{key}（画笔/箭头/框/文字共用）')
            b.clicked.connect(lambda _=False, k=key: self._pick_color(k))
            pl.addWidget(b)
            self._color_btns[key] = b
        row2.addWidget(pal_box)
        row2.addSpacing(8)
        # 线宽档（细/中/粗）：只对画笔/箭头/框/荧光笔这类“画线”工具有意义，
        # 文字/手工具时整组隐藏（用户反馈无关档位常年显示太啰嗦）
        self._width_group = QWidget()
        self._width_group.setStyleSheet('background:transparent;')
        wl = QHBoxLayout(self._width_group)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(5)
        self._width_btns = {}
        for key, px in ANN_WIDTHS.items():
            b = _LineWidthBtn(px, f'线宽：{key}（{px}px；画笔/箭头/框/荧光共用）')
            b.clicked.connect(lambda _=False, k=key: self._pick_width(k))
            wl.addWidget(b)
            self._width_btns[key] = b
        row2.addWidget(self._width_group)
        # 字号：点按钮从像素档里选（与线宽档占同一个槽位，切换工具不跳位置）
        self._size_group = QWidget()
        self._size_group.setStyleSheet('background:transparent;')
        sl = QHBoxLayout(self._size_group)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(5)
        size_lab = QLabel('字号')
        size_lab.setStyleSheet('background:transparent; color:#64748b; font-size:11px;')
        sl.addWidget(size_lab)
        self._size_btn = QPushButton()
        self._size_btn.setFixedSize(64, 24)
        self._size_btn.setToolTip('文字字号：点击选择像素大小')
        self._size_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._size_btn.clicked.connect(self._show_size_menu)
        sl.addWidget(self._size_btn)
        self._size_menu = QMenu(self)
        self._size_actions = {}
        for px in ANN_TEXT_SIZES:
            act = self._size_menu.addAction(f'{px} px')
            act.setCheckable(True)
            act.triggered.connect(lambda _=False, v=px: self._pick_size(v))
            self._size_actions[px] = act
        # QMenu 是独立顶层窗口，不会继承工具条的 WDA 排除；不单独处理会被录进视频
        _exclude_from_capture(self._size_menu)
        row2.addWidget(self._size_group)
        row2.addStretch()
        root.addLayout(row2)

        row3 = QHBoxLayout()
        row3.setSpacing(5)
        row3.addStretch()
        undo_btn = QPushButton('撤销')
        undo_btn.setFixedSize(44, 24)
        undo_btn.setToolTip('撤销上一个标注')
        undo_btn.setCursor(QCursor(Qt.PointingHandCursor))
        undo_btn.clicked.connect(self._on_undo)
        row3.addWidget(undo_btn)
        clr_btn = QPushButton('清空')
        clr_btn.setObjectName('annDanger')
        clr_btn.setFixedSize(44, 24)
        clr_btn.setToolTip('清空全部标注（只影响之后的画面）')
        clr_btn.setCursor(QCursor(Qt.PointingHandCursor))
        clr_btn.clicked.connect(self._on_clear)
        row3.addWidget(clr_btn)
        root.addLayout(row3)

        # 文字输入框（点击画面位置后弹出）：它就是画面上那行字本身，不再另画一层预览
        self._text_popup = _TextInputPopup(self._on_text_ok, self._on_text_cancel,
                                           self._sync_exclude)
        _exclude_from_capture(self._text_popup)
        self._overlay._on_text_click = self._open_text_input

        self._refresh_styles()
        self.hide()

    # ── 对外 ────────────────────────

    def paintEvent(self, e):
        # 半透明顶层 + 自绘圆角卡片：与主面板同一张“浅色玻璃卡”（白底 + 灰蓝描边）
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(226, 232, 240), 1))
        p.setBrush(QColor(255, 255, 255, 246))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 12, 12)

    def reset(self):
        self._tool = 'hand'
        self._color_key = '红色'
        self._width_key = '中'
        self._size = ANN_TEXT_DEFAULT
        self._overlay.set_tool('hand')
        self._overlay.set_style(color=ANN_COLORS['红色'], width=ANN_WIDTHS['中'])
        self._refresh_styles()

    def dismiss(self):
        """隐藏工具条（含文字输入框）"""
        self._text_popup.cancel()
        self.hide()
        self._sync_exclude()

    def _place_at(self, panel):
        pg = panel.geometry()
        vg = QApplication.primaryScreen().virtualGeometry()
        x = max(vg.x(), min(pg.x(), vg.x() + vg.width() - self.width()))
        y = pg.y() + pg.height() + 8
        if y + self.height() > vg.y() + vg.height():
            y = pg.y() - self.height() - 8
        self.move(x, y)

    def show_beside(self, panel):
        self._panel = panel
        self._place_at(panel)
        self.show()
        self.raise_()
        self._sync_exclude()

    def move_beside(self, panel):
        self._panel = panel
        if self.isVisible():
            self._place_at(panel)
            self._sync_exclude()

    def _sync_exclude(self):
        """把工具条/文字输入框矩形同步给标注层（区域清空 alpha，点击可穿透）"""
        rects = []
        if self.isVisible():
            rects.append(self.geometry())
        if self._text_popup.isVisible():
            rects.append(self._text_popup.geometry())
        self._overlay.set_exclude(rects)

    # ── 交互 ────────────────────────

    def _pick_tool(self, key):
        prev = self._tool
        self._tool = 'hand' if (key == self._tool and key != 'hand') else key
        # 虚线框首次选中默认最细；用户手动调粗后保持当前选择，不强行改回
        if self._tool == 'dashrect' and prev != 'dashrect':
            self._width_key = '细'
            self._overlay.set_style(width=ANN_WIDTHS['细'])
        self._overlay.set_tool(self._tool)
        self._refresh_styles()
        self.raise_()    # 始终显示在标注层之上，保证按钮可点击

    def _pick_color(self, key):
        self._color_key = key
        self._overlay.set_style(color=ANN_COLORS[key])
        self._refresh_styles()

    def _pick_width(self, key):
        self._width_key = key
        self._overlay.set_style(width=ANN_WIDTHS[key])
        self._refresh_styles()

    def _show_size_menu(self):
        self._size_menu.popup(self._size_btn.mapToGlobal(
            QPoint(0, self._size_btn.height() + 4)))

    def _pick_size(self, px):
        self._size = int(px)
        self._refresh_styles()

    def _open_text_input(self, gx, gy):
        """点击画面：正在输入时 = 确认当前这条；空着时 = 在点击处新建输入框"""
        popup = self._text_popup
        if popup.isVisible():
            popup.submit()      # 点到画面 = 确认（不用回车，也不会另起一个空框）
            return
        # 这一下点击刚刚引发失焦提交（“点外面 = 确认”）：不再紧接着弹出新框
        if time.time() - popup.last_closed_at() < 0.35:
            return
        popup.open_at(gx, gy, ANN_COLORS[self._color_key], self._size)

    def _on_undo(self):
        self._recorder.undo_annotation()
        self._overlay.update()

    def _on_clear(self):
        self._recorder.clear_annotations()
        self._overlay.update()

    def _on_text_ok(self, text):
        gx, gy = self._text_popup.anchor()
        self._overlay.set_temp_text(None)   # 预览转正：避免与最终标注重影
        self._recorder.add_annotation({'type': 'text', 'color': ANN_COLORS[self._color_key],
                                       'size': self._size,
                                       'x': gx, 'y': gy, 'text': text})
        self._overlay.update()
        # 保持文字工具：写完可立即按住文字拖动位置（不用再点一次按钮）

    def _on_text_cancel(self):
        # 取消输入：清掉画面上的实时预览，保持文字工具便于重新点击或拖动已有文字
        self._overlay.set_temp_text(None)

    def _refresh_styles(self):
        text_mode = self._tool == 'text'
        hand_mode = self._tool == 'hand'
        for key, b in self._tool_btns.items():
            on = key == self._tool
            b.setObjectName('annOn' if on else 'annOff')
            b.setIcon(_tool_icon(key, '#ffffff' if on else '#475569'))
            b.setStyle(b.style())
        for key, b in self._color_btns.items():
            b.set_selected(key == self._color_key)
        for key, b in self._width_btns.items():
            b.set_selected(key == self._width_key)
        self._size_btn.setText(f'{self._size} px')
        for px, act in self._size_actions.items():
            act.setChecked(px == self._size)
        # 档位按工具显隐：线宽只在绘图工具出现，字号只在文字工具出现（手模式都收起）
        self._width_group.setVisible(not text_mode and not hand_mode)
        self._size_group.setVisible(text_mode)
        self.adjustSize()
        # 宽度变了就重新贴靠面板，并把新的鼠标穿透排除区同步给标注层
        if self.isVisible() and self._panel is not None:
            self._place_at(self._panel)
            self._sync_exclude()


class _MiniRecBar(QWidget):
    """录制中的迷你状态条：面板隐藏后的常驻控制入口。

    用户点“—”隐藏面板后找不到状态框（以为录制失控、没有停止入口），
    这里把隐藏收成一条迷你胶囊：● REC + 计时 + 暂停/继续 + 停止 + 恢复面板。
    置顶可拖动，与面板一样被排除在录屏/截图外。
    """

    def __init__(self, panel):
        super().__init__()
        self._panel = panel
        self._drag_pos = None
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("""
            QWidget { color:#1e293b; font-family:'Microsoft YaHei'; }
            QLabel#miniRec { color:#ef4444; font-size:12px; font-weight:700; background:transparent; }
            QLabel#miniTime { color:#1e293b; font-size:15px; font-weight:600;
                              font-family:Consolas,'Courier New',monospace; background:transparent; }
            QPushButton { border:none; border-radius:6px; height:24px; padding:0 10px;
                          font-size:12px; font-weight:600; font-family:'Microsoft YaHei'; }
            QPushButton#miniPause { background:#d97706; color:#fff; }
            QPushButton#miniPause:hover { background:#b45309; }
            QPushButton#miniStop { background:#059669; color:#fff; }
            QPushButton#miniStop:hover { background:#047857; }
            QPushButton#miniBack { background:#eef1f6; color:#475569; }
            QPushButton#miniBack:hover { background:#e2e8f0; color:#1e293b; }
        """)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 8, 8)
        lay.setSpacing(8)
        self._rec_lbl = QLabel('● REC')
        self._rec_lbl.setObjectName('miniRec')
        lay.addWidget(self._rec_lbl)
        self._time_lbl = QLabel('00:00')
        self._time_lbl.setObjectName('miniTime')
        lay.addWidget(self._time_lbl)
        self._pause_btn = QPushButton('暂停')
        self._pause_btn.setObjectName('miniPause')
        self._pause_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._pause_btn.setToolTip('暂停/继续（与面板同步）')
        self._pause_btn.clicked.connect(self._on_pause)
        lay.addWidget(self._pause_btn)
        self._stop_btn = QPushButton('■ 停止')
        self._stop_btn.setObjectName('miniStop')
        self._stop_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._stop_btn.setToolTip('停止并保存录制')
        self._stop_btn.clicked.connect(self._on_stop)
        lay.addWidget(self._stop_btn)
        back = QPushButton('⤢ 恢复面板')
        back.setObjectName('miniBack')
        back.setCursor(QCursor(Qt.PointingHandCursor))
        back.setToolTip('回到完整录制控制台')
        back.clicked.connect(self._on_back)
        lay.addWidget(back)
        self.hide()

    def paintEvent(self, e):
        # 与主面板同一张浅色玻璃卡
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(226, 232, 240), 1))
        p.setBrush(QColor(255, 255, 255, 246))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 12, 12)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_pos = QPoint(e.globalPos() - self.frameGeometry().topLeft())

    def mouseMoveEvent(self, e):
        if self._drag_pos and e.buttons() & Qt.LeftButton:
            self.move(e.globalPos() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None

    def set_recording(self, on):
        """录制中：REC 红字 + 计时 + 暂停/停止；就绪态：只留「恢复面板」。

        用户点“—”后必须总能看到这个入口（以前非录制态隐藏面板就凭空消失），
        所以迷你条常驻显示，只是就绪态收起与录制相关的控件。"""
        self._rec_lbl.setText('● REC' if on else '● 就绪')
        self._rec_lbl.setStyleSheet('color:#ef4444;' if on else 'color:#16a34a;')
        for w in (self._time_lbl, self._pause_btn, self._stop_btn):
            w.setVisible(bool(on))
        self.adjustSize()

    def show_at(self, pos=None):
        """在面板原位置出现（没给位置时落屏幕右上角），并收敛回可视屏内"""
        self.adjustSize()
        vg = QApplication.primaryScreen().virtualGeometry()
        if pos is not None:
            x, y = pos.x(), pos.y()
        else:
            x = vg.x() + vg.width() - self.width() - 16
            y = vg.y() + 16
        x = max(vg.x() + 4, min(x, vg.x() + vg.width() - self.width() - 4))
        y = max(vg.y() + 4, min(y, vg.y() + vg.height() - self.height() - 4))
        self.move(x, y)
        self.show()
        self.raise_()

    def set_time(self, text):
        self._time_lbl.setText(text)

    def set_paused(self, paused):
        self._pause_btn.setText('继续' if paused else '暂停')

    def _on_pause(self):
        self._panel._on_pause()

    def _on_stop(self):
        self._panel._on_stop()      # 收尾完成后 _on_stop 会把面板恢复出来

    def _on_back(self):
        self._panel._mini_restore()


# ═══════════════════════════════════════════════════
#  录制区域边框
# ═══════════════════════════════════════════════════

class RecordingBorder(QWidget):
    def __init__(self):
        super().__init__()
        self._state = 'preview'
        self._paused_flag = False
        self._blink_phase = 0
        self._rect_w = 0
        self._rect_h = 0
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            | Qt.BypassWindowManagerHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setStyleSheet("background: transparent;")
        self.hide()
        self._blink_timer = QTimer(self)
        self._blink_timer.timeout.connect(self._on_blink)
        self._blink_timer.setInterval(500)
        # B3: 置顶频率降为 5s，且不再带 SWP_SHOWWINDOW 避免闪屏
        self._topmost_timer = QTimer(self)
        self._topmost_timer.timeout.connect(self._force_topmost)
        self._topmost_timer.setInterval(5000)

    def showEvent(self, e):
        super().showEvent(e)
        self._make_click_through()
        self._force_topmost()
        self._topmost_timer.start()

    def _make_click_through(self):
        """原生层显式鼠标穿透：边框线/角标像素不再接管光标查询，
        鼠标滑过边框时不会闪动（Qt 的 WA_TransparentForMouseEvents 仅作用于 Qt 内部）"""
        try:
            import win32gui
            import win32con
            hwnd = int(self.winId())
            ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE,
                                   ex | win32con.WS_EX_TRANSPARENT | 0x08000000)
        except Exception:
            pass

    def hideEvent(self, e):
        super().hideEvent(e)
        self._topmost_timer.stop()

    def _force_topmost(self):
        try:
            import win32gui, win32con
            hwnd = int(self.winId())
            if hwnd:
                win32gui.SetWindowPos(
                    hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                    | win32con.SWP_NOACTIVATE
                )
        except Exception:
            pass

    def _on_blink(self):
        self._blink_phase = (self._blink_phase + 1) % 10
        self.update()

    def paintEvent(self, e):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        recording = self._state == 'recording'
        # 三态三色：预览=玫红（未开始）、录制=正红、暂停=琥珀（一眼区分“在录但没写帧”）
        if recording and self._paused_flag:
            main = QColor(245, 158, 11)
        elif recording:
            main = QColor(220, 38, 38)
        else:
            main = QColor(244, 63, 94)
        # 1) 外发光：三层由粗到细、由淡到浓的同心描边，柔和衰减不出现硬边
        painter.setBrush(Qt.NoBrush)
        for grow, alpha in ((13, 14), (8, 34), (4, 70)):
            pen = QPen(QColor(main.red(), main.green(), main.blue(), alpha), grow, Qt.SolidLine)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            painter.drawRect(4, 4, w - 8, h - 8)
        # 2) 主描边改为 3px 实线：轻盈利落，替代旧版 7px 虚线（“木讷感”的来源）
        pen = QPen(main, 3, Qt.SolidLine)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawRect(4, 4, w - 8, h - 8)
        # 3) 四角圆头角标：白色软衬 + 彩色线，比旧版 16px 粗白角标精致
        cl = 30
        wp = QPen(QColor(255, 255, 255, 90), 9, Qt.SolidLine)
        wp.setCapStyle(Qt.RoundCap)
        painter.setPen(wp)
        self._draw_corners(painter, cl)
        cp = QPen(main, 4.5, Qt.SolidLine)
        cp.setCapStyle(Qt.RoundCap)
        painter.setPen(cp)
        self._draw_corners(painter, cl)
        # 4) 胶囊标签：预览时附带目标尺寸，录制时显示状态
        if recording:
            # 暂停用双竖线而不是 ⏸：后者在 Segoe UI Emoji 里会渲染成异色方块，与胶囊底色撞色
            label = '‖ 已暂停' if self._paused_flag else '● REC'
        else:
            label = f'● 预览　{self._rect_w} × {self._rect_h}' if self._rect_w else '● 预览'
        font = QFont('Microsoft YaHei', 11, QFont.Bold)
        painter.setFont(font)
        tw = painter.fontMetrics().width(label) + 30
        th = 28
        # 胶囊：深色描边压住背景杂色，浅阴影让标签从画面里“浮”起来
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 46))
        painter.drawRoundedRect(QRectF(10, 12, tw, th), th / 2.0, th / 2.0)
        painter.setPen(QPen(QColor(0, 0, 0, 70), 1))
        painter.setBrush(main)
        painter.drawRoundedRect(QRectF(10, 10, tw, th), th / 2.0, th / 2.0)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(QRectF(10, 10, tw, th), Qt.AlignCenter, label)

    def _draw_corners(self, painter, cl):
        w, h = self.width(), self.height()
        painter.drawLine(0, cl, 0, 0); painter.drawLine(0, 0, cl, 0)
        painter.drawLine(w - cl, 0, w, 0); painter.drawLine(w, 0, w, cl)
        painter.drawLine(0, h - cl, 0, h); painter.drawLine(0, h, cl, h)
        painter.drawLine(w - cl, h, w, h); painter.drawLine(w, h - cl, w, h)

    def set_state(self, state, paused=False):
        self._state = state
        self._paused_flag = paused
        # 不再启动脉冲定时器，边框保持稳定
        self._blink_timer.stop()
        self._blink_phase = 0
        self.update()

    def set_recording_rect(self, x, y, w, h):
        # 记录目标尺寸，供预览胶囊标签显示（宽 × 高）
        self._rect_w, self._rect_h = int(w), int(h)
        self.setGeometry(x - 4, y - 4, w + 8, h + 8)


# ═══════════════════════════════════════════════════
#  D1: notify-finished
# ═══════════════════════════════════════════════════

def _notify_finished(api_port, filename, click_count):
    def _run():
        try:
            req = urllib.request.Request(
                f'http://127.0.0.1:{api_port}/api/record/notify-finished',
                data=json.dumps({'file': filename, 'clicks': click_count}).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            urllib.request.urlopen(req, timeout=2).read()
        except Exception as e:
            print(f"[notify] 失败: {e}")
    threading.Thread(target=_run, daemon=True).start()


# ═══════════════════════════════════════════════════
#  控制面板
# ═══════════════════════════════════════════════════

def run_control_panel(fps, save_dir, api_port, quality=QUALITY_DEFAULT):
    _install_crash_guard()
    app = QApplication.instance() or QApplication(sys.argv)

    recorder = ScreenRecorder(fps=fps, save_dir=save_dir, quality=quality)
    state = {'mouse_listener': None}
    border = RecordingBorder()
    overlay = ClickOverlay(recorder)
    _exclude_from_capture(border)
    _exclude_from_capture(overlay)
    # 录制标注：全屏绘制层 + 工具条（UI 同面板一致不进录屏/截图；标注由帧渲染进视频）
    ann_overlay = AnnotationOverlay(recorder)
    ann_toolbar = AnnotationToolbar(recorder, ann_overlay)
    _exclude_from_capture(ann_overlay)
    _exclude_from_capture(ann_toolbar)

    class ControlPanel(QWidget):
        def __init__(self):
            super().__init__()
            self._drag_pos = None
            self._mode = 'full'
            self._target_win = None
            self._pick_web = False
            self._pick_note = ''
            init_status = _read_status()
            self._last_wake_ts = init_status.get('panel_wake_ts', 0)
            self._last_stop_req = init_status.get('stop_request_ts', 0)
            self._last_pending_fps = init_status.get('pending_fps')
            self._last_pending_dir = init_status.get('pending_save_dir')
            self._last_pending_quality = init_status.get('pending_quality')
            self._deferred_fps = None   # 录制中切档：先挂起，停止后应用
            self._deferred_quality = None

            self.setWindowTitle('屏幕录制控制台')
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
            self.setObjectName('panelRoot')
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setFixedSize(372, 224)
            self.setStyleSheet("""
                QWidget { color:#1e293b; font-family:'Microsoft YaHei'; }
                QLabel { background:transparent; }
                QPushButton { border:none; border-radius:7px; padding:0 10px;
                              font-size:12px; font-weight:600; font-family:'Microsoft YaHei'; }
                /* 标题栏：帧率徽章 + 隐藏 / 退出 */
                QLabel#title { font-size:12px; color:#64748b; font-weight:600; }
                QLabel#chip { background:#eef1fe; color:#4f6ef7; font-size:11px;
                              font-weight:600; border-radius:8px; padding:1px 7px; }
                QPushButton#btnHide { background:transparent; color:#94a3b8; font-size:13px; padding:0; }
                QPushButton#btnHide:hover { background:#eef1f6; color:#1e293b; }
                /* 读数区：状态 + 计时 */
                QFrame#readout { background:#f5f7fa; border:1px solid #e6ebf3;
                                 border-radius:10px; }
                QLabel#status { font-size:13px; font-weight:600; color:#16a34a; }
                QLabel#timer { font-size:23px; font-weight:600; color:#1e293b;
                               font-family:Consolas,'Courier New',monospace; }
                /* 模式分段控件：选中填主色、未选中透明（只换颜色不换文字） */
                QFrame#seg { background:#f1f4fa; border:1px solid #e6ebf3;
                             border-radius:9px; }
                QPushButton#btnModeOn { background:#4f6ef7; color:#fff; }
                QPushButton#btnModeOn:hover { background:#3b5de7; }
                QPushButton#btnModeOff { background:transparent; color:#64748b; }
                QPushButton#btnModeOff:hover { background:#e6ebf6; color:#4f6ef7; }
                QPushButton#btnModeOn:disabled, QPushButton#btnModeOff:disabled {
                    background:transparent; color:#cbd5e1; }
                /* 主操作：开始/停止在左（红→绿）、暂停在右（琥珀，禁用时隐入底色） */
                QPushButton#btnStart { background:#dc2626; color:#fff; font-size:13px; font-weight:700;
                                       border-radius:8px; }
                QPushButton#btnStart:hover { background:#b91c1c; }
                QPushButton#btnStop { background:#059669; color:#fff; font-size:13px; font-weight:700;
                                      border-radius:8px; }
                QPushButton#btnStop:hover { background:#047857; }
                QPushButton#btnPause { background:#d97706; color:#fff; border-radius:8px; }
                QPushButton#btnPause:hover { background:#b45309; }
                QPushButton#btnPause:disabled { background:#eef1f6; color:#cbd5e1; }
                QLabel#foot { font-size:11px; color:#94a3b8; }
            """)

            layout = QVBoxLayout()
            layout.setContentsMargins(14, 12, 14, 12)
            layout.setSpacing(9)

            tr = QHBoxLayout()
            tr.setSpacing(6)
            t = QLabel('🎬 录制控制台')
            t.setObjectName('title')
            tr.addWidget(t)
            self._fps_chip = QLabel(f"{int(getattr(recorder, '_fps', fps))} fps")
            self._fps_chip.setObjectName('chip')
            tr.addWidget(self._fps_chip)
            # 画质档标识（清晰度由编码 CRF 决定，与帧率无关，这里如实展示当前档）
            self._q_chip = QLabel(QUALITY_PRESETS[recorder.get_quality()]['label'])
            self._q_chip.setObjectName('chip')
            self._q_chip.setToolTip('画质档：决定编码质量（清晰度），Web 录制页可切换')
            tr.addWidget(self._q_chip)
            tr.addStretch()
            hide_btn = QPushButton('—')
            hide_btn.setObjectName('btnHide')
            hide_btn.setFixedSize(26, 22)
            hide_btn.setCursor(QCursor(Qt.PointingHandCursor))
            hide_btn.setToolTip('隐藏面板（进程保持常驻，Web 端可再次唤醒）')
            hide_btn.clicked.connect(self._on_hide)
            tr.addWidget(hide_btn)
            close_btn = QPushButton('×')
            close_btn.setObjectName('btnHide')
            close_btn.setFixedSize(26, 22)
            close_btn.setCursor(QCursor(Qt.PointingHandCursor))
            close_btn.setToolTip('完全退出面板进程')
            close_btn.clicked.connect(self._on_close)
            tr.addWidget(close_btn)
            layout.addLayout(tr)

            readout = QFrame()
            readout.setObjectName('readout')
            sr = QHBoxLayout(readout)
            sr.setContentsMargins(12, 8, 12, 8)
            sr.setSpacing(8)
            self._status_lbl = QLabel('● 就绪')
            self._status_lbl.setObjectName('status')
            self._status_lbl.setStyleSheet('color:#10b981;')
            sr.addWidget(self._status_lbl)
            sr.addStretch()
            self._timer_lbl = QLabel('00:00')
            self._timer_lbl.setObjectName('timer')
            sr.addWidget(self._timer_lbl)
            layout.addWidget(readout)

            seg = QFrame()
            seg.setObjectName('seg')
            mr = QHBoxLayout(seg)
            mr.setContentsMargins(3, 3, 3, 3)
            mr.setSpacing(2)
            self._full_btn = QPushButton('📺 全屏')
            self._full_btn.setObjectName('btnModeOn')
            self._full_btn.setToolTip('录制整个屏幕')
            self._full_btn.setFixedHeight(28)
            self._full_btn.setCursor(QCursor(Qt.PointingHandCursor))
            self._full_btn.clicked.connect(self._set_full_mode)
            mr.addWidget(self._full_btn, 1)
            self._win_btn = QPushButton('📋 选择窗口')
            self._win_btn.setObjectName('btnModeOff')
            self._win_btn.setToolTip('弹出窗口列表，点选要录制的窗口')
            self._win_btn.setFixedHeight(28)
            self._win_btn.setCursor(QCursor(Qt.PointingHandCursor))
            self._win_btn.clicked.connect(self._pick_from_list)
            self._win_btn.setVisible(False)  # 按需求仅保留“活动窗口”入口
            mr.addWidget(self._win_btn, 1)
            self._active_btn = QPushButton('🎯 活动窗口')
            self._active_btn.setObjectName('btnModeOff')
            self._active_btn.setToolTip('点后面板隐藏并倒计时3秒，自动录制当前最前面的窗口')
            self._active_btn.setFixedHeight(28)
            self._active_btn.setCursor(QCursor(Qt.PointingHandCursor))
            self._active_btn.clicked.connect(self._pick_active)
            mr.addWidget(self._active_btn, 1)
            self._web_btn = QPushButton('🌐 网页区域')
            self._web_btn.setObjectName('btnModeOff')
            self._web_btn.setToolTip('只录窗口内的网页内容，自动排除浏览器标签栏/地址栏（Firefox/Chrome/Edge）')
            self._web_btn.setFixedHeight(28)
            self._web_btn.setCursor(QCursor(Qt.PointingHandCursor))
            self._web_btn.clicked.connect(self._pick_web_area)
            mr.addWidget(self._web_btn, 1)
            layout.addWidget(seg)

            ar = QHBoxLayout()
            ar.setSpacing(8)
            self._start_btn = QPushButton('● 开始录制')
            self._start_btn.setObjectName('btnStart')
            self._start_btn.setToolTip('开始录制；录制中点击则停止并保存文件')
            self._start_btn.setFixedHeight(38)
            self._start_btn.setCursor(QCursor(Qt.PointingHandCursor))
            self._start_btn.clicked.connect(self._on_start)
            ar.addWidget(self._start_btn, 1)
            self._pause_btn = QPushButton('暂停')
            self._pause_btn.setObjectName('btnPause')
            self._pause_btn.setToolTip('暂停/继续：冻结画面但不停止，点“继续”后接着录（暂停时段不写入视频）')
            self._pause_btn.setFixedSize(104, 38)
            self._pause_btn.setCursor(QCursor(Qt.PointingHandCursor))
            self._pause_btn.clicked.connect(self._on_pause)
            self._pause_btn.setEnabled(False)
            ar.addWidget(self._pause_btn)
            layout.addLayout(ar)

            foot = QLabel('拖动面板可移动 · 隐藏后进程常驻')
            foot.setObjectName('foot')
            layout.addWidget(foot)

            self.setLayout(layout)

            # A2: 恢复上次位置
            saved_pos = init_status.get('panel_pos')
            vg = QApplication.primaryScreen().virtualGeometry()
            if saved_pos and isinstance(saved_pos, dict):
                try:
                    px = int(saved_pos.get('x', vg.x() + vg.width() - 392))
                    py = int(saved_pos.get('y', vg.y() + 20))
                    px = max(vg.x(), min(px, vg.x() + vg.width() - self.width()))
                    py = max(vg.y(), min(py, vg.y() + vg.height() - self.height()))
                    self.move(px, py)
                except Exception:
                    self.move(vg.x() + vg.width() - 392, vg.y() + 20)
            else:
                self.move(vg.x() + vg.width() - 392, vg.y() + 20)

            self._ui_timer = QTimer(self)
            self._ui_timer.timeout.connect(self._update_timer)
            # 准备中轮询：等抓帧时间轴真正建立后再把计时器从 00:00 放行
            self._start_wait_timer = QTimer(self)
            self._start_wait_timer.timeout.connect(self._poll_started)
            self._start_wait_polls = 0
            self._rec_start_time = 0
            self._paused_accum = 0
            self._pause_start = 0

            # 迷你状态条：录制中点“—”隐藏面板时替代面板常驻（REC+计时+暂停/停止/恢复）
            self._mini = _MiniRecBar(self)

            self._click_timer = QTimer(self)
            self._click_timer.timeout.connect(self._update_click_display)

            # A1/A3/F5: 状态文件轮询
            self._status_poll = QTimer(self)
            self._status_poll.timeout.connect(self._poll_status_file)
            self._status_poll.start(500)

            _write_status(panel_visible=True, panel_pid=os.getpid(),
                          panel_script_mtime=_script_mtime())
            self._update_status()

        def paintEvent(self, e):
            # 顶层半透明 + 自绘圆角卡片：浅色玻璃底 + 1px 描边（白卡 + 灰蓝边，与全站卡片一致）
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QPen(QColor(226, 232, 240), 1))
            p.setBrush(QColor(255, 255, 255, 248))
            p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 14, 14)

        def mousePressEvent(self, e):
            if e.button() == Qt.LeftButton:
                self._drag_pos = QPoint(e.globalPos() - self.frameGeometry().topLeft())

        def mouseMoveEvent(self, e):
            if self._drag_pos and e.buttons() & Qt.LeftButton:
                self.move(e.globalPos() - self._drag_pos)

        def mouseReleaseEvent(self, e):
            if self._drag_pos:
                self._drag_pos = None
                p = self.pos()
                _write_status(panel_pos={'x': p.x(), 'y': p.y()})

        def moveEvent(self, e):
            super().moveEvent(e)
            ann_toolbar.move_beside(self)

        def _poll_status_file(self):
            s = _read_status()
            sr = s.get('stop_request_ts', 0)
            if sr and sr != self._last_stop_req:
                self._last_stop_req = sr
                if recorder.is_running():
                    # 网页上点“停止录制”：走和面板按钮完全相同的收尾流程（writer 正常落盘）
                    self._on_stop()
                    self._flash_status('● 已通过网页停止并保存', '#10b981')
            wake = s.get('panel_wake_ts', 0)
            if wake and wake != self._last_wake_ts:
                self._last_wake_ts = wake
                self._mini.hide()     # 唤醒面板时收起迷你条，避免两个状态框同时出现
                if not self.isVisible():
                    self.show()
                    self.raise_()
                    self.activateWindow()
                if recorder.is_running():    # 录制中唤醒：恢复标注工具条
                    ann_toolbar.show_beside(self)
                _write_status(panel_visible=True, panel_pid=os.getpid(),
                              panel_script_mtime=_script_mtime())
            pf = s.get('pending_fps')
            if pf and pf != self._last_pending_fps:
                self._last_pending_fps = pf
                if not recorder.is_running():
                    try:
                        recorder.set_fps(int(pf))
                        self._fps_chip.setText(f'{int(pf)} fps')
                        self._flash_status(f'● 帧率已切换为 {int(pf)} fps', '#10b981')
                    except Exception:
                        pass
                else:
                    # 录制中改档无法即时生效（正在写的文件帧率已固定），先挂起到停止后应用。
                    # 必须给出提示，否则用户会以为已切档、录出来却毫无差别
                    self._deferred_fps = int(pf)
                    self._flash_status(f'● 录制中不可切换，停止后按 {int(pf)} fps', '#f59e0b')
            pd = s.get('pending_save_dir')
            if pd and pd != self._last_pending_dir:
                self._last_pending_dir = pd
                if not recorder.is_running() and os.path.isabs(pd):
                    try:
                        os.makedirs(pd, exist_ok=True)
                        recorder.set_save_dir(pd)
                        self._flash_status('● 保存目录已更新', '#10b981')
                    except Exception:
                        pass
            pq = s.get('pending_quality')
            if pq and pq != self._last_pending_quality:
                self._last_pending_quality = pq
                label = QUALITY_PRESETS[_snap_quality(pq)]['label']
                if not recorder.is_running():
                    recorder.set_quality(pq)
                    self._q_chip.setText(label)
                    self._flash_status(f'● 画质已切换为{label}', '#10b981')
                else:
                    # 正在写的文件编码参数已固定，停止后生效
                    self._deferred_quality = pq
                    self._flash_status(f'● 录制中不可切换，停止后按{label}画质', '#f59e0b')

        def _flash_status(self, text, color):
            self._status_lbl.setText(text)
            self._status_lbl.setStyleSheet(f'color:{color};')
            QTimer.singleShot(2000, self._update_status)

        def _show_preview_border(self):
            try:
                _ensure_heavy_deps()
                if self._mode == 'window' and self._target_win:
                    _, _, x, y, w, h = self._target_win
                else:
                    with mss.mss() as sct:
                        mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                        x, y, w, h = mon['left'], mon['top'], mon['width'], mon['height']
                if w > 0 and h > 0:
                    border.set_state('preview')
                    border.set_recording_rect(x, y, w, h)
                    border.show()
                    border.raise_()
            except Exception as e:
                print(f"[预览边框] 错误: {e}")

        def _set_full_mode(self):
            if recorder.is_running():
                return
            self._mode = 'full'
            self._target_win = None
            self._refresh_mode_buttons()
            self._status_lbl.setText('● 就绪（全屏）')
            self._status_lbl.setStyleSheet('color:#10b981;')

        def _pick_active(self):
            """识别最顶层活动窗口并红框圈出，顶部操作栏确认后 3 秒倒数开始录制。"""
            self._pick_entry(web=False)

        def _pick_web_area(self):
            """网页区域模式：只录最顶层窗口内的网页内容，自动排除浏览器外壳。"""
            self._pick_entry(web=True)

        def _pick_entry(self, web=False):
            if recorder.is_running():
                return
            try:
                self._pick_active_impl(web=web)
            except Exception as e:
                print(f'[选择窗口] 异常: {e}')
                self._pick_hide_controls()
                border.hide()
                self.show()
                QApplication.processEvents()
                self._status_lbl.setText('● 活动窗口选择失败')
                self._status_lbl.setStyleSheet('color:#ef4444;')

        def _pick_active_impl(self, web=False):
            self._pick_web = web
            self.hide()
            QApplication.processEvents()
            scr = QApplication.primaryScreen().geometry()
            # 顶部一体式操作栏：信息 + 确认 + 取消聚合为一条，居中悬浮于屏幕顶部
            # （原先分散在屏幕右上角的按钮常被输入法条/悬浮条等置顶元素遮挡）
            self._pick_bar, self._pick_hint, self._pick_ok, self._pick_no = \
                _build_pick_bar(self._pick_confirm, self._pick_cancel)
            self._pick_bar.show()
            self._pick_center_bar(scr)
            _exclude_from_capture(self._pick_bar)
            # 置顶保活：防止被后出现的其他置顶小窗（输入法条/悬浮条）插队覆盖
            self._pick_bar_timer = QTimer(self)
            self._pick_bar_timer.timeout.connect(self._pick_keep_top)
            self._pick_bar_timer.start(1500)
            self._pick_last_rect = None
            self._pick_cur = None
            self._pick_last_hwnd = 0
            self._pick_deadline = time.time() + 60
            self._pick_timer = QTimer(self)
            self._pick_timer.timeout.connect(self._pick_tick)
            self._pick_timer.start(100)

        def _pick_center_bar(self, scr=None):
            """操作栏按当前内容宽度水平居中于屏幕顶部"""
            bar = getattr(self, '_pick_bar', None)
            if not bar:
                return
            if scr is None:
                scr = QApplication.primaryScreen().geometry()
            bar.adjustSize()
            bar.move(max(8, (scr.width() - bar.width()) // 2), 36)

        def _pick_keep_top(self):
            """置顶保活：周期性强制操作栏到最顶层，防被其他置顶窗插队"""
            bar = getattr(self, '_pick_bar', None)
            if not bar or not bar.isVisible():
                return
            try:
                import win32gui, win32con
                hwnd = int(bar.winId())
                if hwnd:
                    win32gui.SetWindowPos(
                        hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                        | win32con.SWP_NOACTIVATE)
            except Exception:
                pass

        def _set_pick_text(self, text):
            """更新操作栏信息文本（超长标题中间省略）并重新居中"""
            lbl = getattr(self, '_pick_hint', None)
            if not lbl:
                return
            fm = lbl.fontMetrics()
            lbl.setText(fm.elidedText(text, Qt.ElideMiddle, 720))
            self._pick_center_bar()

        def _pick_tick(self):
            import win32api
            import win32con
            if time.time() > self._pick_deadline:
                self._pick_cancel()
                return
            if win32api.GetAsyncKeyState(win32con.VK_ESCAPE) & 0x8000:
                self._pick_cancel()
                return
            wins = _enum_windows()
            if not wins:
                return
            # 硬闸1：绝不把红框边框自身当作目标（切断反馈回路）
            try:
                own_border_hwnd = int(border.winId())
            except Exception:
                own_border_hwnd = 0
            top = None
            for cand in wins:
                if cand[0] == own_border_hwnd:
                    continue
                top = cand
                break
            if top is None:
                return
            # 硬闸2：同一窗口句柄只采纳第一次的矩形，防止矩形被逐 tick 累积放大
            if top[0] == getattr(self, '_pick_last_hwnd', 0):
                if self._pick_cur:
                    self._set_pick_text(f'{self._pick_note}：{top[1]}　'
                                        f'{self._pick_cur[4]}×{self._pick_cur[5]}')
                return
            self._pick_last_hwnd = top[0]
            rect = (top[2], top[3], top[4], top[5])
            note = '即将录制'
            if getattr(self, '_pick_web', False):
                # 网页区域模式：UIA 检测 Document 边界；检测不到时回退整窗
                wr = _get_web_content_rect(top[0], frame_rect=rect)
                if wr:
                    rect = wr
                    note = '即将录制网页区域'
                else:
                    note = '未检测到网页内容，将录制整个窗口'
            self._pick_note = note
            self._pick_cur = (top[0], top[1], rect[0], rect[1], rect[2], rect[3])
            self._pick_last_rect = rect
            border.set_state('preview')
            border.set_recording_rect(*rect)
            border.show()
            border.raise_()
            self._set_pick_text(f'{note}：{top[1]}　{rect[2]}×{rect[3]}')

        def _pick_confirm(self):
            self._pick_timer.stop()
            target = getattr(self, '_pick_cur', None)
            self._pick_hide_controls()
            if not target:
                self._pick_cancel()
                return
            self._pick_target = target
            border.set_state('preview')
            border.set_recording_rect(target[2], target[3], target[4], target[5])
            border.show()
            border.raise_()
            scr = QApplication.primaryScreen().geometry()
            self._cd = QLabel('3', None,
                              Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
            self._cd.setAlignment(Qt.AlignCenter)
            self._cd.setStyleSheet("color:#f8fafc;font-size:64px;font-weight:bold;"
                                   "font-family:Consolas,'Courier New',monospace;"
                                   "background:rgba(15,23,42,214);"
                                   "border:1px solid rgba(148,163,184,.28);border-radius:20px;")
            self._cd.setFixedSize(140, 140)
            self._cd.move((scr.width() - 140) // 2, (scr.height() - 140) // 2)
            self._cd.show()
            _exclude_from_capture(self._cd)
            self._cd_n = 3
            QTimer.singleShot(1000, self._cd_step)

        def _pick_hide_controls(self):
            t = getattr(self, '_pick_bar_timer', None)
            if t:
                t.stop()
                t.deleteLater()
                self._pick_bar_timer = None
            bar = getattr(self, '_pick_bar', None)
            if bar:
                bar.hide()
                bar.deleteLater()
                self._pick_bar = None
            # 子控件随操作栏一并销毁，这里仅清空引用（tick/confirm 中的判空逻辑保持兼容）
            for attr in ('_pick_hint', '_pick_ok', '_pick_no'):
                setattr(self, attr, None)

        def _fg_rect(self):
            import win32gui
            hwnd = win32gui.GetForegroundWindow()
            try:
                import win32process
                _, pid = win32gui.GetWindowThreadProcessId(hwnd)
                if pid == os.getpid():
                    return None
            except Exception:
                return None
            fr = _get_window_frame_rect(hwnd)
            if not fr or fr[2] <= 50 or fr[3] <= 50:
                return None
            x, y, w, h = fr
            return (hwnd, win32gui.GetWindowText(hwnd) or '活动窗口', x, y, w, h)

        def _cd_step(self):
            self._cd_n -= 1
            if self._cd_n <= 0:
                if getattr(self, '_cd', None):
                    self._cd.hide()
                    self._cd.deleteLater()
                    self._cd = None
                border.hide()
                self.show()
                QApplication.processEvents()
                self._target_win = self._pick_target
                self._mode = 'web' if getattr(self, '_pick_web', False) else 'window'
                self._refresh_mode_buttons()
                self._on_start()
                return
            if getattr(self, '_cd', None):
                self._cd.setText(str(self._cd_n))
            QTimer.singleShot(1000, self._cd_step)

        def _pick_cancel(self):
            if getattr(self, '_pick_timer', None):
                self._pick_timer.stop()
            self._pick_hide_controls()
            if getattr(self, '_cd', None):
                self._cd.hide()
                self._cd.deleteLater()
                self._cd = None
            border.hide()
            self.show()
            QApplication.processEvents()
            self._status_lbl.setText('● 已取消选择')
            self._status_lbl.setStyleSheet('color:#9ca3af;')

        def _pick_from_list(self):
            """弹出窗口列表，点选即录（无遮罩不闪烁）"""
            if recorder.is_running():
                return
            wins = _enum_windows()
            if not wins:
                self._status_lbl.setText('● 没有可选窗口')
                self._status_lbl.setStyleSheet('color:#ef4444;')
                return
            from PyQt5.QtWidgets import (QDialog, QListWidget, QVBoxLayout,
                                          QDialogButtonBox)
            dlg = QDialog(self)
            dlg.setWindowTitle('选择要录制的窗口')
            dlg.resize(420, 360)
            vl = QVBoxLayout(dlg)
            lw = QListWidget(dlg)
            for (hwnd, title, x, y, w, h) in wins:
                lw.addItem(f'{title}　({w}×{h})')
            vl.addWidget(lw)
            bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            bb.accepted.connect(dlg.accept)
            bb.rejected.connect(dlg.reject)
            vl.addWidget(bb)
            lw.itemDoubleClicked.connect(dlg.accept)
            if dlg.exec_() == QDialog.Accepted and lw.currentRow() >= 0:
                hwnd, title, x, y, w, h = wins[lw.currentRow()]
                self._target_win = (hwnd, title, x, y, w, h)
                self._mode = 'window'
                self._refresh_mode_buttons()
                self._on_start()

        def _refresh_mode_buttons(self):
            # 按钮文字一律恒定不变（含选中态）：四个模式按钮的文字永远保持原样，
            # “当前选中哪个模式”只用颜色高亮（蓝色）表示，不做任何文字替换
            self._full_btn.setText('📺 全屏')
            self._active_btn.setText('🎯 活动窗口')
            self._web_btn.setText('🌐 网页区域')
            self._win_btn.setText('📋 选择窗口')
            for b in (self._full_btn, self._win_btn, self._active_btn, self._web_btn):
                b.setObjectName('btnModeOff')
            if self._target_win and self._mode == 'window':
                # 活动窗口模式已选定目标：高亮对应按钮（仅颜色，文字不变）
                self._active_btn.setObjectName('btnModeOn')
            elif self._target_win and self._mode == 'web':
                # 网页区域模式已选定目标：高亮对应按钮（仅颜色，文字不变）
                self._web_btn.setObjectName('btnModeOn')
            else:
                # 默认（全屏）：高亮“📺 全屏”
                self._full_btn.setObjectName('btnModeOn')
            for b in (self._full_btn, self._win_btn, self._active_btn, self._web_btn):
                b.setStyle(b.style())

        def _on_start(self):
            if recorder.is_running():
                self._on_stop()
                return
            if self._mode in ('window', 'web') and self._target_win:
                hwnd, title, x, y, w, h = self._target_win
                ok = recorder.start_window(hwnd, title, x, y, w, h, web=(self._mode == 'web'))
            else:
                ok = recorder.start_fullscreen()

            if ok:
                # 准备中：抓屏后端/编码器还没就绪，不能先把 00:00 计时跑起来
                self._rec_start_time = 0
                self._paused_accum = 0
                self._pause_start = 0
                self._timer_lbl.setText('00:00')
                self._ui_timer.start(500)
                self._click_timer.start(500)
                self._start_wait_polls = 0
                self._start_wait_timer.start(50)
                self._update_status()
            else:
                self._status_lbl.setText('启动失败')
                self._status_lbl.setStyleSheet('color:#ef4444;')

        def _poll_started(self):
            """准备完成：立刻把计时器对齐视频第一帧，再亮录制边框/点击层/标注工具"""
            if not recorder.is_running():
                self._start_wait_timer.stop()
                self._rec_start_time = 0
                self._update_status()
                return
            start_time = recorder.get_start_time()
            if not start_time:
                # 抓屏/编码初始化异常时事件不会置位：超时就不再等，别让面板永远停在“准备中”
                self._start_wait_polls += 1
                if self._start_wait_polls < 200:
                    return
                self._start_wait_timer.stop()
                self._status_lbl.setText('● 启动超时')
                self._status_lbl.setStyleSheet('color:#ef4444;')
                return
            self._start_wait_timer.stop()
            self._rec_start_time = start_time
            self._timer_lbl.setText('00:00')
            rect = recorder.get_record_rect()
            if rect:
                border.set_recording_rect(*rect)
                border.set_state('recording', paused=False)
                border.show()
                border.raise_()
            overlay.set_enabled(True)
            overlay.raise_()
            ann_toolbar.reset()
            ann_overlay.enable(True)
            if self.isVisible():
                ann_toolbar.show_beside(self)
            else:
                ann_toolbar.dismiss()
            if state['mouse_listener'] is None:
                state['mouse_listener'] = MouseClickListener(on_click=recorder.add_click)
                state['mouse_listener'].start()
            _write_status(recording=True, paused=False, click_count=0,
                          last_file='', start_time=start_time)
            self._update_status()

        def _on_pause(self):
            if not recorder.is_running() or not recorder.is_started():
                return
            if recorder.is_paused():
                recorder.resume()
                if self._pause_start:
                    self._paused_accum += time.time() - self._pause_start
                    self._pause_start = 0
                self._pause_btn.setText('暂停')
                border.set_state('recording', paused=False)
                overlay.set_enabled(True)
                overlay.raise_()
                self._mini.set_paused(False)
                _write_status(paused=False)
            else:
                recorder.pause()
                self._pause_start = time.time()
                self._pause_btn.setText('继续')
                border.set_state('recording', paused=True)
                overlay.set_enabled(False)
                self._mini.set_paused(True)
                _write_status(paused=True)
            self._update_status()

        def _on_stop(self):
            filepath, click_count = recorder.stop()
            self._start_wait_timer.stop()
            self._ui_timer.stop()
            self._click_timer.stop()
            self._rec_start_time = 0
            self._timer_lbl.setText('00:00')
            self._pause_btn.setText('暂停')
            overlay.set_enabled(False)
            ann_toolbar.dismiss()
            ann_overlay.enable(False)
            recorder.clear_annotations()
            border.hide()  # 停止后不残留预览红框
            if state['mouse_listener']:
                state['mouse_listener'].stop()
                state['mouse_listener'] = None
            self._update_status()
            if getattr(self, '_deferred_fps', None):
                # 录制中挂起的切档在此生效（否则用户会以为切了但实际没切）
                try:
                    recorder.set_fps(self._deferred_fps)
                    self._fps_chip.setText(f'{self._deferred_fps} fps')
                    self._flash_status(f'● 已按 {self._deferred_fps} fps 录制下一段', '#10b981')
                except Exception:
                    pass
                self._deferred_fps = None
            if getattr(self, '_deferred_quality', None):
                try:
                    recorder.set_quality(self._deferred_quality)
                    self._q_chip.setText(QUALITY_PRESETS[_snap_quality(self._deferred_quality)]['label'])
                    self._flash_status('● 已按新画质录制下一段', '#10b981')
                except Exception:
                    pass
                self._deferred_quality = None
            if filepath:
                fn = os.path.basename(filepath)
                _write_status(recording=False, paused=False, click_count=click_count,
                              last_finished_at=time.time(), last_file=fn)
                print(f"[录制] 已保存: {filepath} (点击: {click_count})")
                _notify_finished(api_port, fn, click_count)
            # 停止来源是迷你条/面板隐藏中被网页停止：收条并恢复完整面板
            if self._mini.isVisible():
                self._mini.hide()
                self.show()
                self.raise_()
                self._update_status()
                _write_status(panel_visible=True, panel_pid=os.getpid(),
                              panel_script_mtime=_script_mtime())

        def _update_status(self):
            # 帧率徽章跟随“真正落盘的帧率”：抓屏跟不上设定值时如实显示（如 1080p 全屏选 20 实际 ~18）
            try:
                self._fps_chip.setText(f"{int(recorder.get_effective_fps())} fps")
            except Exception:
                pass
            if recorder.is_running():
                started = recorder.is_started()
                if not started:
                    self._status_lbl.setText('◐ 准备中...')
                    self._status_lbl.setStyleSheet('color:#f59e0b;')
                elif recorder.is_paused():
                    self._status_lbl.setText('‖ 已暂停')
                    self._status_lbl.setStyleSheet('color:#f59e0b;')
                else:
                    self._status_lbl.setText('● 录制中')
                    self._status_lbl.setStyleSheet('color:#ef4444;')
                self._start_btn.setText('■ 停止录制')
                self._start_btn.setObjectName('btnStop')
                self._full_btn.setEnabled(False)
                self._win_btn.setEnabled(False)
                self._active_btn.setEnabled(False)
                self._web_btn.setEnabled(False)
                self._pause_btn.setEnabled(started)
            else:
                self._status_lbl.setText('● 就绪')
                self._status_lbl.setStyleSheet('color:#10b981;')
                self._start_btn.setText('● 开始录制')
                self._start_btn.setObjectName('btnStart')
                self._full_btn.setEnabled(True)
                self._win_btn.setEnabled(True)
                self._active_btn.setEnabled(True)
                self._web_btn.setEnabled(True)
                self._pause_btn.setEnabled(False)
            self._start_btn.setStyle(self._start_btn.style())

        def _update_timer(self):
            if not self._rec_start_time:
                return
            now = time.time()
            paused = self._paused_accum + (now - self._pause_start if self._pause_start else 0)
            elapsed = int(now - self._rec_start_time - paused)
            if elapsed < 0:
                elapsed = 0
            m, s = elapsed // 60, elapsed % 60
            self._timer_lbl.setText(f'{m:02d}:{s:02d}')
            if self._mini.isVisible():
                self._mini.set_time(self._timer_lbl.text())

        def _update_click_display(self):
            # 仅同步点击数到状态文件（UI 不再展示点击计数）
            if recorder.is_running():
                _write_status(click_count=recorder._total_clicks)

        def _on_hide(self):
            """A1: 隐藏面板但保留进程，等待 Web 唤醒（录制继续）
            隐藏后统一收成迷你状态条：录制中带 REC + 计时 + 暂停/停止；
            就绪态只留「恢复面板」——任何状态点“—”都能找回控制入口，不再凭空消失"""
            self.hide()
            ann_toolbar.dismiss()   # 工具条随面板收起；标注层保持显示（录制继续）
            self._mini.set_recording(recorder.is_running())
            self._mini.show_at(self.pos())
            _write_status(panel_visible=False)

        def _mini_restore(self):
            """迷你条“恢复面板”：收起迷你条，完整控制台回到原位置"""
            self._mini.hide()
            self.show()
            self.raise_()
            self.activateWindow()
            if recorder.is_running():
                ann_toolbar.show_beside(self)
            _write_status(panel_visible=True, panel_pid=os.getpid(),
                          panel_script_mtime=_script_mtime())

        def _on_close(self):
            """D2: 优雅退出：先停录制并等 writer.release()，再退出"""
            try:
                if recorder.is_running():
                    self._on_stop()
                if state['mouse_listener']:
                    state['mouse_listener'].stop()
                    state['mouse_listener'] = None
                overlay.set_enabled(False)
                ann_toolbar.dismiss()
                ann_overlay.enable(False)
                border.hide()
                self.hide()
                _write_status(panel_visible=False, panel_pid=0)
                QApplication.processEvents()
                app.quit()
            except Exception as e:
                print(f"[关闭] 异常: {e}")
            finally:
                def _force():
                    os._exit(0)
                t = threading.Timer(0.5, _force)
                t.daemon = True
                t.start()

    panel = ControlPanel()
    _exclude_from_capture(panel)
    _exclude_from_capture(panel._mini)   # 迷你状态条同样不进录屏/截图
    panel.show()
    _write_status(panel_visible=True, panel_pid=os.getpid(),
                  panel_script_mtime=_script_mtime())
    sys.exit(app.exec_())


# ═══════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fps', type=int, default=20)
    parser.add_argument('--save-dir', type=str, required=True)
    parser.add_argument('--api-port', type=int, default=5050)
    parser.add_argument('--quality', type=str, default=QUALITY_DEFAULT,
                        choices=list(QUALITY_PRESETS.keys()))
    args = parser.parse_args()
    run_control_panel(fps=args.fps, save_dir=args.save_dir, api_port=args.api_port,
                       quality=args.quality)


if __name__ == '__main__':
    main()
