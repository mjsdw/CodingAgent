# tools/code_tool/__init__.py
# ========== code_tool 包：代码模块工具集（按职责拆分） ==========
#
# 子模块：
#   - path_security    路径校验 + 白名单管理（内置/静态/会话动态）
#   - snapshot         修改前快照 + 撤销 + 历史记录
#   - diff_preview     Diff 预览 + 待确认修改缓存
#   - code_tools       工具实现（*_impl）+ @tool 装饰版本
#
# 对外统一导出，调用方只需 `from tools.code_tool import xxx`。
# 保持与原 tools.code_tools 完全一致的对外接口（向后兼容）。

# --- 路径安全 / 白名单 ---
from tools.code_tool.path_security import (
    add_session_workspace,
    remove_session_workspace,
    get_session_workspaces,
    has_session_files,
    # 以下为内部函数，导出供其他子模块 / 测试使用
    _validate_path,
    _validate_write_path,
    _get_effective_allowed,
    _is_within_allowed,
    _is_blocked,
)

# --- 快照 / 撤销 / 历史 ---
from tools.code_tool.snapshot import (
    undo_last,
    get_history,
    _create_snapshot,
    _get_history_dir,
)

# --- Diff 预览 / 待确认修改 ---
from tools.code_tool.diff_preview import (
    preview_edit_impl,
    preview_write_impl,
    get_pending_modifications,
    confirm_modifications,
    cancel_modifications,
    _generate_diff,
)

# --- 工具实现（裸函数） + @tool 装饰版本 ---
from tools.code_tool.code_tools import (
    # @tool 装饰版本（给 LLM bind_tools）
    read_file,
    edit_file,
    write_file,
    list_dir,
    grep_code,
    # 裸函数版本（给 LangGraph 节点 / 直接调用）
    read_file_impl,
    edit_file_impl,
    write_file_impl,
    list_dir_impl,
    grep_code_impl,
    list_tree_impl,
    read_workspace_file_impl,
    save_workspace_file_impl,
)

__all__ = [
    # 路径安全 / 白名单
    "add_session_workspace",
    "remove_session_workspace",
    "get_session_workspaces",
    "has_session_files",
    # 快照 / 撤销 / 历史
    "undo_last",
    "get_history",
    # Diff 预览 / 待确认修改
    "preview_edit_impl",
    "preview_write_impl",
    "get_pending_modifications",
    "confirm_modifications",
    "cancel_modifications",
    # @tool 装饰版本
    "read_file",
    "edit_file",
    "write_file",
    "list_dir",
    "grep_code",
    # 裸函数版本
    "read_file_impl",
    "edit_file_impl",
    "write_file_impl",
    "list_dir_impl",
    "grep_code_impl",
    "list_tree_impl",
    "read_workspace_file_impl",
    "save_workspace_file_impl",
]
