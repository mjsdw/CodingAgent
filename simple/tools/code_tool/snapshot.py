# tools/code_tool/snapshot.py
# ========== 备份与撤销（快照系统） ==========
#
# 职责：
#   1. 修改前创建快照（_create_snapshot）
#   2. 撤销最近一次修改，恢复上一个快照（undo_last）
#   3. 获取文件修改历史列表（get_history）
#
# 快照存储结构：
#   {CODE_HISTORY_DIR}/{filename}/{序号:03d}.snapshot      # 文件内容快照
#   {CODE_HISTORY_DIR}/{filename}/{序号:03d}.meta.json     # 元数据
#
# 撤销机制：栈式撤销，每次 undo 取最新快照恢复并删除该快照。

import json
import time
from pathlib import Path

from config import CODE_HISTORY_DIR

from tools.code_tool.path_security import _validate_path


def _get_history_dir(filepath: str, session_id: str = None) -> Path:
    """获取文件对应的历史备份目录。

    :param session_id: 会话 ID（当前仅用于兼容签名，历史目录不按会话隔离）
    """
    p = Path(filepath).resolve()
    # 用文件名（含扩展名）作为子目录名
    history_base = Path(CODE_HISTORY_DIR).resolve()
    file_history = history_base / p.name
    file_history.mkdir(parents=True, exist_ok=True)
    return file_history


def _create_snapshot(filepath: str, action_desc: str, session_id: str = None) -> int:
    """修改前创建快照，返回 snapshot_id。

    快照存储在 {CODE_HISTORY_DIR}/{filename}/{序号}.snapshot
    元数据存储在 {CODE_HISTORY_DIR}/{filename}/{序号}.meta.json
    """
    p = _validate_path(filepath, session_id)
    history_dir = _get_history_dir(filepath, session_id)

    # 计算下一个 snapshot_id
    existing = sorted(history_dir.glob("*.snapshot"))
    next_id = len(existing) + 1

    # 存储快照内容
    snapshot_path = history_dir / f"{next_id:03d}.snapshot"
    snapshot_path.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")

    # 存储元数据
    meta = {
        "snapshot_id": next_id,
        "filename": p.name,
        "filepath": str(p),
        "timestamp": time.time(),
        "action_desc": action_desc,
    }
    meta_path = history_dir / f"{next_id:03d}.meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return next_id


def undo_last(filepath: str, session_id: str = None) -> dict:
    """撤销最近一次修改，恢复到上一个快照。

    :param filepath: 文件路径
    :param session_id: 会话 ID（用于会话级白名单校验）
    :return: {"snapshot_id": int, "remaining_undos": int, "status": str}
    """
    p = _validate_path(filepath, session_id)
    history_dir = _get_history_dir(filepath, session_id)

    existing = sorted(history_dir.glob("*.snapshot"))
    if not existing:
        return {"snapshot_id": 0, "remaining_undos": 0, "status": "no_history"}

    # 取最新的快照
    latest_snapshot = existing[-1]
    latest_id = int(latest_snapshot.stem)
    content = latest_snapshot.read_text(encoding="utf-8")

    # 恢复文件内容
    p.write_text(content, encoding="utf-8")

    # 删除已恢复的快照和元数据
    latest_snapshot.unlink()
    meta_path = history_dir / f"{latest_id:03d}.meta.json"
    if meta_path.exists():
        meta_path.unlink()

    remaining = len(list(history_dir.glob("*.snapshot")))
    return {"snapshot_id": latest_id, "remaining_undos": remaining, "status": "undone"}


def get_history(filepath: str, session_id: str = None) -> list[dict]:
    """获取文件的修改历史列表。

    :param filepath: 文件路径
    :param session_id: 会话 ID（用于会话级白名单校验）
    """
    # get_history 只读历史，不需要校验文件路径（历史目录是固定的）
    history_dir = _get_history_dir(filepath, session_id)
    metas = sorted(history_dir.glob("*.meta.json"))
    result = []
    for meta_path in metas:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        result.append(meta)
    return result
