# tools/code_tool/code_tools.py
# ========== 代码模块工具集（工具实现 + @tool 装饰版本） ==========
#
# 职责：
#   1. 提供 *_impl 裸函数版本（给 LangGraph 节点 / 直接代码调用）
#   2. 提供 @tool 装饰版本（给 LLM bind_tools 用）
#
# 工具列表：
#   - read_file / read_file_impl         读取代码文件
#   - edit_file / edit_file_impl         局部替换（自动备份）
#   - write_file / write_file_impl       全量覆写（自动备份）
#   - list_dir  / list_dir_impl          列目录（一层）
#   - grep_code / grep_code_impl         代码内容搜索（正则）
#   - list_tree_impl                     扫描目录树（前端文件树）
#   - read_workspace_file_impl            读取工作区文件（Monaco 展示）
#   - save_workspace_file_impl            保存工作区文件（Ctrl+S，自动备份）
#
# 依赖关系：
#   - path_security：路径校验 + 白名单
#   - snapshot：修改前自动快照
#   - diff_preview：修改前 diff 预览（在 diff_preview.py 中实现）

import re
from pathlib import Path

from langchain_core.tools import tool

from config import (
    WORKSPACE_TREE_DEFAULT_DEPTH, WORKSPACE_TREE_MAX_DEPTH,
    WORKSPACE_HIDDEN_DIRS,
)

from tools.code_tool.path_security import (
    _validate_path, _validate_write_path, _get_effective_allowed,
)
from tools.code_tool.snapshot import _create_snapshot


# ------------------------------------------------------------------
# 基础读写工具实现（裸函数，给 LangGraph 节点直接调）
# ------------------------------------------------------------------
def read_file_impl(filepath: str, session_id: str = None) -> str:
    """读取本地代码文件内容。

    :param session_id: 会话 ID，传入时合并会话级动态白名单
    """
    p = _validate_path(filepath, session_id)
    if not p.exists():
        return f"错误：文件不存在: {p}"
    if not p.is_file():
        return f"错误：不是文件: {p}"
    return p.read_text(encoding="utf-8")


def edit_file_impl(filepath: str, old_string: str, new_string: str, session_id: str = None) -> str:
    """修改代码文件的指定部分，修改前自动备份。

    :param filepath: 文件路径
    :param old_string: 需要替换的原始代码片段（必须完全匹配）
    :param new_string: 替换后的新代码
    :param session_id: 会话 ID，传入时合并会话级动态白名单
    :return: 修改结果描述
    """
    p = _validate_path(filepath, session_id)
    if not p.exists():
        return f"错误：文件不存在: {p}"

    content = p.read_text(encoding="utf-8")

    # 校验 old_string 存在
    if old_string not in content:
        return f"错误：old_string 在文件中未找到，请重新 read_file 确认内容"

    # 校验唯一性
    count = content.count(old_string)
    if count > 1:
        # 返回所有匹配位置（行号+上下文），帮助 LLM 精确定位
        lines = content.splitlines()
        locations = []
        search_start = 0
        for _ in range(min(count, 5)):  # 最多返回 5 处，防止输出过长
            pos = content.find(old_string, search_start)
            if pos == -1:
                break
            # 通过累计字符数定位行号
            lineno = content[:pos].count('\n') + 1
            # 提取该行上下文（前一行 + 当前行 + 后一行）
            ctx_lines = []
            for offset in (-1, 0, 1):
                idx = lineno - 1 + offset
                if 0 <= idx < len(lines):
                    prefix = "  >" if offset == 0 else "   "
                    ctx_lines.append(f"{prefix} {idx + 1}: {lines[idx][:120]}")
            locations.append(f"  位置 {len(locations) + 1}（第 {lineno} 行）:\n" + "\n".join(ctx_lines))
            search_start = pos + len(old_string)
        hint = "\n".join(locations)
        if count > 5:
            hint += f"\n  ...（共 {count} 处匹配，仅显示前 5 处）"
        return f"错误：old_string 在文件中出现 {count} 次，请提供更长的上下文使其唯一。\n匹配位置：\n{hint}"

    # 创建快照
    snapshot_id = _create_snapshot(filepath, f"edit: 替换片段", session_id=session_id)

    # 执行替换
    new_content = content.replace(old_string, new_string)
    p.write_text(new_content, encoding="utf-8")

    return f"已修改 {p.name}，快照ID={snapshot_id}，可撤销。"


def write_file_impl(filepath: str, content: str, session_id: str = None) -> str:
    """覆写整个代码文件，修改前自动备份。

    :param filepath: 文件路径
    :param content: 新的文件内容
    :param session_id: 会话 ID，传入时合并会话级动态白名单
    :return: 修改结果描述
    """
    p = _validate_write_path(filepath, session_id)

    # 如果文件已存在，创建快照
    snapshot_id = 0
    if p.exists():
        snapshot_id = _create_snapshot(filepath, f"write: 全量覆写", session_id=session_id)

    # 确保父目录存在
    p.parent.mkdir(parents=True, exist_ok=True)

    # 写入新内容
    p.write_text(content, encoding="utf-8")

    return f"已写入 {p.name}，快照ID={snapshot_id}，可撤销。"


# ------------------------------------------------------------------
# 探索类工具实现（list_dir / grep_code）
# ------------------------------------------------------------------
# grep_code 允许搜索的代码/文本文件扩展名白名单
_GREP_ALLOWED_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs", ".c", ".cpp",
    ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt", ".scala",
    ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".html", ".css", ".scss", ".less", ".vue", ".svelte",
    ".sql", ".sh", ".bat", ".ps1", ".dockerfile",
}


def list_dir_impl(dirpath: str, session_id: str = None) -> str:
    """列出指定目录下的文件和子目录（仅一层，不递归）。

    :param dirpath: 目录路径
    :param session_id: 会话 ID（用于会话级白名单校验，必须传入才能访问用户打开的项目）
    :return: 目录内容列表文本，每行一个条目，带 [DIR]/[FILE] 标记
    """
    p = _validate_path(dirpath, session_id)
    if not p.exists():
        return f"错误：目录不存在: {p}"
    if not p.is_dir():
        return f"错误：不是目录: {p}"

    try:
        entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except PermissionError:
        return f"错误：无权限访问目录: {p}"
    except Exception as e:
        return f"错误：列目录异常 - {e}"

    # 统一复用 WORKSPACE_HIDDEN_DIRS 过滤隐藏目录（.venv/.git/__pycache__/node_modules 等），并跳过 . 开头隐藏文件
    visible = [e for e in entries if not (e.is_dir() and _is_hidden_dir(e.name)) and not (e.is_file() and e.name.startswith("."))]
    if not visible:
        return f"（目录 {p.name} 为空）"

    lines = [f"目录: {p}（共 {len(visible)} 项）"]
    for entry in visible:
        tag = "[DIR] " if entry.is_dir() else "[FILE]"
        lines.append(f"  {tag}{entry.name}")

    return "\n".join(lines)


def grep_code_impl(pattern: str, dirpath: str = "", session_id: str = None) -> str:
    """在代码文件中搜索匹配的行（支持正则）。

    :param pattern: 搜索模式（正则表达式或关键字）
    :param dirpath: 限定搜索目录（空字符串表示搜索所有白名单工作区）
    :param session_id: 会话 ID（用于会话级白名单校验，必须传入才能搜索用户打开的项目）
    :return: 匹配结果文本，格式：文件路径:行号:匹配行内容
    """
    if not pattern:
        return "错误：grep_code 缺少 pattern 参数"

    # ReDoS 防护：编译前检查正则安全性
    is_safe, reason = _check_regex_safe(pattern)
    if not is_safe:
        return f"错误：正则表达式不安全 - {reason}（请简化正则或拆分为多个简单模式）"

    # 编译正则
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"错误：正则表达式编译失败 - {e}"

    # 确定搜索范围
    if dirpath:
        search_root = _validate_path(dirpath, session_id)
        if not search_root.exists():
            return f"错误：目录不存在: {search_root}"
        if not search_root.is_dir():
            return f"错误：不是目录: {search_root}"
        search_roots = [search_root]
    else:
        # 不指定目录时，遍历所有生效白名单（内置 + 静态 + 会话动态）
        search_roots = _get_effective_allowed(session_id)

    return _grep_search(regex, search_roots)


# ReDoS 防护：危险正则特征（嵌套量词 + 重叠量词）
_RE_DANGEROUS_PATTERNS = [
    re.compile(r'\(.+[*+?]\)[*+?]'),       # (a+)+  嵌套量词
    re.compile(r'\(.+\.\*\)[*+?]'),        # (.*)+  嵌套通配
    re.compile(r'\([^)]+[*+?][^)]*\)[*+]'), # (a+b*)+ 重叠量词
]
_GREP_MAX_PATTERN_LEN = 200   # 正则表达式最大长度
_GREP_MAX_LINE_LEN = 5000     # 单行最大长度（超过跳过正则匹配）


def _check_regex_safe(pattern: str) -> tuple[bool, str]:
    """检查正则表达式是否有 ReDoS 风险。

    :return: (is_safe, reason)  is_safe=False 时 reason 说明原因
    """
    if len(pattern) > _GREP_MAX_PATTERN_LEN:
        return False, f"正则表达式过长（{len(pattern)} > {_GREP_MAX_PATTERN_LEN}）"
    for dangerous in _RE_DANGEROUS_PATTERNS:
        if dangerous.search(pattern):
            return False, f"正则包含危险嵌套量词模式（可能触发 ReDoS）"
    return True, ""


def _safe_regex_search(regex, line: str) -> bool:
    """安全的正则匹配，带行长度限制防止 ReDoS。

    超长行直接跳过（minified JS、大 JSON 行等不适宜正则逐行匹配）。
    """
    if len(line) > _GREP_MAX_LINE_LEN:
        return False
    return bool(regex.search(line))


def _grep_search(regex, roots: list) -> str:
    """在多个目录下递归搜索匹配行。

    限制：
    - 仅搜索 _GREP_ALLOWED_EXTS 中的代码/文本文件
    - 跳过 WORKSPACE_HIDDEN_DIRS 全部隐藏目录（.venv/.git/__pycache__/node_modules 等）
    - 单文件最多 20 条，总数最多 50 条（防 prompt 膨胀）
    - 单行截断 200 字符
    """
    matches = []
    total_matches = 0
    max_total = 50
    max_per_file = 20

    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        try:
            for file_path in root.rglob("*"):
                if not file_path.is_file():
                    continue
                if file_path.suffix.lower() not in _GREP_ALLOWED_EXTS:
                    continue
                # 统一复用 WORKSPACE_HIDDEN_DIRS：任何一级目录名命中即跳过（含 .venv/.git/__pycache__/node_modules 等）
                if any(_is_hidden_dir(part) for part in file_path.parts):
                    continue

                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue

                file_matches = 0
                for lineno, line in enumerate(content.splitlines(), start=1):
                    if _safe_regex_search(regex, line):
                        line_short = line.strip()[:200]
                        matches.append(f"{file_path}:{lineno}: {line_short}")
                        file_matches += 1
                        total_matches += 1
                        if file_matches >= max_per_file:
                            matches.append(f"  ...（{file_path.name} 超过 {max_per_file} 条匹配，已截断）")
                            break
                        if total_matches >= max_total:
                            break
                if total_matches >= max_total:
                    break
            if total_matches >= max_total:
                break
        except PermissionError:
            continue
        except Exception:
            continue

    if not matches:
        return f"未找到匹配项（pattern={regex.pattern}）"

    header = f"搜索 {regex.pattern}（共 {total_matches} 条匹配）:"
    if total_matches >= max_total:
        header += f"（已达上限 {max_total} 条，已截断）"
    return header + "\n" + "\n".join(matches)


# ------------------------------------------------------------------
# 文件树扫描（工作区懒加载）
# ------------------------------------------------------------------
def _is_hidden_dir(name: str) -> bool:
    """判断目录名是否在隐藏列表中（不区分大小写）。"""
    return name.lower() in WORKSPACE_HIDDEN_DIRS


def _scan_tree(root: Path, depth: int, max_depth: int) -> list[dict]:
    """递归扫描目录树，返回嵌套结构。

    :param root: 当前扫描目录
    :param depth: 当前深度（从 1 开始）
    :param max_depth: 最大深度
    :return: [{name, path, type, size?, children?}, ...]
    """
    if depth > max_depth:
        return []

    items = []
    try:
        entries = sorted(root.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except PermissionError:
        return []
    except Exception:
        return []

    for entry in entries:
        # 跳过隐藏目录
        if entry.is_dir() and _is_hidden_dir(entry.name):
            continue
        # 跳过隐藏文件（. 开头）
        if entry.is_file() and entry.name.startswith("."):
            continue

        item = {
            "name": entry.name,
            "path": str(entry.resolve()),
            "type": "dir" if entry.is_dir() else "file",
        }

        if entry.is_dir():
            # 递归扫描子目录
            if depth < max_depth:
                item["children"] = _scan_tree(entry, depth + 1, max_depth)
            else:
                # 达到最大深度，标记为未展开
                item["children"] = None   # None 表示有子项但未加载
                item["expandable"] = True
        else:
            # 文件附加大小
            try:
                item["size"] = entry.stat().st_size
            except Exception:
                item["size"] = 0

        items.append(item)

    return items


def list_tree_impl(dirpath: str, session_id: str = None, depth: int = None) -> dict:
    """扫描目录树，返回嵌套结构（供前端文件树展示）。

    :param dirpath: 目录路径（通常是项目根或子目录）
    :param session_id: 会话 ID（用于会话级白名单校验）
    :param depth: 扫描深度，默认 WORKSPACE_TREE_DEFAULT_DEPTH
    :return: {name, path, type, children} 或 {error: ...}
    """
    if depth is None:
        depth = WORKSPACE_TREE_DEFAULT_DEPTH
    # 防御：不超过最大深度
    depth = min(depth, WORKSPACE_TREE_MAX_DEPTH)

    try:
        p = _validate_path(dirpath, session_id)
    except ValueError as e:
        return {"error": str(e)}

    if not p.exists():
        return {"error": f"目录不存在: {p}"}
    if not p.is_dir():
        return {"error": f"不是目录: {p}"}

    return {
        "name": p.name,
        "path": str(p),
        "type": "dir",
        "children": _scan_tree(p, 1, depth),
    }


def read_workspace_file_impl(filepath: str, session_id: str = None) -> str:
    """读取工作区文件内容（供前端 Monaco 编辑器展示，与 read_file_impl 同逻辑）。

    :param filepath: 文件路径
    :param session_id: 会话 ID（用于会话级白名单校验）
    :return: 文件内容字符串或错误信息
    """
    try:
        p = _validate_path(filepath, session_id)
    except ValueError as e:
        return f"错误：{e}"

    if not p.exists():
        return f"错误：文件不存在: {p}"
    if not p.is_file():
        return f"错误：不是文件: {p}"

    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # 二进制文件尝试忽略错误
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return f"错误：读取失败: {e}"


def save_workspace_file_impl(filepath: str, content: str, session_id: str = None) -> dict:
    """保存工作区文件内容（前端 Ctrl+S 调用，自动创建快照）。

    :param filepath: 文件路径
    :param content: 新内容
    :param session_id: 会话 ID（用于会话级白名单校验）
    :return: {status, snapshot_id?, filepath} 或 {error: ...}
    """
    try:
        p = _validate_write_path(filepath, session_id)
    except ValueError as e:
        return {"error": str(e)}

    # 已存在文件创建快照
    snapshot_id = 0
    if p.exists():
        snapshot_id = _create_snapshot(filepath, f"workspace save: 前端保存", session_id=session_id)

    # 确保父目录存在
    p.parent.mkdir(parents=True, exist_ok=True)

    try:
        p.write_text(content, encoding="utf-8")
    except Exception as e:
        return {"error": f"保存失败: {e}"}

    return {
        "status": "saved",
        "snapshot_id": snapshot_id,
        "filepath": str(p),
    }


# ------------------------------------------------------------------
# @tool 装饰版本（给 LLM bind_tools 用）
# ------------------------------------------------------------------
@tool
def read_file(filepath: str) -> str:
    """读取本地代码文件内容。

    Args:
        filepath: 文件的绝对路径或相对路径
    Returns:
        文件内容字符串
    """
    return read_file_impl(filepath)


@tool
def edit_file(filepath: str, old_string: str, new_string: str) -> str:
    """修改代码文件的指定部分。修改前会自动备份，支持撤销。
    old_string 必须在文件中存在且唯一匹配。

    Args:
        filepath: 文件路径
        old_string: 需要替换的原始代码片段（必须与文件内容完全匹配）
        new_string: 替换后的新代码
    Returns:
        修改结果描述
    """
    return edit_file_impl(filepath, old_string, new_string)


@tool
def write_file(filepath: str, content: str) -> str:
    """覆写整个代码文件。修改前会自动备份，支持撤销。
    如果文件不存在会创建新文件。

    Args:
        filepath: 文件路径
        content: 新的文件完整内容
    Returns:
        修改结果描述
    """
    return write_file_impl(filepath, content)


@tool
def list_dir(dirpath: str) -> str:
    """列出指定目录下的文件和子目录（仅一层，不递归）。

    Args:
        dirpath: 目录的绝对路径或相对路径
    Returns:
        目录内容列表，每行一个条目，带类型标记 [DIR]/[FILE]
    """
    return list_dir_impl(dirpath)


@tool
def grep_code(pattern: str, dirpath: str = "") -> str:
    """在代码文件中搜索匹配的行（支持正则）。

    不指定 dirpath 时搜索整个工作区，指定时只搜该目录。
    只搜索代码/文本文件（.py/.js/.ts/.java/.go/.md/.txt/.json/.yaml/.yml 等）。

    Args:
        pattern: 搜索模式（正则表达式或关键字）
        dirpath: 限定搜索目录（可选，默认整个工作区）
    Returns:
        匹配结果，格式：文件路径:行号:匹配行内容
    """
    return grep_code_impl(pattern, dirpath)
