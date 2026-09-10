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

from core.session_lifecycle import begin_request, run_if_current
from tools.code_tool.atomic_write import atomic_write_text
from tools.code_tool.path_security import _validate_path, _validate_write_path
from tools.code_tool.snapshot import (
    _create_snapshot, _create_creation_snapshot, _discard_snapshot,
)


# 会话级待确认修改缓存：{session_id: [{filepath, action, old_content, new_content, diff}, ...]}
# 内存维护，进程重启清空
_PENDING_MODIFICATIONS: dict[str, list[dict]] = {}


def _find_pending_modification(session_id: str, filepath: str) -> dict | None:
    """查找同一会话、同一路径尚未确认的累计修改。"""
    for mod in _PENDING_MODIFICATIONS.get(session_id, []):
        if mod["filepath"] == filepath:
            return mod
    return None


def _store_pending_modification(session_id: str, modification: dict) -> None:
    """同一文件只保留一个累计版本，避免确认时后一个版本覆盖前一个。"""
    pending = _PENDING_MODIFICATIONS.setdefault(session_id, [])
    for index, existing in enumerate(pending):
        if existing["filepath"] == modification["filepath"]:
            pending[index] = modification
            return
    pending.append(modification)


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


def preview_edit_impl(
    filepath: str,
    old_string: str,
    new_string: str,
    session_id: str = None,
    session_generation: int = None,
) -> dict:
    """预览 edit_file 修改（不实际执行），暂存到待确认列表。

    :return: {status, filepath, diff, pending_count} 或 {error}
    """
    try:
        p = _validate_path(filepath, session_id)
    except ValueError as e:
        return {"error": str(e)}

    sid = session_id or "default"
    generation = session_generation if session_generation is not None else begin_request(sid)
    existing = _find_pending_modification(sid, str(p))
    if not p.exists() and existing is None:
        return {"error": f"文件不存在: {p}"}

    # 同一文件已有待确认修改时，继续在累计版本上编辑，而不是重新读取磁盘旧版本。
    content = existing["new_content"] if existing else p.read_text(encoding="utf-8")
    old_content = existing["old_content"] if existing else content

    # 校验 old_string 存在性 + 唯一性（与 edit_file_impl 一致）
    if old_string not in content:
        return {"error": "old_string 在文件中未找到，请重新 read_file 确认内容"}
    count = content.count(old_string)
    if count > 1:
        return {"error": f"old_string 在文件中出现 {count} 次，请提供更长的上下文使其唯一"}

    # 计算修改后内容
    new_content = content.replace(old_string, new_string, 1)

    # 生成 diff
    diff = _generate_diff(old_content, new_content, str(p))

    # 暂存；同一路径覆盖为累计后的单个版本。
    modification = {
        "filepath": str(p),
        "action": existing["action"] if existing else "edit_file",
        "old_string": old_string,
        "new_string": new_string,
        "old_content": old_content,
        "new_content": new_content,
        "diff": diff,
        "is_new": existing.get("is_new", False) if existing else False,
    }
    run_if_current(sid, generation, lambda: _store_pending_modification(sid, modification))

    pending = _PENDING_MODIFICATIONS.get(sid, [])
    return {
        "status": "previewed",
        "filepath": str(p),
        "diff": diff,
        "pending_count": len(pending),
    }


def preview_write_impl(
    filepath: str,
    content: str,
    session_id: str = None,
    session_generation: int = None,
) -> dict:
    """预览 write_file 修改（不实际执行），暂存到待确认列表。

    :return: {status, filepath, diff, pending_count, is_new} 或 {error}
    """
    try:
        p = _validate_write_path(filepath, session_id)
    except ValueError as e:
        return {"error": str(e)}

    sid = session_id or "default"
    generation = session_generation if session_generation is not None else begin_request(sid)
    existing = _find_pending_modification(sid, str(p))
    is_new = existing.get("is_new", False) if existing else not p.exists()
    old_content = (
        existing["old_content"]
        if existing
        else p.read_text(encoding="utf-8") if not is_new else ""
    )

    # 生成 diff
    diff = _generate_diff(old_content, content, str(p))

    # 暂存；write_file 的内容是目标完整版本，同一路径只保留最新累计结果。
    modification = {
        "filepath": str(p),
        "action": "write_file",
        "old_content": old_content,
        "new_content": content,
        "diff": diff,
        "is_new": is_new,
    }
    run_if_current(sid, generation, lambda: _store_pending_modification(sid, modification))

    pending = _PENDING_MODIFICATIONS.get(sid, [])
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

    安全与容错设计：
      1. 写入前对每个文件重新调用 _validate_write_path——preview 时的校验
         结果可能已过期（用户查看 diff 期间可能关闭了项目，白名单已变化），
         不能信任 preview 阶段的校验结论（TOCTOU）
      2. 写入失败的暂存项保留在暂存区（只移除成功项）——用户处理完失败
         原因（如重新打开项目）后可再次 confirm 重试，失败内容不丢失

    :return: {status, confirmed_count, failed_count, results: [{filepath, snapshot_id, status}]}
             status: no_pending / confirmed（全部成功）/ partial_confirmed（部分失败，失败项保留）
    """
    sid = session_id or "default"
    pending = _PENDING_MODIFICATIONS.get(sid, [])
    if not pending:
        return {"status": "no_pending", "confirmed_count": 0, "failed_count": 0, "results": []}

    results = []
    succeeded = []   # 成功写入的暂存项
    failed = []      # 写入失败的暂存项（保留在暂存区供重试）
    conflict_count = 0

    for mod in pending:
        filepath = mod["filepath"]
        new_content = mod["new_content"]
        action = mod["action"]

        try:
            # 写入前重新校验路径（不能信任 preview 时的校验结果）
            p = _validate_write_path(filepath, sid)

            # 预览后磁盘内容发生变化时拒绝覆盖，保护用户或其他任务的新修改。
            is_new = mod.get("is_new", False)
            if is_new and p.exists():
                raise FileExistsError("文件在预览后已被创建，请重新生成修改预览")
            if not is_new:
                if not p.exists():
                    raise FileNotFoundError("文件在预览后已被删除，请重新生成修改预览")
                if p.read_text(encoding="utf-8") != mod["old_content"]:
                    results.append({
                        "filepath": filepath,
                        "status": "conflict",
                        "error": "文件在预览后已发生变化，请重新生成修改预览",
                    })
                    failed.append(mod)
                    conflict_count += 1
                    continue

            # 已存在文件创建快照
            snapshot_id = 0
            try:
                if p.exists():
                    snapshot_id = _create_snapshot(filepath, f"confirm: {action}", session_id=sid)
                else:
                    snapshot_id = _create_creation_snapshot(
                        filepath,
                        new_content,
                        f"confirm: {action}",
                        session_id=sid,
                    )

                # 确保父目录存在并写入；任一步失败都回滚本次快照。
                p.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(p, new_content)
            except Exception:
                if snapshot_id:
                    _discard_snapshot(filepath, snapshot_id, session_id=sid)
                raise

            results.append({
                "filepath": filepath,
                "snapshot_id": snapshot_id,
                "status": "applied",
            })
            succeeded.append(mod)
        except (FileExistsError, FileNotFoundError) as e:
            results.append({
                "filepath": filepath,
                "status": "conflict",
                "error": str(e),
            })
            failed.append(mod)
            conflict_count += 1
        except Exception as e:
            results.append({
                "filepath": filepath,
                "status": "error",
                "error": str(e),
            })
            failed.append(mod)

    # 只清除成功项；失败项保留在暂存区，用户可处理后重试
    if not failed:
        del _PENDING_MODIFICATIONS[sid]
    else:
        _PENDING_MODIFICATIONS[sid] = failed

    return {
        "status": (
            "confirmed" if not failed
            else "conflict" if conflict_count == len(failed) and not succeeded
            else "partial_confirmed"
        ),
        "confirmed_count": len(succeeded),
        "failed_count": len(failed),
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
