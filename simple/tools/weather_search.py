# tools/weather_search.py
# ========== 天气查询工具（Tool 层最小单元）==========
#
# 职责：查询指定城市的实时天气情况。
#   - weather_search (@tool 版)：给 LLM/ReAct bind_tools 用；
#   - weather_search_impl (裸函数版)：给 WeatherSkill / 其他业务层直接调用，
#     结构化返回 dict（字段齐全，便于 Skill 格式化输出），异常返回错误 dict。
#
# 数据源：wttr.in（免费、无需 Key、支持中文城市名和中文天气描述）
# 失败策略：所有异常均转成 {"error": "..."} dict，不向外抛错，调用方统一判断 ok 字段即可。

from __future__ import annotations

import requests
from urllib.parse import quote
from langchain_core.tools import tool


# ===================== 英文天气描述 → 中文兜底翻译表 =====================
# wttr.in 的 lang_zh 字段在部分天气状况下可能为空，或直接返回英文，
# 这时用翻译表做最后一层兜底，保证给用户的永远是中文描述。
# 覆盖常见 50+ 种 OpenWeatherMap / wttr.in 返回值。
_WEATHER_EN_TO_ZH = {
    # ---- 晴 / 云 ----
    "Sunny": "晴", "Clear": "晴", "Clear sky": "晴",
    "Partly cloudy": "多云", "Partly Cloudy": "多云",
    "Cloudy": "阴天", "Overcast": "阴", "Mist": "薄雾",
    "Fog": "雾", "Freezing fog": "冻雾",
    # ---- 雨 ----
    "Patchy rain nearby": "局部小雨",
    "Patchy rain possible": "可能有小雨",
    "Light rain": "小雨", "Moderate rain": "中雨",
    "Heavy rain": "大雨", "Torrential rain shower": "暴雨",
    "Light rain shower": "小阵雨", "Moderate or heavy rain shower": "中到大阵雨",
    "Moderate rain at times": "间歇性中雨",
    "Heavy rain at times": "间歇性大雨",
    "Patchy light rain": "局部小雨",
    "Patchy light rain in area with thunder": "局部小雷阵雨",
    "Moderate or heavy rain in area with thunder": "中到大雷阵雨",
    # ---- 雷 / 电 ----
    "Thundery outbreaks possible": "可能有雷暴",
    "Patchy light rain with thunder": "小雷阵雨",
    "Moderate or heavy rain with thunder": "中到大雷阵雨",
    # ---- 雪 / 冰 ----
    "Patchy snow possible": "可能有小雪",
    "Light snow": "小雪", "Moderate snow": "中雪", "Heavy snow": "大雪",
    "Blizzard": "暴风雪", "Snow": "雪",
    "Patchy light snow": "局部小雪",
    "Light snow showers": "小阵雪", "Moderate or heavy snow showers": "中到大阵雪",
    "Sleet": "雨夹雪", "Light sleet": "小雨夹雪", "Moderate or heavy sleet": "中到大雨夹雪",
    "Light freezing rain": "小冻雨", "Moderate or heavy freezing rain": "中到大冻雨",
    "Ice pellets": "冰雹", "Light showers of ice pellets": "小冰雹",
    "Moderate or heavy showers of ice pellets": "中到大冰雹",
    # ---- 其他 ----
    "Drizzle": "毛毛雨", "Light drizzle": "毛毛雨",
    "Freezing drizzle": "冻毛毛雨", "Heavy freezing drizzle": "强冻毛毛雨",
    "Haze": "霾", "Sand": "沙尘", "Dust": "浮尘",
    "Smoke": "烟雾", "Ash": "火山灰",
    "Squalls": "飑线", "Tornado": "龙卷风",
}


def _translate_weather_desc(desc: str) -> str:
    """英文天气描述 → 中文，查不到就原样返回（可能已经是中文）。"""
    if not desc:
        return "未知"
    s = desc.strip()
    # 已经是中文（包含任意中文汉字）→ 直接返回
    if any("\u4e00" <= ch <= "\u9fff" for ch in s):
        return s
    return _WEATHER_EN_TO_ZH.get(s, s)


# ===================== 对外导出：裸函数版（结构化返回，推荐） =====================

def weather_search_impl(city: str) -> dict:
    """查询指定城市的实时天气（结构化返回）。

    返回 dict：
      {
        "ok": bool,            # True=成功, False=失败
        "city": str,           # 原始城市名（清洗后）
        "temp_c": str,         # 当前温度（摄氏度），失败时 "N/A"
        "feels_c": str,        # 体感温度（摄氏度），失败时 "N/A"
        "desc_zh": str,        # 中文天气描述（多云/小雨...），失败时 "未知"
        "humidity": str,       # 相对湿度 %，失败时 "N/A"
        "wind_kmh": str,       # 风速 km/h，失败时 "N/A"
        "wind_dir": str,       # 风向 16 点位（如 NNE / SW），失败时 ""
        "text": str,           # 格式化后的中文文本段落（直接可展示给用户）
        "error": str | None,   # ok=False 时为错误原因，ok=True 时 None
      }
    """
    city_clean = (city or "").strip()
    base_fail = {
        "ok": False, "city": city_clean,
        "temp_c": "N/A", "feels_c": "N/A", "desc_zh": "未知",
        "humidity": "N/A", "wind_kmh": "N/A", "wind_dir": "",
        "text": "", "error": None,
    }
    if not city_clean:
        base_fail["error"] = "城市名为空，请输入具体城市名（如「广州」「上海」）"
        base_fail["text"] = "请告诉我具体的城市名（例如：广州、上海、北京），我再帮你查天气～"
        return base_fail

    try:
        city_encoded = quote(city_clean)
        url = f"https://wttr.in/{city_encoded}?format=j1&lang=zh"
        # User-Agent 避免某些 CDN 拦截脚本请求
        resp = requests.get(
            url,
            timeout=10,
            headers={
                "Accept-Language": "zh-CN,zh;q=0.9",
                "User-Agent": "Mozilla/5.0 (CodingAgent WeatherSkill)",
            },
        )
        # wttr.in 对不认识的城市会 404 但也可能返回 HTML，这里双重校验
        if resp.status_code == 404:
            base_fail["error"] = f"未找到城市「{city_clean}」的天气数据，请确认城市名是否正确"
            base_fail["text"] = (
                f"抱歉，没有查到「{city_clean}」的天气信息。"
                f"请确认城市名（省份+城市可提高匹配率，如「广东广州」），稍后再试～"
            )
            return base_fail
        resp.raise_for_status()
        data = resp.json()

        current = (data.get("current_condition") or [{}])[0]
        temp = current.get("temp_C") or "N/A"
        feels = current.get("FeelsLikeC") or "N/A"

        desc = "未知"
        lang_zh_list = current.get("lang_zh") or []
        if lang_zh_list and isinstance(lang_zh_list, list):
            desc = lang_zh_list[0].get("value") or desc
        if desc in ("未知", ""):
            weather_desc_list = current.get("weatherDesc") or []
            if weather_desc_list and isinstance(weather_desc_list, list):
                desc = weather_desc_list[0].get("value") or desc
        # 最后一层兜底：英文描述 → 中文翻译表（50+ 常见项覆盖）
        desc = _translate_weather_desc(desc)

        humidity = current.get("humidity") or "N/A"
        wind = current.get("windspeedKmph") or "N/A"
        wind_dir = current.get("winddir16Point") or ""

        # 穿衣建议（纯规则，不依赖 LLM，秒级给出）
        try:
            temp_num = int(temp)
            if temp_num >= 30:
                cloth = "天气炎热，建议穿短袖、短裤/裙，注意防晒补水 🧴"
            elif temp_num >= 24:
                cloth = "温度舒适，穿薄长袖或 T 恤即可～"
            elif temp_num >= 16:
                cloth = "天气偏凉，建议加一件薄外套或卫衣。"
            elif temp_num >= 8:
                cloth = "气温较低，推荐穿厚外套/毛衣，注意保暖。"
            else:
                cloth = "天气寒冷，请穿羽绒服/厚棉服，做好防寒 ❄️"
        except (TypeError, ValueError):
            cloth = ""

        rain_hint = ""
        if any(k in desc for k in ("雨", "雷", "阵雨", "毛毛雨", "小雨", "大雨", "暴雨")):
            rain_hint = "，出门记得带伞 ☔️"

        text = (
            f"🌤 【{city_clean}实时天气】\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"  天气状况：{desc}{rain_hint}\n"
            f"  当前温度：{temp} °C（体感 {feels} °C）\n"
            f"  相对湿度：{humidity} %\n"
            f"  风速风向：{wind} km/h  {wind_dir}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            + (f"  👕 穿衣建议：{cloth}\n" if cloth else "")
        )

        return {
            "ok": True, "city": city_clean,
            "temp_c": temp, "feels_c": feels, "desc_zh": desc,
            "humidity": humidity, "wind_kmh": wind, "wind_dir": wind_dir,
            "text": text, "error": None,
        }

    except requests.exceptions.Timeout:
        base_fail["error"] = "请求 wttr.in 超时（>10s）"
        base_fail["text"] = f"查询「{city_clean}」天气超时了，请稍后再试一次～"
        return base_fail
    except requests.exceptions.RequestException as e:
        base_fail["error"] = f"网络错误：{e.__class__.__name__}: {e}"
        base_fail["text"] = (
            f"查询「{city_clean}」天气时网络出问题了（{e.__class__.__name__}），请稍后再试。"
        )
        return base_fail
    except (ValueError, KeyError, IndexError, TypeError) as e:
        base_fail["error"] = f"数据解析失败：{e.__class__.__name__}: {e}"
        base_fail["text"] = (
            f"解析「{city_clean}」的天气数据失败，可能是城市名不规范，"
            f"可以试试加上省份（如「广东广州」）再查一次～"
        )
        return base_fail
    except Exception as e:  # 最后兜底，Skill 层永远不要因为工具异常崩溃
        base_fail["error"] = f"未知错误：{e.__class__.__name__}: {e}"
        base_fail["text"] = f"查询「{city_clean}」天气时出了点小问题，请稍后再试。"
        return base_fail


# ===================== 保留 @tool 版（给 LLM ReAct 用，内部复用 impl） =====================

@tool
def weather_search(city: str) -> str:
    """查询指定城市的实时天气情况。

    适用场景：用户询问某地天气、温度、是否下雨、穿什么衣服等与实时天气相关的问题。

    输入：城市名称（中英文均可，如 "北京"、"上海"、"Tokyo"、"New York"）
    输出：当前温度、体感温度、天气描述、湿度、风速等信息的自然语言描述

    使用建议：
    - 仅用于查询实时天气，不要用于知识库相关问题
    - 城市名越具体越好（如 "杭州" 而非 "浙江"）
    - 查询失败会返回错误提示，可换城市名重试
    """
    return weather_search_impl(city)["text"]
