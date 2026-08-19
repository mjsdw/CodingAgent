# skills/code_gen.py
# ========== 代码生成/修改 Skill（Plan-and-Execute + Reflexion）==========
#
# 架构升级：从 ReAct（每轮临时决策）→ Plan-and-Execute + Reflexion
#   1. planner   ：LLM 一次性生成完整执行计划（步骤列表）
#   2. executor  ：按计划逐步执行（read/edit/write）
#   3. reflector  ：每步执行后 LLM 反思，决定 continue/retry/replan/done
#   4. replanner  ：反思失败时带反馈重新规划（复用 planner 逻辑 + REPLAN_PROMPT）
#   5. summarize  ：最终总结
#
# 状态机拓扑：
#   START → planner → executor → reflector → executor → ... → summarize → END
#                       ↑              ↓
#                       └←←← replanner ←┘（反思判定 replan 时）
#                                      ↓
#                                 executor（反思判定 retry 时，重试当前步）
#
# 三重保护：
#   1. MAX_CODE_ROUNDS：总执行步数上限（防死循环）
#   2. MAX_REPLAN_COUNT：重规划次数上限（防无限重规划）
#   3. MAX_RETRY_COUNT：单步重试次数上限（防单步卡死）

from __future__ import annotations

from typing import Optional, TypedDict

from langchain_core.documents import Document
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from skills.base import BaseSkill, SkillContext

from core.llm_client import invoke_with_system
from core.utils import parse_llm_json
from core.memory import inject_history_into_prompt

from tools.code_tool import (
    read_file_impl,
    edit_file_impl,
    write_file_impl,
    list_dir_impl,
    grep_code_impl,
    # Diff 预览
    preview_edit_impl,
    preview_write_impl,
    get_pending_modifications,
)

from prompts import (
    PLAN_PROMPT,
    REPLAN_PROMPT,
    REFLECT_PROMPT,
    CODE_ANSWER_PROMPT,
    ANALYZE_PROMPT,
    GENERATE_EDIT_PROMPT,
    GENERATE_WRITE_PROMPT,
)
from config import MAX_CODE_ROUNDS, ENABLE_CODE_AGENT

# 重规划与重试上限
MAX_REPLAN_COUNT = 3
MAX_RETRY_COUNT = 2


# ===================== 状态定义 =====================

class CodeState(TypedDict, total=False):
    """LangGraph 状态：贯穿 planner / executor / reflector / summarize。

    核心字段：
    - question:          用户原始需求
    - history:           对话历史（供 prompt 注入）
    - plan:              LLM 生成的步骤列表 [{step_id, action, action_input, description}, ...]
    - current_step_idx:  当前执行到第几步（0-based）
    - executed_steps:    已执行步骤记录 [{step, result, success}, ...]
    - reflections:       反思记录列表 [str, ...]
    - plan_revision_count: 重规划次数
    - retry_count:       当前步重试次数
    - total_steps:       已执行总步数（含重试，防死循环）
    - final_answer:      最终总结
    - done:              是否完成（reflector 判定 done 或达上限）
    """
    question: str
    history: list[dict]
    plan: list[dict]
    current_step_idx: int
    executed_steps: list[dict]
    reflections: list[str]
    plan_revision_count: int
    retry_count: int
    total_steps: int
    final_answer: str
    done: bool
    _route_signal: str
    # Diff 预览相关
    session_id: str               # 会话 ID（用于 preview/confirm 的暂存隔离）
    pending_modifications: list   # 待确认修改列表（来自 preview_edit/write）
    # 任务控制（暂停/继续/取消）
    task_control: object          # TaskControl 实例（可选）


# ===================== 辅助函数 =====================

def _format_plan(plan: list[dict]) -> str:
    """把计划列表渲染成 prompt 文本。"""
    if not plan:
        return "（空计划）"
    lines = []
    for step in plan:
        sid = step.get("step_id", "?")
        action = step.get("action", "?")
        desc = step.get("description", "")
        lines.append(f"  步骤{sid}: {action} - {desc}")
    return "\n".join(lines)


def _format_executed_steps(executed: list[dict]) -> str:
    """把已执行步骤渲染成 prompt 文本。"""
    if not executed:
        return "（暂无）"
    lines = []
    for item in executed:
        step = item.get("step", {})
        result = item.get("result", "")
        success = "✅" if item.get("success") else "❌"
        action = step.get("action", "?")
        desc = step.get("description", "")
        # 结果截断防 prompt 膨胀
        result_short = result[:300] + "..." if len(result) > 300 else result
        lines.append(f"  {success} 步骤{step.get('step_id', '?')}: {action} - {desc}")
        lines.append(f"    结果: {result_short}")
    return "\n".join(lines)


def _format_reflections(reflections: list[str]) -> str:
    """把反思记录渲染成 prompt 文本。"""
    if not reflections:
        return "（暂无）"
    return "\n".join(f"  - {r}" for r in reflections)


def _format_current_step(step: dict) -> str:
    """渲染当前步骤信息。"""
    return (
        f"步骤{step.get('step_id', '?')}: {step.get('action', '?')}\n"
        f"描述: {step.get('description', '')}\n"
        f"参数: {step.get('action_input', {})}"
    )


def _format_plan_progress(plan: list[dict], current_idx: int, executed: list[dict]) -> str:
    """渲染整体进度。"""
    total = len(plan)
    done = len(executed)
    return f"计划共 {total} 步，已执行 {done} 步，当前第 {current_idx + 1} 步"


# ===================== 修改参数动态生成 =====================
# Plan-and-Execute 架构的核心补丁：
#   planner 规划 edit_file/write_file 时未读过文件，无法生成精确的 old_string/content
#   因此 executor 执行时，若参数为空，调 LLM 基于"已读内容 + 修改意图"动态生成

def _find_read_content(state: CodeState, filepath: str) -> str:
    """从 executed_steps 中找到指定文件最近一次 read_file 的结果。

    :return: 文件内容字符串；未找到返回空字符串
    """
    if not filepath:
        return ""
    # 规范化比较（统一小写、统一分隔符）
    target = filepath.replace("\\", "/").lower()
    for item in reversed(state.get("executed_steps", [])):
        step = item.get("step", {})
        if step.get("action", "").lower() != "read_file":
            continue
        fp = step.get("action_input", {}).get("filepath", "")
        if fp.replace("\\", "/").lower() == target:
            return item.get("result", "")
    return ""


def _generate_edit_params(
    question: str, intent: str, filepath: str, file_content: str
) -> dict:
    """调 LLM 基于已读内容 + 修改意图，生成 edit_file 的 old_string/new_string。

    :return: {old_string, new_string, explanation} 或 {error}
    """
    # 截断防 prompt 膨胀（与 read_file 截断一致）
    content = file_content[:3000]
    if len(file_content) > 3000:
        content += f"\n...(共 {len(file_content)} 字符，已截断)"

    prompt = GENERATE_EDIT_PROMPT.format(
        question=question,
        intent=intent,
        filepath=filepath,
        file_content=content,
    )

    print(f"🤖 [CodeGen] 动态生成 edit 参数（文件: {filepath}，内容 {len(content)} 字符）")
    resp = invoke_with_system(prompt)
    raw = resp.content if resp and resp.content else ""
    result = parse_llm_json(raw)

    if result is None:
        print(f"⚠️ [CodeGen] edit 参数生成 JSON 解析失败，原始输出前 200 字: {raw[:200]}")
        return {"error": f"JSON 解析失败: {raw[:100]}"}

    if "error" in result:
        return result

    old_string = result.get("old_string", "")
    new_string = result.get("new_string", "")
    if not old_string or not new_string:
        return {"error": "LLM 未返回有效的 old_string/new_string"}

    return result


def _generate_write_content(
    question: str, intent: str, filepath: str, existing_content: str
) -> dict:
    """调 LLM 基于修改意图，生成 write_file 的完整 content。

    :return: {content, explanation} 或 {error}
    """
    content = existing_content[:3000]
    if len(existing_content) > 3000:
        content += f"\n...(共 {len(existing_content)} 字符，已截断)"

    prompt = GENERATE_WRITE_PROMPT.format(
        question=question,
        intent=intent,
        filepath=filepath,
        existing_content=content,
    )

    print(f"🤖 [CodeGen] 动态生成 write 内容（文件: {filepath}，已有内容 {len(content)} 字符）")
    resp = invoke_with_system(prompt)
    raw = resp.content if resp and resp.content else ""
    result = parse_llm_json(raw)

    if result is None:
        print(f"⚠️ [CodeGen] write 内容生成 JSON 解析失败，原始输出前 200 字: {raw[:200]}")
        return {"error": f"JSON 解析失败: {raw[:100]}"}

    if "error" in result:
        return result

    content = result.get("content", "")
    if not content:
        return {"error": "LLM 未返回有效的 content"}

    return result


# ===================== 动作处理器（每个返回执行结果字符串）=====================

def _exec_read_file(action_input: dict, state: CodeState) -> str:
    """read_file 动作：读取文件内容，截断防 prompt 膨胀。"""
    filepath = action_input.get("filepath", "")
    if not filepath:
        return "错误：read_file 缺少 filepath 参数"
    session_id = state.get("session_id")
    result = read_file_impl(filepath, session_id=session_id)
    if len(result) > 3000:
        result = result[:3000] + f"\n...(共 {len(result)} 字符，已截断)"
    return result


def _exec_edit_file(action_input: dict, state: CodeState) -> str:
    """edit_file 动作：生成 diff 预览（不直接修改），暂存待用户确认。

    修改流程：Agent 规划 edit_file → executor 检测参数为空 →
    调 LLM 基于"已读内容 + 修改意图"动态生成 old_string/new_string →
    生成 diff 暂存 → 状态机提前结束 → 前端展示 diff → 用户确认 → /api/code/confirm 真正写入。
    """
    filepath = action_input.get("filepath", "")
    old_string = action_input.get("old_string", "") or ""
    new_string = action_input.get("new_string", "") or ""
    if not filepath:
        return "错误：edit_file 缺少 filepath 参数"

    # Plan-and-Execute 核心补丁：planner 规划时未读过文件，old_string 必然错误
    # 检测到 old_string 为空时，调 LLM 基于"已读内容 + 修改意图"动态生成
    if not old_string:
        intent = action_input.get("description", "") or state.get("question", "")
        question = state.get("question", "")

        # 从已执行步骤中找该文件的 read_file 结果
        file_content = _find_read_content(state, filepath)
        if not file_content:
            return (
                f"错误：执行 edit_file 前未读取过 {filepath}，无法动态生成修改参数。"
                f"请在计划中先安排 read_file 该文件。"
            )

        # 调 LLM 生成 old_string/new_string
        params = _generate_edit_params(question, intent, filepath, file_content)
        if "error" in params:
            return f"错误：动态生成修改参数失败 - {params['error']}"
        old_string = params["old_string"]
        new_string = params["new_string"]
        explanation = params.get("explanation", "")
        print(f"✅ [CodeGen] edit 参数已生成（{explanation[:80]}）")

    session_id = state.get("session_id", "default")
    result = preview_edit_impl(filepath, old_string, new_string, session_id=session_id)

    if "error" in result:
        return f"错误：{result['error']}"

    # 记录到 state 的 pending 列表
    pending = state.get("pending_modifications", [])
    pending.append({
        "filepath": result["filepath"],
        "action": "edit_file",
        "diff": result["diff"],
    })
    state["pending_modifications"] = pending

    return (
        f"已生成修改预览（待用户确认）。\n"
        f"文件: {result['filepath']}\n"
        f"待确认修改数: {result['pending_count']}\n"
        f"Diff 预览:\n{result['diff']}"
    )


def _exec_write_file(action_input: dict, state: CodeState) -> str:
    """write_file 动作：生成 diff 预览（不直接修改），暂存待用户确认。"""
    filepath = action_input.get("filepath", "")
    content = action_input.get("content", "") or ""
    if not filepath:
        return "错误：write_file 缺少 filepath 参数"

    # Plan-and-Execute 核心补丁：planner 规划时未读过文件，content 可能不准确
    # 检测到 content 为空时，调 LLM 基于"修改意图"动态生成
    if not content:
        intent = action_input.get("description", "") or state.get("question", "")
        question = state.get("question", "")

        # 尝试从已执行步骤中找该文件的 read_file 结果（覆写场景）
        existing_content = _find_read_content(state, filepath)

        # 调 LLM 生成 content
        params = _generate_write_content(question, intent, filepath, existing_content)
        if "error" in params:
            return f"错误：动态生成写入内容失败 - {params['error']}"
        content = params["content"]
        explanation = params.get("explanation", "")
        print(f"✅ [CodeGen] write 内容已生成（{explanation[:80]}）")

    session_id = state.get("session_id", "default")
    result = preview_write_impl(filepath, content, session_id=session_id)

    if "error" in result:
        return f"错误：{result['error']}"

    # 记录到 state 的 pending 列表
    pending = state.get("pending_modifications", [])
    pending.append({
        "filepath": result["filepath"],
        "action": "write_file",
        "diff": result["diff"],
        "is_new": result.get("is_new", False),
    })
    state["pending_modifications"] = pending

    label = "新建文件" if result.get("is_new") else "覆写已有文件"
    return (
        f"已生成{label}预览（待用户确认）。\n"
        f"文件: {result['filepath']}\n"
        f"待确认修改数: {result['pending_count']}\n"
        f"Diff 预览:\n{result['diff']}"
    )


def _exec_list_dir(action_input: dict, state: CodeState) -> str:
    """list_dir 动作：列出目录内容（一层，不递归）。"""
    dirpath = action_input.get("dirpath", "")
    if not dirpath:
        return "错误：list_dir 缺少 dirpath 参数"
    session_id = state.get("session_id")
    return list_dir_impl(dirpath, session_id=session_id)


def _exec_grep_code(action_input: dict, state: CodeState) -> str:
    """grep_code 动作：在代码文件中搜索匹配行（支持正则）。"""
    pattern = action_input.get("pattern", "")
    if not pattern:
        return "错误：grep_code 缺少 pattern 参数"
    dirpath = action_input.get("dirpath", "")
    session_id = state.get("session_id")
    return grep_code_impl(pattern, dirpath, session_id=session_id)


def _exec_analyze(action_input: dict, state: CodeState) -> str:
    """analyze 动作：汇总已读代码内容，调 LLM 按 focus 分析。"""
    focus = action_input.get("focus", "代码分析")
    question = state.get("question", "")

    # 汇总此前所有 read_file 步骤读取到的代码内容
    code_snippets = []
    for item in state.get("executed_steps", []):
        step = item.get("step", {})
        if step.get("action", "").lower() == "read_file":
            fp = step.get("action_input", {}).get("filepath", "未知文件")
            content = item.get("result", "")
            # 单文件截断 3000 字符（与 read_file 输出截断一致）
            if len(content) > 3000:
                content = content[:3000] + f"\n...(共 {len(content)} 字符，已截断)"
            code_snippets.append(f"=== 文件: {fp} ===\n{content}")

    if not code_snippets:
        return "错误：analyze 前未执行任何 read_file，没有可分析的代码内容"

    code_context = "\n\n".join(code_snippets)
    # 总长度兜底截断，防 prompt 膨胀
    if len(code_context) > 8000:
        code_context = code_context[:8000] + "\n...(已截断)"

    prompt = ANALYZE_PROMPT.format(
        question=question,
        focus=focus,
        code_context=code_context,
    )

    print(f"🔍 [CodeGen] 执行分析（focus={focus}，代码 {len(code_context)} 字符）")
    resp = invoke_with_system(prompt)
    result = resp.content.strip() if resp and resp.content else "错误：分析未产生有效结论"

    if result.startswith("错误"):
        print(f"❌ [CodeGen] 分析失败: {result[:200]}")
    else:
        print(f"📊 [CodeGen] 分析完成（{len(result)} 字）")
    return result


# 动作分发表：所有动作都需要访问 state（至少取 session_id 做白名单校验）
# 统一签名：(action_input: dict, state: CodeState) -> str
_STATE_ACTIONS = {
    "read_file": _exec_read_file,
    "list_dir": _exec_list_dir,
    "grep_code": _exec_grep_code,
    "analyze": _exec_analyze,
    "edit_file": _exec_edit_file,
    "write_file": _exec_write_file,
}


# ===================== 节点实现 =====================

def planner_node(state: CodeState) -> CodeState:
    """计划节点：调 LLM 生成完整执行计划。

    首次调用用 PLAN_PROMPT，重规划时用 REPLAN_PROMPT（带反馈）。
    """
    question = state["question"]
    history = state.get("history", [])
    is_replan = state.get("plan_revision_count", 0) > 0

    if is_replan:
        # 重规划：带已执行步骤和反思反馈
        prompt = REPLAN_PROMPT.format(
            question=question,
            max_steps=MAX_CODE_ROUNDS,
            executed_steps=_format_executed_steps(state.get("executed_steps", [])),
            reflections=_format_reflections(state.get("reflections", [])),
        )
        print(f"🔄 [CodeGen] 重规划（第 {state.get('plan_revision_count', 0)} 次）")
    else:
        # 初始规划
        prompt = PLAN_PROMPT.format(
            question=question,
            max_steps=MAX_CODE_ROUNDS,
        )

    if history:
        prompt = inject_history_into_prompt(prompt, history)

    # 调 LLM
    resp = invoke_with_system(prompt)
    raw = resp.content if resp and resp.content else ""
    result = parse_llm_json(raw)

    if result is None:
        print(f"⚠️ [CodeGen] planner JSON 解析失败，原始输出前 200 字: {raw[:200]}")
        # 解析失败，创建一个空计划直接走总结
        return {**state, "plan": [], "done": True}

    plan = result.get("steps", [])
    analysis = result.get("plan_analysis", "")

    if not plan:
        print(f"⚠️ [CodeGen] planner 返回空计划")
        return {**state, "plan": [], "done": True}

    print(f"📋 [CodeGen] 计划生成完成（{len(plan)} 步）: {analysis}")
    for step in plan:
        print(f"   步骤{step.get('step_id', '?')}: {step.get('action', '?')} - {step.get('description', '')}")

    # 重规划时重置步索引、重试计数和总步数计数器
    # total_steps 重置让重规划后有完整的步数额度（已执行的步骤记录仍保留在 executed_steps 中）
    new_state = {
        **state,
        "plan": plan,
        "current_step_idx": 0,
        "retry_count": 0,
    }
    if is_replan:
        new_state["total_steps"] = 0
        print(f"♻️ [CodeGen] 重规划后重置 total_steps=0，保留 {len(state.get('executed_steps', []))} 条已执行记录")
    return new_state


def executor_node(state: CodeState) -> CodeState:
    """执行节点：按计划执行当前步骤，调对应动作处理器。"""
    plan = state.get("plan", [])
    idx = state.get("current_step_idx", 0)
    total_steps = state.get("total_steps", 0)

    # 守卫1：计划已执行完
    if idx >= len(plan):
        return {**state, "done": True}

    step = plan[idx]
    action = step.get("action", "").strip().lower()
    action_input = step.get("action_input", {}) or {}

    # 防御：action_input 类型修正
    if not isinstance(action_input, dict):
        action_input = {}

    print(f"🔧 [CodeGen] 执行步骤 {idx + 1}/{len(plan)}: {action}")

    # 守卫2：finish 直接完成
    if action == "finish":
        print(f"✅ [CodeGen] 收到 finish 信号，任务完成")
        return {**state, "done": True}

    # 分发执行：所有动作统一走 _STATE_ACTIONS（均需 state 取 session_id 做白名单校验）
    if action in _STATE_ACTIONS:
        try:
            result = _STATE_ACTIONS[action](action_input, state)
        except Exception as e:
            result = f"错误：工具执行异常 - {e}"
    else:
        result = f"错误：未知 action '{action}'"

    is_fail = result.startswith("错误")
    print(f"{'❌' if is_fail else '✅'} [CodeGen] 步骤 {idx + 1} 结果: {result[:200]}")

    # 统一记录已执行步骤
    executed = list(state.get("executed_steps", []))
    executed.append({
        "step": step,
        "result": result,
        "success": not is_fail,
    })

    # Diff 预览模式：edit_file/write_file 成功后，检测到 pending 修改则提前 finish
    # 让 summarize_node 输出预览提示，等用户在前端确认
    new_state = {
        **state,
        "executed_steps": executed,
        "total_steps": total_steps + 1,
    }
    if not is_fail and action in ("edit_file", "write_file"):
        pending = state.get("pending_modifications", [])
        if pending:
            print(f"⏸️ [CodeGen] 检测到 {len(pending)} 个待确认修改，提前结束等待用户确认")
            new_state["done"] = True

    return new_state


def reflector_node(state: CodeState) -> CodeState:
    """反思节点：LLM 评估执行结果，决定 continue/retry/replan/done。"""
    question = state["question"]
    plan = state.get("plan", [])
    idx = state.get("current_step_idx", 0)
    executed = state.get("executed_steps", [])
    reflections = list(state.get("reflections", []))
    total_steps = state.get("total_steps", 0)

    # 获取刚执行的步骤和结果
    last_executed = executed[-1] if executed else None
    if not last_executed:
        # 无执行记录，直接完成
        return {**state, "done": True}

    current_step = last_executed["step"]
    execution_result = last_executed["result"]

    # 如果是 finish 步骤，直接完成
    if current_step.get("action", "").lower() == "finish":
        return {**state, "done": True}

    # 拼 prompt
    prompt = REFLECT_PROMPT.format(
        question=question,
        current_step=_format_current_step(current_step),
        execution_result=execution_result,
        plan_progress=_format_plan_progress(plan, idx, executed),
    )

    resp = invoke_with_system(prompt)
    raw = resp.content if resp and resp.content else ""
    result = parse_llm_json(raw)

    if result is None:
        print(f"⚠️ [CodeGen] reflector JSON 解析失败，默认 continue")
        next_action = "continue"
        feedback = ""
    else:
        next_action = result.get("next_action", "continue").strip().lower()
        feedback = result.get("feedback", "")
        reason = result.get("reason", "")
        print(f"🤔 [CodeGen] 反思: {next_action}（{reason}）")

    # 记录反思
    reflection_text = f"步骤{current_step.get('step_id', '?')}: {next_action} - {feedback or reason}"
    reflections.append(reflection_text)

    # 根据反思决定下一步，设置 _route_signal 供路由函数使用
    if next_action == "done":
        return {**state, "reflections": reflections, "done": True, "_route_signal": "summarize"}

    elif next_action == "continue":
        # 前进到下一步
        return {
            **state,
            "reflections": reflections,
            "current_step_idx": idx + 1,
            "retry_count": 0,
            "_route_signal": "executor",
        }

    elif next_action == "retry":
        # 重试当前步（不前进），检查重试次数
        retry_count = state.get("retry_count", 0) + 1
        if retry_count >= MAX_RETRY_COUNT:
            print(f"⚠️ [CodeGen] 单步重试达上限 {MAX_RETRY_COUNT}，转为 replan")
            # 超限转为 replan
            plan_revision_count = state.get("plan_revision_count", 0) + 1
            if plan_revision_count >= MAX_REPLAN_COUNT:
                print(f"⚠️ [CodeGen] 重规划也达上限，强制完成走总结")
                return {**state, "reflections": reflections, "done": True, "_route_signal": "summarize"}
            return {
                **state,
                "reflections": reflections,
                "retry_count": 0,
                "plan_revision_count": plan_revision_count,
                "_route_signal": "planner",
            }
        # 未超限，重试当前步
        print(f"🔄 [CodeGen] 重试当前步（第 {retry_count} 次）")
        return {
            **state,
            "reflections": reflections,
            "retry_count": retry_count,
            "_route_signal": "executor",
        }

    elif next_action == "replan":
        # 重规划，检查重规划次数
        plan_revision_count = state.get("plan_revision_count", 0) + 1
        if plan_revision_count >= MAX_REPLAN_COUNT:
            print(f"⚠️ [CodeGen] 重规划达上限 {MAX_REPLAN_COUNT}，强制完成走总结")
            return {**state, "reflections": reflections, "done": True, "_route_signal": "summarize"}
        print(f"🔄 [CodeGen] 触发重规划（第 {plan_revision_count} 次）")
        return {
            **state,
            "reflections": reflections,
            "plan_revision_count": plan_revision_count,
            "retry_count": 0,
            "_route_signal": "planner",
        }

    else:
        # 未知 action，默认 continue
        return {
            **state,
            "reflections": reflections,
            "current_step_idx": idx + 1,
            "retry_count": 0,
            "_route_signal": "executor",
        }


def summarize_node(state: CodeState) -> CodeState:
    """总结节点：基于执行记录和反思生成最终修改说明。

    自动识别 Diff 预览场景：
      - 若 pending_modifications 非空 → 提示用户在前端确认
      - 否则按正常流程总结（分析/修改/无操作）
    """
    question = state["question"]
    history = state.get("history", [])
    executed = state.get("executed_steps", [])
    reflections = state.get("reflections", [])

    # 构建 pending_status 文本，让 LLM 明确当前是否处于待确认状态
    pending = state.get("pending_modifications", [])
    if pending:
        files = "、".join(sorted({m.get("filepath", "?") for m in pending}))
        pending_status = (
            f"有 {len(pending)} 个待确认修改，涉及文件: {files}（Diff 预览已生成，"
            f"文件尚未实际写入，等待用户在前端点击确认或撤销）"
        )
    else:
        pending_status = "无待确认修改（所有操作已实际执行或为只读分析）"

    prompt = CODE_ANSWER_PROMPT.format(
        executed_steps=_format_executed_steps(executed),
        reflections=_format_reflections(reflections),
        question=question,
        pending_status=pending_status,
    )
    if history:
        prompt = inject_history_into_prompt(prompt, history)

    resp = invoke_with_system(prompt)
    answer = resp.content.strip() if resp and resp.content else "（未能生成总结）"

    print(f"✅ [CodeGen] 总结生成完成（{len(executed)} 步执行，{len(reflections)} 条反思，"
          f"pending={len(pending)}）")
    return {**state, "final_answer": answer}


# ===================== 路由函数 =====================

def _route_after_planner(state: CodeState) -> str:
    """planner 后：有计划去 executor，空计划去 summarize。"""
    if state.get("done", False):
        return "summarize"
    plan = state.get("plan", [])
    if not plan:
        return "summarize"
    return "executor"


def _route_after_executor(state: CodeState) -> str:
    """executor 后：去 reflector 评估。"""
    if state.get("done", False):
        return "summarize"
    # 总步数超限兜底
    if state.get("total_steps", 0) >= MAX_CODE_ROUNDS:
        print(f"⚠️ [CodeGen] 总步数达上限 {MAX_CODE_ROUNDS}，走总结")
        return "summarize"
    return "reflector"


def _route_after_reflector(state: CodeState) -> str:
    """reflector 后的路由：基于 _route_signal 字段。

    reflector_node 设置 _route_signal:
    - "executor" → 继续执行（continue 或 retry）
    - "planner"  → 重规划
    - "summarize" → 完成
    """
    if state.get("done", False):
        return "summarize"
    if state.get("total_steps", 0) >= MAX_CODE_ROUNDS:
        return "summarize"

    signal = state.get("_route_signal", "executor")
    return signal


# ===================== 状态机构建 =====================

# LangGraph 原生 checkpointer：持久化状态，支持 interrupt_before 暂停/恢复
# 每个 thread_id 对应一个独立的状态机执行流（用 task_id 隔离）
_CHECKPOINTER = MemorySaver()

# interrupt_before 配置：在 executor 节点前自动暂停
#   - executor 是循环执行节点，每次进入前都停一下
#   - CodeGenSkill.execute 在外层循环驱动，检查暂停/取消信号后 resume
#   - 首次 invoke 会执行 planner 后停在 executor 前；后续 invoke(None) 推进到下一个 executor 前
_INTERRUPT_BEFORE = ["executor"]


def _build_code_graph():
    """构建 LangGraph 状态机并编译。

    拓扑：
        START → planner → executor → reflector → executor → ... → summarize → END
                        ↑                ↓
                        └←←← replanner ←┘（反思判定 replan）
                                           ↓
                                      executor（反思判定 retry）

    四重保护：
        1. MAX_CODE_ROUNDS：总执行步数上限
        2. MAX_REPLAN_COUNT：重规划次数上限
        3. MAX_RETRY_COUNT：单步重试次数上限
        4. JSON 解析失败 → 兜底 summarize

    暂停机制（LangGraph 原生 interrupt_before）：
        - 在 executor 节点前自动暂停，状态持久化到 _CHECKPOINTER
        - CodeGenSkill.execute 外层循环驱动 invoke，检查 TaskControl 信号
        - 取消：不调用 resume，直接退出循环
        - 暂停：阻塞等待 resume 信号，恢复后继续推进
    """
    graph = StateGraph(CodeState)

    graph.add_node("planner", planner_node)
    graph.add_node("executor", executor_node)
    graph.add_node("reflector", reflector_node)
    graph.add_node("summarize", summarize_node)

    graph.add_edge(START, "planner")

    # planner → executor（有计划）或 summarize（空计划/失败）
    graph.add_conditional_edges(
        "planner",
        _route_after_planner,
        {"executor": "executor", "summarize": "summarize"},
    )

    # executor → reflector（正常）或 summarize（done/超限）
    graph.add_conditional_edges(
        "executor",
        _route_after_executor,
        {"reflector": "reflector", "summarize": "summarize"},
    )

    # reflector → executor（continue/retry）或 planner（replan）或 summarize（done/超限）
    graph.add_conditional_edges(
        "reflector",
        _route_after_reflector,
        {"executor": "executor", "planner": "planner", "summarize": "summarize"},
    )

    graph.add_edge("summarize", END)

    return graph.compile(
        checkpointer=_CHECKPOINTER,
        interrupt_before=_INTERRUPT_BEFORE,
    )


# 模块级单例
_CODE_GRAPH = None


def get_code_graph():
    """获取编译后的 LangGraph 单例。"""
    global _CODE_GRAPH
    if _CODE_GRAPH is None:
        _CODE_GRAPH = _build_code_graph()
    return _CODE_GRAPH


# ===================== Skill 实现 =====================

class CodeGenSkill(BaseSkill):
    """代码生成/修改 Skill（Plan-and-Execute + Reflexion）。

    LLM 先生成完整计划，再逐步执行 + 反思。
    每次修改前自动快照备份，支持后续撤销。
    """

    name: str = "code_gen"
    description: str = (
        "代码生成/修改技能，基于 Plan-and-Execute + Reflexion 架构。"
        "先规划完整步骤，再逐步执行 + 反思评估，失败可重试或重规划。"
        "读取本地代码文件、按需求修改、生成修改总结。"
        "支持 edit_file 局部替换和 write_file 全量覆写，修改前自动备份。"
    )

    def can_handle(self, question: str, ctx: Optional[SkillContext] = None) -> float:
        """代码类问题返回中等置信度 0.6。"""
        if not ENABLE_CODE_AGENT:
            return 0.0
        return 0.6

    def execute(
        self,
        question: str,
        ctx: Optional[SkillContext] = None,
    ) -> tuple[str, list[Document]]:
        """运行 Plan-and-Execute + Reflexion 状态机。

        暂停/取消由 LangGraph 原生 interrupt_before 驱动：
          1. 首次 invoke 提交初始状态 → planner 跑完，停在 executor 前
          2. 循环：检查 TaskControl → 暂停则等待，取消则退出 → invoke(None) 推进到下一个 interrupt
          3. 状态机走完 summarize → END 后，state.next 为空，循环退出
        """
        if not ENABLE_CODE_AGENT:
            return "代码模块未启用（ENABLE_CODE_AGENT=False）", []

        ctx = ctx or SkillContext(question=question)
        history = ctx.history or []
        session_id = ctx.session_id or "default"
        task_control = ctx.task_control

        # thread_id：用 task_id 隔离不同任务的状态机执行流
        # task_control 必传时用其 task_id，否则降级为 session_id（仅用于一次性同步调用，无暂停能力）
        if task_control is not None:
            thread_id = task_control.task_id
        else:
            thread_id = f"codegen-{session_id}"

        print(f"\n===== [CodeGenSkill] 启动 Plan-and-Execute + Reflexion =====")
        print(f"📝 用户需求: {question}")
        print(f"⚙️ 最大步数: {MAX_CODE_ROUNDS} | 最大重规划: {MAX_REPLAN_COUNT} | 最大重试: {MAX_RETRY_COUNT}")
        print(f"🧵 thread_id: {thread_id}")

        # 初始状态（task_control 不放 state，避免 checkpointer 序列化复杂对象）
        initial_state: CodeState = {
            "question": question,
            "history": history,
            "plan": [],
            "current_step_idx": 0,
            "executed_steps": [],
            "reflections": [],
            "plan_revision_count": 0,
            "retry_count": 0,
            "total_steps": 0,
            "final_answer": "",
            "done": False,
            "_route_signal": "",
            "session_id": session_id,
            "pending_modifications": [],
        }

        graph = get_code_graph()
        config = {"configurable": {"thread_id": thread_id}}

        # ---- 第 1 步：首次 invoke，跑到第一个 interrupt（executor 前）----
        # 此时 planner 已执行完，state.next == ("executor",)
        graph.invoke(initial_state, config)

        # ---- 第 2 步：循环驱动，每次推进到下一个 interrupt ----
        while True:
            state_snapshot = graph.get_state(config)

            # 2.1 完成判断：next 为空表示状态机已走到 END
            if not state_snapshot.next:
                break

            # 2.2 取消信号检查：用户取消则直接退出，不再 resume
            if task_control is not None and task_control.is_cancelled:
                print(f"🛑 [CodeGen] 检测到取消信号，终止状态机（thread_id={thread_id}）")
                break

            # 2.3 暂停信号检查：阻塞等待恢复（基于 Event.wait，不自旋）
            if task_control is not None and task_control.is_paused:
                print(f"⏸️ [CodeGen] 状态机在 {state_snapshot.next} 前暂停，等待恢复")
                task_control.wait_resume()
                # 恢复后再次检查取消（用户可能在暂停期间点了取消）
                if task_control.is_cancelled:
                    print(f"🛑 [CodeGen] 暂停期间收到取消信号，终止状态机")
                    break

            # 2.4 更新进度信息（供前端轮询）
            if task_control is not None:
                next_node = state_snapshot.next[0] if state_snapshot.next else "?"
                idx = state_snapshot.values.get("current_step_idx", 0)
                total = len(state_snapshot.values.get("plan", []))
                task_control.update_step(
                    f"节点: {next_node}（步骤 {idx + 1}/{total}）",
                    progress=idx / total if total else 0.0,
                )

            # 2.5 推进到下一个 interrupt（或 END）
            #     invoke(None, config) 从 checkpoint 恢复，执行下一个节点
            #     遇到 interrupt_before 会再次停下；走到 END 则 next 变空
            graph.invoke(None, config)

        # ---- 第 3 步：获取最终状态 ----
        final_state = graph.get_state(config).values
        answer = final_state.get("final_answer", "").strip()

        # 取消时可能没走到 summarize，给个兜底提示
        if not answer:
            if task_control is not None and task_control.is_cancelled:
                answer = "（任务已取消，未生成总结）"
            else:
                answer = "（代码修改流程未产生有效总结）"

        print(f"===== [CodeGenSkill] 状态机执行完成 =====\n")
        return answer, []
