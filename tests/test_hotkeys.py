from pathlib import Path
import queue
import sys
import tempfile
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hotkeys import (DEFAULT_KEYS, WindowsHotkeys, load_shortcuts, parse_shortcut,
                     shortcut_from_event, validate_shortcuts)
from reader import Reader
from reader_core import read_json
from test_floating_reader import FakeTray, FakeHotkeys


class ShortcutFormatTests(unittest.TestCase):
    def test_defaults_validation_and_corrupt_saved_settings(self):
        self.assertEqual(load_shortcuts({}), DEFAULT_KEYS)
        for value in (None, {}, {"next": "Garbage"}, {"previous": "Left", "next": "Left"}):
            self.assertEqual(load_shortcuts({"hotkeys": value}), DEFAULT_KEYS)
        self.assertEqual(parse_shortcut("Shift+Alt+Ctrl+Left"), ("Ctrl+Alt+Shift+Left", 7, 0x25))
        for key in ("Ctrl+B", "Ctrl+H", "F12", "Escape", "Ctrl+Ctrl+A"):
            with self.assertRaises(ValueError):
                parse_shortcut(key)
        with self.assertRaises(ValueError):
            validate_shortcuts({"previous": "Alt+Ctrl+A", "next": "Ctrl+Alt+A"})

    def test_capture_ignores_caps_lock_and_keeps_shift_digit(self):
        with patch("hotkeys.sys.platform", "win32"):
            event = SimpleNamespace(keycode=0x31, keysym="exclam", state=0x20000 | 0x4 | 0x1 | 0x2)
            self.assertEqual(shortcut_from_event(event), "Ctrl+Alt+Shift+1")
            event.keycode = 0x11  # Modifier alone does not become a binding.
            self.assertIsNone(shortcut_from_event(event))


class ReaderHotkeyTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        with patch.object(Reader, "run"):
            self.reader = Reader(self.root, self.directory.name, tray_factory=FakeTray, hotkey_factory=FakeHotkeys)
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.reader.open_scene({"url": "https://wiki.biligame.com/zspms/shortcut", "title": "测试"},
                               {"title": "测试", "events": [
                                   {"kind": "line", "speaker": "甲", "text": "一句"},
                                   {"kind": "line", "speaker": "乙", "text": "两句"},
                                   {"kind": "choice", "id": "reply", "options": [{"label": "答", "events": []}]},
                               ]})
        self.root.update()

    def tearDown(self):
        if hasattr(self, "reader") and not self.reader.closed:
            self.reader.close()
        if hasattr(self, "directory"):
            self.directory.cleanup()
        if hasattr(self, "errors"):
            self.assertFalse(self.errors, self.errors)

    def key(self, code, state=0, widget=None):
        return self.reader.reading_key(SimpleNamespace(keycode=code, state=state,
                                                     keysym="", widget=widget or self.reader.text))

    def deliver(self, action, generation=None):
        service = self.reader.hotkey_service
        service.post(action, service.generation if generation is None else generation)
        self.root.after_cancel(self.reader.poll_timer)
        self.reader.poll()

    def test_arrows_share_mouse_semantics_and_wait_at_choice(self):
        self.key(0x27)
        self.assertEqual(self.reader.position, 1)
        self.key(0x25)
        self.assertEqual(self.reader.position, 0)
        self.reader.enter_floating()
        for _ in range(3):
            self.deliver("next")
        self.assertEqual(self.reader.position, 2)
        self.assertEqual(self.reader.choices, {})
        self.deliver("previous")
        self.assertEqual(self.reader.position, 1)

    def test_rebind_replaces_old_keys_and_persists(self):
        bindings = {"previous": "Ctrl+Alt+A", "next": "Ctrl+Alt+D"}
        self.reader.apply_hotkeys(bindings, True)
        self.key(0x27)
        self.assertEqual(self.reader.position, 0)
        self.key(0x44, 0x20004)
        self.assertEqual(self.reader.position, 1)
        self.key(0x41, 0x20004)
        self.assertEqual(self.reader.position, 0)
        self.key(0x44, 0x20004, self.reader.search)
        self.assertEqual(self.reader.position, 0)
        saved = read_json(self.reader.state_path, {})
        self.assertEqual(load_shortcuts(saved), bindings)
        self.reader.enter_floating()
        self.assertEqual(self.reader.hotkey_service.bindings, bindings)

    def test_modified_reserved_key_is_available_and_tk_binding_reaches_handler(self):
        self.reader.apply_hotkeys({"previous": "Ctrl+Alt+H", "next": "Ctrl+Alt+B"}, False)
        self.reader.restore_main()
        self.root.update()
        self.reader.text.focus_force()
        self.root.update()
        self.reader.text.event_generate("<Control-Alt-KeyPress-b>")
        self.assertEqual(self.reader.position, 1)
        self.reader.text.event_generate("<Control-Alt-KeyPress-h>")
        self.assertEqual(self.reader.position, 0)

    def test_release_when_hidden_restored_or_disabled_and_drop_stale_keys(self):
        service = self.reader.hotkey_service
        self.assertFalse(service.bindings)
        self.reader.enter_floating()
        old_generation = service.generation
        self.assertEqual(service.bindings, DEFAULT_KEYS)
        self.reader.hide_to_tray()
        self.assertFalse(service.bindings)
        self.reader.enter_floating()
        self.deliver("next", old_generation)
        self.assertEqual(self.reader.position, 0)
        self.reader.restore_main()
        self.assertFalse(service.bindings)
        self.reader.apply_hotkeys(DEFAULT_KEYS, False)
        self.reader.enter_floating()
        self.assertFalse(service.bindings)
        self.reader.close()
        self.assertTrue(service.stopped)

    def test_dialog_pauses_keys_validation_cancel_and_default(self):
        self.reader.enter_floating()
        self.reader.show_hotkey_settings()
        dialog = self.reader.hotkey_dialog
        self.root.update()
        self.assertFalse(self.reader.hotkey_service.bindings)
        dialog.entries["next"].focus_force()
        self.root.update()
        dialog.entries["next"].event_generate("<Control-Alt-KeyPress-d>")
        self.assertEqual(dialog.values["next"].get(), "Ctrl+Alt+D")
        self.assertEqual(self.reader.position, 0)
        dialog.values["previous"].set("Ctrl+Alt+D")
        dialog.save()
        self.assertIs(self.reader.hotkey_dialog, dialog)
        self.assertIn("相同", dialog.error.cget("text"))
        dialog.defaults()
        dialog.values["next"].set("Ctrl+Alt+D")
        dialog.save()
        self.assertIsNone(self.reader.hotkey_dialog)
        self.assertEqual(self.reader.hotkeys["next"], "Ctrl+Alt+D")
        self.assertEqual(self.reader.hotkey_service.bindings["next"], "Ctrl+Alt+D")
        self.reader.show_hotkey_settings()
        self.reader.hotkey_dialog.defaults()
        self.reader.hotkey_dialog.destroy()
        self.assertEqual(self.reader.hotkeys["next"], "Ctrl+Alt+D")

    def test_opening_dialog_does_not_reactivate_keys_during_idle_callbacks(self):
        self.reader.enter_floating()
        from hotkey_settings import HotkeySettings

        def create(*args):
            self.reader.sync_hotkeys(force=True)
            self.assertFalse(self.reader.hotkey_service.bindings)
            return HotkeySettings(*args)

        with patch("reader.HotkeySettings", side_effect=create):
            self.reader.show_hotkey_settings()
        self.assertFalse(self.reader.hotkey_service.bindings)

    def test_registration_failure_keeps_saved_configuration(self):
        self.reader.show_hotkey_settings()
        dialog = self.reader.hotkey_dialog
        self.reader.hotkey_service.errors = {"next": "按键已被占用"}
        dialog.values["next"].set("Ctrl+Alt+D")
        dialog.save()
        self.assertEqual(self.reader.hotkeys, DEFAULT_KEYS)
        self.assertIn("占用", dialog.error.cget("text"))
        self.assertFalse(self.reader.hotkey_service.bindings)

    def test_menu_and_picker_pause_then_resume(self):
        self.reader.enter_floating()
        floating = self.reader.floating
        floating.menu_open = True
        self.reader.sync_hotkeys()
        self.assertFalse(self.reader.hotkey_service.bindings)
        floating.menu_closed()
        self.root.update()
        self.assertEqual(self.reader.hotkey_service.bindings, DEFAULT_KEYS)
        self.reader.picker = SimpleNamespace(alive=True, destroy=lambda: None)
        self.reader.sync_hotkeys()
        self.assertFalse(self.reader.hotkey_service.bindings)
        self.reader.close_picker()
        self.reader.sync_hotkeys()
        self.assertEqual(self.reader.hotkey_service.bindings, DEFAULT_KEYS)


@unittest.skipUnless(sys.platform == "win32", "Windows registration test")
class NativeHotkeyTests(unittest.TestCase):
    def test_native_dispatch_conflict_replacement_and_release(self):
        commands = queue.Queue()
        first = WindowsHotkeys(lambda *command: commands.put(command))
        second = WindowsHotkeys(lambda *_: None)
        binding = {"next": "Ctrl+Alt+Shift+F10"}
        try:
            errors = first.replace(binding, 1)
            if errors and all("1409" in error for error in errors.values()):
                self.skipTest(str(errors))
            self.assertEqual(errors, {})
            old_id = next(iter(first.active))
            # Send WM_HOTKEY directly, without injecting keys into other apps.
            first.user32.PostThreadMessageW(first.thread_id, first.HOTKEY, old_id, 0)
            self.assertEqual(commands.get(timeout=2), ("next", 1))
            self.assertIn("next", second.replace(binding, 5))
            self.assertEqual(first.replace(binding, 2), {})
            new_id = next(iter(first.active))
            first.user32.PostThreadMessageW(first.thread_id, first.HOTKEY, old_id, 0)
            first.user32.PostThreadMessageW(first.thread_id, first.HOTKEY, new_id, 0)
            self.assertEqual(commands.get(timeout=2), ("next", 2))
            self.assertTrue(commands.empty())
            first.replace({}, 3)
            self.assertEqual(second.replace(binding, 6), {})
            second.stop()
            self.assertEqual(first.replace(binding, 4), {})
        finally:
            first.stop()
            second.stop()
