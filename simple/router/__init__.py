# router 包：Router + Skill 架构的路由层
#
# 三层路由策略（HybridRouter 编排）：
#   1. RuleRouter    规则路由（零延迟，处理明显场景）
#   2. LLMRouter     LLM 路由（~200ms，处理模糊场景）
#   3. 兜底          KBSearchSkill（最稳兜底）
#
# 路由流程：
#   question → RuleRouter.route()
#                ├─ 命中 → 返回对应 Skill
#                └─ 未命中 → LLMRouter.route()
#                              ├─ 解析成功 → 返回对应 Skill
#                              └─ 解析失败 → KBSearchSkill 兜底

from router.rule_router import RuleRouter
from router.llm_router import LLMRouter
from router.hybrid_router import HybridRouter

__all__ = [
    "RuleRouter",
    "LLMRouter",
    "HybridRouter",
]
