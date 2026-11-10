"""Shortcut timing and native listener lifecycle, without injecting OS keystrokes."""
import os
import queue
import time
import unittest

from clawx.hotkeys import DoubleAltDetector, GlobalHotkey
from clawx.llm import LLMConfig
from clawx.core import PolicyError


class HotkeyTests(unittest.TestCase):
    def test_double_alt_requires_two_released_taps_and_resets_after_match(self):
        detector = DoubleAltDetector()
        results = [detector.feed(0xA4, down, now)
                   for down, now in [(True, 0), (False, .05), (True, .15), (False, .20),
                                     (True, .25), (False, .30)]]
        self.assertEqual(results, [False, False, False, True, False, False])

    def test_repeated_keydown_long_hold_and_slow_taps_do_not_wake(self):
        sequences = [
            [(0xA4, True, 0), (0xA4, True, .1), (0xA4, True, .2), (0xA4, False, .3)],
            [(0xA4, True, 0), (0xA4, False, .8), (0xA4, True, .85), (0xA4, False, .9)],
            [(0xA4, True, 0), (0xA4, False, .1), (0xA4, True, 1), (0xA4, False, 1.1)]]
        for events in sequences:
            detector = DoubleAltDetector()
            self.assertFalse(any(detector.feed(*event) for event in events))

    def test_alt_tab_ctrl_alt_and_intervening_typing_break_the_gesture(self):
        for chord in (0x09, 0x11, 0x41):
            detector = DoubleAltDetector()
            events = [(0xA4, True, 0), (0xA4, False, .05), (chord, True, .10),
                      (0xA4, True, .12), (chord, False, .15), (0xA4, False, .20)]
            self.assertFalse(any(detector.feed(*event) for event in events))

    def test_invalid_hotkey_is_rejected_and_default_is_double_alt(self):
        self.assertEqual(LLMConfig().quick_hotkey, "Alt+Alt")
        with self.assertRaises(PolicyError):
            LLMConfig(quick_hotkey="invalid").validate()
        with self.assertRaises(ValueError):
            GlobalHotkey(queue.Queue(), "invalid")

    @unittest.skipUnless(os.name == "nt", "Windows native shortcut listener")
    def test_windows_listener_registers_reconfigures_and_releases_on_close(self):
        events = queue.Queue()
        listener = GlobalHotkey(events)
        self.assertTrue(listener.start())
        try:
            self.assertTrue(listener.ready.wait(timeout=3))
            self.assertEqual(listener.registered, "Alt+Alt", list(events.queue))
            listener.configure("关闭")
            deadline = time.monotonic() + 3
            while listener.registered != "关闭" and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(listener.registered, "关闭")
            listener.configure("Alt+Alt")
            deadline = time.monotonic() + 3
            while listener.registered != "Alt+Alt" and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(listener.registered, "Alt+Alt")
        finally:
            listener.close()
            listener.thread.join(timeout=3)
        self.assertFalse(listener.thread.is_alive())
        self.assertIsNone(listener.user32)
        self.assertEqual(listener.thread_id, 0)
        self.assertTrue(events.empty(), list(events.queue))


if __name__ == "__main__":
    unittest.main()
