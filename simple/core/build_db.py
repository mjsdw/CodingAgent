# core/build_db.py
# 知识库构建脚本：PDF → 语义切片 → 入库
# 运行方式：在 simple/ 根目录执行 python -m core.build_db
import os
import warnings
# HuggingFace国内镜像
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
# 关闭软链接警告
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
# 屏蔽废弃警告
warnings.filterwarnings("ignore", category=DeprecationWarning, module="langchain_community")

from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.document_loaders import PyPDFLoader
# 统一从 config 包读取配置常量
from config import (
    EMBED_MODEL_NAME,
    EMBED_DEVICE,
    EMBED_NORMALIZE,
    EMBED_OFFLINE,
    EMBED_CACHE_DIR,
    CHUNK_SIZE,
    CHUNK_OVERLAP,
    CHUNK_SEPARATORS,
    VECTOR_DB_PATH,
    DATA_FILE_PATH,
)

# ====================== 中文BGE向量模型 ======================
_model_kwargs = {"device": EMBED_DEVICE}
if EMBED_OFFLINE:
    _model_kwargs["local_files_only"] = True
_emb_kwargs = dict(
    model_name=EMBED_MODEL_NAME,
    model_kwargs=_model_kwargs,
    encode_kwargs={"normalize_embeddings": EMBED_NORMALIZE},
)
if EMBED_CACHE_DIR:
    _emb_kwargs["cache_folder"] = EMBED_CACHE_DIR
embedding = HuggingFaceEmbeddings(**_emb_kwargs)

def build_vector_db():
    loader = PyPDFLoader(DATA_FILE_PATH)
    docs = loader.load()

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=CHUNK_SEPARATORS,
        keep_separator=True
    )
    splits = splitter.split_documents(docs)
    print(f"📄 文档切片总数：{len(splits)}")

    vectordb = Chroma.from_documents(
        documents=splits,
        embedding=embedding,
        persist_directory=VECTOR_DB_PATH
    )
    # 新版langchain_chroma无需手动persist
    print("✅ 向量库构建完成，持久化目录：./db")

if __name__ == "__main__":
    build_vector_db()
