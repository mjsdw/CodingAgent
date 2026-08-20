# skills 包：Router + Skill 架构的 Skill 层
#
# 每个 Skill 是一个自包含的问题处理单元：
#   - ChitchatSkill    闲聊/问候（零检索直接LLM回复）
#   - KBSearchSkill    统一知识库检索（judge 自适应单轮/多轮）
#   - WeatherSkill     天气查询（实时天气+穿衣建议，wttr.in 免费接口）
#   - CodeGenSkill     代码生成/修改（LangGraph 状态机）
#
# Router 根据 can_handle() 返回的置信度选择最合适的 Skill 执行。

from skills.base import BaseSkill, SkillContext
from skills.chitchat import ChitchatSkill
from skills.kb_search import KBSearchSkill
from skills.weather import WeatherSkill
from skills.code_gen import CodeGenSkill

__all__ = [
    "BaseSkill",
    "SkillContext",
    "ChitchatSkill",
    "KBSearchSkill",
    "WeatherSkill",
    "CodeGenSkill",
]
