"""Headless presentation tests; all messages and failures are synthetic."""
from contextlib import contextmanager, nullcontext
from contextlib import redirect_stderr
from io import StringIO
import threading
from tkinter import CallWrapper
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from genesis.adapters.desktop_worker import ChatWorker
from genesis.adapters.desktop import DesktopDataConfig, DesktopSession, DesktopWindow, ViewState, backend_context, main
from genesis.adapters.desktop_backend import BackendConfig
from genesis.models.contracts import ModelError
from genesis.models.fake import FakeModel
from genesis.models.ollama import OllamaError, OllamaFailure


class RecordingView:
    def __init__(self):
        self.states = []
        self.turns = []
        self.cleared = 0
        self.closed_count = 0
        self.threads = []

    def render(self, state):
        self.states.append(state)
        self.threads.append(threading.get_ident())

    def append_turn(self, turn):
        self.turns.append(turn)
        self.threads.append(threading.get_ident())

    def clear_input(self):
        self.cleared += 1
        self.threads.append(threading.get_ident())

    def closed(self):
        self.closed_count += 1
        self.threads.append(threading.get_ident())


class DelayedModel(FakeModel):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate(self, request):
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("synthetic release missing")
        return super().generate(request)


class DesktopSessionTests(unittest.TestCase):
    def create(self, model=None, factory=None):
        model = model if model is not None else FakeModel()
        worker = ChatWorker(factory or (lambda: nullcontext(model)))
        self.addCleanup(self.finish, worker)
        view = RecordingView()
        presenter = DesktopSession(worker, view)
        self.receive(presenter)
        self.assertEqual(presenter.state.phase, "ready")
        return presenter, view, model

    def finish(self, worker):
        worker.close()
        self.assertTrue(worker.join(6))
        worker.poll_events()

    def receive(self, presenter):
        self.assertTrue(presenter.worker.wait_for_events(5), "missing synthetic event")
        presenter.pump()

    def test_complete_turn_only_and_original_multiline_text_preserved(self):
        presenter, view, model = self.create()
        text = "  合成问候\n第二行  "
        self.assertTrue(presenter.send(text))
        self.assertEqual(view.turns, [])
        self.assertEqual(presenter.state.phase, "busy")
        self.assertEqual(presenter.state.pending, text)
        self.assertFalse(presenter.send(text))
        self.receive(presenter)
        self.assertEqual(len(view.turns), 1)
        self.assertEqual(view.turns[0].user_input.content, text)
        self.assertEqual(view.cleared, 1)
        self.assertEqual(presenter.state.phase, "ready")
        self.assertIsNone(presenter.state.pending)
        self.assertTrue(presenter.send("合成后续问题"))
        self.receive(presenter)
        self.assertEqual(len(model.requests[1].messages), 3)
        self.assertEqual(set(view.threads), {threading.get_ident()})

    def test_failed_input_remains_separate_and_explicit_retry_keeps_identity(self):
        presenter, view, model = self.create(FakeModel([ModelError("synthetic-private-value"), "合成重试成功"]))
        original = " 合成重试\n "
        self.assertTrue(presenter.send(original))
        self.receive(presenter)
        self.assertEqual(view.turns, [])
        self.assertEqual(view.cleared, 0)
        self.assertEqual(presenter.state.phase, "failed")
        self.assertEqual(presenter.state.pending, original)
        self.assertTrue(presenter.state.can_retry)
        self.assertNotIn("synthetic-private-value", presenter.state.error)
        self.assertEqual(len(model.requests), 1)
        self.assertTrue(presenter.retry())
        self.assertFalse(presenter.retry())
        self.receive(presenter)
        self.assertEqual(model.requests[0], model.requests[1])
        self.assertEqual(len(view.turns), 1)
        self.assertEqual(view.turns[0].assistant.content, "合成重试成功")
        self.assertEqual(view.cleared, 1)

    def test_edited_submission_after_failure_does_not_publish_abandoned_text(self):
        presenter, view, model = self.create(FakeModel([ModelError("synthetic"), "合成回复"]))
        presenter.send("合成失败输入")
        self.receive(presenter)
        presenter.send("合成修改后输入")
        self.receive(presenter)
        self.assertEqual(len(view.turns), 1)
        self.assertEqual(view.turns[0].user_input.content, "合成修改后输入")
        self.assertNotEqual(model.requests[0].input_id, model.requests[1].input_id)
        self.assertEqual(len(model.requests[1].messages), 1)

    def test_invalid_input_does_not_clear_editor_or_contact_model(self):
        presenter, view, model = self.create()
        for text in (" \n\t", "synthetic\0value", "\ud800"):
            self.assertFalse(presenter.send(text))
            self.assertEqual(presenter.state.phase, "ready")
            self.assertTrue(presenter.state.error)
        self.assertEqual(view.turns, [])
        self.assertEqual(view.cleared, 0)
        self.assertEqual(model.requests, ())

    def test_timeout_is_safe_and_not_misrepresented_as_cancellation(self):
        presenter, view, _ = self.create(FakeModel([OllamaError(OllamaFailure.READ_TIMEOUT)]))
        presenter.send("合成超时输入")
        self.receive(presenter)
        self.assertEqual(view.turns, [])
        self.assertIn("服务端可能仍在运行", presenter.state.error)
        self.assertTrue(presenter.state.can_retry)

    def test_unavailable_real_service_is_configured_then_fails_only_on_send(self):
        config = BackendConfig("ollama", "http://127.0.0.1:11434", "synthetic-v1", True)
        with patch("socket.create_connection", side_effect=ConnectionRefusedError) as connection:
            presenter, view, _ = self.create(factory=lambda: backend_context(config))
            connection.assert_not_called()
            self.assertTrue(presenter.send("合成连接失败测试"))
            self.receive(presenter)
            connection.assert_called_once()
        self.assertEqual(presenter.state.phase, "failed")
        self.assertIn("连接被拒绝", presenter.state.error)
        self.assertEqual(view.turns, [])
        self.assertTrue(presenter.state.can_retry)

    def test_real_protocol_failure_categories_have_safe_distinct_ui_states(self):
        cases = ((OllamaFailure.HTTP_ERROR, "服务拒绝"),
                 (OllamaFailure.SERVER_ERROR, "模型服务返回错误"),
                 (OllamaFailure.MODEL_MISMATCH, "模型与设置不一致"),
                 (OllamaFailure.INCOMPLETE, "回复未完整结束"))
        for reason, expected in cases:
            with self.subTest(reason=reason):
                presenter, view, _ = self.create(FakeModel([OllamaError(reason)]))
                presenter.send("合成错误状态测试")
                self.receive(presenter)
                self.assertEqual(presenter.state.phase, "failed")
                self.assertIn(expected, presenter.state.error)
                self.assertEqual(view.turns, [])

    def test_close_waits_without_joining_or_claiming_cancelled(self):
        model = DelayedModel()
        presenter, view, _ = self.create(model)
        self.addCleanup(model.release.set)
        presenter.send("合成关闭时输入")
        self.assertTrue(model.entered.wait(5))
        presenter.close()
        self.assertEqual(presenter.state.phase, "closing")
        self.assertEqual(view.closed_count, 0)
        self.assertTrue(presenter.worker.is_alive)
        self.assertFalse(presenter.send("合成重复输入"))
        self.assertFalse(presenter.retry())
        model.release.set()
        self.assertTrue(presenter.worker.join(5))
        presenter.pump()
        self.assertEqual(len(view.turns), 1)
        self.assertEqual(view.closed_count, 1)
        presenter.pump()
        self.assertEqual(view.closed_count, 1)

    def test_unexpected_failure_is_fatal_and_never_asserts_not_committed(self):
        class BrokenModel(FakeModel):
            def generate(self, request):
                raise RuntimeError("synthetic-private-error-body")

        presenter, view, _ = self.create(BrokenModel())
        presenter.send("合成内部错误")
        self.assertTrue(presenter.worker.join(5))
        presenter.pump()
        self.assertEqual(presenter.state.phase, "fatal")
        self.assertIn("提交结果需要核查", presenter.state.error)
        self.assertNotIn("synthetic-private-error-body", presenter.state.error)
        self.assertEqual(view.turns, [])
        self.assertFalse(presenter.retry())
        self.assertFalse(presenter.send("合成新输入"))
        presenter.close()
        presenter.pump()
        self.assertEqual(view.closed_count, 1)

    def test_synthetic_http_backend_exercises_existing_adapter_without_real_model(self):
        presenter, view, _ = self.create(factory=lambda: backend_context(BackendConfig("synthetic-http")))
        self.assertEqual(presenter.state.model_id, "synthetic-http-v1")
        presenter.send("合成 HTTP 中文\n第二行")
        self.receive(presenter)
        self.assertEqual(view.turns[0].assistant.content, "合成 HTTP 回复：合成 HTTP 中文\n第二行")
        self.assertEqual(presenter.state.phase, "ready")

    def test_unsupported_backend_is_rejected_before_resource_creation(self):
        with self.assertRaises(ValueError):
            BackendConfig("real-ollama")

    def test_mainloop_exit_or_interrupt_requests_worker_cleanup(self):
        for error in (None, KeyboardInterrupt(), RuntimeError("synthetic GUI failure")):
            with self.subTest(error=type(error).__name__):
                root = Mock()
                root.mainloop.side_effect = error
                worker = Mock()
                fake_tk = SimpleNamespace(Tk=lambda: root, TclError=type("TclError", (Exception,), {}))
                window = SimpleNamespace(session=SimpleNamespace(worker=worker))
                with patch.dict("sys.modules", {"tkinter": fake_tk}), patch(
                    "genesis.adapters.desktop.DesktopWindow", return_value=window
                ):
                    if error is None:
                        self.assertEqual(main([]), 0)
                    else:
                        with self.assertRaises(type(error)):
                            main([])
                worker.close.assert_called_once_with()
                root.destroy.assert_called_once_with()

    def test_cli_requires_complete_explicit_real_service_selection_before_tk(self):
        invalid = (
            ["--real-service"],
            ["--endpoint", "http://127.0.0.1:11434"],
            ["--model", "synthetic-v1"],
            ["--local-service-verified"],
            ["--synthetic-http", "--real-service"],
            ["--synthetic-http", "--local-service-verified"],
            ["--real-service", "--endpoint", "http://127.0.0.1:11434", "--model", "synthetic-v1"],
            ["--real-service", "--endpoint", "http://example.invalid:11434", "--model", "synthetic-v1", "--local-service-verified"],
        )
        for args in invalid:
            with self.subTest(args=args), patch("genesis.adapters.desktop.DesktopWindow") as window:
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as failure:
                    main(args)
                self.assertEqual(failure.exception.code, 2)
                window.assert_not_called()

    def test_cli_freezes_explicit_real_config_without_connecting(self):
        root, worker = Mock(), Mock()
        fake_tk = SimpleNamespace(Tk=lambda: root, TclError=type("TclError", (Exception,), {}))
        instance = SimpleNamespace(session=SimpleNamespace(worker=worker))
        with patch.dict("sys.modules", {"tkinter": fake_tk}), patch(
            "genesis.adapters.desktop.DesktopWindow", return_value=instance
        ) as window, patch("socket.socket.connect", side_effect=AssertionError("no startup I/O")):
            self.assertEqual(main(["--real-service", "--endpoint", "http://127.0.0.1:11434",
                                   "--model", "synthetic-v1", "--local-service-verified"]), 0)
        config = window.call_args.kwargs["config"]
        self.assertEqual(config, BackendConfig("ollama", "http://127.0.0.1:11434", "synthetic-v1", True))


class Value:
    def __init__(self, value="", name=""):
        self.value, self.name = value, name

    def get(self):
        return self.value

    def set(self, value):
        self.value = value

    def __str__(self):
        return self.name


class Widget:
    def __init__(self):
        self.options = {}

    def configure(self, **options):
        self.options.update(options)

    def get(self, *_args):
        return ""


class DesktopSettingsTests(unittest.TestCase):
    """Control-policy tests without a native Tk window or UI automation."""

    def window(self):
        view = DesktopWindow.__new__(DesktopWindow)
        view._active_config = BackendConfig("ollama", "http://127.0.0.1:11434", "synthetic-v1", True)
        view._active_data = DesktopDataConfig()
        view._turn_count = 0
        view._next_config = None
        view._fatal_exit_requested = False
        view._callback_interruption = None
        view.mode_var = Value("本地 Ollama", "mode")
        view.endpoint_var = Value("http://127.0.0.1:11434", "endpoint")
        view.model_input_var = Value("synthetic-v1", "model")
        view.verified_var = Value(True, "verified")
        view.data_mode_var = Value("临时，不保存", "data_mode")
        view.data_root_var = Value("", "data_root")
        view.preview_ack_var = Value(False, "preview_ack")
        view.session_choice_var = Value("")
        for name in ("status_var", "model_var", "pending_var", "error_var", "settings_note_var", "session_status_var"):
            setattr(view, name, Value())
        for name in ("input", "send_button", "retry_button", "mode_box", "new_button",
                     "endpoint_entry", "model_entry", "verified_check", "data_mode_box", "data_root_entry",
                     "browse_root_button", "preview_ack_check", "create_preview_button", "open_preview_button",
                     "session_box", "restore_button", "retry_selected_button"):
            setattr(view, name, Widget())
        view.pending_tree = Mock()
        view.pending_tree.selection.return_value = ()
        view.pending_tree.get_children.return_value = ()
        view.session = Mock(state=ViewState("ready", "synthetic-v1"))
        return view

    def test_real_ready_means_configured_not_server_available(self):
        view = self.window()
        view.render(view.session.state)
        self.assertIn("恢复历史不验证当前服务", view.status_var.get())
        self.assertEqual(view.send_button.options["state"], "normal")
        view._turn_count = 1
        view.render(view.session.state)
        self.assertIn("恢复历史不验证当前服务", view.status_var.get())
        self.assertNotIn("请求成功", view.status_var.get())
        self.assertNotIn("已连接", view.status_var.get())

    def test_unapplied_edits_disable_send_retry_and_do_not_retarget_session(self):
        view = self.window()
        original = view._active_config
        view.endpoint_var.set("http://127.0.0.1:11435")
        view._settings_changed("endpoint")
        self.assertFalse(view.verified_var.get())
        self.assertEqual(view.send_button.options["state"], "disabled")
        self.assertEqual(view.retry_button.options["state"], "disabled")
        self.assertIn("尚未应用", view.settings_note_var.get())
        view.send()
        view.retry()
        view.session.send.assert_not_called()
        view.session.retry.assert_not_called()
        self.assertIs(view._active_config, original)

    def test_model_edit_clears_verification_and_invalid_apply_preserves_session(self):
        view = self.window()
        view.model_input_var.set("synthetic-other")
        view._settings_changed("model")
        self.assertFalse(view.verified_var.get())
        view.new_session()
        view.session.close.assert_not_called()
        self.assertIsNone(view._next_config)
        self.assertIn("设置无效", view.error_var.get())
        view.verified_var.set(True)
        view.new_session()
        view.session.close.assert_called_once_with()
        self.assertEqual(view._next_config.model, "synthetic-other")
        self.assertEqual(view._active_config.model, "synthetic-v1")

    def test_busy_and_closing_disable_all_model_configuration(self):
        view = self.window()
        for phase in ("busy", "closing"):
            view.render(ViewState(phase, "synthetic-v1"))
            for widget in (view.send_button, view.retry_button, view.mode_box, view.new_button,
                           view.endpoint_entry, view.model_entry, view.verified_check):
                self.assertEqual(widget.options["state"], "disabled")

    def test_synthetic_selection_does_not_use_real_endpoint_fields(self):
        view = self.window()
        view.mode_var.set("离线演示")
        view.endpoint_var.set("not an endpoint")
        view.verified_var.set(False)
        view.render(view.session.state)
        self.assertEqual(view.endpoint_entry.options["state"], "disabled")
        self.assertEqual(view.model_entry.options["state"], "disabled")
        self.assertEqual(view._read_config().mode, "fake")


class DesktopStartupCleanupTests(unittest.TestCase):
    """Fail real worker ownership transfers without creating a native window."""

    def tracked_factory(self, model=None):
        workers, lifecycle = [], []

        @contextmanager
        def resource():
            owner = threading.get_ident()
            lifecycle.append(("enter", owner))
            try:
                yield FakeModel() if model is None else model
            finally:
                lifecycle.append(("exit", threading.get_ident()))

        def create(_config):
            worker = ChatWorker(resource)
            workers.append(worker)
            self.addCleanup(self.force_cleanup, worker)
            # Ensure a real context was entered before injecting the GUI fault.
            # The view, not this factory, acknowledges the queued ready event.
            self.assertTrue(worker.wait_for_events(5))
            return worker

        return create, workers, lifecycle

    def force_cleanup(self, worker):
        # Test fallback only: assertions below first verify application cleanup.
        worker.close()
        self.assertTrue(worker.join(5))
        worker.poll_events()

    def assert_clean(self, workers, lifecycle):
        for worker in workers:
            self.assertTrue(worker.join(5), "startup failure leaked a worker")
        self.assertEqual(sum(kind == "enter" for kind, _ in lifecycle), len(workers))
        self.assertEqual(sum(kind == "exit" for kind, _ in lifecycle), len(workers))
        for index in range(0, len(lifecycle), 2):
            self.assertEqual(lifecycle[index][0], "enter")
            self.assertEqual(lifecycle[index + 1][0], "exit")
            self.assertEqual(lifecycle[index][1], lifecycle[index + 1][1])
            self.assertNotEqual(lifecycle[index][1], threading.get_ident())
        self.assertFalse([t for t in threading.enumerate() if t.name == "genesis-chat"])

    def fake_tk(self):
        class Variable(Value):
            def __init__(self, *args, value="", **kwargs):
                super().__init__(value)

            def trace_add(self, *_args):
                return "synthetic-trace"

        root = Mock()
        root.winfo_children.return_value = []
        def widget(master=None, *args, **kwargs):
            result = Mock()
            result.master = master
            result.winfo_children.return_value = []
            result.winfo_width.return_value = 900
            result.winfo_height.return_value = 400
            result.winfo_ismapped.return_value = True
            result.focus_get.return_value = None
            if master is not None and isinstance(master.winfo_children.return_value, list):
                master.winfo_children.return_value.append(result)
            return result
        ttk = SimpleNamespace(**{name: Mock(side_effect=widget) for name in (
            "Style", "Frame", "Label", "Combobox", "Button", "Entry", "Checkbutton", "LabelFrame", "Notebook", "Scrollbar")})
        def tree(*args, **kwargs):
            result = widget(*args, **kwargs)
            result.selection.return_value = ()
            result.get_children.return_value = ()
            return result
        ttk.Treeview = Mock(side_effect=tree)
        tk = SimpleNamespace(Tk=lambda: root, TclError=type("TclError", (Exception,), {}),
                             StringVar=Variable, BooleanVar=Variable, ttk=ttk, messagebox=Mock(), filedialog=Mock(),
                             Frame=Mock(side_effect=widget), Label=Mock(side_effect=widget),
                             Text=Mock(side_effect=widget), Canvas=Mock(side_effect=widget))
        modules = {"tkinter": tk, "tkinter.ttk": ttk,
                   "tkinter.scrolledtext": SimpleNamespace(ScrolledText=Mock(side_effect=lambda *a, **kw: Mock()))}
        return root, tk, modules

    def run_main(self, modules, factory):
        # Inject only the worker factory; execute the actual window constructor.
        with patch.dict("sys.modules", modules), patch(
            "genesis.adapters.desktop.DesktopWindow",
            side_effect=lambda root, config, data: DesktopWindow(root, config=config, data=data, worker_factory=factory),
        ):
            return main([])

    def run_main_callback(self, root, modules, factory, action):
        windows = []

        def create(root, config, data):
            window = DesktopWindow(root, config=config, data=data, worker_factory=factory)
            windows.append(window)
            return window

        root.mainloop.side_effect = lambda: action(windows[0])
        with patch.dict("sys.modules", modules), patch(
            "genesis.adapters.desktop.DesktopWindow", side_effect=create
        ):
            return main([])

    def test_session_initial_render_failure_closes_worker_and_reraises(self):
        for exception in (RuntimeError("synthetic render failure"), KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(exception=type(exception).__name__):
                factory, workers, lifecycle = self.tracked_factory()
                worker = factory(BackendConfig())
                view = Mock()
                view.render.side_effect = exception
                with self.assertRaises(type(exception)) as caught:
                    DesktopSession(worker, view)
                self.assertIs(caught.exception, exception)
                self.assert_clean(workers, lifecycle)

    def test_constructor_scheduler_failure_closes_worker_and_main_destroys_root(self):
        for exception in (RuntimeError("synthetic scheduler failure"), KeyboardInterrupt(), SystemExit(8)):
            with self.subTest(exception=type(exception).__name__):
                factory, workers, lifecycle = self.tracked_factory()
                root, _, modules = self.fake_tk()
                root.after.side_effect = exception
                with self.assertRaises(type(exception)) as caught:
                    self.run_main(modules, factory)
                self.assertIs(caught.exception, exception)
                root.mainloop.assert_not_called()
                root.destroy.assert_called_once_with()
                self.assertEqual(len(workers), 1)
                self.assert_clean(workers, lifecycle)

    def test_widget_failure_before_worker_creation_still_destroys_root(self):
        root, tk, modules = self.fake_tk()
        exception = RuntimeError("synthetic widget failure")
        tk.Frame.side_effect = exception
        factory = Mock()
        with self.assertRaises(RuntimeError) as caught:
            self.run_main(modules, factory)
        self.assertIs(caught.exception, exception)
        factory.assert_not_called()
        root.mainloop.assert_not_called()
        root.destroy.assert_called_once_with()
        self.assert_clean([], [])

    def test_factory_failure_has_no_owned_worker_and_main_destroys_root(self):
        root, _, modules = self.fake_tk()
        exception = RuntimeError("synthetic worker factory failure")
        factory = Mock(side_effect=exception)
        with self.assertRaises(RuntimeError) as caught:
            self.run_main(modules, factory)
        self.assertIs(caught.exception, exception)
        factory.assert_called_once()
        root.mainloop.assert_not_called()
        root.destroy.assert_called_once_with()
        self.assert_clean([], [])

    def test_restart_initial_render_failure_closes_replacement_worker(self):
        factory, workers, lifecycle = self.tracked_factory()
        root, _, modules = self.fake_tk()
        with patch.dict("sys.modules", modules):
            window = DesktopWindow(root, worker_factory=factory)
            window.session.pump()
            old_session = window.session
            old_session.close()
            self.assertTrue(old_session.worker.join(5))
            window._next_config = BackendConfig()
            exception = RuntimeError("synthetic replacement render failure")
            with patch.object(window, "render", side_effect=exception):
                with self.assertRaises(RuntimeError) as caught:
                    window.closed()
            self.assertIs(caught.exception, exception)
            self.assertIs(window.session, old_session)
        self.assertEqual(len(workers), 2)
        self.assert_clean(workers, lifecycle)

    def test_poll_reschedule_failure_after_restart_closes_new_owner(self):
        factory, workers, lifecycle = self.tracked_factory()
        root, _, modules = self.fake_tk()
        with patch.dict("sys.modules", modules):
            window = DesktopWindow(root, worker_factory=factory)
            window.session.pump()
            old_session = window.session
            old_session.close()
            self.assertTrue(old_session.worker.join(5))
            window._next_config = BackendConfig()
            exception = RuntimeError("synthetic restart scheduling failure")
            root.after.side_effect = exception
            with self.assertRaises(RuntimeError) as caught:
                window._poll()
            self.assertIs(caught.exception, exception)
            self.assertIsNot(window.session, old_session)
        self.assertEqual(len(workers), 2)
        self.assert_clean(workers, lifecycle)

    def test_callwrapper_poll_failure_quits_destroys_and_returns_nonzero(self):
        factory, workers, lifecycle = self.tracked_factory()
        root, _, modules = self.fake_tk()
        observed = []

        def action(window):
            observed.append(window)
            window.session.pump()
            window._next_config = BackendConfig()
            root.after.reset_mock()
            with patch.object(window.session, "pump", side_effect=RuntimeError("synthetic polling failure")):
                CallWrapper(window._poll, None, root)()
            root._report_exception.assert_called_once_with()
            root.quit.assert_called_once_with()
            root.destroy.assert_called_once_with()
            root.after.assert_not_called()
            with patch.object(window.session, "close", side_effect=AssertionError("no further presentation")):
                window.request_close()

        self.assertEqual(self.run_main_callback(root, modules, factory, action), 1)
        self.assertTrue(observed[0]._fatal_exit_requested)
        self.assertTrue(observed[0]._exiting)
        self.assertIsNone(observed[0]._next_config)
        self.assertGreaterEqual(root.destroy.call_count, 2)
        self.assert_clean(workers, lifecycle)

    def test_callwrapper_interruptions_propagate_after_cleanup_even_with_tcl_errors(self):
        for interruption in (KeyboardInterrupt("synthetic interruption"), SystemExit(9)):
            for cleanup_tcl_error in (False, True):
                with self.subTest(interruption=type(interruption).__name__, tcl=cleanup_tcl_error):
                    factory, workers, lifecycle = self.tracked_factory()
                    root, tk, modules = self.fake_tk()
                    if cleanup_tcl_error:
                        root.quit.side_effect = tk.TclError("synthetic interpreter already closed")
                        root.destroy.side_effect = tk.TclError("synthetic interpreter already closed")

                    def action(window):
                        window.session.pump()
                        with patch.object(window.session, "pump", side_effect=interruption):
                            CallWrapper(window._poll, None, root)()

                    with self.assertRaises(type(interruption)) as caught:
                        self.run_main_callback(root, modules, factory, action)
                    self.assertIs(caught.exception, interruption)
                    root.quit.assert_called_once_with()
                    self.assertGreaterEqual(root.destroy.call_count, 2)
                    if isinstance(interruption, SystemExit):
                        root._report_exception.assert_not_called()
                    else:
                        root._report_exception.assert_called_once_with()
                    self.assert_clean(workers, lifecycle)

    def test_callwrapper_reschedule_failure_after_restart_terminates_new_session(self):
        factory, workers, lifecycle = self.tracked_factory()
        root, _, modules = self.fake_tk()

        def action(window):
            window.session.pump()
            previous = window.session
            previous.close()
            self.assertTrue(previous.worker.join(5))
            window._next_config = BackendConfig()
            root.after.side_effect = RuntimeError("synthetic restart timer failure")
            CallWrapper(window._poll, None, root)()
            self.assertIsNot(window.session, previous)
            root.quit.assert_called_once_with()
            root.destroy.assert_called_once_with()
            self.assertIsNone(window._next_config)

        self.assertEqual(self.run_main_callback(root, modules, factory, action), 1)
        self.assertEqual(len(workers), 2)
        self.assert_clean(workers, lifecycle)

    def test_callwrapper_failure_while_closing_does_not_need_another_poll(self):
        factory, workers, lifecycle = self.tracked_factory()
        root, _, modules = self.fake_tk()

        def action(window):
            window.session.pump()
            root.after.reset_mock()
            with patch.object(window, "render", side_effect=RuntimeError("synthetic closing view failure")):
                CallWrapper(window.request_close, None, root)()
            self.assertTrue(window._exiting)
            self.assertTrue(window._fatal_exit_requested)
            root.after.assert_not_called()
            root.quit.assert_called_once_with()
            root.destroy.assert_called_once_with()

        self.assertEqual(self.run_main_callback(root, modules, factory, action), 1)
        self.assert_clean(workers, lifecycle)

    def test_callwrapper_apply_failure_clears_rebuild_intent_and_exits(self):
        factory, workers, lifecycle = self.tracked_factory()
        root, _, modules = self.fake_tk()

        def action(window):
            window.session.pump()
            window.input.get.return_value = ""
            with patch.object(window, "render", side_effect=RuntimeError("synthetic apply view failure")):
                CallWrapper(window.new_session, None, root)()
            self.assertIsNone(window._next_config)
            self.assertTrue(window._fatal_exit_requested)
            root.quit.assert_called_once_with()
            root.destroy.assert_called_once_with()

        self.assertEqual(self.run_main_callback(root, modules, factory, action), 1)
        self.assertEqual(len(workers), 1)
        self.assert_clean(workers, lifecycle)

    def test_fatal_exit_does_not_wait_for_active_model_or_claim_cancellation(self):
        model = DelayedModel()
        factory, workers, lifecycle = self.tracked_factory(model)
        self.addCleanup(model.release.set)
        root, _, modules = self.fake_tk()

        def action(window):
            window.session.pump()
            self.assertTrue(window.session.send("合成仍在运行的输入"))
            self.assertTrue(model.entered.wait(5))
            with patch.object(window.session, "pump", side_effect=RuntimeError("synthetic active view failure")):
                with patch.object(window.session.worker, "join", side_effect=AssertionError("GUI must not join")):
                    CallWrapper(window._poll, None, root)()
            self.assertTrue(window.session.worker.is_alive)
            self.assertFalse(model.release.is_set())
            root.quit.assert_called_once_with()
            root.destroy.assert_called_once_with()

        self.assertEqual(self.run_main_callback(root, modules, factory, action), 1)
        self.assertTrue(workers[0].is_alive)
        self.assertFalse(model.release.is_set())
        model.release.set()
        self.assert_clean(workers, lifecycle)

    def test_modal_return_cannot_retry_or_rebuild_after_nested_fatal_callback(self):
        for operation in ("new_session", "retry"):
            with self.subTest(operation=operation):
                model = FakeModel([ModelError("synthetic retry setup")])
                factory, workers, lifecycle = self.tracked_factory(model)
                root, _, modules = self.fake_tk()

                def action(window):
                    window.session.pump()
                    if operation == "retry":
                        window.session.send("synthetic original input")
                        self.assertTrue(window.session.worker.wait_for_events(5))
                        window.session.pump()
                    else:
                        window._turn_count = 1
                    window.input.get.return_value = "synthetic edited input"

                    def modal_callback(*args, **kwargs):
                        with patch.object(window.session, "pump", side_effect=RuntimeError("synthetic modal timer failure")):
                            CallWrapper(window._poll, None, root)()
                        # Fatal handling destroys the modal owner before this
                        # synchronous dialog resumes; quit alone is insufficient.
                        root.destroy.assert_called_once_with()
                        self.assertTrue(window._fatal_exit_requested)
                        return True

                    window.messagebox.askyesno.side_effect = modal_callback
                    with patch.object(window.session, "retry", wraps=window.session.retry) as retry:
                        with patch.object(window.session, "close", wraps=window.session.close) as close:
                            CallWrapper(getattr(window, operation), None, root)()
                            retry.assert_not_called()
                            close.assert_not_called()
                    self.assertIsNone(window._next_config)

                self.assertEqual(self.run_main_callback(root, modules, factory, action), 1)
                self.assertEqual(len(workers), 1)
                self.assertEqual(len(model.requests), 1 if operation == "retry" else 0)
                self.assert_clean(workers, lifecycle)


if __name__ == "__main__":
    unittest.main()
