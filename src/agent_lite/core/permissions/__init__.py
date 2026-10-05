from typing import TYPE_CHECKING, Any

from agent_lite.core.permissions.errors import PermissionDeniedError
from agent_lite.core.permissions.policy import PermissionDecision, ToolPolicy
from agent_lite.core.permissions.storage import load_policy_file, save_policy_file

if TYPE_CHECKING:
    from agent_lite.core.permissions.manager import PermissionManager


# 延迟导入运行时权限管理器，防止配置和模型依赖形成初始化循环。
def __getattr__(name: str) -> Any:
    if name == "PermissionManager":
        from agent_lite.core.permissions.manager import PermissionManager

        return PermissionManager
    raise AttributeError(name)

__all__ = [
    "PermissionDecision",
    "PermissionDeniedError",
    "PermissionManager",
    "ToolPolicy",
    "load_policy_file",
    "save_policy_file",
]
