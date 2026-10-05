from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select


class PlanInputScreen(ModalScreen[dict[str, str] | None]):
    DEFAULT_CSS = """
    PlanInputScreen { align: center middle; background: $background 70%; }
    PlanInputScreen > VerticalScroll {
        width: 85%; max-width: 100; height: auto; max-height: 90%;
        border: round $accent; padding: 1 2; background: $surface;
    }
    PlanInputScreen Label { margin-top: 1; }
    """

    # 保存问题数据以构建可选择和自由填写的表单。
    def __init__(self, questions: list[dict[str, Any]]) -> None:
        super().__init__()
        self.questions = questions

    # 逐题提供选项与补充输入，不把默认选择自动提交。
    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("计划问题")
            for index, question in enumerate(self.questions):
                yield Label(question["question"], markup=False)
                options = [(f'{o["label"]} — {o["description"]}', o["label"])
                           for o in question["options"]]
                options.append(("其他（自行填写）", ""))
                yield Select(options, value=options[0][1], allow_blank=False, id=f"choice-{index}")
                yield Input(placeholder="补充说明或其他方案", id=f"text-{index}")
            yield Label("", id="input-error")
            yield Button("提交答案", id="submit", variant="primary")
            yield Button("取消本次任务", id="cancel")

    # 检查自由填写完整性后将答案交回当前运行。
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        answers: dict[str, str] = {}
        for index, question in enumerate(self.questions):
            choice = self.query_one(f"#choice-{index}", Select).value
            extra = self.query_one(f"#text-{index}", Input).value.strip()
            answer = "\n".join(value for value in (str(choice), extra) if value)
            if not answer:
                self.query_one("#input-error", Label).update("请填写其他方案。")
                return
            answers[question["id"]] = answer
        self.dismiss(answers)
