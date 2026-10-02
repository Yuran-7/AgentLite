from __future__ import annotations

import base64
import binascii
import io
from typing import Literal

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field, model_validator

MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_IMAGE_BASE64 = 4 * 1024 * 1024


class ImageAttachment(BaseModel):
    name: str = Field(default="image", max_length=255)
    media_type: Literal["image/png", "image/jpeg", "image/gif", "image/webp"]
    data: str = Field(min_length=1, max_length=MAX_IMAGE_BASE64)

    @model_validator(mode="after")
    # 校验真实图片格式、完整性与尺寸，拒绝伪装文件和过大图片
    def validate_image(self) -> ImageAttachment:
        try:
            raw = base64.b64decode(self.data, validate=True)
            if len(raw) > MAX_IMAGE_BYTES:
                raise ValueError("图片不能超过 3 MiB")
            with Image.open(io.BytesIO(raw)) as image:
                if Image.MIME.get(image.format or "") != self.media_type:
                    raise ValueError("图片格式与 MIME 类型不匹配")
                if max(image.size) > 8192 or image.width * image.height > 32_000_000:
                    raise ValueError("图片尺寸过大，请缩小到 8192 像素以内")
                image.verify()
        except (
            binascii.Error, OSError, UnidentifiedImageError, Image.DecompressionBombError,
        ) as exc:
            raise ValueError("无效图片，请使用 PNG、JPEG、GIF 或 WebP") from exc
        return self

    # 生成可直接发送给 Anthropic 并持久化到历史的图片块
    def block(self) -> dict[str, object]:
        return {"type": "image", "source": {
            "type": "base64", "media_type": self.media_type, "data": self.data,
        }}
