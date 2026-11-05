"""Headless data-operation UI checks; all databases and messages are synthetic."""

from contextlib import nullcontext
from pathlib import Path
import tempfile
import threading
from tkinter import CallWrapper
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

from genesis.adapters.desktop import DesktopDataConfig, DesktopWindow, main
from genesis.adapters.desktop_data_worker import DataTaskWorker
from genesis.adapters.desktop_worker import ChatWorker
from genesis.models.contracts import ModelError
from genesis.models.fake import FakeModel
from genesis.storage.backup import BackupError, BackupFailure, BackupReport, RestoreReport
from genesis.storage.sqlite import SQLiteStore
import test_desktop as helpers


class DesktopBackupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="genesis-ui-backup-synthetic-")
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        self.source = self.base / "source"

    def finish(self, worker):
        worker.close()
        self.assertTrue(worker.join(5), "synthetic worker failed to close")
        worker.poll_events()

    def report(self, directory):
        return BackupReport(Path(directory), UUID(int=8101), "a" * 64, 1, 1, 0, "b" * 64,
                            "2026-11-05T00:00:00Z")

    def window(self, *, preview=False, model=None, runner=None, factory=None):
        root, tk, modules = helpers.DesktopStartupCleanupTests().fake_tk()
        model = FakeModel() if model is None else model
        chat_workers, data_workers, requests = [], [], []

        def remember(worker, collection):
            collection.append(worker)
            self.addCleanup(self.finish, worker)
            return worker

        def temporary(config):
            return remember(ChatWorker(lambda: nullcontext(model)), chat_workers)

        def persistent(config, data):
            return remember(ChatWorker(lambda: nullcontext(model),
                store_factory=lambda: SQLiteStore(data.root, data_kind="local_preview", mode=data.action),
                startup=data.startup, resume_session_id=data.session_id), chat_workers)

        def data_factory(request):
            requests.append(request)
            if factory is not None:
                return factory(request)
            return remember(DataTaskWorker(request, operation_runner=runner), data_workers)

        data = DesktopDataConfig("local_preview", str(self.source), "create", acknowledged=True) if preview else None
        with patch.dict("sys.modules", modules):
            window = DesktopWindow(root, data=data, worker_factory=temporary,
                                   persistent_worker_factory=persistent, data_worker_factory=data_factory)
        window.input.get.return_value = ""
        self.receive(window)
        return SimpleNamespace(window=window, root=root, tk=tk, modules=modules, model=model,
                               chats=chat_workers, tasks=data_workers, requests=requests)

    def receive(self, window):
        self.assertTrue(window.session.worker.wait_for_events(5))
        window.session.pump()

    def finish_task(self, bundle):
        worker = bundle.window._data_worker
        self.assertIsNotNone(worker)
        self.assertTrue(worker.join(5))
        bundle.window._poll_data_task()
        self.assertIsNone(bundle.window._data_worker)
        return worker

    def scroll_window(self):
        """Control geometry without creating Tk or acquiring a data resource."""
        window = DesktopWindow.__new__(DesktopWindow)
        window._fatal_exit_requested = window._exiting = False
        window.backup_canvas, window.backup_content, window.backup_scrollbar = Mock(), Mock(), Mock()
        canvas = window.backup_canvas
        canvas.winfo_ismapped.return_value = True
        canvas.winfo_height.return_value = 102  # A 100px viewport inside its focus border.
        canvas.winfo_rooty.return_value = 200
        canvas.bbox.return_value = (0, 0, 400, 1000)
        canvas.canvasy.side_effect = lambda y: 99 + y
        canvas.focus_get.return_value = None
        window._backup_content_id = 7
        window._backup_view_width = 0
        window._backup_content_size = None
        window._backup_wrapped_labels = (Mock(), Mock())
        window._backup_scroll_widgets = {canvas, window.backup_content, window.backup_scrollbar}
        window._backup_focus_widgets = {canvas, window.backup_scrollbar}
        window._backup_geometry_widgets = set()
        window._backup_geometry = {}
        return window

    def test_default_is_idle_without_backup_and_paths_require_explicit_acknowledgment(self):
        bundle = self.window()
        window = bundle.window
        self.assertEqual(bundle.tasks, [])
        self.assertFalse(window.backup_plaintext_var.get())
        self.assertIn("临时会话", window.backup_source_var.get())
        self.assertEqual(window.backup_button.configure.call_args.kwargs["state"], "disabled")
        window.backup_target_var.set(str(self.base / "new-backup"))
        window.backup_plaintext_var.set(True)
        window.backup_current()
        self.assertEqual(bundle.tasks, [])
        window.backup_input_var.set("relative")
        window.verify_selected_backup()
        self.assertIn("设置无效", window.data_task_status_var.get())
        window.backup_input_var.set(str(self.base / "synthetic-backup"))
        window.restore_target_var.set(str(self.base / "new-restore"))
        window.backup_plaintext_var.set(False)
        window.restore_selected_backup()
        self.assertEqual(bundle.tasks, [])
        self.assertEqual(bundle.model.requests, ())

    def test_new_destination_picker_only_fills_a_path_and_modal_fatal_cannot_start_work(self):
        bundle = self.window()
        window = bundle.window
        window.filedialog.askdirectory.return_value = str(self.base)
        window.choose_data_task_path("backup_target")
        destination = Path(window.backup_target_var.get())
        self.assertEqual(destination.parent, self.base)
        self.assertFalse(destination.exists())
        self.assertTrue(window.filedialog.askdirectory.call_args.kwargs["mustexist"])
        window.backup_plaintext_var.set(True)
        window._data_task_settings_changed()
        self.assertFalse(window.backup_plaintext_var.get())
        original = window.restore_target_var.get()

        def fail_inside_dialog(**kwargs):
            window._request_fatal_exit(RuntimeError("synthetic modal failure"))
            return str(self.base)

        window.filedialog.askdirectory.side_effect = fail_inside_dialog
        window.choose_data_task_path("restore_target")
        self.assertEqual(window.restore_target_var.get(), original)
        self.assertEqual(bundle.tasks, [])
        bundle.root.destroy.assert_called_once()

    def test_backup_uses_applied_root_and_rejects_unapplied_storage_edits(self):
        captured = []
        def runner(request):
            captured.append(request)
            return self.report(request.destination_root)

        bundle = self.window(preview=True, runner=runner)
        window = bundle.window
        window.backup_target_var.set(str(self.base / "backup"))
        window.backup_plaintext_var.set(True)
        window.data_root_var.set(str(self.base / "draft-root"))
        window.backup_current()
        self.assertEqual(bundle.tasks, [])
        self.assertFalse((self.base / "draft-root").exists())
        window.data_root_var.set(str(self.source))
        window.preview_ack_var.set(True)
        window.backup_current()
        self.finish_task(bundle)
        self.assertEqual(captured[0].source_root, str(self.source))
        self.assertEqual(captured[0].destination_root, str(self.base / "backup"))
        self.assertEqual(bundle.model.requests, ())
        self.assertEqual(window.backup_input_var.get(), str(self.base / "backup"))

    def test_busy_task_freezes_request_and_preserves_draft_and_failed_retry(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        captured = []
        def runner(request):
            captured.append(request)
            entered.set()
            if not release.wait(5):
                raise AssertionError("synthetic task release missing")
            return self.report(request.source_root)

        model = FakeModel([ModelError("synthetic generation failure")])
        bundle = self.window(model=model, runner=runner)
        window = bundle.window
        window.session.send("  synthetic original\n原文  ")
        self.receive(window)
        saved_state = window.session.state
        self.assertTrue(saved_state.can_retry)
        window.input.get.return_value = "synthetic unsent draft"
        deletes = window.input.delete.call_count
        window.backup_input_var.set(str(self.base / "backup"))
        window.verify_selected_backup()
        self.assertTrue(entered.wait(5))
        worker = window._data_worker
        try:
            self.assertEqual(window.send_button.configure.call_args.kwargs["state"], "disabled")
            self.assertEqual(window.retry_button.configure.call_args.kwargs["state"], "disabled")
            self.assertEqual(window.new_button.configure.call_args.kwargs["state"], "disabled")
            window.backup_input_var.set(str(self.base / "edited-backup"))
            window.verify_selected_backup()
            window.send()
            window.retry()
            window.retry_selected()
            window.new_session()
            window.open_preview()
            self.assertEqual(len(bundle.tasks), 1)
            self.assertEqual(len(bundle.chats), 1)
            self.assertEqual(len(model.requests), 1)
            self.assertEqual(captured[0].source_root, str(self.base / "backup"))
        finally:
            release.set()
            self.assertTrue(worker.join(5))
        window._poll_data_task()
        self.assertEqual(window.session.state, saved_state)
        self.assertEqual(window.input.delete.call_count, deletes)
        self.assertEqual(window.input.get(), "synthetic unsent draft")
        self.assertEqual(window.retry_button.configure.call_args.kwargs["state"], "normal")

    def test_real_synthetic_backup_verify_restore_requires_explicit_open_and_never_generates(self):
        bundle = self.window(preview=True)
        window = bundle.window
        window.session.send("合成备份验收\n保留原文")
        self.receive(window)
        sid, history = window.session.state.session_id, window.session._shown.copy()
        source_session = window.session
        destination, restored = self.base / "backup", self.base / "restored"
        window.backup_target_var.set(str(destination))
        window.backup_plaintext_var.set(True)
        window.backup_current()
        self.finish_task(bundle)
        self.assertIn("备份完成并通过验证", window.data_task_status_var.get())
        window.backup_plaintext_var.set(False)
        window.verify_selected_backup()
        self.finish_task(bundle)
        self.assertIn("完整性及格式验证通过", window.data_task_status_var.get())
        window.restore_target_var.set(str(restored))
        window.backup_plaintext_var.set(True)
        window.restore_selected_backup()
        self.finish_task(bundle)
        self.assertIn("恢复完成，尚未打开", window.data_task_status_var.get())
        self.assertIs(window.session, source_session)
        self.assertEqual(window.session._shown, history)
        self.assertEqual(window._active_data.root, str(self.source))
        self.assertEqual(len(bundle.model.requests), 1)
        window.restore_target_var.set(str(self.base / "unapplied-restore"))
        window.open_restored_preview()
        self.assertTrue(source_session.worker.join(5))
        source_session.pump()
        self.receive(window)
        self.assertEqual(window._active_data.root, str(restored))
        self.assertEqual(window.session.state.phase, "browse")
        self.assertIn(sid, window.session.state.session_ids)
        self.assertEqual(len(bundle.model.requests), 1)
        with SQLiteStore(self.source, data_kind="local_preview", mode="open") as original:
            with SQLiteStore(restored, data_kind="local_preview", mode="open") as restored_store:
                self.assertEqual(original.identity, restored_store.identity)
                self.assertEqual(original.get_session(sid), restored_store.get_session(sid))

    def test_existing_empty_destination_is_rejected_without_overwrite_or_chat_change(self):
        bundle = self.window(preview=True)
        window = bundle.window
        target = self.base / "existing"
        target.mkdir()
        original = window.session.state
        window.backup_target_var.set(str(target))
        window.backup_plaintext_var.set(True)
        window.backup_current()
        self.finish_task(bundle)
        self.assertIn("空目录也不能覆盖", window.data_task_status_var.get())
        self.assertEqual(tuple(target.iterdir()), ())
        self.assertEqual(window.session.state, original)
        self.assertEqual(bundle.model.requests, ())

    def test_safe_operation_errors_preserve_chat_and_do_not_expose_original_details(self):
        marker = "SYNTHETIC_PRIVATE_DATA_ERROR"
        for reason in BackupFailure:
            with self.subTest(reason=reason):
                def runner(request):
                    error = BackupError(reason)
                    error.add_note(marker)
                    raise error from RuntimeError(marker)

                bundle = self.window(runner=runner)
                window = bundle.window
                state = window.session.state
                window.backup_input_var.set(str(self.base / "synthetic-backup"))
                window.verify_selected_backup()
                self.finish_task(bundle)
                self.assertEqual(window.session.state, state)
                self.assertEqual(bundle.model.requests, ())
                self.assertNotIn(marker, window.data_task_status_var.get())
                self.assertIn(window.data_task_status_var.get(), window.error_var.get())
                self.assertNotIn(str(self.base), window.data_task_status_var.get())
                if reason.value == "outcome_unknown":
                    self.assertIn("目标可能已经完成", window.data_task_status_var.get())
                self.finish(window.session.worker)

    def test_normal_close_waits_for_both_workers_without_waiting_in_callback(self):
        entered, release, cleaned = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        owners = []
        def runner(request):
            owners.append(threading.get_ident())
            entered.set()
            try:
                if not release.wait(5):
                    raise AssertionError("synthetic task release missing")
                return self.report(request.source_root)
            finally:
                owners.append(threading.get_ident())
                cleaned.set()

        bundle = self.window(runner=runner)
        window = bundle.window
        window.backup_input_var.set(str(self.base / "backup"))
        window.verify_selected_backup()
        self.assertTrue(entered.wait(5))
        worker = window._data_worker
        try:
            window.request_close()
            self.assertFalse(cleaned.is_set())
            self.assertTrue(worker.is_alive)
            self.assertIn("未取消数据操作", window.status_var.get())
            self.assertTrue(window.session.worker.join(5))
            window.session.pump()
            bundle.root.destroy.assert_not_called()
        finally:
            release.set()
            self.assertTrue(worker.join(5))
        window._poll_data_task()
        self.assertTrue(cleaned.is_set())
        bundle.root.destroy.assert_called_once()
        self.assertEqual(owners[0], owners[1])
        self.assertNotEqual(owners[0], threading.get_ident())

    def test_callwrapper_fatal_closes_both_owners_without_future_poll(self):
        for origin in ("poll", "scrollbar", "canvas_configure", "scroll_position", "local_configure"):
            with self.subTest(origin=origin):
                entered, release = threading.Event(), threading.Event()
                self.addCleanup(release.set)
                def runner(request):
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("synthetic task release missing")
                    return self.report(request.source_root)

                bundle = self.window(runner=runner)
                window = bundle.window
                window.backup_input_var.set(str(self.base / "backup"))
                window.verify_selected_backup()
                self.assertTrue(entered.wait(5))
                worker = window._data_worker
                failure = RuntimeError("synthetic callback failure")
                if origin == "poll":
                    window.session.pump = Mock(side_effect=failure)
                    callback = window._poll
                elif origin == "scrollbar":
                    window.backup_canvas.yview.side_effect = failure
                    callback = lambda: window._backup_scrollbar("moveto", "0.5")
                elif origin == "canvas_configure":
                    window.backup_canvas.itemconfigure.side_effect = failure
                    callback = lambda: window._backup_canvas_configure(SimpleNamespace(width=502))
                elif origin == "local_configure":
                    window.backup_canvas.focus_get.return_value = window.backup_button
                    window._backup_reveal_focus = Mock(side_effect=failure)
                    callback = lambda: window._backup_local_configure(SimpleNamespace(
                        widget=window.backup_button, x=0, y=0, width=100, height=30))
                else:
                    window.backup_scrollbar.set.side_effect = failure
                    callback = lambda: window._backup_scroll_position("0.1", "0.3")
                scheduled = bundle.root.after.call_count
                try:
                    CallWrapper(callback, None, bundle.root)()
                    self.assertTrue(window._fatal_exit_requested)
                    bundle.root.quit.assert_called_once()
                    bundle.root.destroy.assert_called_once()
                    self.assertTrue(window.session.worker.join(5))
                    self.assertTrue(worker.is_alive)
                    self.assertEqual(worker.state, "closing")
                    count = len(bundle.tasks)
                    window.verify_selected_backup()
                    window._poll()
                    self.assertEqual(len(bundle.tasks), count)
                    self.assertEqual(bundle.root.after.call_count, scheduled)
                finally:
                    release.set()
                    self.assertTrue(worker.join(5))

    def test_scroll_width_updates_once_and_content_resize_only_refreshes_scroll_region(self):
        window = self.scroll_window()
        canvas = window.backup_canvas
        for width in (0, 1, 2):
            window._backup_canvas_configure(SimpleNamespace(width=width))
        canvas.itemconfigure.assert_not_called()
        window._backup_canvas_configure(SimpleNamespace(width=402))
        canvas.itemconfigure.assert_called_once_with(7, width=400)
        for label in window._backup_wrapped_labels:
            label.configure.assert_called_once_with(wraplength=384)
        window._backup_canvas_configure(SimpleNamespace(width=402))
        self.assertEqual(canvas.itemconfigure.call_count, 1)
        self.assertTrue(all(label.configure.call_count == 1 for label in window._backup_wrapped_labels))
        # A height-only shrink must reveal focus without starting a width/wrap
        # feedback loop. Coordinates below refer to the fixed content system.
        focused = Mock()
        focused.winfo_rooty.return_value = 200 + 175 - 99
        focused.winfo_height.return_value = 20
        focused.winfo_ismapped.return_value = True
        window._backup_scroll_widgets.add(focused)
        window._backup_focus_widgets.add(focused)
        canvas.focus_get.return_value = focused
        canvas.winfo_height.return_value = 82
        window._backup_canvas_configure(SimpleNamespace(width=402))
        canvas.yview_moveto.assert_called_once()
        self.assertAlmostEqual(canvas.yview_moveto.call_args.args[0], 0.119)
        self.assertEqual(canvas.itemconfigure.call_count, 1)
        self.assertTrue(all(label.configure.call_count == 1 for label in window._backup_wrapped_labels))
        canvas.focus_get.return_value = None
        canvas.winfo_height.return_value = 102
        window._backup_content_configure()
        canvas.configure.assert_called_once_with(scrollregion=(0, 0, 400, 1000))
        self.assertEqual(canvas.itemconfigure.call_count, 1)
        window._backup_canvas_configure(SimpleNamespace(width=10))
        canvas.itemconfigure.assert_called_with(7, width=8)
        for label in window._backup_wrapped_labels:
            label.configure.assert_called_with(wraplength=1)
        # A hidden notebook page may receive useful geometry configuration;
        # it must update width but must not try to move focus or scroll.
        canvas.winfo_ismapped.return_value = False
        canvas.yview_moveto.reset_mock()
        window._backup_canvas_configure(SimpleNamespace(width=602))
        canvas.itemconfigure.assert_called_with(7, width=600)
        for label in window._backup_wrapped_labels:
            label.configure.assert_called_with(wraplength=584)
        canvas.yview_moveto.assert_not_called()
        canvas.bbox.return_value = None
        window._backup_content_configure()
        self.assertEqual(canvas.configure.call_count, 1)
        canvas.after_idle.assert_not_called()
        canvas.update.assert_not_called()
        canvas.update_idletasks.assert_not_called()
        # Moving the embedded window during a wheel scroll may emit Configure
        # without a size change. It must not snap back to an offscreen Entry.
        canvas.winfo_ismapped.return_value = True
        canvas.bbox.return_value = (0, 0, 400, 1000)
        canvas.focus_get.return_value = focused
        window._backup_content_configure(SimpleNamespace(width=400, height=1000, y=0))
        canvas.yview_moveto.reset_mock()
        calls = canvas.configure.call_count
        window._backup_content_configure(SimpleNamespace(width=400, height=1000, y=-200))
        self.assertEqual(canvas.configure.call_count, calls)
        canvas.yview_moveto.assert_not_called()

    def test_focus_reveal_moves_only_as_needed_clamps_and_never_steals_focus(self):
        window = self.scroll_window()
        canvas = window.backup_canvas
        for top, height, expected in ((124, 30, None), (80, 20, 0.076), (190, 20, 0.114),
                                      (300, 180, 0.296), (1, 20, 0.0), (990, 30, 0.9)):
            with self.subTest(top=top, height=height):
                widget = Mock()
                widget.winfo_rooty.return_value = 200 + top - 99
                widget.winfo_height.return_value = height
                widget.winfo_ismapped.return_value = True
                window._backup_scroll_widgets.add(widget)
                window._backup_focus_widgets.add(widget)
                canvas.yview_moveto.reset_mock()
                window._backup_scroll_focus(SimpleNamespace(widget=widget))
                if expected is None:
                    canvas.yview_moveto.assert_not_called()
                else:
                    self.assertEqual(canvas.yview_moveto.call_count, 1)
                    self.assertAlmostEqual(canvas.yview_moveto.call_args.args[0], expected)
                widget.focus_set.assert_not_called()
                canvas.focus_set.assert_not_called()
        canvas.yview_moveto.reset_mock()
        for widget in (None, Mock(), canvas, window.backup_scrollbar):
            window._backup_scroll_focus(SimpleNamespace(widget=widget))
        hidden = Mock()
        hidden.winfo_ismapped.return_value = False
        window._backup_scroll_widgets.add(hidden)
        window._backup_focus_widgets.add(hidden)
        window._backup_scroll_focus(SimpleNamespace(widget=hidden))
        canvas.yview_moveto.assert_not_called()
        canvas.winfo_ismapped.return_value = False
        window._backup_scroll_focus(SimpleNamespace(widget=window.backup_content))
        canvas.yview_moveto.assert_not_called()
        canvas.after_idle.assert_not_called()
        canvas.update.assert_not_called()

    def test_wheel_bindings_stay_local_preserve_entry_editing_and_ignore_modifiers_or_hidden_page(self):
        bundle = self.window()
        window, canvas = bundle.window, bundle.window.backup_canvas
        self.assertNotIn("yscrollincrement", bundle.tk.Canvas.call_args.kwargs)
        controls = (*window.backup_path_entries, *window.backup_path_buttons,
                    window.backup_plaintext_check, window.backup_button,
                    window.verify_backup_button, window.restore_backup_button, window.open_restored_button)
        self.assertTrue(set(controls) <= window._backup_scroll_widgets)
        self.assertEqual(window._backup_focus_widgets, set(controls) | {canvas, window.backup_scrollbar})
        self.assertTrue(set(controls) <= window._backup_geometry_widgets)
        self.assertFalse({canvas, window.backup_content, window.backup_scrollbar} & window._backup_geometry_widgets)
        for widget in window._backup_scroll_widgets:
            bound = {call.args[0] for call in widget.bind.call_args_list}
            self.assertTrue({"<MouseWheel>", "<Button-4>", "<Button-5>"} <= bound)
            if widget in window._backup_focus_widgets:
                self.assertIn("<FocusIn>", bound)
            else:
                # Tk's native Tab traversal can make a decorative widget
                # focusable merely because it has a Key or Focus binding.
                self.assertFalse(any("Key" in sequence or "Focus" in sequence for sequence in bound))
                self.assertFalse({"<Prior>", "<Next>", "<Home>", "<End>"} & bound)
            widget.bind_all.assert_not_called()
        for entry in window.backup_path_entries:
            bound = {call.args[0] for call in entry.bind.call_args_list}
            self.assertFalse({"<Prior>", "<Next>", "<Home>", "<End>", "<KeyPress>"} & bound)
        bundle.root.bind_all.assert_not_called()
        canvas.bbox.return_value = (0, 0, 400, 1000)
        canvas.winfo_height.return_value = 102
        canvas.canvasy.side_effect = lambda y: 99 + y
        entry = window.backup_path_entries[0]
        for number, delta, direction in ((None, 120, -1), (None, -120, 1), (None, 1, -1),
                                          (None, 240, -2), (4, 0, -1), (5, 0, 1)):
            with self.subTest(number=number, delta=delta):
                canvas.yview_scroll.reset_mock()
                result = window._backup_scroll_wheel(SimpleNamespace(widget=entry, num=number, delta=delta, state=0))
                self.assertEqual(result, "break")
                self.assertEqual(canvas.yview_scroll.call_count, 1)
                amount, units = canvas.yview_scroll.call_args.args
                self.assertEqual(amount, direction)
                self.assertEqual(units, "units")
        canvas.yview_scroll.reset_mock()
        for modifier in (1, 4, 8, 13, 0x20000, 0x2000D):
            self.assertIsNone(window._backup_scroll_wheel(SimpleNamespace(widget=entry, delta=120, state=modifier)))
        self.assertIsNone(window._backup_scroll_wheel(SimpleNamespace(widget=window.input, delta=120, state=0)))
        canvas.winfo_ismapped.return_value = False
        self.assertIsNone(window._backup_scroll_wheel(SimpleNamespace(widget=entry, delta=120, state=0)))
        canvas.winfo_ismapped.return_value = True
        for top, delta in ((0, 120), (900, -120)):
            canvas.canvasy.side_effect = lambda y, top=top: top - 1 + y
            self.assertEqual(window._backup_scroll_wheel(SimpleNamespace(widget=entry, delta=delta, state=0)), "break")
        for region in (None, (0, 0, 400, 80)):
            canvas.bbox.return_value = region
            self.assertEqual(window._backup_scroll_wheel(SimpleNamespace(widget=entry, delta=120, state=0)), "break")
        canvas.yview_scroll.assert_not_called()
        self.assertEqual(bundle.model.requests, ())
        self.assertEqual(bundle.tasks, [])

    def test_local_layout_change_reveals_final_control_bounds_without_undoing_wheel_scroll(self):
        window = self.scroll_window()
        canvas = window.backup_canvas
        visible_top, actions_y, button_height = [100.0], [195], [30]
        actions, button, other_row = Mock(), Mock(), Mock()
        actions.master = other_row.master = window.backup_content
        button.master = actions
        button.winfo_ismapped.return_value = True
        button.winfo_height.side_effect = lambda: button_height[0]
        button.winfo_rooty.side_effect = lambda: 200 + actions_y[0] + 5 - (visible_top[0] - 1)
        window._backup_scroll_widgets.update((actions, button, other_row))
        window._backup_focus_widgets.add(button)
        window._backup_geometry_widgets.update((actions, button, other_row))
        canvas.canvasy.side_effect = lambda y: visible_top[0] - 1 + y
        canvas.yview_moveto.side_effect = lambda fraction: visible_top.__setitem__(0, fraction * 1000)
        initial = SimpleNamespace(widget=actions, x=0, y=195, width=400, height=40)
        window._backup_local_configure(initial)
        self.assertEqual(window._backup_geometry[actions], (0, 195, 400, 40))
        canvas.yview_moveto.assert_not_called()
        canvas.focus_get.return_value = button
        # Outer Configure arrives before pack has finished the button row.
        window._backup_canvas_configure(SimpleNamespace(width=402))
        window._backup_content_configure(SimpleNamespace(width=400, height=1000))
        self.assertEqual(canvas.yview_moveto.call_count, 1)
        self.assertAlmostEqual(visible_top[0], 134)
        canvas.yview_moveto.reset_mock()
        # The final parent-row move does not change the button's local y;
        # observing that ancestor must reveal the new bottom, including margin.
        actions_y[0] = 260
        moved = SimpleNamespace(widget=actions, x=0, y=260, width=400, height=40)
        window._backup_local_configure(moved)
        canvas.yview_moveto.assert_called_once()
        self.assertAlmostEqual(visible_top[0], 199)
        self.assertLessEqual(actions_y[0] + 5 + button_height[0] + 4, visible_top[0] + 100)
        canvas.yview_moveto.reset_mock()
        window._backup_geometry[button] = (10, 5, 100, 30)
        button_height[0] = 40
        resized = SimpleNamespace(widget=button, x=10, y=5, width=100, height=40)
        window._backup_local_configure(resized)
        canvas.yview_moveto.assert_called_once()
        self.assertAlmostEqual(visible_top[0], 209)
        # Wheel motion changes screen coordinates but not local geometry.
        # Repeated local/whole-content events must preserve the user's scroll.
        visible_top[0] = 350
        canvas.yview_moveto.reset_mock()
        window._backup_local_configure(moved)
        window._backup_local_configure(resized)
        window._backup_content_configure(SimpleNamespace(width=400, height=1000, y=-350))
        for excluded in (window.backup_content, canvas, window.backup_scrollbar, Mock()):
            window._backup_local_configure(SimpleNamespace(widget=excluded, x=0, y=-350, width=400, height=1000))
        window._backup_local_configure(SimpleNamespace(widget=other_row, x=0, y=600, width=400, height=40))
        self.assertEqual(visible_top[0], 350)
        canvas.yview_moveto.assert_not_called()
        canvas.focus_get.return_value = Mock()  # Another notebook page owns focus.
        window._backup_local_configure(SimpleNamespace(widget=actions, x=0, y=280, width=400, height=40))
        # A decorative widget remains outside the focus whitelist even though
        # it participates in local wheel events and ancestor geometry tracking.
        window._backup_scroll_focus(SimpleNamespace(widget=actions))
        canvas.yview_moveto.assert_not_called()
        for widget in (canvas, button, actions, other_row):
            widget.focus_set.assert_not_called()
        canvas.after_idle.assert_not_called()
        canvas.update.assert_not_called()

    def test_scroll_keyboard_and_scrollbar_commands_are_scoped_and_stop_during_exit(self):
        window = self.scroll_window()
        canvas = window.backup_canvas
        entry = Mock()
        window._backup_scroll_widgets.add(entry)
        window._backup_focus_widgets.add(entry)
        for key in ("Prior", "Next", "Home", "End"):
            self.assertIsNone(window._backup_scroll_key(SimpleNamespace(widget=entry, keysym=key)))
        canvas.yview_scroll.assert_not_called()
        canvas.yview_moveto.assert_not_called()
        for key, amount in (("Prior", -1), ("Next", 1)):
            self.assertEqual(window._backup_scroll_key(SimpleNamespace(widget=canvas, keysym=key)), "break")
            canvas.yview_scroll.assert_called_with(amount, "pages")
        for key, fraction in (("Home", 0), ("End", 1)):
            self.assertEqual(window._backup_scroll_key(SimpleNamespace(widget=canvas, keysym=key)), "break")
            canvas.yview_moveto.assert_called_with(fraction)
        self.assertIsNone(window._backup_scroll_key(SimpleNamespace(widget=canvas, keysym="Left")))
        window._backup_canvas_focus(SimpleNamespace(widget=entry))
        canvas.focus_set.assert_not_called()
        window._backup_canvas_focus(SimpleNamespace(widget=canvas))
        canvas.focus_set.assert_called_once()
        window._backup_scrollbar("moveto", "0.25")
        canvas.yview.assert_called_once_with("moveto", "0.25")
        window._backup_scroll_position("0.25", "0.35")
        window.backup_scrollbar.set.assert_called_once_with("0.25", "0.35")
        for flag in ("_exiting", "_fatal_exit_requested"):
            with self.subTest(flag=flag):
                canvas.reset_mock()
                window.backup_scrollbar.reset_mock()
                setattr(window, flag, True)
                window._backup_canvas_configure(SimpleNamespace(width=502))
                window._backup_content_configure()
                window._backup_local_configure(SimpleNamespace(widget=entry, x=0, y=0, width=100, height=20))
                window._backup_scroll_focus(SimpleNamespace(widget=entry))
                window._backup_canvas_focus(SimpleNamespace(widget=canvas))
                window._backup_scroll_wheel(SimpleNamespace(widget=canvas, delta=120, state=0))
                window._backup_scroll_key(SimpleNamespace(widget=canvas, keysym="Next"))
                window._backup_scrollbar("moveto", "0.5")
                window._backup_scroll_position("0.5", "0.6")
                canvas.assert_not_called()
                self.assertEqual(canvas.mock_calls, [])
                self.assertEqual(window.backup_scrollbar.mock_calls, [])
                setattr(window, flag, False)

    def test_data_factory_failure_preserves_exception_and_cleans_chat_owner(self):
        for error in (RuntimeError("synthetic factory failure"), KeyboardInterrupt(), SystemExit(9)):
            with self.subTest(error=type(error).__name__):
                def factory(request):
                    raise error
                bundle = self.window(factory=factory)
                window = bundle.window
                window.backup_input_var.set(str(self.base / "backup"))
                with self.assertRaises(type(error)) as caught:
                    window.verify_selected_backup()
                self.assertIs(caught.exception, error)
                self.assertTrue(window._fatal_exit_requested)
                self.assertIsNone(window._data_worker)
                self.assertTrue(window.session.worker.join(5))
                bundle.root.destroy.assert_called_once()

    def test_render_failure_after_data_worker_start_releases_both_owners(self):
        for error in (RuntimeError("synthetic render failure"), KeyboardInterrupt(), SystemExit(10)):
            with self.subTest(error=type(error).__name__):
                entered, release, cleaned = threading.Event(), threading.Event(), threading.Event()
                def runner(request):
                    entered.set()
                    try:
                        if not release.wait(5):
                            raise AssertionError("synthetic task release missing")
                        return self.report(request.source_root)
                    finally:
                        cleaned.set()
                bundle = self.window(runner=runner)
                window = bundle.window
                window.backup_input_var.set(str(self.base / "backup"))
                def failed_render(state):
                    self.assertTrue(entered.wait(5))
                    raise error
                window.render = failed_render
                try:
                    with self.assertRaises(type(error)) as caught:
                        window.verify_selected_backup()
                    self.assertIs(caught.exception, error)
                    self.assertTrue(window._fatal_exit_requested)
                    self.assertTrue(window.session.worker.join(5))
                    bundle.root.destroy.assert_called_once()
                finally:
                    release.set()
                    self.assertTrue(bundle.tasks[0].join(5))
                self.assertTrue(cleaned.is_set())

    def test_main_finally_requests_cleanup_of_data_worker_after_early_loop_return(self):
        for error in (None, RuntimeError("synthetic mainloop failure"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                entered, release = threading.Event(), threading.Event()
                def runner(request):
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("synthetic task release missing")
                    return self.report(request.source_root)
                bundle = self.window(runner=runner)
                window = bundle.window
                window.backup_input_var.set(str(self.base / "backup"))
                window.verify_selected_backup()
                self.assertTrue(entered.wait(5))
                worker = window._data_worker
                bundle.root.mainloop.side_effect = error
                try:
                    with patch.dict("sys.modules", bundle.modules), patch(
                            "genesis.adapters.desktop.DesktopWindow", return_value=window):
                        if error is None:
                            self.assertEqual(main([]), 0)
                        else:
                            with self.assertRaises(type(error)) as caught:
                                main([])
                            self.assertIs(caught.exception, error)
                    self.assertTrue(window.session.worker.join(5))
                    self.assertEqual(worker.state, "closing")
                    bundle.root.destroy.assert_called_once()
                finally:
                    release.set()
                    self.assertTrue(worker.join(5))


if __name__ == "__main__":
    unittest.main()
