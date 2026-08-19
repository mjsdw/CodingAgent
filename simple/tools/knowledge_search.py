# tools/knowledge_search.py
# ========== 知识库检索工具（Tool 层最小单元）==========
#
# 职责：检索本地知识库，返回相关文档片段。
# 双接口设计：
#   - knowledge_search       @tool 装饰，返回格式化字符串（给 LLM/ReAct 用）
#   - knowledge_search_docs  裸函数，返回 list[Document]（给 Skill 用）
#
# 依赖方向：tools → core（Tool 是 Core 的薄封装，不含业务逻辑）

from langchain_core.tools import tool
from langchain_core.documents import Document

from core.vector_store import search


@tool
def knowledge_search(query: str) -> str:
    """检索本地知识库，返回相关文档片段。

    适用场景：回答与业务文档、技术资料、合同条款等本地知识库内容相关的问题。

    输入：检索词，建议为简洁明确的关键词或问句
          （系统内部自动执行混合检索：向量检索 + BM25 关键词检索 + RRF 融合排序）
    输出：相关文档片段的拼接文本；若未检索到相关内容会返回提示

    使用建议：
    - 问题涉及本地文档/资料时必须调用本工具
    - 可多次调用不同检索词以获取更全面信息
    - 检索词越简洁明确，召回质量越高
    - 如果已经用 query_rewriter 改写过问题，优先用改写后的词检索
    """
    docs = search(query)
    if not docs:
        return "未检索到相关资料，请换一个更简洁的关键词重试。"
    chunks = []
    for i, doc in enumerate(docs, 1):
        chunks.append(f"[片段{i}]\n{doc.page_content}")
    return "\n\n".join(chunks)


def knowledge_search_docs(query: str) -> list[Document]:
    """直接检索，返回 Document 列表（含 similarity_distance/rrf_score 等 metadata）。

    给 Skill 用：Skill 需要原始 Document 做后续处理（去重、judge、答案生成），
    而非 LLM 可读的格式化字符串。
    """
    return search(query)
