# core/task_manager.py
# ========== 任务管理器：支持暂停/继续/取消 ==========
#
# 职责：管理异步执行的任务，支持暂停/继续/取消信号。
#
# 核心概念：
#   - TaskControl：单个任务的控制信号（paused / cancelled / step_info）
#   - task 注册表：{task_id: TaskControl}，进程内内存维护
#   - 暂停实现：
#       * CodeGenSkill：LangGraph 原生 interrupt_before（引擎自动停在节点前）
#       * 非 LangGraph Skill（chitchat/kb_search）：check_point() + wait_resume()
#
# 信号语义：
#   - paused=True：当前节点执行完后挂起等待，直到 resume 或 cancel
#   - cancelled=True：立即终止（LangGraph 不再 resume；非 LangGraph 抛 TaskCancelled）
#   - step_info：当前执行的步骤信息（供前端轮询展示进度）
#
# 设计原则：
#   1. 中断粒度：节点边界（planner/executor/reflector 之间），单次 LLM 调用内部不可中断
#   2. 暂停是"软"的：用 Event.wait() 阻塞等待，不自旋，不占 CPU
#   3. 取消是"硬"的：立即响应（cancel 时清除 paused，让阻塞的 wait() 立即返回）
#   4. 线程安全：用 threading.Event + threading.Lock 保护状态
#   5. 生命周期：任务到达终态后延迟 TASK_RETENTION_SECONDS 秒自动移除（防注册表泄漏）

import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from config import TASK_RETENTION_SECONDS


class TaskState(str, Enum):
    """任务状态。"""
    RUNNING = "running"       # 执行中
    PAUSED = "paused"         # 已暂停（等待继续）
    DONE = "done"             # 已完成
    CANCELLED = "cancelled"   # 已取消
    ERROR = "error"           # 执行出错


class TaskCancelled(Exception):
    """任务被用户取消时抛出的异常（仅用于非 LangGraph Skill）。"""
    pass


@dataclass
class TaskControl:
    """单个任务的控制信号。

    由 /api/chat 创建，传递给 Orchestrator → Skill。
    - CodeGenSkill：通过 LangGraph interrupt_before 暂停，循环驱动检查信号
    - 非 LangGraph Skill：通过 check_point() 在调 LLM 前检查信号

    Event 语义说明（重要）：
      - _paused：is_set() = True 表示"暂停中"（用于 is_paused 状态查询）
      - _resume_event：is_set() = True 表示"收到恢复信号"（用于 wait_resume 阻塞）
      - pause()：_paused.set() + _resume_event.clear()
      - resume()/cancel()：_resume_event.set()（让 wait_resume 返回）
      - wait_resume()：_resume_event.wait()（阻塞直到收到恢复/取消信号）
    """
    task_id: str
    session_id: str = ""
    question: str = ""

    # 控制信号（线程安全）
    _paused: threading.Event = field(default_factory=threading.Event)
    _resume_event: threading.Event = field(default_factory=threading.Event)
    _cancelled: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # 任务状态
    _state: TaskState = TaskState.RUNNING

    # 结果
    answer: str = ""
    sources: list = field(default_factory=list)
    error: str = ""

    # 进度信息（供前端轮询展示）
    step_info: str = ""        # 当前步骤描述，如 "执行步骤 3/8: edit_file"
    step_progress: float = 0.0  # 进度 0.0 ~ 1.0

    # 后台线程
    _thread: Optional[threading.Thread] = None

    def pause(self):
        """设置暂停信号。

        _paused.set() 标记"暂停中"状态；_resume_event.clear() 重置恢复信号。
        """
        with self._lock:
            if self._state == TaskState.RUNNING:
                self._paused.set()
                self._resume_event.clear()
                self._state = TaskState.PAUSED
                print(f"⏸️ [TaskControl] 任务 {self.task_id} 收到暂停信号")

    def resume(self):
        """恢复执行：清除暂停状态，发出恢复信号。"""
        with self._lock:
            if self._state == TaskState.PAUSED:
                self._paused.clear()
                self._resume_event.set()  # 唤醒阻塞在 wait_resume 的线程
                self._state = TaskState.RUNNING
                print(f"▶️ [TaskControl] 任务 {self.task_id} 已恢复")

    def cancel(self):
        """设置取消信号。

        同时发出恢复信号，让阻塞在 wait_resume() 的线程立即返回。
        """
        with self._lock:
            if self._state in (TaskState.RUNNING, TaskState.PAUSED):
                self._cancelled.set()
                self._paused.clear()
                self._resume_event.set()  # 唤醒阻塞在 wait_resume 的线程
                self._state = TaskState.CANCELLED
                print(f"🛑 [TaskControl] 任务 {self.task_id} 收到取消信号")

    def mark_done(self, answer: str, sources: list):
        """标记任务完成。"""
        with self._lock:
            self.answer = answer
            self.sources = sources
            self._state = TaskState.DONE

    def mark_error(self, error: str):
        """标记任务出错。"""
        with self._lock:
            self.error = error
            self._state = TaskState.ERROR

    def update_step(self, info: str, progress: float = 0.0):
        """更新当前步骤信息（供前端轮询展示）。"""
        with self._lock:
            self.step_info = info
            self.step_progress = progress

    @property
    def state(self) -> TaskState:
        with self._lock:
            return self._state

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    def wait_resume(self):
        """阻塞等待恢复信号（resume 或 cancel 都会唤醒）。

        用于 LangGraph interrupt_before 循环驱动：
          - 用户点"继续" → resume() → _resume_event.set() → wait() 返回
          - 用户点"取消" → cancel() → _resume_event.set() → wait() 返回，is_cancelled=True

        与原 check_point 自旋的区别：
          - 不再每 0.3s 轮询，纯阻塞，CPU 占用为 0
          - 取消响应延迟从 0.3s 降到立即
        """
        self._resume_event.wait()

    def check_point(self, node_name: str = ""):
        """检查点：供非 LangGraph Skill 调用（如 chitchat 在调 LLM 前检查）。

        - 若 paused：阻塞等待恢复（wait_resume）
        - 若 cancelled：抛出 TaskCancelled 异常

        :param node_name: 当前节点名（用于日志）
        :raises TaskCancelled: 任务被取消时抛出
        """
        # 1. 检查暂停：阻塞等待（不自旋）
        if self._paused.is_set():
            print(f"⏸️ [TaskControl] {node_name} 暂停，等待恢复")
            self.wait_resume()

        # 2. 检查取消：抛异常
        if self._cancelled.is_set():
            print(f"🛑 [TaskControl] {node_name} 检测到取消信号，终止执行")
            raise TaskCancelled(f"任务被用户取消（节点: {node_name}）")

    def to_status_dict(self) -> dict:
        """转换为前端轮询用的状态字典。"""
        with self._lock:
            return {
                "task_id": self.task_id,
                "state": self._state.value,
                "step_info": self.step_info,
                "step_progress": self.step_progress,
                "answer": self.answer if self._state == TaskState.DONE else "",
                "error": self.error,
                "session_id": self.session_id,
            }


# ===================== task 注册表 =====================

_TASKS: dict[str, TaskControl] = {}
_TASKS_LOCK = threading.Lock()


def create_task(session_id: str = "", question: str = "") -> TaskControl:
    """创建新任务，返回 TaskControl。

    :param session_id: 会话 ID
    :param question: 用户问题
    :return: TaskControl 实例
    """
    task_id = f"task-{uuid.uuid4().hex[:12]}"
    tc = TaskControl(task_id=task_id, session_id=session_id, question=question)
    with _TASKS_LOCK:
        _TASKS[task_id] = tc
    print(f"📋 [TaskManager] 创建任务 {task_id}（会话: {session_id}）")
    return tc


def get_task(task_id: str) -> Optional[TaskControl]:
    """获取任务控制对象。"""
    with _TASKS_LOCK:
        return _TASKS.get(task_id)


def remove_task(task_id: str):
    """从注册表移除任务（由 _schedule_removal 延迟调用，防内存泄漏）。"""
    with _TASKS_LOCK:
        _TASKS.pop(task_id, None)


def _schedule_removal(tc: TaskControl):
    """任务到达终态后，延迟从注册表移除 TaskControl。

    不能立即移除：前端需要轮询终态（done/error/cancelled）拿最终结果，
    立即移除会导致 /api/task/{id}/status 返回 404。
    保留 TASK_RETENTION_SECONDS 秒后自动清理，防止注册表无限增长。
    """
    timer = threading.Timer(
        TASK_RETENTION_SECONDS,
        remove_task,
        args=(tc.task_id,),
    )
    timer.daemon = True   # 不阻塞进程退出
    timer.start()


def run_task(tc: TaskControl, orch, question: str, session_id: str):
    """后台线程执行函数：调用 Orchestrator.query 并更新任务状态。

    :param tc: TaskControl
    :param orch: Orchestrator 实例
    :param question: 用户问题
    :param session_id: 会话 ID
    """
    try:
        answer, sources = orch.query(question, session_id=session_id, task_control=tc)
        # 查询返回后检查点：
        #   - CodeGenSkill 内部已用 interrupt_before 控制，返回时状态机已走完
        #   - 非 LangGraph Skill 的 LLM 调用期间用户点暂停，这里阻塞等待恢复
        #   - 取消则丢弃答案
        tc.check_point("before_mark_done")
        tc.mark_done(answer, sources)
        print(f"✅ [TaskManager] 任务 {tc.task_id} 完成")
    except TaskCancelled:
        tc.mark_error("任务已取消")
        print(f"🛑 [TaskManager] 任务 {tc.task_id} 已取消")
    except Exception as e:
        tc.mark_error(str(e))
        print(f"❌ [TaskManager] 任务 {tc.task_id} 出错: {e}")
    finally:
        # 无论正常完成/取消/异常，都延迟清理注册表条目（防 TaskControl 泄漏）
        _schedule_removal(tc)
