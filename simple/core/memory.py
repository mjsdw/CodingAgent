# core/memory.py
# ========== 对话记忆模块 ==========
#
# 职责：按 session_id 存储对话历史，供 Skill 注入 LLM 上下文。
#
# 设计：
#   - MemoryStore 抽象基类：定义 get_history / add_message / clear 接口
#   - InMemoryStore：内存 dict 实现，重启丢失（简单场景用）
#   - SQLiteStore：SQLite 持久化，重启不丢（生产/演示用）
#   - get_memory_store()：工厂函数，按 config.MEMORY_BACKEND 返回单例
#
# 消息格式：{"role": "user"|"assistant", "content": str, "timestamp": float}
#
# 集成方式：
#   - Skill 内部调 inject_history_into_prompt() 把历史拼进 prompt
#   - Orchestrator 统一管理读写，Skill 不关心记忆存储细节
import sqlite3
import time
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from config import (
    ENABLE_MEMORY,
    MEMORY_BACKEND,
    MEMORY_DB_PATH,
    MEMORY_MAX_MESSAGES,
)


# ===================== 抽象基类 =====================

class MemoryStore(ABC):
    """对话记忆存储抽象基类。"""

    @abstractmethod
    def get_history(self, session_id: str, limit: Optional[int] = None) -> list[dict]:
        """获取指定会话的历史消息。

        :param session_id: 会话 ID
        :param limit: 最多返回最近 N 条；None 表示用 MEMORY_MAX_MESSAGES
        :return: [{"role": "user"|"assistant", "content": str, "timestamp": float}, ...]
                 按时间正序排列（最旧在前，最新在后）
        """

    @abstractmethod
    def add_message(self, session_id: str, role: str, content: str) -> None:
        """追加一条消息到指定会话。

        :param role: "user" 或 "assistant"
        """

    @abstractmethod
    def clear(self, session_id: str) -> None:
        """清空指定会话的历史。"""

    def add_turn(self, session_id: str, user_question: str, assistant_answer: str) -> None:
        """便捷方法：一次性存入一轮对话（user + assistant）。"""
        self.add_message(session_id, "user", user_question)
        self.add_message(session_id, "assistant", assistant_answer)


# ===================== 内存实现 =====================

class InMemoryStore(MemoryStore):
    """内存 dict 实现，重启丢失。简单场景 / 测试用。"""

    def __init__(self):
        self._store: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    def get_history(self, session_id: str, limit: Optional[int] = None) -> list[dict]:
        n = limit if limit is not None else MEMORY_MAX_MESSAGES
        with self._lock:
            history = self._store.get(session_id, [])
            # 返回最近 n 条（滑动窗口），保持正序
            return history[-n:] if n > 0 else list(history)

    def add_message(self, session_id: str, role: str, content: str) -> None:
        with self._lock:
            if session_id not in self._store:
                self._store[session_id] = []
            self._store[session_id].append({
                "role": role,
                "content": content,
                "timestamp": time.time(),
            })

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._store.pop(session_id, None)


# ===================== SQLite 持久化实现 =====================

class SQLiteStore(MemoryStore):
    """SQLite 持久化实现，重启不丢。生产 / 演示用。

    表结构：
        conversations(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,         -- "user" | "assistant"
            content TEXT NOT NULL,
            timestamp REAL NOT NULL
        )
    索引：session_id + timestamp（按会话查历史用）
    """

    def __init__(self, db_path: str):
        self._db_path = str(db_path)
        # 确保目录存在
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        """初始化表结构（IF NOT EXISTS 幂等）。"""
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_session_time
                ON conversations(session_id, timestamp)
            """)

    def _get_conn(self) -> sqlite3.Connection:
        """每个线程用独立连接（sqlite3 线程安全限制）。
        check_same_thread=False 允许跨线程，配合 _lock 保证安全。"""
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def get_history(self, session_id: str, limit: Optional[int] = None) -> list[dict]:
        n = limit if limit is not None else MEMORY_MAX_MESSAGES
        with self._lock:
            with self._get_conn() as conn:
                if n > 0:
                    # 子查询取最近 n 条，外层再正序排列
                    rows = conn.execute("""
                        SELECT role, content, timestamp FROM (
                            SELECT role, content, timestamp
                            FROM conversations
                            WHERE session_id = ?
                            ORDER BY timestamp DESC
                            LIMIT ?
                        ) ORDER BY timestamp ASC
                    """, (session_id, n)).fetchall()
                else:
                    rows = conn.execute("""
                        SELECT role, content, timestamp
                        FROM conversations
                        WHERE session_id = ?
                        ORDER BY timestamp ASC
                    """, (session_id,)).fetchall()
            return [{"role": r["role"], "content": r["content"], "timestamp": r["timestamp"]}
                    for r in rows]

    def add_message(self, session_id: str, role: str, content: str) -> None:
        with self._lock:
            with self._get_conn() as conn:
                conn.execute("""
                    INSERT INTO conversations (session_id, role, content, timestamp)
                    VALUES (?, ?, ?, ?)
                """, (session_id, role, content, time.time()))

    def clear(self, session_id: str) -> None:
        with self._lock:
            with self._get_conn() as conn:
                conn.execute("DELETE FROM conversations WHERE session_id = ?", (session_id,))


# ===================== 工厂单例 =====================

_store_instance: Optional[MemoryStore] = None
_store_lock = threading.Lock()


def get_memory_store() -> MemoryStore:
    """获取全局 MemoryStore 单例（按 config.MEMORY_BACKEND 选择实现）。

    首次调用时创建实例，后续返回缓存。
    """
    global _store_instance
    if _store_instance is None:
        with _store_lock:
            if _store_instance is None:
                if MEMORY_BACKEND == "sqlite":
                    _store_instance = SQLiteStore(MEMORY_DB_PATH)
                    print(f"✅ 记忆模块：SQLite 持久化（{MEMORY_DB_PATH}）")
                else:
                    _store_instance = InMemoryStore()
                    print(f"✅ 记忆模块：内存模式（重启丢失）")
    return _store_instance


# ===================== 格式转换工具 =====================

def format_history_for_prompt(history: list[dict]) -> str:
    """把历史格式化为 prompt 文本段（Skill 用）。

    返回形如：
        ## 对话历史
        用户：xxx
        助手：xxx

    历史为空时返回空字符串（调用方判空后可不拼接）。
    """
    if not history:
        return ""
    lines = ["## 对话历史"]
    for m in history:
        role_label = "用户" if m["role"] == "user" else "助手"
        # 截断过长的历史内容，避免 prompt 爆炸
        content = m["content"]
        if len(content) > 500:
            content = content[:500] + "...(截断)"
        lines.append(f"{role_label}：{content}")
    return "\n".join(lines)


def inject_history_into_prompt(prompt: str, history: list[dict]) -> str:
    """把历史段 prepend 到 prompt 前面（Skill 便捷方法）。

    历史为空时原样返回 prompt。
    """
    history_section = format_history_for_prompt(history)
    if not history_section:
        return prompt
    return f"{history_section}\n\n{prompt}"
