from __future__ import annotations

from unittest.mock import patch

from rich.markdown import Markdown
from textual.app import App, ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from agent_lite.tui.app import (
    AgentLiteTuiApp,
    LLMStreamBlock,
    MemorySelect,
    PlanBlock,
    ResumeSelect,
    SlashCompleteWidget,
    ToolCallBlock,
    _format_session_time,
    _param_summary,
    _preview,
)


class _ToolBlockHarness(App[None]):
    # 挂载独立工具块和鼠标移出目标，供交互样式测试使用
    def compose(self) -> ComposeResult:
        yield ToolCallBlock("bash", {"command": "echo hi"})
        yield Static("outside", id="outside")


class _ContextStatusHarness(AgentLiteTuiApp):
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


# 功能：验证恢复选择器展示自然语言名称、工作区和更新时间
# 设计：直接渲染一条历史摘要，覆盖 roadmap 要求的三个识别字段且无需启动真实 daemon
def test_resume_select_renders_session_identity() -> None:
    select = ResumeSelect(
        [
            {
                "session_id": "session-1",
                "title": "Implement resume",
                "workspace_root": "D:/repo",
                "updated_at": "2026-08-27T10:20:00+00:00",
            }
        ],
        "D:/repo",
    )

    rendered = select._render_ui()  # type: ignore[attr-defined]
    assert "Implement resume" in rendered
    assert "D:/repo" in rendered
    assert "2026-08-27 18:20" in rendered


# 功能：验证在 session 间切换后能恢复各自最后一次上下文、轮次、耗时和 token 统计
# 设计：保存一组非零统计，模拟进入新会话清零后再切回，逐项断言快照完整恢复
def test_session_stats_are_restored_after_switching_back() -> None:
    app = AgentLiteTuiApp("127.0.0.1", 9999)
    app._last_context_pct = 0.42  # type: ignore[attr-defined]
    app._last_usage = (42_000, 1_200, 20_000)  # type: ignore[attr-defined]
    app._rounds = 3  # type: ignore[attr-defined]
    app._steps = 9  # type: ignore[attr-defined]
    app._llm_elapsed_s = 12.5  # type: ignore[attr-defined]
    app._tool_elapsed_s = 4.25  # type: ignore[attr-defined]
    app._ttft_total_s = 1.8  # type: ignore[attr-defined]
    app._ttft_samples = 3  # type: ignore[attr-defined]
    app._generation_elapsed_s = 9.0  # type: ignore[attr-defined]
    app._throughput_output_tokens = 900  # type: ignore[attr-defined]
    app._total_input_tokens = 42_000  # type: ignore[attr-defined]
    app._total_output_tokens = 1_200  # type: ignore[attr-defined]
    app._total_cache_read_tokens = 20_000  # type: ignore[attr-defined]

    app._remember_session_stats("session-a")  # type: ignore[attr-defined]
    app._restore_session_stats("session-new")  # type: ignore[attr-defined]
    assert app._rounds == 0  # type: ignore[attr-defined]

    app._restore_session_stats("session-a")  # type: ignore[attr-defined]

    assert app._last_context_pct == 0.42  # type: ignore[attr-defined]
    assert app._last_usage == (42_000, 1_200, 20_000)  # type: ignore[attr-defined]
    assert app._rounds == 3  # type: ignore[attr-defined]
    assert app._steps == 9  # type: ignore[attr-defined]
    assert app._llm_elapsed_s == 12.5  # type: ignore[attr-defined]
    assert app._tool_elapsed_s == 4.25  # type: ignore[attr-defined]
    assert app._ttft_samples == 3  # type: ignore[attr-defined]
    assert app._total_input_tokens == 42_000  # type: ignore[attr-defined]
    assert app._total_output_tokens == 1_200  # type: ignore[attr-defined]
    assert app._total_cache_read_tokens == 20_000  # type: ignore[attr-defined]


# 功能：验证恢复列表把 UTC 时间稳定转换为北京时间而非直接截取原字符串
# 设计：使用截图同类的 UTC 上午时间，精确断言 UTC+8 后为北京时间下午
def test_format_session_time_uses_beijing_timezone() -> None:
    assert _format_session_time("2026-08-27T09:36:00+00:00") == "2026-08-27 17:36"


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


# 功能：验证顶部状态栏第二行显示绝对 workspace 路径和模型名称
# 设计：使用带 workspace 和 model 的 TUI 实例，断言不再显示 ws 前缀且信息不占用第一行
async def test_header_shows_workspace_path_and_model_on_second_line() -> None:
    workspace = r"C:\Users\HuanZhu\Desktop\AgentLite"
    app = AgentLiteTuiApp(
        "127.0.0.1",
        7437,
        workspace_root=workspace,
        model="deepseek-chat",
    )

    async with app.run_test(size=(120, 24)):
        app._update_header("ready")  # type: ignore[attr-defined]
        content = str(app.query_one("#header", Static).content)

        lines = content.splitlines()
        assert len(lines) == 2
        assert "ws:" not in content
        assert f"workspace: {workspace}" in lines[1]
        assert "model: deepseek-chat" in lines[1]
        assert workspace not in lines[0]


# 功能：验证工具参数摘要优先展示工具最关键字段
# 设计：覆盖 read_file/bash 两类常见工具，避免工具块摘要退化成整段 JSON
def test_param_summary_prefers_key_fields() -> None:
    assert _param_summary("read_file", {"path": "README.md"}) == "path='README.md'"
    assert _param_summary("bash", {"command": "echo hi", "timeout": 1}) == "command='echo hi'"


# 功能：验证输入斜杠时 /new 作为首个内建命令出现在自动补全中
# 设计：直接检查候选顺序和说明，确保新会话入口不会被动态 Skill 列表淹没
def test_slash_items_put_new_session_first() -> None:
    app = AgentLiteTuiApp("127.0.0.1", 9999)
    assert app._build_slash_items()[0] == (  # type: ignore[attr-defined]
        "new",
        "start a new session",
        False,
    )


# 功能：验证斜杠菜单将通用命令置顶，并在首个 Skill 前显示不可选中的分组标题
# 设计：使用真实内建候选渲染菜单，比较文本位置并检查类别标记，不把标题混入导航列表
def test_slash_menu_separates_general_commands_and_skills() -> None:
    app = AgentLiteTuiApp("127.0.0.1", 9999)
    items = app._build_slash_items()  # type: ignore[attr-defined]
    assert [item[2] for item in items[:4]] == [False, False, False, False]
    assert all(item[2] for item in items[4:])

    popup = SlashCompleteWidget(items)
    popup._redraw()  # type: ignore[attr-defined]
    rendered = str(popup.content)
    assert rendered.index("/resume") < rendered.index("Skills")
    assert rendered.index("/compact") < rendered.index("Skills")
    assert rendered.index("/memories") < rendered.index("Skills")
    assert rendered.index("Skills") < rendered.index("/init")
    assert len(popup._filtered) == len(items)  # type: ignore[attr-defined]


# 功能：验证 /memories 的两个开关可以独立切换
# 设计：直接驱动选择器状态，确认只开一个和同时开启都能表示
def test_memory_select_toggles_independently() -> None:
    select = MemorySelect()
    assert "[ ] Generate memories" in select._render_ui()  # type: ignore[attr-defined]
    assert "[ ] Use memories" in select._render_ui()  # type: ignore[attr-defined]

    select._toggle_current()  # type: ignore[attr-defined]
    assert "[√] Generate memories" in select._render_ui()  # type: ignore[attr-defined]
    assert "[ ] Use memories" in select._render_ui()  # type: ignore[attr-defined]

    select._cursor = 1  # type: ignore[attr-defined]
    select._toggle_current()  # type: ignore[attr-defined]
    rendered = select._render_ui()  # type: ignore[attr-defined]
    assert "[√] Generate memories" in rendered
    assert "[√] Use memories" in rendered


# 功能：验证从斜杠补全中选择 /memories 后一次 Enter 直接打开设置页
# 设计：模拟补全选择事件，断言输入框被清空并挂载 MemorySelect，不需要第二次提交
async def test_memories_completion_opens_settings_on_first_enter() -> None:
    app = _ContextStatusHarness()

    async with app.run_test(size=(100, 24)) as pilot:
        app._client = object()  # type: ignore[assignment]
        app._session_id = "session-1"  # type: ignore[attr-defined]
        popup = SlashCompleteWidget([("memories", "configure memory settings", False)])
        await app.mount(popup, before="#prompt")
        app.on_slash_complete_widget_selected(SlashCompleteWidget.Selected("memories"))
        await pilot.pause()

        prompt = app.query_one("#prompt")
        assert prompt.text == ""
        assert prompt.disabled
        assert app.query(MemorySelect)


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
            "session.set_stats",
            "session.create",
            "event.subscribe",
            "session.close",
        ]
        assert client.calls[0][1]["session_id"] == "session-old"
        assert client.calls[2][1]["scope"] == "session:session-new"
        assert client.calls[3][1] == {"session_id": "session-old"}
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


# 功能：验证恢复历史 session 会切换订阅、加载消息并关闭被替换的临时会话
# 设计：用假 IPC 返回恢复摘要和 thread，执行完整 TUI 切换后同时断言调用顺序与历史渲染结果
async def test_resume_session_switches_subscription_and_renders_history() -> None:
    class _FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        # 记录恢复链路的 IPC 请求并返回固定会话状态和历史
        async def send_command(
            self, method: str, params: dict[str, object]
        ) -> dict[str, object]:
            self.calls.append((method, params))
            if method == "session.resume":
                return {
                    "session_id": "session-history",
                    "title": "Previous work",
                    "status": "waiting_for_input",
                    "workspace_root": "D:/current",
                    "memory_generate_enabled": True,
                    "memory_use_enabled": False,
                    "stats": {
                        "last_context_pct": 0.37,
                        "last_usage": [37_000, 900, 18_000],
                        "rounds": 2,
                        "steps": 6,
                        "llm_elapsed_s": 8.5,
                        "tool_elapsed_s": 2.0,
                        "ttft_total_s": 1.0,
                        "ttft_samples": 2,
                        "generation_elapsed_s": 6.0,
                        "throughput_output_tokens": 900,
                        "total_input_tokens": 37_000,
                        "total_output_tokens": 900,
                        "total_cache_read_tokens": 18_000,
                    },
                }
            if method == "session.get_history":
                return {
                    "messages": [
                        {"role": "user", "content": "old question"},
                        {"role": "assistant", "content": "old answer"},
                    ]
                }
            return {}

    app = _ContextStatusHarness()
    client = _FakeClient()

    async with app.run_test(size=(100, 24)):
        app._client = client  # type: ignore[assignment]
        app._session_id = "session-temporary"  # type: ignore[attr-defined]
        app._workspace_root = "D:/current"  # type: ignore[attr-defined]
        app._busy = True  # type: ignore[attr-defined]

        await app._do_resume_session("session-history", True)  # type: ignore[attr-defined]

        assert [method for method, _ in client.calls] == [
            "session.resume",
            "session.get_history",
            "event.subscribe",
            "session.close",
        ]
        assert client.calls[0][1]["workspace_root"] == "D:/current"
        assert client.calls[2][1]["scope"] == "session:session-history"
        assert app._session_id == "session-history"  # type: ignore[attr-defined]
        assert app._memory_generate_enabled  # type: ignore[attr-defined]
        assert not app._memory_use_enabled  # type: ignore[attr-defined]
        assert app._last_context_pct == 0.37  # type: ignore[attr-defined]
        assert app._rounds == 2  # type: ignore[attr-defined]
        assert app._steps == 6  # type: ignore[attr-defined]
        assert app._total_input_tokens == 37_000  # type: ignore[attr-defined]
        assert not app._busy  # type: ignore[attr-defined]
        rendered = " ".join(str(widget.content) for widget in app.query("#log-view Static"))
        assert "Previous work" in rendered
        assert "old question" in rendered


# 功能：验证 llm.token 事件累积到 LLMStreamBlock，不连续 token 各自新开一块
# 设计：monkey-patch _append 收集追加的 widgets，断言 token 追加到同一块；
#       发送非 token 事件后新 block 被重置，下一个 token 开启新块
def test_llm_tokens_accumulate_in_block() -> None:
    app = AgentLiteTuiApp("127.0.0.1", 9999)
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
    app = AgentLiteTuiApp("127.0.0.1", 9999)
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
    app = AgentLiteTuiApp("127.0.0.1", 9999)
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
    app = AgentLiteTuiApp("127.0.0.1", 9999)
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
    app = AgentLiteTuiApp("127.0.0.1", 9999)
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
    app = AgentLiteTuiApp("127.0.0.1", 9999, llm_protocol="openai")
    app._append = lambda _widget: None  # type: ignore[method-assign]

    with patch("agent_lite.tui.app.time.monotonic", side_effect=[0.0, 2.0, 4.0]):
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
    app = AgentLiteTuiApp("127.0.0.1", 9999)
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
    app = AgentLiteTuiApp("127.0.0.1", 9999)
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
    app = AgentLiteTuiApp("127.0.0.1", 9999)
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


# 功能：验证 plan.updated 创建计划区块并在再次更新时复用原区块
# 设计：直接驱动 TUI 事件处理，覆盖完成、进行中和待处理三种状态的展示更新
def test_plan_updated_reuses_plan_block() -> None:
    app = AgentLiteTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({
        "type": "plan.updated",
        "run_id": "root-run",
        "plan": [
            {"step": "inspect", "status": "in_progress"},
            {"step": "test", "status": "pending"},
        ],
        "explanation": "starting",
        "ts": "t",
    })
    app._handle_event({
        "type": "plan.updated",
        "run_id": "root-run",
        "plan": [
            {"step": "inspect", "status": "completed"},
            {"step": "test", "status": "in_progress"},
        ],
        "explanation": "next",
        "ts": "t",
    })

    assert len(appended) == 1
    assert isinstance(appended[0], PlanBlock)
    block = appended[0]
    assert "completed" not in str(block.content)
    assert "next" in str(block.content)
    assert "[x]" in str(block.content)


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


# 功能：验证 Ctrl+C 优先路由到“取消当前 run / 空闲退出”动作
# 设计：直接检查应用级绑定，防止输入框抢走 Ctrl+C 或回退到直接 quit
def test_cancel_binding_uses_priority_ctrl_c() -> None:
    bindings = [
        binding for binding in AgentLiteTuiApp.BINDINGS
        if binding.action == "cancel_run"
    ]
    assert len(bindings) == 1
    assert bindings[0].key == "ctrl+c"
    assert bindings[0].priority


# 功能：运行期间 Ctrl+C 发送 session.cancel，直到 Session ready 事件才恢复输入
# 设计：驱动完整 TUI 状态机，验证 RPC 参数、停止文案和最终输入状态
async def test_ctrl_c_cancels_current_run_without_exiting() -> None:
    class _CancelClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def send_command(
            self, method: str, params: dict[str, object]
        ) -> dict[str, object]:
            self.calls.append((method, params))
            return {"run_id": params.get("run_id", ""), "accepted": True}

    app = _ContextStatusHarness()
    client = _CancelClient()

    async with app.run_test(size=(100, 24)):
        app._client = client  # type: ignore[assignment]
        app._session_id = "session-1"  # type: ignore[attr-defined]
        app._busy = True  # type: ignore[attr-defined]
        app._current_run_id = "run-1"  # type: ignore[attr-defined]

        await app.action_cancel_run()

        assert client.calls == [
            (
                "session.cancel",
                {"session_id": "session-1", "run_id": "run-1"},
            )
        ]
        assert app._busy  # type: ignore[attr-defined]
        assert app._cancel_requested  # type: ignore[attr-defined]
        assert app.query_one("#prompt").disabled

        app._handle_event({  # type: ignore[attr-defined]
            "type": "run.finished",
            "run_id": "run-1",
            "status": "failed",
            "reason": "cancelled",
            "steps": 2,
        })
        assert app._busy  # type: ignore[attr-defined]

        app._handle_event({  # type: ignore[attr-defined]
            "type": "session.waiting_for_input",
            "session_id": "session-1",
            "last_run_id": "run-1",
        })

        assert not app._busy  # type: ignore[attr-defined]
        assert app._current_run_id is None  # type: ignore[attr-defined]
        assert not app._cancel_requested  # type: ignore[attr-defined]
        assert not app.query_one("#prompt").disabled
        rendered = " ".join(str(widget.content) for widget in app.query("#log-view Static"))
        assert "stopped" in rendered


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

    app = AgentLiteTuiApp("127.0.0.1", 9999)
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
    app = AgentLiteTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({"type": "some.unknown.type", "run_id": "r", "ts": "t"})
    assert appended == []
