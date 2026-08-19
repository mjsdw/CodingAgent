# core/utils.py
# ========== 共享工具：被 Agent 模式与 Pipeline 模式共同调用 ==========
# 包含两个无状态工具函数：
#   - parse_llm_json：LLM 输出 JSON 的鲁棒解析
#   - rewrite_query : 首轮 Query 改写
# 抽离到 core/ 是因为两者均被 agent/ 和 pipeline/ 依赖，避免双向引用

import json
import re

from core.llm_client import invoke_with_system
from prompts import QUERY_REWRITE_PROMPT


# ---------------------- JSON 鲁棒解析 ----------------------
def parse_llm_json(raw_content):
    """
    健壮解析LLM输出的JSON。
    按优先级依次尝试：
    1. 直接解析原始内容
    2. 剥离 <think>...</think> 思考标签后解析
    3. 剥离 ```json / ``` 代码块围栏后解析
    4. 提取首尾 { ... } 之间的JSON块解析
    全部失败则返回 None，交由上层 try/except 兜底
    """
    if not raw_content:
        return None
    text = raw_content.strip()

    # 1) 直接尝试
    try:
        return json.loads(text)
    except Exception:
        pass

    # 2) 剥离 <think> 标签（Qwen3思考模式残余内容）
    #    同时兼容 <think> ... </think> 和 <|begin_of_thought|> ... 变体
    think_stripped = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    think_stripped = re.sub(r'<\|begin_of_thought\|>.*?<\|end_of_thought\|>',
                            '', think_stripped, flags=re.DOTALL)
    think_stripped = think_stripped.strip()
    if think_stripped:
        try:
            return json.loads(think_stripped)
        except Exception:
            pass

    # 3) 剥离 ```json 或 ``` 代码围栏
    fenced = think_stripped if think_stripped else text
    # ```json ... ```
    m = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', fenced)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            pass

    # 4) 提取首尾 {} 包围的 JSON 块
    block = fenced
    lbrace = block.find('{')
    rbrace = block.rfind('}')
    if lbrace != -1 and rbrace != -1 and rbrace > lbrace:
        candidate = block[lbrace:rbrace + 1]
        try:
            return json.loads(candidate)
        except Exception:
            pass

    return None


# ---------------------- Query 改写 ----------------------
def rewrite_query(question):
    """将用户口语化问题改写为检索友好的表述，提升首轮向量召回质量。
    仅在首轮检索前执行一次；后续轮次的查询调整由 judge 的 new_search_query 接管。
    改写失败或结果异常时兜底返回原始问题，不影响主流程。"""
    try:
        rewrite_prompt = QUERY_REWRITE_PROMPT.format(question=question)
        resp = invoke_with_system(rewrite_prompt)
        if not resp or not resp.content:
            print("🔄 Query 改写：返回为空，使用原始问题")
            return question

        rewritten = resp.content.strip()
        # 剥离可能残留的 <think> 标签（双保险，配合 parse_llm_json 思路）
        rewritten = re.sub(r'<think>.*?</think>', '', rewritten, flags=re.DOTALL).strip()
        # 去除可能包裹的引号与尾部标点
        rewritten = rewritten.strip('"\'“”‘’').rstrip('。.!！?？;；')

        if rewritten and rewritten != question:
            print(f"🔄 Query 改写：{question} → {rewritten}")
            return rewritten
        print("🔄 Query 改写：保持原问题（改写结果为空或与原问题相同）")
        return question
    except Exception as e:
        print(f"⚠️ Query 改写失败，使用原始问题：{e}")
        return question
