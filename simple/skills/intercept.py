# skills/intercept.py
# ========== 拦截 Skill：未打开项目时拦截代码类问题 ==========
#
# 职责：当用户未打开项目且未上传代码文件时，拦截代码类问题，
#       避免无效调用 CodeGenSkill 的多轮 LLM（planner/executor/reflector）。
#
# 触发条件（由 HybridRouter 在路由后检查）：
#   1. Router 判定问题应路由到 CodeGenSkill（规则命中或 LLM 判定）
#   2. 但 has_session_files(session_id) == False（无打开项目 + 无上传文件）
#   → 返回 InterceptSkill，输出引导提示
#
# 设计原则：
#   - 永不主动拦截：can_handle 返回 0.0，只由 HybridRouter 显式实例化
#   - 零 LLM 调用：execute 直接返回固定文本，不调任何模型
#   - 引导用户操作：提示用户"打开项目"或"上传文件"后再提问

from typing import Optional

from langchain_core.documents import Document

from skills.base import BaseSkill, SkillContext


# 拦截消息模板（前端可检测 [INTERCEPT] 标记渲染特殊 UI）
_INTERCEPT_MESSAGE = """[INTERCEPT:need_project]

⚠️ 请先打开项目或上传代码文件

检测到您的请求需要操作代码文件，但当前未打开任何项目，也未上传代码文件。

您可以通过以下方式操作：
1. 点击左侧 **📁 打开项目** 按钮，选择要操作的代码目录
2. 或点击 **📤 上传文件** 按钮，上传要操作的代码文件

完成后再重新提问即可。"""


class InterceptSkill(BaseSkill):
    """拦截 Skill：未打开项目时拦截代码类问题，引导用户先打开项目。

    由 HybridRouter 在路由判定为 code_gen 但无可用文件时显式实例化，
    不会通过 can_handle 自动选中。
    """

    name: str = "intercept"
    description: str = (
        "拦截技能：用户未打开项目且未上传文件时，拦截代码类问题，"
        "引导用户先打开项目或上传文件。零 LLM 调用。"
    )

    def can_handle(self, question: str, ctx: Optional[SkillContext] = None) -> float:
        """永不主动匹配：只在 HybridRouter 中显式实例化。"""
        return 0.0

    def execute(
        self,
        question: str,
        ctx: Optional[SkillContext] = None,
    ) -> tuple[str, list[Document]]:
        """返回拦截提示，不调用任何 LLM。"""
        print(f"🚫 [InterceptSkill] 拦截代码类问题（未打开项目/未上传文件）")
        print(f"📝 原始问题: {question[:100]}")
        return _INTERCEPT_MESSAGE, []
