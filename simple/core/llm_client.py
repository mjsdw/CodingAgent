
# core/llm_client.py
import os
import base64
import mimetypes
import httpx
from pathlib import Path
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
# 统一从 config 包读取配置常量
from config import SYSTEM_ROLE_FILE, LLM_MODEL, LLM_BASE_URL, LLM_TEMPERATURE, VL_MODEL

load_dotenv()

# Windows 代理环境下 SSL 证书验证会失败（httpcore 走 http_proxy 时无法验证 CA）
# 用 certifi 的 CA 证书包作为默认验证源；若仍失败可在 .env 设 LLM_SSL_VERIFY=False 关闭验证
import certifi
_ssl_verify = os.getenv("LLM_SSL_VERIFY", "true").lower() != "false"
_http_client = httpx.Client(
    verify=certifi.where() if _ssl_verify else False,
    timeout=60.0,
)

# 内存缓存全局角色文本
_cached_system_prompt = ""

def load_system_prompt_from_file():
    """从md文件加载身份提示词，读取后存入缓存"""
    global _cached_system_prompt
    try:
        with open(SYSTEM_ROLE_FILE, "r", encoding="utf-8") as f:
            _cached_system_prompt = f.read().strip()
        print(f"✅ 成功加载系统角色文件：{SYSTEM_ROLE_FILE}")
    except FileNotFoundError:
        print(f"❌ 未找到 {SYSTEM_ROLE_FILE}，使用空角色")
        _cached_system_prompt = ""
    except Exception as e:
        print(f"⚠️ 读取角色文件异常：{str(e)}")
        _cached_system_prompt = ""

# 程序启动自动加载一次
load_system_prompt_from_file()

llm=None
def get_llm():
    """获取阿里云百炼大模型实例（关闭思考模式，避免污染JSON输出）"""
    global llm
    if llm is None:
        llm = ChatOpenAI(
           api_key=os.getenv("DASHSCOPE_API_KEY"),
           base_url=LLM_BASE_URL,
           model=LLM_MODEL,
           temperature=LLM_TEMPERATURE,
           http_client=_http_client,
        )
    return llm

def invoke_with_system(user_content: str):
    """
    调用LLM，自动拼接内存中缓存的system角色
    """
    llm = get_llm()
    chat_template = ChatPromptTemplate.from_messages([
        ("system", _cached_system_prompt),
        ("human", "{user_input}")
    ])
    chain = chat_template | llm
    return chain.invoke({"user_input": user_content})


# ===================== 多模态（图片理解）调用 =====================

# 视觉模型懒加载单例（与文本模型 llm 分开管理，互不影响）
_vl_llm = None


def get_vl_llm():
    """获取视觉语言模型实例（qwen-vl-max 等，走同一 DashScope 端点）。

    与文本模型 get_llm() 分开单例的原因：
      - 两者 model 不同（LLM_MODEL vs VL_MODEL），不能共用实例
      - 懒加载 + 单例：首次带图请求才创建，纯文本流量零开销
      - 共享同一个 _http_client（连接池复用，SSL 证书配置一致）
    """
    global _vl_llm
    if _vl_llm is None:
        _vl_llm = ChatOpenAI(
           api_key=os.getenv("DASHSCOPE_API_KEY"),
           base_url=LLM_BASE_URL,
           model=VL_MODEL,
           temperature=LLM_TEMPERATURE,
           http_client=_http_client,
        )
    return _vl_llm


def _image_to_data_url(image_path: str) -> str:
    """读取本地图片文件，编码为 data URL（base64 内联格式）。

    :param image_path: 图片绝对路径
    :return: "data:image/png;base64,xxxx" 格式字符串
    :raises FileNotFoundError: 文件不存在
    :raises ValueError: 扩展名不在图片白名单中

    为什么用 data URL 而不是传文件 URL：
      - 上传的图片存在本地磁盘，没有公网可访问的 URL，
        DashScope 无法主动下载本地文件 → 只能内联 base64
      - data URL 是 OpenAI 多模态协议的标准格式，所有兼容端点都认
    """
    p = Path(image_path)
    if not p.is_file():
        raise FileNotFoundError(f"图片文件不存在: {p}")

    # 根据扩展名推断 MIME 类型；png/jpg 等常见类型 mimetypes 都能覆盖，
    # 推断失败时兜底为 image/png（视觉模型对 MIME 误标容错性较高）
    mime, _ = mimetypes.guess_type(p.name)
    if not mime or not mime.startswith("image/"):
        mime = "image/png"

    raw = p.read_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def invoke_with_images(user_text: str, image_paths: list[str]):
    """多模态调用：文本 + 多张图片，返回视觉模型的分析结果。

    消息构造遵循 OpenAI 多模态协议（DashScope 兼容端点同样支持）：
      human 消息体 = [
        {"type": "text",  "text": "..."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
        ...每张图片一个 block
      ]

    :param user_text: 用户文本问题（不能为空，纯图也需要描述性提示词）
    :param image_paths: 图片文件路径列表（1~IMAGE_MAX_PER_MESSAGE 张）
    :return: LangChain AIMessage（与 invoke_with_system 返回类型一致，
             调用方统一用 resp.content 取文本）
    """
    # 组装混合消息体：文本块在前（先给问题，再看图，符合人类阅读顺序，
    # 也有助于模型先理解任务再看证据）
    content_blocks = [{"type": "text", "text": user_text}]
    for img_path in image_paths:
        data_url = _image_to_data_url(img_path)
        content_blocks.append({
            "type": "image_url",
            "image_url": {"url": data_url},
        })

    # 与 invoke_with_system 保持完全一致的消息结构（system + human），
    # 区别仅在于 human 的内容从纯字符串变成了 content blocks 列表
    llm = get_vl_llm()
    chat_template = ChatPromptTemplate.from_messages([
        ("system", _cached_system_prompt),
        ("human", "{user_input}")
    ])
    chain = chat_template | llm
    return chain.invoke({"user_input": content_blocks})


# 【可选】手动热重载接口，修改md后不需要重启程序
def reload_system_role():
    load_system_prompt_from_file()
