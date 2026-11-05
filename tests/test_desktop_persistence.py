"""Headless local-preview selection and recovery; every input is synthetic."""
from contextlib import nullcontext, redirect_stderr
from dataclasses import FrozenInstanceError, replace
from io import StringIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

from genesis.adapters.desktop import (DesktopDataConfig, DesktopSession, DesktopWindow,
    ViewState, main, persistent_worker, unfinished_label)
from genesis.adapters.desktop_backend import BackendConfig
from genesis.adapters.desktop_worker import ChatEvent, ChatWorker
from genesis.core import GenesisCore, Turn, TurnGenerationError, UserInput
from genesis.models.contracts import Message, ModelError, Role
from genesis.models.fake import FakeModel
from genesis.storage.sqlite import AttemptStatus, SQLiteStore, UnfinishedRequest
import test_desktop as helpers


class SavedModel(FakeModel):
    @property
    def model_id(self):
        return "synthetic-saved-model"


class DesktopPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="genesis-desktop-preview-synthetic-")
        self.addCleanup(self.folder.cleanup)
        self.base = Path(self.folder.name)
        self.root = self.base / "preview"

    def finish(self, worker):
        worker.close()
        self.assertTrue(worker.join(5))
        worker.poll_events()

    def seed(self):
        sid = UUID(int=5101)
        first = UserInput(UUID(int=5102), "合成已保存输入")
        failed = UserInput(UUID(int=5103), "  合成失败原文\n保留空格  ")
        running = UserInput(UUID(int=5104), "合成执行结果未确认")
        with SQLiteStore(self.root, data_kind="local_preview", mode="create") as store:
            model = SavedModel(["合成已保存回复", ModelError("synthetic failure")])
            core = GenesisCore(store.identity, model, store=store)
            core.create_session(sid)
            turn = core.send(sid, first)
            with self.assertRaises(TurnGenerationError):
                core.send(sid, failed)
            store.begin_attempt(sid, running)
        return sid, turn, failed, running

    def presenter(self, *, startup="resume", session_id=None, model=None, root=None):
        model = FakeModel() if model is None else model
        target = self.root if root is None else root
        worker = ChatWorker(lambda: nullcontext(model),
            store_factory=lambda: SQLiteStore(target, data_kind="local_preview", mode="open"),
            startup=startup, resume_session_id=session_id)
        self.addCleanup(self.finish, worker)
        view = helpers.RecordingView()
        session = DesktopSession(worker, view)
        self.receive(session)
        return session, view, model

    def receive(self, session):
        self.assertTrue(session.worker.wait_for_events(5))
        session.pump()

    def window(self, *, data=None, model=None):
        root, tk, modules = helpers.DesktopStartupCleanupTests().fake_tk()
        def widget(master, **options):
            instance = Mock()
            instance.master = master
            instance.construction_options = options
            instance.winfo_children.return_value = []
            if isinstance(master.winfo_children.return_value, list):
                master.winfo_children.return_value.append(instance)
            return instance
        tk.ttk.Label.side_effect = widget
        tk.ttk.Notebook.side_effect = widget
        model = FakeModel() if model is None else model
        workers, calls = [], []

        def remember(worker):
            workers.append(worker)
            self.addCleanup(self.finish, worker)
            return worker

        def temporary_factory(config):
            return remember(ChatWorker(lambda: nullcontext(model)))

        def preview_factory(config, selection):
            calls.append(selection)
            return remember(ChatWorker(lambda: nullcontext(model),
                store_factory=lambda: SQLiteStore(selection.root, data_kind="local_preview", mode=selection.action),
                startup=selection.startup, resume_session_id=selection.session_id))

        with patch.dict("sys.modules", modules):
            window = DesktopWindow(root, data=data, worker_factory=temporary_factory,
                                   persistent_worker_factory=preview_factory)
        window.input.get.return_value = ""
        self.receive(window.session)
        return window, root, model, workers, calls

    def finish_switch(self, window):
        previous = window.session
        self.assertTrue(previous.worker.join(5))
        previous.pump()
        self.assertIsNot(window.session, previous)
        self.receive(window.session)

    def select_preview(self, window, root=None, ack=True):
        window.data_mode_var.set("本地预览，保存记录")
        window.data_root_var.set(str(root or self.root))
        window.preview_ack_var.set(ack)

    def test_default_and_configuration_validation_never_open_storage(self):
        with patch("genesis.adapters.desktop.SQLiteStore", side_effect=AssertionError("unexpected storage")):
            self.assertEqual(DesktopDataConfig().mode, "temporary")
            config = DesktopDataConfig("local_preview", str(self.root), "create", acknowledged=True)
            with self.assertRaises(FrozenInstanceError):
                config.root = "changed"
            invalid = (
                {"mode": "synthetic_development"},
                {"mode": "local_preview", "root": str(self.root), "action": "create"},
                {"mode": "local_preview", "root": "relative", "action": "open", "acknowledged": True},
                {"mode": "local_preview", "root": str(self.root), "action": "create", "startup": "browse", "acknowledged": True},
                {"mode": "local_preview", "root": str(self.root), "action": "open", "startup": "resume", "acknowledged": True},
                {"mode": "temporary", "root": str(self.root)},
            )
            for kwargs in invalid:
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    DesktopDataConfig(**kwargs)
            window, _, _, _, calls = self.window()
            self.assertEqual(window._active_data.mode, "temporary")
            self.assertEqual(calls, [])
            self.assertIn("临时内存", window.data_status_var.get())
        self.assertFalse(self.root.exists())

    def test_invalid_cli_preview_combinations_fail_before_tk_or_storage(self):
        invalid = (["--create-preview", str(self.root)],
            ["--open-preview", str(self.root)], ["--preview-data-acknowledged"],
            ["--session", str(UUID(int=1))],
            ["--create-preview", str(self.root), "--preview-data-acknowledged", "--session", str(UUID(int=1))],
            ["--open-preview", "relative", "--preview-data-acknowledged"],
            ["--open-preview", str(self.root), "--preview-data-acknowledged", "--session", "invalid"],
            ["--open-preview", str(self.root), "--create-preview", str(self.root), "--preview-data-acknowledged"])
        for args in invalid:
            with self.subTest(args=args), patch("genesis.adapters.desktop.DesktopWindow") as window:
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as failure:
                    main(args)
                self.assertEqual(failure.exception.code, 2)
                window.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_cli_freezes_explicit_create_browse_or_resume_without_io(self):
        sid = UUID(int=5201)
        cases = ((["--create-preview", str(self.root)], "create", "new", None),
                 (["--open-preview", str(self.root)], "open", "browse", None),
                 (["--open-preview", str(self.root), "--session", str(sid)], "open", "resume", sid))
        for args, action, startup, expected_id in cases:
            with self.subTest(startup=startup):
                root = Mock()
                fake_tk = SimpleNamespace(Tk=lambda: root, TclError=type("TclError", (Exception,), {}))
                window = SimpleNamespace(session=SimpleNamespace(worker=Mock()))
                with patch.dict("sys.modules", {"tkinter": fake_tk}), patch(
                    "genesis.adapters.desktop.DesktopWindow", return_value=window
                ) as constructor, patch("genesis.adapters.desktop.SQLiteStore", side_effect=AssertionError("CLI validation wrote data")):
                    self.assertEqual(main(args + ["--preview-data-acknowledged"]), 0)
                data = constructor.call_args.kwargs["data"]
                self.assertEqual(data, DesktopDataConfig("local_preview", str(self.root), action, startup, expected_id, True))
        self.assertFalse(self.root.exists())

    def test_resume_displays_only_committed_history_and_preserves_unfinished_states(self):
        sid, turn, failed, running = self.seed()
        session, view, model = self.presenter(session_id=sid)
        self.assertEqual(session.state.session_id, sid)
        self.assertEqual(view.turns, [turn])
        self.assertEqual(model.requests, ())
        pending = {item.user_input.input_id: item for item in session.state.pending_requests}
        self.assertEqual(set(pending), {failed.input_id, running.input_id})
        self.assertEqual(pending[failed.input_id].attempts[-1].state, "failed")
        self.assertEqual(pending[running.input_id].attempts[-1].state, "running")
        self.assertIn("未确认", unfinished_label(pending[running.input_id]))
        self.assertFalse(session.state.can_retry)
        self.assertFalse(session.retry())
        self.assertEqual(model.requests, ())

    def test_browse_catalog_does_not_create_session_or_enable_sending(self):
        sid, _, _, _ = self.seed()
        session, view, model = self.presenter(startup="browse")
        self.assertEqual(session.state.phase, "browse")
        self.assertIsNone(session.state.session_id)
        self.assertEqual(session.state.session_ids, (sid,))
        self.assertEqual(view.turns, [])
        self.assertFalse(session.send("合成禁止发送"))
        self.assertFalse(session.retry(UUID(int=1)))
        self.assertEqual(model.requests, ())
        with SQLiteStore(self.root, data_kind="local_preview", mode="open") as store:
            self.assertEqual(store.session_ids(), (sid,))

    def test_selected_restored_retry_uses_original_id_text_and_commits_once(self):
        sid, turn, failed, _ = self.seed()
        session, view, model = self.presenter(session_id=sid)
        self.assertTrue(session.retry(failed.input_id))
        self.receive(session)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(model.requests[0].input_id, failed.input_id)
        self.assertEqual(model.requests[0].messages[-1].content, failed.content)
        self.assertEqual(view.turns[0], turn)
        self.assertEqual(len(view.turns), 2)
        self.assertFalse(session.retry(failed.input_id))
        self.assertEqual(len(model.requests), 1)
        self.assertNotIn(failed.input_id, [item.user_input.input_id for item in session.state.pending_requests])

    def test_presenter_merges_authoritative_history_and_recovered_commit_without_duplicate(self):
        sid = UUID(int=5301)
        first = Turn(UserInput(UUID(int=5302), "synthetic first"), Message(Role.ASSISTANT, "synthetic saved"), "saved-model")
        recovered = Turn(UserInput(UUID(int=5303), "synthetic current"), Message(Role.ASSISTANT, "synthetic winning reply"), "winning-model")
        worker = Mock(can_retry=False, is_alive=True)
        worker.poll_events.side_effect = [
            (ChatEvent("ready", session_id=sid, model_id="current-model", history=(first,)),),
            (ChatEvent("succeeded", input_id=recovered.user_input.input_id, turn=recovered,
                       session_id=sid, history=(first, recovered), recovered_commit=True),),
            (ChatEvent("failed", input_id=recovered.user_input.input_id, session_id=sid,
                       history=(first, recovered), error_code="model_error"),),
        ]
        view = helpers.RecordingView()
        session = DesktopSession(worker, view)
        session.pump()
        session.pump()
        self.assertEqual(view.turns, [first, recovered])
        self.assertIn("本次模型调用未成功", session.state.notice)
        session.pump()
        self.assertEqual(view.turns, [first, recovered])
        self.assertEqual(session.state.phase, "ready")
        self.assertIn("已恢复", session.state.notice)
        self.assertNotIn("没有加入", session.state.error)
        worker.retry.assert_not_called()

    def test_unapplied_root_edit_clears_ack_and_never_retargets_active_store(self):
        sid, _, _, _ = self.seed()
        data = DesktopDataConfig("local_preview", str(self.root), "open", "resume", sid, True)
        window, _, model, _, calls = self.window(data=data)
        original = window._active_data
        window.data_root_var.set(str(self.base / "another"))
        window._settings_changed(str(window.data_root_var))
        self.assertFalse(window.preview_ack_var.get())
        self.assertIs(window._active_data, original)
        self.assertEqual(calls, [original])
        self.assertIn(str(self.root), window.data_status_var.get())
        self.assertEqual(window.send_button.configure.call_args.kwargs["state"], "disabled")
        window.send()
        self.assertEqual(model.requests, ())
        self.assertFalse((self.base / "another").exists())

    def test_create_and_open_controls_require_ack_and_keep_actions_distinct(self):
        window, _, _, _, _ = self.window()
        self.select_preview(window, ack=False)
        window.create_preview()
        self.assertIsNone(window._next_data)
        self.assertIn("预览设置无效", window.error_var.get())
        self.assertFalse(self.root.exists())
        with patch.object(window, "_queue_start") as queue:
            window.preview_ack_var.set(True)
            window.create_preview()
            self.assertEqual(queue.call_args.args[1].action, "create")
            self.assertEqual(queue.call_args.args[1].startup, "new")
            window.open_preview()
            self.assertEqual(queue.call_args.args[1].action, "open")
            self.assertEqual(queue.call_args.args[1].startup, "browse")
        self.assertFalse(self.root.exists())

    def test_window_create_save_browse_choose_and_restore_is_operable_without_auto_generation(self):
        window, _, model, _, calls = self.window()
        self.select_preview(window)
        window.create_preview()
        self.finish_switch(window)
        sid = window.session.state.session_id
        self.assertIsNotNone(sid)
        self.assertEqual(calls[-1].action, "create")
        self.assertTrue(window.session.send("合成本地预览文本"))
        self.receive(window.session)
        self.assertEqual(len(model.requests), 1)
        window.open_preview()
        self.finish_switch(window)
        self.assertEqual(window.session.state.phase, "browse")
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(window.send_button.configure.call_args.kwargs["state"], "disabled")
        window.session_choice_var.set(str(sid))
        window.restore_session()
        self.finish_switch(window)
        self.assertEqual(window.session.state.session_id, sid)
        self.assertEqual(window._turn_count, 1)
        self.assertEqual(len(model.requests), 1)
        self.assertIn("local_preview", window.data_status_var.get())
        self.assertIn(str(sid), window.session_status_var.get())
        self.assertTrue((self.root / "genesis-local-preview.sqlite3").is_file())
        self.assertFalse((self.root / "genesis-synthetic.sqlite3").exists())
        with SQLiteStore(self.root, data_kind="local_preview", mode="open") as store:
            self.assertEqual(json.loads(store.day0()[0])["data_kind"], "local_preview")

    def test_selected_pending_ui_confirms_original_identity_and_ignores_edited_input(self):
        sid, _, failed, _ = self.seed()
        data = DesktopDataConfig("local_preview", str(self.root), "open", "resume", sid, True)
        window, _, model, _, _ = self.window(data=data)
        window.pending_tree.selection.return_value = (str(failed.input_id),)
        window.input.get.return_value = "synthetic edited draft"
        window.messagebox.askyesno.return_value = False
        window.retry_selected()
        self.assertEqual(model.requests, ())
        window.messagebox.askyesno.return_value = True
        window.retry_selected()
        self.receive(window.session)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(model.requests[0].input_id, failed.input_id)
        self.assertEqual(model.requests[0].messages[-1].content, failed.content)
        confirmation = window.messagebox.askyesno.call_args.args[1]
        self.assertIn(str(failed.input_id), confirmation)
        self.assertIn("可能再次执行", confirmation)

    def test_open_missing_preview_reports_failure_without_creating_or_rebuilding(self):
        data = DesktopDataConfig("local_preview", str(self.root), "open", "browse", acknowledged=True)
        window, _, model, workers, _ = self.window(data=data)
        self.assertTrue(workers[0].join(5))
        window.session.pump()
        self.assertEqual(window.session.state.phase, "fatal")
        self.assertIn("不会自动重建", window.session.state.error)
        window.pages.select.assert_called_with(window.records_page)
        # The selected records tab must not hide a startup failure or loading
        # status: both bound labels are packed beside the notebook, not in a tab.
        for label, variable in ((window.status_label, window.status_var), (window.error_label, window.error_var)):
            self.assertIs(label.master, window.pages.master)
            self.assertIsNot(label.master, window.conversation_page)
            self.assertIsNot(label.master, window.records_page)
            self.assertIs(label.construction_options["textvariable"], variable)
            label.pack.assert_called_once()
        self.assertIn("会话已停止", window.status_var.get())
        self.assertIn("不会自动重建", window.error_var.get())
        self.assertEqual(model.requests, ())
        self.assertFalse(self.root.exists())

    def test_restored_turn_source_uses_saved_model_and_ready_does_not_claim_connection(self):
        window = helpers.DesktopSettingsTests().window()
        window._active_config = BackendConfig("fake")
        window.transcript, window.count_var = Mock(), helpers.Value()
        turn = Turn(UserInput(UUID(int=5401), "synthetic history"), Message(Role.ASSISTANT, "synthetic reply"), "saved-real-model")
        window.append_turn(turn)
        labels = [call.args[1] for call in window.transcript.insert.call_args_list]
        self.assertIn("助手 · saved-real-model\n", labels)
        self.assertFalse(any("演示回复" in label for label in labels))
        window._active_config = BackendConfig("ollama", "http://127.0.0.1:11434", "synthetic-v1", True)
        window.render(ViewState("ready", "synthetic-v1", session_id=UUID(int=5402)))
        self.assertIn("恢复历史不验证当前服务", window.status_var.get())
        self.assertNotIn("请求成功", window.status_var.get())
        self.assertNotIn("已连接", window.status_var.get())

    def test_unfinished_labels_distinguish_running_unknown_stale_and_failed(self):
        user_input = UserInput(UUID(int=5501), "synthetic pending")
        expected = {"running": "未确认", "unknown": "未知", "stale": "上下文已变化", "failed": "失败", "interrupted": "未确认服务端停止"}
        for state, phrase in expected.items():
            item = UnfinishedRequest(user_input, (AttemptStatus(UUID(int=5502), state, "stale_context" if state == "stale" else None),))
            self.assertIn(phrase, unfinished_label(item))

    def test_browse_and_busy_disable_send_retry_and_storage_reconfiguration(self):
        sid, _, _, _ = self.seed()
        data = DesktopDataConfig("local_preview", str(self.root), "open", "browse", acknowledged=True)
        window, _, _, _, _ = self.window(data=data)
        self.assertEqual(window.session.state.phase, "browse")
        self.assertEqual(window.send_button.configure.call_args.kwargs["state"], "disabled")
        self.assertEqual(window.retry_selected_button.configure.call_args.kwargs["state"], "disabled")
        self.assertIn(str(sid), window.session_box.configure.call_args.kwargs["values"])
        window.render(replace(window.session.state, phase="busy"))
        for widget in (window.data_mode_box, window.data_root_entry, window.browse_root_button,
                       window.preview_ack_check, window.create_preview_button, window.open_preview_button,
                       window.restore_button, window.session_box, window.send_button):
            self.assertEqual(widget.configure.call_args.kwargs["state"], "disabled")


if __name__ == "__main__":
    unittest.main()
