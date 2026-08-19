# tools/query_rewriter.py
# ========== Query 改写工具（Tool 层最小单元）==========
#
# 职责：将口语化问题改写为检索友好的表述，提升向量召回质量。
# 双接口设计：
#   - query_rewriter          @tool 装饰，返回字符串（给 LLM/ReAct 用）
#   - query_rewriter_plain    裸函数，返回字符串（给 Skill 用）
#
# 两者返回类型一致（都是 str），区别仅在于 @tool 装饰器给 LLM 提供工具描述。
# Skill 直接调 plain 版本避免 @tool 的 invoke 开销。

from langchain_core.tools import tool

from core.utils import rewrite_query as _rewrite_query


@tool
def query_rewriter(question: str) -> str:
    """将口语化问题改写为检索友好的表述。

    适用场景：用户问题口语化严重、含省略或指代不清时，先改写再检索效果更好。

    输入：用户原始问题
    输出：改写后的检索词（已去除口语化表达、补全指代）

    使用建议：
    - 仅在问题口语化严重时调用，简洁明确的问题无需改写
    - 改写后的结果应作为 knowledge_search 的输入
    - 改写失败时会自动回退为原始问题，不影响后续流程
    """
    return _rewrite_query(question)


def query_rewriter_plain(question: str) -> str:
    """直接改写，返回字符串。给 Skill 用。

    与 @tool 版本逻辑完全一致，只是不走 LangChain @tool 的 invoke 机制。
    """
    return _rewrite_query(question)
