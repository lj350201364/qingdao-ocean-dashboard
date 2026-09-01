import gzip
import hmac
import http.cookiejar
import http.server
import io
import json
import mimetypes
import os
import re
import shutil
import socketserver
import sys
import threading
import time
import datetime
import urllib.error
import urllib.parse
import urllib.request

try:
    from ocean_notifications import NotificationManager, conservative_wave_height, wind_level, wind_relation
except Exception as notification_import_error:
    NotificationManager = None
    conservative_wave_height = None
    wind_level = None
    wind_relation = None
    print("【通知模块加载失败】", repr(notification_import_error))

try:
    import webview
except Exception:
    webview = None


PORT = int(os.environ.get("PORT", "5051"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
BEACH_NAME = "栈桥浴场"
GLOBAL_TIDE_SITE_CODE = "T046"
GLOBAL_TIDE_SITE_NAME = "青岛"

# 栈桥浴场附近坐标。如需更精确，可按实际点位微调。
WEATHER_LATITUDE = 36.061
WEATHER_LONGITUDE = 120.326

server = None
notification_manager = None
notification_cache_lock = threading.RLock()
notification_data_cache = {
    "tide_date": None,
    "tide_days": None,
    "tide_fetched_at": 0,
    "weather": None,
    "weather_fetched_at": 0,
    "wave": None,
    "wave_fetched_at": 0,
}

cache = {
    "tide_table": None,
    "tide_chart": [],
    "wave": None,
    "offshore_wave": None,
    "offshore_wave_tomorrow": None,
    "alarm": [],
    "sd_alarm": [],
    "cma_alarm": [],
    "weather": None,
    "refresh": {
        "tide_table": "--",
        "tide_chart": "--",
        "wave": "--",
        "offshore_wave": "--",
        "offshore_wave_tomorrow": "--",
        "weather": "--",
        "alarm": "--",
        "sd_alarm": "--",
        "cma_alarm": "--",
    },
}


def _tz():
    """固定返回 Asia/Shanghai 时区，避免服务器时区不一致。"""
    return datetime.timezone(datetime.timedelta(hours=8))

def _now():
    return datetime.datetime.now(_tz())


def extract_time_from_title(title, default_year=None):
    """从预警标题中提取发布时间，返回格式化后的时间字符串 (YYYY-MM-DD HH:MM 或 YYYY-MM-DD)。
    支持的格式示例：
    - 2026年07月11日16时30分
    - 2026年07月11日16时
    - 2026年07月11日
    - 2026/07/11 16:30
    - 2026-07-11 16:30
    - 07月11日16时30分（需补全年份）
    - 7月11日16时（需补全年份）
    提取失败返回空字符串。
    """
    if not title:
        return ""
    s = str(title).strip()
    if not s:
        return ""
    if default_year is None:
        default_year = datetime.datetime.now().year

    # 模式1: 2026年07月11日16时30分 / 2026年7月11日16时30分
    m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日(\d{1,2})时(\d{1,2})分', s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d} {int(m.group(4)):02d}:{int(m.group(5)):02d}"

    # 模式2: 2026年07月11日16时
    m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日(\d{1,2})时', s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d} {int(m.group(4)):02d}:00"

    # 模式3: 2026年07月11日
    m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    # 模式4: 2026/07/11 16:30 或 2026-07-11 16:30
    m = re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{1,2})', s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d} {int(m.group(4)):02d}:{int(m.group(5)):02d}"

    # 模式5: 2026/07/11 或 2026-07-11
    m = re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    # 模式6: 07月11日16时30分（无年份，补全默认年份）
    m = re.search(r'(\d{1,2})月(\d{1,2})日(\d{1,2})时(\d{1,2})分', s)
    if m:
        return f"{int(default_year):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d} {int(m.group(3)):02d}:{int(m.group(4)):02d}"

    # 模式7: 07月11日16时（无年份，补全默认年份）
    m = re.search(r'(\d{1,2})月(\d{1,2})日(\d{1,2})时', s)
    if m:
        return f"{int(default_year):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d} {int(m.group(3)):02d}:00"

    # 模式8: 07月11日（无年份，补全默认年份）
    m = re.search(r'(\d{1,2})月(\d{1,2})日', s)
    if m:
        return f"{int(default_year):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"

    return ""


def complete_alarm_time(base_time, title, default_year=None):
    """补全预警发布时间：如果基础时间只有日期没有时分，尝试从标题中提取更完整的时间。
    优先使用标题中包含时分的完整时间（日期需匹配或base_time为空）。
    返回格式化后的时间字符串。
    """
    base_time = (base_time or "").strip()
    # 如果已有完整时间（包含冒号或空格+时间），直接返回
    if base_time and (":" in base_time or " " in base_time and len(base_time) > 11):
        return base_time

    # 从标题中提取完整时间
    title_time = extract_time_from_title(title, default_year)
    if not title_time:
        return base_time  # 标题中也没提取到，返回原值

    # 如果标题提取的时间包含时分，则优先使用
    if ":" in title_time:
        # 如果 base_time 有日期，检查日期是否匹配（统一格式后比较）
        if base_time and len(base_time) >= 10:
            base_date = base_time[:10].replace("/", "-").replace(".", "-")
            title_date = title_time[:10]
            if base_date == title_date:
                return title_time  # 日期匹配，用更完整的
            else:
                return base_time  # 日期不匹配，保留原值
        else:
            return title_time  # base_time 为空或不完整，直接用标题提取的

    return base_time or title_time


def now_hm(target_date=None):
    n = _now()
    if target_date and target_date != today_ymd():
        try:
            parts = target_date.split("-")
            return f"{int(parts[1]):02d}-{int(parts[2]):02d} 数据"
        except Exception:
            pass
    return f"{n.month:02d}-{n.day:02d} {n.hour:02d}:{n.minute:02d}"


def today_ymd():
    return _now().strftime("%Y-%m-%d")


def date_ymd(offset=0):
    return (_now() + datetime.timedelta(days=offset)).strftime("%Y-%m-%d")


def normalize_date(value):
    if not value:
        return today_ymd()
    if value in ("today", "0"):
        return today_ymd()
    if value in ("tomorrow", "1"):
        return date_ymd(1)
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        return today_ymd()


def timestamp_ms():
    return str(int(time.time() * 1000))


def new_empty_opener():
    """每次请求使用独立 CookieJar，减少跨接口缓存串扰。"""
    jar = http.cookiejar.CookieJar()
    handler = urllib.request.HTTPCookieProcessor(jar)
    return urllib.request.build_opener(handler)


def base_headers():
    # 不声明 br，避免 Python 标准库无法解码 brotli 内容。
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0.0 Safari/537.36",
        "Referer": "http://www.qdmf.org.cn/Index.aspx",
        "Origin": "http://www.qdmf.org.cn",
        "Accept": "application/json,text/html;charset=UTF-8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }


def read_response(resp):
    raw = resp.read()
    encoding = resp.getheader("Content-Encoding", "")
    if "gzip" in encoding:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
            return gz.read().decode("utf-8", errors="ignore")
    return raw.decode("utf-8", errors="ignore")


def fetch_text(url, headers=None, timeout=15):
    """GET 请求返回文本内容"""
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_json(url, headers=None, timeout=25):
    opener = new_empty_opener()
    req = urllib.request.Request(url, headers=headers or base_headers())
    res = opener.open(req, timeout=timeout)
    text = read_response(res).strip()
    if not text:
        raise ValueError("接口返回空内容")
    # 部分接口返回非标准 JSON（值用单引号包裹），先修复再解析
    if "'" in text:
        text = re.sub(r":'([^']*)'", r':"\1"', text)
    return json.loads(text)


def extract_offshore_wave(html):
    if not html:
        return None
    normalized_html = html.replace("&nbsp;", " ").replace("&#160;", " ")
    plain_text = re.sub(r"<[^>]+>", "", normalized_html)
    compact_text = re.sub(r"\s+", "", plain_text)
    patterns = [
        r"青岛近海(?:<[^>]+>|\s)*浪高(?:<[^>]+>|\s)*([0-9]+(?:\.[0-9]+)?(?:\s*-\s*[0-9]+(?:\.[0-9]+)?)?)\s*米",
        r"青岛近海([0-9]+(?:\.[0-9]+)?(?:-[0-9]+(?:\.[0-9]+)?)?)米",
    ]
    for source in (normalized_html, compact_text):
        for pattern in patterns:
            match = re.search(pattern, source, re.I)
            if match:
                return re.sub(r"\s+", "", match.group(1))
    return None


def post_global_tide_api(command, data, timeout=25):
    api_url = "https://global-tide.nmdis.org.cn/Api/Service.ashx"
    payload = {
        "Server": "User",
        "Command": command,
        "Data": data,
    }
    form = urllib.parse.urlencode({
        "ApiRequest": json.dumps(payload, ensure_ascii=False)
    }).encode("utf-8")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0.0 Safari/537.36",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json,text/javascript,*/*;q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://global-tide.nmdis.org.cn",
        "Referer": "https://global-tide.nmdis.org.cn/Site/Site.html",
        "X-Requested-With": "XMLHttpRequest",
    }
    req = urllib.request.Request(api_url, data=form, headers=headers)
    res = urllib.request.urlopen(req, timeout=timeout)
    text = res.read().decode("utf-8", errors="ignore").strip()
    if not text:
        raise ValueError("全球潮汐平台返回空内容")
    result = json.loads(text)
    if not result.get("State"):
        raise ValueError(result.get("Message") or "全球潮汐平台接口返回失败")
    data = result.get("Data")
    if isinstance(data, str):
        data = re.sub(r":\s*(\d{16,})", r':"\1"', data)
        result["Data"] = json.loads(data)
    return result


def classify_extrema(extrema):
    sorted_items = sorted(extrema, key=lambda x: x["minute"])
    total = len(sorted_items)
    for i, item in enumerate(sorted_items):
        prev_h = sorted_items[i - 1]["height"] if i > 0 else None
        next_h = sorted_items[i + 1]["height"] if i < total - 1 else None
        h = item["height"]
        if prev_h is None and next_h is not None:
            item["type"] = "低潮" if h < next_h else "满潮"
        elif next_h is None and prev_h is not None:
            item["type"] = "低潮" if h < prev_h else "满潮"
        elif prev_h is not None and next_h is not None:
            item["type"] = "低潮" if h <= prev_h and h <= next_h else "满潮"
        else:
            item["type"] = "潮位"
    return sorted_items


def build_qingdao_tide_table(site, report, sub, extrema, target_date):
    highs = [x for x in extrema if x["type"] == "满潮"]
    lows = [x for x in extrema if x["type"] == "低潮"]

    def pick(items, index, key):
        if index >= len(items):
            return "-"
        value = items[index][key]
        if key == "height":
            return str(value)
        return value

    row = {
        "SEABEACH": site.get("Name") or GLOBAL_TIDE_SITE_NAME,
        "FORECASTDATE": target_date,
        "FIRSTHIGHTIME": pick(highs, 0, "time"),
        "FIRSTHIGHLEVEL": pick(highs, 0, "height"),
        "SECONDHIGHTIME": pick(highs, 1, "time"),
        "SECONDHEIGHTLEVEL": pick(highs, 1, "height"),
        "FIRSTLOWTIME": pick(lows, 0, "time"),
        "FIRSTLOWLEVEL": pick(lows, 0, "height"),
        "SECONDLOWTIME": pick(lows, 1, "time"),
        "SECONDLOWLEVEL": pick(lows, 1, "height"),
        "SOURCE": "全球潮汐预报服务平台",
        "BENCHMARK": report.get("Benchmark", ""),
        "COORDINATE": report.get("Coordinate", ""),
    }
    return {"rows": [row], "site": site, "extrema": extrema}


def fetch_qingdao_tide_data(target_date=None):
    target_date = normalize_date(target_date)
    result = post_global_tide_api("GetData", {
        "code": GLOBAL_TIDE_SITE_CODE,
        "date": target_date,
    })
    body = result.get("Data") or {}
    site = body.get("Site") or {}
    report = body.get("Data") or {}
    sub = body.get("SubData") or {}
    n = _now()
    year = int(report.get("Year") or n.year)
    month = int(report.get("Month") or n.month)
    day = int(sub.get("Day") or n.day)
    date_text = f"{year}/{month}/{day}"

    chart = []
    for hour in range(24):
        val = sub.get(f"a{hour}")
        if val is None:
            continue
        chart.append({
            "TIDETIME": str(hour),
            "TIDEHEIGHT": str(val),
            "TIDEDATE": f"{date_text} 0:00:00",
            "SOURCE": "global-tide",
            "POINT_TYPE": "hour",
        })

    extrema = []
    for i in range(6):
        t = sub.get(f"cs{i}")
        h = sub.get(f"cg{i}")
        if t and h is not None:
            try:
                hh, mm = [int(x) for x in str(t).split(":")[:2]]
                minute = hh * 60 + mm
            except Exception:
                continue
            extrema.append({"time": t, "height": h, "minute": minute})
    extrema = classify_extrema(extrema)

    for item in extrema:
        chart.append({
            "TIDETIME": item["time"],
            "TIDEHEIGHT": str(item["height"]),
            "TIDEDATE": f"{date_text} 0:00:00",
            "SOURCE": "global-tide",
            "POINT_TYPE": "extrema",
            "EXTREMA_TYPE": item["type"],
        })

    site_info = {
        "name": site.get("Name") or GLOBAL_TIDE_SITE_NAME,
        "code": site.get("Code") or GLOBAL_TIDE_SITE_CODE,
        "coordinate": report.get("Coordinate", ""),
        "benchmark": report.get("Benchmark", ""),
    }

    return {
        "chart": chart,
        "table": build_qingdao_tide_table(site, report, sub, extrema, target_date),
        "site": site_info,
        "extrema": extrema,
        "sourceTime": result.get("ResultTime", "--"),
    }


def json_payload(success, data=None, update_time=None, msg="", **extra):
    payload = {
        "success": success,
        "data": data,
        "updateTime": update_time or now_hm(),
        "msg": msg,
    }
    payload.update(extra)
    return payload


def wind_direction_text(degree):
    if degree is None:
        return "--"
    try:
        degree = float(degree) % 360
    except (TypeError, ValueError):
        return "--"
    names = ["北风", "东北风", "东风", "东南风", "南风", "西南风", "西风", "西北风"]
    return names[int((degree + 22.5) // 45) % 8]


def weather_code_text(code):
    mapping = {
        0: "晴",
        1: "大部晴朗",
        2: "局部多云",
        3: "阴",
        45: "雾",
        48: "雾凇",
        51: "小毛毛雨",
        53: "毛毛雨",
        55: "较强毛毛雨",
        61: "小雨",
        63: "中雨",
        65: "大雨",
        71: "小雪",
        73: "中雪",
        75: "大雪",
        80: "阵雨",
        81: "较强阵雨",
        82: "强阵雨",
        95: "雷暴",
        96: "雷暴伴冰雹",
        99: "强雷暴伴冰雹",
    }
    return mapping.get(code, "未知天气")


def _notification_fetch_weather():
    params = urllib.parse.urlencode({
        "latitude": WEATHER_LATITUDE,
        "longitude": WEATHER_LONGITUDE,
        "current": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
        "timezone": "Asia/Shanghai",
    })
    raw = fetch_json(
        f"https://api.open-meteo.com/v1/forecast?{params}",
        headers={"User-Agent": "OceanWindow-NotificationEngine/1.0", "Accept": "application/json"},
        timeout=18,
    )
    current = raw.get("current") or {}
    return {
        "degree": numeric_or_none(current.get("wind_direction_10m")),
        "speed_kmh": numeric_or_none(current.get("wind_speed_10m")),
        "gust_kmh": numeric_or_none(current.get("wind_gusts_10m")),
        "source_time": current.get("time", "--"),
    }


def _notification_fetch_wave():
    target = f"http://www.qdmf.org.cn/Ajax/SeaArea24HSumWave.ashx?date={today_ymd()}&_t={timestamp_ms()}"
    result = fetch_json(target, timeout=18)
    rows = []
    if isinstance(result, dict):
        rows = result.get("rows") or result.get("Rows") or result.get("data") or result.get("Data") or []
    elif isinstance(result, list):
        rows = result
    if isinstance(rows, dict):
        rows = [rows]
    row = pick_named_row(rows, ["青岛近海", "青岛近岸", "青岛"])
    explicit = row.get("SA24HWFQDOFFSHOREWAVEHEIGHT") if isinstance(row, dict) else None
    raw_value = explicit or extract_wave_from_row(row) or extract_wave_from_row(result if isinstance(result, dict) else {})
    height = conservative_wave_height(raw_value) if conservative_wave_height else None
    if height is None:
        raise ValueError("通知规则无法解析青岛近海浪高")
    return {"height_m": height, "raw": str(raw_value), "source": "青岛海洋预报"}


def numeric_or_none(value):
    if value is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else None


def _notification_tide_snapshot(tide_days, tide_date, current=None):
    """按潮汐缓存的基准日期计算，避免跨午夜时相对 offset 突然错位。"""
    now = current or _now()
    base_date = datetime.datetime.strptime(tide_date, "%Y-%m-%d").date()
    day_offset = (now.date() - base_date).days
    now_minute = day_offset * 1440 + now.hour * 60 + now.minute
    extrema = []
    for offset, data in tide_days.items():
        for item in data.get("extrema") or []:
            extrema.append({
                "minute": int(item.get("minute", 0)) + offset * 1440,
                "height": numeric_or_none(item.get("height")),
                "type": item.get("type", "潮位"),
                "time": item.get("time", "--"),
                "offset": offset,
            })
    extrema = sorted([x for x in extrema if x["height"] is not None], key=lambda x: x["minute"])
    previous_items = [x for x in extrema if x["minute"] <= now_minute]
    future_items = [x for x in extrema if x["minute"] > now_minute]
    if not previous_items or not future_items:
        raise ValueError("潮汐极值不足，无法计算通知潮段")
    previous, following = previous_items[-1], future_items[0]
    total = max(1, following["minute"] - previous["minute"])
    progress = max(0, min(100, round((now_minute - previous["minute"]) / total * 100)))
    rising = following["type"] in ("高潮", "满潮") or following["height"] > previous["height"]
    chart = []
    for offset, data in tide_days.items():
        for point in data.get("chart") or []:
            if point.get("POINT_TYPE") != "hour":
                continue
            hour = numeric_or_none(point.get("TIDETIME"))
            height = numeric_or_none(point.get("TIDEHEIGHT"))
            if hour is not None and height is not None:
                chart.append({"minute": offset * 1440 + int(hour * 60), "height": height})
    chart.sort(key=lambda x: x["minute"])
    level = None
    for index in range(1, len(chart)):
        left, right = chart[index - 1], chart[index]
        if left["minute"] <= now_minute <= right["minute"]:
            ratio = (now_minute - left["minute"]) / max(1, right["minute"] - left["minute"])
            level = round(left["height"] + (right["height"] - left["height"]) * ratio)
            break
    previous_date = (base_date + datetime.timedelta(days=previous["offset"])).isoformat()
    following_date = (base_date + datetime.timedelta(days=following["offset"])).isoformat()
    # 使用潮段两端的绝对日期时间。相对日期 offset 会在午夜从 0/1 变成
    # -1/0，导致同一潮段生成不同 ID 并重复通知。
    segment_id = f"{previous_date}T{previous['time']}->{following_date}T{following['time']}"
    return {
        "phase": "rising" if rising else "falling",
        "phase_text": "涨潮中" if rising else "退潮中",
        "progress": progress,
        "level_cm": level,
        "segment_id": segment_id,
        "previous_extrema": previous,
        "next_extrema": following,
    }


def notification_snapshot_provider(config):
    settings = config.get("settings") or {}
    now_ts = time.time()
    today = today_ymd()
    errors = []
    with notification_cache_lock:
        if notification_data_cache["tide_date"] != today or not notification_data_cache["tide_days"]:
            tide_days = {}
            for offset in (-1, 0, 1):
                try:
                    tide_days[offset] = fetch_qingdao_tide_data(date_ymd(offset))
                except Exception as exc:
                    errors.append(f"tide[{offset}]={repr(exc)}")
            if 0 in tide_days and len(tide_days) >= 2:
                notification_data_cache["tide_date"] = today
                notification_data_cache["tide_days"] = tide_days
                notification_data_cache["tide_fetched_at"] = now_ts
        if now_ts - notification_data_cache["weather_fetched_at"] >= 600 or not notification_data_cache["weather"]:
            try:
                notification_data_cache["weather"] = _notification_fetch_weather()
                notification_data_cache["weather_fetched_at"] = now_ts
            except Exception as exc:
                errors.append(f"weather={repr(exc)}")
        if now_ts - notification_data_cache["wave_fetched_at"] >= 900 or not notification_data_cache["wave"]:
            try:
                notification_data_cache["wave"] = _notification_fetch_wave()
                notification_data_cache["wave_fetched_at"] = now_ts
            except Exception as exc:
                errors.append(f"wave={repr(exc)}")
        tide_days = notification_data_cache["tide_days"]
        tide_date = notification_data_cache["tide_date"]
        weather = notification_data_cache["weather"]
        wave = notification_data_cache["wave"]
        fetched_at = {
            "tide": notification_data_cache["tide_fetched_at"],
            "weather": notification_data_cache["weather_fetched_at"],
            "wave": notification_data_cache["wave_fetched_at"],
        }
    max_age = numeric_or_none(settings.get("max_data_age_minutes")) or 15
    source_age_minutes = {
        name: round(max(0, now_ts - fetched_time) / 60, 1) if fetched_time else None
        for name, fetched_time in fetched_at.items()
    }
    tide_available = bool(tide_days) and tide_date == today
    weather_available = bool(weather) and source_age_minutes["weather"] is not None and source_age_minutes["weather"] <= max_age
    wave_available = bool(wave) and source_age_minutes["wave"] is not None and source_age_minutes["wave"] <= max_age
    unavailable_sources = []
    if not tide_available:
        unavailable_sources.append("tide")
    if not weather_available:
        unavailable_sources.append("weather")
    if not wave_available:
        unavailable_sources.append("wave")

    # 单个实时数据源异常时，只让依赖该数据的规则无法匹配；不要连带阻止
    # 潮汐等其他数据仍然完整的规则。过期缓存会被置空，避免误触发。
    snapshot_now = _now()
    tide = _notification_tide_snapshot(tide_days, tide_date, snapshot_now) if tide_available else {
        "phase": None,
        "phase_text": "--",
        "progress": None,
        "level_cm": None,
        "segment_id": None,
        "previous_extrema": None,
        "next_extrema": None,
    }
    weather = weather if weather_available else {}
    wave = wave if wave_available else {"height_m": None, "raw": "--"}
    shore_degree = settings.get("shore_inward_degree")
    degree = weather.get("degree")
    realtime_ages = [
        source_age_minutes[name]
        for name in ("weather", "wave")
        if source_age_minutes[name] is not None
    ]
    snapshot = {
        "generated_at": snapshot_now.isoformat(timespec="seconds"),
        "site": {"name": settings.get("site_name", BEACH_NAME)},
        "tide": tide,
        "wind": {
            "direction": wind_direction_text(degree),
            "degree": degree,
            "speed_kmh": weather.get("speed_kmh"),
            "gust_kmh": weather.get("gust_kmh"),
            "level": wind_level(weather.get("speed_kmh")) if wind_level else "--",
            "relation": wind_relation(degree, shore_degree) if wind_relation else "unknown",
        },
        "wave": wave,
        "data": {
            # 潮汐表是按日期生效的预报数据，一天只需获取一次，不能用其下载时间
            # 判断实时天气/浪高是否过期，否则每天凌晨后都会被错误拦截。
            "age_minutes": max(realtime_ages) if realtime_ages else None,
            "source_age_minutes": source_age_minutes,
            "tide_table_date": tide_date,
            "freshness_basis": ["weather", "wave"],
            "unavailable_sources": unavailable_sources,
            "status": "degraded" if unavailable_sources else "ok",
            "errors": errors,
        },
    }
    return snapshot


class MyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{now_hm()}] {self.address_string()} {fmt % args}")

    def send_header(self, key, val):
        if key in ("X-Frame-Options", "Content-Security-Policy"):
            return
        super().send_header(key, val)

    def set_json_response(self, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json;charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def write_json(self, payload, status=200):
        self.set_json_response(status)
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def query_date(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        return normalize_date((query.get("date") or [""])[0])

    def query_param(self, name):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        return (query.get(name) or [""])[0]

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        routes = {
            "/api/tide": self.handle_tide,
            "/api/tideChart": self.handle_tide_chart,
            "/api/wave": self.handle_wave,
            "/api/offshore_wave": self.handle_offshore_wave,
            "/api/alarm": self.handle_alarm,
            "/api/sd_alarm": self.handle_sd_alarm,
            "/api/sd_alarm_detail": self.handle_sd_alarm_detail,
            "/api/cma_alarm": self.handle_cma_alarm,
            "/api/cma_alarm_detail": self.handle_cma_alarm_detail,
            "/api/weather": self.handle_weather,
            "/api/notification/status": self.handle_notification_status,
            "/api/notification/config": self.handle_notification_config,
            "/api/notification/logs": self.handle_notification_logs,
        }
        if path in routes:
            routes[path]()
            return
        settings_path = os.environ.get("NOTIFICATION_SETTINGS_PATH", "/notification-admin")
        if path in ("/", settings_path):
            self.handle_page()
            return
        if path.startswith("/assets/"):
            self.handle_static_asset(path)
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        self.write_json(json_payload(False, None, "--", "页面不存在"), status=404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        admin_token = os.environ.get("NOTIFICATION_ADMIN_TOKEN", "")
        supplied_token = self.headers.get("X-Notification-Admin-Token", "")
        is_local = self.client_address[0] in ("127.0.0.1", "::1")
        if admin_token:
            authorized = hmac.compare_digest(admin_token, supplied_token)
        else:
            authorized = is_local
        if not authorized:
            self.write_json(json_payload(False, None, "--", "管理令牌不正确，设置操作被拒绝"), status=403)
            return
        if notification_manager is None:
            self.write_json(json_payload(False, None, "--", "通知引擎未加载"), status=503)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1024 * 1024:
                raise ValueError("请求内容为空或过大")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if path == "/api/notification/config":
                data = notification_manager.save_config(body.get("config"), body.get("secrets"))
                self.write_json(json_payload(True, data, now_hm(), "通知配置已保存"))
                return
            if path == "/api/notification/test":
                data = notification_manager.send_test(str(body.get("role", "")))
                self.write_json(json_payload(True, data, now_hm(), "测试消息已发送"))
                return
            self.write_json(json_payload(False, None, "--", "接口不存在"), status=404)
        except Exception as exc:
            self.write_json(json_payload(False, None, "--", f"操作失败：{exc}"), status=400)

    def handle_notification_status(self):
        if notification_manager is None:
            self.write_json(json_payload(False, None, "--", "通知引擎未加载"), status=503)
            return
        self.write_json(json_payload(True, notification_manager.status(), now_hm(), "通知引擎状态"))

    def handle_notification_config(self):
        if notification_manager is None:
            self.write_json(json_payload(False, None, "--", "通知引擎未加载"), status=503)
            return
        try:
            self.write_json(json_payload(True, notification_manager.public_config(), now_hm(), "通知配置"))
        except Exception as exc:
            self.write_json(json_payload(False, None, "--", f"读取通知配置失败：{exc}"), status=500)

    def handle_notification_logs(self):
        if notification_manager is None:
            self.write_json(json_payload(False, None, "--", "通知引擎未加载"), status=503)
            return
        try:
            limit = self.query_param("limit") or "50"
            self.write_json(json_payload(True, notification_manager.logs(limit), now_hm(), "通知日志"))
        except Exception as exc:
            self.write_json(json_payload(False, None, "--", f"读取通知日志失败：{exc}"), status=500)

    def handle_tide(self):
        target_date = self.query_date()
        print(f"\n【青岛高低潮表接口】global-tide {GLOBAL_TIDE_SITE_NAME}({GLOBAL_TIDE_SITE_CODE}) {target_date}")
        try:
            qd = fetch_qingdao_tide_data(target_date)
            data = qd["table"]
            cache["tide_table"] = data
            cache["refresh"]["tide_table"] = now_hm(target_date)
            self.write_json(json_payload(
                True,
                data,
                cache["refresh"]["tide_table"],
                "青岛高低潮表数据",
                site=qd["site"],
                extrema=qd["extrema"],
                sourceTime=qd["sourceTime"],
            ))
        except Exception as e:
            print(f"【青岛高低潮表】异常：{repr(e)}")
            is_tomorrow = target_date != today_ymd()
            if is_tomorrow:
                self.write_json(json_payload(False, None, "--", "暂无明日潮汐表数据", tomorrow_unavailable=True))
            else:
                self.write_json(json_payload(
                    False,
                    cache["tide_table"],
                    cache["refresh"]["tide_table"],
                    "青岛高低潮表接口异常，展示缓存" if cache["tide_table"] else "青岛高低潮表接口异常",
                ))

    def handle_tide_chart(self):
        target_date = self.query_date()
        print(f"\n【青岛潮汐曲线接口】global-tide {GLOBAL_TIDE_SITE_NAME}({GLOBAL_TIDE_SITE_CODE}) {target_date}")
        try:
            qd = fetch_qingdao_tide_data(target_date)
            chart_arr = qd["chart"]
            if not chart_arr:
                raise ValueError("青岛潮汐曲线数据为空")
            cache["tide_chart"] = chart_arr
            cache["refresh"]["tide_chart"] = now_hm(target_date)
            self.write_json(json_payload(
                True,
                None,
                cache["refresh"]["tide_chart"],
                "青岛潮汐曲线数据",
                chart=chart_arr,
                site=qd["site"],
                extrema=qd["extrema"],
                sourceTime=qd["sourceTime"],
            ))
        except Exception as e:
            print(f"【青岛潮汐曲线】异常：{repr(e)}")
            is_tomorrow = target_date != today_ymd()
            if is_tomorrow:
                self.write_json(json_payload(False, None, "--", "暂无明日潮汐曲线数据", chart=[], tomorrow_unavailable=True))
            else:
                self.write_json(json_payload(False, None, "--", "青岛潮汐曲线数据获取失败", chart=[]))

    def handle_wave(self):
        target_date = self.query_date()
        target = f"http://www.qdmf.org.cn/Ajax/SeaBeach24hWave.ashx?date={target_date}&_t={timestamp_ms()}"
        print(f"\n【浪高接口】{target}")
        try:
            data = fetch_json(target)
            rows = data.get("rows", []) if isinstance(data, dict) else []
            row = rows[0] if rows else None
            if not row:
                raise ValueError("无浴场浪高数据")

            # 主要浴场数据
            beach_fields = [
                ("栈桥浴场", "SB24hWFSixthBathing"),
                ("第一浴场", "SB24hWFFirstBathing"),
                ("石老人浴场", "SB24hWFSLRBathing"),
                ("金沙滩", "SB24hWFGoldBeach"),
                ("第二浴场", "SB24hWFSecondBathing"),
                ("第三浴场", "SB24hWFThirdBathing"),
                ("仰口浴场", "SB24hWFYangKouBathing"),
                ("银沙滩", "SB24hWFSliverBathing"),
                ("灵山湾", "SB24hWFLingShanBathing"),
            ]
            beaches = []
            for name, prefix in beach_fields:
                wave_key = prefix + "WaveHeight"
                temp_keys = [
                    prefix + "WaterTemperature",
                    prefix + "WaterTemp",
                    prefix + "SeaTemp",
                    prefix + "Temperature",
                ]
                swim_key1 = prefix + "SwimWarn"
                swim_key2 = prefix + "SwimWain"
                wave_val = row.get(wave_key)
                temp_val = first_present(row, temp_keys)
                swim_val = row.get(swim_key1) or row.get(swim_key2) or "--"
                if wave_val is None and temp_val is None:
                    continue
                # 计算评分用于推荐（适宜>水温>浪高）
                score = 0
                if "适宜" in str(swim_val):
                    score += 50
                elif "较适宜" in str(swim_val):
                    score += 30
                if temp_val and isinstance(temp_val, (int, float, str)):
                    try:
                        t = float(temp_val)
                        if 22 <= t <= 28:
                            score += 30
                        elif 20 <= t < 22 or 28 < t <= 30:
                            score += 15
                    except (ValueError, TypeError):
                        pass
                if wave_val and isinstance(wave_val, (int, float, str)):
                    try:
                        w = float(wave_val)
                        if w <= 0.8:
                            score += 20
                        elif w <= 1.2:
                            score += 10
                    except (ValueError, TypeError):
                        pass
                beaches.append({
                    "name": name,
                    "wave_height": format_value(wave_val, "m"),
                    "water_temp": format_value(temp_val, "℃"),
                    "swim_tip": swim_val,
                    "score": score,
                })
            # 按评分排序找推荐浴场
            beaches.sort(key=lambda b: b["score"], reverse=True)
            recommended = beaches[0]["name"] if beaches else "--"

            # 主要数据（用栈桥浴场作为默认）
            wave_info = {
                "wave_height": format_value(row.get("SB24hWFSixthBathingWaveHeight"), "m"),
                "water_temp": format_value(
                    first_present(
                        row,
                        [
                            "SB24hWFSixthBathingWaterTemperature",
                            "SB24hWFSixthBathingWaterTemp",
                            "SB24hWFSixthBathingSeaTemp",
                            "SB24hWFSixthBathingTemperature",
                        ],
                    ),
                    "℃",
                ),
                "swim_tip": row.get("SB24hWFSixthBathingSwimWarn") or "--",
                "recommended_beach": recommended,
                "beaches": beaches[:6],  # 前6个浴场
            }
            cache["wave"] = wave_info
            cache["refresh"]["wave"] = now_hm(target_date)
            self.write_json(json_payload(True, wave_info, cache["refresh"]["wave"], "浪高实时数据"))
        except Exception as e:
            print(f"【浪高】异常：{repr(e)}")
            is_tomorrow = target_date != today_ymd()
            if is_tomorrow:
                self.write_json(json_payload(False, None, "--", "暂无明日海况数据", tomorrow_unavailable=True))
            else:
                self.write_json(json_payload(
                    False,
                    cache["wave"],
                    cache["refresh"]["wave"] if cache["wave"] else "--",
                    "浪高接口异常，展示缓存" if cache["wave"] else "浪高接口异常",
                ))

    def handle_offshore_wave(self):
        target_date = self.query_date()
        is_tomorrow = target_date != today_ymd()
        try:
            target = f"http://www.qdmf.org.cn/Ajax/SeaArea24HSumWave.ashx?date={target_date}&_t={timestamp_ms()}"
            print(f"\n【近海浪高接口】{target}")
            result = fetch_json(target)
            rows = []
            if isinstance(result, dict):
                rows = result.get("rows") or result.get("Rows") or result.get("data") or result.get("Data") or []
            elif isinstance(result, list):
                rows = result
            if isinstance(rows, dict):
                rows = [rows]
            row = pick_named_row(rows, ["青岛近海", "青岛近岸", "青岛"])
            explicit_wave = None
            if isinstance(row, dict):
                explicit_wave = row.get("SA24HWFQDOFFSHOREWAVEHEIGHT")
            if explicit_wave in (None, "", "-") and isinstance(result, dict):
                explicit_wave = result.get("SA24HWFQDOFFSHOREWAVEHEIGHT")
            wave_val = normalize_wave_value(explicit_wave or extract_wave_from_row(row) or extract_wave_from_row(result if isinstance(result, dict) else {}))
            if wave_val != "--":
                data = {"wave_height": wave_val}
                if is_tomorrow:
                    cache["offshore_wave_tomorrow"] = data
                    cache["refresh"]["offshore_wave_tomorrow"] = now_hm(target_date)
                else:
                    cache["offshore_wave"] = data
                    cache["refresh"]["offshore_wave"] = now_hm(target_date)
                self.write_json(json_payload(True, data, now_hm(target_date), f"青岛{'明日' if is_tomorrow else '今日'}近海浪高数据"))
            else:
                if is_tomorrow:
                    self.write_json(json_payload(False, None, "--", "暂无明日近海浪高数据", tomorrow_unavailable=True))
                else:
                    self.write_json(json_payload(
                        False,
                        None,
                        "--",
                        "青岛近海浪高数据解析失败",
                        error_code="PARSE_ERROR",
                    ))
        except Exception as e:
            print(f"【近海浪高】异常：{repr(e)}")
            if is_tomorrow:
                self.write_json(json_payload(False, None, "--", "暂无明日近海浪高数据", tomorrow_unavailable=True))
            else:
                error_code = getattr(e, "code", None) or "UPSTREAM_ERROR"
                message = "青岛浪高接口返回 502" if error_code == 502 else "青岛浪高接口请求失败"
                self.write_json(json_payload(
                    False,
                    None,
                    "--",
                    message,
                    error_code=error_code,
                ))

    def handle_alarm(self):
        try:
            html = fetch_text("http://www.qdmf.org.cn/AlarmPage.aspx?cata=0&indx=1&num=30", headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }, timeout=15)
            import re
            from datetime import datetime, timedelta
            now_dt = _now()
            current_year = now_dt.year
            three_days_ago = now_dt.date() - timedelta(days=3)
            alarms = []

            # 方法1：从JS数组 emer 中提取数据（包含文件名等完整信息）
            emer_match = re.search(r'var\s+emer\s*=\s*(\[.*?\]);', html, re.S)
            if emer_match:
                try:
                    import json
                    js_arr = emer_match.group(1)
                    js_arr = re.sub(r"(\w+):", r'"\1":', js_arr)
                    js_arr = js_arr.replace("'", '"')
                    data_list = json.loads(js_arr)
                    for item in data_list[:30]:
                        des = item.get("DES") or item.get("des") or ""
                        filename = item.get("FILENAME") or item.get("filename") or item.get("FILE") or item.get("file") or ""
                        pub = item.get("PUBTIME") or item.get("pubtime") or item.get("time") or ""
                        if not des:
                            continue
                        des = str(des).strip()
                        if "警报" not in des and "解除" not in des:
                            continue
                        if str(current_year) not in des and str(current_year) not in str(pub):
                            continue
                        # 提取发布时间（含时分）
                        pub_time = ""
                        pub_date_obj = None
                        m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日(\d{1,2})时', des)
                        if m:
                            pub_time = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d} {int(m.group(4)):02d}:00"
                            pub_date_obj = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
                        else:
                            m2 = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', des)
                            if m2:
                                pub_time = f"{m2.group(1)}-{int(m2.group(2)):02d}-{int(m2.group(3)):02d}"
                                pub_date_obj = datetime(int(m2.group(1)), int(m2.group(2)), int(m2.group(3))).date()
                            elif pub:
                                pm = re.search(r'(\d{4})-(\d{1,2})-(\d{1,2})', str(pub))
                                if pm:
                                    pub_time = f"{pm.group(1)}-{int(pm.group(2)):02d}-{int(pm.group(3)):02d}"
                                    pub_date_obj = datetime(int(pm.group(1)), int(pm.group(2)), int(pm.group(3))).date()
                        # 只保留最近3天
                        if pub_date_obj and pub_date_obj < three_days_ago:
                            continue
                        # 构造详情页URL
                        detail_url = ""
                        if filename:
                            fname = str(filename)
                            if not fname.lower().endswith('.doc') and not fname.lower().endswith('.docx'):
                                fname += ".docx"
                            detail_url = "http://www.qdmf.org.cn/Alermfile.aspx?fliename=" + fname
                        # 标题时间兜底
                        alarms.append({
                            "title": des,
                            "url": detail_url,
                            "publish_time": complete_alarm_time(pub_time, des, current_year),
                            "level": "",
                            "type": "海洋预警"
                        })
                except Exception:
                    alarms = []

            # 方法2：从a标签提取（兜底）
            if not alarms:
                # 匹配所有a标签，提取href和文本，支持带span时间的格式
                items = re.findall(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', html, re.S)
                for link, title_html in items[:50]:
                    title = re.sub(r'<[^>]+>', '', title_html).strip()
                    title = title.replace('&nbsp;', ' ').strip()
                    if not title or ("警报" not in title and "解除" not in title):
                        continue
                    if str(current_year) not in title:
                        continue
                    # 提取发布时间（含时分）
                    pub_time = ""
                    pub_date_obj = None
                    m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日(\d{1,2})时', title)
                    if m:
                        pub_time = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d} {int(m.group(4)):02d}:00"
                        pub_date_obj = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
                    else:
                        m2 = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', title)
                        if m2:
                            pub_time = f"{m2.group(1)}-{int(m2.group(2)):02d}-{int(m2.group(3)):02d}"
                            pub_date_obj = datetime(int(m2.group(1)), int(m2.group(2)), int(m2.group(3))).date()
                    # 只保留最近3天
                    if pub_date_obj and pub_date_obj < three_days_ago:
                        continue
                    # 从链接路径中提取文件名，构造Alermfile.aspx格式
                    detail_url = ""
                    fname = ""
                    # 从URL参数中提取
                    fm = re.search(r'[?&](?:fliename|filename|file|name)=([^&]+)', link, re.I)
                    if fm:
                        fname = fm.group(1)
                    else:
                        # 从路径中提取文件名
                        fm2 = re.search(r'([^/]+\.docx?)', link, re.I)
                        if fm2:
                            fname = fm2.group(1)
                        else:
                            # 尝试从路径最后一段提取
                            parts = link.rstrip('/').split('/')
                            last = parts[-1] if parts else ""
                            if last and '.' in last and not last.startswith('?'):
                                fname = last
                    if fname:
                        if not fname.lower().endswith('.doc') and not fname.lower().endswith('.docx'):
                            fname += ".docx"
                        detail_url = "http://www.qdmf.org.cn/Alermfile.aspx?fliename=" + fname
                    else:
                        detail_url = link if link.startswith("http") else "http://www.qdmf.org.cn/" + link
                    # 标题时间兜底
                    alarms.append({
                        "title": title,
                        "url": detail_url,
                        "publish_time": complete_alarm_time(pub_time, title, current_year),
                        "level": "",
                        "type": "海洋预警"
                    })

            if alarms:
                cache["alarm"] = alarms
                cache["refresh"]["alarm"] = now_hm()
                self.write_json(json_payload(True, alarms, cache["refresh"]["alarm"], "海洋灾害预警信息"))
            else:
                self.write_json(json_payload(False, cache.get("alarm") or [], cache["refresh"].get("alarm", "--"), "暂无预警信息"))
        except Exception as e:
            print(f"【海洋预警】异常：{repr(e)}")
            self.write_json(json_payload(False, cache.get("alarm") or [], cache["refresh"].get("alarm", "--"), "预警信息获取异常"))

    def handle_sd_alarm(self):
        """抓取山东气象台预警信息（山东省气象局官网）"""
        try:
            url = "http://sd.cma.gov.cn/xwzx_3497/qxrd/"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "http://sd.cma.gov.cn/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
            # 重试机制：网络不稳定时最多重试2次
            html = None
            last_error = None
            for attempt in range(3):
                try:
                    html = fetch_text(url, headers=headers, timeout=15)
                    if html and len(html) > 1000:
                        break
                except Exception as e:
                    last_error = e
                    if attempt < 2:
                        time.sleep(0.5)
                        continue
                    raise
            if not html:
                raise last_error or Exception("获取内容为空")

            all_alarms = []
            seen_titles = set()

            # 解析山东省气象局气象热点列表中的预警信息
            # 备用匹配模式（更准确，匹配日期格式的列表项）
            items = re.findall(
                r'<a[^>]*href="(\./\d{6}/t\d{8}_\d+\.html)"[^>]*>(.*?)</a>\s*<span>\s*(\d{4}-\d{2}-\d{2})\s*</span>',
                html, re.S
            )

            # 备用匹配模式2
            if not items:
                items = re.findall(
                    r'<li>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>\s*<span>\s*(.*?)\s*</span>\s*</li>',
                    html, re.S
                )

            for link, title, time_text in items[:30]:
                title = re.sub(r'<[^>]+>', '', title).strip()
                title = title.replace('&middot;', '').replace('&nbsp;', ' ').strip()
                title = re.sub(r'^\s*[·•]\s*', '', title).strip()
                time_text = time_text.strip()

                # 只保留包含预警/警报的条目
                if not title or ("预警" not in title and "警报" not in title):
                    continue

                # 去重
                if title in seen_titles:
                    continue
                seen_titles.add(title)

                # 判断是否与青岛相关
                # 1. 标题明确包含青岛及各区县
                # 2. 台风预警（台风影响范围广，青岛可能受影响）
                # 3. 山东省发布的省级预警默认覆盖青岛（青岛属于山东）
                has_qingdao_keyword = (
                    "青岛" in title or "市南" in title or "市北" in title
                    or "李沧" in title or "崂山" in title or "黄岛" in title
                    or "城阳" in title or "即墨" in title or "胶州" in title
                    or "平度" in title or "莱西" in title
                )
                has_typhoon = "台风" in title
                is_provincial = "山东省" in title or "山东" in title

                # 保留所有山东省内的预警（省级预警覆盖青岛）
                # 同时保留明确提到青岛的市级预警和台风预警
                if not (is_provincial or has_qingdao_keyword or has_typhoon):
                    continue

                # 识别预警等级
                level = "蓝色"
                if "红色" in title:
                    level = "红色"
                elif "橙色" in title:
                    level = "橙色"
                elif "黄色" in title:
                    level = "黄色"

                # 识别预警类型
                alarm_type = "气象预警"
                type_patterns = [
                    "台风", "暴雨", "暴雪", "寒潮", "大风", "沙尘暴", "高温",
                    "干旱", "雷电", "冰雹", "霜冻", "大雾", "霾", "道路结冰",
                    "海上大风", "强对流", "山洪", "地质灾害", "森林火险"
                ]
                for p in type_patterns:
                    if p in title:
                        alarm_type = p + "预警"
                        break

                # 补全URL
                full_url = link
                if link and not link.startswith("http"):
                    base_url = "http://sd.cma.gov.cn/xwzx_3497/qxrd"
                    if link.startswith("./"):
                        full_url = base_url + "/" + link[2:]
                    elif link.startswith("/"):
                        full_url = "http://sd.cma.gov.cn" + link
                    else:
                        full_url = base_url + "/" + link

                # 标记是否青岛相关，用于前端展示
                qingdao_related = has_qingdao_keyword or has_typhoon

                all_alarms.append({
                    "title": title,
                    "type": alarm_type,
                    "level": level,
                    "publish_time": complete_alarm_time(time_text if time_text and time_text != "--" else "", title),
                    "url": full_url,
                    "region": "山东",
                    "qingdao_related": qingdao_related,
                })

            # 按青岛相关程度和时间排序：青岛相关的排在前面，然后按时间倒序
            all_alarms.sort(key=lambda a: (
                2 if a["qingdao_related"] else (1 if "台风" in a["type"] else 0),
                a["publish_time"]
            ), reverse=True)
            # 只保留最近10天的预警
            from datetime import datetime, timedelta
            today = datetime.now(_tz()).date()
            filtered_alarms = []
            for alarm in all_alarms:
                try:
                    pub_date = datetime.strptime(alarm["publish_time"], "%Y-%m-%d").date()
                    if (today - pub_date).days <= 10:
                        filtered_alarms.append(alarm)
                except Exception:
                    # 日期解析失败的保留
                    filtered_alarms.append(alarm)
            all_alarms = filtered_alarms

            if all_alarms:
                cache["sd_alarm"] = all_alarms
                cache["refresh"]["sd_alarm"] = now_hm()
                self.write_json(json_payload(True, all_alarms, cache["refresh"]["sd_alarm"], "山东气象预警信息"))
            else:
                self.write_json(json_payload(False, cache.get("sd_alarm") or [], cache["refresh"].get("sd_alarm", "--"), "暂无气象预警信息"))
        except Exception as e:
            print(f"【山东预警】异常：{repr(e)}")
            self.write_json(json_payload(False, cache.get("sd_alarm") or [], cache["refresh"].get("sd_alarm", "--"), "气象预警获取异常"))

    def handle_sd_alarm_detail(self):
        """获取山东气象预警详情内容"""
        try:
            url = self.query_param("url")
            if not url:
                self.write_json(json_payload(False, None, "--", "缺少URL参数"))
                return
            # 安全校验：只允许山东省气象局域名
            if not url.startswith("http://sd.cma.gov.cn/") and not url.startswith("https://sd.cma.gov.cn/"):
                self.write_json(json_payload(False, None, "--", "URL不合法"))
                return
            html = fetch_text(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "http://sd.cma.gov.cn/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }, timeout=15)
            # 提取标题
            title = ""
            title_match = re.search(r'<div[^>]*class="[^"]*title[^"]*"[^>]*>(.*?)</div>', html, re.S)
            if not title_match:
                title_match = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
            if not title_match:
                title_match = re.search(r'<title>(.*?)</title>', html, re.S)
            if title_match:
                title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip()
                title = re.sub(r'\s+', ' ', title).strip()
                # 去掉网站名称前缀
                if '山东省气象局' in title and '--' in title:
                    title = title.split('--')[-1].strip()
                elif '山东省气象局' in title and '|' in title:
                    title = title.split('|')[0].strip()
            # 提取正文内容
            content = ""
            # 优先使用p标签提取正文（更可靠）
            paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', html, re.S)
            text_parts = []
            # 过滤关键词：排除页脚、导航、版权等无关内容
            skip_keywords = ['版权所有', 'ICP备', '公网安备', '网站标识码', '地址：', '联系电话', '当前位置', '作者：', '来源：', '时间：', '为了最佳观看效果']
            for p in paragraphs:
                p_text = re.sub(r'<[^>]+>', '', p).strip()
                p_text = re.sub(r'&nbsp;', ' ', p_text)
                p_text = re.sub(r'&ldquo;', '"', p_text)
                p_text = re.sub(r'&rdquo;', '"', p_text)
                p_text = re.sub(r'&mdash;', '—', p_text)
                if len(p_text) > 15 and not any(kw in p_text for kw in skip_keywords):
                    text_parts.append(p_text)
            if text_parts:
                content = '\n\n'.join(text_parts)
            # 如果p标签提取内容太少，尝试从div容器提取
            if not content or len(content) < 50:
                content_patterns = [
                    r'<div[^>]*class="[^"]*content[^"]*"[^>]*>(.*?)</div>\s*(?:<div|</div>)',
                    r'<div[^>]*class="[^"]*article[^"]*"[^>]*>(.*?)</div>\s*(?:<div|</div>)',
                    r'<div[^>]*class="[^"]*detail[^"]*"[^>]*>(.*?)</div>\s*(?:<div|</div>)',
                    r'<div[^>]*id="[^"]*content[^"]*"[^>]*>(.*?)</div>\s*(?:<div|</div>)',
                ]
                for pat in content_patterns:
                    m = re.search(pat, html, re.S)
                    if m:
                        raw = m.group(1)
                        raw = re.sub(r'<br\s*/?>', '\n', raw, flags=re.I)
                        raw = re.sub(r'</p>', '\n', raw, flags=re.I)
                        raw = re.sub(r'</div>', '\n', raw, flags=re.I)
                        raw = re.sub(r'<[^>]+>', '', raw)
                        raw = re.sub(r'&nbsp;', ' ', raw)
                        raw = re.sub(r'&ldquo;', '"', raw)
                        raw = re.sub(r'&rdquo;', '"', raw)
                        raw = re.sub(r'&mdash;', '—', raw)
                        raw = re.sub(r'\n\s*\n', '\n\n', raw)
                        raw = raw.strip()
                        if len(raw) > 50:
                            content = raw
                            break
            # 提取发布时间
            pub_time = ""
            time_match = re.search(r'(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?\s*\d{1,2}:\d{1,2})', html)
            if time_match:
                pub_time = time_match.group(1)
            self.write_json(json_payload(True, {
                "title": title or "预警详情",
                "content": content or "暂无详情内容",
                "pub_time": pub_time or "--",
                "url": url,
            }, "--", "预警详情"))
        except Exception as e:
            print(f"【山东预警详情】异常：{repr(e)}")
            self.write_json(json_payload(False, None, "--", "获取详情失败"))

    def handle_cma_alarm_detail(self):
        """获取CMA预警详情内容（支持山东气象局和青岛海洋预报台）"""
        try:
            url = self.query_param("url")
            if not url:
                self.write_json(json_payload(False, None, "--", "缺少URL参数"))
                return
            # 安全校验：只允许可信域名
            allowed_domains = [
                "http://sd.cma.gov.cn/", "https://sd.cma.gov.cn/",
                "http://www.qdmf.org.cn/", "https://www.qdmf.org.cn/",
                "http://qdmf.org.cn/", "https://qdmf.org.cn/",
            ]
            if not any(url.startswith(d) for d in allowed_domains):
                self.write_json(json_payload(False, None, "--", "URL不合法"))
                return
            html = fetch_text(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": url.rsplit('/', 1)[0] + '/' if '/' in url else url,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }, timeout=15)
            if not html:
                self.write_json(json_payload(False, None, "--", "无法获取页面内容"))
                return
            # 提取标题
            title = ""
            title_match = re.search(r'<div[^>]*class="[^"]*title[^"]*"[^>]*>(.*?)</div>', html, re.S)
            if not title_match:
                title_match = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
            if not title_match:
                title_match = re.search(r'<title>(.*?)</title>', html, re.S)
            if title_match:
                title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip()
                title = re.sub(r'\s+', ' ', title).strip()
            # 提取正文内容 - 尝试多种方式
            content = ""
            paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', html, re.S)
            text_parts = []
            skip_keywords = ['版权所有', 'ICP备', '公网安备', '网站标识码', '地址：', '联系电话', '当前位置', '作者：', '来源：', '时间：', '为了最佳观看效果', '浏览次数', '分享到', '责任编辑', '上一篇', '下一篇']
            for p in paragraphs:
                p_text = re.sub(r'<[^>]+>', '', p).strip()
                p_text = re.sub(r'&nbsp;', ' ', p_text)
                p_text = re.sub(r'&ldquo;', '"', p_text)
                p_text = re.sub(r'&rdquo;', '"', p_text)
                p_text = re.sub(r'&mdash;', '—', p_text)
                p_text = re.sub(r'&amp;', '&', p_text)
                if len(p_text) > 10 and not any(kw in p_text for kw in skip_keywords):
                    text_parts.append(p_text)
            if text_parts:
                content = '\n\n'.join(text_parts)
            # 如果p标签提取太少，尝试从div容器提取
            if not content or len(content) < 80:
                content_patterns = [
                    r'<div[^>]*class="[^"]*content[^"]*"[^>]*>(.*?)</div>\s*(?:<div|</div>)',
                    r'<div[^>]*class="[^"]*article[^"]*"[^>]*>(.*?)</div>\s*(?:<div|</div>)',
                    r'<div[^>]*class="[^"]*detail[^"]*"[^>]*>(.*?)</div>\s*(?:<div|</div>)',
                    r'<div[^>]*class="[^"]*main[^"]*"[^>]*>(.*?)</div>\s*(?:<div|</div>)',
                    r'<div[^>]*id="[^"]*content[^"]*"[^>]*>(.*?)</div>\s*(?:<div|</div>)',
                ]
                for pat in content_patterns:
                    m = re.search(pat, html, re.S)
                    if m:
                        raw = m.group(1)
                        raw = re.sub(r'<br\s*/?>', '\n', raw, flags=re.I)
                        raw = re.sub(r'</p>', '\n', raw, flags=re.I)
                        raw = re.sub(r'</div>', '\n', raw, flags=re.I)
                        raw = re.sub(r'<[^>]+>', '', raw)
                        raw = re.sub(r'&nbsp;', ' ', raw)
                        raw = re.sub(r'&ldquo;', '"', raw)
                        raw = re.sub(r'&rdquo;', '"', raw)
                        raw = re.sub(r'&mdash;', '—', raw)
                        raw = re.sub(r'&amp;', '&', raw)
                        raw = re.sub(r'\n\s*\n', '\n\n', raw)
                        raw = raw.strip()
                        if len(raw) > 80:
                            content = raw
                            break
            # 提取发布时间
            pub_time = ""
            time_match = re.search(r'(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?\s*\d{1,2}:\d{1,2})', html)
            if time_match:
                pub_time = time_match.group(1)
            self.write_json(json_payload(True, {
                "title": title or "预警详情",
                "content": content or "暂无详情内容，请点击下方查看原文链接查看",
                "pub_time": pub_time or "--",
                "url": url,
            }, "--", "预警详情"))
        except Exception as e:
            print(f"【CMA预警详情】异常：{repr(e)}")
            self.write_json(json_payload(False, None, "--", "获取详情失败"))

    def handle_cma_alarm(self):
        """获取青岛预警信息（CMA气象预警 + 青岛海洋预报台预警 + 山东气象台降级）"""
        alarms = []
        # 1. 尝试 CMA 气象预警接口
        try:
            url = "https://weather.cma.cn/api/map/alarm?adcode=370200"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://weather.cma.cn/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept-Encoding": "gzip, deflate",
            }
            result = fetch_json(url, headers=headers, timeout=12)
            if isinstance(result, dict):
                data = result.get("data") or result.get("Data") or result
                if isinstance(data, list):
                    raw_list = data
                elif isinstance(data, dict):
                    raw_list = data.get("alarms") or data.get("Alarms") or data.get("list") or data.get("rows") or []
                else:
                    raw_list = []
                for item in raw_list:
                    if not isinstance(item, dict):
                        continue
                    title = item.get("title") or item.get("Title") or item.get("alarmTitle") or item.get("headline") or ""
                    level = item.get("level") or item.get("Level") or item.get("severity") or item.get("alarmLevel") or ""
                    alarm_type = item.get("type") or item.get("Type") or item.get("category") or item.get("alarmType") or ""
                    pub_time = item.get("pubTime") or item.get("PubTime") or item.get("publishTime") or item.get("effective") or ""
                    item_url = item.get("url") or item.get("Url") or item.get("link") or item.get("href") or ""
                    alarm_id = item.get("id") or item.get("alarmId") or item.get("alertId") or item.get("alarmid") or ""
                    # 统一时间格式：2026/07/11 16:30 -> 2026-07-11 16:30
                    if pub_time:
                        pub_time = str(pub_time).replace("/", "-").replace(".", "-")
                    # 标题时间补全：如果只有日期没有时分，从标题中提取
                    pub_time = complete_alarm_time(pub_time, title)
                    level_name = ""
                    if "红" in str(level):
                        level_name = "红色"
                    elif "橙" in str(level):
                        level_name = "橙色"
                    elif "黄" in str(level):
                        level_name = "黄色"
                    elif "蓝" in str(level):
                        level_name = "蓝色"
                    else:
                        level_name = str(level) if level else "蓝色"
                    if not alarm_type and title:
                        type_match = re.search(r'(.+?)(预警|信号)', title)
                        if type_match:
                            alarm_type = type_match.group(1)
                    if not level_name or level_name == level:
                        if "红色" in title:
                            level_name = "红色"
                        elif "橙色" in title:
                            level_name = "橙色"
                        elif "黄色" in title:
                            level_name = "黄色"
                        elif "蓝色" in title:
                            level_name = "蓝色"
                    # 构造详情页URL
                    if not item_url and alarm_id:
                        item_url = f"https://weather.cma.cn/web/alarm/{alarm_id}.html"
                    elif not item_url and title:
                        item_url = "https://weather.cma.cn/"
                    alarms.append({
                        "title": title,
                        "level": level_name,
                        "type": alarm_type,
                        "publish_time": pub_time,
                        "url": item_url,
                        "source": "气象局",
                        "qingdao_related": True,
                    })
        except Exception as e:
            print(f"【CMA预警】接口获取失败：{repr(e)}")

        # 2. 青岛海洋预报台预警（海洋灾害预警）
        try:
            qdmf_url = f"http://www.qdmf.org.cn/Ajax/Alert.ashx?cata=0&indx=0&num=10&_t={timestamp_ms()}"
            qdmf_result = fetch_json(qdmf_url, timeout=12)
            qdmf_rows = []
            if isinstance(qdmf_result, dict):
                qdmf_rows = qdmf_result.get("rows") or qdmf_result.get("Rows") or []
            elif isinstance(qdmf_result, list):
                qdmf_rows = qdmf_result
            for row in qdmf_rows:
                if not isinstance(row, dict):
                    continue
                content = row.get("JBNEIRONG") or ""  # 风暴潮/海浪
                level_raw = row.get("JBJIBIE") or ""  # 蓝色警报/黄色警报/消息/解除警报
                area = row.get("JBQUYU") or ""  # 青岛近海
                time_str = row.get("JBSHIJIAN") or ""  # 发布时间
                code = row.get("JBBIANHAO") or ""
                unit = row.get("JBDANWEI") or ""
                doc_name = row.get("JBWENJIANMING") or ""
                # 标准化等级
                level_name = "蓝色"
                if "红" in level_raw:
                    level_name = "红色"
                elif "橙" in level_raw:
                    level_name = "橙色"
                elif "黄" in level_raw:
                    level_name = "黄色"
                elif "蓝" in level_raw:
                    level_name = "蓝色"
                elif "解除" in level_raw:
                    level_name = "解除"
                elif "消息" in level_raw:
                    level_name = "消息"
                # 构造标题
                title = f"{area}{content}{level_raw}"
                # 只保留近期的（当前年份）
                current_year = str(_now().year)
                if current_year not in time_str and current_year not in code:
                    continue
                # 跳过太旧的解除警报（保留3天内的）
                if "解除" in level_raw:
                    try:
                        from datetime import datetime, timedelta
                        time_clean = time_str.replace("年", "-").replace("月", "-").replace("日", "").replace("时", "")
                        parts = time_clean.split("-")
                        if len(parts) >= 3:
                            y, m, d = int(parts[0]), int(parts[1]), int(parts[2][:2])
                            pub_date = datetime(y, m, d).date()
                            today = _now().date()
                            if (today - pub_date).days > 3:
                                continue
                    except Exception:
                        pass
                detail_url = ""
                if doc_name:
                    fname = str(doc_name)
                    if not fname.lower().endswith('.doc') and not fname.lower().endswith('.docx'):
                        fname += ".docx"
                    detail_url = "http://www.qdmf.org.cn/Alermfile.aspx?fliename=" + fname
                alarms.append({
                    "title": title,
                    "level": level_name,
                    "type": content + "预警" if content else "海洋预警",
                    "publish_time": complete_alarm_time(time_str, title, current_year),
                    "url": detail_url,
                    "source": "海洋预报台",
                    "qingdao_related": True,
                })
        except Exception as e:
            print(f"【海洋预警】获取失败：{repr(e)}")

        # 3. 中央气象台全国预警API（筛选山东地区）
        try:
            import time
            nmc_url = f"http://www.nmc.cn/rest/findAlarm?pageNo=1&pageSize=30&signaltype=&signallevel=&province=&_={int(time.time()*1000)}"
            nmc_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "http://www.nmc.cn/f/alarm.html",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
            nmc_result = fetch_json(nmc_url, headers=nmc_headers, timeout=12)
            nmc_list = []
            if isinstance(nmc_result, dict):
                data = nmc_result.get("data") or nmc_result
                if isinstance(data, dict):
                    page = data.get("page") or data
                    if isinstance(page, dict):
                        nmc_list = page.get("list") or []
                    else:
                        nmc_list = data.get("list") or []
                else:
                    nmc_list = nmc_result.get("list") or nmc_result.get("rows") or []
            elif isinstance(nmc_result, list):
                nmc_list = nmc_result
            for item in nmc_list:
                if not isinstance(item, dict):
                    continue
                title = item.get("title") or ""
                alertid = item.get("alertid") or item.get("id") or ""
                issuetime = item.get("issuetime") or item.get("pubtime") or ""
                # 只保留山东地区的预警（alertid以37开头，或标题含山东/青岛）
                is_shandong = False
                if alertid.startswith("37"):
                    is_shandong = True
                elif "山东" in title or "青岛" in title or "济南" in title or "烟台" in title or "威海" in title:
                    is_shandong = True
                if not is_shandong:
                    continue
                # 识别级别
                level_name = "蓝色"
                if "红色" in title:
                    level_name = "红色"
                elif "橙色" in title:
                    level_name = "橙色"
                elif "黄色" in title:
                    level_name = "黄色"
                elif "蓝色" in title:
                    level_name = "蓝色"
                elif "解除" in title:
                    level_name = "解除"
                # 识别类型
                alarm_type = "气象预警"
                type_match = re.search(r'发布(.+?)(预警|信号)', title)
                if type_match:
                    alarm_type = type_match.group(1) + "预警"
                # 详情链接
                detail_url = ""
                if alertid:
                    detail_url = f"http://www.nmc.cn/publish/alarm/{alertid}.html"
                # 格式化发布时间
                pub_time = issuetime.replace("/", "-").replace(".", "-") if issuetime else ""
                pub_time = complete_alarm_time(pub_time, title)
                qingdao_flag = "青岛" in title
                alarms.append({
                    "title": title,
                    "level": level_name,
                    "type": alarm_type,
                    "publish_time": pub_time,
                    "url": detail_url,
                    "source": "中央气象台",
                    "qingdao_related": qingdao_flag,
                })
        except Exception as e:
            print(f"【中央气象台预警】获取失败：{repr(e)}")

        # 4. 中央气象台台风路径API（北上台风影响山东/青岛时显示）
        try:
            import time
            typhoon_url = f"http://typhoon.nmc.cn/weatherservice/typhoon/jsons/list_default?t={int(time.time()*1000)}&callback=typhoon_jsons_list_default"
            typhoon_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "http://typhoon.nmc.cn/web.html",
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
            typhoon_text = fetch_text(typhoon_url, headers=typhoon_headers, timeout=12)
            if typhoon_text and "typhoon_jsons_list_default" in typhoon_text:
                # 解析JSONP（可能是单括号或双括号）
                json_start = typhoon_text.find("(")
                json_end = typhoon_text.rfind(")")
                if json_start > 0 and json_end > json_start:
                    import json as _json
                    json_str = typhoon_text[json_start+1:json_end]
                    # 处理双括号情况 ((...))
                    if json_str.startswith("(") and json_str.endswith(")"):
                        json_str = json_str[1:-1]
                    typhoon_data = _json.loads(json_str)
                    typhoon_list = []
                    if isinstance(typhoon_data, dict):
                        typhoon_list = typhoon_data.get("typhoonList") or typhoon_data.get("list") or []
                    elif isinstance(typhoon_data, list):
                        typhoon_list = typhoon_data
                    for t in typhoon_list:
                        # 支持数组格式和对象格式
                        if isinstance(t, list) and len(t) >= 6:
                            # 数组格式: [id, ename, cname, number, ..., status]
                            typhoon_id = str(t[0])
                            ename = str(t[1])
                            name = str(t[2])
                            status = str(t[7]) if len(t) > 7 else ""
                            # 数组格式没有经纬度，需要从详情接口获取，这里默认显示
                            lat_f = 25  # 默认偏南纬度，后续可通过详情接口补充
                            lon_f = 125
                            strong = name
                        elif isinstance(t, dict):
                            status = t.get("status") or ""
                            name = t.get("name") or t.get("cname") or ""
                            ename = t.get("ename") or ""
                            typhoon_id = str(t.get("id") or t.get("tfid") or "")
                            lat = t.get("lat") or 0
                            lon = t.get("lng") or t.get("lon") or 0
                            try:
                                lat_f = float(lat) if lat else 0
                                lon_f = float(lon) if lon else 0
                            except (ValueError, TypeError):
                                lat_f = 0
                                lon_f = 0
                            strong = t.get("strong") or t.get("level") or name
                        else:
                            continue
                        # 只处理活跃台风（status为start或active）
                        if status and status not in ("start", "active", "2"):
                            continue
                        # 判断是否靠近山东/青岛（青岛纬度约36°N，经度约120°E）
                        # 北上台风：纬度>25°N，经度在115°-130°E范围内
                        affects_shandong = False
                        if lat_f > 20 and 115 <= lon_f <= 130:
                            affects_shandong = True
                        if not affects_shandong:
                            continue
                        # 台风等级名称
                        level_name = "蓝色"
                        strong_str = str(strong or "")
                        if "超强" in strong_str or "超台" in strong_str:
                            level_name = "红色"
                        elif "强台风" in strong_str or "强台" in strong_str:
                            level_name = "橙色"
                        elif "台风" in strong_str:
                            level_name = "黄色"
                        elif "热带风暴" in strong_str or "强热带风暴" in strong_str:
                            level_name = "蓝色"
                        elif "热带低压" in strong_str:
                            level_name = "消息"
                        # 构造标题
                        title = f"台风{name}({ename}) {strong_str} 接近华东沿海"
                        # 详情链接
                        detail_url = f"http://typhoon.nmc.cn/web.html?id={typhoon_id}" if typhoon_id else "http://typhoon.nmc.cn/web.html"
                        alarms.append({
                            "title": title,
                            "level": level_name,
                            "type": "台风预警",
                            "publish_time": complete_alarm_time("", title),
                            "url": detail_url,
                            "source": "中央气象台台风网",
                            "qingdao_related": True,
                            "is_typhoon": True,
                        })
        except Exception as e:
            print(f"【台风预警】获取失败：{repr(e)}")

        # 5. 如果 CMA 没有数据，补充山东气象台数据
        cma_has_data = any(a.get("source") == "气象局" for a in alarms)
        if not cma_has_data:
            try:
                sd_alarms = cache.get("sd_alarm") or []
                for item in sd_alarms:
                    if item.get("qingdao_related") or "青岛" in item.get("title", ""):
                        alarms.append(item)
                if not any(a.get("source") == "气象局" for a in alarms) and sd_alarms:
                    alarms.extend(sd_alarms[:3])
            except Exception as e2:
                print(f"【降级预警】获取失败：{repr(e2)}")

        # 6. 山东省海洋预报台预警（全省沿海海洋灾害预警）
        try:
            sd_marine_url = "http://123.234.129.236/"
            sd_marine_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
            sd_marine_html = fetch_text(sd_marine_url, headers=sd_marine_headers, timeout=12)
            if sd_marine_html:
                # 提取最新警报列表
                # 匹配形如：海浪蓝色警报、风暴潮黄色警报等
                alarm_patterns = [
                    r'<a[^>]*href="([^"]*)"[^>]*>([^<]*?(?:海浪|风暴潮|海冰|海啸)[^<]*?(?:警报|消息|解除)[^<]*?)</a>',
                    r'<a[^>]*>([^<]*?(?:海浪|风暴潮|海冰|海啸)[^<]*?(?:警报|消息|解除)[^<]*?)</a>',
                ]
                found_alarms = []
                for pat in alarm_patterns:
                    matches = re.findall(pat, sd_marine_html, re.S)
                    for m in matches:
                        if isinstance(m, tuple) and len(m) >= 2:
                            link, title = m[0], m[1]
                        else:
                            link, title = "", str(m)
                        title = re.sub(r'\s+', ' ', title).strip()
                        if len(title) > 5 and len(title) < 80:
                            found_alarms.append((title, link))
                        if len(found_alarms) >= 10:
                            break
                    if found_alarms:
                        break
                current_year = str(_now().year)
                for title, link in found_alarms:
                    # 识别级别
                    level_name = "蓝色"
                    if "红" in title:
                        level_name = "红色"
                    elif "橙" in title:
                        level_name = "橙色"
                    elif "黄" in title:
                        level_name = "黄色"
                    elif "蓝" in title:
                        level_name = "蓝色"
                    elif "解除" in title:
                        level_name = "解除"
                    elif "消息" in title:
                        level_name = "消息"
                    # 识别类型
                    alarm_type = "海洋预警"
                    if "海浪" in title:
                        alarm_type = "海浪预警"
                    elif "风暴潮" in title:
                        alarm_type = "风暴潮预警"
                    elif "海冰" in title:
                        alarm_type = "海冰预警"
                    elif "海啸" in title:
                        alarm_type = "海啸预警"
                    # 只保留青岛相关或省级预警
                    is_related = True  # 省级预警默认覆盖青岛
                    # 详情链接
                    detail_url = ""
                    if link:
                        if link.startswith("http"):
                            detail_url = link
                        elif link.startswith("/"):
                            detail_url = sd_marine_url.rstrip("/") + link
                        else:
                            detail_url = sd_marine_url + link
                    # 只保留今年的
                    if current_year not in title and current_year not in link:
                        continue
                    alarms.append({
                        "title": title,
                        "level": level_name,
                        "type": alarm_type,
                        "publish_time": complete_alarm_time("", title, current_year),
                        "url": detail_url,
                        "source": "山东省海洋预报台",
                        "qingdao_related": True,
                    })
        except Exception as e:
            print(f"【山东省海洋预报台】获取失败：{repr(e)}")

        # 排序：红色 > 橙色 > 黄色 > 蓝色 > 消息 > 解除
        level_order = {"红色": 6, "橙色": 5, "黄色": 4, "蓝色": 3, "消息": 2, "解除": 1}
        alarms.sort(key=lambda a: level_order.get(a.get("level", ""), 0), reverse=True)
        if alarms:
            cache["cma_alarm"] = alarms
            cache["refresh"]["cma_alarm"] = now_hm()
            self.write_json(json_payload(True, alarms, cache["refresh"]["cma_alarm"], "青岛预警信息"))
        else:
            self.write_json(json_payload(False, cache.get("cma_alarm") or [], cache["refresh"].get("cma_alarm", "--"), "暂无预警信息"))

    def handle_weather(self):
        target_date = self.query_date()
        params = urllib.parse.urlencode({
            "latitude": WEATHER_LATITUDE,
            "longitude": WEATHER_LONGITUDE,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m",
            "daily": "temperature_2m_max,temperature_2m_min,weather_code,wind_speed_10m_max,wind_gusts_10m_max,wind_direction_10m_dominant",
            "forecast_days": 2,
            "timezone": "Asia/Shanghai",
        })
        target = f"https://api.open-meteo.com/v1/forecast?{params}"
        print(f"\n【实时天气接口】{target}")
        try:
            raw = fetch_json(target, headers={
                "User-Agent": "OceanWindow/2.0",
                "Accept": "application/json",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Cache-Control": "no-cache",
            }, timeout=18)
            current = raw.get("current", {}) if isinstance(raw, dict) else {}
            daily = raw.get("daily", {}) if isinstance(raw, dict) else {}
            daily_times = daily.get("time") or []
            day_index = daily_times.index(target_date) if target_date in daily_times else 0

            def daily_pick(key):
                arr = daily.get(key)
                return arr[day_index] if isinstance(arr, list) and len(arr) > day_index else None

            is_today = target_date == today_ymd()
            temp_max = daily_pick("temperature_2m_max")
            temp_min = daily_pick("temperature_2m_min")
            code = current.get("weather_code") if is_today else daily_pick("weather_code")
            direction_degree = current.get("wind_direction_10m") if is_today else daily_pick("wind_direction_10m_dominant")
            weather = {
                "temperature": format_value(current.get("temperature_2m"), "℃") if is_today else (format_value(temp_max, "℃") if temp_max is not None else "--"),
                "apparent_temperature": format_value(current.get("apparent_temperature"), "℃") if is_today else (format_value(temp_min, "℃") if temp_min is not None else "--"),
                "temperature_range": "--" if temp_min is None or temp_max is None else f"{temp_min} ℃ ~ {temp_max} ℃",
                "humidity": format_value(current.get("relative_humidity_2m"), "%") if is_today else "--",
                "weather": weather_code_text(code),
                "wind_speed": format_value(current.get("wind_speed_10m"), "km/h") if is_today else format_value(daily_pick("wind_speed_10m_max"), "km/h"),
                "wind_direction": wind_direction_text(direction_degree),
                "wind_direction_degree": "--" if direction_degree is None else f"{direction_degree}°",
                "wind_gusts": format_value(current.get("wind_gusts_10m"), "km/h") if is_today else format_value(daily_pick("wind_gusts_10m_max"), "km/h"),
                "source_time": current.get("time", "--") if is_today else target_date,
            }
            cache["weather"] = weather
            cache["refresh"]["weather"] = now_hm(target_date)
            self.write_json(json_payload(True, weather, cache["refresh"]["weather"], "实时天气数据"))
        except Exception as e:
            print(f"【实时天气】异常：{repr(e)}")
            is_tomorrow = target_date != today_ymd()
            if is_tomorrow:
                self.write_json(json_payload(False, None, "--", "暂无明日天气数据", tomorrow_unavailable=True))
            else:
                self.write_json(json_payload(
                    False,
                    cache["weather"],
                    cache["refresh"]["weather"] if cache["weather"] else "--",
                    "天气接口异常，展示缓存" if cache["weather"] else "天气接口异常",
                ))

    def handle_page(self):
        settings_path = os.environ.get("NOTIFICATION_SETTINGS_PATH", "/notification-admin")
        filename = "notification-admin.html" if urllib.parse.urlparse(self.path).path == settings_path else "index.html"
        self.serve_web_file(filename, cache_seconds=0)

    def handle_static_asset(self, path):
        relative_path = urllib.parse.unquote(path[len("/assets/"):])
        self.serve_web_file(os.path.join("assets", relative_path), cache_seconds=300)

    def serve_web_file(self, relative_path, cache_seconds=0):
        web_root = os.path.realpath(WEB_DIR)
        target = os.path.realpath(os.path.join(web_root, relative_path))
        if target != web_root and not target.startswith(web_root + os.sep):
            self.write_json(json_payload(False, None, "--", "非法静态文件路径"), status=403)
            return
        try:
            with open(target, "rb") as handle:
                content = handle.read()
        except FileNotFoundError:
            self.write_json(json_payload(False, None, "--", "静态文件不存在"), status=404)
            return
        content_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
        self.send_response(200)
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += ";charset=utf-8"
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", f"public, max-age={cache_seconds}" if cache_seconds else "no-store")
        self.end_headers()
        self.wfile.write(content)


def first_present(row, keys):
    for key in keys:
        value = row.get(key)
        if value not in (None, "", "-"):
            return value
    return None


def pick_named_row(rows, keywords):
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = " ".join([str(v) for v in row.values() if v not in (None, "", "-")])
        if any(keyword in text for keyword in keywords):
            return row
    return rows[0] if rows else None


def normalize_wave_value(value):
    if value in (None, "", "-"):
        return "--"
    text = str(value).strip()
    if not text:
        return "--"
    text = re.sub(r"\s+", "", text).replace("米", "m")
    if "m" in text.lower():
        return text.replace("M", "m")
    if re.match(r"^\d+(\.\d+)?(-\d+(\.\d+)?)?$", text):
        return text + "m"
    return text


def extract_wave_from_row(row):
    if not isinstance(row, dict):
        return None
    direct = first_present(row, [
        "SA24HWFQDOFFSHOREWAVEHEIGHT",
        "QA24HSWWaveHeight",
        "QA24HSWWave",
        "SeaArea24HSumWaveHeight",
        "SeaArea24HSumWave",
        "WaveHeight",
        "WAVEHEIGHT",
        "wave_height",
        "wave",
        "浪高",
    ])
    if direct not in (None, "", "-"):
        return direct
    for key, value in row.items():
        key_text = str(key).lower()
        if value in (None, "", "-"):
            continue
        if ("wave" in key_text or "浪高" in str(key)) and not any(tag in key_text for tag in ["time", "date", "warn", "level"]):
            return value
    return None


def format_value(value, unit):
    if value in (None, "", "-"):
        return "--"
    return f"{value} {unit}"

class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_server():
    global server, notification_manager
    try:
        if NotificationManager is not None and notification_manager is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            config_path = os.environ.get("OCEAN_NOTIFICATION_CONFIG") or os.path.join(base_dir, "notification_config.json")
            db_path = os.environ.get("OCEAN_NOTIFICATION_DB") or os.path.join(base_dir, "ocean_notifications.db")
            if not os.path.exists(config_path):
                bundled_config = os.path.join(base_dir, "notification_config.json")
                if os.path.exists(bundled_config):
                    os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
                    shutil.copyfile(bundled_config, config_path)
            if os.path.exists(config_path):
                notification_manager = NotificationManager(config_path, db_path, notification_snapshot_provider)
                notification_manager.start()
                print(f"===== 通知规则引擎已启动（配置：{config_path}） =====")
        server = ThreadingTCPServer(("", PORT), MyHandler)
        print(f"===== Web 服务启动 0.0.0.0:{PORT} =====")
        server.serve_forever()
    except Exception as e:
        print("【服务启动异常】", e)
    finally:
        if notification_manager is not None:
            notification_manager.stop()
            notification_manager = None


def stop_server():
    global server, notification_manager
    if server is not None:
        server.shutdown()
        server.server_close()
        server = None
    if notification_manager is not None:
        notification_manager.stop()
        notification_manager = None


if __name__ == "__main__":
    if "--server" in sys.argv or os.environ.get("SERVER_MODE") == "1":
        start_server()
        raise SystemExit(0)

    if webview is None:
        print("当前环境未安装 pywebview，已切换为服务器模式。")
        start_server()
        raise SystemExit(0)

    t = threading.Thread(target=start_server, daemon=True)
    t.start()
    time.sleep(1.2)
    webview.create_window(
        "青岛潮汐与栈桥浴场天气海况",
        url=f"http://127.0.0.1:{PORT}",
        width=1280,
        height=760,
        resizable=True,
        maximized=True,
    )
    webview.start()
    stop_server()
