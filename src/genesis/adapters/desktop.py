"""Temporary desktop chat; a real local backend requires explicit selection."""
import argparse
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import wraps
from pathlib import Path
import sys
from typing import Protocol
from uuid import UUID, uuid4

from genesis.adapters.desktop_worker import ChatWorker, WorkerStateError
from genesis.adapters.desktop_backend import BackendConfig, backend_context
from genesis.adapters.desktop_data_worker import DataTaskRequest, DataTaskWorker
from genesis.core import Turn
from genesis.models.contracts import ModelError
from genesis.storage.sqlite import SQLiteStore, UnfinishedRequest
from genesis.storage.backup import BackupReport, RestoreReport
from genesis.text import TextContractError


BACKENDS = {"离线演示": "fake", "本地合成服务": "synthetic-http", "本地 Ollama": "ollama"}
DATA_MODES = {"临时，不保存": "temporary", "本地预览，保存记录": "local_preview"}
ERROR_MESSAGES = {
    "model_error": "模型未完成回复。本轮没有加入聊天历史，可明确重试。",
    "connection_refused": "连接被拒绝。本轮没有加入聊天历史，可检查服务后重试。",
    "connection_error": "连接中断，服务端执行结果可能未知。本轮没有加入聊天历史。",
    "connect_timeout": "建立连接超时。本轮没有加入聊天历史，可明确重试。",
    "read_timeout": "等待回复超时，服务端可能仍在运行。本轮没有加入聊天历史。",
    "model_mismatch": "返回的模型与设置不一致，本轮没有加入聊天历史。",
    "incomplete_response": "回复未完整结束或达到长度上限，本轮没有加入聊天历史。",
    "invalid_response": "回复格式未通过检查，本轮没有加入聊天历史。",
    "invalid_text": "回复文本未通过检查，本轮没有加入聊天历史。",
    "http_error": "服务拒绝了请求，请核对模型名称和服务状态。本轮没有加入聊天历史。",
    "server_error": "模型服务返回错误，本轮没有加入聊天历史。不会自动下载模型或切换后端。",
    "redirect_rejected": "服务要求重定向，已拒绝连接其他地址。本轮没有加入聊天历史。",
    "invalid_json": "服务响应不是完整的有效 JSON，本轮没有加入聊天历史。",
    "response_too_large": "服务响应超过允许大小，本轮没有加入聊天历史。",
    "interrupted": "后台任务中断，会话已停止；没有确认服务端取消。",
    "worker_interrupted": "后台任务中断，会话已停止；没有确认服务端取消。",
    "unexpected_error": "发生内部错误，会话已停止；提交结果需要核查。请新建会话继续。",
    "internal_error": "发生内部错误，会话已停止；提交结果需要核查。请新建会话继续。",
    "worker_startup_error": "后端未能启动，请核对设置后重新打开会话。",
    "storage_startup_error": "预览库未能打开或创建。请检查目录、数据分类、版本与完整性；不会自动重建，已有文件需保留核查。",
    "storage_initialization_busy": "目录正由另一个初始化操作占用，本次未创建数据库。请保留目录和锁文件，待其结束后核对数据分类；已有库选择“打开”，无库可明确重试“新建”或选择其他空目录。请勿删除锁文件；不会自动重试。",
    "session_startup_error": "会话未能加载或创建。请重新打开预览库并核对会话列表；本次操作是否留下新会话需核查。",
}

DATA_OPERATIONS = {"backup": "备份", "verify": "验证备份", "restore": "恢复备份"}
DATA_ERROR_MESSAGES = {
    "data_invalid_argument": "数据操作参数无效，请核对绝对路径和操作选择；不会自动重试。",
    "data_path_rejected": "所选路径不符合数据目录要求。请使用仓库外允许的位置，并保留已有内容。",
    "data_target_exists": "目标目录已经存在；空目录也不能覆盖。请选择一个不存在的新目录。",
    "data_source_invalid": "来源不是可用的本地预览库。请保留来源，核对分类、版本与完整性。",
    "data_invalid_backup": "备份格式或完整性验证未通过。请保留备份，不会自动修复或恢复。",
    "data_busy": "数据操作遇到占用，请待其他操作结束后明确重试；不要删除锁文件。",
    "data_timeout": "数据操作超过允许等待时间。请保留所选位置并核查结果，不会自动重试。",
    "data_io_error": "无法完成数据读写，请核对位置、权限和可用空间，并保留已有输出检查。",
    "data_outcome_unknown": "数据操作的结果未能确认，目标可能已经完成。请保留目标并验证；不要覆盖或自动重做。",
    "data_internal_error": "数据操作发生内部错误，结果需要核查。请保留所选位置，不会自动重试或覆盖。",
    "data_interrupted": "数据操作被中断，输出是否完成需要核查；不代表操作已取消。请保留所选位置。",
}


@dataclass(frozen=True)
class DesktopDataConfig:
    """Explicit storage intent; validation never creates or opens a database."""
    mode: str = "temporary"
    root: str | None = None
    action: str | None = None
    startup: str = "new"
    session_id: UUID | None = None
    acknowledged: bool = False

    def __post_init__(self) -> None:
        if self.mode not in ("temporary", "local_preview") or type(self.acknowledged) is not bool:
            raise ValueError("invalid desktop data selection")
        if self.mode == "temporary":
            if self.root is not None or self.action is not None or self.startup != "new" or self.session_id is not None:
                raise ValueError("temporary sessions do not select persistent storage")
            return
        if not self.acknowledged or not isinstance(self.root, str) or not self.root or not Path(self.root).is_absolute():
            raise ValueError("local preview requires an explicit absolute root and data acknowledgment")
        if self.action not in ("create", "open") or self.startup not in ("new", "resume", "browse"):
            raise ValueError("invalid local-preview operation")
        if self.action == "create" and self.startup != "new":
            raise ValueError("create requires a new session")
        if self.startup == "resume":
            if not isinstance(self.session_id, UUID):
                raise ValueError("resume requires a session UUID")
        elif self.session_id is not None:
            raise ValueError("session UUID is valid only for resume")


def persistent_worker(config: BackendConfig, data: DesktopDataConfig) -> ChatWorker:
    """Open the explicitly selected profile only on its worker thread."""
    if data.mode != "local_preview":
        raise ValueError("persistent worker requires local-preview configuration")
    return ChatWorker(lambda: backend_context(config),
                      store_factory=lambda: SQLiteStore(data.root, data_kind="local_preview", mode=data.action),
                      startup=data.startup, resume_session_id=data.session_id)


def unfinished_label(request: UnfinishedRequest) -> str:
    state = request.attempts[-1].state if request.attempts else "pending"
    return {"running": "上次结果未确认，可能仍在运行", "unknown": "执行结果未知",
            "failed": "失败，未发布回复", "interrupted": "已中断，未确认服务端停止",
            "stale": "上下文已变化，未发布回复"}.get(state, "状态待核查")


@dataclass(frozen=True)
class ViewState:
    phase: str = "starting"
    model_id: str = "准备中"
    pending: str | None = None
    error: str = ""
    can_retry: bool = False
    session_id: UUID | None = None
    session_ids: tuple[UUID, ...] = ()
    pending_requests: tuple[UnfinishedRequest, ...] = ()
    notice: str = ""


class ChatView(Protocol):
    def render(self, state: ViewState) -> None: ...
    def append_turn(self, turn: Turn) -> None: ...
    def clear_input(self) -> None: ...
    def closed(self) -> None: ...


class DesktopSession:
    """Main-thread presenter; only completed Turns reach the transcript."""

    def __init__(self, worker: ChatWorker, view: ChatView) -> None:
        self.worker = worker
        self.view = view
        self.state = ViewState()
        self._pending_id: UUID | None = None
        self._shown: set[UUID] = set()
        self._closing = False
        self._resources_closed = False
        self._closed_delivered = False
        try:
            self.view.render(self.state)
        except BaseException:
            # Construction did not transfer ownership to a usable presenter.
            # Request cleanup without calling the failed view or joining Tk.
            worker.close()
            raise

    def _render(self, *, phase: str, pending: str | None = None, error: str = "", can_retry: bool = False,
                notice: str = "") -> None:
        self.state = replace(self.state, phase=phase, pending=pending, error=error, can_retry=can_retry, notice=notice)
        self.view.render(self.state)

    def send(self, text: str) -> bool:
        if self._closing or self.state.phase not in {"ready", "failed"}:
            return False
        try:
            request = self.worker.submit(text)
        except TextContractError:
            self._render(phase=self.state.phase, pending=self.state.pending,
                         error="请输入非空文本；文本不能包含 NUL 或无效 Unicode。", can_retry=self.state.can_retry)
            return False
        except WorkerStateError:
            return False
        self._pending_id = request.input_id
        self._render(phase="busy", pending=request.content)
        return True

    def retry(self, input_id: UUID | None = None) -> bool:
        if self._closing or self.state.phase not in {"ready", "failed"}:
            return False
        if input_id is None and not self.state.can_retry:
            return False
        if input_id is not None and not any(item.user_input.input_id == input_id for item in self.state.pending_requests):
            return False
        try:
            request = self.worker.retry(input_id)
        except WorkerStateError:
            return False
        self._pending_id = request.input_id
        self._render(phase="busy", pending=request.content)
        return True

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.worker.close()
        self._render(phase="closing", pending=self.state.pending)

    def pump(self) -> None:
        for event in self.worker.poll_events():
            if event.kind in {"ready", "succeeded", "failed"}:
                self.state = replace(self.state, session_id=event.session_id, session_ids=event.session_ids,
                                     pending_requests=event.pending_requests)
                for turn in event.history:
                    if turn.user_input.input_id not in self._shown:
                        self.view.append_turn(turn)
                        self._shown.add(turn.user_input.input_id)
            if event.kind == "ready":
                self.state = replace(self.state, model_id=event.model_id or "未知模型")
                if not self._closing:
                    self._render(phase="ready" if event.session_id is not None else "browse")
            elif event.kind == "succeeded":
                assert event.turn is not None
                if event.turn.user_input.input_id not in self._shown:
                    self.view.append_turn(event.turn)
                    self._shown.add(event.turn.user_input.input_id)
                if event.input_id == self._pending_id:
                    self.view.clear_input()
                    self._pending_id = None
                notice = ("已恢复同一输入的成功提交；本次模型调用未成功，不会自动重试。"
                          if getattr(event, "recovered_commit", False) else "")
                self._render(phase="closing" if self._closing else "ready", notice=notice)
            elif event.kind == "failed":
                message = ERROR_MESSAGES.get(event.error_code, ERROR_MESSAGES["model_error"])
                if any(turn.user_input.input_id == event.input_id for turn in event.history):
                    if event.input_id == self._pending_id:
                        self.view.clear_input()
                        self._pending_id = None
                    self._render(phase="closing" if self._closing else "ready",
                                 notice="本次调用失败，但已恢复同一输入的成功提交；不会自动重试。")
                else:
                    self._render(phase="closing" if self._closing else "failed", pending=self.state.pending,
                                 error=message, can_retry=not self._closing and self.worker.can_retry)
            elif event.kind == "fatal":
                message = ERROR_MESSAGES.get(event.error_code, ERROR_MESSAGES["internal_error"])
                self._render(phase="closing" if self._closing else "fatal", pending=self.state.pending, error=message)
            elif event.kind == "closed":
                self._resources_closed = True
        # A final event can be queued just before the worker returns. Never join
        # on the Tk thread or destroy the window before resource cleanup finishes.
        if self._closing and self._resources_closed and not self.worker.is_alive and not self._closed_delivered:
            self._closed_delivered = True
            self.view.closed()


def _guard_window_callback(callback):
    """Tk may swallow callback exceptions; terminate this window explicitly."""
    @wraps(callback)
    def guarded(self, *args, **kwargs):
        if self._fatal_exit_requested:
            return None
        try:
            return callback(self, *args, **kwargs)
        except BaseException as error:
            self._request_fatal_exit(error)
            raise
    return guarded


class DesktopWindow:
    """Tk widgets are accessed only by the thread running Tk's event loop."""

    def __init__(self, root, *, config: BackendConfig | None = None,
                 data: DesktopDataConfig | None = None,
                 worker_factory: Callable[[BackendConfig], ChatWorker] | None = None,
                 persistent_worker_factory: Callable[[BackendConfig, DesktopDataConfig], ChatWorker] | None = None,
                 data_worker_factory: Callable[[DataTaskRequest], DataTaskWorker] | None = None) -> None:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        from tkinter.scrolledtext import ScrolledText

        config = BackendConfig() if config is None else config
        data = DesktopDataConfig() if data is None else data
        self.root, self.tk, self.messagebox, self.filedialog = root, tk, messagebox, filedialog
        self._worker_factory = worker_factory or (lambda selected: ChatWorker(lambda: backend_context(selected)))
        self._persistent_worker_factory = persistent_worker_factory or persistent_worker
        self._data_worker_factory = data_worker_factory or DataTaskWorker
        self._data_worker: DataTaskWorker | None = None
        self._data_request: DataTaskRequest | None = None
        self._data_closed = False
        self._restored_report: RestoreReport | None = None
        self._data_notice = ""
        self._data_error = ""
        self._next_config: BackendConfig | None = None
        self._next_data: DesktopDataConfig | None = None
        self._exiting = False
        self._fatal_exit_requested = False
        self._callback_interruption: BaseException | None = None
        self._turn_count = 0
        self._active_config = config
        self._active_data = data
        self.session: DesktopSession | None = None
        root.title("Genesis · 本地对话预览")
        root.geometry("1000x860")
        root.minsize(800, 740)
        root.configure(background="#f4f6f8")
        root.protocol("WM_DELETE_WINDOW", self.request_close)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f4f6f8")
        style.configure("TLabel", background="#f4f6f8", foreground="#273445", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(14, 7))
        style.configure("Send.TButton", background="#176f69", foreground="white")
        style.map("Send.TButton", background=[("disabled", "#cad4d5"), ("active", "#105b56")])
        style.configure("TCombobox", padding=5, font=("Microsoft YaHei UI", 10))

        header = tk.Frame(root, background="#143e41", padx=26, pady=20)
        header.pack(fill="x")
        tk.Label(header, text="GENESIS", font=("Segoe UI", 11, "bold"), foreground="#86d2c5", background="#143e41").pack(anchor="w")
        tk.Label(header, text="从一次对话开始", font=("Microsoft YaHei UI", 23, "bold"), foreground="white", background="#143e41").pack(anchor="w", pady=(4, 4))
        self.banner_var = tk.StringVar()
        tk.Label(header, textvariable=self.banner_var, font=("Microsoft YaHei UI", 10), foreground="#d6e9e7", background="#143e41").pack(anchor="w")

        body = ttk.Frame(root, padding=(24, 16, 24, 18))
        body.pack(fill="both", expand=True)
        settings = ttk.Frame(body)
        settings.pack(fill="x")
        ttk.Label(settings, text="对话后端").pack(side="left", padx=(0, 10))
        self.mode_var = tk.StringVar(value=next(label for label, value in BACKENDS.items() if value == config.mode))
        self.mode_box = ttk.Combobox(settings, textvariable=self.mode_var, values=tuple(BACKENDS), state="readonly", width=18)
        self.mode_box.pack(side="left")
        self.new_button = ttk.Button(settings, text="新会话 / 应用", command=self.new_session)
        self.new_button.pack(side="left", padx=10)
        self.model_var = tk.StringVar(value="模型：准备中")
        ttk.Label(settings, textvariable=self.model_var).pack(side="right")
        local_settings = ttk.Frame(body)
        local_settings.pack(fill="x", pady=(10, 0))
        local_settings.columnconfigure(1, weight=1)
        local_settings.columnconfigure(3, weight=1)
        ttk.Label(local_settings, text="服务地址").grid(row=0, column=0, padx=(0, 8))
        self.endpoint_var = tk.StringVar(value=config.endpoint)
        self.endpoint_entry = ttk.Entry(local_settings, textvariable=self.endpoint_var, width=28)
        self.endpoint_entry.grid(row=0, column=1, sticky="ew", padx=(0, 14))
        ttk.Label(local_settings, text="模型名称").grid(row=0, column=2, padx=(0, 8))
        self.model_input_var = tk.StringVar(value=config.model)
        self.model_entry = ttk.Entry(local_settings, textvariable=self.model_input_var, width=32)
        self.model_entry.grid(row=0, column=3, sticky="ew")
        self.verified_var = tk.BooleanVar(value=config.local_service_verified)
        self.verified_check = ttk.Checkbutton(body, text="已核查所选服务为本地运行且关闭云功能", variable=self.verified_var)
        self.verified_check.pack(anchor="w", pady=(8, 0))
        self.settings_note_var = tk.StringVar()
        ttk.Label(body, textvariable=self.settings_note_var, foreground="#8b5a12", wraplength=900).pack(anchor="w", pady=(2, 0))
        self.connection_var = tk.StringVar()
        ttk.Label(body, textvariable=self.connection_var, wraplength=850).pack(anchor="w", pady=(10, 12))
        self.data_status_var = tk.StringVar()
        ttk.Label(body, textvariable=self.data_status_var, wraplength=900).pack(anchor="w")
        self.session_status_var = tk.StringVar()
        ttk.Label(body, textvariable=self.session_status_var, wraplength=900).pack(anchor="w", pady=(0, 5))
        # Operation status belongs to the whole window: opening a database can
        # fail while the records tab is selected, before any session is loaded.
        self.status_var = tk.StringVar(value="正在准备演示…")
        self.status_label = ttk.Label(body, textvariable=self.status_var, wraplength=850)
        self.status_label.pack(anchor="w", pady=(2, 4))
        self.error_var = tk.StringVar()
        self.error_label = ttk.Label(body, textvariable=self.error_var, wraplength=850, foreground="#a53832")
        self.error_label.pack(anchor="w", pady=(0, 6))
        self.pages = ttk.Notebook(body)
        self.pages.pack(fill="both", expand=True)
        self.conversation_page = ttk.Frame(self.pages, padding=(8, 8))
        self.records_page = ttk.Frame(self.pages, padding=(8, 8))
        self.backup_page = ttk.Frame(self.pages, padding=(8, 8))
        self.pages.add(self.conversation_page, text="对话")
        self.pages.add(self.records_page, text="记录与恢复")
        self.pages.add(self.backup_page, text="备份与恢复")

        # The embedded frame can grow without increasing the notebook's
        # requested height. Only this page owns these scroll bindings.
        self.backup_page.rowconfigure(0, weight=1)
        self.backup_page.columnconfigure(0, weight=1)
        self.backup_canvas = tk.Canvas(self.backup_page, width=1, height=1, takefocus=True,
            background="#f4f6f8", borderwidth=0, highlightthickness=1,
            highlightbackground="#cad4d5", highlightcolor="#176f69")
        self.backup_canvas.grid(row=0, column=0, sticky="nsew")
        self.backup_scrollbar = ttk.Scrollbar(self.backup_page, orient="vertical", command=self._backup_scrollbar)
        self.backup_scrollbar.grid(row=0, column=1, sticky="ns")
        self.backup_canvas.configure(yscrollcommand=self._backup_scroll_position)
        self.backup_content = ttk.Frame(self.backup_canvas, padding=(0, 0, 8, 8))
        self._backup_content_id = self.backup_canvas.create_window((0, 0), window=self.backup_content, anchor="nw")
        self._backup_view_width = 0
        self._backup_content_size = None
        self._backup_wrapped_labels = []
        self._backup_scroll_widgets = set()

        def backup_label(**options):
            label = ttk.Label(self.backup_content, wraplength=850, **options)
            self._backup_wrapped_labels.append(label)
            return label

        self.backup_source_var = tk.StringVar()
        backup_label(textvariable=self.backup_source_var).pack(anchor="w", pady=(0, 5))
        self.backup_plaintext_var = tk.BooleanVar(value=False)
        self.backup_plaintext_check = ttk.Checkbutton(self.backup_content, variable=self.backup_plaintext_var,
            text="我确认以下明文保存范围和新目录要求")
        self.backup_plaintext_check.pack(anchor="w")
        backup_label(text="备份及恢复会明文保存输入（含失败和未完成请求）、回复与状态；目标必须是不存在的新目录，不覆盖已有文件。").pack(anchor="w", pady=(0, 8))
        self.backup_target_var = tk.StringVar()
        self.backup_input_var = tk.StringVar()
        self.restore_target_var = tk.StringVar()
        self.backup_path_entries = []
        self.backup_path_buttons = []
        for label, variable, picker in (
            ("新备份目录", self.backup_target_var, lambda: self.choose_data_task_path("backup_target")),
            ("已有备份目录", self.backup_input_var, lambda: self.choose_data_task_path("backup_input")),
            ("恢复到新目录", self.restore_target_var, lambda: self.choose_data_task_path("restore_target")),
        ):
            row = ttk.Frame(self.backup_content)
            row.pack(fill="x", pady=(0, 6))
            ttk.Label(row, text=label, width=14).pack(side="left")
            entry = ttk.Entry(row, textvariable=variable)
            entry.pack(side="left", fill="x", expand=True, padx=(0, 7))
            button = ttk.Button(row, text="选择目录" if variable is self.backup_input_var else "选择父目录", command=picker)
            button.pack(side="right")
            self.backup_path_entries.append(entry)
            self.backup_path_buttons.append(button)
        actions = ttk.Frame(self.backup_content)
        actions.pack(fill="x", pady=(2, 8))
        self.backup_button = ttk.Button(actions, text="备份当前预览库", command=self.backup_current)
        self.verify_backup_button = ttk.Button(actions, text="验证备份", command=self.verify_selected_backup)
        self.restore_backup_button = ttk.Button(actions, text="恢复到新目录", command=self.restore_selected_backup)
        for button in (self.backup_button, self.verify_backup_button, self.restore_backup_button):
            button.pack(side="left", padx=(0, 7))
        self.open_restored_button = ttk.Button(self.backup_content, text="打开上次恢复目录", command=self.open_restored_preview)
        self.open_restored_button.pack(anchor="w", pady=(0, 8))
        self.data_task_status_var = tk.StringVar(value="尚未执行数据操作。备份、验证和恢复均不会调用模型。")
        backup_label(textvariable=self.data_task_status_var).pack(anchor="w", pady=(0, 6))
        backup_label(
            text="备份使用上方已应用的预览目录，不包含未发送草稿。选择父目录仅填写新的子路径，不创建目录。恢复完成后仍须明确打开；当前聊天不会自动切换。\nTab 可聚焦滚动区域；此时 PageUp / PageDown 翻页，Home / End 到首尾。",
            foreground="#5e6d7b").pack(anchor="w")
        for variable in (self.backup_target_var, self.backup_input_var, self.restore_target_var):
            variable.trace_add("write", self._data_task_settings_changed)
        self.backup_canvas.bind("<Configure>", self._backup_canvas_configure)
        self.backup_content.bind("<Configure>", self._backup_content_configure)
        self.backup_canvas.bind("<Button-1>", self._backup_canvas_focus, add="+")
        for sequence in ("<Prior>", "<Next>", "<Home>", "<End>"):
            self.backup_canvas.bind(sequence, self._backup_scroll_key, add="+")
        self._backup_focus_widgets = {self.backup_canvas, self.backup_scrollbar, self.backup_plaintext_check,
            *self.backup_path_entries, *self.backup_path_buttons, self.backup_button,
            self.verify_backup_button, self.restore_backup_button, self.open_restored_button}
        self._backup_geometry_widgets = set()
        self._backup_geometry = {}
        for control in self._backup_focus_widgets - {self.backup_canvas, self.backup_scrollbar}:
            current = control
            while current is not self.backup_content:
                self._backup_geometry_widgets.add(current)
                current = current.master
        self._backup_bind_scrolling(self.backup_canvas)
        self._backup_bind_scrolling(self.backup_scrollbar)

        records = ttk.LabelFrame(self.records_page, text="记录保存（默认不保存）", padding=(10, 7))
        records.pack(fill="x", pady=(0, 8))
        record_settings = ttk.Frame(records)
        record_settings.pack(fill="x")
        self.data_mode_var = tk.StringVar(value=next(label for label, value in DATA_MODES.items() if value == data.mode))
        self.data_mode_box = ttk.Combobox(record_settings, textvariable=self.data_mode_var,
                                         values=tuple(DATA_MODES), state="readonly", width=21)
        self.data_mode_box.pack(side="left", padx=(0, 8))
        self.data_root_var = tk.StringVar(value=data.root or "")
        self.data_root_entry = ttk.Entry(record_settings, textvariable=self.data_root_var, width=38)
        self.data_root_entry.pack(side="left", fill="x", expand=True)
        self.browse_root_button = ttk.Button(record_settings, text="选择目录", command=self.choose_data_root)
        self.browse_root_button.pack(side="left", padx=(8, 0))
        self.preview_ack_var = tk.BooleanVar(value=data.acknowledged)
        self.preview_ack_check = ttk.Checkbutton(records, variable=self.preview_ack_var,
            text="我了解本地预览会明文记录输入（含失败）、回复和状态；不是正式伙伴档案，备份也为明文，尚无加密或删除工具。")
        self.preview_ack_check.pack(anchor="w", pady=(5, 3))
        record_actions = ttk.Frame(records)
        record_actions.pack(fill="x")
        self.create_preview_button = ttk.Button(record_actions, text="新建预览库", command=self.create_preview)
        self.create_preview_button.pack(side="left")
        self.open_preview_button = ttk.Button(record_actions, text="打开预览库", command=self.open_preview)
        self.open_preview_button.pack(side="left", padx=7)
        self.session_choice_var = tk.StringVar()
        self.session_box = ttk.Combobox(record_actions, textvariable=self.session_choice_var, state="readonly", width=38)
        self.session_box.pack(side="left", fill="x", expand=True, padx=(5, 7))
        self.restore_button = ttk.Button(record_actions, text="恢复所选会话", command=self.restore_session)
        self.restore_button.pack(side="left")

        history_heading = ttk.Frame(self.conversation_page)
        history_heading.pack(fill="x")
        ttk.Label(history_heading, text="本次聊天", font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        self.count_var = tk.StringVar(value="0 轮已完成")
        ttk.Label(history_heading, textvariable=self.count_var).pack(side="right")
        self.transcript = ScrolledText(self.conversation_page, wrap="word", font=("Microsoft YaHei UI", 11), background="white",
                                       foreground="#273445", relief="flat", borderwidth=0, padx=18, pady=14, height=7)
        self.transcript.tag_configure("user", foreground="#245d92", font=("Microsoft YaHei UI", 10, "bold"))
        self.transcript.tag_configure("assistant", foreground="#176f69", font=("Microsoft YaHei UI", 10, "bold"))
        self.transcript.tag_configure("hint", foreground="#6c7887")
        self._empty_transcript()

        unfinished = ttk.Frame(self.records_page)
        unfinished.pack(fill="x", pady=(0, 5))
        ttk.Label(unfinished, text="未完成请求（不计入成功历史）").pack(side="left")
        self.retry_selected_button = ttk.Button(unfinished, text="重试所选原消息", command=self.retry_selected)
        self.retry_selected_button.pack(side="right")
        self.pending_tree = ttk.Treeview(self.records_page, columns=("status", "request", "text"), show="headings", height=7)
        for key, title, width in (("status", "上次状态", 200), ("request", "输入 ID", 250), ("text", "原文预览", 430)):
            self.pending_tree.heading(key, text=title)
            self.pending_tree.column(key, width=width, stretch=(key == "text"))
        self.pending_tree.pack(fill="both", expand=True, pady=(0, 5))
        self.pending_tree.bind("<<TreeviewSelect>>", self._pending_selected)
        ttk.Label(self.records_page, text="打开或恢复不会调用模型。上次 running 表示结果未确认，不能当作当前窗口正在生成；重试可能再次执行。",
                  wraplength=900).pack(anchor="w", pady=(3, 5))

        self.pending_var = tk.StringVar()
        self.pending_label = ttk.Label(self.conversation_page, textvariable=self.pending_var, wraplength=850, foreground="#5e6d7b")
        self.input = tk.Text(self.conversation_page, wrap="word", height=3, font=("Microsoft YaHei UI", 11), relief="solid",
                             borderwidth=1, background="white", padx=12, pady=9, undo=True)
        self.input.bind("<Control-Return>", self._send_key)
        self.input.bind("<Control-KP_Enter>", self._send_key)
        bottom = ttk.Frame(self.conversation_page)
        self.retention_var = tk.StringVar()
        ttk.Label(bottom, textvariable=self.retention_var, foreground="#6c7887").pack(side="left")
        self.send_button = ttk.Button(bottom, text="发送", style="Send.TButton", command=self.send)
        self.send_button.pack(side="right")
        self.retry_button = ttk.Button(bottom, text="重试原消息", command=self.retry)
        self.retry_button.pack(side="right", padx=(0, 8))
        # Reserve the controls before allocating the remaining height to the
        # scrollable transcript, including at larger system display scales.
        bottom.pack(side="bottom", fill="x", pady=(10, 0))
        self.input.pack(side="bottom", fill="x")
        self.pending_label.pack(side="bottom", anchor="w", pady=(0, 4))
        self.transcript.pack(fill="both", expand=True, pady=(8, 8))
        for variable in (self.mode_var, self.endpoint_var, self.model_input_var, self.verified_var,
                         self.data_mode_var, self.data_root_var, self.preview_ack_var):
            variable.trace_add("write", self._settings_changed)
        try:
            self._start(config, data)
            root.after(40, self._poll)
        except BaseException:
            # main() cannot retain a constructor that never returned. This
            # instance must release any worker it started before propagating.
            self._close_worker()
            raise

    def _backup_bind_scrolling(self, widget) -> None:
        if widget in self._backup_scroll_widgets:
            return
        self._backup_scroll_widgets.add(widget)
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(sequence, self._backup_scroll_wheel, add="+")
        # Tk's default traversal treats a Focus binding as a reason to visit a
        # widget. Decorating Labels/Frames here would create invisible stops.
        if widget in self._backup_focus_widgets:
            widget.bind("<FocusIn>", self._backup_scroll_focus, add="+")
        if widget in self._backup_geometry_widgets:
            widget.bind("<Configure>", self._backup_local_configure, add="+")
        for child in widget.winfo_children():
            self._backup_bind_scrolling(child)

    def _backup_scroll_active(self) -> bool:
        return not self._exiting and bool(self.backup_canvas.winfo_ismapped())

    def _backup_scroll_bounds(self):
        region = self.backup_canvas.bbox("all")
        height = self.backup_canvas.winfo_height() - 2
        if region is None or height <= 1:
            return None
        return region, height

    def _backup_reveal_focus(self, widget) -> None:
        if (not self._backup_scroll_active() or widget is None or widget not in self._backup_focus_widgets
                or widget in (self.backup_canvas, self.backup_scrollbar)
                or not widget.winfo_ismapped()):
            return
        bounds = self._backup_scroll_bounds()
        if bounds is None:
            return
        region, height = bounds
        extent = region[3] - region[1]
        if extent <= height:
            return
        visible_top = self.backup_canvas.canvasy(1)
        widget_top = self.backup_canvas.canvasy(widget.winfo_rooty() - self.backup_canvas.winfo_rooty())
        widget_height = widget.winfo_height()
        if widget_top < visible_top + 4 or widget_height > height - 8:
            target = widget_top - 4
        elif widget_top + widget_height > visible_top + height - 4:
            target = widget_top + widget_height - height + 4
        else:
            return
        offset = min(max(target - region[1], 0), max(extent - height, 0))
        self.backup_canvas.yview_moveto(offset / extent)

    @_guard_window_callback
    def _backup_canvas_configure(self, event) -> None:
        if self._exiting or event.width <= 2:
            return
        width = event.width - 2  # Exclude the focus border on both sides.
        if width != self._backup_view_width:
            self._backup_view_width = width
            self.backup_canvas.itemconfigure(self._backup_content_id, width=width)
            for label in self._backup_wrapped_labels:
                label.configure(wraplength=max(1, width - 16))
        # Height-only resizing may not reconfigure the embedded frame.
        self._backup_reveal_focus(self.backup_canvas.focus_get())

    @_guard_window_callback
    def _backup_content_configure(self, event=None) -> None:
        if self._exiting:
            return
        if event is not None:
            size = (event.width, event.height)
            if size == self._backup_content_size:
                return  # Scrolling can move the window without resizing it.
            self._backup_content_size = size
        region = self.backup_canvas.bbox("all")
        if region is not None:
            self.backup_canvas.configure(scrollregion=region)
            self._backup_reveal_focus(self.backup_canvas.focus_get())

    @_guard_window_callback
    def _backup_scroll_focus(self, event) -> None:
        if self._backup_scroll_active():
            self._backup_reveal_focus(event.widget)

    @_guard_window_callback
    def _backup_local_configure(self, event) -> None:
        if not self._backup_scroll_active() or event.widget not in self._backup_geometry_widgets:
            return
        geometry = (event.x, event.y, event.width, event.height)
        if self._backup_geometry.get(event.widget) == geometry:
            return
        self._backup_geometry[event.widget] = geometry
        focused = self.backup_canvas.focus_get()
        if focused not in self._backup_focus_widgets:
            return
        current = focused
        while current in self._backup_geometry_widgets:
            if current is event.widget:
                # pack may finish positioning a row after the outer frame's
                # Configure. Local coordinates ignore whole-canvas scrolling.
                self._backup_reveal_focus(focused)
                return
            current = current.master

    @_guard_window_callback
    def _backup_canvas_focus(self, event) -> None:
        if self._backup_scroll_active() and event.widget is self.backup_canvas:
            self.backup_canvas.focus_set()

    @_guard_window_callback
    def _backup_scroll_wheel(self, event):
        if (not self._backup_scroll_active() or event.widget not in self._backup_scroll_widgets
                or getattr(event, "state", 0) & 0x2000D):  # Shift/Ctrl/Mod1 and Windows Alt.
            return None
        number, delta = getattr(event, "num", None), getattr(event, "delta", 0)
        if number in (4, 5):
            units = -1 if number == 4 else 1
        elif delta:
            units = (-1 if delta > 0 else 1) * max(1, int(abs(delta) / 120))
        else:
            return None
        bounds = self._backup_scroll_bounds()
        if bounds is not None:
            region, height = bounds
            top = self.backup_canvas.canvasy(1)
            if region[3] - region[1] > height and (
                units < 0 and top > region[1] or units > 0 and top + height < region[3]
            ):
                self.backup_canvas.yview_scroll(units, "units")
        return "break"

    @_guard_window_callback
    def _backup_scroll_key(self, event):
        if not self._backup_scroll_active() or event.widget is not self.backup_canvas:
            return None
        if event.keysym in ("Prior", "Next"):
            self.backup_canvas.yview_scroll(-1 if event.keysym == "Prior" else 1, "pages")
        elif event.keysym in ("Home", "End"):
            self.backup_canvas.yview_moveto(0 if event.keysym == "Home" else 1)
        else:
            return None
        return "break"

    @_guard_window_callback
    def _backup_scrollbar(self, *args) -> None:
        if self._backup_scroll_active():
            self.backup_canvas.yview(*args)

    @_guard_window_callback
    def _backup_scroll_position(self, first, last) -> None:
        if not self._exiting:
            self.backup_scrollbar.set(first, last)

    def _close_worker(self) -> None:
        """Request owned cleanup without rendering, waiting, or cancellation."""
        try:
            if self.session is not None:
                self.session.worker.close()
        finally:
            data_worker = getattr(self, "_data_worker", None)
            if data_worker is not None:
                data_worker.close()

    def _data_busy(self) -> bool:
        # Keep ownership even after a result arrives: the final resource-close
        # event and actual thread exit must both be observed before reuse.
        return getattr(self, "_data_worker", None) is not None

    def _data_operation_allowed(self) -> bool:
        return (not self._data_busy() and not getattr(self, "_exiting", False)
                and not self._fatal_exit_requested and self.session is not None
                and self.session.state.phase in {"ready", "failed", "browse", "fatal"})

    def _request_fatal_exit(self, error: BaseException) -> None:
        """End a failed callback without another timer, render, or model wait.

        Destroy also releases a synchronous Tk modal dialog, which quit alone
        does not guarantee. The worker remains responsible for its own cleanup;
        closing the UI neither cancels generation nor imposes a total deadline.
        """
        if not isinstance(error, Exception) and self._callback_interruption is None:
            # CallWrapper rethrows SystemExit but catches KeyboardInterrupt.
            # main rethrows a caught interruption after its final cleanup.
            self._callback_interruption = error
        if self._fatal_exit_requested:
            return
        self._fatal_exit_requested = True
        self._exiting = True
        self._next_config = None
        self._next_data = None
        try:
            self._close_worker()
        finally:
            try:
                self.root.quit()
            except self.tk.TclError:
                pass  # A destroyed interpreter must not replace the failure.
            finally:
                try:
                    self.root.destroy()
                except self.tk.TclError:
                    pass  # main also performs idempotent final destruction.

    def _start(self, config: BackendConfig, data: DesktopDataConfig | None = None) -> None:
        data = self._active_data if data is None else data
        self._active_config = config
        self._active_data = data
        if hasattr(self, "backup_source_var"):
            self.backup_source_var.set("备份来源（已应用）：" + data.root if data.mode == "local_preview"
                                       else "备份来源：当前为临时会话，不能备份未保存聊天。")
            self.backup_plaintext_var.set(False)
        scope = "本地预览，明文保存" if data.mode == "local_preview" else "临时会话，不保存"
        if config.mode == "ollama":
            self.banner_var.set(scope + " · 本地 Ollama")
            self.connection_var.set("当前服务：" + config.endpoint + " · 发送时才连接；设置就绪不代表服务可用。")
        elif config.mode == "fake":
            self.banner_var.set(scope + " · FakeModel 演示，不使用真实模型")
            self.connection_var.set("当前会话：离线演示，没有网络连接。")
        else:
            self.banner_var.set(scope + " · 本地合成服务，不加载真实模型")
            self.connection_var.set("当前会话：本地合成服务，临时回环端口，不连接已安装的 Ollama。")
        # Freeze the selected configuration before a worker is started. Pending
        # field edits never change the current worker's endpoint or model.
        self.data_status_var.set("数据分类：local_preview · 当前目录：" + data.root if data.root is not None
                                 else "数据分类：临时内存 · 没有打开数据库")
        self.retention_var.set("Ctrl+Enter 发送 · Enter 换行\n" +
                               ("本地预览会保存成功历史与未完成输入。" if data.mode == "local_preview"
                                else "本次窗口内保留；关闭后不保存。"))
        self.pages.select(self.records_page if data.startup == "browse" else self.conversation_page)
        worker = (self._worker_factory(config) if data.mode == "temporary"
                  else self._persistent_worker_factory(config, data))
        try:
            self.session = DesktopSession(worker, self)
        except BaseException:
            # A failed presenter constructor leaves self.session unassigned (or
            # still referring to the previous, closed session during restart).
            worker.close()
            raise

    def _settings_match_active(self) -> bool:
        selected = BACKENDS.get(self.mode_var.get())
        if selected != self._active_config.mode:
            return False
        backend_matches = selected != "ollama" or (
            self.endpoint_var.get() == self._active_config.endpoint
            and self.model_input_var.get() == self._active_config.model
            and self.verified_var.get() == self._active_config.local_service_verified
        )
        return backend_matches and self._data_settings_match_active()

    def _data_settings_match_active(self) -> bool:
        selected = DATA_MODES.get(self.data_mode_var.get())
        if selected != self._active_data.mode:
            return False
        return selected == "temporary" or (
            self.data_root_var.get() == self._active_data.root and self.preview_ack_var.get() is True
        )

    @_guard_window_callback
    def _settings_changed(self, variable_name: str, *_args) -> None:
        if variable_name in {str(self.endpoint_var), str(self.model_input_var)} and self.verified_var.get():
            self.verified_var.set(False)
        if variable_name == str(self.data_root_var) and self.preview_ack_var.get():
            self.preview_ack_var.set(False)
        if self.session is not None:
            self.render(self.session.state)

    def _read_config(self) -> BackendConfig:
        return BackendConfig(mode=BACKENDS[self.mode_var.get()], endpoint=self.endpoint_var.get(),
                             model=self.model_input_var.get(), local_service_verified=self.verified_var.get())

    def _read_data(self, action: str, *, startup: str | None = None, session_id: UUID | None = None) -> DesktopDataConfig:
        if DATA_MODES.get(self.data_mode_var.get()) != "local_preview":
            raise ValueError("select local preview explicitly")
        return DesktopDataConfig("local_preview", self.data_root_var.get(), action,
                                 startup or ("new" if action == "create" else "browse"),
                                 session_id, self.preview_ack_var.get())

    def _empty_transcript(self) -> None:
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.insert("end", "聊天会从这里开始。\n\n可以输入：请用一句中文问好。\n\n离线演示和本地合成服务只验证消息流程。\n本地 Ollama 模式使用所选模型，回复可能有误。", "hint")
        self.transcript.configure(state="disabled")

    def render(self, state: ViewState) -> None:
        labels = {"starting": "正在准备演示…", "ready": "演示已就绪", "busy": "正在等待完整回复…",
                  "failed": "本轮失败，可重试原消息或编辑后重新发送", "fatal": "会话已停止，可新建会话",
                  "closing": "正在结束会话；等待当前请求与资源清理，没有取消服务端请求。",
                  "browse": "预览库已打开；在“记录与恢复”页选择会话恢复，或新建会话。"}
        status = labels[state.phase]
        if self._active_config.mode == "ollama" and state.phase == "ready":
            status = "设置已就绪；恢复历史不验证当前服务，已提交请求可直接重放。"
        if state.notice and state.phase != "closing":
            status = state.notice
        if self._data_busy():
            operation = DATA_OPERATIONS[self._data_request.operation]
            status = ("正在结束会话；等待" + operation + "及资源清理，未取消数据操作。"
                      if self._exiting else "正在" + operation + "；聊天和设置暂不可操作。")
        elif getattr(self, "_data_notice", "") and state.phase not in {"starting", "busy", "closing"}:
            status += " · " + self._data_notice
        self.status_var.set(status)
        self.model_var.set("模型：" + state.model_id)
        preview = "" if state.pending is None else state.pending.replace("\n", " ↵ ")
        if len(preview) > 100:
            preview = preview[:100] + "…"
        self.pending_var.set("本轮输入：" + preview if state.pending is not None else "")
        data_error = getattr(self, "_data_error", "")
        self.error_var.set("\n".join(part for part in (state.error, "数据操作：" + data_error if data_error else "") if part))
        settings_match = self._settings_match_active()
        self.settings_note_var.set("" if settings_match else "设置尚未应用。后端/临时模式使用“新会话 / 应用”；预览库目录使用“新建”或“打开”按钮。")
        ready = state.phase in {"ready", "failed"} and settings_match and not self._data_busy()
        self.input.configure(state="normal" if ready else "disabled")
        self.send_button.configure(state="normal" if ready else "disabled")
        self.retry_button.configure(state="normal" if state.can_retry and settings_match and not self._data_busy() else "disabled")
        idle = state.phase in {"ready", "failed", "fatal", "browse"} and not self._data_busy()
        self.mode_box.configure(state="readonly" if idle else "disabled")
        self.new_button.configure(state="normal" if idle else "disabled")
        real_settings = idle and BACKENDS.get(self.mode_var.get()) == "ollama"
        for widget in (self.endpoint_entry, self.model_entry, self.verified_check):
            widget.configure(state="normal" if real_settings else "disabled")
        self.session_status_var.set("当前会话 UUID：" + str(state.session_id) if state.session_id is not None
                                    else "当前会话：尚未选择或尚未打开成功")
        self.data_mode_box.configure(state="readonly" if idle else "disabled")
        preview_settings = idle and DATA_MODES.get(self.data_mode_var.get()) == "local_preview"
        for widget in (self.data_root_entry, self.browse_root_button, self.preview_ack_check,
                       self.create_preview_button, self.open_preview_button):
            widget.configure(state="normal" if preview_settings else "disabled")
        values = tuple(str(value) for value in state.session_ids)
        self.session_box.configure(values=values, state="readonly" if idle and self._active_data.mode == "local_preview" else "disabled")
        if self.session_choice_var.get() not in values:
            self.session_choice_var.set(str(state.session_id) if state.session_id is not None else "")
        self.restore_button.configure(state="normal" if idle and values and self._active_data.mode == "local_preview"
                                       and self._data_settings_match_active() else "disabled")
        self._sync_pending(state)
        self._render_data_controls()

    def _render_data_controls(self) -> None:
        if not hasattr(self, "backup_button"):
            return  # Also permits a presenter-only view without these widgets.
        idle = self._data_operation_allowed()
        for widget in (*self.backup_path_entries, *self.backup_path_buttons, self.backup_plaintext_check,
                       self.verify_backup_button, self.restore_backup_button):
            widget.configure(state="normal" if idle else "disabled")
        can_backup = (idle and self._active_data.mode == "local_preview"
                      and self.session.state.phase in {"ready", "failed", "browse"}
                      and self._data_settings_match_active())
        self.backup_button.configure(state="normal" if can_backup else "disabled")
        self.open_restored_button.configure(state="normal" if idle and self._restored_report is not None else "disabled")

    def _sync_pending(self, state: ViewState) -> None:
        if getattr(self, "_pending_display", None) != state.pending_requests:
            selected = tuple(self.pending_tree.selection())
            self._pending_display = state.pending_requests
            for row in self.pending_tree.get_children():
                self.pending_tree.delete(row)
            valid = set()
            for item in state.pending_requests:
                key = str(item.user_input.input_id)
                valid.add(key)
                preview = item.user_input.content.replace("\n", " ↵ ")
                self.pending_tree.insert("", "end", iid=key,
                    values=(unfinished_label(item), key, preview[:160] + ("…" if len(preview) > 160 else "")))
            retained = [key for key in selected if key in valid]
            if retained:
                self.pending_tree.selection_set(retained)
        enabled = (state.phase in {"ready", "failed"} and not self._data_busy()
                   and self._settings_match_active() and bool(self.pending_tree.selection()))
        self.retry_selected_button.configure(state="normal" if enabled else "disabled")

    @_guard_window_callback
    def _pending_selected(self, _event=None) -> None:
        if self.session is not None:
            self._sync_pending(self.session.state)

    def append_turn(self, turn: Turn) -> None:
        self.transcript.configure(state="normal")
        if not self._turn_count:
            self.transcript.delete("1.0", "end")
        self.transcript.insert("end", "你\n", "user")
        self.transcript.insert("end", turn.user_input.content + "\n\n")
        self.transcript.insert("end", "助手 · " + turn.model_id + "\n", "assistant")
        self.transcript.insert("end", turn.assistant.content + "\n\n")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")
        self._turn_count += 1
        self.count_var.set(f"{self._turn_count} 轮已完成")

    def clear_input(self) -> None:
        self.input.configure(state="normal")
        self.input.delete("1.0", "end")
        self.input.edit_reset()

    @_guard_window_callback
    def send(self) -> None:
        if self.session is not None and not self._data_busy() and self._settings_match_active():
            self.session.send(self.input.get("1.0", "end-1c"))

    def _send_key(self, _event=None) -> str:
        self.send()
        return "break"

    @_guard_window_callback
    def retry(self) -> None:
        if self.session is None or self._data_busy() or not self._settings_match_active():
            return
        original = self.session.state.pending
        if original is not None and self.input.get("1.0", "end-1c") != original:
            if not self.messagebox.askyesno("重试原消息", "当前输入已修改。重试会使用上次失败的原消息；是否继续？", parent=self.root):
                return
        if self._fatal_exit_requested or self._data_busy() or getattr(self, "_exiting", False):
            return  # A timer may have failed inside the modal dialog.
        if self.session.retry() and original is not None:
            self.input.configure(state="normal")
            self.input.delete("1.0", "end")
            self.input.insert("1.0", original)
            self.input.configure(state="disabled")

    @_guard_window_callback
    def retry_selected(self) -> None:
        if (self.session is None or self._data_busy() or self.session.state.phase not in {"ready", "failed"}
                or not self._settings_match_active()):
            return
        selected = self.pending_tree.selection()
        if len(selected) != 1:
            return
        item = next((item for item in self.session.state.pending_requests
                     if str(item.user_input.input_id) == selected[0]), None)
        if item is None:
            return
        preview = item.user_input.content[:200]
        if not self.messagebox.askyesno("重试保留的原消息",
            "输入 ID：" + str(item.user_input.input_id) + "\n" + unfinished_label(item) +
            "\n\n原文预览：\n" + preview + "\n\n将沿用原文和同一 ID；服务端可能已经执行过，本次可能再次执行。是否继续？",
            parent=self.root):
            return
        if self._fatal_exit_requested or self._data_busy() or getattr(self, "_exiting", False):
            return
        if self.session.retry(item.user_input.input_id):
            self.input.configure(state="normal")
            self.input.delete("1.0", "end")
            self.input.insert("1.0", item.user_input.content)
            self.input.configure(state="disabled")
            self.pages.select(self.conversation_page)

    @_guard_window_callback
    def choose_data_root(self) -> None:
        selected = self.filedialog.askdirectory(parent=self.root, title="选择仓库外的本地预览目录", mustexist=False)
        if self._fatal_exit_requested:
            return
        if selected:
            self.data_root_var.set(selected)

    @_guard_window_callback
    def _data_task_settings_changed(self, *_args) -> None:
        # An acknowledgment for one destination must not silently authorize a
        # subsequently edited source or destination. No filesystem work here.
        if self.backup_plaintext_var.get():
            self.backup_plaintext_var.set(False)

    @_guard_window_callback
    def choose_data_task_path(self, field: str) -> None:
        if not self._data_operation_allowed():
            return
        variables = {"backup_target": self.backup_target_var, "backup_input": self.backup_input_var,
                     "restore_target": self.restore_target_var}
        if field not in variables:
            raise ValueError("unknown data path field")
        source = field == "backup_input"
        selected = self.filedialog.askdirectory(parent=self.root, mustexist=True,
            title="选择已有备份目录" if source else "选择父目录（不会创建目标目录）")
        if not self._data_operation_allowed() or not selected:
            return
        target = selected if source else str(Path(selected) / (
            ("genesis-backup-" if field == "backup_target" else "genesis-restored-") + uuid4().hex[:12]))
        variables[field].set(target)

    def _launch_data_task(self, request: DataTaskRequest) -> None:
        if not self._data_operation_allowed():
            return
        self._data_request = request
        self._data_closed = False
        self._restored_report = None
        self._data_notice = ""
        self._data_error = ""
        worker = self._data_worker_factory(request)
        self._data_worker = worker
        try:
            self.data_task_status_var.set("正在" + DATA_OPERATIONS[request.operation] + "；请保留所选位置，关闭窗口不会取消操作。")
            self.render(self.session.state)
        except BaseException:
            worker.close()
            raise

    def _select_data_task(self, operation: str) -> None:
        if not self._data_operation_allowed():
            return
        if operation == "backup":
            if (self._active_data.mode != "local_preview" or not self._data_settings_match_active()
                    or self.session.state.phase not in {"ready", "failed", "browse"}):
                self.data_task_status_var.set("备份需要已成功打开且设置未改动的本地预览库；临时聊天不能转存为备份。")
                return
            source, destination = self._active_data.root, self.backup_target_var.get()
        else:
            source = self.backup_input_var.get()
            destination = self.restore_target_var.get() if operation == "restore" else None
        try:
            request = DataTaskRequest(operation, source, destination,
                                      self.backup_plaintext_var.get() if operation != "verify" else False)
        except (ValueError, TypeError):
            self.data_task_status_var.set("数据操作设置无效：请输入完整绝对路径；备份或恢复还须确认明文范围。目标必须是不存在的新目录。")
            return
        self._launch_data_task(request)

    @_guard_window_callback
    def backup_current(self) -> None:
        self._select_data_task("backup")

    @_guard_window_callback
    def verify_selected_backup(self) -> None:
        self._select_data_task("verify")

    @_guard_window_callback
    def restore_selected_backup(self) -> None:
        self._select_data_task("restore")

    def _poll_data_task(self) -> None:
        worker = getattr(self, "_data_worker", None)
        if worker is None:
            return
        request = self._data_request
        for event in worker.poll_events():
            if event.operation_id != request.operation_id or event.operation != request.operation:
                raise RuntimeError("data task event does not match its owner")
            if event.kind == "succeeded":
                report = event.report.backup if isinstance(event.report, RestoreReport) else event.report
                if not isinstance(report, BackupReport):
                    raise RuntimeError("data task returned an invalid report")
                if request.operation == "restore":
                    if not isinstance(event.report, RestoreReport):
                        raise RuntimeError("restore task returned an invalid report")
                    self._restored_report = event.report
                    result = "恢复完成，尚未打开：" + str(event.report.target_root)
                elif request.operation == "backup":
                    self.backup_input_var.set(str(report.backup_dir))
                    result = "备份完成并通过验证：" + str(report.backup_dir)
                else:
                    result = "备份完整性及格式验证通过：" + str(report.backup_dir)
                self.data_task_status_var.set(result + "\n账本 UUID：" + str(report.companion_id) +
                    f"\n会话 {report.session_count} · 成功轮次 {report.turn_count} · 未完成请求 {report.unfinished_request_count}" +
                    "\n这是明文数据；验证不证明内容来源可信，也不表示已调用模型。")
                self._data_notice = DATA_OPERATIONS[request.operation] + "完成；详情见“备份与恢复”。"
            elif event.kind in {"failed", "fatal"}:
                self._data_error = DATA_ERROR_MESSAGES.get(event.error_code, DATA_ERROR_MESSAGES["data_internal_error"])
                self.data_task_status_var.set(self._data_error)
                self._data_notice = DATA_OPERATIONS[request.operation] + "未能确认完成。"
            elif event.kind == "closed":
                self._data_closed = True
        if self._data_closed and not worker.is_alive:
            self._data_worker = None
            if self._exiting:
                if self.session is None or self.session._closed_delivered:
                    self.root.destroy()
            elif self.session is not None:
                self.render(self.session.state)

    @_guard_window_callback
    def open_restored_preview(self) -> None:
        if not self._data_operation_allowed() or self._restored_report is None:
            return
        # An explicit click opens the completed restore, never a draft path.
        # It uses the already applied backend, not unsubmitted backend edits.
        report = self._restored_report
        data = DesktopDataConfig("local_preview", str(report.target_root), "open", "browse", acknowledged=True)
        if self._turn_count or self.input.get("1.0", "end-1c"):
            if not self.messagebox.askyesno("打开恢复目录", "当前界面和未发送输入将清空，原预览库不会删除。是否打开上次恢复完成的目录？", parent=self.root):
                return
        if not self._data_operation_allowed():
            return
        self.mode_var.set(next(label for label, value in BACKENDS.items() if value == self._active_config.mode))
        self.endpoint_var.set(self._active_config.endpoint)
        self.model_input_var.set(self._active_config.model)
        self.verified_var.set(self._active_config.local_service_verified)
        self.data_mode_var.set("本地预览，保存记录")
        self.data_root_var.set(str(report.target_root))
        self.preview_ack_var.set(True)
        if not self._data_operation_allowed():
            return
        self._next_config, self._next_data = self._active_config, data
        self.session.close()

    def _queue_start(self, config: BackendConfig, data: DesktopDataConfig) -> None:
        if self.session is None or self._data_busy() or self.session.state.phase not in {"ready", "failed", "fatal", "browse"}:
            return
        if self._turn_count or self.input.get("1.0", "end-1c"):
            if not self.messagebox.askyesno("切换会话", "当前界面和未发送输入将清空。已有预览库记录不会删除，临时聊天不会转存。是否继续？", parent=self.root):
                return
        if self._fatal_exit_requested or self._data_busy() or getattr(self, "_exiting", False):
            return
        self._next_config, self._next_data = config, data
        self.session.close()

    def _select_preview(self, action: str) -> None:
        try:
            config, data = self._read_config(), self._read_data(action)
        except (ValueError, TypeError, ModelError):
            self.error_var.set("预览设置无效：请明确选择本地预览、输入仓库外绝对目录，并确认明文保存范围；本地模型设置也须有效。")
            return
        self._queue_start(config, data)

    @_guard_window_callback
    def create_preview(self) -> None:
        self._select_preview("create")

    @_guard_window_callback
    def open_preview(self) -> None:
        self._select_preview("open")

    @_guard_window_callback
    def restore_session(self) -> None:
        if self._active_data.mode != "local_preview" or not self._data_settings_match_active() or self.session is None:
            return
        try:
            session_id = UUID(self.session_choice_var.get())
            if session_id not in self.session.state.session_ids:
                raise ValueError("choose an existing session")
            config = self._read_config()
            data = self._read_data("open", startup="resume", session_id=session_id)
        except (ValueError, TypeError, ModelError):
            self.error_var.set("请从已打开预览库的会话列表中选择 UUID，并核对本地模型设置。")
            return
        self._queue_start(config, data)

    @_guard_window_callback
    def new_session(self) -> None:
        if self.session is None or self.session.state.phase not in {"ready", "failed", "fatal", "browse"}:
            return
        try:
            config = self._read_config()
            if DATA_MODES.get(self.data_mode_var.get()) == "temporary":
                data = DesktopDataConfig()
            elif self._active_data.mode == "local_preview" and self._data_settings_match_active():
                data = self._read_data("open", startup="new")
            else:
                self.error_var.set("请先在“记录与恢复”页使用“新建预览库”或“打开预览库”应用所选目录。")
                return
        except (ValueError, TypeError, ModelError):
            self.error_var.set("设置无效：本地 Ollama 需要显式回环 HTTP 地址、非空模型名称，以及针对所选服务的核查确认。")
            return
        self._queue_start(config, data)

    @_guard_window_callback
    def request_close(self) -> None:
        self._exiting = True
        self._next_config = None
        self._next_data = None
        data_worker = getattr(self, "_data_worker", None)
        if data_worker is not None:
            data_worker.close()
        if self.session is not None:
            self.session.close()

    def closed(self) -> None:
        if self._fatal_exit_requested:
            return
        if self._exiting:
            if not self._data_busy():
                self.root.destroy()
        elif self._next_config is not None:
            config, self._next_config = self._next_config, None
            data, self._next_data = self._next_data or self._active_data, None
            self._turn_count = 0
            self.count_var.set("0 轮已完成")
            self._empty_transcript()
            self.clear_input()
            self._start(config, data)

    @_guard_window_callback
    def _poll(self) -> None:
        if self.session is not None:
            self.session.pump()
        self._poll_data_task()
        if not self._exiting or self._data_busy() or (self.session is not None and self.session.worker.is_alive):
            self.root.after(40, self._poll)
        elif self.session is not None and not self.session._closed_delivered:
            self.root.after(40, self._poll)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Genesis desktop chat. FakeModel and unsaved memory by default; local preview is explicit.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--synthetic-http", action="store_true", help="Use an isolated synthetic loopback server instead of FakeModel")
    modes.add_argument("--real-service", action="store_true", help="Explicitly select an existing local Ollama service")
    parser.add_argument("--endpoint", help="Explicit loopback HTTP IP and port; real-service only")
    parser.add_argument("--model", help="Exact installed model name; real-service only")
    parser.add_argument("--local-service-verified", action="store_true", help="Acknowledge prior independent local/cloud-disabled service verification")
    data_modes = parser.add_mutually_exclusive_group()
    data_modes.add_argument("--create-preview", metavar="ABSOLUTE_ROOT", help="Create a local-preview database in a new or empty outside-repository root")
    data_modes.add_argument("--open-preview", metavar="ABSOLUTE_ROOT", help="Open an existing local-preview database; never create a missing database")
    parser.add_argument("--preview-data-acknowledged", action="store_true",
                        help="Acknowledge plaintext storage of inputs including failures, replies, and states; manual backup/restore available separately; no encryption/deletion tooling")
    parser.add_argument("--session", help="Exact saved session UUID to restore; only with --open-preview")
    args = parser.parse_args(argv)
    if args.real_service:
        if not (args.endpoint and args.model and args.local_service_verified):
            parser.error("real service requires --endpoint, --model and --local-service-verified")
        try:
            config = BackendConfig("ollama", args.endpoint, args.model, True)
        except (ValueError, TypeError, ModelError):
            parser.error("invalid real-service endpoint or model; explicit loopback HTTP origin required")
    else:
        if args.endpoint is not None or args.model is not None or args.local_service_verified:
            parser.error("real-service settings require --real-service")
        config = BackendConfig("synthetic-http" if args.synthetic_http else "fake")
    if args.create_preview is not None or args.open_preview is not None:
        if not args.preview_data_acknowledged:
            parser.error("local preview requires --preview-data-acknowledged")
        if args.session is not None and args.open_preview is None:
            parser.error("--session requires --open-preview")
        try:
            session_id = UUID(args.session) if args.session is not None else None
            data = DesktopDataConfig("local_preview", args.create_preview if args.create_preview is not None else args.open_preview,
                "create" if args.create_preview is not None else "open",
                "new" if args.create_preview is not None else ("resume" if session_id is not None else "browse"),
                session_id, True)
        except (ValueError, TypeError):
            parser.error("local preview requires an absolute data root and a valid explicitly selected session UUID")
    else:
        if args.preview_data_acknowledged or args.session is not None:
            parser.error("preview-data acknowledgment and session require an explicit preview operation")
        data = DesktopDataConfig()
    try:
        import tkinter as tk
    except ImportError:
        print("Desktop preview requires Python with Tkinter.", file=sys.stderr)
        return 1
    try:
        root = tk.Tk()
    except tk.TclError:
        print("Desktop preview could not open a Tk window in this environment.", file=sys.stderr)
        return 1
    window = None
    callback_interruption = None
    exit_code = 0
    try:
        window = DesktopWindow(root, config=config, data=data)
        root.mainloop()
        callback_interruption = getattr(window, "_callback_interruption", None)
        if getattr(window, "_fatal_exit_requested", False) is True:
            exit_code = 1
    finally:
        # A console interrupt or early mainloop return also requests cleanup.
        # Do not call view methods after Tk has been destroyed, and do not
        # pretend this nonblocking close cancels an active model request.
        try:
            try:
                if window is not None and window.session is not None:
                    window.session.worker.close()
            finally:
                data_worker = getattr(window, "_data_worker", None)
                if data_worker is not None:
                    data_worker.close()
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass  # The normal window-close path may already have destroyed Tk.
    if callback_interruption is not None:
        raise callback_interruption
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
