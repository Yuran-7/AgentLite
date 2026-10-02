import base64
import io
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from agent_lite.core.bus.commands import SessionSendMessageCommand
from agent_lite.core.events.bus import EventBus
from agent_lite.core.llm.images import ImageAttachment
from agent_lite.core.llm.openai_provider import _convert_messages
from agent_lite.core.session.manager import SessionManager
from agent_lite.core.session.store import SessionStore
from tests.unit.test_session_manager import _Runner


# 生成真实的小 PNG 以验证图片校验和模型序列化
def attachment() -> ImageAttachment:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(buffer, format="PNG")
    return ImageAttachment(media_type="image/png", data=base64.b64encode(buffer.getvalue()).decode())


# 功能：图片随消息持久化，恢复后仍可转换为模型的图片输入
# 设计：使用真实 SessionStore 和运行替身覆盖仅图片和图文消息的完整保存链路
@pytest.mark.parametrize("text", ["", "看看这张图片"])
async def test_image_history_and_provider(tmp_path: Path, text: str) -> None:
    image = attachment()
    command = SessionSendMessageCommand(session_id="test", content=text, images=[image])
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")
    await manager.send_message(session.id, command.content, images=command.images)
    messages = store.read_messages(session.id)
    assert messages[0]["content"][-1] == image.block()
    converted = _convert_messages(messages, None)
    assert converted[1]["content"][-1] == {"type": "image_url", "image_url": {  # type: ignore[index]
        "url": f"data:image/png;base64,{image.data}",
    }}
    assert store.read_meta(session.id).title == (text or "图片提问")


# 功能：拒绝伪造格式、无效编码和超出数量限制的附件
# 设计：从可信 PNG 派生错误输入，确保协议入口验证不依赖前端限制
def test_invalid_images_rejected() -> None:
    image = attachment()
    for change in [{"data": "bad!"}, {"data": "AAAA"}, {"media_type": "image/jpeg"}]:
        with pytest.raises(ValidationError):
            ImageAttachment.model_validate(image.model_dump() | change)
    with pytest.raises(ValidationError):
        SessionSendMessageCommand(session_id="test", content="", images=[image] * 5)
