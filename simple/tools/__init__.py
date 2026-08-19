# tools 包：Tool 层（最小能力单元）
#
# Tool 是 Skill 和 Core 之间的中间层：
#   - 每个 Tool 封装一个原子能力（检索/改写/天气/联网搜索/代码读写）
#   - 提供 @tool 版本（给 LLM/ReAct bind_tools）和 plain 版本（给 Skill 直接调用）
#   - Tool 是 Core 的薄封装，不含业务逻辑
#
# 当前工具：
#   - knowledge_search    知识库检索（@tool + docs 双接口）
#   - query_rewriter      Query 改写（@tool + plain 双接口）
#   - weather_search      天气查询（仅 @tool，待 WeatherSkill 时扩展）
#   - web_search          联网搜索（@tool + docs 双接口）
#   - read_file           读取代码文件（@tool + impl 双接口）
#   - edit_file           修改代码文件（@tool + impl 双接口，自动备份）
#   - write_file          覆写代码文件（@tool + impl 双接口，自动备份）
#   - list_dir           列出目录内容（@tool + impl 双接口）
#   - grep_code          代码内容搜索（@tool + impl 双接口，支持正则）
#   - undo_last          撤销最近一次修改（plain 接口，API 直接调）
#   - get_history        获取文件修改历史（plain 接口，API 直接调）
#   - 工作区相关（plain 接口，给 app.py 的 workspace API 调用）：
#     - add_session_workspace     打开项目时加入会话级白名单
#     - remove_session_workspace  关闭项目时移除白名单
#     - get_session_workspaces    查询会话已打开的项目列表
#     - list_tree_impl            扫描目录树（懒加载）
#     - read_workspace_file_impl  读取工作区文件（前端 Monaco 展示）
#     - save_workspace_file_impl  保存工作区文件（前端 Ctrl+S，自动备份）

from tools.knowledge_search import knowledge_search, knowledge_search_docs
from tools.query_rewriter import query_rewriter, query_rewriter_plain
from tools.weather_search import weather_search
from tools.web_search import web_search, web_search_docs
from tools.code_tool import (
    read_file, edit_file, write_file,
    list_dir, grep_code,
    undo_last, get_history,
    # 工作区相关
    add_session_workspace, remove_session_workspace, get_session_workspaces,
    has_session_files,
    list_tree_impl, read_workspace_file_impl, save_workspace_file_impl,
    # Diff 预览与待确认修改
    preview_edit_impl, preview_write_impl,
    get_pending_modifications, confirm_modifications, cancel_modifications,
)

__all__ = [
    "knowledge_search",
    "knowledge_search_docs",
    "query_rewriter",
    "query_rewriter_plain",
    "weather_search",
    "web_search",
    "web_search_docs",
    "read_file",
    "edit_file",
    "write_file",
    "list_dir",
    "grep_code",
    "undo_last",
    "get_history",
    # 工作区相关
    "add_session_workspace",
    "remove_session_workspace",
    "get_session_workspaces",
    "has_session_files",
    "list_tree_impl",
    "read_workspace_file_impl",
    "save_workspace_file_impl",
    # Diff 预览与待确认修改
    "preview_edit_impl",
    "preview_write_impl",
    "get_pending_modifications",
    "confirm_modifications",
    "cancel_modifications",
]
