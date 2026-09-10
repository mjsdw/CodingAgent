# tools/code_tool/snapshot.py
# ========== 备份与撤销（快照系统） ==========
#
# 职责：
#   1. 修改前创建快照（_create_snapshot）
#   2. 撤销最近一次修改，恢复上一个快照（undo_last）
#   3. 获取文件修改历史列表（get_history）
#
# 快照存储结构（按会话 + 完整路径 hash 隔离）：
#   {CODE_HISTORY_DIR}/{session_id}/{filename}.{path_hash}/{序号:03d}.snapshot      # 文件内容快照
#   {CODE_HISTORY_DIR}/{session_id}/{filename}.{path_hash}/{序号:03d}.meta.json     # 元数据
#   path_hash = 完整路径的 md5 前 12 位：不同目录的同名文件快照互不混存
#
# 撤销机制：栈式撤销，每次 undo 取最新快照恢复并删除该快照。

import hashlib
import json
import re
import time
from pathlib import Path

from config import CODE_HISTORY_DIR, MAX_SNAPSHOTS_PER_FILE

from tools.code_tool.path_security import _validate_path, _validate_write_path


def _get_history_dir(filepath: str, session_id: str = None) -> Path:
    """获取文件对应的历史备份目录（按会话 + 完整路径隔离）。

    存储结构：{CODE_HISTORY_DIR}/{session_id}/{filename}.{path_hash}/
    目录名带完整路径的 12 位 hash：
      - 不同目录下的同名文件（如 a/main.py 与 b/main.py）hash 不同，快照互不混存
      - 保留文件名前缀，便于人工排查快照目录归属

    不同 session 的快照互不干扰，避免跨会话撤销污染。

    :param session_id: 会话 ID（None 时 fallback 到 "default"）
    """
    p = Path(filepath).resolve()
    sid = session_id or "default"
    # 安全化 session_id：只允许字母数字._-，防止路径穿越
    safe_sid = re.sub(r"[^a-zA-Z0-9._-]", "_", sid)
    # 完整路径 hash：区分同名文件（仅用 p.name 会导致不同目录同名文件快照混存、undo 交叉污染）
    path_hash = hashlib.md5(str(p).encode("utf-8")).hexdigest()[:12]
    history_base = Path(CODE_HISTORY_DIR).resolve()
    file_history = history_base / safe_sid / f"{p.name}.{path_hash}"
    file_history.mkdir(parents=True, exist_ok=True)
    return file_history


def _create_snapshot(filepath: str, action_desc: str, session_id: str = None) -> int:
    """修改前创建快照，返回 snapshot_id。

    快照存储在 {CODE_HISTORY_DIR}/{session_id}/{filename}.{path_hash}/{序号}.snapshot
    元数据存储在 {CODE_HISTORY_DIR}/{session_id}/{filename}.{path_hash}/{序号}.meta.json
    """
    p = _validate_path(filepath, session_id)
    history_dir = _get_history_dir(filepath, session_id)

    # 计算下一个 snapshot_id：取现有最大编号 + 1。
    # 用 max 而非 len：上限清理会删除最旧的编号（如 001），此时 len(existing)=19 但最大编号=020，
    # 用 len+1=20 会覆盖已有 020 快照；用 max+1=21 则新增编号，历史链不损坏。
    existing = history_dir.glob("*.snapshot")
    max_id = max((int(f.stem) for f in existing), default=0)
    next_id = max_id + 1

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

    # 超出上限时自动清理最旧快照
    _cleanup_old_snapshots(history_dir)

    return next_id


def _create_creation_snapshot(
    filepath: str,
    new_content: str,
    action_desc: str,
    session_id: str = None,
) -> int:
    """记录一次新文件创建，使撤销能够安全删除该文件。"""
    p = _validate_write_path(filepath, session_id)
    if p.exists():
        raise ValueError(f"创建快照要求目标文件不存在: {p}")

    history_dir = _get_history_dir(filepath, session_id)
    existing = history_dir.glob("*.snapshot")
    next_id = max((int(f.stem) for f in existing), default=0) + 1

    # 空 snapshot 作为统一的历史栈条目；实际撤销行为由元数据类型决定。
    (history_dir / f"{next_id:03d}.snapshot").write_text("", encoding="utf-8")
    meta = {
        "snapshot_id": next_id,
        "filename": p.name,
        "filepath": str(p),
        "timestamp": time.time(),
        "action_desc": action_desc,
        "snapshot_type": "creation",
        "content_sha256": hashlib.sha256(new_content.encode("utf-8")).hexdigest(),
    }
    (history_dir / f"{next_id:03d}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _cleanup_old_snapshots(history_dir)
    return next_id


def _cleanup_old_snapshots(history_dir: Path) -> int:
    """清理超出 MAX_SNAPSHOTS_PER_FILE 的最旧快照。

    :return: 实际删除的快照数量
    """
    snapshots = sorted(history_dir.glob("*.snapshot"))
    deleted = 0
    while len(snapshots) > MAX_SNAPSHOTS_PER_FILE:
        oldest = snapshots.pop(0)           # 最旧的快照
        oldest_id = oldest.stem
        oldest.unlink()                      # 删除快照内容
        # 同步删除元数据
        meta = history_dir / f"{oldest_id}.meta.json"
        if meta.exists():
            meta.unlink()
        deleted += 1
    return deleted


def _discard_snapshot(
    filepath: str,
    snapshot_id: int,
    session_id: str = None,
) -> bool:
    """删除一次尚未提交成功的快照及其元数据。"""
    if snapshot_id <= 0:
        return False
    history_dir = _get_history_dir(filepath, session_id)
    snapshot_path = history_dir / f"{snapshot_id:03d}.snapshot"
    meta_path = history_dir / f"{snapshot_id:03d}.meta.json"
    removed = False
    for path in (snapshot_path, meta_path):
        if path.exists():
            path.unlink()
            removed = True
    return removed


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
    meta_path = history_dir / f"{latest_id:03d}.meta.json"
    meta = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

    if meta.get("snapshot_type") == "creation":
        expected_hash = meta.get("content_sha256", "")
        if p.exists():
            current_hash = hashlib.sha256(p.read_bytes()).hexdigest()
            if current_hash != expected_hash:
                return {
                    "snapshot_id": latest_id,
                    "remaining_undos": len(existing),
                    "status": "conflict",
                    "error": "新建文件在创建后已发生变化，为避免误删，请先检查文件内容",
                }
            p.unlink()
    else:
        content = latest_snapshot.read_text(encoding="utf-8")
        p.write_text(content, encoding="utf-8")

    # 删除已恢复的快照和元数据
    latest_snapshot.unlink()
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
