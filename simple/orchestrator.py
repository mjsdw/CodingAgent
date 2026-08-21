# orchestrator.py
# ========== 编排器：Router + Skill 架构的统一入口 ==========
#
# 职责：整合 Router（路由） + Skill（执行） + Memory（记忆），替代原 main.py 的模式切换。
#
# 流程：
#   1. 组装 SkillContext（注入历史记忆）
#   2. HybridRouter 选 Skill
#   3. Skill.execute 处理问题
#   4. 保存本轮对话到记忆
#
# 设计原则：
#   1. 接口与原 query_agent / query_rag_agent 完全一致：(answer, sources)
#      → main.py / api/app.py 切换零改动
#   2. 记忆管理集中在 Orchestrator，Skill 不关心记忆读写
#   3. Orchestrator 无状态：每次 query 调用独立，不持有会话数据

from langchain_core.documents import Document

from core.memory import get_memory_store
from config import ENABLE_MEMORY, ENABLE_CODE_AGENT, UPLOAD_DIR
from skills.base import SkillContext
from router.hybrid_router import HybridRouter


def _load_session_uploads(session_id: str) -> list[dict]:
    """加载指定会话的上传文件清单。

    返回：[{filename, filepath, size}, ...]，按文件名排序。
    仅在 ENABLE_CODE_AGENT=True 且目录存在时扫描，否则返回空列表。
    """
    if not (ENABLE_CODE_AGENT and session_id and UPLOAD_DIR):
        return []
    try:
        from pathlib import Path
        import re
        # session_id 规范化（与 app.py 保持一致，防止路径穿越）
        safe_sid = re.sub(r"[^a-zA-Z0-9._-]", "_", session_id)
        session_dir = Path(UPLOAD_DIR).resolve() / safe_sid
        if not session_dir.exists() or not session_dir.is_dir():
            return []
        files = []
        for f in session_dir.iterdir():
            if not f.is_file():
                continue
            try:
                stat = f.stat()
                files.append({
                    "filename": f.name,
                    "filepath": str(f.resolve()),
                    "size": stat.st_size,
                })
            except Exception:
                continue
        files.sort(key=lambda x: x["filename"])
        return files
    except Exception:
        return []


def _build_upload_context(uploads: list[dict]) -> str:
    """根据上传文件清单构建上下文注入文本。

    若无上传文件返回空字符串。
    """
    if not uploads:
        return ""
    lines = ["【会话已上传代码文件】（Agent 可直接 read_file/grep_code/edit_file 操作）"]
    for f in uploads:
        lines.append(f"- {f['filepath']}")
    return "\n".join(lines) + "\n\n"


# """编排器：Router + Skill + Memory 的统一入口。
#
#     使用方式：
#         orch = Orchestrator()
#         answer, sources = orch.query("索引失效的场景", session_id="cli-default")
#     """
class Orchestrator:

    def __init__(self):
        self.router = HybridRouter()

    def query(self, question: str, session_id: str = None, task_control=None,
              pre_classified_skill=None) -> tuple[str, list[Document]]:
        # """处理用户问题，返回 (答案, 引用片段)。
        #
        # :param question: 用户原始问题
        # :param session_id: 会话 ID；传入且 ENABLE_MEMORY=True 时自动注入历史 + 保存本轮
        # :param task_control: 任务控制对象（可选），传入则支持暂停/继续/取消
        # :return: (answer, sources)
        #          - answer: 最终给用户的答案文本
        #          - sources: 引用片段列表（无检索的 Skill 返回空列表）
        # """
        # ---- 1. 组装 SkillContext（注入历史记忆 + 上传文件清单 + 任务控制）----
        history = []
        if ENABLE_MEMORY and session_id:
            store = get_memory_store()
            history = store.get_history(session_id)
            if history:
                print(f"💬 [记忆] 注入 {len(history)} 条历史消息（会话：{session_id}）")

        # 方案A：会话级文件清单注入到 question 前部，让 Agent 知道有哪些文件可操作
        upload_ctx = ""
        if ENABLE_CODE_AGENT and session_id:
            uploads = _load_session_uploads(session_id)
            if uploads:
                upload_ctx = _build_upload_context(uploads)
                print(f"📎 [上传] 注入 {len(uploads)} 个上传文件路径（会话：{session_id}）")

        # 实际交给 Skill 的 question（含上传文件清单前缀）
        effective_question = upload_ctx + question if upload_ctx else question

        ctx = SkillContext(
            question=effective_question,
            session_id=session_id,
            history=history,
            task_control=task_control,
        )

        # ---- 2. Router 选 Skill ----
        print(f"\n🧠 [Orchestrator] 开始处理问题：{question}")
        if pre_classified_skill is not None:
            skill = pre_classified_skill
        else:
            skill = self.router.classify(effective_question, ctx)

        # 任务控制检查点：Router 之后、Skill.execute 之前
        # 用户在 Router 期间点暂停 → 阻塞等待，Skill 不会被执行
        if task_control is not None:
            task_control.check_point("before_skill_execute")

        # ---- 3. Skill 执行 ----
        answer, sources = skill.execute(effective_question, ctx)

        # ---- 4. 保存本轮对话到记忆 ----
        if ENABLE_MEMORY and session_id:
            store = get_memory_store()
            # 保存原始用户问题（不含上传文件清单前缀），避免历史污染
            store.add_turn(session_id, question, answer)

        return answer, sources
