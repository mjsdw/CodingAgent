# core/vector_store.py
import os
# HF环境变量必须放在最顶部！防止导入顺序导致镜像失效
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = "300"
os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"

from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_core.documents import Document
# 统一从 config 包读取配置常量
from config import (
    VECTOR_DB_PATH,
    EMBED_MODEL_NAME,
    EMBED_DEVICE,
    EMBED_NORMALIZE,
    EMBED_OFFLINE,
    EMBED_CACHE_DIR,
    RETRIEVE_TOP_K,
    SIMILARITY_THRESHOLD,
    ENABLE_HYBRID_SEARCH,
    BM25_TOP_K,
    RRF_K,
)

# 全局单例，懒加载
_embedding = None
_vectordb = None
_retriever = None
_bm25_index = None    # BM25 索引单例
_bm25_docs = None     # BM25 索引对应的文档列表（与索引顺序一致）

def get_embedding():
    global _embedding
    if _embedding is None:
        print("🔧 正在加载Embedding模型..."
              + (" [离线模式，仅使用本地缓存]" if EMBED_OFFLINE else ""))
        model_kwargs = {"device": EMBED_DEVICE}
        if EMBED_OFFLINE:
            # 强制离线：禁止任何联网请求，从本地 cache 加载
            model_kwargs["local_files_only"] = True
        kwargs = dict(
            model_name=EMBED_MODEL_NAME,
            model_kwargs=model_kwargs,
            encode_kwargs={"normalize_embeddings": EMBED_NORMALIZE},
        )
        if EMBED_CACHE_DIR:
            kwargs["cache_folder"] = EMBED_CACHE_DIR
        _embedding = HuggingFaceEmbeddings(**kwargs)
    return _embedding

def get_vector_store():
    global _vectordb
    if _vectordb is None:
        emb = get_embedding()
        _vectordb = Chroma(
            persist_directory=VECTOR_DB_PATH,
            embedding_function=emb
        )
    return _vectordb

def get_retriever():
    global _retriever
    if _retriever is None:
        db = get_vector_store()
        _retriever = db.as_retriever(search_kwargs={"k": RETRIEVE_TOP_K})
    return _retriever

def search_with_scores(query):
    """
    带相似度分数的检索，自动过滤距离 > SIMILARITY_THRESHOLD 的低相关度片段
    :param query: 检索词
    :return: 过滤后的文档列表（已按相关度从高到低排序）
    """
    db = get_vector_store()
    # similarity_search_with_score 返回 [(Document, distance), ...]，distance 越小越相似
    results = db.similarity_search_with_score(query, k=RETRIEVE_TOP_K)

    filtered_docs = []
    for doc, distance in results:
        if distance <= SIMILARITY_THRESHOLD:
            # 把距离写入元数据，方便上游展示与调试
            doc.metadata["similarity_distance"] = round(float(distance), 4)
            filtered_docs.append(doc)

    print(f"📊 检索到 {len(results)} 条，相似度过滤后保留 {len(filtered_docs)} 条"
          f"（阈值距离 ≤ {SIMILARITY_THRESHOLD}）")
    return filtered_docs


# ---------------------- BM25 关键词检索 ----------------------
def _tokenize_zh(text):
    """中文分词：jieba 切词后去空白，BM25 要求 token 列表"""
    import jieba
    return [w for w in jieba.cut(text) if w.strip()]


def get_bm25_index():
    """懒加载构建 BM25 索引：从 Chroma 读取全部文档，jieba 分词后建索引。
    首次检索时构建，后续复用单例。"""
    global _bm25_index, _bm25_docs
    if _bm25_index is not None:
        return _bm25_index, _bm25_docs

    from rank_bm25 import BM25Okapi

    db = get_vector_store()
    # Chroma.get() 返回 {ids, documents, metadatas, ...}
    all_data = db.get()
    _bm25_docs = []
    tokenized_corpus = []
    for content, meta in zip(all_data["documents"], all_data["metadatas"]):
        doc = Document(page_content=content, metadata=meta or {})
        _bm25_docs.append(doc)
        tokenized_corpus.append(_tokenize_zh(content))

    _bm25_index = BM25Okapi(tokenized_corpus)
    print(f"📊 BM25 索引构建完成，共 {len(_bm25_docs)} 篇文档")
    return _bm25_index, _bm25_docs


def bm25_search(query, top_k=BM25_TOP_K):
    """BM25 关键词检索：返回按相关度降序的文档列表（已过滤分数为 0 的完全不匹配项）"""
    index, docs = get_bm25_index()
    tokenized_query = _tokenize_zh(query)
    scores = index.get_scores(tokenized_query)

    # 按分数降序取 top_k，过滤分数 ≤ 0 的项
    ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
    results = []
    for idx in ranked:
        if scores[idx] > 0:
            doc = docs[idx]
            doc.metadata["bm25_score"] = round(float(scores[idx]), 4)
            results.append(doc)
    return results


# ---------------------- 混合检索（向量 + BM25，RRF 融合）----------------------
def hybrid_search(query):
    """混合检索：向量检索 + BM25 关键词检索并行召回，RRF 融合排序后返回 top_k。
    - 向量侧：复用 similarity_search_with_score，保留阈值过滤
    - BM25 侧：独立召回，不受向量阈值影响（互补召回）
    - 融合：RRF（Reciprocal Rank Fusion），score = Σ 1/(RRF_K + rank)
    两路独立召回，互为补充，覆盖向量模型漏召回的精确匹配场景。"""
    # 1. 向量检索（带阈值过滤）
    db = get_vector_store()
    vec_results = db.similarity_search_with_score(query, k=RETRIEVE_TOP_K)
    vec_ranked = []  # [(doc, rank)]，rank 从 0 开始
    for rank, (doc, distance) in enumerate(vec_results):
        if distance <= SIMILARITY_THRESHOLD:
            doc.metadata["similarity_distance"] = round(float(distance), 4)
            vec_ranked.append((doc, rank))

    # 2. BM25 检索
    bm25_results = bm25_search(query, top_k=BM25_TOP_K)
    bm25_ranked = [(doc, rank) for rank, doc in enumerate(bm25_results)]

    # 3. RRF 融合：以 page_content 为 key 聚合两路分数
    rrf_scores = {}  # {content: {"score": float, "doc": Document}}
    for doc, rank in vec_ranked:
        key = doc.page_content
        rrf_scores.setdefault(key, {"score": 0.0, "doc": doc})
        rrf_scores[key]["score"] += 1.0 / (RRF_K + rank + 1)
    for doc, rank in bm25_ranked:
        key = doc.page_content
        rrf_scores.setdefault(key, {"score": 0.0, "doc": doc})
        rrf_scores[key]["score"] += 1.0 / (RRF_K + rank + 1)

    # 4. 按 RRF 分数降序，取 top_k
    merged = sorted(rrf_scores.values(), key=lambda x: x["score"], reverse=True)
    final_docs = []
    for item in merged[:RETRIEVE_TOP_K]:
        doc = item["doc"]
        doc.metadata["rrf_score"] = round(item["score"], 4)
        final_docs.append(doc)

    print(f"📊 混合检索：向量 {len(vec_ranked)} 条 + BM25 {len(bm25_ranked)} 条"
          f" → RRF 融合后保留 {len(final_docs)} 条")
    return final_docs


def search(query):
    """统一检索入口：根据开关选择混合检索或纯向量检索"""
    if ENABLE_HYBRID_SEARCH:
        return hybrid_search(query)
    return search_with_scores(query)
