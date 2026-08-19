
# core/llm_client.py
import os
import httpx
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
# 统一从 config 包读取配置常量
from config import SYSTEM_ROLE_FILE, LLM_MODEL, LLM_BASE_URL, LLM_TEMPERATURE

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


# 【可选】手动热重载接口，修改md后不需要重启程序
def reload_system_role():
    load_system_prompt_from_file()
