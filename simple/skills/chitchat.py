# skills/chitchat.py
# ========== 闲聊 Skill ==========
#
# 处理场景：问候 / 通用常识 / 你是谁 / 感谢 / 简单寒暄
# 不检索知识库，不调用任何工具，直接用 LLM 生成自然回复。
#
# 设计要点：
#   - can_handle 用正则匹配明显闲聊场景，返回 1.0 高置信度（让规则路由直接命中）
#   - execute 复用 CHAT_PROMPT_TPL + invoke_with_system，与 Pipeline 闲聊分支逻辑一致
#   - 历史注入：闲聊需要上下文（如"刚才你说的那个"），用 inject_history_into_prompt 拼进 prompt

import re

from langchain_core.documents import Document

from core.llm_client import invoke_with_system
from core.memory import inject_history_into_prompt
from prompts import CHAT_PROMPT_TPL
from skills.base import BaseSkill, SkillContext, check_task_control


# 明显闲聊关键词：问候 / 自我认知 / 感谢 / 简单寒暄
# 用正则做边界匹配，避免误伤技术问题（如"索引你好用吗"不应命中）
_CHITCHAT_PATTERNS = [
    r'^\s*(你好|您好|hi|hello|hey|哈喽|嗨)\s*[!！。.?\?]*\s*$',  # 纯问候
    r'^\s*(你是谁|你叫什么|你能做什么|你是干什么的)\s*[?？]*\s*$',  # 自我认知
    r'^\s*(谢谢|感谢|多谢|thanks|thank you)\s*[!！。.]*\s*$',      # 感谢
    r'^\s*(再见|拜拜|bye|goodbye)\s*[!！。.]*\s*$',                # 告别
    r'^\s*(早上好|下午好|晚上好|早安|晚安)\s*[!！。.]*\s*$',        # 时段问候
]


class ChitchatSkill(BaseSkill):
    """闲聊技能：处理问候、寒暄、自我认知等无需检索的问题。"""

    name = "chitchat"
    description = "闲聊/问候/通用常识/你是谁等无关知识库的问题，直接用 LLM 回复，不检索"

    def can_handle(self, question: str, ctx: SkillContext) -> float:
        """正则匹配明显闲聊场景，命中返回 1.0，否则返回 0.0。

        规则路由阶段用，只处理"100% 确定"的闲聊，模糊场景交给 LLM Router。
        """
        q = question.strip().lower()
        for pattern in _CHITCHAT_PATTERNS:
            if re.match(pattern, q, flags=re.IGNORECASE):
                return 1.0
        return 0.0

    def execute(self, question: str, ctx: SkillContext) -> tuple[str, list[Document]]:
        """用 CHAT_PROMPT_TPL 生成自然回复，不检索知识库。

        历史注入：闲聊场景需要上下文（用户可能接着前文聊），把历史拼进 prompt。
        """
        print("💬 [ChitchatSkill] 闲聊问题，直接 LLM 回复")

        # 任务控制检查点：调 LLM 前检查暂停/取消信号
        check_task_control(ctx, "chitchat")

        # 拼 prompt + 历史上下文
        prompt = CHAT_PROMPT_TPL.format(question=question)
        if ctx.history:
            prompt = inject_history_into_prompt(prompt, ctx.history)

        resp = invoke_with_system(prompt)
        answer = resp.content.strip() if resp and resp.content else "你好，有什么可以帮你的？"

        # 闲聊不检索，无引用片段
        return answer, []
