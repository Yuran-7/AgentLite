from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from rich.markdown import Markdown
from rich.markup import escape
from textual import events
from textual.app import App, ComposeResult  # Textual 是用于构建终端用户界面（TUI）的第三方库
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, Static, TextArea

from agent_lite.core.config import AgentLiteConfig
from agent_lite.core.skills.loader import SkillLoader
from agent_lite.core.transport.socket_client import IpcError, SocketClient

log = logging.getLogger(__name__)
_BEIJING_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


# 将 session 的 ISO 8601 UTC 时间转换为北京时间分钟文本
def _format_session_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(_BEIJING_TIMEZONE).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value[:16].replace("T", " ")


def _preview(s: str, n: int) -> str:
    return s[:n] + "…" if len(s) > n else s


# 将秒数压缩为适合单行状态栏的时长文本
def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, whole_seconds = divmod(int(seconds), 60)
    return f"{minutes}m{whole_seconds:02d}s"


# 将 token 数量压缩为 K/M 单位，保留有意义的一位小数
def _format_token_count(count: int) -> str:
    if count >= 1_000_000:
        value = count / 1_000_000
        compact = f"{value:.0f}" if value >= 100 else f"{value:.1f}".rstrip("0").rstrip(".")
        return f"{compact}M"
    if count >= 1_000:
        value = count / 1_000
        compact = f"{value:.0f}" if value >= 100 else f"{value:.1f}".rstrip("0").rstrip(".")
        return f"{compact}K"
    return str(count)


def _params_str(params: dict[str, Any]) -> str:
    return json.dumps(params, ensure_ascii=False, indent=2)


# 从工具参数中提取最适合摘要展示的关键字段
def _param_summary(tool_name: str, params: dict[str, Any], max_len: int = 72) -> str:
    keys_by_tool = {
        "read_file": ("path",),
        "write_file": ("path",),
        "list_dir": ("path", "max_depth"),
        "bash": ("command",),  # legacy sessions
        "shell": ("command",),
    }
    keys = keys_by_tool.get(tool_name, ())
    parts = [f"{key}={params[key]!r}" for key in keys if key in params]
    if not parts:
        parts = [f"{key}={value!r}" for key, value in list(params.items())[:2]]
    return _preview(", ".join(parts), max_len)


class LLMStreamBlock(Static):
    """在同一个 Static widget 中累积 LLM 流式 token。"""

    DEFAULT_CSS = "LLMStreamBlock { padding: 0 2; color: $text; }"

    # 初始化为空文本块
    def __init__(self) -> None:
        super().__init__("")
        self._text = ""
        self._finalized = False

    # 追加一个 token 并刷新显示
    def append_token(self, token: str) -> None:
        if self._finalized:
            return
        self._text += token
        self.update(self._text)

    # 将累积文本渲染为 Markdown，供流式块结束后显示
    def finalize_markdown(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        if self._text.strip():
            self.update(Markdown(self._text, code_theme="monokai"))


class ToolCallBlock(Widget):
    """可折叠的工具调用块：折叠时显示摘要，点击后展开完整 params 和 output。"""

    DEFAULT_CSS = """
    ToolCallBlock { height: auto; padding: 0 2; color: $text-muted; }
    ToolCallBlock > .summary-row { height: 1; }
    ToolCallBlock > .summary-row > .summary {
        width: auto;
        color: $text-muted;
        transition: color 120ms linear;
    }
    ToolCallBlock > .summary-row > .chevron {
        width: 1;
        margin-left: 1;
        opacity: 0%;
        color: $text-muted;
        transition: opacity 120ms linear, color 120ms linear;
    }
    ToolCallBlock.expandable.hovered > .summary-row > .summary {
        color: $text;
        text-style: bold;
    }
    ToolCallBlock.expandable.hovered > .summary-row > .chevron {
        opacity: 100%;
        color: $text;
        text-style: bold;
    }
    ToolCallBlock > .detail { display: none; padding: 0 2 0 4; color: $text-muted; }
    ToolCallBlock.expanded > .detail { display: block; }
    """

    # 初始化工具调用信息
    def __init__(self, tool_name: str, params: dict[str, Any]) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._params = params
        self._params_full = _params_str(params)
        self._output = ""
        self._elapsed_ms = 0
        self._is_error = False
        self._finished = False

    # 组合工具摘要、悬停箭头和折叠详情区域
    def compose(self) -> ComposeResult:
        with Horizontal(classes="summary-row"):
            yield Static(self._summary(), classes="summary")
            yield Static(self._chevron(), classes="chevron")
        yield Static("", classes="detail")

    # 根据折叠状态返回与 Codex 风格一致的方向提示
    def _chevron(self) -> str:
        return "▾" if "expanded" in self.classes else ">"

    # 生成摘要行文本
    def _summary(self) -> str:
        params_pre = _param_summary(self._tool_name, self._params)
        line = f"  [dim]tool[/dim] [bold]{self._tool_name}[/bold]"
        if params_pre:
            line += f"  [dim]{params_pre}[/dim]"
        if self._finished:
            color = "red" if self._is_error else "green"
            status = "failed" if self._is_error else "done"
            hint = "  [dim](click to expand)[/dim]" if self._output else ""
            line += f"  [{color}]{status}[/{color}]  [dim]{self._elapsed_ms}ms[/dim]{hint}"
        return line

    # 工具调用完成时更新结果并刷新摘要（widget 未挂载时跳过 DOM 更新）
    def set_result(self, output: str, elapsed_ms: int, *, is_error: bool = False) -> None:
        self._output = output
        self._elapsed_ms = elapsed_ms
        self._is_error = is_error
        self._finished = True
        self.add_class("expandable")
        if self.children:
            self.query_one(".summary", Static).update(self._summary())
            self.query_one(".chevron", Static).update(self._chevron())

    # 鼠标进入工具块或其子控件时启用可点击的悬停样式
    def on_enter(self, event: events.Enter) -> None:
        if self._finished:
            self.add_class("hovered")

    # 鼠标离开当前命中区域时清除悬停样式，进入相邻子控件会立即重新启用
    def on_leave(self, event: events.Leave) -> None:
        self.remove_class("hovered")

    # 点击时切换展开/折叠状态
    def on_click(self) -> None:
        if not self._finished:
            return
        if "expanded" in self.classes:
            self.remove_class("expanded")
        else:
            detail = self.query_one(".detail", Static)
            detail.update(
                f"[dim]params[/dim]\n{self._params_full}\n\n"
                f"[dim]output[/dim]\n{self._output}\n\n"
                f"[dim]elapsed:[/dim] {self._elapsed_ms}ms"
            )
            self.add_class("expanded")
        self.query_one(".chevron", Static).update(self._chevron())


class PlanBlock(Static):
    """展示某个 run 的最新计划，并在后续更新时复用同一个区块。"""

    DEFAULT_CSS = "PlanBlock { padding: 0 2; color: $text-muted; }"

    # 初始化计划展示区块
    def __init__(
        self,
        run_id: str,
        plan: list[dict[str, Any]],
        explanation: str | None = None,
    ) -> None:
        self._run_id = run_id
        self._plan = plan
        self._explanation = explanation or ""
        super().__init__(self._render_plan())

    # 更新计划内容并刷新展示文本
    def set_plan(self, plan: list[dict[str, Any]], explanation: str | None = None) -> None:
        self._plan = plan
        self._explanation = explanation or ""
        self.update(self._render_plan())

    # 将计划状态渲染为简洁的 Codex 风格清单
    def _render_plan(self) -> str:
        title = f"  [bold cyan]plan[/bold cyan]  [dim]{escape(self._run_id[:12])}[/dim]"
        if self._explanation:
            title += f"  [dim]{escape(self._explanation)}[/dim]"
        lines = [title]
        icons = {"completed": "[green][x][/green]", "in_progress": "[yellow][>][/yellow]"}
        for item in self._plan:
            status = str(item.get("status", "pending"))
            icon = icons.get(status, "[dim][ ][/dim]")
            lines.append(f"    {icon} {escape(str(item.get('step', '')))}")
        return "\n".join(lines)


class PermissionSelect(Static):
    """内联权限选择控件：挂载在日志流中，键盘焦点无需 ModalScreen。"""

    can_focus = True

    DEFAULT_CSS = """
    PermissionSelect {
        height: auto;
        padding: 0 2;
        margin-bottom: 1;
    }
    """

    _CHOICES: tuple[tuple[str, str, str], ...] = (
        ("allow_once",   "Allow once",   "y / 1"),
        ("always_allow", "Always allow", "a / 2"),
        ("deny_once",    "Deny",         "n / 3"),
        ("always_deny",  "Always deny",  "d / 4"),
    )
    _KEY_MAP: dict[str, str] = {
        "y": "allow_once",  "1": "allow_once",
        "a": "always_allow","2": "always_allow",
        "n": "deny_once",   "3": "deny_once",
        "d": "always_deny", "4": "always_deny",
    }

    # 用户作出权限决策时发布，携带工具 ID 和决策字符串
    class Decided(Message):
        # 初始化决策消息，存储控件引用、工具 ID 和决策
        def __init__(self, widget: PermissionSelect, tool_use_id: str, decision: str) -> None:
            self.widget = widget
            self.tool_use_id = tool_use_id
            self.decision = decision
            super().__init__()

    # 初始化控件，存储工具 ID（用于 IPC 回复）
    def __init__(self, tool_use_id: str) -> None:
        super().__init__("")
        self._tool_use_id = tool_use_id
        self._cursor = 0

    def on_mount(self) -> None:
        self.update(self._render_ui())
        self.focus()
        log.debug(
            "PermissionSelect.on_mount  can_focus=%s  focused_after=%r",
            self.can_focus,
            self.app.focused,
        )
        self.app.call_after_refresh(self._log_deferred_focus)

    # 在下一帧记录焦点是否真正转移到本控件
    def _log_deferred_focus(self) -> None:
        log.debug(
            "PermissionSelect.deferred_focus  app.focused=%r  has_focus=%s  focusable=%s",
            self.app.focused,
            self.has_focus,
            self.focusable,
        )

    # 焦点到达时记录，用于确认 focus() 是否真正生效
    def on_focus(self, event: events.Focus) -> None:
        log.debug("PermissionSelect.on_focus  has_focus=%s  app.focused=%r", self.has_focus, self.app.focused)

    # 焦点离开时记录，用于追踪是否被其他控件抢走焦点
    def on_blur(self, event: events.Blur) -> None:
        log.debug("PermissionSelect.on_blur  app.focused=%r", self.app.focused)

    # 生成带光标高亮的选项列表文本
    def _render_ui(self) -> str:
        lines: list[str] = []
        for i, (_, label, key_hint) in enumerate(self._CHOICES):
            if i == self._cursor:
                lines.append(f"  [bold cyan]❯ {label}[/bold cyan]  [dim]{key_hint}[/dim]")
            else:
                lines.append(f"    {label}  [dim]{key_hint}[/dim]")
        lines.append("[dim]  ↑↓ navigate   enter confirm[/dim]")
        return "\n".join(lines)

    # 方向键导航；快捷键直接选择；enter 确认光标位置
    def on_key(self, event: events.Key) -> None:
        log.debug("PermissionSelect.on_key  key=%r  char=%r", event.key, event.character)
        key = event.key
        if key in ("up", "k"):
            event.stop()
            self._cursor = (self._cursor - 1) % len(self._CHOICES)
            self.update(self._render_ui())
        elif key in ("down", "j"):
            event.stop()
            self._cursor = (self._cursor + 1) % len(self._CHOICES)
            self.update(self._render_ui())
        elif key == "enter":
            event.stop()
            self._pick(self._CHOICES[self._cursor][0])
        else:
            decision = self._KEY_MAP.get(key)
            if decision is not None:
                event.stop()
                self._pick(decision)

    # 发布决策消息，由宿主 App 负责 IPC 回复和控件清理
    def _pick(self, decision: str) -> None:
        log.debug("PermissionSelect._pick  decision=%s", decision)
        self.post_message(self.Decided(self, self._tool_use_id, decision))


class PermissionBlock(Static):
    """日志里的权限审批摘要"""

    _LABEL_MAP: dict[str, str] = {
        "allow_once":   "allowed (once)",
        "always_allow": "always allowed",
        "deny_once":    "denied",
        "always_deny":  "always denied",
        "timeout":      "⏱ timed out",
    }
    LABEL_MAP = _LABEL_MAP

    # 子类提交消息：用户作出权限决策时发布
    class Resolved(Message):
        def __init__(self, block: PermissionBlock, decision: str) -> None:
            self.block = block
            self.decision = decision
            super().__init__()

    # 初始化审批块，记录工具 ID、名称和参数预览
    def __init__(self, tool_use_id: str, tool_name: str, param_preview: str) -> None:
        self._tool_use_id = tool_use_id
        self._tool_name = tool_name
        self._param_preview = param_preview
        self._resolved = False
        super().__init__(self._pending_text(), classes="log-line")

    def _pending_text(self) -> str:
        preview = f"  [dim]{self._param_preview}[/dim]" if self._param_preview else ""
        return f"[bold red]? permission[/bold red]  [bold]{self._tool_name}[/bold]{preview}"

    # 将块收缩为单行摘要并发布 Resolved 消息
    def _resolve(self, decision: str) -> None:
        if self._resolved:
            return
        self._resolved = True
        allowed = decision in ("allow_once", "always_allow")
        icon = "[bold green]✓[/bold green]" if allowed else "[bold red]✗[/bold red]"
        label = self._LABEL_MAP.get(decision, decision)
        preview = f"  [dim]{self._param_preview}[/dim]" if self._param_preview else ""
        self.update(
            f"{icon} permission  [bold]{self._tool_name}[/bold]{preview}  [dim]{label}[/dim]"
        )
        self.post_message(self.Resolved(self, decision))


SlashItem = tuple[str, str, bool]  # name, description, is_skill


class ResumeSelect(Static):
    """历史 session 选择器；跨工作区时在原目录和当前目录之间二次确认。"""

    can_focus = True

    DEFAULT_CSS = """
    ResumeSelect {
        height: auto;
        max-height: 14;
        padding: 0 2;
        margin-bottom: 1;
    }
    """

    class Confirmed(Message):
        # 初始化恢复确认消息，标记是否用当前工作区覆盖历史工作区
        def __init__(
            self,
            widget: ResumeSelect,
            session_id: str,
            use_current_workspace: bool,
        ) -> None:
            self.widget = widget
            self.session_id = session_id
            self.use_current_workspace = use_current_workspace
            super().__init__()

    class Cancelled(Message):
        # 初始化恢复取消消息
        def __init__(self, widget: ResumeSelect) -> None:
            self.widget = widget
            super().__init__()

    # 初始化历史列表和当前 TUI 工作区
    def __init__(self, sessions: list[dict[str, Any]], current_workspace: str | None) -> None:
        super().__init__("")
        self._sessions = sessions
        self._current_workspace = current_workspace
        self._cursor = 0
        self._workspace_cursor = 0
        self._choosing_workspace = False

    # 首次挂载时渲染列表并接管键盘焦点
    def on_mount(self) -> None:
        self.update(self._render_ui())
        self.focus()

    # 渲染 session 列表或跨工作区二次确认页
    def _render_ui(self) -> str:
        if self._choosing_workspace:
            selected = self._sessions[self._cursor]
            original = selected.get("workspace_root") or "(no workspace)"
            choices = (
                ("Session workspace", str(original)),
                ("Current workspace", self._current_workspace or "(no workspace)"),
            )
            lines = ["  [bold cyan]Choose workspace[/bold cyan]"]
            for index, (label, path) in enumerate(choices):
                prefix = "❯" if index == self._workspace_cursor else " "
                value = f"{prefix} {label}  [dim]{escape(path)}[/dim]"
                lines.append(
                    f"  [bold cyan]{value}[/bold cyan]"
                    if index == self._workspace_cursor
                    else f"  {value}"
                )
            lines.append("  [dim]↑↓ navigate   enter confirm   esc back[/dim]")
            return "\n".join(lines)

        lines = ["  [bold cyan]Resume session[/bold cyan]"]
        start = max(0, min(self._cursor - 3, max(0, len(self._sessions) - 8)))
        for index in range(start, min(start + 8, len(self._sessions))):
            session = self._sessions[index]
            title = escape(str(session.get("title") or "Untitled session"))
            workspace = escape(str(session.get("workspace_root") or "(no workspace)"))
            updated = escape(_format_session_time(str(session.get("updated_at", ""))))
            prefix = "❯" if index == self._cursor else " "
            value = f"{prefix} {title}  [dim]{updated}  {workspace}[/dim]"
            lines.append(
                f"  [bold cyan]{value}[/bold cyan]"
                if index == self._cursor
                else f"  {value}"
            )
        lines.append("  [dim]↑↓ navigate   enter resume   esc cancel[/dim]")
        return "\n".join(lines)

    # 判断两个可选工作区是否指向同一个规范化路径
    @staticmethod
    def _same_workspace(left: str | None, right: str | None) -> bool:
        if left is None or right is None:
            return left == right
        return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)

    # 处理列表导航、工作区选择、确认和取消
    def on_key(self, event: events.Key) -> None:
        key = event.key
        if key in ("up", "k"):
            event.stop()
            if self._choosing_workspace:
                self._workspace_cursor = (self._workspace_cursor - 1) % 2
            else:
                self._cursor = (self._cursor - 1) % len(self._sessions)
            self.update(self._render_ui())
        elif key in ("down", "j"):
            event.stop()
            if self._choosing_workspace:
                self._workspace_cursor = (self._workspace_cursor + 1) % 2
            else:
                self._cursor = (self._cursor + 1) % len(self._sessions)
            self.update(self._render_ui())
        elif key == "enter":
            event.stop()
            selected = self._sessions[self._cursor]
            if self._choosing_workspace:
                self.post_message(
                    self.Confirmed(
                        self,
                        str(selected["session_id"]),
                        self._workspace_cursor == 1,
                    )
                )
            elif self._same_workspace(
                selected.get("workspace_root"), self._current_workspace
            ):
                self.post_message(
                    self.Confirmed(self, str(selected["session_id"]), False)
                )
            else:
                self._choosing_workspace = True
                self.update(self._render_ui())
        elif key == "escape":
            event.stop()
            if self._choosing_workspace:
                self._choosing_workspace = False
                self.update(self._render_ui())
            else:
                self.post_message(self.Cancelled(self))


class MemorySelect(Static):
    """内联记忆开关选择器；仅保存 TUI 选择，具体记忆逻辑另行实现。"""

    can_focus = True

    DEFAULT_CSS = """
    MemorySelect {
        height: auto;
        padding: 0 2;
        margin-bottom: 1;
    }
    """

    _CHOICES: tuple[tuple[str, str], ...] = (
        ("generate", "Generate memories"),
        ("use", "Use memories"),
    )

    class Confirmed(Message):
        def __init__(
            self,
            widget: MemorySelect,
            generate_enabled: bool,
            use_enabled: bool,
        ) -> None:
            self.widget = widget
            self.generate_enabled = generate_enabled
            self.use_enabled = use_enabled
            super().__init__()

    class Cancelled(Message):
        def __init__(self, widget: MemorySelect) -> None:
            self.widget = widget
            super().__init__()

    def __init__(self, generate_enabled: bool = False, use_enabled: bool = False) -> None:
        super().__init__("")
        self._cursor = 0
        self._enabled = {
            "generate": generate_enabled,
            "use": use_enabled,
        }

    def on_mount(self) -> None:
        self.update(self._render_ui())
        self.focus()

    def _render_ui(self) -> str:
        lines = ["  [bold cyan]Memory settings[/bold cyan]"]
        for i, (key, label) in enumerate(self._CHOICES):
            marker = "√" if self._enabled[key] else " "
            prefix = "❯" if i == self._cursor else " "
            text = f"{prefix} [{marker}] {label}"
            lines.append(
                f"  [bold cyan]{text}[/bold cyan]"
                if i == self._cursor
                else f"  {text}"
            )
        lines.append("  [dim]↑↓ navigate   space toggle   enter confirm   esc cancel[/dim]")
        return "\n".join(lines)

    def _toggle_current(self) -> None:
        key = self._CHOICES[self._cursor][0]
        self._enabled[key] = not self._enabled[key]
        self.update(self._render_ui())

    def on_key(self, event: events.Key) -> None:
        key = event.key
        if key in ("up", "k"):
            event.stop()
            self._cursor = (self._cursor - 1) % len(self._CHOICES)
            self.update(self._render_ui())
        elif key in ("down", "j"):
            event.stop()
            self._cursor = (self._cursor + 1) % len(self._CHOICES)
            self.update(self._render_ui())
        elif key in ("space", "g", "u"):
            event.stop()
            if key == "g":
                self._enabled["generate"] = not self._enabled["generate"]
                self._cursor = 0
                self.update(self._render_ui())
            elif key == "u":
                self._enabled["use"] = not self._enabled["use"]
                self._cursor = 1
                self.update(self._render_ui())
            else:
                self._toggle_current()
        elif key == "enter":
            event.stop()
            self.post_message(
                self.Confirmed(
                    self,
                    self._enabled["generate"],
                    self._enabled["use"],
                )
            )
        elif key == "escape":
            event.stop()
            self.post_message(self.Cancelled(self))


class SlashCompleteWidget(Static):
    """斜杠命令自动补全弹出框：区分通用命令和 Skill，并支持筛选与选择。"""

    can_focus = False

    DEFAULT_CSS = """
    SlashCompleteWidget {
        height: auto;
        padding: 0 1;
        margin: 0 2;
        background: $surface;
        border: round $surface-lighten-2;
    }
    """

    # 用户选中某条命令时发布
    class Selected(Message):
        # 初始化，携带被选中的 skill 名称
        def __init__(self, skill_name: str) -> None:
            self.skill_name = skill_name
            super().__init__()

    # 初始化，接收全量 (name, description, is_skill) 列表
    def __init__(self, items: list[SlashItem]) -> None:
        super().__init__("")
        self._all_items = items
        self._filtered: list[SlashItem] = list(items)
        self._cursor = 0

    # 根据查询字符串筛选列表，重置光标并重新渲染
    def set_query(self, query: str) -> None:
        q = query.lower()
        self._filtered = [item for item in self._all_items if not q or q in item[0].lower()]
        self._cursor = min(self._cursor, max(0, len(self._filtered) - 1))
        if self.is_attached:
            self._redraw()

    # 向上移动光标并重新渲染
    def move_up(self) -> None:
        if self._filtered:
            self._cursor = (self._cursor - 1) % len(self._filtered)
            self._redraw()

    # 向下移动光标并重新渲染
    def move_down(self) -> None:
        if self._filtered:
            self._cursor = (self._cursor + 1) % len(self._filtered)
            self._redraw()

    # 选中当前光标项并发布 Selected 消息
    def select_current(self) -> None:
        if self._filtered:
            self.post_message(self.Selected(self._filtered[self._cursor][0]))

    # 返回当前是否有可选项
    def has_selection(self) -> bool:
        return len(self._filtered) > 0

    def on_mount(self) -> None:
        self._redraw()

    # 渲染筛选后的命令列表，高亮当前光标项
    def _redraw(self) -> None:
        if not self._filtered:
            self.update("[dim]  no matching commands[/dim]")
            return
        lines: list[str] = []
        skills_heading_added = False
        for i, (name, desc, is_skill) in enumerate(self._filtered):
            if is_skill and not skills_heading_added:
                lines.append("[dim]  Skills[/dim]")
                skills_heading_added = True
            desc_part = f"  [dim]{desc}[/dim]" if desc else ""
            if i == self._cursor:
                lines.append(f"  [bold cyan]❯ /{name}[/bold cyan]{desc_part}")
            else:
                lines.append(f"    [cyan]/{name}[/cyan]{desc_part}")
        lines.append("[dim]  ↑↓ navigate   tab/enter select   esc dismiss[/dim]")
        self.update("\n".join(lines))


class ChatTextArea(TextArea):
    """支持 Enter 提交、Cmd/Shift/Alt+Enter 换行的多行聊天输入框。"""

    DEFAULT_CSS = """
    ChatTextArea {
        height: auto;
        min-height: 3;
        max-height: 12;
        border: round $surface-lighten-2;
        background: $background;
        padding: 0 1;
        margin: 1 2 0 2;
        scrollbar-size-vertical: 1;
    }
    ChatTextArea:focus {
        border: round $accent;
        background: $background;
    }
    """

    # 子类自定义的提交消息，供宿主 App 监听
    class Submitted(Message):
        def __init__(self, area: ChatTextArea) -> None:
            self.text_area = area
            self.value = area.text
            super().__init__()

    # 输入内容以 / 开头且无空格时发布，query 为 / 之后的字符串（可为空串）；None 表示收起弹窗
    class SlashChanged(Message):
        def __init__(self, query: str | None) -> None:
            self.query = query
            super().__init__()

    # 文本变化时检测 / 前缀，通知宿主 App 更新自动补全弹窗
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        text = self.text
        if text.startswith("/") and " " not in text:
            self.post_message(ChatTextArea.SlashChanged(query=text[1:]))
        else:
            self.post_message(ChatTextArea.SlashChanged(query=None))

    # Enter 提交；↑↓/Tab/Esc 路由到自动补全弹窗；Cmd/Shift/Alt+Enter 插入换行；其余键交回 TextArea
    async def _on_key(self, event: events.Key) -> None:
        key = event.key

        popup: SlashCompleteWidget | None = None
        try:
            popup = self.app.query_one(SlashCompleteWidget)
        except NoMatches:
            popup = None

        if key == "enter":
            event.stop()
            event.prevent_default()
            if popup is not None and popup.has_selection():
                popup.select_current()
                return
            if self.text.strip():
                self.post_message(self.Submitted(self))
            return
        if key in ("alt+enter", "shift+enter", "ctrl+j", "super+enter"):
            event.stop()
            event.prevent_default()
            if not self.read_only:
                self.insert("\n")
            return
        if popup is not None:
            if key == "up":
                event.stop()
                event.prevent_default()
                popup.move_up()
                return
            elif key == "down":
                event.stop()
                event.prevent_default()
                popup.move_down()
                return
            elif key == "tab":
                event.stop()
                event.prevent_default()
                popup.select_current()
                return
            elif key == "escape":
                event.stop()
                event.prevent_default()
                self.post_message(ChatTextArea.SlashChanged(query=None))
                return
        await super()._on_key(event)


class AgentLiteTuiApp(App[None]):
    """AgentLite TUI：终端滚屏风格，实时展示 agent 执行过程。"""

    TITLE = "AgentLite"
    BINDINGS = [
        Binding("ctrl+c", "quit", "quit", priority=True),
    ]
    CSS = """
    Screen { background: $background; }
    #header {
        height: 2;
        background: $surface;
        color: $text;
        padding: 0 1;
    }
    #log-view {
        height: 1fr;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
    }
    #banner { padding: 1 2 0 2; }
    Static.user-turn { color: $text; padding: 1 2 0 2; }
    Static.run-header { color: $text-muted; padding: 1 2 0 2; }
    Static.step-divider { color: $text-muted; padding: 0 2; }
    Static.run-ok { color: green; padding: 0 2 1 2; }
    Static.run-err { color: red; padding: 0 2 1 2; }
    #context-status {
        height: 1;
        padding: 0 3;
        margin-bottom: 1;
        color: $text-muted;
    }
    Static.log-line { padding: 0 2; }
    """

    _BANNER = (
        "[bold cyan] █████╗  ██████╗ ███████╗███╗   ██╗████████╗██╗     ██╗████████╗███████╗[/bold cyan]\n"
        "[bold cyan]██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝██║     ██║╚══██╔══╝██╔════╝[/bold cyan]\n"
        "[bold cyan]███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║   ██║     ██║   ██║   █████╗  [/bold cyan]\n"
        "[bold cyan]██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║   ██║     ██║   ██║   ██╔══╝  [/bold cyan]\n"
        "[bold cyan]██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║   ███████╗██║   ██║   ███████╗[/bold cyan]\n"
        "[bold cyan]╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝   ╚══════╝╚═╝   ╚═╝   ╚══════╝[/bold cyan]\n"
        "[dim]  输入消息开始对话  ·  键入 / 触发 skill  ·  Ctrl+C 退出[/dim]"
    )

    # 初始化连接参数和 TUI 内部状态
    def __init__(
        self,
        host: str,
        port: int,
        replay_run_id: str | None = None,
        llm_protocol: str = "anthropic",
        workspace_root: str | None = None,
        model: str = "",
    ) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._replay_run_id = replay_run_id
        self._llm_protocol = llm_protocol
        self._workspace_root = workspace_root
        self._model_name = model
        self._header_state = "connecting"
        self._client: SocketClient | None = None
        self._current_llm: LLMStreamBlock | None = None
        self._pending_tool_blocks: dict[str, ToolCallBlock] = {}
        self._pending_permission_blocks: dict[str, PermissionBlock] = {}
        self._plan_blocks: dict[str, PlanBlock] = {}
        self._session_id: str | None = None
        self._busy = False
        self._last_context_pct: float = 0.0
        self._last_usage: tuple[int, int, int] = (0, 0, 0)
        self._rounds = 0
        self._steps = 0
        self._llm_elapsed_s = 0.0
        self._tool_elapsed_s = 0.0
        self._ttft_total_s = 0.0
        self._ttft_samples = 0
        self._generation_elapsed_s = 0.0
        self._throughput_output_tokens = 0
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_cache_read_tokens = 0
        self._llm_calls: dict[str, tuple[float, float | None]] = {}
        self._session_stats: dict[str, dict[str, Any]] = {}
        self._slash_items: list[SlashItem] = []
        self._memory_generate_enabled = False
        self._memory_use_enabled = False
        self._subagent_run_ids: dict[str, str] = {}  # child run_id -> description
        self._subagent_start_times: dict[str, float] = {}  # child run_id -> start time

    def compose(self) -> ComposeResult:
        yield Label("[bold]AgentLite[/bold]  [dim]connecting...[/dim]", id="header")
        yield VerticalScroll(id="log-view")
        yield ChatTextArea(id="prompt", show_line_numbers=False)
        yield Static(self._render_context_status(), id="context-status")

    def on_mount(self) -> None:
        self._slash_items = self._build_slash_items()
        self._append(Static(self._BANNER, id="banner"))
        self.run_worker(self._socket_loop(), exclusive=True, name="socket")
        prompt = self.query_one("#prompt", ChatTextArea)
        prompt.disabled = True
        prompt.border_title = "connecting..."

    # 构建斜杠命令候选列表：内建命令 + 所有已注册 skill
    def _build_slash_items(self) -> list[SlashItem]:
        items: list[SlashItem] = [
            ("new", "start a new session", False),
            ("resume", "resume a previous session", False),
            ("compact", "compress context window", False),
            ("memories", "configure and review memories", False),
        ]
        try:
            loader = SkillLoader()
            for skill in loader.list_all_skills():
                desc = skill.description.splitlines()[0] if skill.description else ""
                if len(desc) > 60:
                    desc = desc[:57] + "..."
                items.append((skill.name, desc, True))
        except Exception:
            pass
        return items

    # 构造新会话参数；入口默认已将当前目录作为 workspace
    def _session_create_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"mode": "chat"}
        if self._workspace_root is not None:
            params["workspace_root"] = self._workspace_root
        return params

    # 根据 / 前缀查询字符串挂载、更新或移除自动补全弹窗
    def on_chat_text_area_slash_changed(self, event: ChatTextArea.SlashChanged) -> None:
        query = event.query
        if query is None:
            try:
                self.query_one(SlashCompleteWidget).remove()
            except NoMatches:
                pass
            return
        try:
            popup = self.query_one(SlashCompleteWidget)
            popup.set_query(query)
        except NoMatches:
            popup = SlashCompleteWidget(self._slash_items)
            self.mount(popup, before="#prompt")
            popup.set_query(query)

    # 用户选中补全项后填入命令；/resume 和 /memories 直接打开对应选择页
    def on_slash_complete_widget_selected(self, event: SlashCompleteWidget.Selected) -> None:
        prompt = self._prompt()
        try:
            self.query_one(SlashCompleteWidget).remove()
        except NoMatches:
            pass
        if event.skill_name == "memories":
            if prompt is not None:
                self._open_memory_settings(prompt)
            return
        if event.skill_name == "resume":
            if prompt is not None:
                self._open_resume_selector(prompt)
            return
        if prompt is not None:
            prompt.text = f"/{event.skill_name} "
            prompt.move_cursor(prompt.document.end)

    # 记录按键焦点；当 PermissionSelect 失去焦点后作为兜底处理权限快捷键
    def on_key(self, event: events.Key) -> None:
        log.debug("App.on_key  key=%r  focused=%r", event.key, self.focused)
        if not self._pending_permission_blocks:
            return
        try:
            select = self.query_one(PermissionSelect)
            if select.has_focus:
                return  # PermissionSelect 有焦点时自行处理，事件不会冒泡到这里
            key = event.key
            decision = PermissionSelect._KEY_MAP.get(key)
            if decision:
                event.stop()
                select._pick(decision)
            elif key in ("up", "k"):
                event.stop()
                select._cursor = (select._cursor - 1) % len(PermissionSelect._CHOICES)
                select.update(select._render_ui())
            elif key in ("down", "j"):
                event.stop()
                select._cursor = (select._cursor + 1) % len(PermissionSelect._CHOICES)
                select.update(select._render_ui())
            elif key == "enter":
                event.stop()
                select._pick(PermissionSelect._CHOICES[select._cursor][0])
        except Exception:
            pass

    # 退出前尽力关闭当前 session，失败也不阻塞 TUI 退出
    async def action_quit(self) -> None:
        if self._client is not None and self._session_id is not None:
            try:
                await self._persist_session_stats(self._session_id)
                await self._client.send_command("session.close", {"session_id": self._session_id})
            except (IpcError, RuntimeError, OSError):
                self._append(Static("[yellow]warning: failed to close session[/yellow]"))
        self.exit()

    # 将输入框提交内容发送给当前 chat session；用 worker 发送，避免 await 阻塞 App 消息泵
    async def on_chat_text_area_submitted(self, event: ChatTextArea.Submitted) -> None:
        content = event.value.strip()
        if not content:
            return
        # 检测 /new 指令
        if content == "/new":
            event.text_area.text = ""
            if self._client is None or self._session_id is None or self._busy:
                self._append(
                    Static("[yellow]agent busy or disconnected[/yellow]", classes="log-line")
                )
                return
            self._busy = True
            event.text_area.disabled = True
            event.text_area.border_title = "starting a new session..."
            self._update_header("connecting")
            self.run_worker(self._do_new_session(), name="new_session", exclusive=False)
            return
        # 检测 /resume 指令并加载全部历史 session 供用户选择
        if content == "/resume":
            self._open_resume_selector(event.text_area)
            return
        # 检测 /compact 指令
        if content == "/compact":
            event.text_area.text = ""
            if self._client is not None and self._session_id is not None and not self._busy:
                self.run_worker(self._do_compact(), name="compact", exclusive=False)
            return
        # 检测 /memories 指令；先展示 Generate/Use 两个独立开关，暂不执行具体逻辑
        if content == "/memories":
            self._open_memory_settings(event.text_area)
            return
        if content.startswith("/memories "):
            event.text_area.text = ""
            if self._client is None or self._session_id is None or self._busy:
                self._append(
                    Static("[yellow]agent busy or disconnected[/yellow]", classes="log-line")
                )
                return
            self.run_worker(
                self._do_memory_command(content[len("/memories "):].strip()),
                name="memory_command",
                exclusive=False,
            )
            return
        if self._client is None or self._session_id is None or self._busy:
            self._append(Static("[yellow]agent busy or disconnected[/yellow]", classes="log-line"))
            return
        self._busy = True
        prompt = event.text_area
        prompt.text = ""
        prompt.disabled = True
        prompt.read_only = False
        prompt.border_title = "agent is working..."
        self._append(Static(f"[bold]>[/bold] {content}", classes="user-turn"))
        self._update_header("running")
        self.run_worker(self._do_send_message(content), name="send_message", exclusive=False)

    # 打开 /memories 设置页；这里只处理 TUI 交互，不执行具体记忆逻辑
    def _open_memory_settings(self, prompt: ChatTextArea) -> None:
        prompt.text = ""
        if self._client is None or self._session_id is None or self._busy:
            self._append(
                Static("[yellow]agent busy or disconnected[/yellow]", classes="log-line")
            )
            return
        prompt.disabled = True
        prompt.border_title = "configure memory settings..."
        self._mount_memory_select(
            MemorySelect(
                generate_enabled=self._memory_generate_enabled,
                use_enabled=self._memory_use_enabled,
            )
        )

    # 禁用输入框并异步加载历史 session 选择器
    def _open_resume_selector(self, prompt: ChatTextArea) -> None:
        prompt.text = ""
        if self._client is None or self._session_id is None or self._busy:
            self._append(
                Static("[yellow]agent busy or disconnected[/yellow]", classes="log-line")
            )
            return
        prompt.disabled = True
        prompt.border_title = "loading sessions..."
        self.run_worker(self._do_open_resume(), name="resume_list", exclusive=False)

    # 在 worker 中执行手动压缩命令，完成后显示结果横幅
    async def _do_compact(self) -> None:
        if self._client is None or self._session_id is None:
            return
        self._append(Static("[dim]⚡ compacting context...[/dim]", classes="log-line"))
        try:
            result = await self._client.send_command(
                "session.compact",
                {"session_id": self._session_id, "focus": ""},
            )
            summary_tokens = result.get("summary_tokens", 0)
            saved_tokens = result.get("saved_tokens", 0)
            self._last_context_pct = 0.0
            self._last_usage = (int(summary_tokens or 0), 0, 0)
            self._update_context_status()
            self._append(Static(
                f"[bold cyan]⚡ Context compacted[/bold cyan]"
                f"  [dim]summary={summary_tokens} tokens  saved≈{saved_tokens} tokens[/dim]",
                classes="log-line",
            ))
        except (IpcError, RuntimeError, OSError) as e:
            self._append(Static(f"[red]compact error: {e}[/red]", classes="log-line"))
        self._update_header("ready")

    # 接收 /memories 选择结果；这里只保存 TUI 状态，不连接具体记忆实现
    def on_memory_select_confirmed(self, msg: MemorySelect.Confirmed) -> None:
        self._memory_generate_enabled = msg.generate_enabled
        self._memory_use_enabled = msg.use_enabled
        msg.widget.remove()
        self._restore_prompt_after_memory_select()
        generate = "on" if self._memory_generate_enabled else "off"
        use = "on" if self._memory_use_enabled else "off"
        self._append(Static(
            f"[bold cyan]memory settings[/bold cyan]  "
            f"Generate memories: {generate}  ·  Use memories: {use}  "
            "[dim](saved for this session)[/dim]",
            classes="log-line",
        ))
        if self._client is not None and self._session_id is not None:
            self.run_worker(
                self._do_set_memory(msg.generate_enabled, msg.use_enabled),
                name="set_memory",
                exclusive=False,
            )

    # 取消 /memories 选择，不改变之前的开关状态
    def on_memory_select_cancelled(self, msg: MemorySelect.Cancelled) -> None:
        msg.widget.remove()
        self._restore_prompt_after_memory_select()

    # 接收历史 session 选择结果并启动恢复流程
    def on_resume_select_confirmed(self, msg: ResumeSelect.Confirmed) -> None:
        msg.widget.remove()
        self._busy = True
        self._update_header("connecting")
        self.run_worker(
            self._do_resume_session(msg.session_id, msg.use_current_workspace),
            name="resume_session",
            exclusive=False,
        )

    # 取消历史 session 选择并恢复输入框
    def on_resume_select_cancelled(self, msg: ResumeSelect.Cancelled) -> None:
        msg.widget.remove()
        self._restore_prompt_after_resume()

    # 从 core 加载全部历史 session 并挂载选择器
    async def _do_open_resume(self) -> None:
        if self._client is None:
            self._restore_prompt_after_resume()
            return
        try:
            result = await self._client.send_command("session.list", {})
            sessions = [
                session
                for session in result.get("sessions", [])
                if session.get("session_id") != self._session_id
            ]
            if not sessions:
                self._append(
                    Static("[dim]no previous sessions found[/dim]", classes="log-line")
                )
                self._restore_prompt_after_resume()
                return
            self.mount(
                ResumeSelect(sessions, self._workspace_root),
                before="#prompt",
            )
        except (IpcError, RuntimeError, OSError, KeyError, TypeError) as exc:
            self._append(Static(f"[red]resume list error: {exc}[/red]", classes="log-line"))
            self._restore_prompt_after_resume()

    # 恢复选中的 session、历史消息、事件订阅和 session 级设置
    async def _do_resume_session(
        self, session_id: str, use_current_workspace: bool
    ) -> None:
        if self._client is None:
            self._busy = False
            self._restore_prompt_after_resume()
            return
        old_session_id = self._session_id
        if old_session_id is not None:
            self._remember_session_stats(old_session_id)
            await self._persist_session_stats(old_session_id)
        try:
            params: dict[str, Any] = {"session_id": session_id}
            if use_current_workspace and self._workspace_root is not None:
                params["workspace_root"] = self._workspace_root
            resumed = await self._client.send_command("session.resume", params)
            history = await self._client.send_command(
                "session.get_history", {"session_id": session_id}
            )
            await self._subscribe_to_session(session_id)
            self._session_id = session_id
            self._workspace_root = (
                str(resumed["workspace_root"])
                if resumed.get("workspace_root") is not None
                else None
            )
            self._memory_generate_enabled = bool(
                resumed.get("memory_generate_enabled", False)
            )
            self._memory_use_enabled = bool(resumed.get("memory_use_enabled", True))
            self._restore_session_stats(session_id, resumed.get("stats"))
            if old_session_id is not None and old_session_id != session_id:
                try:
                    await self._client.send_command(
                        "session.close", {"session_id": old_session_id}
                    )
                except (IpcError, RuntimeError, OSError):
                    log.warning("failed to close replaced session session_id=%s", old_session_id)
            await self._show_resumed_history(
                str(resumed.get("title") or "Untitled session"),
                history.get("messages", []),
            )
            self._busy = False
            self._restore_prompt_after_resume()
            self._update_header("ready")
        except (IpcError, RuntimeError, OSError, KeyError, TypeError) as exc:
            self._busy = False
            self._restore_prompt_after_resume()
            self._update_header("ready")
            self._append(Static(f"[red]resume error: {exc}[/red]", classes="log-line"))

    # 用持久化 thread 重建恢复后的 TUI 对话历史
    async def _show_resumed_history(
        self, title: str, messages: list[dict[str, Any]]
    ) -> None:
        self._break_llm()
        self._pending_tool_blocks.clear()
        self._pending_permission_blocks.clear()
        self._plan_blocks.clear()
        self._subagent_run_ids.clear()
        self._subagent_start_times.clear()
        log_view = self.query_one("#log-view", VerticalScroll)
        await log_view.remove_children()
        await log_view.mount(
            Static(f"[bold cyan]Resumed[/bold cyan]  {escape(title)}", id="banner")
        )
        for message in messages:
            text = self._history_text(message.get("content"))
            if not text:
                continue
            if message.get("role") == "user":
                await log_view.mount(
                    Static(f"[bold]>[/bold] {escape(text)}", classes="user-turn")
                )
            else:
                block = LLMStreamBlock()
                block.append_token(text)
                block.finalize_markdown()
                await log_view.mount(block)
        self._update_context_status()
        log_view.scroll_end(animate=False)

    # 从字符串或内容块列表提取适合恢复页展示的可读文本
    @staticmethod
    def _history_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                parts.append(str(block["text"]))
            elif block.get("type") == "tool_use":
                parts.append(f"[tool: {block.get('name', 'unknown')}]")
        return "\n\n".join(parts)

    # 恢复或取消后重新启用消息输入框
    def _restore_prompt_after_resume(self) -> None:
        prompt = self._prompt()
        if prompt is not None:
            prompt.disabled = False
            prompt.read_only = False
            prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
            prompt.focus()

    # /memories 选择完成或取消后恢复输入框
    def _restore_prompt_after_memory_select(self) -> None:
        prompt = self._prompt()
        if prompt is not None:
            prompt.disabled = False
            prompt.read_only = False
            prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
            prompt.focus()

    # 将 TUI 的记忆开关同步到 core session
    async def _do_set_memory(self, generate_enabled: bool, use_enabled: bool) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            await self._client.send_command(
                "session.set_memory",
                {
                    "session_id": self._session_id,
                    "generate_enabled": generate_enabled,
                    "use_enabled": use_enabled,
                },
            )
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(f"[red]memory settings error: {exc}[/red]", classes="log-line"))

    # 通过 slash command 管理候选和长期记忆，不把管理指令发送给 Agent
    async def _do_memory_command(self, arguments: str) -> None:
        if self._client is None or self._session_id is None:
            return
        parts = arguments.split(None, 1)
        action = parts[0].lower() if parts else "list"
        value = parts[1].strip() if len(parts) > 1 else ""
        try:
            if action == "generate":
                result = await self._client.send_command(
                    "memory.generate", {"session_id": self._session_id}
                )
                candidates = result.get("candidates", [])
                if not candidates:
                    self._append(
                        Static("[dim]no memory candidates found[/dim]", classes="log-line")
                    )
                for candidate in candidates:
                    self._append(
                        Static(self._render_memory_candidate(candidate), classes="log-line")
                    )
                return
            if action in {"list", "search"}:
                method = "memory.search" if action == "search" else "memory.list"
                params: dict[str, Any] = {"session_id": self._session_id}
                if action == "search":
                    params["query"] = value
                result = await self._client.send_command(method, params)
                candidates = result.get("candidates", [])
                memories = result.get("memories", [])
                for candidate in candidates:
                    self._append(
                        Static(self._render_memory_candidate(candidate), classes="log-line")
                    )
                for memory in memories:
                    self._append(Static(self._render_memory_record(memory), classes="log-line"))
                if not candidates and not memories:
                    self._append(Static("[dim]no memories found[/dim]", classes="log-line"))
                return
            if action in {"accept", "commit"} and value:
                result = await self._client.send_command(
                    "memory.commit", {"candidate_id": value}
                )
                self._append(Static(
                    "[green]memory committed[/green]" if result.get("committed")
                    else "[yellow]candidate was not committed[/yellow]",
                    classes="log-line",
                ))
                return
            if action == "reject" and value:
                result = await self._client.send_command(
                    "memory.reject", {"candidate_id": value}
                )
                self._append(Static(
                    "[yellow]memory candidate rejected[/yellow]"
                    if result.get("rejected") else "[dim]candidate not pending[/dim]",
                    classes="log-line",
                ))
                return
            if action == "delete" and value:
                result = await self._client.send_command(
                    "memory.delete", {"memory_id": value}
                )
                self._append(Static(
                    "[yellow]memory deleted[/yellow]" if result.get("deleted")
                    else "[dim]memory not found[/dim]",
                    classes="log-line",
                ))
                return
            self._append(Static(
                "[dim]usage: /memories list | generate | search <text> | "
                "accept <candidate_id> | reject <candidate_id> | delete <memory_id>[/dim]",
                classes="log-line",
            ))
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(f"[red]memory command error: {exc}[/red]", classes="log-line"))

    @staticmethod
    def _render_memory_candidate(candidate: dict[str, Any]) -> str:
        return (
            f"[cyan]candidate[/cyan] [{candidate.get('scope', '')}] "
            f"[{candidate.get('type', '')}] {candidate.get('key', '')}: "
            f"{candidate.get('content', '')}  [dim]id={candidate.get('id', '')}[/dim]"
        )

    @staticmethod
    def _render_memory_record(memory: dict[str, Any]) -> str:
        return (
            f"[green]memory[/green] [{memory.get('scope', '')}] "
            f"[{memory.get('type', '')}] {memory.get('key', '')}: "
            f"{memory.get('content', '')}  [dim]id={memory.get('id', '')}[/dim]"
        )

    # 关闭旧会话、创建新会话并将 TUI 恢复到干净的初始状态
    async def _do_new_session(self) -> None:
        if self._client is None or self._session_id is None:
            self._busy = False
            return
        old_session_id = self._session_id
        self._remember_session_stats(old_session_id)
        await self._persist_session_stats(old_session_id)
        self._session_id = None
        try:
            created = await self._client.send_command(
                "session.create", self._session_create_params()
            )
            self._session_id = str(created["session_id"])
            await self._subscribe_to_session(self._session_id)
            self._workspace_root = (
                str(created["workspace_root"])
                if created.get("workspace_root") is not None
                else None
            )
            self._memory_generate_enabled = bool(
                created.get("memory_generate_enabled", self._memory_generate_enabled)
            )
            self._memory_use_enabled = bool(
                created.get("memory_use_enabled", self._memory_use_enabled)
            )
            try:
                await self._client.send_command(
                    "session.close", {"session_id": old_session_id}
                )
            except (IpcError, RuntimeError, OSError):
                log.warning("failed to close previous session session_id=%s", old_session_id)
            self._break_llm()
            self._pending_tool_blocks.clear()
            self._pending_permission_blocks.clear()
            self._plan_blocks.clear()
            self._subagent_run_ids.clear()
            self._subagent_start_times.clear()
            self._restore_session_stats(self._session_id)

            log_view = self.query_one("#log-view", VerticalScroll)
            await log_view.remove_children()
            await log_view.mount(Static(self._BANNER, id="banner"))
            self._update_context_status()
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.read_only = False
                prompt.border_title = (
                    "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                )
                prompt.focus()
            self._update_header("ready")
        except (IpcError, RuntimeError, OSError, KeyError) as exc:
            self._session_id = old_session_id
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.read_only = False
                prompt.border_title = "new session failed"
                prompt.focus()
            self._update_header("ready")
            self._append(Static(f"[red]new session error: {exc}[/red]", classes="log-line"))

    # 在 worker 中执行 IPC 发送，使 App 消息泵在 agent 运行期间仍能处理键盘/焦点等消息
    async def _do_send_message(self, content: str) -> None:
        if self._client is None:
            return
        try:
            await self._client.send_command(
                "session.send_message",
                {"session_id": self._session_id, "content": content},
            )
        except (IpcError, RuntimeError, OSError) as e:
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.read_only = False
                prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
            self._update_header("ready")
            self._append(Static(f"[red]send error: {e}[/red]", classes="log-line"))

    # 处理内联审批控件的用户决策：发送 IPC 响应并恢复输入框
    async def on_permission_select_decided(self, msg: PermissionSelect.Decided) -> None:
        tool_use_id = msg.tool_use_id
        decision = msg.decision
        log.info("permission decided tool_use_id=%s decision=%s", tool_use_id, decision)
        try:
            msg.widget.remove()
            perm_block = self._pending_permission_blocks.pop(tool_use_id, None)
            if perm_block is not None:
                perm_block._resolve(decision)
            if self._client is not None:
                try:
                    await self._client.send_command(
                        "permission.respond",
                        {"tool_use_id": tool_use_id, "decision": decision},
                    )
                except (IpcError, RuntimeError, OSError):
                    pass
            if not self._pending_permission_blocks:
                p = self._prompt()
                if p is not None:
                    p.disabled = False
                    p.read_only = False
                    p.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                    p.focus()
        except Exception:
            log.exception("on_permission_select_decided failed tool_use_id=%s", tool_use_id)

    # 向日志视图追加一个 widget 并滚动到底部
    def _append(self, widget: Widget) -> None:
        log_view = self.query_one("#log-view", VerticalScroll)
        log_view.mount(widget)
        log_view.scroll_end(animate=False)

    # 结束当前 LLM 流式块（下一个 token 将开启新块）
    def _break_llm(self) -> None:
        if self._current_llm is not None:
            self._current_llm.finalize_markdown()
        self._current_llm = None

    # 将选择控件挂载到 Screen 顶层（#prompt 之前），避免 VerticalScroll 争抢焦点
    def _mount_permission_select(self, select: PermissionSelect) -> None:
        self.mount(select, before="#prompt")

    # 将 /memories 选择控件挂载到输入框之前
    def _mount_memory_select(self, select: MemorySelect) -> None:
        self.mount(select, before="#prompt")

    # 安全获取输入框，便于组件测试中未挂载时跳过 UI 操作
    def _prompt(self) -> ChatTextArea | None:
        try:
            return self.query_one("#prompt", ChatTextArea)
        except Exception:
            return None

    # 重置新会话的上下文和性能统计，不保留旧 session 数据
    def _reset_session_stats(self) -> None:
        self._last_context_pct = 0.0
        self._last_usage = (0, 0, 0)
        self._rounds = 0
        self._steps = 0
        self._llm_elapsed_s = 0.0
        self._tool_elapsed_s = 0.0
        self._ttft_total_s = 0.0
        self._ttft_samples = 0
        self._generation_elapsed_s = 0.0
        self._throughput_output_tokens = 0
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_cache_read_tokens = 0
        self._llm_calls.clear()

    # 保存指定 session 当前显示的上下文、耗时与 token 统计快照
    def _remember_session_stats(self, session_id: str) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "last_context_pct": self._last_context_pct,
            "last_usage": self._last_usage,
            "rounds": self._rounds,
            "steps": self._steps,
            "llm_elapsed_s": self._llm_elapsed_s,
            "tool_elapsed_s": self._tool_elapsed_s,
            "ttft_total_s": self._ttft_total_s,
            "ttft_samples": self._ttft_samples,
            "generation_elapsed_s": self._generation_elapsed_s,
            "throughput_output_tokens": self._throughput_output_tokens,
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "total_cache_read_tokens": self._total_cache_read_tokens,
        }
        self._session_stats[session_id] = snapshot
        return snapshot

    # 恢复本地或 core 返回的 session 统计快照，首次进入的 session 保持全新零值
    def _restore_session_stats(
        self, session_id: str, persisted: Any = None
    ) -> None:
        if isinstance(persisted, dict) and persisted:
            self._session_stats[session_id] = dict(persisted)
        snapshot = self._session_stats.get(session_id)
        self._reset_session_stats()
        if snapshot is None:
            return
        self._last_context_pct = float(snapshot["last_context_pct"])
        last_usage = snapshot["last_usage"]
        if isinstance(last_usage, (list, tuple)) and len(last_usage) == 3:
            self._last_usage = tuple(int(value) for value in last_usage)  # type: ignore[assignment]
        self._rounds = int(snapshot["rounds"])
        self._steps = int(snapshot["steps"])
        self._llm_elapsed_s = float(snapshot["llm_elapsed_s"])
        self._tool_elapsed_s = float(snapshot["tool_elapsed_s"])
        self._ttft_total_s = float(snapshot["ttft_total_s"])
        self._ttft_samples = int(snapshot["ttft_samples"])
        self._generation_elapsed_s = float(snapshot["generation_elapsed_s"])
        self._throughput_output_tokens = int(snapshot["throughput_output_tokens"])
        self._total_input_tokens = int(snapshot["total_input_tokens"])
        self._total_output_tokens = int(snapshot["total_output_tokens"])
        self._total_cache_read_tokens = int(snapshot["total_cache_read_tokens"])

    # 将非零统计快照写入 core，使下次启动后恢复 session 时仍能显示
    async def _persist_session_stats(self, session_id: str) -> None:
        if self._client is None:
            return
        snapshot = self._remember_session_stats(session_id)
        has_data = any(
            float(snapshot[key]) != 0
            for key in (
                "last_context_pct",
                "rounds",
                "steps",
                "llm_elapsed_s",
                "tool_elapsed_s",
                "ttft_samples",
                "total_input_tokens",
                "total_output_tokens",
            )
        )
        if not has_data:
            return
        try:
            await self._client.send_command(
                "session.set_stats",
                {"session_id": session_id, "stats": snapshot},
            )
        except (IpcError, RuntimeError, OSError):
            log.warning("failed to persist session stats session_id=%s", session_id)

    # 生成输入框下方的分组统计，窄屏时从右向左整组隐藏
    def _render_context_status(self, width: int | None = None) -> str:
        pct = self._last_context_pct
        if pct >= 0.85:
            color = "bold red"
        elif pct >= 0.70:
            color = "yellow"
        else:
            color = "dim"

        average_ttft = (
            self._ttft_total_s / self._ttft_samples if self._ttft_samples else 0.0
        )
        tokens_per_second = (
            self._throughput_output_tokens / self._generation_elapsed_s
            if self._generation_elapsed_s > 0
            else 0.0
        )
        cache_hit_pct = (
            self._total_cache_read_tokens / self._total_input_tokens * 100
            if self._total_input_tokens > 0
            else 0.0
        )
        plain_groups = [
            f"ctx {pct * 100:.1f}%",
            f"{self._rounds}轮 · {self._steps}步",
            f"LLM {_format_duration(self._llm_elapsed_s)}"
            f" · tools {_format_duration(self._tool_elapsed_s)}",
            f"TTFT {_format_duration(average_ttft)} · {tokens_per_second:.0f} tok/s",
            f"cache {cache_hit_pct:.0f}%",
            f"↑{_format_token_count(self._total_input_tokens)}"
            f" · ↓{_format_token_count(self._total_output_tokens)}",
        ]
        visible_count = len(plain_groups)
        if width is not None and width > 0:
            while (
                visible_count > 1
                and len("  |  ".join(plain_groups[:visible_count])) > width
            ):
                visible_count -= 1

        rendered_groups = [
            f"[{color}]{plain_groups[0]}[/{color}]",
            *[f"[dim]{group}[/dim]" for group in plain_groups[1:visible_count]],
        ]
        return "  |  ".join(rendered_groups)

    # 使用最近一次根 Agent usage 刷新固定状态栏，未挂载时静默跳过
    def _update_context_status(self) -> None:
        try:
            status = self.query_one("#context-status", Static)
        except NoMatches:
            return
        status.update(self._render_context_status(status.content_size.width))

    # 终端尺寸变化时重算状态栏可见分组，避免窄屏截断指标
    def on_resize(self, event: events.Resize) -> None:
        self._update_context_status()

    # 根据连接和运行状态刷新顶部标题
    def _update_header(self, state: str) -> None:
        self._header_state = state
        try:
            header = self.query_one("#header", Label)
        except NoMatches:
            return
        session = f"  [dim]{self._session_id}[/dim]" if self._session_id else ""
        color = {
            "ready": "green",
            "running": "yellow",
            "disconnected": "red",
            "connecting": "dim",
        }.get(state, "dim")
        first_line = (
            f"[bold]AgentLite[/bold]  [dim]{self._host}:{self._port}[/dim]"
        )
        first_line += f"{session}  [{color}]{state}[/{color}]"

        second_line_parts: list[str] = []
        if self._workspace_root is not None:
            second_line_parts.append(
                f"[dim]workspace: {Path(self._workspace_root).resolve()}[/dim]"
            )
        if self._model_name:
            second_line_parts.append(f"[dim]model: {self._model_name}[/dim]")
        second_line = "  ".join(second_line_parts)
        header.update(f"{first_line}\n{second_line}" if second_line else first_line)

    # 管理 SocketClient 生命周期：连接、订阅事件、断线重连
    async def _socket_loop(self) -> None:
        header = self.query_one("#header", Label)

        while True:
            client = SocketClient(self._host, self._port)
            self._client = None
            try:
                await client.connect()
            except (ConnectionRefusedError, OSError):
                log.warning("connection refused %s:%s, retrying", self._host, self._port)
                self._update_header("disconnected")
                await asyncio.sleep(2)
                continue

            log.info("connected to %s:%s", self._host, self._port)
            self._client = client
            self._update_header("connecting")
            loop_task = asyncio.create_task(client.run_event_loop())

            async def on_event(event: dict[str, Any]) -> None:
                self._handle_event(event)

            client.on_event(on_event)

            try:
                loop_task.add_done_callback(
                    lambda t: log.error("loop_task failed: %s", t.exception())
                    if not t.cancelled() and t.exception() is not None
                    else None
                )
                created = await client.send_command(
                    "session.create", self._session_create_params()
                )
                self._session_id = str(created["session_id"])
                await self._subscribe_to_session(
                    self._session_id,
                    replay_from_run=self._replay_run_id,
                )
                self._memory_generate_enabled = bool(
                    created.get("memory_generate_enabled", self._memory_generate_enabled)
                )
                self._memory_use_enabled = bool(
                    created.get("memory_use_enabled", self._memory_use_enabled)
                )
                self._workspace_root = (
                    str(created["workspace_root"])
                    if created.get("workspace_root") is not None
                    else None
                )
                log.info("session created session_id=%s", self._session_id)
                prompt = self._prompt()
                if prompt is not None:
                    prompt.disabled = False
                    prompt.read_only = False
                    prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                    prompt.focus()
                self._update_header("ready")
                await loop_task
            except IpcError as e:
                header.update(f"[bold]AgentLite[/bold]  [red]subscribe error: {e}[/red]")
            finally:
                if not loop_task.done():
                    loop_task.cancel()
                self._client = None
                self._session_id = None
                prompt = self._prompt()
                if prompt is not None:
                    prompt.disabled = True
                    prompt.read_only = False
                    prompt.border_title = "disconnected, retrying..."
                self._break_llm()
                await client.close()

            self._update_header("disconnected")
            await asyncio.sleep(2)

    # 将当前连接的事件订阅切换到指定 session，避免不同 TUI 之间串流
    async def _subscribe_to_session(
        self,
        session_id: str,
        *,
        replay_from_run: str | None = None,
    ) -> None:
        if self._client is None:
            raise RuntimeError("TUI client is not connected")
        params: dict[str, Any] = {
            "topics": [
                "session.*",
                "run.*",
                "step.*",
                "tool.*",
                "llm.*",
                "log.*",
                "permission.*",
                "context.*",
                "subagent.*",
                "skill.*",
                "memory.*",
            ],
            "scope": f"session:{session_id}",
        }
        if replay_from_run is not None:
            params["replay_from_run"] = replay_from_run
        await self._client.send_command("event.subscribe", params)

    # 根据事件 type 路由到对应渲染逻辑；捕获异常防止 socket loop 因单个事件崩溃
    def _handle_event(self, event: dict[str, Any]) -> None:
        try:
            self._handle_event_inner(event)
        except Exception:
            log.exception("_handle_event crashed  event_type=%s", event.get("type", "?"))

    # 实际的事件路由逻辑
    def _handle_event_inner(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")

        if t == "llm.token":
            run_id = str(event.get("run_id") or "")
            call = self._llm_calls.get(run_id)
            if call is not None and call[1] is None:
                self._llm_calls[run_id] = (call[0], time.monotonic())
            token = event.get("token", "")
            if self._current_llm is None:
                llm_block = LLMStreamBlock()
                self._append(llm_block)
                self._current_llm = llm_block
            self._current_llm.append_token(token)
            return

        self._break_llm()

        if t == "session.waiting_for_input":
            session_id = str(event.get("session_id") or "")
            if session_id and session_id != self._session_id:
                return
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.read_only = False
                prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                prompt.focus()
            self._update_context_status()
            self._update_header("ready")

        elif t == "session.closed":
            session_id = str(event.get("session_id") or "")
            if session_id and session_id != self._session_id:
                return
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = True
                prompt.read_only = False
                prompt.border_title = "session closed"
            self._update_header("disconnected")

        elif t == "run.started":
            run_id = event.get("run_id", "")
            goal = event.get("goal", "")
            self._append(Static(
                f"[dim]run[/dim]  [cyan]{run_id}[/cyan]  [dim]{_preview(goal, 96)}[/dim]",
                classes="run-header",
            ))

        elif t == "llm.model_selected":
            run_id = str(event.get("run_id") or "")
            if run_id not in self._subagent_run_ids:
                self._llm_calls[run_id] = (time.monotonic(), None)
                model = str(event.get("model") or "")
                if model:
                    self._model_name = model
                    self._update_header(self._header_state)

        elif t == "skill.invoked":
            skill_name = event.get("skill_name", "")
            arguments = event.get("arguments", "")
            args_preview = _preview(arguments, 80) if arguments else ""
            args_part = f"  [dim]{args_preview}[/dim]" if args_preview else ""
            self._append(Static(
                f"[bold cyan]/{skill_name}[/bold cyan]{args_part}",
                classes="log-line",
            ))

        elif t == "plan.updated":
            run_id = str(event.get("run_id") or "")
            plan = [item for item in (event.get("plan") or []) if isinstance(item, dict)]
            explanation = event.get("explanation")
            block = self._plan_blocks.get(run_id)
            if block is None:
                block = PlanBlock(run_id, plan, str(explanation) if explanation else None)
                if run_id in self._subagent_run_ids:
                    block.styles.padding = (0, 2, 0, 6)
                self._plan_blocks[run_id] = block
                self._append(block)
            else:
                block.set_plan(plan, str(explanation) if explanation else None)

        elif t == "subagent.started":
            run_id = event.get("run_id", "")
            description = event.get("description", "")
            self._subagent_run_ids[run_id] = description
            self._subagent_start_times[run_id] = time.monotonic()
            short_id = run_id[:8] if len(run_id) >= 8 else run_id
            self._append(Static(
                f"[dim]┌─[/dim] [cyan]{_preview(description, 72)}[/cyan]  [dim]{short_id}[/dim]",
                classes="log-line",
            ))

        elif t == "subagent.finished":
            run_id = event.get("run_id", "")
            status = event.get("status", "")
            description = self._subagent_run_ids.pop(run_id, event.get("description", ""))
            start = self._subagent_start_times.pop(run_id, None)
            elapsed = f"  [dim]{time.monotonic() - start:.1f}s[/dim]" if start is not None else ""
            desc_part = f"[cyan]{_preview(description, 72)}[/cyan]{elapsed}"
            if status == "success":
                self._append(Static(
                    f"[dim]└─[/dim] [bold green]✓[/bold green] {desc_part}",
                    classes="log-line",
                ))
            else:
                self._append(Static(
                    f"[dim]└─[/dim] [bold red]✗[/bold red] {desc_part}",
                    classes="log-line",
                ))

        elif t == "step.started":
            run_id = event.get("run_id", "")
            if run_id in self._subagent_run_ids:
                return
            step = event.get("step", "")
            self._append(Static(
                f"[dim]step {step}[/dim]",
                classes="step-divider",
            ))

        elif t == "tool.call_started":
            tool_use_id = str(event.get("tool_use_id", ""))
            tool_name = str(event.get("tool_name", ""))
            params = event.get("params") or {}
            run_id = event.get("run_id", "")
            tc_block = ToolCallBlock(tool_name, params)
            if run_id in self._subagent_run_ids:
                tc_block.styles.padding = (0, 2, 0, 6)
            self._pending_tool_blocks[tool_use_id] = tc_block
            self._append(tc_block)

        elif t == "tool.call_finished":
            tool_use_id = str(event.get("tool_use_id", ""))
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            run_id = str(event.get("run_id") or "")
            if run_id not in self._subagent_run_ids:
                self._tool_elapsed_s += elapsed_ms / 1_000
            output = str(event.get("output") or "")
            if tool_use_id in self._pending_tool_blocks:
                tc_done = self._pending_tool_blocks.pop(tool_use_id)
                tc_done.set_result(output, elapsed_ms)

        elif t == "tool.call_failed":
            tool_use_id = str(event.get("tool_use_id", ""))
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            run_id = str(event.get("run_id") or "")
            if run_id not in self._subagent_run_ids:
                self._tool_elapsed_s += elapsed_ms / 1_000
            error_msg = str(event.get("error_message") or "")
            if tool_use_id in self._pending_tool_blocks:
                tc_done = self._pending_tool_blocks.pop(tool_use_id)
                tc_done.set_result(error_msg, elapsed_ms, is_error=True)

        elif t == "run.finished":
            run_id = str(event.get("run_id") or "")
            status = event.get("status", "")
            steps = int(event.get("steps") or 0)
            reason = event.get("reason") or ""
            if run_id not in self._subagent_run_ids:
                self._rounds += 1
                self._steps += steps
            if status == "success":
                self._append(Static(
                    f"[bold green]✓ completed[/bold green]  [dim]{steps} steps[/dim]",
                    classes="run-ok",
                ))
            else:
                detail = f"  [dim]{reason}[/dim]" if reason else ""
                self._append(Static(
                    f"[bold red]✗ failed[/bold red]{detail}  [dim]{steps} steps[/dim]",
                    classes="run-err",
                ))

        elif t == "memory.candidates_created":
            session_id = str(event.get("session_id") or "")
            if session_id and session_id != self._session_id:
                return
            count = int(event.get("count") or 0)
            self._append(Static(
                f"[bold cyan]memory[/bold cyan]  {count} pending candidate(s)  "
                "[dim]review with memory.list / memory.commit[/dim]",
                classes="log-line",
            ))

        elif t == "memory.saved":
            self._append(Static(
                f"[bold green]memory saved[/bold green]  "
                f"[dim]{event.get('key', '')}[/dim]",
                classes="log-line",
            ))

        elif t == "memory.deleted":
            self._append(Static(
                f"[bold yellow]memory deleted[/bold yellow]  "
                f"[dim]{event.get('memory_id', '')}[/dim]",
                classes="log-line",
            ))
        elif t == "llm.usage":
            run_id = str(event.get("run_id") or "")
            if run_id in self._subagent_run_ids:
                return
            now = time.monotonic()
            call = self._llm_calls.pop(run_id, None)
            output_tokens = int(event.get("output_tokens") or 0)
            if call is not None:
                started_at, first_token_at = call
                self._llm_elapsed_s += max(0.0, now - started_at)
                if first_token_at is not None:
                    self._ttft_total_s += max(0.0, first_token_at - started_at)
                    self._ttft_samples += 1
                    self._generation_elapsed_s += max(0.0, now - first_token_at)
                    self._throughput_output_tokens += output_tokens
            pct = float(event.get("context_pct") or 0.0)
            self._last_context_pct = pct
            input_tokens = int(event.get("input_tokens") or 0)
            cache_read_tokens = int(event.get("cache_read_input_tokens") or 0)
            cache_creation_tokens = int(event.get("cache_creation_input_tokens") or 0)
            total_input_tokens = input_tokens
            if self._llm_protocol == "anthropic":
                total_input_tokens += cache_read_tokens + cache_creation_tokens
            self._last_usage = (
                total_input_tokens,
                output_tokens,
                cache_read_tokens,
            )
            self._total_input_tokens += total_input_tokens
            self._total_output_tokens += output_tokens
            self._total_cache_read_tokens += cache_read_tokens

        elif t == "context.compacted":
            orig = event.get("original_tokens", 0)
            summary = event.get("summary_tokens", 0)
            self._last_context_pct = 0.0
            self._last_usage = (int(summary or 0), 0, 0)
            self._update_context_status()
            self._append(Static(
                f"[bold cyan]⚡ Context compacted[/bold cyan]"
                f"  [dim]original≈{orig} tokens → summary={summary} tokens[/dim]",
                classes="log-line",
            ))

        elif t == "permission.requested":
            tool_use_id = str(event.get("tool_use_id", ""))
            tool_name = str(event.get("tool_name", ""))
            param_preview = str(event.get("param_preview", ""))
            try:
                _focused_repr = repr(self.focused)
            except Exception:
                _focused_repr = "?"
            log.info(
                "permission.requested tool=%s id=%s  app.focused=%s",
                tool_name, tool_use_id, _focused_repr,
            )
            perm_block = PermissionBlock(tool_use_id, tool_name, param_preview)
            self._pending_permission_blocks[tool_use_id] = perm_block
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = True
                prompt.border_title = "permission required"
            self._append(perm_block)
            select = PermissionSelect(tool_use_id)
            self._mount_permission_select(select)
            log.debug("PermissionSelect mounted before #prompt  pending=%d", len(self._pending_permission_blocks))

        elif t == "permission.denied":
            # 处理超时或断连等非用户交互触发的 deny（用户主动 deny 已由 on_permission_select_decided 处理）
            tool_use_id = str(event.get("tool_use_id", ""))
            decision = str(event.get("decision", "denied"))
            if tool_use_id in self._pending_permission_blocks:
                perm_block = self._pending_permission_blocks.pop(tool_use_id)
                perm_block._resolve(decision)
                try:
                    select = self.query_one(PermissionSelect)
                    select.remove()
                except Exception:
                    pass
                if not self._pending_permission_blocks:
                    p = self._prompt()
                    if p is not None:
                        p.disabled = False
                        p.read_only = False
                        p.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                        p.focus()

        elif t == "log.line":
            level = event.get("level", "INFO")
            color = "bold red" if level == "ERROR" else ("yellow" if level == "WARNING" else "dim")
            self._append(Static(
                f"[{color}]{level}[/{color}]  "
                f"[dim]{event.get('source', '')}[/dim]  {event.get('message', '')}",
                classes="log-line",
            ))


# TUI 入口：读取配置并启动 AgentLiteTuiApp
def run(config: AgentLiteConfig, replay_run_id: str | None = None) -> None:
    app = AgentLiteTuiApp(
        config.host,
        config.port,
        replay_run_id=replay_run_id,
        llm_protocol=config.llm.protocol,
        model=config.llm.default_model,
    )
    app.run()
