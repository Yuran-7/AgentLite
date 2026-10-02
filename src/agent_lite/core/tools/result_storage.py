from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoredToolResult:
    content: str
    output_path: str | None = None
    original_chars: int = 0
    truncated: bool = False


class ToolResultStore:
    # 初始化工具正文目录与当前轮模型预览预算，token 采用 UTF-8 字节除以四的估算
    def __init__(
        self, session_dir: Path | None, *, limit_chars: int = 8_000,
        keep_chars: int = 4_000, token_limit: int = 4_000, batch_token_limit: int = 12_000,
    ) -> None:
        self.directory = session_dir.resolve() / "tool-results" if session_dir else None
        self.limit_chars = limit_chars
        self.keep_chars = keep_chars
        self.token_limit = token_limit
        self.batch_token_limit = batch_token_limit

    # 落盘超量正文并生成带全文路径的头尾预览；失败时仍限量且明确告知无法取回全文
    def prepare(
        self, content: str, run_id: str, call_id: str, *, token_budget: int | None = None,
    ) -> StoredToolResult:
        budget_bytes = 4 * min(self.token_limit, token_budget or self.token_limit)
        raw = content.encode("utf-8")
        if len(content) <= self.limit_chars and len(raw) <= budget_bytes:
            return StoredToolResult(content=content, original_chars=len(content))
        output_path = None
        if self.directory is not None:
            digest = hashlib.sha256(
                (run_id + "\0" + call_id + "\0").encode("utf-8") + raw
            ).hexdigest()
            path = self.directory / f"{digest}.txt"
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as stream:
                    stream.write(raw)
                output_path = str(path)
            except FileExistsError:
                # 同一次调用相同正文重放时使用已存在的稳定结果文件
                try:
                    if path.read_bytes() == raw:
                        output_path = str(path)
                except OSError:
                    output_path = None
            except OSError:
                output_path = None
        location = (
            f"Full tool output saved to: {output_path}\n"
            if output_path else "Full output could not be saved; omitted content is unavailable.\n"
        )
        header = (
            "<persisted-output>\n" + location
            + f"Original size: {len(raw)} bytes, {len(content)} characters.\n"
            + "Preview only (head and tail); middle content omitted. "
            + "Use read_file with start_line/end_line to read a small range.\n"
        )
        footer = "\n</persisted-output>"
        marker = "\n[... middle content omitted ...]\n"
        remaining = max(0, budget_bytes - len((header + footer + marker).encode("utf-8")))
        preview_bytes = min(remaining, self.keep_chars)
        head_size = int(preview_bytes * 0.3)
        tail_size = preview_bytes - head_size
        head = raw[:head_size].decode("utf-8", errors="ignore")
        tail = raw[-tail_size:].decode("utf-8", errors="ignore") if tail_size else ""
        # 多行输出尽量保留完整记录，避免头尾预览切出半条 JSONL 日志
        if "\n" in head:
            head = head.rsplit("\n", 1)[0] + "\n"
        if "\n" in tail:
            tail = tail.split("\n", 1)[1]
        preview = header + head + marker + tail + footer
        if len(preview.encode("utf-8")) > budget_bytes:
            # 极小批量预算下也不透传正文，优先保留可检索路径
            preview = f"Output omitted. Full output: {output_path or 'unavailable'}"
            preview = preview.encode("utf-8")[:budget_bytes].decode("utf-8", errors="ignore")
        return StoredToolResult(preview, output_path, len(content), True)
