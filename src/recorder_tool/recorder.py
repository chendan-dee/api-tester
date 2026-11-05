# -*- coding: utf-8 -*-
"""屏幕录制脚本 - CLI 版（已统一到 control_panel 实现）

C6/C7 优化：
- 移除重复的 ScreenRecorder / draw_overlay 实现
- 直接复用 control_panel.ScreenRecorder + CaptureBackend + MouseClickListener
- 保留 CLI 入口与 --mode region 的区域选择器（GUI 面板不用区域选择）
- 移除旧版 tkinter 文字标注（模态对话框会阻塞抓帧线程，实际不可用）

用法：
    python recorder.py --fps 15 --save-dir <DIR> --mode full
    python recorder.py --fps 15 --save-dir <DIR> --mode region
"""

import sys
import os
import signal
import time
import argparse

# 复用 control_panel 的核心实现（兼容包导入与脚本直跑两种方式）
try:
    from .control_panel import (
        ScreenRecorder,
        MouseClickListener,
        CaptureBackend,  # noqa: F401  (显式导出，方便调试)
        _write_status,
        _read_status,
    )
except ImportError:  # 以脚本方式直接运行（无包上下文）时回退同级导入
    from control_panel import (
        ScreenRecorder,
        MouseClickListener,
        CaptureBackend,  # noqa: F401
        _write_status,
        _read_status,
    )


# ═══════════════════════════════════════════════════
#  区域选择器（仅 CLI region 模式使用）
# ═══════════════════════════════════════════════════

def select_region():
    """弹出全屏蒙层选择录制区域或窗口"""
    from PyQt5.QtWidgets import QApplication, QWidget
    from PyQt5.QtCore import Qt, QRect
    from PyQt5.QtGui import QPixmap, QPainter, QColor, QCursor, QPen, QFont, QImage
    import mss
    try:
        from .control_panel import _enum_windows
    except ImportError:  # 脚本直跑模式
        from control_panel import _enum_windows

    # 由于 control_panel 未导出 _find_window_at，此处本地实现
    def _find_window_at(x, y, windows):
        for hwnd, title, wx, wy, ww, wh in reversed(windows):
            if wx <= x < wx + ww and wy <= y < wy + wh:
                return (hwnd, title, wx, wy, ww, wh)
        return None

    app = QApplication.instance() or QApplication(sys.argv)

    class RegionSelector(QWidget):
        def __init__(self):
            super().__init__()
            self._begin = None
            self._drag_rect = QRect()
            self._result = None
            self._hover_win = None
            self._is_dragging = False

            with mss.mss() as sct:
                mon = sct.monitors[0]
                img = sct.grab(mon)
                qimg = QImage(img.tobytes(), img.width, img.height, QImage.Format_RGB888)
                self._pixmap = QPixmap.fromImage(qimg)
                self._full_w = img.width
                self._full_h = img.height

            self._windows = _enum_windows()
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
            self.setGeometry(0, 0, self._full_w, self._full_h)
            self.setCursor(QCursor(Qt.CrossCursor))
            self.setMouseTracking(True)
            self.show()

        def paintEvent(self, event):
            painter = QPainter(self)
            painter.drawPixmap(0, 0, self._pixmap)
            painter.fillRect(self.rect(), QColor(0, 0, 0, 100))

            for hwnd, title, wx, wy, ww, wh in self._windows:
                if self._hover_win and self._hover_win[0] == hwnd:
                    continue
                painter.setPen(QPen(QColor(100, 180, 255, 120), 1, Qt.DashLine))
                painter.drawRect(wx, wy, ww, wh)

            if self._hover_win and not self._is_dragging:
                hwnd, title, wx, wy, ww, wh = self._hover_win
                painter.setPen(QPen(QColor(0, 220, 120), 3, Qt.SolidLine))
                painter.drawRect(wx, wy, ww, wh)
                font = QFont(); font.setPointSize(11); painter.setFont(font)
                tw = painter.fontMetrics().boundingRect(title).width() + 16
                th = 26
                tx, ty = wx, wy - th - 4
                if ty < 0:
                    ty = wy + 4
                painter.fillRect(tx, ty, tw, th, QColor(0, 220, 120))
                painter.setPen(QColor(255, 255, 255))
                painter.drawText(tx + 8, ty + 18, title)

            if self._is_dragging and self._drag_rect.isValid():
                r = self._drag_rect
                painter.setPen(QPen(QColor(0, 180, 255), 2, Qt.SolidLine))
                painter.drawRect(r)
                painter.setPen(QColor(255, 255, 255))
                ty = r.y() - 8 if r.y() > 20 else r.bottom() + 20
                painter.drawText(r.x(), ty, f'{r.width()} x {r.height()}')

            font = QFont(); font.setPointSize(13); painter.setFont(font)
            painter.setPen(QColor(0, 0, 0, 180))
            hint = '悬停选择窗口 | 点击选中 | 拖拽自定义区域 | Esc 取消'
            painter.drawText(21, self._full_h - 19, hint)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(20, self._full_h - 20, hint)

        def mouseMoveEvent(self, event):
            if self._is_dragging:
                if self._begin:
                    self._drag_rect = QRect(self._begin, event.pos()).normalized()
                    self._hover_win = None
                    self.update()
                return
            pos = event.pos()
            win = _find_window_at(pos.x(), pos.y(), self._windows)
            if win != self._hover_win:
                self._hover_win = win
                self.update()

        def mousePressEvent(self, event):
            if event.button() == Qt.LeftButton:
                self._begin = event.pos()
                self._is_dragging = True
                self._hover_win = None
                self._drag_rect = QRect()

        def mouseReleaseEvent(self, event):
            if event.button() != Qt.LeftButton:
                return
            if self._is_dragging and self._drag_rect.width() > 10 and self._drag_rect.height() > 10:
                self._result = (self._drag_rect.x(), self._drag_rect.y(),
                                self._drag_rect.width(), self._drag_rect.height())
                self.close()
            elif self._hover_win:
                _, _, wx, wy, ww, wh = self._hover_win
                self._result = (wx, wy, ww, wh)
                self.close()
            else:
                self._result = None
                self.close()
            self._is_dragging = False

        def keyPressEvent(self, event):
            if event.key() == Qt.Key_Escape:
                self._result = None
                self.close()

    selector = RegionSelector()
    app.exec_()
    return selector._result


# ═══════════════════════════════════════════════════
#  CLI 主流程
# ═══════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='屏幕录制工具（CLI）')
    parser.add_argument('--fps', type=int, default=15, help='录制帧率 (5-30)')
    parser.add_argument('--save-dir', default=None, help='保存目录')
    parser.add_argument('--mode', choices=['full', 'region'], default='full',
                        help='录制模式：full=全屏，region=区域/窗口选择')
    args = parser.parse_args()

    fps = min(max(5, args.fps), 30)
    save_dir = args.save_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'transfer', 'recordings'
    )

    recorder = ScreenRecorder(fps=fps, save_dir=save_dir)

    # 停止信号处理
    def _sig(_signum, _frame):
        print("[录制] 收到停止信号")
        if recorder.is_running():
            recorder.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sig)
    if sys.platform == 'win32':
        signal.signal(signal.SIGBREAK, _sig)

    # 确定录制区域
    if args.mode == 'region':
        print("[录制] 请选择录制区域或窗口...")
        region = select_region()
        if region is None:
            print("[录制] 已取消")
            return 0
        rx, ry, rw, rh = region
    else:
        import mss
        with mss.mss() as sct:
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            rx, ry, rw, rh = mon['left'], mon['top'], mon['width'], mon['height']

    # 启动鼠标监听
    listener = MouseClickListener(on_click=recorder.add_click)
    try:
        listener.start()
    except Exception as e:
        print(f"[录制] 鼠标监听启动失败：{e}")
        listener = None

    # 直接调用内部 _record_rect + _start（绕过 start_fullscreen 的自动侦测）
    recorder._record_rect = (rx, ry, rw, rh)
    recorder._target_hwnd = None
    ok = recorder._start()
    if not ok:
        print("[录制] 启动失败")
        return 1

    _write_status(recording=True, paused=False, click_count=0,
                  last_file='', start_time=time.time())
    print(f"[录制] 开始 (FPS={fps}, 区域={rw}x{rh}, 起点={rx},{ry})  按 Ctrl+C 停止")

    try:
        while recorder.is_running():
            time.sleep(0.5)
            _write_status(click_count=recorder._total_clicks)
    except KeyboardInterrupt:
        pass
    finally:
        filepath, clicks = recorder.stop()
        if listener:
            listener.stop()
        if filepath:
            fn = os.path.basename(filepath)
            _write_status(recording=False, paused=False, click_count=clicks,
                          last_finished_at=time.time(), last_file=fn)
            print(f"[录制] 已保存: {filepath} (点击: {clicks})")

    return 0


if __name__ == '__main__':
    # 允许作为脚本直接运行（非包导入）
    if __package__ in (None, ''):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from control_panel import (  # type: ignore
            ScreenRecorder, MouseClickListener, _write_status, _read_status,
        )
    sys.exit(main())
