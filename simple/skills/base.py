# skills/base.py
# ========== Skill 抽象基类 ==========
#
# Router + Skill 架构的基石：
#   - SkillContext：Skill 执行时的上下文（question / session_id / history）
#   - BaseSkill：所有 Skill 的抽象基类，定义 can_handle + execute 契约
#
# 设计原则：
#   1. 每个 Skill 自包含，不互相调用（共享能力下沉到 core/）
#   2. can_handle 返回 0-1 置信度，Router 按分数选最高
#   3. execute 返回 (answer, sources) 与现有 query_agent / query_rag_agent 接口一致
#      → 编排层（Orchestrator）可无缝替换现有入口

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from langchain_core.documents import Document


# ===================== Skill 执行上下文 =====================

@dataclass
class SkillContext:
    """Skill 执行时的上下文。

    由 Orchestrator 在调用 Skill 前组装，包含 Skill 需要的所有运行时信息。
    字段全部可缺省，Skill 按需读取自己关心的字段。

    字段说明：
    - question:    用户原始问题（与 execute 的 question 参数一致，放这里方便 can_handle 用）
    - session_id:  会话 ID；传入则启用记忆（注入历史 + 保存本轮）
    - history:     对话历史，格式同 core.memory.MemoryStore.get_history 的返回值
                   [{"role": "user"|"assistant", "content": str, "timestamp": float}, ...]
                   按时间正序排列（最旧在前，最新在后）
    - task_control: 任务控制对象（可选），用于暂停/继续/取消
                   Skill 调 LLM 前用 check_task_control(ctx, name) 检查信号
    """
    question: str
    session_id: Optional[str] = None
    history: list[dict] = field(default_factory=list)
    task_control: Optional[object] = None   # TaskControl，类型用 object 避免循环导入


# ===================== 任务控制辅助函数 =====================

def check_task_control(ctx: SkillContext, node_name: str = ""):
    """通用任务控制检查点（供非 LangGraph Skill 使用，如 chitchat/kb_search）。

    在 Skill 调 LLM 前调用：
      - 若暂停：阻塞等待（基于 Event.wait，不自旋），直到 resume 或 cancel
      - 若取消：抛出 TaskCancelled 异常，终止执行

    注意：CodeGenSkill 不用此函数，它的暂停由 LangGraph 原生 interrupt_before 接管，
    在 CodeGenSkill.execute 的循环驱动中直接检查 task_control 信号。

    :param ctx: SkillContext（含 task_control）
    :param node_name: 当前步骤名（用于日志和进度展示）
    :raises TaskCancelled: 任务被取消时抛出
    """
    tc = getattr(ctx, "task_control", None) if ctx else None
    if tc is None:
        return
    tc.check_point(node_name)
    tc.update_step(f"步骤: {node_name}")


# ===================== Skill 抽象基类 =====================

class BaseSkill(ABC):
    """所有 Skill 的抽象基类。

    一个 Skill = 一个自包含的问题处理单元。
    Router 根据 can_handle() 的返回值选择最合适的 Skill，再调 execute() 执行。

    子类必须实现：
        - name / description 类属性
        - can_handle()：返回 0-1 置信度
        - execute()：返回 (answer, sources)

    约定：
        - can_handle 返回 0.0 表示完全无法处理
        - execute 的返回类型与 query_agent / query_rag_agent 完全一致
          → (answer: str, sources: list[Document])
        - Skill 之间不互相调用；需要共享的能力下沉到 core/
    """

    # 技能唯一标识（Router 路由依据，如 "chitchat" / "kb_search"）
    name: str = ""

    # 技能描述（给 LLM Router 看的技能说明，用于 LLM 分类）
    description: str = ""

    @abstractmethod
    def can_handle(self, question: str, ctx: SkillContext) -> float:
        """判断本 Skill 能否处理该问题，返回 0-1 置信度。

        :param question: 用户原始问题
        :param ctx: 执行上下文（含历史等，可用于判断指代类问题）
        :return: 0.0 = 完全无法处理；1.0 = 完全匹配；中间值表示部分匹配
                 Router 会调用所有已注册 Skill 的 can_handle，选分数最高的
        """
        ...

    @abstractmethod
    def execute(self, question: str, ctx: SkillContext) -> tuple[str, list[Document]]:
        """实际处理问题，返回 (答案文本, 引用片段列表)。

        :param question: 用户原始问题
        :param ctx: 执行上下文（含历史等，Skill 按需注入到 prompt）
        :return: (answer, sources)
                 - answer: 最终给用户的答案文本
                 - sources: 引用片段列表（无检索的 Skill 返回空列表 []）
        """
        ...
