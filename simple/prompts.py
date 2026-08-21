# prompts.py
# 项目所有 Prompt 模板的统一入口（原 prompts/ 目录合并而来）
#
# 模块划分：
#   - 通用检索/闲聊 Prompt（原 prompt_config.py）
#   - 代码模块 Prompt（原 code_prompt.py）：Plan-and-Execute + Reflexion 架构
#
# 代码模块架构说明：
#   1. planner  ：生成完整计划（JSON 步骤列表）
#   2. executor ：按计划执行单步（read/edit/write）
#   3. reflector：反思执行结果，决定 continue/retry/replan/done
#   4. summarize：最终总结
#
# 与原 ReAct 的差异：
#   - ReAct 每轮临时决策，无全局规划
#   - Plan-and-Execute 先规划再执行，减少 LLM 调用次数
#   - Reflexion 每步反思，失败可带反馈重规划，提升闭环率


# ==========================================================
# 一、通用检索/闲聊 Prompt（原 prompt_config.py）
# ==========================================================

# ========== ReAct 推理提示词 ==========
# LLM 自主决定：用哪个工具检索（本地/联网），还是直接作答
REACT_PROMPT_TEMPLATE = """任务：通过工具收集资料，回答用户问题。

可用工具：
- search：检索本地知识库（已入库的技术文档、资料）
- web_search：联网搜索互联网（本地知识库没有或不完整时使用）

每轮你必须输出 JSON，格式严格如下，不要输出任何其他内容：
{{
    "thought": "分析当前已有资料，决定下一步行动",
    "action": "search" / "web_search" / "answer",
    "action_input": "search或web_search时填写检索词；answer时填写最终答案"
}}

规则：
- 优先用 search 检索本地知识库
- 本地搜不到或资料不足时，用 web_search 搜索互联网
- action="answer"：资料已充足，基于已检索到的资料直接生成最终答案
- 每次检索词应与之前不同，从不同角度、不同关键词切入
- 严禁编造资料中不存在的信息
- 最多可检索 {max_rounds} 轮，若资料仍不足应基于现有资料尽力作答

已有检索记录：
{observations}

用户问题：{question}
"""

# ========== 兜底答案生成Prompt（ReAct 达上限时使用） ==========
ANSWER_PROMPT_TEMPLATE = """
任务：基于下方检索到的上下文资料回答用户问题。

重要规则：
1. 下方上下文是经过多轮检索筛选后的资料，请优先据此作答，不要轻易判定为无关；
2. 只要上下文中存在与问题相关的内容，就必须综合这些资料给出完整答案；
3. 仅当上下文确实完全不涉及用户问题时，才输出：【知识库中未查询到相关信息】；
4. 严禁编造上下文中不存在的信息。

上下文：
{context}
用户问题：{question}
答案：
"""

# ========== 闲聊Prompt ==========
CHAT_PROMPT_TPL = """友好简洁地回应用户，不要引入知识库相关内容。
用户：{question}
回答："""

# ========== Query 改写Prompt ==========
# 将用户口语化问题改写为检索友好的简洁表述，用于向量检索召回
QUERY_REWRITE_PROMPT = """任务：将用户口语化问题改写为检索友好的简洁表述，用于向量检索召回。

改写规则：
1. 保留核心实体、关键词、专业术语，不丢失关键信息；
2. 去除口语化表达（如"请问"、"我想了解"、"怎么样"、"有什么"等）；
3. 补全省略的主语/宾语，使问题语义自足；
4. 不要改变问题原意，不要回答问题，不要扩展无关内容；
5. 只输出改写后的检索词，不要任何解释、不要引号、不要标点符号结尾。

用户原始问题：{question}
改写后的检索词："""


# ==========================================================
# 二、代码模块 Prompt（原 code_prompt.py）
# ==========================================================

# ========== 1. 计划生成 Prompt ==========
# LLM 看到用户需求，生成结构化执行计划
# 占位符：{available_paths} / {question} / {max_steps}
PLAN_PROMPT = """任务：分析用户需求，制定分步执行计划。

{available_paths}

需求：{question}

你需要输出一个 JSON 执行计划，包含有序的步骤列表。每个步骤是一个独立的操作单元。

可用操作类型及参数 schema（严格按此参数名填写）：
- read_file：读取文件内容
  参数：{{"filepath": "文件绝对路径"}}
- edit_file：修改文件片段（执行时基于 read_file 结果动态生成 old_string/new_string，规划时留空）
  参数：{{"filepath": "文件绝对路径", "old_string": "", "new_string": ""}}
  说明：规划时 old_string/new_string 必须留空字符串，description 中写清楚修改意图即可
- write_file：覆写文件（执行时基于 read_file 结果动态生成 content，规划时留空）
  参数：{{"filepath": "文件绝对路径", "content": ""}}
  说明：规划时 content 必须留空字符串，description 中写清楚要写入什么内容
- list_dir：列出目录内容（仅一层，不递归）
  参数：{{"dirpath": "目录绝对路径"}}（注意是 dirpath，不是 filepath）
- grep_code：在代码文件中搜索匹配行
  参数：{{"pattern": "正则表达式或关键字", "dirpath": "可选，限定搜索目录"}}
- analyze：基于已读取的代码内容进行分析并输出结论
  参数：{{"focus": "分析重点"}}
- finish：任务完成
  参数：{{}}

任务类型识别（关键）：
- 修改类任务（写代码/改bug/重构/优化）→ 用 list_dir/grep_code 定位 → read_file → edit_file/write_file → finish
- 分析类任务（检查bug/代码审查/解释逻辑/评估质量）→ 用 list_dir/grep_code 定位 → read_file → analyze → finish
- 分析类任务严禁直接 read_file 后 finish，必须经过 analyze 步骤输出分析结论

analyze 动作说明：
- focus：分析重点（如"bug检查"、"代码质量评估"、"逻辑解释"、"性能分析"）
- 执行时会自动汇总此前所有 read_file 读取到的代码内容，交给 LLM 按 focus 分析
- analyze 的结果即为分析结论，会作为最终总结的核心依据

list_dir / grep_code 使用建议：
- 不确定文件路径时，先用 list_dir 列出目录结构再决策
- 需要定位函数定义/调用位置时，用 grep_code 搜索函数名或关键字
- grep_code 的 pattern 支持正则，dirpath 为空时搜索整个工作区

输出格式严格如下，不要输出任何其他内容：
{{
    "plan_analysis": "简要分析用户需求和执行策略（1-2句，需指明是修改类还是分析类任务）",
    "steps": [
        {{
            "step_id": 1,
            "action": "read_file",
            "action_input": {{
                "filepath": "文件绝对路径"
            }},
            "description": "读取目标文件了解结构"
        }},
        {{
            "step_id": 2,
            "action": "analyze",
            "action_input": {{
                "focus": "分析重点（如bug检查/代码质量/逻辑解释）"
            }},
            "description": "分析代码是否存在bug"
        }},
        {{
            "step_id": 3,
            "action": "finish",
            "action_input": {{}},
            "description": "任务完成"
        }}
    ]
}}

规划规则：
- ★★ 最优先规则：先阅读上方的【当前会话可用路径清单】。如果用户提到的文件名/路径在清单中直接出现，必须直接使用清单中给出的**完整绝对路径**，严禁把文件名拼接到错误的 workspace 根目录下。
- 首步通常是 read_file 了解当前代码结构
- 分析类任务：read_file 之后必须安排 analyze 步骤，不能直接 finish
- 修改类任务：edit_file 前必须先 read_file 同一文件（executor 执行 edit_file 时会基于已读内容动态生成 old_string/new_string）
- edit_file 的 description 必须写清楚修改意图（要改什么、为什么改），executor 会基于此意图和已读内容自动生成精确的 old_string/new_string
- 严禁在规划阶段填写 edit_file 的 old_string/new_string 具体值（因为还没读过文件，填了也是错的）
- 严禁在规划阶段填写 write_file 的 content 具体值（同上）
- 最多 {max_steps} 个步骤，超出时应在最后用 finish 收尾
- filepath/dirpath 必须使用绝对路径，且必须在【当前会话可用路径清单】范围内
- 严禁规划与用户需求无关的操作
- 如需操作多个文件，按文件分组安排步骤
"""


# ========== 1b. 修改参数生成 Prompt ==========
# executor 执行 edit_file/write_file 时，若规划阶段未填具体参数，
# 用此 prompt 基于"已读文件内容 + 修改意图"动态生成 old_string/new_string 或 content
# 占位符：{question} / {intent} / {filepath} / {file_content}
GENERATE_EDIT_PROMPT = """任务：基于已读取的文件内容，生成代码修改的具体参数。

用户需求（多步骤任务的整体背景，仅供参考）：{question}
修改意图（本步骤唯一要实现的目标）：{intent}
目标文件：{filepath}

已读取的文件内容：
{file_content}

请生成修改参数，严格输出 JSON（不要输出其他内容）：
{{
    "old_string": "要从文件中替换的原始代码片段（必须与上方文件内容完全一致且唯一匹配，包括缩进和空格）",
    "new_string": "替换后的新代码；若为删除操作，输出空字符串 \"\"",
    "explanation": "为什么这样修改（1句话）"
}}

生成规则：
- 【作用域，最高优先级】用户需求是多步骤任务的整体描述，可能涉及多个文件的多个操作；本步骤只针对目标文件实现"修改意图"描述的那一个操作。需求中涉及其他文件或其他操作的内容与本步骤无关，一律忽略，绝不因此报错
- 【禁止误判】不要因为用户需求中提到的内容在目标文件中不存在而认为无法执行——能否执行只取决于"修改意图"本身在本文件中是否可实现
- old_string 必须是上方文件内容中真实存在且唯一的片段（包括缩进、空格、换行）
- 如果 old_string 在文件中出现多次，需扩展上下文使其唯一
- new_string 是替换后的新代码，应精确实现修改意图
- 删除操作（如删除注释、删除代码行）时 new_string 用空字符串 "" 表示
- 在某处插入内容时：old_string 取插入位置的锚点片段，new_string = 插入内容 + 锚点片段
- 修改应最小化，只改必要的部分，不要重写整个文件
- 如果修改意图确实无法在本文件实现，输出 {{"error": "原因说明"}}
"""


# ========== 1c. 写入内容生成 Prompt ==========
# executor 执行 write_file 时，若规划阶段未填 content，
# 用此 prompt 基于"修改意图"动态生成完整文件内容
# 占位符：{question} / {intent} / {filepath} / {existing_content}
GENERATE_WRITE_PROMPT = """任务：基于修改意图，生成要写入文件的完整内容。

用户需求（多步骤任务的整体背景，仅供参考）：{question}
修改意图（本步骤唯一要实现的目标）：{intent}
目标文件：{filepath}

文件当前内容（若为空表示新建文件）：
{existing_content}

请生成要写入文件的完整内容，严格输出 JSON（不要输出其他内容）：
{{
    "content": "要写入文件的完整内容",
    "explanation": "为什么这样写（1句话）"
}}

生成规则：
- 【作用域，最高优先级】用户需求是多步骤任务的整体描述，可能涉及多个文件的多个操作；本步骤只针对目标文件实现"修改意图"描述的那一个操作。需求中涉及其他文件或其他操作的内容与本步骤无关，一律忽略，绝不因此报错
- content 是要写入文件的完整内容（不是片段，是整个文件）
- 如果是新建文件，生成完整的文件内容
- 如果是覆写已有文件，基于修改意图生成新的完整内容
- 内容应精确实现修改意图
- 如果修改意图确实无法实现，输出 {{"error": "原因说明"}}
"""


# ========== 2. 重规划 Prompt ==========
# 反思失败后，带反馈重新生成计划
# 占位符：{available_paths} / {question} / {max_steps} / {executed_steps} / {reflections}
REPLAN_PROMPT = """任务：之前的执行计划遇到问题，根据反思反馈重新制定计划。

{available_paths}

用户需求：{question}

已执行步骤及结果：
{executed_steps}

反思反馈：
{reflections}

请基于已有结果和反馈，制定新的执行计划。可以保留已成功的步骤结果，调整失败的步骤。

可用操作类型及参数 schema（严格按此参数名填写）：
- read_file：{{"filepath": "文件绝对路径"}}
- edit_file：{{"filepath": "文件绝对路径", "old_string": "", "new_string": ""}}（规划时留空，description 写修改意图）
- write_file：{{"filepath": "文件绝对路径", "content": ""}}（规划时留空，description 写要写入什么）
- list_dir：{{"dirpath": "目录绝对路径"}}（注意是 dirpath，不是 filepath）
- grep_code：{{"pattern": "正则或关键字", "dirpath": "可选，限定搜索目录"}}
- analyze：{{"focus": "分析重点"}}
- finish：{{}}

输出格式与初始计划相同：
{{
    "plan_analysis": "基于反思的新策略（1-2句）",
    "steps": [
        {{
            "step_id": 1,
            "action": "read_file / edit_file / write_file / finish",
            "action_input": {{}},
            "description": "步骤说明"
        }}
    ]
}}

规则：
- ★★ 最优先规则：先阅读上方的【当前会话可用路径清单】。如果用户提到的文件名/路径在清单中直接出现，必须直接使用清单中给出的完整绝对路径，严禁把文件名拼到错误的 workspace 根目录下。
- 不要重复已成功的步骤
- 基于反思反馈调整策略
- edit_file 前必须先 read_file 同一文件
- 严禁在规划阶段填写 edit_file 的 old_string/new_string 具体值（留空，executor 动态生成）
- 严禁在规划阶段填写 write_file 的 content 具体值（留空，executor 动态生成）
- 最多 {max_steps} 个步骤
- filepath/dirpath 必须使用绝对路径，且必须在【当前会话可用路径清单】范围内
"""


# ========== 3. 反思 Prompt ==========
# 每步执行后，LLM 评估结果并决定下一步
# 占位符：{question} / {current_step} / {execution_result} / {plan_progress}
REFLECT_PROMPT = """任务：评估当前步骤的执行结果，决定下一步行动。

用户需求：{question}

当前步骤：
{current_step}

执行结果：
{execution_result}

整体进度：
{plan_progress}

请评估当前步骤是否成功，并决定下一步：
- continue：当前步骤成功，继续执行下一步
- retry：当前步骤失败但计划正确，重试当前步骤（修正参数）
- replan：当前计划有误，需要重新规划
- done：所有步骤完成，任务达成

严格只输出 JSON：
{{
    "evaluation": "当前步骤是否成功（成功/失败）",
    "reason": "分析执行结果，说明判定原因",
    "feedback": "如果是 retry/replan，给出具体修正建议（done/continue 时为空）",
    "next_action": "continue" / "retry" / "replan" / "done"
}}

评估规则：
- read_file 返回文件内容即为成功
- edit_file 返回"已生成修改预览"即为成功（preview 模式，文件尚未实际写入），返回"错误"即为失败
- write_file 返回"已生成...预览"即为成功（preview 模式），返回"错误"即为失败
- analyze 返回分析结论即为成功，返回"错误"即为失败
- 如果执行结果包含"错误"，当前步骤判定为失败
- 如果修改结果不符合用户需求，应 replan
- 关键缺陷识别：若用户需求是分析类（检查bug/审查/解释），但计划在 read_file 后直接 finish 而没有 analyze 步骤，必须 replan 补充 analyze 步骤
- 注意：edit_file/write_file 成功后修改只是暂存（pending），计划剩余步骤仍会继续执行；若计划中该文件后续还有依赖实际写入的步骤，判定为 continue 即可
"""


# ========== 4. 最终总结 Prompt ==========
# 所有步骤完成后，生成用户可读的总结
# 自动适配三类场景：
#   A. 分析类任务 → 输出分析结论
#   B. 修改类任务 + Diff 预览模式 → 输出"等待用户确认"提示（文件尚未实际写入）
#   C. 修改类任务 + 已实际执行 → 输出修改说明
# 占位符：{executed_steps} / {reflections} / {question} / {pending_status}
CODE_ANSWER_PROMPT = """任务：根据执行记录和反思，生成用户可读的总结。

执行记录：
{executed_steps}

反思记录：
{reflections}

用户原始需求：{question}

待确认修改状态：{pending_status}

总结生成规则：
1. 若执行记录中包含 analyze 步骤（分析类任务）：
   - 直接以 analyze 步骤的执行结果作为核心回答
   - 简要说明分析了哪些文件、分析重点是什么
   - 如有失败的操作，说明原因和最终处理方式
2. 若"待确认修改状态"显示有 pending 修改（Diff 预览模式，文件尚未实际写入）：
   - 明确告知用户：已生成修改预览，正在等待确认
   - 列出待确认的修改文件清单（基于执行记录中 edit_file/write_file 的结果）
   - 简要说明每处修改的目的和预期影响
   - 提示用户：在前端 diff 预览面板点击"确认修改"应用修改，点击"撤销"重新输入需求
   - 严禁声称修改已应用或文件已更新（因为尚未真正写入）
3. 若执行记录中包含 edit_file/write_file 且"待确认修改状态"为无 pending（修改已实际执行）：
   - 说明修改了哪个文件
   - 具体做了什么修改（新增/删除/替换了哪些代码）
   - 修改前后的关键差异
   - 如有失败的操作，说明原因和最终处理方式
4. 若执行记录为空或仅有 read_file（无修改也无分析）：
   - 说明未执行任何修改或分析操作
   - 简要说明原因（如计划失败、文件不存在等）

总结："""


# ========== 5. 代码分析 Prompt ==========
# analyze 动作专用：基于已读取的代码内容进行分析
# 占位符：{question} / {focus} / {code_context}
ANALYZE_PROMPT = """任务：基于已读取的代码内容，按照指定重点进行分析。

用户原始需求：{question}

分析重点：{focus}

已读取的代码内容：
{code_context}

分析要求：
1. 严格基于上方代码内容分析，不要编造代码中不存在的内容
2. 按分析重点（focus）组织输出结构
3. 如果是 bug 检查：列出发现的问题，说明问题位置（函数名/行号区间）、问题描述、可能影响、修复建议
4. 如果是代码质量评估：从可读性、可维护性、性能、安全性等维度评估，给出具体改进建议
5. 如果是逻辑解释：说明代码的整体结构、关键函数职责、数据流向、核心算法
6. 如果未发现任何问题，明确说明"未发现明显问题"并简要给出代码质量的正面评价
7. 用中文输出，结构清晰，便于用户理解

分析结论："""


# ========== 兼容旧版（KBSearchSkill 等仍用 ReAct）==========
# 保留 CODE_REACT_PROMPT 供其他模块兼容引用
CODE_REACT_PROMPT = """任务：根据用户需求，操作本地代码文件。

可用工具：
- read_file：读取代码文件内容（参数：filepath）
- edit_file：修改文件的指定部分（参数：filepath, old_string, new_string）
- write_file：覆写整个文件（参数：filepath, content）
- answer：任务完成，输出修改总结（参数：summary）

每轮你必须输出 JSON，格式严格如下，不要输出任何其他内容、不要使用 markdown 代码块：
{{
    "thought": "分析当前状态，决定下一步行动",
    "action": "read_file" / "edit_file" / "write_file" / "answer",
    "action_input": {{
        "filepath": "文件绝对路径（read_file/edit_file/write_file 时必填）",
        "old_string": "要替换的原始代码（edit_file 时必填，必须与文件内容完全一致）",
        "new_string": "替换后的新代码（edit_file 时必填）",
        "content": "完整的文件内容（write_file 时必填）",
        "summary": "任务总结（answer 时必填，描述做了什么修改）"
    }}
}}

操作规则：
- 首轮必须使用 read_file 读取用户提到的目标文件，了解当前代码结构
- 根据 read_file 返回结果和用户需求，使用 edit_file 进行精准修改
- edit_file 的 old_string 必须与文件内容完全一致且唯一匹配（包括缩进、空格、换行）
- 如果 old_string 在文件中出现多次，需扩展上下文使其唯一
- 修改完成后必须再次 read_file 验证修改结果是否符合预期
- 确认无误后使用 answer 输出修改总结
- 严禁修改与用户需求无关的代码
- filepath 必须使用绝对路径（如 d:/workspace/project/src/xxx.py，必须是工作区白名单内的真实路径）
- 如果工具返回错误信息（如"未找到"、"出现 N 次"），应在下一轮调整后重试
- 最多 {max_rounds} 轮操作，达上限应基于当前结果输出 answer

已执行的操作记录：
{observations}

用户需求：{question}
"""
