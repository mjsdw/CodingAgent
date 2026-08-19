# tools/web_search.py
# ========== 联网搜索 Tool ==========
#
# 封装百度搜索，提供双接口：
#   - web_search(query) → str          # @tool 版本（给 LLM bind_tools 用）
#   - web_search_docs(query) → list[Document]  # plain 版本（给 Skill 用）
#
# 两阶段检索：
#   1. 百度搜索 → 获取结果列表（标题 + URL + 摘要）
#   2. 抓取网页正文 → 用 BeautifulSoup 提取 <p> 标签文本

from langchain_core.documents import Document
from langchain_core.tools import tool

from config import (
    WEB_SEARCH_MAX_RESULTS,
    WEB_FETCH_TIMEOUT,
    WEB_CONTENT_MAX_CHARS,
)

# 请求头（模拟浏览器，避免被反爬拦截）
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


@tool
def web_search(query: str) -> str:
    """联网搜索互联网，获取本地知识库之外的实时信息。

    Args:
        query: 搜索关键词
    Returns:
        搜索结果摘要文本
    """
    docs = web_search_docs(query)
    if not docs:
        return "未找到相关结果"
    parts = []
    for i, doc in enumerate(docs, 1):
        title = doc.metadata.get("title", "")
        source = doc.metadata.get("source", "")
        parts.append(f"[{i}] {title}\n来源: {source}\n{doc.page_content}")
    return "\n\n".join(parts)


def web_search_docs(query: str) -> list[Document]:
    """百度搜索 + 抓取网页正文，返回 Document 列表。

    两阶段：
      1. 百度搜索 → 获取 URL 列表
      2. 逐个抓取 URL → 提取正文

    :param query: 搜索关键词
    :return: Document 列表，page_content 为网页正文
    """
    import requests
    from bs4 import BeautifulSoup

    try:
        # ---------- 阶段 1：百度搜索获取 URL 列表 ----------
        url = "https://www.baidu.com/s"
        params = {"wd": query, "rn": str(WEB_SEARCH_MAX_RESULTS)}

        resp = requests.get(url, params=params, headers=_HEADERS, timeout=10)
        resp.encoding = "utf-8"

        soup = BeautifulSoup(resp.text, "html.parser")
        results: list[tuple[str, str]] = []  # [(title, url), ...]

        for item in soup.select("div.result, div.c-container"):
            if len(results) >= WEB_SEARCH_MAX_RESULTS:
                break
            title_tag = item.select_one("h3 a")
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)
            href = title_tag.get("href", "")
            if title and href:
                results.append((title, href))

        if not results:
            print(f"🌐 [web_search] 百度搜索 '{query}' 无结果")
            return []

        print(f"🌐 [web_search] 百度搜索 '{query}' 返回 {len(results)} 条，开始抓取正文...")

        # ---------- 阶段 2：逐个抓取网页正文 ----------
        docs: list[Document] = []
        for title, href in results:
            try:
                page_resp = requests.get(href, headers=_HEADERS, timeout=WEB_FETCH_TIMEOUT, allow_redirects=True)
                page_resp.encoding = page_resp.apparent_encoding or "utf-8"
                page_soup = BeautifulSoup(page_resp.text, "html.parser")

                # 移除脚本/样式/导航等干扰元素
                for tag in page_soup(["script", "style", "nav", "header", "footer", "aside", "iframe"]):
                    tag.decompose()

                # 提取正文：<article> > <main> > 所有 <p> 标签拼接
                content = ""
                article = page_soup.find("article") or page_soup.find("main")
                if article:
                    paragraphs = article.find_all("p")
                else:
                    paragraphs = page_soup.find_all("p")

                for p in paragraphs:
                    text = p.get_text(strip=True)
                    if len(text) >= 20:  # 过滤太短的碎片
                        content += text + "\n"
                    if len(content) >= WEB_CONTENT_MAX_CHARS:
                        break

                content = content[:WEB_CONTENT_MAX_CHARS].strip()
                if not content:
                    # 正文提取失败 → 用搜索摘要兜底
                    abstract_tag = page_soup.find("meta", {"name": "description"})
                    content = abstract_tag.get("content", "") if abstract_tag else title

                docs.append(Document(
                    page_content=content,
                    metadata={
                        "source": page_resp.url or href,
                        "title": title,
                        "type": "web",
                    },
                ))
                print(f"  ✅ [{len(docs)}] {title[:30]}... ({len(content)} 字符)")

            except Exception as e:
                print(f"  ❌ 抓取失败: {title[:30]}... → {e}")
                continue

        print(f"🌐 [web_search] 成功抓取 {len(docs)}/{len(results)} 个网页正文")
        return docs

    except Exception as e:
        print(f"⚠️ [web_search] 联网搜索失败：{e}")
        return []
