# skills/kb_search.py
# ========== 知识库检索 Skill（手写 ReAct）==========
#
# 设计：LLM 自主决定搜什么、搜几次、何时停。
#
# 控制流：
#   首轮 Query 改写（可开关）→ search → 无结果则兜底
#   if not ENABLE_MULTI_ROUND:
#       直接 ANSWER_PROMPT 生成答案
#   else:
#       ReAct 循环（最多 MAX_SEARCH_ROUND 轮）：
#           LLM 看到 question + 所有历史检索记录 → 输出 {thought, action, action_input}
#           action="search" → 执行检索，拼接 Observation，继续循环
#           action="answer" → 直接返回 action_input 作为答案
#       达上限 → ANSWER_PROMPT 兜底作答
#
# 与旧 judge 方式的区别：
#   - judge：固定模板判断 enough + 换词，LLM 只做判断不做推理
#   - ReAct：LLM 自主推理（Thought），自主选择行动（Action），更灵活

from __future__ import annotations

from langchain_core.documents import Document
from typing import Optional

from skills.base import BaseSkill, SkillContext, check_task_control

from tools.knowledge_search import knowledge_search_docs
from tools.query_rewriter import query_rewriter_plain
from tools.web_search import web_search_docs
from core.llm_client import invoke_with_system
from core.utils import parse_llm_json
from core.memory import inject_history_into_prompt

from prompts import (
    REACT_PROMPT_TEMPLATE,
    ANSWER_PROMPT_TEMPLATE,
)
from config import MAX_SEARCH_ROUND, ENABLE_QUERY_REWRITE, ENABLE_MULTI_ROUND, ENABLE_WEB_SEARCH


class KBSearchSkill(BaseSkill):
    """知识库检索技能（手写 ReAct）。

    LLM 自主决定检索策略：搜什么、搜几次、何时停。
    简单问题第一轮即可作答，复杂问题多轮深挖。
    """

    name: str = "kb_search"
    description: str = (
        "知识库检索技能，手写 ReAct 实现。"
        "LLM 自主决定检索词、检索轮次和作答时机。"
    )

    def can_handle(self, question: str, ctx: Optional[SkillContext] = None) -> float:
        """作为知识库检索的默认兜底 Skill，返回固定中等置信度 0.5。"""
        return 0.5

    # ------------------------------------------------------------------
    # execute：ReAct 检索主逻辑
    # ------------------------------------------------------------------
    def execute(
        self,
        question: str,
        ctx: Optional[SkillContext] = None,
    ) -> tuple[str, list[Document]]:
        """知识库检索：改写 → 检索 → ReAct 循环 → 生成答案。

        :return: (答案文本, 引用片段 Document 列表)
        """
        ctx = ctx or SkillContext(question=question)
        history = ctx.history or []

        # 任务控制检查点：Query 改写前
        check_task_control(ctx, "kb_search: query_rewrite")

        # ---------- 首轮检索词：可选 Query 改写 ----------
        if ENABLE_QUERY_REWRITE:
            rewrite_input = inject_history_into_prompt(question, history)
            search_query = query_rewriter_plain(rewrite_input)
            print(f"🔄 Query 改写：{question} → {search_query}")
        else:
            search_query = question

        # ---------- 第一轮检索 ----------
        print(f"\n===== [KBSearch] 第1轮检索，检索词：{search_query} =====")
        docs = knowledge_search_docs(search_query)

        if not docs:
            print("⚠️ [KBSearch] 相似度过滤后无相关片段")
            return "【知识库中未查询到相关信息】", []

        all_source_docs: list[Document] = list(docs)
        seen_contents: set[str] = {doc.page_content for doc in docs}

        # ---------- 单轮模式：直接生成答案 ----------
        if not ENABLE_MULTI_ROUND:
            # 任务控制检查点：单轮作答前
            check_task_control(ctx, "kb_search: single_answer")
            return self._generate_answer(question, docs, history)

        # ---------- ReAct 多轮模式 ----------
        # 拼接首轮检索记录作为 observations
        observations = self._format_observations([(search_query, docs, "本地")])
        search_count = 1  # 已检索 1 次
        web_fail_count = 0  # 联网搜索连续失败计数
        _WEB_MAX_FAILS = 2  # 连续失败 2 次后禁用 web_search
        used_queries: set[str] = {search_query}  # 已用过的检索词（含首轮改写词）
        seen_urls: set[str] = set()              # 已抓取过的 URL（避免联网重复抓取）

        while search_count < MAX_SEARCH_ROUND:
            # 任务控制检查点：每轮 ReAct 推理前
            check_task_control(ctx, f"kb_search: react_round_{search_count}")

            # 构造 ReAct prompt
            react_prompt = REACT_PROMPT_TEMPLATE.format(
                max_rounds=MAX_SEARCH_ROUND,
                observations=observations,
                question=question,
            )
            if history:
                react_prompt = inject_history_into_prompt(react_prompt, history)

            # LLM 推理
            resp = invoke_with_system(react_prompt)
            result_json = parse_llm_json(resp.content)

            if result_json is None:
                print("⚠️ [KBSearch] ReAct JSON 解析失败，终止循环，走兜底作答")
                break

            thought: str = result_json.get("thought", "")
            action: str = str(result_json.get("action", "")).strip().lower()
            action_input: str = str(result_json.get("action_input", "")).strip()

            print(f"🧠 Thought: {thought}")
            print(f"🎯 Action: {action} | Input: {action_input}")

            # action=answer → LLM 认为资料充足，直接返回答案
            if action == "answer":
                if not action_input:
                    print("⚠️ [KBSearch] action=answer 但 action_input 为空，走兜底作答")
                    break
                print(f"✅ [KBSearch] ReAct 完成，共 {search_count} 轮检索")
                return action_input, all_source_docs

            # action=search → 搜本地知识库
            if action == "search":
                if not action_input:
                    print("⚠️ [KBSearch] action=search 但 action_input 为空，走兜底作答")
                    break

                # 检索词重复检查：LLM 用了已搜过的词 → 跳过本次，提示换词
                if action_input in used_queries:
                    print(f"⚠️ [KBSearch] 检索词 '{action_input}' 已用过，跳过并提示 LLM 换词")
                    observations += f'\n[系统提示] 检索词 "{action_input}" 已经搜索过，请换一个不同的关键词或直接作答。\n\n'
                    continue

                used_queries.add(action_input)
                search_count += 1
                print(f"\n===== [KBSearch] 第{search_count}轮本地检索，检索词：{action_input} =====")
                new_docs = knowledge_search_docs(action_input)

                if not new_docs:
                    print("⚠️ [KBSearch] 本轮无相关片段")
                    observations += self._format_observations([(action_input, [], "本地")])
                    continue

                # 跨轮去重
                unique_new: list[Document] = []
                for doc in new_docs:
                    if doc.page_content not in seen_contents:
                        seen_contents.add(doc.page_content)
                        unique_new.append(doc)
                if unique_new:
                    all_source_docs.extend(unique_new)
                    print(f"📋 本轮新增 {len(unique_new)} 条，累计 {len(all_source_docs)} 条")
                else:
                    print("📋 本轮无新增片段（均为已检索过的重复内容）")

                observations += self._format_observations([(action_input, new_docs, "本地")])
                continue

            # action=web_search → 联网搜索
            if action == "web_search" and ENABLE_WEB_SEARCH:
                if not action_input:
                    print("⚠️ [KBSearch] action=web_search 但 action_input 为空，走兜底作答")
                    break

                # 连续失败保护：超过阈值后禁用 web_search
                if web_fail_count >= _WEB_MAX_FAILS:
                    print(f"⚠️ [KBSearch] 联网搜索已连续失败 {web_fail_count} 次，服务不可用，禁止重试")
                    observations += "\n[系统提示] 联网搜索服务当前不可用，请勿再使用 web_search，基于本地资料作答。\n\n"
                    continue

                # 检索词重复检查：与本地/联网已搜过的词重复 → 跳过
                if action_input in used_queries:
                    print(f"⚠️ [KBSearch] 检索词 '{action_input}' 已用过，跳过并提示 LLM 换词")
                    observations += f'\n[系统提示] 检索词 "{action_input}" 已经搜索过，请换一个不同的关键词或直接作答。\n\n'
                    continue

                used_queries.add(action_input)
                search_count += 1
                print(f"\n===== [KBSearch] 第{search_count}轮联网搜索，检索词：{action_input} =====")
                web_docs = web_search_docs(action_input)

                if not web_docs:
                    web_fail_count += 1
                    print(f"⚠️ [KBSearch] 联网搜索无结果（连续失败 {web_fail_count}/{_WEB_MAX_FAILS}）")
                    observations += self._format_observations([(action_input, [], "联网")])
                    continue

                # 搜索成功，重置失败计数
                web_fail_count = 0

                # 联网结果做双重去重：URL + 内容
                unique_web: list[Document] = []
                for doc in web_docs:
                    url = doc.metadata.get("source", "")
                    # URL 已抓取过 → 跳过
                    if url and url in seen_urls:
                        continue
                    # 内容已见过 → 跳过
                    if doc.page_content in seen_contents:
                        continue
                    if url:
                        seen_urls.add(url)
                    seen_contents.add(doc.page_content)
                    unique_web.append(doc)
                if unique_web:
                    all_source_docs.extend(unique_web)
                    print(f"📋 联网新增 {len(unique_web)} 条，累计 {len(all_source_docs)} 条")
                else:
                    print("📋 联网无新增片段（URL/内容均为重复）")

                observations += self._format_observations([(action_input, unique_web if unique_web else web_docs, "联网")])
                continue

            # 未知 action 或 web_search 未启用
            if action == "web_search" and not ENABLE_WEB_SEARCH:
                print("⚠️ [KBSearch] 联网搜索未启用（ENABLE_WEB_SEARCH=False），走兜底作答")
            else:
                print(f"⚠️ [KBSearch] 未知 action '{action}'，走兜底作答")
            break

        # ---------- 兜底：已达轮次上限 或 异常中断 ----------
        print(f"\n⚠️ [KBSearch] 基于现有 {len(all_source_docs)} 条资料兜底作答")
        # 任务控制检查点：兜底作答前
        check_task_control(ctx, "kb_search: fallback_answer")
        return self._generate_answer(question, all_source_docs, history)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------
    @staticmethod
    def _format_observations(searches: list[tuple[str, list[Document], str]]) -> str:
        """把检索记录格式化为 LLM 可读的文本段。

        :param searches: [(检索词, 文档列表, 来源标签), ...]
                         来源标签: "本地" 或 "联网"
        :return: 格式化文本，形如：

            [第1轮] [本地] 检索词: "索引失效场景"
            [片段1] xxx

            [第2轮] [联网] 检索词: "MySQL 索引优化"
            [片段2] yyy
        """
        parts: list[str] = []
        for i, (query, docs, source) in enumerate(searches, 1):
            if not docs:
                parts.append(f'[第{i}轮] [{source}] 检索词: "{query}"\n（无相关结果）\n')
            else:
                lines = [f'[第{i}轮] [{source}] 检索词: "{query}"']
                for j, doc in enumerate(docs, 1):
                    content = doc.page_content[:500]
                    if len(doc.page_content) > 500:
                        content += "..."
                    lines.append(f"[片段{j}] {content}")
                parts.append("\n".join(lines) + "\n")
        return "\n".join(parts)

    @staticmethod
    def _generate_answer(
        question: str,
        docs: list[Document],
        history: list[dict],
    ) -> tuple[str, list[Document]]:
        """用 ANSWER_PROMPT_TEMPLATE 基于检索结果生成兜底答案。"""
        context_text = "\n".join(doc.page_content for doc in docs)
        prompt = ANSWER_PROMPT_TEMPLATE.format(context=context_text, question=question)
        if history:
            prompt = inject_history_into_prompt(prompt, history)

        resp = invoke_with_system(prompt)
        answer = resp.content.strip() if resp and resp.content else "【知识库中未查询到相关信息】"
        print(f"✅ [KBSearch] 基于 {len(docs)} 条片段生成兜底答案")
        return answer, docs
