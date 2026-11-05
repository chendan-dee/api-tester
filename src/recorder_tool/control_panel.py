# -*- coding: utf-8 -*-
"""屏幕录制控制面板 - 常驻浮动工具栏（优化版）

优化项：
- A1 面板常驻：— 按钮 hide 而非退出，Web 通过 panel_wake_ts 唤醒；× 才真正退出
- A2 记忆位置：拖动结束写入 panel_pos，下次启动恢复
- A3 FPS 热更新：pending_fps 由 Web 写入，非录制态即时生效
- B1 首帧不丢：writer 未就绪前用 deque 缓存前 N 帧，就绪后 flush
- B2 抓屏后端：优先 d3dshot（DXGI Desktop Duplication），无则回退 mss
- B3 边框置顶频率降为 2s；闪烁保持 500ms
- C1 桌面 ClickOverlay：30Hz 立即绘制点击脉冲（不等下一视频帧）
- C2 perf_counter 调度，落后过多时重置基线避免忙等
- C3 点击过期从左侧 popleft，遍历不拷贝
- C4 窗口 rect 每 5 帧才查询一次
- C6 窗口边界改用 DWM EXTENDED_FRAME_BOUNDS（排除隐形阴影），避免多录周边内容
- C7 “网页区域”模式：UIA 检测浏览器 Document 边界，只录网页内容（排除标签栏/地址栏外壳）
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
import threading
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
                             QVBoxLayout, QHBoxLayout, QLineEdit)
from PyQt5 import sip
from PyQt5.QtCore import Qt, QTimer, QPoint, QPointF, QEventLoop, QRectF, QSize
from PyQt5.QtGui import (QFont, QCursor, QColor, QPainter, QPen, QImage, QPolygonF,
                         QPixmap, QFontMetrics, QIcon)

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
    """封装 mss / d3dshot 两种后端，对外统一接口"""

    def __init__(self):
        self._kind = None
        self._impl = None

    def open(self):
        _ensure_heavy_deps()
        try:
            import d3dshot
            self._impl = d3dshot.create(capture_output="numpy", frame_buffer_size=8)
            self._kind = 'd3dshot'
            print("[录制] 抓屏后端: d3dshot (DXGI)")
            return
        except Exception:
            pass
        # 默认弃用 mss：其 BitBlt 固定带 CAPTUREBLT 标志，会在部分 Windows 上
        # 干扰系统光标合成，造成录制过程中鼠标持续闪烁；GDI 直抓内容等价
        self._impl = _GdiGrabber()
        self._kind = 'gdi'
        print("[录制] 抓屏后端: GDI（SRCCOPY，无 CAPTUREBLT）")

    def grab(self, rect):
        """rect = (left, top, width, height)，返回 BGR ndarray"""
        _ensure_heavy_deps()
        rx, ry, rw, rh = rect
        if self._kind == 'd3dshot':
            frame = self._impl.capture(region=(rx, ry, rx + rw, ry + rh))
            if frame is None:
                return None
            return cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        return self._impl.grab((rx, ry, rw, rh))

    def close(self):
        try:
            if self._kind == 'd3dshot' and self._impl:
                self._impl.stop()
            elif self._impl:
                self._impl.close()
        except Exception:
            pass
        self._impl = None


# ═══════════════════════════════════════════════════
#  录制器
# ═══════════════════════════════════════════════════

class ScreenRecorder:
    MAX_BUFFER_FRAMES = 30  # writer 未就绪时最多缓存 30 帧（≈2s @15fps）

    def __init__(self, fps, save_dir):
        self._fps = fps
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

    def is_paused(self):
        return self._paused

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def get_record_rect(self):
        return self._record_rect

    def set_fps(self, fps):
        self._fps = max(5, min(30, int(fps)))

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
        self._start_time = time.time()
        self._start_ts = self._start_time
        self._frame_count = 0
        self._total_clicks = 0
        self._pending_frames.clear()
        self._writer_ready.clear()
        self._stopped_event.clear()
        with self._click_lock:
            self._clicks.clear()

        threading.Thread(target=self._init_writer, args=(rw, rh), daemon=True).start()
        threading.Thread(target=self._capture_loop, daemon=True).start()
        return True

    def _init_writer(self, rw, rh):
        _ensure_heavy_deps()
        self._writer_size = (rw, rh)
        self._writer = None
        for codec in ('avc1', 'mp4v'):
            fourcc = cv2.VideoWriter_fourcc(*codec)
            w = cv2.VideoWriter(self._filepath, fourcc, self._fps, (rw, rh))
            if w.isOpened():
                self._writer = w
                print(f"[录制] 编码器: {codec}")
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
        resized = cv2.resize(frame, (nw, nh))
        canvas = np.zeros((ch, cw, 3), dtype=np.uint8)
        x0 = (cw - nw) // 2
        y0 = (ch - nh) // 2
        canvas[y0:y0 + nh, x0:x0 + nw] = resized
        return canvas

    def _capture_loop(self):
        self._backend = CaptureBackend()
        self._backend.open()

        rx, ry, rw, rh = self._record_rect
        interval = 1.0 / self._fps
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
                ws_w, ws_h = getattr(self, '_writer_size', (gw, gh))
                if frame.shape[1] != ws_w or frame.shape[0] != ws_h:
                    # 等比缩放+黑边居中：窗口移动/缩放/最大化都不变形
                    frame = self._fit_canvas(frame, ws_w, ws_h)

                # B1: writer 未就绪时缓存前若干帧
                if self._writer_ready.is_set() and self._writer and self._writer.isOpened():
                    while self._pending_frames:
                        self._writer.write(self._pending_frames.popleft())
                        self._frame_count += 1
                    self._writer.write(frame)
                    self._frame_count += 1
                else:
                    self._pending_frames.append(frame)

                frame_idx += 1
            except Exception as e:
                print(f"[录制] 帧错误: {e}")

        # 释放
        self._writer_ready.wait(timeout=5.0)
        if self._writer:
            while self._pending_frames:
                try:
                    self._writer.write(self._pending_frames.popleft())
                    self._frame_count += 1
                except Exception:
                    break
            self._writer.release()
            self._writer = None
        if self._backend:
            self._backend.close()
            self._backend = None
        elapsed = time.time() - self._start_time
        print(f"[录制] 停止: {self._frame_count} 帧, {elapsed:.1f} 秒 -> {self._filepath}")
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
        """C3: 从左侧 popleft 过期点击，遍历不拷贝"""
        _ensure_heavy_deps()
        now = time.time()
        H, W = frame.shape[:2]
        with self._click_lock:
            while self._clicks and now - self._clicks[0]['t'] >= 0.8:
                self._clicks.popleft()
            for c in self._clicks:
                age = now - c['t']
                if age < 0 or age >= 0.8:
                    continue
                progress = age / 0.8
                lx, ly = c['x'] - rx, c['y'] - ry
                if not (0 <= lx < W and 0 <= ly < H):
                    continue
                max_r = 35
                # 淡黄色点击标记（BGR: 102,224,255 = #FFE066）+ 深褐描边保证浅/深背景都可见
                for ring_idx in range(2):
                    p2 = min(1.0, progress + ring_idx * 0.2)
                    alpha = 1 - progress - ring_idx * 0.2
                    r = int(max_r * p2)
                    if r > 0 and alpha > 0:
                        cv2.circle(frame, (lx, ly), r + 2, (14, 64, 146), 1)   # 描边
                        cv2.circle(frame, (lx, ly), r, (102, 224, 255), 2)     # 淡黄环
                cv2.circle(frame, (lx, ly), 5, (14, 64, 146), 1)               # 点描边
                cv2.circle(frame, (lx, ly), 4, (102, 224, 255), -1)            # 淡黄点


# ═══════════════════════════════════════════════════
#  鼠标监听
# ═══════════════════════════════════════════════════

class MouseClickListener:
    """点击监听：轮询 GetAsyncKeyState 边沿检测（替代 pynput 全局钩子）。

    低级鼠标钩子（WH_MOUSE_LL）会让每个鼠标事件都经过回调线程，
    会造成录制中系统光标闪烁；轮询方案零钩子、零系统影响，
    坐标取按压瞬间的 GetCursorPos（物理像素，与抓帧坐标一致）。"""

    def __init__(self, on_click):
        self._on_click = on_click
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
                    self._on_click(*win32api.GetCursorPos())
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
        # 仅当 0.8s 生命窗口内还有活跃点击脉冲时才重绘（含收尾清屏帧）：
        # 平时零重绘，避免全屏 layered 窗口 30Hz 空转干扰系统光标合成（鼠标闪烁）
        has_clicks = bool(self._recorder.get_clicks_snapshot(window=0.8))
        if has_clicks or self._was_active:
            self.update()
        self._was_active = has_clicks

    def paintEvent(self, event):
        if not self._enabled:
            return
        clicks = self._recorder.get_clicks_snapshot(window=0.8)
        if not clicks:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        ox, oy = self.geometry().x(), self.geometry().y()
        for (sx, sy, age) in clicks:
            progress = age / 0.8
            if progress >= 1.0:
                continue
            lx, ly = sx - ox, sy - oy
            max_r = 35
            base_alpha = int(220 * (1 - progress))
            # 淡黄色（#FFE066）+ 深褐描边，浅/深背景都清晰
            for ring_idx in range(2):
                p2 = min(1.0, progress + ring_idx * 0.2)
                r = int(max_r * p2)
                a = max(0, base_alpha - ring_idx * 60)
                if r > 0 and a > 0:
                    painter.setPen(QPen(QColor(146, 64, 14, a), 1))          # 描边
                    painter.drawEllipse(QPoint(lx, ly), r + 2, r + 2)
                    painter.setPen(QPen(QColor(255, 224, 102, a), 2))         # 淡黄环
                    painter.drawEllipse(QPoint(lx, ly), r, r)
            painter.setPen(QPen(QColor(146, 64, 14, min(255, base_alpha + 30)), 1))
            painter.setBrush(QColor(255, 224, 102, min(255, base_alpha + 30)))
            painter.drawEllipse(QPoint(lx, ly), 4, 4)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(Qt.NoPen)


# ═══════════════════════════════════════════════════
#  录制标注（画笔 / 箭头 / 框选 / 荧光 / 文字）
# ═══════════════════════════════════════════════════

# 标注调色板：高饱和高对比，浅色/深色背景上都清晰可辨
ANN_COLORS = {
    '红色': (255, 59, 48),
    '黄色': (255, 214, 10),
    '蓝色': (10, 132, 255),
    '绿色': (52, 199, 89),
}
ANN_WIDTHS = {'细': 3, '中': 5, '粗': 8}
ANN_MARKER_FACTOR = 4       # 荧光笔宽度 = 细/中/粗 ×4（半透明粗线）


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
    elif key == 'rect':
        p.setPen(QPen(c, 1.8))
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
        painter.setPen(QPen(col, w))
        painter.setBrush(Qt.NoBrush)
        rect = QRectF(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
        if t == 'rect':
            painter.drawRect(rect)
        else:
            painter.drawEllipse(rect)
    elif t == 'text':
        size = a.get('size', 22)
        painter.setFont(QFont('Microsoft YaHei', size, QFont.Bold))
        typo_x, typo_y = a['x'] - ox, a['y'] - oy + int(size * 0.85)
        text = a.get('text', '')
        # 深色投影：浅色背景上也能看清
        painter.setPen(QColor(0, 0, 0, 150))
        painter.drawText(QPointF(typo_x + 1.5, typo_y + 1.5), text)
        painter.setPen(col)
        painter.drawText(QPointF(typo_x, typo_y), text)


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
        elif self._tool in ('arrow', 'rect', 'ellipse'):
            self._live = {'type': self._tool, 'color': self._color, 'width': self._width,
                          'x1': g.x(), 'y1': g.y(), 'x2': g.x(), 'y2': g.y()}
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
        if not self._live or not (e.buttons() & Qt.LeftButton):
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


class _TextInputPopup(QWidget):
    """文字标注输入框：悬浮在点击位置旁，Enter 确认 / Esc 取消 / 失焦取消"""

    def __init__(self, on_ok, on_cancel, on_vis=None):
        super().__init__()
        self._on_ok = on_ok
        self._on_cancel = on_cancel
        self._on_vis = on_vis       # 显示/隐藏回调（通知标注层同步命中排除区）
        self._anchor = (0, 0)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setFixedSize(300, 50)
        self.setStyleSheet(
            "QWidget { background:#111827; border:1px solid rgba(255,255,255,70);"
            " border-radius:10px; }"
            "QLineEdit { background:#1f2937; color:#fff; border:none; border-radius:7px;"
            " font-size:15px; padding:6px 10px; }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        self._edit = QLineEdit()
        self._edit.setPlaceholderText('输入文字，Enter 确认，Esc 取消')
        self._edit.returnPressed.connect(self._commit)
        lay.addWidget(self._edit)
        self.hide()

    def anchor(self):
        return self._anchor

    def open_at(self, gx, gy):
        self._anchor = (gx, gy)
        vg = QApplication.primaryScreen().virtualGeometry()
        x = max(vg.x() + 4, min(gx, vg.x() + vg.width() - self.width() - 4))
        y = gy + 18
        if y + self.height() > vg.y() + vg.height() - 4:
            y = gy - self.height() - 18
        self.move(x, y)
        self._edit.clear()
        self.show()
        self.raise_()
        self.activateWindow()
        self._edit.setFocus()

    def _commit(self):
        text = self._edit.text().strip()
        self.hide()
        if text:
            self._on_ok(text)
        else:
            self._on_cancel()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.hide()
            self._on_cancel()
        else:
            super().keyPressEvent(e)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        if self.isVisible():
            self.hide()
            self._on_cancel()

    def showEvent(self, e):
        super().showEvent(e)
        if self._on_vis:
            self._on_vis()

    def hideEvent(self, e):
        super().hideEvent(e)
        if self._on_vis:
            self._on_vis()


class AnnotationToolbar(QWidget):
    """录制中标注工具条：工具 / 颜色 / 粗细 / 撤销 / 清空（贴靠主面板显示）"""

    # 按钮直接用中文标注用途（图标 emoji 在不同字体下显示差异大、难以辨认）
    TOOLS = [
        ('hand',    '手',   '手：不绘制，正常操作（鼠标穿透）'),
        ('pen',     '画笔', '画笔：自由手绘'),
        ('arrow',   '箭头', '箭头：拖拽画直线箭头'),
        ('rect',    '矩形', '矩形：拖拽画矩形框'),
        ('ellipse', '椭圆', '椭圆：拖拽画椭圆框'),
        ('marker',  '荧光', '荧光笔：半透明粗线划重点'),
        ('text',    '文字', '文字：点击空白输入文字；按住已有文字可拖动位置'),
    ]

    def __init__(self, recorder, ann_overlay):
        super().__init__()
        self._recorder = recorder
        self._overlay = ann_overlay
        self._tool = 'hand'
        self._color_key = '红色'
        self._width_key = '中'
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setFixedSize(352, 76)
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
            QWidget { background:#1f2937; color:#f3f4f6; border-radius:10px; }
            QPushButton, QToolButton { border:none; border-radius:6px; font-size:13px; font-weight:600; }
            QToolButton { font-size:9px; padding:1px; }
            QPushButton#annOn, QToolButton#annOn { background:#2563eb; color:#fff; }
            QPushButton#annOff, QToolButton#annOff { background:#374151; color:#cbd5e1; }
            QPushButton#annOff:hover, QToolButton#annOff:hover { background:#4b5563; }
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
            b.setIcon(_tool_icon(key, '#cbd5e1'))
            b.setToolTip(tip)
            b.setCursor(QCursor(Qt.PointingHandCursor))
            b.clicked.connect(lambda _=False, k=key: self._pick_tool(k))
            row1.addWidget(b)
            self._tool_btns[key] = b
        row1.addStretch()
        root.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(5)
        self._color_btns = {}
        for key, rgb in ANN_COLORS.items():
            b = QPushButton()
            b.setFixedSize(22, 22)
            b.setToolTip(f'颜色：{key}')
            b.setCursor(QCursor(Qt.PointingHandCursor))
            b.clicked.connect(lambda _=False, k=key: self._pick_color(k))
            row2.addWidget(b)
            self._color_btns[key] = b
        row2.addSpacing(10)
        self._width_btns = {}
        for key in ('细', '中', '粗'):
            b = QPushButton(key)
            b.setFixedSize(34, 24)
            b.setToolTip(f'线宽：{key}')
            b.setCursor(QCursor(Qt.PointingHandCursor))
            b.clicked.connect(lambda _=False, k=key: self._pick_width(k))
            row2.addWidget(b)
            self._width_btns[key] = b
        row2.addStretch()
        undo_btn = QPushButton('撤销')
        undo_btn.setFixedSize(44, 24)
        undo_btn.setToolTip('撤销上一个标注')
        undo_btn.setCursor(QCursor(Qt.PointingHandCursor))
        undo_btn.clicked.connect(self._on_undo)
        row2.addWidget(undo_btn)
        clr_btn = QPushButton('清空')
        clr_btn.setFixedSize(44, 24)
        clr_btn.setToolTip('清空全部标注（只影响之后的画面）')
        clr_btn.setCursor(QCursor(Qt.PointingHandCursor))
        clr_btn.clicked.connect(self._on_clear)
        row2.addWidget(clr_btn)
        root.addLayout(row2)

        # 文字输入弹窗（点击画面位置后弹出）
        self._text_popup = _TextInputPopup(self._on_text_ok, self._on_text_cancel,
                                           self._sync_exclude)
        _exclude_from_capture(self._text_popup)
        self._overlay._on_text_click = self._text_popup.open_at

        self._refresh_styles()
        self.hide()

    # ── 对外 ────────────────────────

    def reset(self):
        self._tool = 'hand'
        self._color_key = '红色'
        self._width_key = '中'
        self._overlay.set_tool('hand')
        self._overlay.set_style(color=ANN_COLORS['红色'], width=ANN_WIDTHS['中'])
        self._refresh_styles()

    def dismiss(self):
        """隐藏工具条（含文字输入框）"""
        self._text_popup.hide()
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
        self._place_at(panel)
        self.show()
        self.raise_()
        self._sync_exclude()

    def move_beside(self, panel):
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
        self._tool = 'hand' if (key == self._tool and key != 'hand') else key
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

    def _on_undo(self):
        self._recorder.undo_annotation()
        self._overlay.update()

    def _on_clear(self):
        self._recorder.clear_annotations()
        self._overlay.update()

    def _on_text_ok(self, text):
        gx, gy = self._text_popup.anchor()
        self._recorder.add_annotation({'type': 'text', 'color': ANN_COLORS[self._color_key],
                                       'size': 22, 'x': gx, 'y': gy, 'text': text})
        self._overlay.update()
        # 保持文字工具：写完可立即按住文字拖动位置（不用再点一次按钮）

    def _on_text_cancel(self):
        pass   # 取消输入后保持文字工具，便于重新点击或拖动已有文字

    def _refresh_styles(self):
        for key, b in self._tool_btns.items():
            on = key == self._tool
            b.setObjectName('annOn' if on else 'annOff')
            b.setIcon(_tool_icon(key, '#ffffff' if on else '#cbd5e1'))
            b.setStyle(b.style())
        for key, b in self._color_btns.items():
            r, g, bl = ANN_COLORS[key]
            sel = key == self._color_key
            b.setStyleSheet(
                "QPushButton { background:rgb(%d,%d,%d); border-radius:11px;"
                " border:2px solid %s; }" % (r, g, bl,
                                             '#ffffff' if sel else 'rgba(255,255,255,60)'))
        for key, b in self._width_btns.items():
            b.setObjectName('annOn' if key == self._width_key else 'annOff')
            b.setStyle(b.style())


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
        # 预览用明亮玫红更友好，录制中用正红更醒目
        main = QColor(220, 38, 38) if recording else QColor(244, 63, 94)
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
            label = '⏸ 已暂停' if self._paused_flag else '● REC'
        else:
            label = f'● 预览　{self._rect_w} × {self._rect_h}' if self._rect_w else '● 预览'
        font = QFont('Microsoft YaHei', 11, QFont.Bold)
        painter.setFont(font)
        tw = painter.fontMetrics().width(label) + 30
        th = 28
        painter.setPen(Qt.NoPen)
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

def run_control_panel(fps, save_dir, api_port):
    app = QApplication.instance() or QApplication(sys.argv)

    recorder = ScreenRecorder(fps=fps, save_dir=save_dir)
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
            self._last_pending_fps = init_status.get('pending_fps')
            self._last_pending_dir = init_status.get('pending_save_dir')

            self.setWindowTitle('屏幕录制控制台')
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
            self.setFixedSize(350, 220)
            self.setStyleSheet("""
                QWidget { background:#1f2937; color:#f3f4f6; border-radius:10px; }
                QLabel { color:#f3f4f6; }
                QPushButton { padding:6px 10px; border:none; border-radius:6px; font-size:12px; font-weight:600; }
                QPushButton#btnStart { background:#dc2626; color:#fff; }
                QPushButton#btnStart:hover { background:#b91c1c; }
                QPushButton#btnStop { background:#059669; color:#fff; }
                QPushButton#btnStop:hover { background:#047857; }
                QPushButton#btnPause { background:#f59e0b; color:#fff; }
                QPushButton#btnPause:hover { background:#d97706; }
                QPushButton#btnPause:disabled { background:#4b5563; color:#9ca3af; }
                QPushButton#btnMode { background:#3b82f6; color:#fff; }
                QPushButton#btnMode:hover { background:#2563eb; }
                QPushButton#btnModeOn { background:#2563eb; color:#fff; }
                QPushButton#btnModeOff { background:#374151; color:#cbd5e1; }
                QPushButton#btnModeOff:hover { background:#4b5563; }
                QPushButton#btnModeOn:disabled, QPushButton#btnModeOff:disabled { background:#1f2937; color:#6b7280; }
                QPushButton#btnHide { background:#4b5563; color:#fff; }
                QPushButton#btnHide:hover { background:#374151; }
                QLabel#title { font-size:13px; color:#9ca3af; font-weight:600; }
                QLabel#status { font-size:13px; color:#fbbf24; font-weight:600; }
                QLabel#timer { font-size:15px; color:#fff; font-family:monospace; }
            """)

            layout = QVBoxLayout()
            layout.setContentsMargins(12, 8, 12, 10)
            layout.setSpacing(6)

            tr = QHBoxLayout()
            t = QLabel('🎬 录制控制台')
            t.setObjectName('title')
            tr.addWidget(t)
            tr.addStretch()
            hide_btn = QPushButton('—')
            hide_btn.setObjectName('btnHide')
            hide_btn.setFixedSize(28, 22)
            hide_btn.setCursor(QCursor(Qt.PointingHandCursor))
            hide_btn.setToolTip('隐藏面板（进程保持常驻，Web 端可再次唤醒）')
            hide_btn.clicked.connect(self._on_hide)
            tr.addWidget(hide_btn)
            close_btn = QPushButton('×')
            close_btn.setObjectName('btnHide')
            close_btn.setFixedSize(28, 22)
            close_btn.setCursor(QCursor(Qt.PointingHandCursor))
            close_btn.setToolTip('完全退出面板进程')
            close_btn.clicked.connect(self._on_close)
            tr.addWidget(close_btn)
            layout.addLayout(tr)

            sr = QHBoxLayout()
            self._status_lbl = QLabel('● 就绪')
            self._status_lbl.setObjectName('status')
            self._status_lbl.setStyleSheet('color:#10b981;')
            sr.addWidget(self._status_lbl)
            sr.addStretch()
            self._timer_lbl = QLabel('00:00')
            self._timer_lbl.setObjectName('timer')
            sr.addWidget(self._timer_lbl)
            layout.addLayout(sr)

            mr = QHBoxLayout()
            self._full_btn = QPushButton('📺 全屏')
            self._full_btn.setObjectName('btnModeOn')
            self._full_btn.setToolTip('录制整个屏幕')
            self._full_btn.clicked.connect(self._set_full_mode)
            mr.addWidget(self._full_btn)
            self._win_btn = QPushButton('📋 选择窗口')
            self._win_btn.setObjectName('btnModeOff')
            self._win_btn.setToolTip('弹出窗口列表，点选要录制的窗口')
            self._win_btn.clicked.connect(self._pick_from_list)
            self._win_btn.setVisible(False)  # 按需求仅保留“活动窗口”入口
            mr.addWidget(self._win_btn)
            self._active_btn = QPushButton('🎯 活动窗口')
            self._active_btn.setObjectName('btnModeOff')
            self._active_btn.setToolTip('点后面板隐藏并倒计时3秒，自动录制当前最前面的窗口')
            self._active_btn.clicked.connect(self._pick_active)
            mr.addWidget(self._active_btn)
            self._web_btn = QPushButton('🌐 网页区域')
            self._web_btn.setObjectName('btnModeOff')
            self._web_btn.setToolTip('只录窗口内的网页内容，自动排除浏览器标签栏/地址栏（Firefox/Chrome/Edge）')
            self._web_btn.clicked.connect(self._pick_web_area)
            mr.addWidget(self._web_btn)
            layout.addLayout(mr)

            ar = QHBoxLayout()
            self._start_btn = QPushButton('● 开始录制')
            self._start_btn.setObjectName('btnStart')
            self._start_btn.setToolTip('开始录制；录制中点击则停止并保存文件')
            self._start_btn.clicked.connect(self._on_start)
            ar.addWidget(self._start_btn)
            self._pause_btn = QPushButton('⏸ 暂停')
            self._pause_btn.setObjectName('btnPause')
            self._pause_btn.setToolTip('暂停/继续：冻结画面但不停止，点“继续”后接着录（暂停时段不写入视频）')
            self._pause_btn.clicked.connect(self._on_pause)
            self._pause_btn.setEnabled(False)
            ar.addWidget(self._pause_btn)
            layout.addLayout(ar)

            self.setLayout(layout)

            # A2: 恢复上次位置
            saved_pos = init_status.get('panel_pos')
            vg = QApplication.primaryScreen().virtualGeometry()
            if saved_pos and isinstance(saved_pos, dict):
                try:
                    px = int(saved_pos.get('x', vg.x() + vg.width() - 320))
                    py = int(saved_pos.get('y', vg.y() + 20))
                    px = max(vg.x(), min(px, vg.x() + vg.width() - self.width()))
                    py = max(vg.y(), min(py, vg.y() + vg.height() - self.height()))
                    self.move(px, py)
                except Exception:
                    self.move(vg.x() + vg.width() - 320, vg.y() + 20)
            else:
                self.move(vg.x() + vg.width() - 320, vg.y() + 20)

            self._ui_timer = QTimer(self)
            self._ui_timer.timeout.connect(self._update_timer)
            self._rec_start_time = 0
            self._paused_accum = 0
            self._pause_start = 0

            self._click_timer = QTimer(self)
            self._click_timer.timeout.connect(self._update_click_display)

            # A1/A3/F5: 状态文件轮询
            self._status_poll = QTimer(self)
            self._status_poll.timeout.connect(self._poll_status_file)
            self._status_poll.start(500)

            _write_status(panel_visible=True, panel_pid=os.getpid())
            self._update_status()

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
            wake = s.get('panel_wake_ts', 0)
            if wake and wake != self._last_wake_ts:
                self._last_wake_ts = wake
                if not self.isVisible():
                    self.show()
                    self.raise_()
                    self.activateWindow()
                if recorder.is_running():    # 录制中唤醒：恢复标注工具条
                    ann_toolbar.show_beside(self)
                _write_status(panel_visible=True, panel_pid=os.getpid())
            pf = s.get('pending_fps')
            if pf and pf != self._last_pending_fps:
                self._last_pending_fps = pf
                if not recorder.is_running():
                    try:
                        recorder.set_fps(int(pf))
                        self._flash_status(f'● FPS 已更新为 {pf}', '#10b981')
                    except Exception:
                        pass
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
            self._cd.setStyleSheet("color:#fff;font-size:60px;font-weight:bold;"
                                   "background:rgba(0,0,0,180);border-radius:16px;")
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
                self._rec_start_time = time.time()
                self._paused_accum = 0
                self._pause_start = 0
                self._ui_timer.start(500)
                self._click_timer.start(500)
                self._update_status()
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
                ann_toolbar.show_beside(self)
                if state['mouse_listener'] is None:
                    state['mouse_listener'] = MouseClickListener(on_click=recorder.add_click)
                    state['mouse_listener'].start()
                _write_status(recording=True, paused=False, click_count=0,
                              last_file='', start_time=time.time())
            else:
                self._status_lbl.setText('启动失败')
                self._status_lbl.setStyleSheet('color:#ef4444;')

        def _on_pause(self):
            if not recorder.is_running():
                return
            if recorder.is_paused():
                recorder.resume()
                if self._pause_start:
                    self._paused_accum += time.time() - self._pause_start
                    self._pause_start = 0
                self._pause_btn.setText('⏸ 暂停')
                border.set_state('recording', paused=False)
                overlay.set_enabled(True)
                overlay.raise_()
                _write_status(paused=False)
            else:
                recorder.pause()
                self._pause_start = time.time()
                self._pause_btn.setText('▶ 继续')
                border.set_state('recording', paused=True)
                overlay.set_enabled(False)
                _write_status(paused=True)
            self._update_status()

        def _on_stop(self):
            filepath, click_count = recorder.stop()
            self._ui_timer.stop()
            self._click_timer.stop()
            self._timer_lbl.setText('00:00')
            self._pause_btn.setText('⏸ 暂停')
            overlay.set_enabled(False)
            ann_toolbar.dismiss()
            ann_overlay.enable(False)
            recorder.clear_annotations()
            border.hide()  # 停止后不残留预览红框
            if state['mouse_listener']:
                state['mouse_listener'].stop()
                state['mouse_listener'] = None
            self._update_status()
            if filepath:
                fn = os.path.basename(filepath)
                _write_status(recording=False, paused=False, click_count=click_count,
                              last_finished_at=time.time(), last_file=fn)
                print(f"[录制] 已保存: {filepath} (点击: {click_count})")
                _notify_finished(api_port, fn, click_count)

        def _update_status(self):
            if recorder.is_running():
                if recorder.is_paused():
                    self._status_lbl.setText('⏸ 已暂停')
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
                self._pause_btn.setEnabled(True)
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

        def _update_click_display(self):
            # 仅同步点击数到状态文件（UI 不再展示点击计数）
            if recorder.is_running():
                _write_status(click_count=recorder._total_clicks)

        def _on_hide(self):
            """A1: 隐藏面板但保留进程，等待 Web 唤醒（录制继续）"""
            self.hide()
            ann_toolbar.dismiss()   # 工具条随面板收起；标注层保持显示（录制继续）
            _write_status(panel_visible=False)

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
    panel.show()
    _write_status(panel_visible=True, panel_pid=os.getpid())
    sys.exit(app.exec_())


# ═══════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fps', type=int, default=15)
    parser.add_argument('--save-dir', type=str, required=True)
    parser.add_argument('--api-port', type=int, default=5050)
    args = parser.parse_args()
    run_control_panel(fps=args.fps, save_dir=args.save_dir, api_port=args.api_port)


if __name__ == '__main__':
    main()
