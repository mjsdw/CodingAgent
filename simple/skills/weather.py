# skills/weather.py
# ========== 天气查询 Skill ==========
#
# 处理场景：用户询问天气、温度、是否下雨、湿度、穿什么衣服等实时天气相关问题。
#
# 设计要点：
#   - 双层城市名提取：
#       1) 规则快路：问题里只有 1 个常见城市名或直接是 "XX 天气" → 正则 + 关键词直接取，零 LLM 开销；
#       2) LLM 兜底：模糊指代 / 多城市 / 省略省名（如"今天浙江下雨吗"）→ 用小 prompt 让 LLM 输出 JSON {"city"}，
#                     避免我们自己维护全国 3000+ 区县字典。
#   - 工具层调用：weather_search_impl（结构化 dict，ok/error/text/... 所有异常已兜底，不会抛错）。
#   - 失败降级：工具返回 ok=False → 把错误 text 原样给用户（含"建议加省份重试"），不再报错。

from __future__ import annotations

import re
from langchain_core.documents import Document

from skills.base import BaseSkill, SkillContext, check_task_control
from core.llm_client import invoke_with_system
from core.utils import parse_llm_json
from core.memory import inject_history_into_prompt
from tools.weather_search import weather_search_impl


# ===================== 1. 规则快路：城市名提取（零 LLM 开销）=====================

# 热门直辖市/省会/计划单列市 + 常见地级市（覆盖 90% 日常提问场景）
# 只放城市名（不放省名），顺序按"被误匹配概率低 → 高"排（例如 "石家庄" 比 "北京" 长，先匹配长词避免短词吞）
_COMMON_CITIES = [
    # 四字及以上（先匹配，避免"上海"吞掉"上海浦东"的浦东词）
    "呼和浩特", "乌鲁木齐", "齐齐哈尔", "哈尔滨", "石家庄", "连云港", "张家港",
    "攀枝花", "六盘水", "景德镇", "三门峡", "张家界", "防城港", "海拉尔",
    # 三字
    "张家口", "秦皇岛", "哈尔滨", "牡丹江", "佳木斯", "马鞍山", "井冈山",
    "攀枝花", "六盘水", "景德镇", "嘉峪关", "青铜峡", "石家庄", "周口店",
    "六盘水", "防城港", "格尔木", "德令哈", "吐鲁番", "阿克苏", "阿勒泰",
    # 直辖市 / 省会 / 副省 / 计划单列
    "北京", "上海", "广州", "深圳", "天津", "重庆", "成都", "杭州", "南京",
    "武汉", "西安", "长沙", "郑州", "济南", "青岛", "沈阳", "大连", "长春",
    "厦门", "福州", "南昌", "合肥", "昆明", "贵阳", "南宁", "海口", "三亚",
    "太原", "呼和浩特", "银川", "兰州", "西宁", "乌鲁木齐", "拉萨", "苏州",
    "无锡", "宁波", "温州", "佛山", "东莞", "珠海", "中山", "惠州", "泉州",
    "烟台", "潍坊", "淄博", "徐州", "常州", "南通", "扬州", "绍兴", "金华",
    "嘉兴", "台州", "保定", "唐山", "洛阳", "南阳", "襄阳", "宜昌", "岳阳",
    "常德", "衡阳", "桂林", "柳州", "遵义", "绵阳", "德阳", "宜宾", "泸州",
    "江门", "汕头", "湛江", "肇庆", "桂林", "乌鲁木齐", "喀什", "银川",
    # 港台
    "香港", "澳门", "台北", "高雄", "台中", "台南", "新竹",
    # 常见海外城市
    "东京", "首尔", "大阪", "京都", "纽约", "伦敦", "巴黎", "悉尼", "新加坡",
    "曼谷", "吉隆坡", "多伦多", "温哥华", "洛杉矶", "旧金山", "芝加哥",
    "波士顿", "西雅图", "莫斯科", "柏林", "罗马", "马德里", "迪拜",
]
# 去重 + 按长度降序（长词优先匹配）
_COMMON_CITIES = sorted(set(_COMMON_CITIES), key=len, reverse=True)

# 从问题里"精确抠"城市的正则：支持「(今天|明天|后天)?XX(市|)?天气|XX气温|XX温度|XX几度|XX下雨|XX穿什么」
_CITY_WEATHER_RE = re.compile(
    r"^(?:今天|明天|后天|今日|明日|昨日|昨天|未来几天|这周|周末)?\s*"
    r"(?P<city>[^，。！？,.!?\s]{1,12}?)"
    r"(?:市|地區|地区)?"
    r"\s*(?:的)?\s*(今天|明天|后天|今日|明日|昨天|昨晚|今晚|现在|实时|当前|近期|未来几天|这周|周末)?"
    r"\s*(天气|气温|温度|几度|下雨|热不热|冷不冷|穿衣|穿什么|湿度|风|刮风|晴|阴|雨|雷|雪|雾|霾)",
    flags=re.IGNORECASE,
)


def _fast_extract_city(question: str) -> str | None:
    """零 LLM 的快速城市提取。

    策略：
      1. 先跑 CITY_WEATHER_RE 精确抠（"广州天气怎么样" → "广州"）。
         抠出来的 city 片段如果正好命中常见城市字典 → 直接返回。
      2. 否则整串扫常见城市字典（按长度降序），匹配到第一个就返回（最左优先）。
      3. 都不命中 → 返回 None，交给 Skill.execute 走 LLM 兜底。
    """
    q = question.strip()
    if not q:
        return None

    m = _CITY_WEATHER_RE.search(q)
    if m:
        city = m.group("city").strip()
        # 1) 片段正好是一个常见城市 → 稳了
        if city in _COMMON_CITIES:
            return city
        # 2) 片段以常见城市开头/结尾（如 "广东广州" → 后两字广州）→ 切一下
        for c in _COMMON_CITIES:
            if city.endswith(c) or city.startswith(c):
                return c
        # 3) 片段只有 1-3 字且完全不含常见省份/方向词 → 大概率是城市名，先返回
        #    （错了也没关系，wttr.in 会 404，我们会提示用户重试）
        if 1 <= len(city) <= 4:
            return city

    # 规则 2：整串扫字典（覆盖 "我明天要去北京玩，那边天气" 这种句子结构）
    for c in _COMMON_CITIES:
        if c in q:
            return c

    return None


# ===================== 2. LLM 兜底：城市名提取（规则失败才调用） =====================

_CITY_EXTRACT_PROMPT = """任务：从用户问题中提取要查询天气的目标城市，只输出 JSON。

要求：
1. 只输出一个目标城市名（中文，具体到地级市/区县级）；
2. 如果问题是模糊指代（如"我在这儿天气怎么样""那边下雨吗"），结合对话历史推断；
   实在无法推断时填 "未知"；
3. 不要省份（如不要"广东省"，直接填"广州"）；
4. 只输出 JSON，不要任何多余解释：
{{"city": "广州"}}

对话历史（用于指代推断，可能为空）：
{history_str}

用户问题：
{question}
"""


def _llm_extract_city(question: str, history: list[dict] | None) -> str:
    """LLM 兜底提取城市名，失败返回空字符串。"""
    history_str = ""
    if history:
        # 只取最近 5 轮，避免 prompt 太长
        recent = history[-5:]
        lines = []
        for h in recent:
            role = "用户" if h.get("role") == "user" else "助手"
            lines.append(f"- {role}: {h.get('content', '')[:100]}")
        history_str = "\n".join(lines)

    prompt = _CITY_EXTRACT_PROMPT.format(history_str=history_str or "（无对话历史）",
                                          question=question)
    try:
        resp = invoke_with_system(prompt)
        data = parse_llm_json(resp.content)
        if not data:
            return ""
        city = str(data.get("city", "")).strip()
        if city and city != "未知":
            return city
    except Exception:
        # 任何异常都不要抛，Skill 会走"请用户告诉我城市名"兜底
        pass
    return ""


# ===================== Skill 本体 =====================

class WeatherSkill(BaseSkill):
    """天气查询技能：调用 wttr.in 免费接口，返回中文天气卡 + 穿衣/带伞建议。"""

    name = "weather"
    description = (
        "天气查询技能，适合用户询问某城市天气、温度、湿度、是否下雨、"
        "穿什么衣服、风速风向等实时天气相关的问题。"
    )

    # ---------------- can_handle：供 RuleRouter 的关键词版本外部使用 ----------------
    def can_handle(self, question: str, ctx: SkillContext) -> float:
        """关键词命中即返回 1.0（明确天气场景），否则 0。"""
        q = question.strip()
        keywords = ["天气", "下雨", "气温", "温度", "几度", "穿什么", "湿度", "风速",
                    "晴", "阴", "雨", "雷", "雪", "雾", "霾", "热不热", "冷不冷",
                    "气候", "降温", "升温"]
        for kw in keywords:
            if kw in q:
                return 1.0
        return 0.0

    # ---------------- execute：提取城市 → 调用工具 → 返回 ----------------
    def execute(self, question: str, ctx: SkillContext) -> tuple[str, list[Document]]:
        print("🌤  [WeatherSkill] 开始处理天气查询")

        # 任务控制检查点（调 LLM/网络前各挂一次）
        check_task_control(ctx, "weather: extract_city")

        # --- 步骤 1：双层提取城市名（快路 1ms，兜底 ~200ms LLM） ---
        city = _fast_extract_city(question)
        if city:
            print(f"📍 [WeatherSkill] 规则快路命中城市：{city}")
        else:
            print("📍 [WeatherSkill] 规则未命中，调用 LLM 提取城市...")
            city = _llm_extract_city(question, getattr(ctx, "history", None))
            if city:
                print(f"📍 [WeatherSkill] LLM 提取到城市：{city}")

        # 城市仍空 → 友好提示用户手动给城市名（不要把空串打给 wttr.in）
        if not city:
            answer = (
                "想查哪个城市的天气呢？告诉我城市名（如「广州」「北京」，"
                "加上省份会更准确，如「广东广州」），我再帮你查～"
            )
            return answer, []

        # --- 步骤 2：调用工具层（永不抛异常，所有失败都在 error dict 里） ---
        check_task_control(ctx, "weather: request_wttr")
        result = weather_search_impl(city)

        if result["ok"]:
            print(f"✅ [WeatherSkill] 查询成功：{result['city']} "
                  f"{result['desc_zh']} {result['temp_c']}°C")
            answer = result["text"]
        else:
            print(f"❌ [WeatherSkill] 查询失败：{result.get('error')}")
            answer = result["text"]

        # 无检索引用片段（天气数据属于实时 API，不是 Document）
        return answer, []
