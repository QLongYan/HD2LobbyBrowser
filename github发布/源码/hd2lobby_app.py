# -*- coding: utf-8 -*-
"""HD2 大厅搜索器 —— 独立窗口版入口

起本地 HTTP 服务（复用 hd2_lobby_browser 的全部接口与页面），
再用 WebView2 开一个独立窗口显示它；支持全局快捷键唤起。

不注入、不读游戏内存、不碰 EAC。
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import socket
import sys
import threading
import time
import traceback

import webview

import hd2_lobby_browser as core

TITLE = "HD2 大厅搜索器"
HOTKEY_LABEL = "Ctrl+Alt+H"

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
VK_H = 0x48
SW_RESTORE = 9
SW_SHOW = 5
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040

window_ref = {"w": None}


def find_hwnd():
    """按标题枚举，取面积最大的可见窗口（同名隐藏窗口不能要）。"""
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def enum_proc(h, lparam):
        n = user32.GetWindowTextLengthW(h)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, buf, n + 1)
            if buf.value == TITLE and user32.IsWindowVisible(h):
                r = wt.RECT()
                user32.GetWindowRect(h, ctypes.byref(r))
                found.append(((r.right - r.left) * (r.bottom - r.top), h))
        return True

    user32.EnumWindows(enum_proc, 0)
    found.sort(reverse=True)
    return found[0][1] if found else 0


def bring_forward():
    """把窗口提到最前（对无边框全屏游戏也有效），并抢焦点。"""
    hwnd = find_hwnd()
    if not hwnd:
        return
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW)
    fg = user32.GetForegroundWindow()
    if fg and fg != hwnd:
        tid_fg = user32.GetWindowThreadProcessId(fg, None)
        tid_me = kernel32.GetCurrentThreadId()
        tid_win = user32.GetWindowThreadProcessId(hwnd, None)
        for tid in (tid_win, tid_fg):
            if tid and tid != tid_me:
                user32.AttachThreadInput(tid_me, tid, True)
        user32.SetForegroundWindow(hwnd)
        user32.SetFocus(hwnd)
        for tid in (tid_win, tid_fg):
            if tid and tid != tid_me:
                user32.AttachThreadInput(tid_me, tid, False)
    else:
        user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)


def toggle_window():
    hwnd = find_hwnd()
    if not hwnd:
        return
    if user32.IsIconic(hwnd):
        bring_forward()
    elif user32.GetForegroundWindow() == hwnd:
        user32.ShowWindow(hwnd, 6)      # SW_MINIMIZE
    else:
        bring_forward()


def hotkey_thread():
    """线程级 RegisterHotKey（hwnd=None）+ 消息循环；Ctrl+Alt+H 全局生效。"""
    for _ in range(60):                 # 等窗口出现
        if find_hwnd():
            break
        time.sleep(0.2)
    ok = user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_H)
    try:
        base = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
        with open(os.path.join(base, "hotkey.log"), "w", encoding="utf-8") as fh:
            fh.write("RegisterHotKey=%s\n" % ok)
    except Exception:
        pass
    if not ok:
        return
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY:
            try:
                toggle_window()
            except Exception:
                pass


def start_server():
    port = core.pick_port()
    srv = core.ThreadingHTTPServer(("127.0.0.1", port), core.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return port


def main():
    core.cleanup_old()                  # 清掉上次升级留下的 .old
    core.STEAM = core.SteamLobbies()
    core.LATENCY = core.Latency()
    core.STATS = core.PlanetStats()
    core.FAILS = core.JoinFailures()
    core.PLATFORM = core.PlatformIndex(core.STEAM)
    core.WAR = core.WarEffects()
    core.WAR.warm()                     # 星球变种（战争状态），后台每 5 分钟刷一次
    if core.STEAM.ok:
        core.LATENCY.warm()
        core.STATS.warm()

    # 启动即查最新版（后台线程，不阻塞窗口）
    threading.Thread(target=core.check_update, daemon=True).start()

    port = start_server()
    url = "http://127.0.0.1:%d/" % port

    w = webview.create_window(
        TITLE, url,
        width=1360, height=820, min_size=(1000, 600),
        background_color="#12151a",
        text_select=True,
    )
    window_ref["w"] = w

    threading.Thread(target=hotkey_thread, daemon=True).start()
    threading.Timer(2.0, bring_forward).start()

    webview.start(gui="edgechromium", debug=False)
    try:
        core.STEAM.shutdown()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            base = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
            with open(os.path.join(base, "startup_error.log"), "w", encoding="utf-8") as fh:
                fh.write(traceback.format_exc())
        except Exception:
            pass
        raise
