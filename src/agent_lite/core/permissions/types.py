from __future__ import annotations

from typing import Literal, cast

PermissionMode = Literal["manual", "accept_edits", "auto"]
PERMISSION_MODES = ("manual", "accept_edits", "auto")
APPROVAL_DECISIONS = ("allow_once", "always_allow", "deny_once", "always_deny")
AUTO_DECISIONS = ("allow_once", "deny_once", "always_deny")


# 校验权限模式，禁止损坏数据静默扩大权限。
def validate_permission_mode(value: object) -> PermissionMode:
    if not isinstance(value, str) or value not in PERMISSION_MODES:
        raise ValueError(f"invalid permission mode: {value!r}")
    return cast(PermissionMode, value)
