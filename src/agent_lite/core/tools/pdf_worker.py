from __future__ import annotations

import io
import json
import re
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium  # type: ignore[import-untyped]

from agent_lite.core.tools.file_operations import atomic_bytes, file_lock


# 校验单页或连续页范围，文档过长时必须明确指定范围
def page_range(value: str | None, count: int) -> range:
    if value is None:
        if count > 5:
            raise ValueError(f"PDF has {count} pages; specify pages (maximum 5 per call)")
        start, end = 1, count
    else:
        match = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", value)
        if match is None:
            raise ValueError("pages must be a page or range, e.g. '3' or '1-5'")
        start = int(match[1])
        end = int(match[2] or match[1])
    if start < 1 or end < start or end > count or end - start + 1 > 5:
        raise ValueError(f"Invalid page range for {count} pages (maximum 5 per call)")
    return range(start - 1, end)


# 提取指定页并复用按内容版本、页码和渲染参数保存的资产
def extract(request: dict[str, Any]) -> dict[str, Any]:
    directory = Path(request["directory"])
    with pdfium.PdfDocument(request["source"]) as document:
        count = len(document)
        indices = page_range(request["pages"], count)
        pages: list[dict[str, Any]] = []
        for index in indices:
            text_path = directory / f"page-{index + 1}-v1.txt"
            image_path = directory / f"page-{index + 1}-1600-v1.jpg"
            with file_lock(text_path), closing(document[index]) as page:
                item: dict[str, Any] = {"page": index + 1}
                if request["mode"] != "images":
                    if not text_path.exists():
                        with closing(page.get_textpage()) as textpage:
                            text = textpage.get_text_range()
                        encoded = text.encode("utf-8")
                        if len(encoded) > 512 * 1024:
                            text = encoded[: 512 * 1024].decode("utf-8", errors="ignore")
                            text += "\n[page text truncated]"
                        atomic_bytes(text_path, text.encode("utf-8"))
                    item["text"] = text_path.read_text("utf-8")
                if request["mode"] != "text":
                    if not image_path.exists():
                        width, height = page.get_size()
                        if width <= 0 or height <= 0:
                            raise ValueError("Invalid PDF page dimensions")
                        bitmap = page.render(scale=1600 / max(width, height))
                        try:
                            image = bitmap.to_pil().convert("RGB")
                            image.thumbnail((1600, 1600))
                        finally:
                            bitmap.close()
                        try:
                            quality = 85
                            while True:
                                output = io.BytesIO()
                                image.save(output, format="JPEG", quality=quality)
                                raw = output.getvalue()
                                # base64 编码后保持不超过 1 MiB
                                if len(raw) <= 768 * 1024:
                                    break
                                if quality > 35:
                                    quality -= 10
                                else:
                                    reduced = image.resize(
                                        (
                                            max(1, image.width * 3 // 4),
                                            max(1, image.height * 3 // 4),
                                        )
                                    )
                                    image.close()
                                    image = reduced
                            atomic_bytes(image_path, raw)
                        finally:
                            image.close()
                    item["image"] = str(image_path)
                pages.append(item)
        return {"total_pages": count, "pages": pages}


# 读取父进程请求并仅返回 JSON 元数据，不在日志中输出图片正文
def main() -> None:
    try:
        request = json.loads(sys.stdin.buffer.read())
        result = extract(request)
    except Exception as exc:
        result = {"error": f"Cannot read PDF (it may be encrypted or damaged): {exc}"}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main()
