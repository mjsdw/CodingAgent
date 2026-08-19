# config/config.py
# ========== 全局配置中心：所有路径、模型、阈值、轮次统一在此管理 ==========

# ---------------------- 路径配置 ----------------------
# 路径相对项目根目录（simple/），由 main.py 作为入口保证工作目录
VECTOR_DB_PATH = "./db"                          # Chroma 向量库持久化目录
SYSTEM_ROLE_FILE = "./config/system_role.md"     # 系统角色提示词文件
DATA_FILE_PATH = "./data/rag.pdf"                # 待入库的源文档

# ---------------------- Embedding 模型配置 ----------------------
EMBED_MODEL_NAME = "BAAI/bge-small-zh-v1.5"  # 中文 BGE 向量模型
EMBED_DEVICE = "cpu"                          # 有N卡cuda环境改成 "cuda"
EMBED_NORMALIZE = True                        # BGE 必须开启归一化

# Embedding 离线模式：Windows 网络受限或 huggingface.co 连接失败时开启
# 开启后强制只从本地 cache 加载模型，不发起任何联网请求（解决 WinError 10060）
# 要求：本地已缓存模型（通常首次成功下载后会存在 ~/.cache/huggingface/hub/）
EMBED_OFFLINE = True
# 本地缓存目录：None = 用默认 ~/.cache/huggingface；也可填绝对路径自定义
EMBED_CACHE_DIR = None

# ---------------------- 文档切分配置 ----------------------
CHUNK_SIZE = 500        # 单个文本块最大字符数
CHUNK_OVERLAP = 100     # 相邻块重叠字符数，避免语义断裂

# 语义切分分隔符：按优先级从高到低依次尝试，优先从中文段落/句号处切分
# 避免从句子中间硬切断，保留语义完整性
CHUNK_SEPARATORS = [
    "\n\n",   # 段落分隔（最优先，语义边界最清晰）
    "\n",     # 换行
    "。",     # 中文句号
    "！",     # 中文感叹号
    "？",     # 中文问号
    "；",     # 中文分号
    "，",     # 中文逗号
    " ",      # 空格
    "",       # 兜底：逐字符切（最后才用）
]

# ---------------------- 检索配置 ----------------------
RETRIEVE_TOP_K = 8      # 每轮向量检索召回的片段数量

# 相似度阈值：Chroma 返回的是 L2 距离，值越小越相似
# BAAI/bge-small-zh-v1.5 为归一化向量，L2 距离范围 [0, 2]
# 距离 > 该阈值的片段将被过滤掉，避免低相关度内容污染答案
# 参考：距离≈1.2 对应余弦相似度≈0.3；≈1.0 对应≈0.5
SIMILARITY_THRESHOLD = 1.0

# ---------------------- 混合检索配置 ----------------------
# 是否启用混合检索（向量 + BM25 关键词检索，RRF 融合）
# 关闭时退回纯向量检索 search_with_scores
# 依赖：pip install rank_bm25 jieba
ENABLE_HYBRID_SEARCH = True
BM25_TOP_K = 10       # BM25 关键词检索召回数量（通常略大于向量 top_k，多召回再融合）
# RRF（Reciprocal Rank Fusion）融合常数：score = Σ 1/(RRF_K + rank)
# 值越大，排名靠后的项衰减越慢，各路召回权重越均衡；标准值 60
RRF_K = 60

# ---------------------- RAG 流程配置 ----------------------
MAX_SEARCH_ROUND = 5    # ReAct 检索最大轮次（含首轮改写检索）
# 是否启用 ReAct 多轮检索（LLM 自主决定搜什么、搜几次、何时停）
# True=ReAct 多轮；False=单轮检索直接生成答案
ENABLE_MULTI_ROUND = True

# ---------------------- Query 改写配置 ----------------------
# 是否启用 Query 改写：首轮检索前，将用户口语化问题经 LLM 改写为检索友好表述
# 后续轮次的检索词由 ReAct 的 action_input 接管，故改写仅在首轮执行一次
ENABLE_QUERY_REWRITE = True

# ---------------------- 联网搜索配置 ----------------------
# 是否启用联网搜索（本地知识库搜不到时，ReAct 可选 web_search 补充资料）
ENABLE_WEB_SEARCH = True
# 联网搜索每次返回的最大结果数
WEB_SEARCH_MAX_RESULTS = 3
# 联网抓取网页正文的超时秒数
WEB_FETCH_TIMEOUT = 8
# 每个网页正文最大保留字符数（避免 token 膨胀）
WEB_CONTENT_MAX_CHARS = 800

# ---------------------- LLM 配置 ----------------------
LLM_MODEL = "qwen3.7-max"
LLM_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
LLM_TEMPERATURE = 0.0

# ---------------------- Web 服务配置 ----------------------
# main.py --web 启动时使用
WEB_HOST = "127.0.0.1"   # 监听地址；局域网访问改成 "0.0.0.0"
WEB_PORT = 8000          # 监听端口

# ---------------------- 对话记忆配置 ----------------------
# 是否启用对话记忆（多轮上下文）
# True=按 session_id 保存历史，下次提问自动注入 LLM 上下文
# False=每次问答独立（无记忆，原行为）
ENABLE_MEMORY = True
# 记忆后端："memory"=内存（重启丢失）/ "sqlite"=SQLite 持久化（重启不丢）
MEMORY_BACKEND = "sqlite"
# SQLite 数据库路径（仅 MEMORY_BACKEND="sqlite" 时生效）
MEMORY_DB_PATH = "./data/memory.db"
# 每个会话最多保留多少条历史消息（滑动窗口，避免 prompt 膨胀）
MEMORY_MAX_MESSAGES = 20

# ---------------------- 代码模块配置 ----------------------
# 是否启用代码 Agent（代码生成/修改/Bug 修复）
ENABLE_CODE_AGENT = True
# 代码修改历史备份目录（每次 edit/write 前自动创建快照，支持撤销）
CODE_HISTORY_DIR = "./data/workspace/history"
# LangGraph Plan-and-Execute 最大执行步数（防死循环）
# 含重试和重规划消耗的步数；重规划后 total_steps 会重置，给新计划完整额度
MAX_CODE_ROUNDS = 15

# 路径安全校验：白名单优先 + 黑名单双保险
# 白名单：只允许访问 ALLOWED_WORKSPACES 列表及其子目录下的文件
# 用 Path.resolve() 规范化后再比较，防止 ../、符号链接等绕过
#
# 安全策略：
#   - ALLOWED_WORKSPACES 默认为空（不暴露项目自身源码）
#   - 用户通过"打开项目"功能动态加入会话级白名单后才能访问项目文件
#   - 上传文件目录和快照目录由 code_tool 内置白名单保护，无需用户打开项目
ALLOWED_WORKSPACES = [
    # 默认为空：未打开项目时 Agent 无法读取任何项目文件
    # 如需永久开放某个目录，在此追加绝对路径，例如：
    # "d:/python/projects/other_repo",
]
# 黑名单：即使在白名单内也禁止访问的系统目录（双保险）
BLOCKED_DIRS = ["C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)"]

# ---------------------- 上传文件配置 ----------------------
# 上传文件存储根目录（按 session_id 隔离：uploads/{session_id}/{filename}）
UPLOAD_DIR = "./data/workspace/uploads"
# 单文件大小上限（字节），默认 1MB
UPLOAD_MAX_FILE_SIZE = 1 * 1024 * 1024
# 单会话总大小上限（字节），默认 10MB
UPLOAD_MAX_SESSION_SIZE = 10 * 1024 * 1024
# 允许上传的文件扩展名白名单（代码/文本类文件）
UPLOAD_ALLOWED_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift",
    ".kt", ".scala", ".md", ".txt", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".html", ".css", ".scss", ".less",
    ".vue", ".svelte", ".sql", ".sh", ".bat", ".ps1",
}

# ---------------------- 工作区（打开项目）配置 ----------------------
# 单会话最多可同时打开的项目目录数
WORKSPACE_MAX_PROJECTS = 3
# 文件树懒加载默认深度（首次打开项目时扫描几层）
WORKSPACE_TREE_DEFAULT_DEPTH = 2
# 文件树最大深度（防止超深目录递归爆栈）
WORKSPACE_TREE_MAX_DEPTH = 5
# 扫描文件树时跳过的目录名（不区分大小写）
WORKSPACE_HIDDEN_DIRS = {
    ".git", ".svn", ".hg", "__pycache__", "node_modules",
    ".venv", "venv", ".idea", ".vscode", "dist", "build",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "egg-info",
}
# 允许通过"打开项目"绑定的根目录前缀限制（防止打开系统目录）
# 空列表表示不限制；非空时只允许这些前缀下的目录（如用户主目录）
WORKSPACE_ALLOWED_ROOTS = []   # 例：["C:\\Users", "D:\\", "/home"]
