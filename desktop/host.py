# -*- coding: utf-8 -*-
"""V10 桌面端宿主 PoC（WebView2 / pywebview）。

用法：
  1. 安装宿主依赖：.venv\\Scripts\\pip install -e ".[desktop]"
  2. 先启动引擎（仓库根 start.bat，默认 http://127.0.0.1:8000）
  3. .venv\\Scripts\\python desktop\\host.py
     或：start_desktop.bat

说明：这是 WP0 的宿主验证，不是最终安装包。窗口按钮通过 pywebview 的
JS API 调用真实的最小化 / 最大化 / 关闭；页面里的标题栏保持 V10 视觉。
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import urllib.request

import webview


class HostApi:
    def __init__(self) -> None:
        # 注意：不要用公开的 self.window 保存窗口。pywebview 会递归遍历
        # js_api 的公开属性来生成 JS 代理，公开 window 会走进 WinForms 原生
        # 对象并触发递归上限，导致 api 方法全部暴露失败。
        self._window: webview.Window | None = None
        self._maxed = False

    def _attach(self, window: webview.Window) -> None:
        self._window = window

    def minimize(self) -> None:
        if self._window:
            self._window.minimize()

    def toggle_max(self) -> None:
        if not self._window:
            return
        if self._maxed:
            self._window.restore()
            self._maxed = False
        else:
            self._window.maximize()
            self._maxed = True

    def close(self) -> None:
        if self._window:
            self._window.destroy()

    def start_drag(self) -> None:
        """无边框窗口拖拽：把标题栏按下转成 Windows 的 HTCAPTION 拖拽。"""
        hwnd = ctypes.windll.user32.FindWindowW(None, "射箭训练报告")
        if hwnd:
            ctypes.windll.user32.ReleaseCapture()
            ctypes.windll.user32.SendMessageW(hwnd, 0xA1, 2, 0)


def engine_ready(base_url: str) -> bool:
    url = base_url.rstrip("/") + "/api/v1/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="SUOOTER 射箭训练报告桌面端宿主 PoC")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000",
                        help="引擎地址（默认 http://127.0.0.1:8000）")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    if not engine_ready(base):
        print(f"引擎未启动：请先运行 start.bat，或确认 {base} 可访问。")
        return 1

    api = HostApi()
    window = webview.create_window(
        "射箭训练报告",
        url=base + "/desktop/",
        width=1280,
        height=800,
        min_size=(1180, 720),
        frameless=True,
        easy_drag=False,
        js_api=api,
        background_color="#F3F5F7",
    )
    api._attach(window)
    webview.start(debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
