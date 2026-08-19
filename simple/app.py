# app.py
# ========== FastAPI Web 服务 ==========
#
# 启动方式：python main.py --web
# 或直接：uvicorn app:app --host 127.0.0.1 --port 8000
#
# 路由设计：
#   GET  /                  → 返回 static/index.html 问答页面
#   POST /api/chat          → 接收 {question: str}，返回 {answer, sources, mode, elapsed_ms}
#   GET  /api/health        → 健康检查，返回 {status, mode}
#   POST /api/code/undo     → 撤销最近一次代码修改，恢复到上一个快照
#   GET  /api/code/history  → 查询指定文件的修改历史列表
#
# 设计原则：
#   1. 完全复用 Orchestrator（Router + Skill + Memory），不重新实现业务逻辑
#   2. CLI 模式（main.py 不带 --web）保持不变，互不影响
#   3. 同步阻塞调用，uvicorn 单 Worker 足够个人/演示用
#   4. sources 字段把 Document 序列化为 dict，前端可直接渲染引用列表
#   5. 撤销/历史接口直接复用 tools.code_tool 的 undo_last / get_history

import time
from pathlib import Path
import re
from fastapi import FastAPI, UploadFile, File, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import (
    WEB_HOST, WEB_PORT, ENABLE_CODE_AGENT,
    UPLOAD_DIR, UPLOAD_MAX_FILE_SIZE, UPLOAD_MAX_SESSION_SIZE, UPLOAD_ALLOWED_EXTS,
    WORKSPACE_TREE_DEFAULT_DEPTH, WORKSPACE_TREE_MAX_DEPTH,
)
from orchestrator import Orchestrator
from tools.code_tool import (
    undo_last, get_history,
    add_session_workspace, remove_session_workspace, get_session_workspaces,
    list_tree_impl, read_workspace_file_impl, save_workspace_file_impl,
    get_pending_modifications, confirm_modifications, cancel_modifications,
)

# 全局 Orchestrator 单例（与 main.py 共享同一套 Router + Skill + Memory）
_orch = Orchestrator()


# ===================== FastAPI 应用 =====================
app = FastAPI(
    title="RAG 智能问答助手",
    description="基于 LangGraph ReAct 的知识库问答 Web 服务",
    version="1.0.0",
)

# 挂载静态文件目录（static/ 下的 index.html、css、js 都能直接访问）
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ===================== 请求/响应模型 =====================
class ChatRequest(BaseModel):
    """问答请求体。"""
    question: str
    session_id: str | None = None   # 会话 ID；为空时服务端自动生成


class SourceItem(BaseModel):
    """单条引用片段。"""
    page_content: str
    metadata: dict
    score: float | None = None   # 优先取 similarity_distance / rrf_score / bm25_score


class ChatResponse(BaseModel):
    """问答响应体。"""
    answer: str
    sources: list[SourceItem]
    mode: str                     # "Agent" 或 "Pipeline"
    elapsed_ms: int               # 端到端耗时（毫秒）
    session_id: str               # 本次使用的会话 ID（回传给前端，后续请求带上)


# ----- 代码撤销相关模型 -----

class CodeUndoRequest(BaseModel):
    """代码撤销请求体。"""
    filepath: str                 # 要撤销修改的文件绝对路径


class CodeUndoResponse(BaseModel):
    """代码撤销响应体。"""
    filepath: str
    snapshot_id: int               # 本次撤销的快照 ID（0 表示无历史可撤销）
    remaining_undos: int           # 剩余可撤销次数
    status: str                    # "undone" / "no_history" / "disabled" / "error"


class CodeHistoryItem(BaseModel):
    """单条修改历史记录。"""
    snapshot_id: int
    filename: str
    filepath: str
    timestamp: float
    action_desc: str


# ----- 上传文件相关模型 -----

class UploadResponse(BaseModel):
    """上传成功响应体。"""
    filename: str                     # 规范化后的文件名
    filepath: str                     # 服务器绝对路径
    size: int                         # 文件大小（字节）
    session_id: str                   # 所属会话 ID


class UploadItem(BaseModel):
    """单条已上传文件信息。"""
    filename: str
    filepath: str
    size: int
    upload_time: float


class UploadDeleteResponse(BaseModel):
    """删除上传文件响应体。"""
    filename: str
    session_id: str
    status: str                       # "deleted" / "not_found" / "error"


# ----- 工作区（打开项目）相关模型 -----

class WorkspaceOpenRequest(BaseModel):
    """打开项目请求体。"""
    session_id: str
    project_path: str                 # 项目根目录绝对路径


class WorkspaceOpenResponse(BaseModel):
    """打开项目响应体。"""
    session_id: str
    project_path: str                 # 规范化后的绝对路径
    project_name: str                 # 项目目录名
    status: str                       # "opened" / "already_open" / "error"
    current_projects: list[str]       # 当前会话已打开的所有项目路径


class WorkspaceTreeResponse(BaseModel):
    """文件树响应体。"""
    name: str
    path: str
    type: str                          # "dir"
    children: list                    # 嵌套结构，元素为 dict


class WorkspaceFileResponse(BaseModel):
    """读取工作区文件响应体。"""
    filepath: str
    content: str
    size: int


class WorkspaceSaveRequest(BaseModel):
    """保存工作区文件请求体。"""
    session_id: str
    filepath: str
    content: str


class WorkspaceSaveResponse(BaseModel):
    """保存工作区文件响应体。"""
    status: str                        # "saved" / "error"
    snapshot_id: int = 0               # 快照 ID（0 表示新文件无快照）
    filepath: str = ""
    error: str = ""


# ===================== 上传文件辅助函数 =====================

# 文件名安全字符集（只保留字母、数字、点、下划线、连字符）
_FILENAME_SAFE_RE = re.compile(r"[^a-zA-Z0-9._-]")


def _sanitize_filename(name: str) -> str:
    """规范化文件名：丢弃路径前缀，替换非法字符为下划线。

    防止：
    - 路径穿越（如 ../../etc/passwd）
    - 特殊字符注入
    - 中文文件名导致后续处理异常
    """
    # 只取文件名部分（丢弃任何路径前缀）
    base = Path(name).name
    # 替换非法字符
    safe = _FILENAME_SAFE_RE.sub("_", base)
    # 防止空文件名或全点文件名
    if not safe or safe.strip(".") == "":
        safe = "unnamed"
    return safe


def _get_session_upload_dir(session_id: str) -> Path:
    """获取指定会话的上传目录路径，自动创建。

    结构：{UPLOAD_DIR}/{session_id}/
    """
    # session_id 也要规范化，防止路径穿越
    safe_sid = _FILENAME_SAFE_RE.sub("_", session_id)
    upload_root = Path(UPLOAD_DIR).resolve()
    session_dir = upload_root / safe_sid
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def _get_session_size(session_dir: Path) -> int:
    """计算会话上传目录的总大小（字节）。"""
    if not session_dir.exists():
        return 0
    return sum(f.stat().st_size for f in session_dir.iterdir() if f.is_file())


# ===================== 路由 =====================

@app.get("/", response_class=HTMLResponse)
async def index():
    """根路径返回问答页面（static/index.html）。"""
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>static/index.html 不存在</h1>", status_code=404)
    return HTMLResponse(index_file.read_text(encoding="utf-8"))


@app.get("/api/health")
async def health():
    """健康检查 + 当前模式。"""
    return {
        "status": "ok",
        "mode": "Router+Skill",
    }


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """问答主接口（异步模式）：提交后立即返回 task_id，后台执行。

    流程：
      1. 创建 TaskControl + 后台线程跑 Orchestrator.query
      2. 立即返回 task_id（前端轮询 /api/task/{id}/status 获取结果）
      3. 前端可通过 /api/task/{id}/pause、/resume、/cancel 控制

    兼容模式：前端传 wait=true 时走旧的同步阻塞逻辑。
    """
    question = req.question.strip()
    if not question:
        return JSONResponse(
            {"error": "question 不能为空"},
            status_code=400,
        )

    # session_id：前端没传则自动生成
    import uuid
    session_id = req.session_id or f"web-{uuid.uuid4().hex[:8]}"

    # 创建任务
    from core.task_manager import create_task, run_task, get_task
    import threading

    tc = create_task(session_id=session_id, question=question)
    # 后台线程执行（daemon=True，进程退出时自动结束）
    t = threading.Thread(
        target=run_task,
        args=(tc, _orch, question, session_id),
        daemon=True,
    )
    t.start()
    tc._thread = t

    return {
        "task_id": tc.task_id,
        "session_id": session_id,
        "state": "running",
        "message": "任务已提交，请轮询 /api/task/{task_id}/status 获取结果",
    }


@app.get("/api/task/{task_id}/status")
async def task_status(task_id: str):
    """查询任务状态（前端轮询用）。

    返回：{task_id, state, step_info, step_progress, answer, error, session_id}
      - state: running | paused | done | cancelled | error
      - answer: 仅 state=done 时有值
      - step_info: 当前执行步骤描述
    """
    from core.task_manager import get_task
    tc = get_task(task_id)
    if tc is None:
        return JSONResponse(
            {"error": f"任务不存在: {task_id}"},
            status_code=404,
        )
    status = tc.to_status_dict()
    # 若已完成，附加 sources
    if status["state"] == "done" and tc.sources:
        sources_data = []
        for src in tc.sources:
            meta = getattr(src, "metadata", {}) or {}
            score = (meta.get("similarity_distance")
                     or meta.get("rrf_score")
                     or meta.get("bm25_score"))
            sources_data.append({
                "page_content": getattr(src, "page_content", ""),
                "metadata": meta,
                "score": score,
            })
        status["sources"] = sources_data
    return status


@app.post("/api/task/{task_id}/pause")
async def task_pause(task_id: str):
    """暂停任务（在下一个节点边界生效）。"""
    from core.task_manager import get_task
    tc = get_task(task_id)
    if tc is None:
        return JSONResponse(
            {"error": f"任务不存在: {task_id}"},
            status_code=404,
        )
    if tc.state.value not in ("running",):
        return {"task_id": task_id, "state": tc.state.value, "message": "任务不在运行中，无法暂停"}
    tc.pause()
    return {"task_id": task_id, "state": "paused", "message": "暂停信号已发送"}


@app.post("/api/task/{task_id}/resume")
async def task_resume(task_id: str):
    """继续执行暂停的任务。"""
    from core.task_manager import get_task
    tc = get_task(task_id)
    if tc is None:
        return JSONResponse(
            {"error": f"任务不存在: {task_id}"},
            status_code=404,
        )
    if tc.state.value not in ("paused",):
        return {"task_id": task_id, "state": tc.state.value, "message": "任务未暂停，无需继续"}
    tc.resume()
    return {"task_id": task_id, "state": "running", "message": "已恢复执行"}


@app.post("/api/task/{task_id}/cancel")
async def task_cancel(task_id: str):
    """取消任务（在下一个节点边界终止）。"""
    from core.task_manager import get_task
    tc = get_task(task_id)
    if tc is None:
        return JSONResponse(
            {"error": f"任务不存在: {task_id}"},
            status_code=404,
        )
    if tc.state.value in ("done", "cancelled", "error"):
        return {"task_id": task_id, "state": tc.state.value, "message": "任务已结束"}
    tc.cancel()
    return {"task_id": task_id, "state": "cancelled", "message": "取消信号已发送"}


# ===================== 代码撤销/历史接口 =====================

@app.post("/api/code/undo", response_model=CodeUndoResponse)
async def code_undo(req: CodeUndoRequest):
    """撤销指定文件最近一次代码修改，恢复到上一个快照。

    使用方式：
        POST /api/code/undo
        Body: {"filepath": "d:/python/RAG/simple/main.py"}

    返回：
        - status="undone"      → 撤销成功，文件已恢复
        - status="no_history"  → 该文件无历史快照可撤销
        - status="disabled"    → 代码模块未启用（ENABLE_CODE_AGENT=False）
        - status="error"       → 路径非法或撤销异常
    """
    filepath = req.filepath.strip()
    if not filepath:
        return JSONResponse(
            {"error": "filepath 不能为空"},
            status_code=400,
        )

    if not ENABLE_CODE_AGENT:
        return CodeUndoResponse(
            filepath=filepath,
            snapshot_id=0,
            remaining_undos=0,
            status="disabled",
        )

    try:
        result = undo_last(filepath)
        return CodeUndoResponse(
            filepath=filepath,
            snapshot_id=result.get("snapshot_id", 0),
            remaining_undos=result.get("remaining_undos", 0),
            status=result.get("status", "error"),
        )
    except ValueError as e:
        # 路径安全校验失败
        return JSONResponse(
            {"error": f"路径非法: {e}"},
            status_code=400,
        )
    except Exception as e:
        return JSONResponse(
            {"error": f"撤销失败: {e}"},
            status_code=500,
        )


@app.get("/api/code/history", response_model=list[CodeHistoryItem])
async def code_history(filepath: str):
    """查询指定文件的修改历史列表。

    使用方式：
        GET /api/code/history?filepath=d:/python/RAG/simple/main.py

    返回：按 snapshot_id 升序排列的历史记录列表。
    """
    filepath = filepath.strip()
    if not filepath:
        return JSONResponse(
            {"error": "filepath 查询参数不能为空"},
            status_code=400,
        )

    if not ENABLE_CODE_AGENT:
        return []

    try:
        history = get_history(filepath)
        return [
            CodeHistoryItem(
                snapshot_id=item.get("snapshot_id", 0),
                filename=item.get("filename", ""),
                filepath=item.get("filepath", ""),
                timestamp=item.get("timestamp", 0.0),
                action_desc=item.get("action_desc", ""),
            )
            for item in history
        ]
    except ValueError as e:
        return JSONResponse(
            {"error": f"路径非法: {e}"},
            status_code=400,
        )
    except Exception as e:
        return JSONResponse(
            {"error": f"查询历史失败: {e}"},
            status_code=500,
        )


# ===================== 上传文件接口 =====================

@app.post("/api/code/upload", response_model=UploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    session_id: str = Query(..., description="会话 ID（用于隔离不同会话的上传文件）"),
):
    """上传代码文件到会话工作区。

    使用方式（multipart/form-data）：
        POST /api/code/upload?session_id=web-abc123
        Body: file=<文件内容>

    存储位置：{UPLOAD_DIR}/{session_id}/{filename}

    返回：
        - 成功：{filename, filepath, size, session_id}
        - 失败：相应 HTTP 状态码 + error 信息

    安全限制：
        - 文件扩展名必须在 UPLOAD_ALLOWED_EXTS 白名单内
        - 单文件大小不超过 UPLOAD_MAX_FILE_SIZE（默认 1MB）
        - 单会话总大小不超过 UPLOAD_MAX_SESSION_SIZE（默认 10MB）
        - 文件名规范化：只保留 [a-zA-Z0-9._-]，其他字符替换为 _
    """
    if not ENABLE_CODE_AGENT:
        return JSONResponse(
            {"error": "代码模块未启用（ENABLE_CODE_AGENT=False）"},
            status_code=403,
        )

    # 1. 读取文件内容（限制单文件大小）
    content = await file.read()
    if len(content) > UPLOAD_MAX_FILE_SIZE:
        return JSONResponse(
            {"error": f"文件过大：{len(content)} 字节，上限 {UPLOAD_MAX_FILE_SIZE} 字节"},
            status_code=413,
        )

    # 2. 文件名规范化
    original_name = file.filename or "unnamed"
    safe_name = _sanitize_filename(original_name)

    # 3. 扩展名白名单校验
    ext = Path(safe_name).suffix.lower()
    if ext not in UPLOAD_ALLOWED_EXTS:
        allowed_str = ", ".join(sorted(UPLOAD_ALLOWED_EXTS))
        return JSONResponse(
            {"error": f"不支持的文件类型: {ext}（允许: {allowed_str}）"},
            status_code=400,
        )

    # 4. 会话目录 + 总大小校验
    session_dir = _get_session_upload_dir(session_id)
    current_size = _get_session_size(session_dir)
    # 如果是覆盖同名文件，减去旧文件大小
    target_path = session_dir / safe_name
    if target_path.exists():
        current_size -= target_path.stat().st_size
    if current_size + len(content) > UPLOAD_MAX_SESSION_SIZE:
        return JSONResponse(
            {"error": f"会话总大小超限：当前 {current_size} 字节 + 新增 {len(content)} 字节，上限 {UPLOAD_MAX_SESSION_SIZE} 字节"},
            status_code=413,
        )

    # 5. 写入文件
    try:
        target_path.write_bytes(content)
    except Exception as e:
        return JSONResponse(
            {"error": f"文件保存失败: {e}"},
            status_code=500,
        )

    return UploadResponse(
        filename=safe_name,
        filepath=str(target_path.resolve()),
        size=len(content),
        session_id=session_id,
    )


@app.get("/api/code/uploads/{session_id}", response_model=list[UploadItem])
async def list_uploads(session_id: str):
    """列出指定会话的所有已上传文件。

    使用方式：
        GET /api/code/uploads/web-abc123

    返回：按文件名排序的上传文件列表。
    """
    if not ENABLE_CODE_AGENT:
        return []

    session_dir = _get_session_upload_dir(session_id)
    if not session_dir.exists():
        return []

    items = []
    try:
        for f in session_dir.iterdir():
            if not f.is_file():
                continue
            stat = f.stat()
            items.append(UploadItem(
                filename=f.name,
                filepath=str(f.resolve()),
                size=stat.st_size,
                upload_time=stat.st_mtime,
            ))
    except Exception as e:
        return JSONResponse(
            {"error": f"列出文件失败: {e}"},
            status_code=500,
        )

    # 按文件名排序
    items.sort(key=lambda x: x.filename)
    return items


@app.delete("/api/code/uploads/{session_id}/{filename}", response_model=UploadDeleteResponse)
async def delete_upload(session_id: str, filename: str):
    """删除指定会话的上传文件。

    使用方式：
        DELETE /api/code/uploads/web-abc123/user_code.py

    返回：
        - status="deleted"    → 删除成功
        - status="not_found"  → 文件不存在
        - status="error"      → 删除异常
    """
    if not ENABLE_CODE_AGENT:
        return JSONResponse(
            {"error": "代码模块未启用（ENABLE_CODE_AGENT=False）"},
            status_code=403,
        )

    safe_name = _sanitize_filename(filename)
    session_dir = _get_session_upload_dir(session_id)
    target_path = session_dir / safe_name

    if not target_path.exists() or not target_path.is_file():
        return UploadDeleteResponse(
            filename=safe_name,
            session_id=session_id,
            status="not_found",
        )

    try:
        target_path.unlink()
        return UploadDeleteResponse(
            filename=safe_name,
            session_id=session_id,
            status="deleted",
        )
    except Exception as e:
        return UploadDeleteResponse(
            filename=safe_name,
            session_id=session_id,
            status="error",
        )


# ===================== 工作区（打开项目）接口 =====================

@app.get("/api/code/pending")
async def code_pending(session_id: str = Query(..., description="会话 ID")):
    """获取待确认的修改列表（Agent 生成 diff 预览后，前端展示用）。

    使用方式：
        GET /api/code/pending?session_id=web-abc123

    返回：{session_id, pending: [{filepath, action, diff, is_new?}, ...], count}
    """
    if not ENABLE_CODE_AGENT:
        return JSONResponse(
            {"error": "代码模块未启用（ENABLE_CODE_AGENT=False）"},
            status_code=403,
        )

    pending = get_pending_modifications(session_id=session_id)
    return {
        "session_id": session_id,
        "pending": pending,
        "count": len(pending),
    }


@app.post("/api/code/confirm")
async def code_confirm(session_id: str = Query(..., description="会话 ID")):
    """确认执行所有待确认的修改（真正写入文件 + 创建快照）。

    使用方式：
        POST /api/code/confirm?session_id=web-abc123

    返回：{status, confirmed_count, results: [{filepath, snapshot_id, status}]}
    """
    if not ENABLE_CODE_AGENT:
        return JSONResponse(
            {"error": "代码模块未启用（ENABLE_CODE_AGENT=False）"},
            status_code=403,
        )

    result = confirm_modifications(session_id=session_id)
    return result


@app.post("/api/code/cancel")
async def code_cancel(session_id: str = Query(..., description="会话 ID")):
    """取消所有待确认的修改（不执行任何写入，前端提示用户重新输入需求）。

    使用方式：
        POST /api/code/cancel?session_id=web-abc123

    返回：{status, cancelled_count}
    """
    if not ENABLE_CODE_AGENT:
        return JSONResponse(
            {"error": "代码模块未启用（ENABLE_CODE_AGENT=False）"},
            status_code=403,
        )

    result = cancel_modifications(session_id=session_id)
    return result


# ===================== 工作区（打开项目）接口 =====================

@app.post("/api/workspace/open", response_model=WorkspaceOpenResponse)
async def workspace_open(req: WorkspaceOpenRequest):
    """打开本地项目目录，加入会话级白名单。

    使用方式：
        POST /api/workspace/open
        Body: {"session_id": "web-abc123", "project_path": "d:/myproject"}

    打开后，Agent 可通过 read_file/edit_file/grep_code 操作该项目内的所有文件。
    单会话最多打开 WORKSPACE_MAX_PROJECTS 个项目。

    返回：
        - status="opened"        → 新打开成功
        - status="already_open"  → 该项目已打开（幂等）
        - status="error"         → 打开失败（路径不存在/超限/黑名单等）
    """
    if not ENABLE_CODE_AGENT:
        return JSONResponse(
            {"error": "代码模块未启用（ENABLE_CODE_AGENT=False）"},
            status_code=403,
        )

    try:
        p = add_session_workspace(req.session_id, req.project_path)
        # 检查是否是新打开的还是已存在
        current = get_session_workspaces(req.session_id)
        # 通过对比添加前后数量判断（add_session_workspace 是幂等的）
        # 这里简单返回 opened，add_session_workspace 已处理幂等
        status = "opened"
        return WorkspaceOpenResponse(
            session_id=req.session_id,
            project_path=str(p),
            project_name=p.name,
            status=status,
            current_projects=[str(x) for x in current],
        )
    except ValueError as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=400,
        )
    except Exception as e:
        return JSONResponse(
            {"error": f"打开项目失败: {e}"},
            status_code=500,
        )


@app.get("/api/workspace/tree")
async def workspace_tree(
    session_id: str = Query(..., description="会话 ID"),
    path: str = Query(..., description="要扫描的目录路径（通常是项目根或子目录）"),
    depth: int = Query(None, description="扫描深度，默认 2，最大 5"),
):
    """懒加载扫描目录树，返回嵌套结构供前端文件树展示。

    使用方式：
        GET /api/workspace/tree?session_id=web-abc123&path=d:/myproject&depth=2

    首次打开项目时传项目根路径 + 默认深度；
    点击展开子目录时传子目录路径 + depth=1。

    返回：{name, path, type, children: [...]}
    """
    if not ENABLE_CODE_AGENT:
        return JSONResponse(
            {"error": "代码模块未启用（ENABLE_CODE_AGENT=False）"},
            status_code=403,
        )

    if depth is None:
        depth = WORKSPACE_TREE_DEFAULT_DEPTH

    result = list_tree_impl(path, session_id=session_id, depth=depth)
    if "error" in result:
        return JSONResponse({"error": result["error"]}, status_code=400)
    return result


@app.get("/api/workspace/file", response_model=WorkspaceFileResponse)
async def workspace_file(
    session_id: str = Query(..., description="会话 ID"),
    filepath: str = Query(..., description="文件路径"),
):
    """读取工作区文件内容，供前端 Monaco 编辑器展示。

    使用方式：
        GET /api/workspace/file?session_id=web-abc123&filepath=d:/myproject/app.py

    返回：{filepath, content, size}
    """
    if not ENABLE_CODE_AGENT:
        return JSONResponse(
            {"error": "代码模块未启用（ENABLE_CODE_AGENT=False）"},
            status_code=403,
        )

    content = read_workspace_file_impl(filepath, session_id=session_id)
    if content.startswith("错误："):
        return JSONResponse({"error": content}, status_code=400)

    return WorkspaceFileResponse(
        filepath=filepath,
        content=content,
        size=len(content.encode("utf-8")),
    )


@app.post("/api/workspace/save", response_model=WorkspaceSaveResponse)
async def workspace_save(req: WorkspaceSaveRequest):
    """保存工作区文件内容（前端 Ctrl+S 调用，自动创建快照支持撤销）。

    使用方式：
        POST /api/workspace/save
        Body: {"session_id": "web-abc123", "filepath": "...", "content": "..."}

    返回：{status, snapshot_id, filepath}
    """
    if not ENABLE_CODE_AGENT:
        return JSONResponse(
            {"error": "代码模块未启用（ENABLE_CODE_AGENT=False）"},
            status_code=403,
        )

    result = save_workspace_file_impl(req.filepath, req.content, session_id=req.session_id)
    if "error" in result:
        return WorkspaceSaveResponse(
            status="error",
            error=result["error"],
        )
    return WorkspaceSaveResponse(
        status=result["status"],
        snapshot_id=result.get("snapshot_id", 0),
        filepath=result.get("filepath", req.filepath),
    )


@app.get("/api/workspace/status")
async def workspace_status(session_id: str = Query(..., description="会话 ID")):
    """查询当前会话已打开的项目列表。

    使用方式：
        GET /api/workspace/status?session_id=web-abc123

    返回：{session_id, projects: [{name, path}, ...], count}
    """
    if not ENABLE_CODE_AGENT:
        return JSONResponse(
            {"error": "代码模块未启用（ENABLE_CODE_AGENT=False）"},
            status_code=403,
        )

    workspaces = get_session_workspaces(session_id)
    return {
        "session_id": session_id,
        "projects": [{"name": p.name, "path": str(p)} for p in workspaces],
        "count": len(workspaces),
    }


@app.post("/api/workspace/close")
async def workspace_close(
    session_id: str = Query(..., description="会话 ID"),
    project_path: str = Query(None, description="要关闭的项目路径；为空时关闭该会话所有项目"),
):
    """关闭已打开的项目，移除会话级白名单。

    使用方式：
        POST /api/workspace/close?session_id=web-abc123
        POST /api/workspace/close?session_id=web-abc123&project_path=d:/myproject

    返回：{session_id, removed_count, status}
    """
    if not ENABLE_CODE_AGENT:
        return JSONResponse(
            {"error": "代码模块未启用（ENABLE_CODE_AGENT=False）"},
            status_code=403,
        )

    removed = remove_session_workspace(session_id, project_path)
    return {
        "session_id": session_id,
        "removed_count": removed,
        "status": "closed" if removed > 0 else "nothing_to_close",
    }


# ===================== 启动入口（python main.py --web 调用）=====================
def run_web():
    """启动 uvicorn 服务。main.py --web 时调用此函数。"""
    import uvicorn
    print(f"🌐 Web 服务启动：http://{WEB_HOST}:{WEB_PORT}")
    print(f"   模式：Router + Skill")
    print(f"   接口：POST /api/chat  |  POST /api/code/undo  |  GET /api/code/history")
    print(f"         POST /api/code/upload  |  GET /api/code/uploads/{{sid}}  |  DELETE /api/code/uploads/{{sid}}/{{file}}")
    print(f"         GET /api/code/pending  |  POST /api/code/confirm  |  POST /api/code/cancel")
    print(f"         POST /api/workspace/open  |  GET /api/workspace/tree  |  GET /api/workspace/file")
    print(f"         POST /api/workspace/save  |  GET /api/workspace/status  |  POST /api/workspace/close")
    print(f"   文档：/docs\n")
    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT)
