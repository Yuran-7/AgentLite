from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from rich.markdown import Markdown
from textual.app import App, ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from kama_claude.tui.app import (
    KamaTuiApp,
    LLMStreamBlock,
    SlashCompleteWidget,
    ToolCallBlock,
    _param_summary,
    _preview,
    _resolve_workspace_argument,
)


class _ToolBlockHarness(App[None]):
    # 挂载独立工具块和鼠标移出目标，供交互样式测试使用
    def compose(self) -> ComposeResult:
        yield ToolCallBlock("bash", {"command": "echo hi"})
        yield Static("outside", id="outside")


class _ContextStatusHarness(KamaTuiApp):
    # 初始化不连接 daemon 的完整 TUI，供固定状态栏交互测试使用
    def __init__(self) -> None:
        super().__init__("127.0.0.1", 9999)

    # 跳过 socket worker，隔离状态栏测试与外部服务
    def on_mount(self) -> None:
        return None


# 功能：验证 _preview 超出长度时截断并追加省略号
# 设计：不依赖任何 TUI 组件，纯函数测试
def test_preview_truncates() -> None:
    assert _preview("abcde", 3) == "abc…"
    assert _preview("ab", 5) == "ab"


# 功能：验证 TUI 产品标题与启动 Banner 已统一更新为 AgentLite
# 设计：检查窗口标题、Header 文本及六行等宽 Banner，避免局部仍残留旧品牌名
async def test_tui_branding_uses_agentlite() -> None:
    app = _ContextStatusHarness()

    async with app.run_test(size=(100, 24)):
        assert app.TITLE == "AgentLite"
        assert "AgentLite" in str(app.query_one("#header", Static).content)
        banner_rows = app._BANNER.splitlines()[:6]  # type: ignore[attr-defined]
        assert len(banner_rows) == 6
        assert len({len(row) for row in banner_rows}) == 1


# 功能：验证工具参数摘要优先展示工具最关键字段
# 设计：覆盖 read_file/bash/note_save 三类常见工具，避免工具块摘要退化成整段 JSON
def test_param_summary_prefers_key_fields() -> None:
    assert _param_summary("read_file", {"path": "README.md"}) == "path='README.md'"
    assert _param_summary("bash", {"command": "echo hi", "timeout": 1}) == "command='echo hi'"
    assert _param_summary("note_save", {"content": "Python 3.12"}) == "content='Python 3.12'"


# 功能：验证输入斜杠时 /new 作为首个内建命令出现在自动补全中
# 设计：直接检查候选顺序和说明，确保新会话入口不会被动态 Skill 列表淹没
def test_slash_items_put_new_session_first() -> None:
    app = KamaTuiApp("127.0.0.1", 9999)
    assert app._build_slash_items()[0] == (  # type: ignore[attr-defined]
        "new",
        "start a new session",
        False,
    )


# 功能：验证 TUI 工作区参数支持空格、成对引号和相对目录并规范化为绝对路径
# 设计：在临时 cwd 下解析带空格的真实目录，覆盖用户从终端输入常见 Windows 路径的方式
def test_resolve_workspace_argument_normalizes_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "my repo"
    workspace.mkdir()
    monkeypatch.chdir(tmp_path)

    assert _resolve_workspace_argument('"my repo"') == str(workspace.resolve())


# 功能：验证 TUI 仅在用户设置工作区时把绝对路径发送给 session.create
# 设计：直接检查纯参数构造方法，同时覆盖通用模式不携带字段和工作区模式携带字段
def test_session_create_params_include_optional_workspace() -> None:
    generic = KamaTuiApp("127.0.0.1", 9999)
    scoped = KamaTuiApp(
        "127.0.0.1",
        9999,
        workspace_root="C:\\repo",
    )

    assert generic._session_create_params() == {"mode": "chat"}  # type: ignore[attr-defined]
    assert scoped._session_create_params() == {  # type: ignore[attr-defined]
        "mode": "chat",
        "workspace_root": "C:\\repo",
    }


# 功能：验证斜杠菜单将通用命令置顶，并在首个 Skill 前显示不可选中的分组标题
# 设计：使用真实内建候选渲染菜单，比较文本位置并检查类别标记，不把标题混入导航列表
def test_slash_menu_separates_general_commands_and_skills() -> None:
    app = KamaTuiApp("127.0.0.1", 9999)
    items = app._build_slash_items()  # type: ignore[attr-defined]
    assert [item[2] for item in items[:3]] == [False, False, False]
    assert all(item[2] for item in items[3:])

    popup = SlashCompleteWidget(items)
    popup._redraw()  # type: ignore[attr-defined]
    rendered = str(popup.content)
    assert rendered.index("/compact") < rendered.index("Skills")
    assert rendered.index("Skills") < rendered.index("/init")
    assert len(popup._filtered) == len(items)  # type: ignore[attr-defined]


# 功能：验证 TUI 可为当前 session 补绑工作区并用 daemon 返回值刷新本地状态
# 设计：挂载无网络测试应用并注入假 IPC 客户端，检查命令参数、提示、busy 恢复和 header 数据源
async def test_set_workspace_updates_current_tui_session(tmp_path: Path) -> None:
    class _FakeClient:
        # 初始化 IPC 调用记录
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        # 记录工作区设置请求并返回规范化路径
        async def send_command(
            self, method: str, params: dict[str, object]
        ) -> dict[str, object]:
            self.calls.append((method, params))
            return {"workspace_root": str(tmp_path.resolve())}

    app = _ContextStatusHarness()
    client = _FakeClient()

    async with app.run_test(size=(100, 24)):
        app._client = client  # type: ignore[assignment]
        app._session_id = "session-current"  # type: ignore[attr-defined]
        app._busy = True  # type: ignore[attr-defined]

        await app._do_set_workspace(str(tmp_path))  # type: ignore[attr-defined]

        assert client.calls == [(
            "session.set_workspace",
            {
                "session_id": "session-current",
                "workspace_root": str(tmp_path.resolve()),
            },
        )]
        assert app._workspace_root == str(tmp_path.resolve())  # type: ignore[attr-defined]
        assert not app._busy  # type: ignore[attr-defined]
        assert not app.query_one("#prompt").disabled
        assert "ws:" in str(app.query_one("#header", Static).content)


# 功能：验证 /new 创建独立 session 并重置聊天日志、上下文水位和输入状态
# 设计：使用假 IPC 客户端执行完整切换，断言先创建后关闭旧会话且页面只保留启动 Banner
async def test_new_session_resets_tui_state() -> None:
    class _FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        # 记录 IPC 请求，并为新会话创建返回固定 ID
        async def send_command(
            self, method: str, params: dict[str, object]
        ) -> dict[str, object]:
            self.calls.append((method, params))
            if method == "session.create":
                return {"session_id": "session-new", "status": "active"}
            return {"status": "closed"}

    app = _ContextStatusHarness()
    client = _FakeClient()

    async with app.run_test(size=(100, 24)):
        app._client = client  # type: ignore[assignment]
        app._session_id = "session-old"  # type: ignore[attr-defined]
        app._busy = True  # type: ignore[attr-defined]
        app._last_context_pct = 0.75  # type: ignore[attr-defined]
        app._last_usage = (150_000, 2_000, 120_000)  # type: ignore[attr-defined]
        app._rounds = 4  # type: ignore[attr-defined]
        app._steps = 12  # type: ignore[attr-defined]
        app._total_input_tokens = 150_000  # type: ignore[attr-defined]
        log_view = app.query_one("#log-view")
        await log_view.mount(Static("old conversation"))

        await app._do_new_session()  # type: ignore[attr-defined]

        assert [method for method, _ in client.calls] == [
            "session.create",
            "session.close",
        ]
        assert client.calls[1][1] == {"session_id": "session-old"}
        assert app._session_id == "session-new"  # type: ignore[attr-defined]
        assert not app._busy  # type: ignore[attr-defined]
        assert app._last_context_pct == 0.0  # type: ignore[attr-defined]
        assert app._last_usage == (0, 0, 0)  # type: ignore[attr-defined]
        assert app._rounds == 0  # type: ignore[attr-defined]
        assert app._steps == 0  # type: ignore[attr-defined]
        assert app._total_input_tokens == 0  # type: ignore[attr-defined]
        assert len(log_view.children) == 1
        assert log_view.query_one("#banner")
        prompt = app.query_one("#prompt")
        assert not prompt.disabled


# 功能：验证 llm.token 事件累积到 LLMStreamBlock，不连续 token 各自新开一块
# 设计：monkey-patch _append 收集追加的 widgets，断言 token 追加到同一块；
#       发送非 token 事件后新 block 被重置，下一个 token 开启新块
def test_llm_tokens_accumulate_in_block() -> None:
    app = KamaTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({"type": "llm.token", "token": "Hello", "run_id": "r", "ts": "t"})
    app._handle_event({"type": "llm.token", "token": " world", "run_id": "r", "ts": "t"})

    assert len(appended) == 1  # same block reused
    assert isinstance(appended[0], LLMStreamBlock)
    assert appended[0]._text == "Hello world"  # type: ignore[attr-defined]


# 功能：验证 LLMStreamBlock 结束时会把累积文本渲染为 Rich Markdown
# 设计：直接调用 finalize_markdown，断言 renderable 类型，覆盖 Markdown polish 的核心行为
def test_llm_block_finalize_renders_markdown() -> None:
    block = LLMStreamBlock()
    block.append_token("## Title\n\n- one\n\n```python\nprint('hi')\n```")
    block.finalize_markdown()
    assert isinstance(block.content, Markdown)


# 功能：验证非 token 事件后 _current_llm 被重置，下一个 token 开启新块
# 设计：插入 step.started 中断流，验证之前的 block 被 finalize，之后的 llm.token 创建新 LLMStreamBlock
def test_llm_block_resets_after_non_token_event() -> None:
    app = KamaTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({"type": "llm.token", "token": "A", "run_id": "r", "ts": "t"})
    app._handle_event({"type": "step.started", "run_id": "r", "step": 2, "ts": "t"})
    app._handle_event({"type": "llm.token", "token": "B", "run_id": "r", "ts": "t"})

    llm_blocks = [w for w in appended if isinstance(w, LLMStreamBlock)]
    assert len(llm_blocks) == 2
    assert llm_blocks[0]._finalized  # type: ignore[attr-defined]


# 功能：验证 run.started 事件追加 Static widget 且包含 run_id 和 goal
# 设计：monkey-patch _append，断言追加的 widget 的 renderable 包含关键字段
def test_run_started_appends_widget_with_content() -> None:
    app = KamaTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({
        "type": "run.started", "run_id": "run-abc", "goal": "do the thing", "ts": "t"
    })

    assert len(appended) == 1
    rendered = appended[0].content
    assert "run-abc" in rendered
    assert "do the thing" in rendered


# 功能：验证 run.finished success 追加包含 "completed" 的 widget
# 设计：monkey-patch _append，检查 rendered 内容包含 completed 和 green
def test_run_finished_success_shows_completed() -> None:
    app = KamaTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({
        "type": "run.finished", "run_id": "r", "status": "success", "steps": 3, "ts": "t"
    })

    rendered = appended[0].content
    assert "completed" in rendered
    assert "green" in rendered


# 功能：验证 run.finished failed 追加包含 "failed" 和 red 的 widget
# 设计：与 success 对称，检查颜色标记差异
def test_run_finished_failed_shows_red() -> None:
    app = KamaTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({
        "type": "run.finished", "run_id": "r", "status": "failed",
        "steps": 1, "reason": "llm_error", "ts": "t"
    })

    rendered = appended[0].content
    assert "failed" in rendered
    assert "red" in rendered


# 功能：验证 usage 不再追加到聊天日志，并只在一次 run 结束后刷新输入框下方状态栏
# 设计：挂载完整底部布局，先发送 usage 检查状态不变，再发送 run.finished 检查紧凑数据更新
async def test_context_status_updates_below_prompt_after_run_finishes() -> None:
    app = _ContextStatusHarness()

    async with app.run_test(size=(180, 24)) as pilot:
        status = app.query_one("#context-status", Static)
        initial = str(status.content)
        assert "ctx 0.0%" in initial

        app._handle_event({
            "type": "llm.usage",
            "run_id": "root-run",
            "input_tokens": 24_810,
            "output_tokens": 632,
            "cache_read_input_tokens": 18_200,
            "context_pct": 0.124,
            "ts": "t",
        })
        await pilot.pause()
        assert str(status.content) == initial
        assert not app.query(".usage")

        app._handle_event({"type": "session.waiting_for_input", "ts": "t"})
        await pilot.pause()

        rendered = str(status.content)
        assert "ctx 12.4%" in rendered
        assert "0轮 · 0步" in rendered
        assert "cache 42%" in rendered
        assert "↑43K · ↓632" in rendered
        prompt = app.query_one("#prompt")
        assert status.region.y >= prompt.region.bottom


# 功能：验证根 Agent 的轮次、耗时、TTFT、吞吐和 token 累计进入分组状态栏
# 设计：固定单调时钟驱动两次 LLM 调用，再校验同组用中点、跨组用竖线
def test_context_status_collects_grouped_run_metrics() -> None:
    app = KamaTuiApp("127.0.0.1", 9999, llm_protocol="openai")
    app._append = lambda _widget: None  # type: ignore[method-assign]

    with patch("kama_claude.tui.app.time.monotonic", side_effect=[0.0, 2.0, 4.0]):
        app._handle_event({"type": "llm.model_selected", "run_id": "root", "ts": "t"})
        app._handle_event({"type": "llm.token", "run_id": "root", "token": "hi", "ts": "t"})
        app._handle_event({
            "type": "llm.usage",
            "run_id": "root",
            "input_tokens": 202_000,
            "output_tokens": 240,
            "cache_read_input_tokens": 96_960,
            "cache_creation_input_tokens": 0,
            "context_pct": 0.42,
            "ts": "t",
        })
    app._handle_event({
        "type": "tool.call_finished",
        "run_id": "root",
        "tool_use_id": "tool-1",
        "elapsed_ms": 1_000,
        "output": "ok",
        "ts": "t",
    })
    app._handle_event({
        "type": "run.finished",
        "run_id": "root",
        "status": "success",
        "steps": 14,
        "ts": "t",
    })

    rendered = app._render_context_status(200)  # type: ignore[attr-defined]
    assert "ctx 42.0%" in rendered
    assert "1轮 · 14步" in rendered
    assert "LLM 4.0s · tools 1.0s" in rendered
    assert "TTFT 2.0s · 120 tok/s" in rendered
    assert "cache 48%" in rendered
    assert "↑202K · ↓240" in rendered
    assert "  |  " in rendered


# 功能：验证窄屏下状态栏从右向左按完整分组隐藏
# 设计：比较宽窄两种渲染，确保 token 组被整体移除而不是字符截断
def test_context_status_hides_whole_groups_on_narrow_screens() -> None:
    app = KamaTuiApp("127.0.0.1", 9999)
    app._total_input_tokens = 202_000  # type: ignore[attr-defined]
    app._total_output_tokens = 2_400  # type: ignore[attr-defined]

    wide = app._render_context_status(200)  # type: ignore[attr-defined]
    narrow = app._render_context_status(45)  # type: ignore[attr-defined]

    assert "↑202K · ↓2.4K" in wide
    assert "↑" not in narrow
    assert "ctx 0.0%" in narrow


# 功能：验证子 Agent usage 不会覆盖根对话的固定 context 水位
# 设计：预登记子 run 后发送其 usage，断言缓存百分比和 token 统计均保持原值
def test_subagent_usage_does_not_change_context_status() -> None:
    app = KamaTuiApp("127.0.0.1", 9999)
    app._last_context_pct = 0.25  # type: ignore[attr-defined]
    app._last_usage = (100, 20, 80)  # type: ignore[attr-defined]
    app._subagent_run_ids["child"] = "worker"  # type: ignore[attr-defined]

    app._handle_event({
        "type": "llm.usage",
        "run_id": "child",
        "input_tokens": 99_999,
        "output_tokens": 9_999,
        "cache_read_input_tokens": 88_888,
        "context_pct": 0.9,
        "ts": "t",
    })

    assert app._last_context_pct == 0.25  # type: ignore[attr-defined]
    assert app._last_usage == (100, 20, 80)  # type: ignore[attr-defined]


# 功能：验证 tool.call_started 追加 ToolCallBlock，call_finished 更新其结果
# 设计：直接调用 _handle_event 两次，通过 _pending_tool_blocks 验证状态流转
def test_tool_call_started_and_finished() -> None:
    app = KamaTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({
        "type": "tool.call_started",
        "tool_use_id": "uid-1",
        "tool_name": "bash",
        "params": {"command": "echo hi"},
        "run_id": "r", "ts": "t",
    })
    assert "uid-1" in app._pending_tool_blocks  # type: ignore[attr-defined]

    app._handle_event({
        "type": "tool.call_finished",
        "tool_use_id": "uid-1",
        "tool_name": "bash",
        "elapsed_ms": 42,
        "output": "hi",
        "run_id": "r", "ts": "t",
    })
    assert "uid-1" not in app._pending_tool_blocks  # type: ignore[attr-defined]
    block = appended[0]
    assert isinstance(block, ToolCallBlock)
    assert block._finished  # type: ignore[attr-defined]
    assert block._output == "hi"  # type: ignore[attr-defined]


# 功能：验证已完成工具块悬停时加粗并显示箭头，点击后箭头随折叠状态切换
# 设计：用 Textual pilot 驱动真实鼠标进入和点击，覆盖 CSS 命中、DOM 更新及移出复原
async def test_tool_block_hover_and_chevron_toggle() -> None:
    app = _ToolBlockHarness()

    async with app.run_test(size=(120, 20)) as pilot:
        block = app.query_one(ToolCallBlock)
        block.set_result("hi", 42)
        await pilot.pause()

        summary = block.query_one(".summary", Static)
        chevron = block.query_one(".chevron", Static)
        assert chevron.styles.opacity == 0

        assert await pilot.hover(".summary")
        await pilot.pause(0.2)
        assert "hovered" in block.classes
        assert summary.styles.text_style.bold
        assert chevron.styles.opacity == 1
        assert chevron.region.x == summary.region.right + 1
        assert str(chevron.content) == ">"

        assert await pilot.click(".summary")
        await pilot.pause()
        assert "expanded" in block.classes
        assert str(chevron.content) == "▾"

        assert await pilot.hover("#outside")
        await pilot.pause(0.2)
        assert "expanded" in block.classes
        assert "hovered" not in block.classes
        assert chevron.styles.opacity == 0
        assert str(chevron.content) == "▾"

        assert await pilot.hover(".summary")
        assert await pilot.click(".summary")
        await pilot.pause(0.2)
        assert "expanded" not in block.classes
        assert chevron.styles.opacity == 1
        assert str(chevron.content) == ">"


# 功能：验证 note_save 成功完成时工具块摘要显示 remembered
# 设计：直接操作 ToolCallBlock，覆盖 note_save 的特殊低噪声展示策略
def test_note_save_tool_block_shows_remembered() -> None:
    block = ToolCallBlock("note_save", {"content": "Python 3.12"})
    block.set_result("saved", 3)
    assert "remembered" in block._summary()  # type: ignore[attr-defined]


# 功能：验证 TUI 使用 VS Code 终端可传递的 Ctrl+C 退出且具有应用级优先级
# 设计：直接检查声明式绑定，防止退回会被 VS Code 截获的 Ctrl+Q 或被输入框覆盖
def test_quit_binding_uses_priority_ctrl_c() -> None:
    quit_bindings = [binding for binding in KamaTuiApp.BINDINGS if binding.action == "quit"]
    assert len(quit_bindings) == 1
    assert quit_bindings[0].key == "ctrl+c"
    assert quit_bindings[0].priority


# 功能：验证提交用户输入时会追加 user turn，并进入 busy 状态
# 设计：用 fake client 替代 SocketClient，直接调用 on_chat_text_area_submitted，
#       覆盖 TextArea 清空内容 + 设置 busy 占位符的核心状态迁移
async def test_input_submit_appends_user_turn_and_disables_prompt() -> None:
    class _FakeArea:
        def __init__(self) -> None:
            self.disabled = False
            self.border_title = ""
            self.text = "hello"

    class _FakeEvent:
        def __init__(self, area: _FakeArea) -> None:
            self.value = area.text
            self.text_area = area

    class _FakeClient:
        async def send_command(self, method: str, params: dict) -> dict:
            return {"run_id": "run-1"}

    app = KamaTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]
    app._update_header = lambda state: None  # type: ignore[method-assign]
    app._client = _FakeClient()  # type: ignore[assignment]
    app._session_id = "sess-1"

    area = _FakeArea()
    event = _FakeEvent(area)
    await app.on_chat_text_area_submitted(event)  # type: ignore[arg-type]

    assert app._busy  # type: ignore[attr-defined]
    assert area.disabled
    assert area.text == ""
    assert "agent is working" in area.border_title.lower()
    assert appended[0].content == "[bold]>[/bold] hello"


# 功能：验证未知事件类型不抛异常也不追加任何 widget
# 设计：发送 type 为 unknown 的事件，断言 appended 为空
def test_unknown_event_silently_ignored() -> None:
    app = KamaTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({"type": "some.unknown.type", "run_id": "r", "ts": "t"})
    assert appended == []
