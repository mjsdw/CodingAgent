# tools/code_tool/diff_preview.py
# ========== Diff 预览与待确认修改 ==========
#
# 职责：
#   1. 修改前生成 unified diff 预览（preview_edit_impl / preview_write_impl）
#   2. 会话级待确认修改缓存（_PENDING_MODIFICATIONS）
#   3. 用户确认后真正执行写入 + 创建快照（confirm_modifications）
#   4. 用户取消则清空暂存（cancel_modifications）
#
# 流程：
#   Agent 生成修改 → preview_xxx 生成 diff 暂存 → 前端展示 diff
#   → 用户点"确认" → confirm_modifications 真正写入（创建快照）
#   → 用户点"撤销" → cancel_modifications 清空暂存，让用户重新描述需求

import difflib
from pathlib import Path

from tools.code_tool.path_security import _validate_path, _validate_write_path
from tools.code_tool.snapshot import _create_snapshot


# 会话级待确认修改缓存：{session_id: [{filepath, action, old_content, new_content, diff}, ...]}
# 内存维护，进程重启清空
_PENDING_MODIFICATIONS: dict[str, list[dict]] = {}


def _generate_diff(old_content: str, new_content: str, filepath: str) -> str:
    """生成 unified diff 格式的差异文本。"""
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"{filepath} (修改前)",
        tofile=f"{filepath} (修改后)",
        lineterm="",
    )
    return "".join(diff)


def preview_edit_impl(filepath: str, old_string: str, new_string: str, session_id: str = None) -> dict:
    """预览 edit_file 修改（不实际执行），暂存到待确认列表。

    :return: {status, filepath, diff, pending_count} 或 {error}
    """
    try:
        p = _validate_path(filepath, session_id)
    except ValueError as e:
        return {"error": str(e)}

    if not p.exists():
        return {"error": f"文件不存在: {p}"}

    content = p.read_text(encoding="utf-8")

    # 校验 old_string 存在性 + 唯一性（与 edit_file_impl 一致）
    if old_string not in content:
        return {"error": "old_string 在文件中未找到，请重新 read_file 确认内容"}
    count = content.count(old_string)
    if count > 1:
        return {"error": f"old_string 在文件中出现 {count} 次，请提供更长的上下文使其唯一"}

    # 计算修改后内容
    new_content = content.replace(old_string, new_string, 1)

    # 生成 diff
    diff = _generate_diff(content, new_content, str(p))

    # 暂存
    _PENDING_MODIFICATIONS.setdefault(session_id or "default", []).append({
        "filepath": str(p),
        "action": "edit_file",
        "old_string": old_string,
        "new_string": new_string,
        "old_content": content,
        "new_content": new_content,
        "diff": diff,
    })

    pending = _PENDING_MODIFICATIONS.get(session_id or "default", [])
    return {
        "status": "previewed",
        "filepath": str(p),
        "diff": diff,
        "pending_count": len(pending),
    }


def preview_write_impl(filepath: str, content: str, session_id: str = None) -> dict:
    """预览 write_file 修改（不实际执行），暂存到待确认列表。

    :return: {status, filepath, diff, pending_count, is_new} 或 {error}
    """
    try:
        p = _validate_write_path(filepath, session_id)
    except ValueError as e:
        return {"error": str(e)}

    is_new = not p.exists()
    old_content = p.read_text(encoding="utf-8") if not is_new else ""

    # 生成 diff
    diff = _generate_diff(old_content, content, str(p))

    # 暂存
    _PENDING_MODIFICATIONS.setdefault(session_id or "default", []).append({
        "filepath": str(p),
        "action": "write_file",
        "old_content": old_content,
        "new_content": content,
        "diff": diff,
        "is_new": is_new,
    })

    pending = _PENDING_MODIFICATIONS.get(session_id or "default", [])
    return {
        "status": "previewed",
        "filepath": str(p),
        "diff": diff,
        "pending_count": len(pending),
        "is_new": is_new,
    }


def get_pending_modifications(session_id: str = None) -> list[dict]:
    """获取会话所有待确认的修改列表。"""
    return list(_PENDING_MODIFICATIONS.get(session_id or "default", []))


def confirm_modifications(session_id: str = None) -> dict:
    """确认执行所有暂存的修改（真正写入文件 + 创建快照）。

    :return: {status, confirmed_count, results: [{filepath, snapshot_id, status}]}
    """
    sid = session_id or "default"
    pending = _PENDING_MODIFICATIONS.get(sid, [])
    if not pending:
        return {"status": "no_pending", "confirmed_count": 0, "results": []}

    results = []
    for mod in pending:
        filepath = mod["filepath"]
        new_content = mod["new_content"]
        action = mod["action"]

        try:
            p = Path(filepath).resolve()
            # 已存在文件创建快照
            snapshot_id = 0
            if p.exists():
                snapshot_id = _create_snapshot(filepath, f"confirm: {action}", session_id=sid)

            # 确保父目录存在
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(new_content, encoding="utf-8")

            results.append({
                "filepath": filepath,
                "snapshot_id": snapshot_id,
                "status": "applied",
            })
        except Exception as e:
            results.append({
                "filepath": filepath,
                "status": "error",
                "error": str(e),
            })

    # 清除暂存
    confirmed_count = len(pending)
    del _PENDING_MODIFICATIONS[sid]

    return {
        "status": "confirmed",
        "confirmed_count": confirmed_count,
        "results": results,
    }


def cancel_modifications(session_id: str = None) -> dict:
    """取消所有暂存的修改（不执行任何写入）。

    :return: {status, cancelled_count}
    """
    sid = session_id or "default"
    pending = _PENDING_MODIFICATIONS.get(sid, [])
    count = len(pending)
    if count > 0:
        del _PENDING_MODIFICATIONS[sid]
    return {"status": "cancelled", "cancelled_count": count}
