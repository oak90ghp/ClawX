"""Windows global wake shortcuts. Native callbacks only publish queue events."""
from __future__ import annotations

import os
import threading
import time


QUICK_HOTKEYS = ("Alt+Alt", "Ctrl+Space", "Ctrl+Shift+Space", "关闭")


class DoubleAltDetector:
    ALT_KEYS = {0x12, 0xA4, 0xA5}

    def __init__(self, interval=0.45, max_hold=0.35):
        self.interval, self.max_hold = interval, max_hold
        self.reset()

    def reset(self):
        self.held = set()
        self.alt_started = None
        self.last_tap = None
        self.clean = False

    def feed(self, key, down, now):
        """Match two standalone taps; reject repeats, long holds and Alt chords."""
        if down:
            if key in self.held:
                return False
            self.held.add(key)
            if key in self.ALT_KEYS and len(self.held) == 1:
                self.alt_started, self.clean = now, True
            else:
                self.clean, self.last_tap = False, None
            return False
        was_held = key in self.held
        self.held.discard(key)
        if key not in self.ALT_KEYS or not was_held:
            return False
        valid = (self.clean and not self.held and self.alt_started is not None
                 and 0 <= now - self.alt_started <= self.max_hold)
        self.clean, self.alt_started = False, None
        if not valid:
            self.last_tap = None
            return False
        matched = self.last_tap is not None and 0 <= now - self.last_tap <= self.interval
        self.last_tap = None if matched else now
        return matched


class GlobalHotkey:
    CONFIG_MESSAGE = 0x8001

    def __init__(self, events, shortcut="Alt+Alt"):
        if shortcut not in QUICK_HOTKEYS:
            raise ValueError("无效的快速唤醒快捷键")
        self.events, self.shortcut = events, shortcut
        self.lock = threading.Lock()
        self.stopped, self.ready = threading.Event(), threading.Event()
        self.thread = None
        self.thread_id = 0
        self.user32 = None
        self.registered = None

    def start(self):
        if os.name != "nt":
            return False
        if self.thread and self.thread.is_alive():
            return True
        self.stopped.clear()
        self.ready.clear()
        self.thread = threading.Thread(target=self._run, daemon=True, name="ClawX-Wake-Hotkey")
        self.thread.start()
        return True

    def configure(self, shortcut):
        if shortcut not in QUICK_HOTKEYS:
            raise ValueError("无效的快速唤醒快捷键")
        with self.lock:
            self.shortcut = shortcut
        if self.user32 and self.thread_id:
            self.user32.PostThreadMessageW(self.thread_id, self.CONFIG_MESSAGE, 0, 0)

    def close(self):
        self.stopped.set()
        if self.user32 and self.thread_id:
            self.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)  # WM_QUIT

    def _run(self):
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        hook = None
        detector = DoubleAltDetector()
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        class KeyboardData(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]

        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, callback_type, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HANDLE
        user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostThreadMessageW.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = ctypes.c_int
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                      wintypes.UINT, wintypes.UINT, wintypes.UINT]
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE

        def on_key(code, message, address):
            if code >= 0 and not self.stopped.is_set():
                try:
                    data = ctypes.cast(address, ctypes.POINTER(KeyboardData)).contents
                    if not data.flags & 0x10 and message in {0x100, 0x101, 0x104, 0x105}:
                        if detector.feed(data.vkCode, message in {0x100, 0x104}, time.monotonic()):
                            self.events.put_nowait(("quick_wake", None))
                except Exception:
                    # Keep the OS input chain intact even if our detector fails.
                    detector.reset()
            return user32.CallNextHookEx(hook, code, message, address)

        callback = callback_type(on_key)  # Retain the callback for the entire native message loop.

        def apply_shortcut():
            nonlocal hook
            if hook:
                user32.UnhookWindowsHookEx(hook)
                hook = None
            user32.UnregisterHotKey(None, 1)
            detector.reset()
            self.registered = None
            with self.lock:
                shortcut = self.shortcut
            if shortcut == "Alt+Alt":
                # Include modifiers held before hook installation, so an Alt chord
                # cannot become a standalone tap when the listener starts midway.
                detector.held = {key for key in (0xA0, 0xA1, 0xA2, 0xA3, 0x5B, 0x5C)
                                 if user32.GetAsyncKeyState(key) & 0x8000}
                hook = user32.SetWindowsHookExW(13, callback, kernel32.GetModuleHandleW(None), 0)
                if not hook:
                    raise OSError("无法安装 Alt+Alt 快捷键，请检查系统权限")
            elif shortcut != "关闭":
                modifiers = 0x4000 | 0x0002 | (0x0004 if shortcut == "Ctrl+Shift+Space" else 0)
                if not user32.RegisterHotKey(None, 1, modifiers, 0x20):
                    raise OSError("快捷键被其他程序占用，请在模型与连接中选择其他快速唤醒快捷键")
            self.registered = shortcut

        def configure_safely():
            try:
                apply_shortcut()
            except OSError as exc:
                self.events.put_nowait(("hotkey_error", str(exc)))

        try:
            message = wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            self.thread_id, self.user32 = kernel32.GetCurrentThreadId(), user32
            configure_safely()
            self.ready.set()
            while not self.stopped.is_set():
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise OSError("快速唤醒消息循环失败")
                if message.message == self.CONFIG_MESSAGE:
                    configure_safely()
                elif (message.message == 0x0312 and not self.stopped.is_set()
                      and self.registered in {"Ctrl+Space", "Ctrl+Shift+Space"}):  # WM_HOTKEY
                    self.events.put_nowait(("quick_wake", None))
        except OSError as exc:
            self.events.put_nowait(("hotkey_error", str(exc)))
        finally:
            if hook:
                user32.UnhookWindowsHookEx(hook)
            user32.UnregisterHotKey(None, 1)
            self.thread_id, self.user32, self.registered = 0, None, None
            self.ready.set()
