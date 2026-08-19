# tools/weather_search.py
# ========== 天气查询工具（Tool 层最小单元）==========
#
# 职责：查询指定城市的实时天气情况。
# 当前仅 @tool 版本（给 LLM/ReAct 用），无 structured 版本（暂无 WeatherSkill 需要）。
# 后续实现 WeatherSkill 时可按需添加 weather_search_structured。

import requests
from urllib.parse import quote
from langchain_core.tools import tool


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
    try:
        city_clean = city.strip()
        city_encoded = quote(city_clean)
        url = f"https://wttr.in/{city_encoded}?format=j1&lang=zh"
        resp = requests.get(url, timeout=8, headers={"Accept-Language": "zh-CN,zh;q=0.9"})
        resp.raise_for_status()
        data = resp.json()

        current = data.get("current_condition", [{}])[0]
        temp = current.get("temp_C", "N/A")
        feels = current.get("FeelsLikeC", "N/A")
        desc = (current.get("lang_zh", [{}])[0].get("value")
                or current.get("weatherDesc", [{}])[0].get("value", "未知"))
        humidity = current.get("humidity", "N/A")
        wind = current.get("windspeedKmph", "N/A")
        wind_dir = current.get("winddir16Point", "")

        return (f"城市：{city_clean}\n"
                f"天气：{desc}\n"
                f"温度：{temp}°C（体感 {feels}°C）\n"
                f"湿度：{humidity}%\n"
                f"风速：{wind} km/h {wind_dir}")
    except requests.exceptions.Timeout:
        return f"查询 {city} 天气超时，请稍后重试或换一个城市名"
    except requests.exceptions.RequestException as e:
        return f"查询 {city} 天气失败：{e}"
    except (KeyError, IndexError, ValueError) as e:
        return f"解析 {city} 天气数据失败：{e}（可能是城市名不规范）"
