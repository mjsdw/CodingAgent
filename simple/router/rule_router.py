# router/rule_router.py
# ========== 规则路由（第一层：零延迟快速路由）==========
#
# 职责：用关键词 + 正则做确定性快速路由，只处理"100% 确定"的明显场景。
# 未命中时返回 None，交给上层 HybridRouter 走 LLM Router。
#
# 设计原则：
#   1. 规则只处理明确场景，不写复杂判断（避免规则膨胀难维护）
#   2. 规则独立于 Skill 的 can_handle（RuleRouter 自己维护关键词，不耦合 Skill）
#   3. 命中即返回 Skill 实例，未命中返回 None
#
# 当前覆盖的规则：
#   - 闲聊：问候 / 自我认知 / 感谢 / 告别 / 时段问候 → ChitchatSkill
#   - 天气：含天气关键词 → WeatherSkill（待 WeatherSkill 实现后启用）
#   - 代码：含"写一个/写代码" → CodeGenSkill（待 CodeGenSkill 实现后启用）
#
# 未覆盖的场景（交给 LLM Router）：
#   - 知识库技术问题（统一路由到 KBSearchSkill，由 judge 自适应单轮/多轮）
#   - 模糊的闲聊（如"今天怎么样"）

import re
from typing import Optional

from config import ENABLE_CODE_AGENT
from skills.base import BaseSkill, SkillContext
from skills.chitchat import ChitchatSkill
from skills.code_gen import CodeGenSkill


# ===================== 规则定义 =====================

# 闲聊关键词：与 ChitchatSkill.can_handle 的正则保持一致
# 用完整匹配（^...$），避免误伤技术问题（如"索引你好用吗"不应命中）
_CHITCHAT_PATTERNS = [
    r'^\s*(你好|您好|hi|hello|hey|哈喽|嗨)\s*[!！。.?\?]*\s*$',
    r'^\s*(你是谁|你叫什么|你能做什么|你是干什么的)\s*[?？]*\s*$',
    r'^\s*(谢谢|感谢|多谢|thanks|thank you)\s*[!！。.]*\s*$',
    r'^\s*(再见|拜拜|bye|goodbye)\s*[!！。.]*\s*$',
    r'^\s*(早上好|下午好|晚上好|早安|晚安)\s*[!！。.]*\s*$',
]

# 天气关键词（待 WeatherSkill 实现后启用）
_WEATHER_KEYWORDS = ["天气", "下雨", "气温", "几度", "温度多少", "穿什么衣服"]

# 代码修改/生成关键词（命中即路由到 CodeGenSkill）
# 覆盖场景：明确的代码修改、Bug 修复、函数实现、文件操作等
# 注意：规则只处理"100% 确定"的代码场景，模糊场景交给 LLM Router
_CODE_PATTERNS = [
    r'(修改.*代码|改一下.*文件|改.*函数|修复.*bug|修复.*错误)',
    r'(写一个|写段|写个|实现一个|实现.*函数|生成.*代码|给个.*示例代码)',
    r'(read_file|edit_file|write_file|打开.*\.py|修改.*\.py)',
    r'(添加.*方法|删除.*代码|重构.*(代码|函数|方法|类)|优化.*(函数|方法|代码))',
]


# ===================== 规则路由器 =====================

class RuleRouter:
    """规则路由：用关键词 + 正则做零延迟快速路由。

    使用方式：
        router = RuleRouter()
        skill = router.route(question, ctx)
        if skill is not None:
            # 规则命中，直接执行
            skill.execute(question, ctx)
        else:
            # 未命中，交给 LLM Router
            ...
    """

    def route(self, question: str, ctx: SkillContext) -> Optional[BaseSkill]:
        """根据规则路由到对应 Skill。

        :param question: 用户原始问题
        :param ctx: 执行上下文
        :return: 命中则返回 Skill 实例；未命中返回 None
        """
        q = question.strip()

        # 1. 闲聊：完整匹配问候/自我认知/感谢/告别
        for pattern in _CHITCHAT_PATTERNS:
            if re.match(pattern, q, flags=re.IGNORECASE):
                return ChitchatSkill()

        # 2. 天气：包含天气关键词
        # （待 WeatherSkill 实现后取消注释）
        # for kw in _WEATHER_KEYWORDS:
        #     if kw in q:
        #         return WeatherSkill()

        # 3. 代码生成/修改：匹配明确的代码操作关键词
        # ENABLE_CODE_AGENT=False 时跳过，交给 LLM Router
        if ENABLE_CODE_AGENT:
            for pattern in _CODE_PATTERNS:
                if re.search(pattern, q):
                    return CodeGenSkill()

        # 4. 未命中任何规则 → 交给 LLM Router
        return None
