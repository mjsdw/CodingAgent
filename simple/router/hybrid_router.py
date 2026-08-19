# router/hybrid_router.py
# ========== 混合路由（编排规则路由 + LLM 路由）==========
#
# 职责：按"规则优先 → LLM 兜底"的顺序编排两层路由，返回最终 Skill 实例。
#
# 路由流程：
#   question → RuleRouter.route()
#                ├─ 命中 → 检查是否为 code_gen 且无可用文件 → InterceptSkill 拦截
#                │        → 否则返回对应 Skill（零延迟，处理明显场景）
#                └─ 未命中（返回 None）→ LLMRouter.route()
#                                         ├─ LLM 分类成功 → 检查是否为 code_gen 且无可用文件 → InterceptSkill 拦截
#                                         │               → 否则返回对应 Skill
#                                         └─ LLM 解析失败/异常 → KBSearchSkill 兜底
#
# 拦截逻辑（功能一）：
#   当 Router 判定问题应路由到 CodeGenSkill，但会话无可用代码文件
#   （未打开项目 + 未上传文件）时，返回 InterceptSkill 拦截，
#   避免无效调用 CodeGenSkill 的多轮 LLM（planner/executor/reflector）。
#
# 设计原则：
#   1. 规则优先：能用规则秒级命中的不浪费 LLM 调用
#   2. LLM 兜底：规则未命中的模糊场景交给 LLM 做语义判断
#   3. 永不返回 None：最差情况走 KBSearchSkill（最稳兜底）
#   4. HybridRouter 不持有 Skill 状态，每次 route 返回新实例
#   5. 代码类问题拦截：无可用文件时零 LLM 调用，直接引导用户打开项目

from skills.base import BaseSkill, SkillContext
from router.rule_router import RuleRouter
from router.llm_router import LLMRouter
from skills.intercept import InterceptSkill
from tools.code_tool import has_session_files


class HybridRouter:
    """混合路由：规则路由优先 + LLM 路由兜底 + 代码拦截。

    使用方式：
        router = HybridRouter()
        skill = router.classify(question, ctx)
        answer, sources = skill.execute(question, ctx)
    """

    def __init__(self):
        self.rule_router = RuleRouter()
        self.llm_router = LLMRouter()

    def _should_intercept_code(self, skill: BaseSkill, ctx: SkillContext) -> bool:
        """检查是否需要拦截代码类问题。

        拦截条件：
          1. skill 是 CodeGenSkill（name == "code_gen"）
          2. 会话无可用代码文件（未打开项目 + 未上传文件）

        :return: True = 需要拦截；False = 放行
        """
        if skill.name != "code_gen":
            return False
        session_id = ctx.session_id
        if not session_id:
            return True   # 无 session_id，必定无文件
        return not has_session_files(session_id)

    def classify(self, question: str, ctx: SkillContext) -> BaseSkill:
        """路由到最合适的 Skill。

        :param question: 用户原始问题
        :param ctx: 执行上下文
        :return: Skill 实例（永不返回 None）
        """
        # 第一层：规则路由（零延迟，处理明显场景）
        skill = self.rule_router.route(question, ctx)
        if skill is not None:
            # 拦截检查：代码类问题但无可用文件 → InterceptSkill
            if self._should_intercept_code(skill, ctx):
                print(f"🚫 [HybridRouter] 拦截代码类问题（规则命中但无可用文件）")
                return InterceptSkill()
            print(f"🎯 [HybridRouter] 规则路由命中：{skill.name}")
            return skill

        # 第二层：LLM 路由（~200ms，处理模糊场景）
        print(f"🎯 [HybridRouter] 规则未命中，调用 LLM Router...")
        skill = self.llm_router.route(question, ctx)

        # 拦截检查：LLM 判定为 code_gen 但无可用文件 → InterceptSkill
        if self._should_intercept_code(skill, ctx):
            print(f"🚫 [HybridRouter] 拦截代码类问题（LLM 判定 code_gen 但无可用文件）")
            return InterceptSkill()

        # LLMRouter 内部已有兜底，这里 skill 一定非 None
        return skill
