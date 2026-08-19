# router/llm_router.py
# ========== LLM 路由（第二层：语义分类）==========
#
# 职责：规则路由未命中时，调用 LLM 做语义分类，输出要调用的 Skill 名。
# 用 parse_llm_json 解析（复用 core.utils 的 4 层 fallback，容错强）。
#
# 设计原则：
#   1. 只在规则未命中时调用（~200ms 延迟，不浪费在明显场景上）
#   2. 复用 invoke_with_system（自带 system_role）+ parse_llm_json（鲁棒解析）
#   3. 解析失败 / 未知 skill 名 → 默认 KBSearchSkill（最稳兜底）
#   4. Skill 名映射表与 skills/ 包解耦（用字符串映射，不直接 import 所有 Skill）
#
# 路由决策依据：
#   - 问题语义（是否需要检索、是否复杂）
#   - 历史上下文（ctx.history 可辅助判断指代类问题）
#   - Skill 描述（拼进 prompt 让 LLM 知道有哪些选项）

from typing import Optional

from langchain_core.documents import Document

from config import ENABLE_CODE_AGENT
from core.llm_client import invoke_with_system
from core.utils import parse_llm_json
from skills.base import BaseSkill, SkillContext
from skills.chitchat import ChitchatSkill
from skills.kb_search import KBSearchSkill
from skills.code_gen import CodeGenSkill


# ===================== LLM 路由 Prompt =====================

_ROUTER_PROMPT = """任务：判断用户问题应该用哪个技能处理，输出 JSON。

可选技能：
- chitchat：闲聊/问候/通用常识/你是谁等无关知识库的问题
- kb_search：知识库检索，适合技术问题、概念查询、对比分析等所有需要检索知识库的问题
- code_gen：代码生成/修改技能，读取/修改/创建本地代码文件，适合"修改 xx 函数"、"修复 bug"、"写一个 xx 函数"等代码操作类问题

判断规则：
1. 纯闲聊、问候、自我认知 → chitchat
2. 明确的代码操作（修改/读取/创建代码文件、修复 bug、实现函数） → code_gen
3. 需要检索知识库的问题（技术/概念/对比/分析） → kb_search
4. 模糊问题时默认选 kb_search（最稳兜底）

严格只输出 JSON，不要任何多余解释：
{{"skill": "chitchat|kb_search|code_gen", "reason": "简要理由"}}

用户问题：{question}
"""


# ===================== Skill 名映射表 =====================
# 字符串 → Skill 类的映射，LLM 输出的 skill 名通过此表找到对应 Skill
# 新增 Skill 时只需在此表加一行（与 skills/__init__.py 的导出保持一致）

_SKILL_MAP: dict[str, type[BaseSkill]] = {
    "chitchat": ChitchatSkill,
    "kb_search": KBSearchSkill,
}
# 代码 Skill 按需注册：ENABLE_CODE_AGENT=False 时不暴露给 LLM Router
if ENABLE_CODE_AGENT:
    _SKILL_MAP["code_gen"] = CodeGenSkill

# 兜底 Skill：LLM 解析失败 / 未知 skill 名时使用
_DEFAULT_SKILL = KBSearchSkill


# ===================== LLM 路由器 =====================

class LLMRouter:
    """LLM 路由：调用 LLM 做语义分类，选择最合适的 Skill。

    使用方式：
        router = LLMRouter()
        skill = router.route(question, ctx)
        skill.execute(question, ctx)
    """

    def route(self, question: str, ctx: SkillContext) -> BaseSkill:
        """调用 LLM 分类，返回 Skill 实例。

        :param question: 用户原始问题
        :param ctx: 执行上下文（含历史，可用于辅助判断）
        :return: Skill 实例（永不返回 None，失败走兜底）
        """
        prompt = _ROUTER_PROMPT.format(question=question)

        try:
            resp = invoke_with_system(prompt)
            data = parse_llm_json(resp.content)

            if data is None:
                print("⚠️ [LLMRouter] JSON 解析失败，走兜底 kb_search")
                return _DEFAULT_SKILL()

            skill_name = data.get("skill", "").strip().lower()
            reason = data.get("reason", "")

            # 查映射表：命中则实例化，未知则兜底
            skill_cls = _SKILL_MAP.get(skill_name)
            if skill_cls is None:
                print(f"⚠️ [LLMRouter] 未知 skill 名 '{skill_name}'，走兜底 kb_search")
                return _DEFAULT_SKILL()

            print(f"🎯 [LLMRouter] 路由到 {skill_name}（理由：{reason}）")
            return skill_cls()

        except Exception as e:
            print(f"⚠️ [LLMRouter] 调用失败：{e}，走兜底 kb_search")
            return _DEFAULT_SKILL()
