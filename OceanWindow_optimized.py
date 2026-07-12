import gzip
import http.cookiejar
import http.server
import io
import json
import os
import re
import socketserver
import sys
import threading
import time
import datetime
import urllib.error
import urllib.parse
import urllib.request

try:
    import webview
except Exception:
    webview = None


PORT = int(os.environ.get("PORT", "5051"))
BEACH_NAME = "第六海水浴场"
GLOBAL_TIDE_SITE_CODE = "T046"
GLOBAL_TIDE_SITE_NAME = "青岛"

# 第六海水浴场附近坐标。如需更精确，可按实际点位微调。
WEATHER_LATITUDE = 36.061
WEATHER_LONGITUDE = 120.326

server = None

cache = {
    "tide_table": None,
    "tide_chart": [],
    "wave": None,
    "offshore_wave": None,
    "offshore_wave_tomorrow": None,
    "weather": None,
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
            item["type"] = "低潮" if h < next_h else "高潮"
        elif next_h is None and prev_h is not None:
            item["type"] = "低潮" if h < prev_h else "高潮"
        elif prev_h is not None and next_h is not None:
            item["type"] = "低潮" if h <= prev_h and h <= next_h else "高潮"
        else:
            item["type"] = "潮位"
    return sorted_items


def build_qingdao_tide_table(site, report, sub, extrema, target_date):
    highs = [x for x in extrema if x["type"] == "高潮"]
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
        }
        if path in routes:
            routes[path]()
            return
        self.handle_page()

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
                ("第六浴场", "SB24hWFSixthBathing"),
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

            # 主要数据（用第六浴场作为默认）
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
                cached_data = cache.get("offshore_wave_tomorrow") if is_tomorrow else cache.get("offshore_wave")
                cached_time = cache["refresh"].get("offshore_wave_tomorrow", "--") if is_tomorrow else cache["refresh"].get("offshore_wave", "--")
                if is_tomorrow:
                    self.write_json(json_payload(False, None, "--", "暂无明日近海浪高数据", tomorrow_unavailable=True))
                else:
                    self.write_json(json_payload(
                        False,
                        cached_data,
                        cached_time,
                        "近海浪高解析失败，展示缓存" if cached_data else "近海浪高解析失败",
                    ))
        except Exception as e:
            print(f"【近海浪高】异常：{repr(e)}")
            cached_data = cache.get("offshore_wave_tomorrow") if is_tomorrow else cache.get("offshore_wave")
            cached_time = cache["refresh"].get("offshore_wave_tomorrow", "--") if is_tomorrow else cache["refresh"].get("offshore_wave", "--")
            if is_tomorrow:
                self.write_json(json_payload(False, None, "--", "暂无明日近海浪高数据", tomorrow_unavailable=True))
            else:
                self.write_json(json_payload(
                    False,
                    cached_data,
                    cached_time,
                    "近海浪高获取异常，展示缓存" if cached_data else "近海浪高获取异常",
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
        self.send_response(200)
        self.send_header("Content-Type", "text/html;charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(HTML.encode("utf-8"))


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


HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>青岛第六海水浴场实时展示大屏</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@4.9.0/dist/echarts.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0;font-family:"Microsoft YaHei","PingFang SC",Arial,sans-serif;}
:root{
  --fs-module-title:clamp(10px,0.7vw,12px);
  --fs-label:clamp(9px,0.65vw,11px);
  --fs-value:clamp(11px,0.8vw,14px);
  --fs-large:clamp(16px,1.4vw,22px);
  --fs-table:clamp(9px,0.65vw,12px);
  --fs-badge:clamp(12px,0.9vw,16px);
  --fs-topbar-code:clamp(10px,0.75vw,13px);
  --fs-topbar-title:clamp(10px,0.7vw,12px);
  --fs-topbar-clock:clamp(14px,1.2vw,20px);
}
html,body{width:100%;height:100%;overflow:hidden;background:#0a0e1a;color:#e8eaf6;}
body{
  background:
    radial-gradient(circle at 50% 0%,rgba(0,229,255,.10),transparent 34%),
    linear-gradient(rgba(0,229,255,.032) 1px,transparent 1px),
    linear-gradient(90deg,rgba(0,229,255,.032) 1px,transparent 1px),
    #0a0e1a;
  background-size:auto,40px 40px,40px 40px,auto;
}
.app{width:100vw;height:100vh;display:flex;flex-direction:column;overflow:hidden;}
.topbar{
  height:56px;flex:0 0 56px;display:flex;align-items:center;justify-content:space-between;
  padding:0 24px;background:linear-gradient(180deg,rgba(10,14,26,.97),rgba(10,14,26,.78));
  border-bottom:1px solid rgba(0,229,255,0.16);box-shadow:0 2px 18px rgba(0,0,0,.42);z-index:10;
}
.brand{display:flex;align-items:center;gap:12px;min-width:0;overflow:hidden;}
.brand-code{font-size:var(--fs-topbar-code);letter-spacing:.16em;color:#00e5ff;text-shadow:0 0 12px rgba(0,229,255,.5);font-weight:700;}
.brand-title{font-size:var(--fs-topbar-title);color:rgba(232,234,246,0.66);font-weight:600;line-height:1.3;}
.live{display:flex;align-items:center;gap:8px;color:#00e676;font-size:var(--fs-label);white-space:nowrap;min-width:0;}
.live-dot{width:8px;height:8px;border-radius:50%;background:#00e676;box-shadow:0 0 10px #00e676;animation:pulse 1.6s infinite;}
.top-actions{display:flex;align-items:center;gap:8px;white-space:nowrap;}
.sound-btn,.refresh-btn,.day-btn{display:inline-flex;align-items:center;gap:4px;border:1px solid rgba(0,229,255,0.34);background:rgba(15,21,40,.88);color:#00e5ff;border-radius:999px;padding:5px 10px;font-size:var(--fs-label);cursor:pointer;box-shadow:0 0 12px rgba(0,229,255,.12);white-space:nowrap;}
.refresh-btn{background:rgba(0,229,255,.12);}
.day-btn.active{background:rgba(0,229,255,.24);color:#fff;border-color:rgba(0,229,255,.55);}
.top-clock{text-align:right;}
.top-date{font-size:var(--fs-label);color:rgba(232,234,246,.72);white-space:normal;line-height:1.2;word-break:break-all;max-width:28vw;}
.top-time{font-size:var(--fs-topbar-clock);letter-spacing:.16em;color:#00e5ff;text-shadow:0 0 12px rgba(0,229,255,.45);font-weight:700;}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.45;transform:scale(.72)}}
@keyframes badgePulse{0%,100%{box-shadow:0 0 0 0 rgba(0,229,255,.45),0 0 12px rgba(0,229,255,.3)}50%{box-shadow:0 0 0 8px rgba(0,229,255,0),0 0 20px rgba(0,229,255,.55)}}
@keyframes badgePulseRising{0%,100%{box-shadow:0 0 0 0 rgba(255,171,0,.45),0 0 12px rgba(255,171,0,.3)}50%{box-shadow:0 0 0 8px rgba(255,171,0,0),0 0 20px rgba(255,171,0,.55)}}
@keyframes scanLine{0%{transform:translateY(-100%);opacity:0}18%,82%{opacity:.45}100%{transform:translateY(100%);opacity:0}}
@keyframes borderGlow{0%,100%{box-shadow:0 8px 24px rgba(0,0,0,0.55),0 0 16px rgba(0,229,255,.12)}50%{box-shadow:0 8px 24px rgba(0,0,0,0.55),0 0 28px rgba(0,229,255,.24)}}
@keyframes titleSpark{0%,100%{opacity:.75;-webkit-filter:drop-shadow(0 0 4px rgba(0,229,255,.45))}50%{opacity:1;-webkit-filter:drop-shadow(0 0 10px rgba(0,229,255,.85))}}
@keyframes floatCloud{0%,100%{transform:translateX(-3px)}50%{transform:translateX(4px)}}
@keyframes sunSpin{to{transform:rotate(360deg)}}
@keyframes rainFall{0%{transform:translateY(-7px);opacity:0}30%{opacity:.9}100%{transform:translateY(14px);opacity:0}}
@keyframes lightning{0%,78%,100%{opacity:.25;-webkit-filter:drop-shadow(0 0 2px #ffeb3b)}82%,88%{opacity:1;-webkit-filter:drop-shadow(0 0 12px #ffeb3b)}}
@keyframes fogMove{0%,100%{transform:translateX(-5px);opacity:.45}50%{transform:translateX(6px);opacity:.85}}
@keyframes snowFall{0%{transform:translateY(-5px) rotate(0deg);opacity:.2}50%{opacity:1}100%{transform:translateY(12px) rotate(180deg);opacity:.15}}
@keyframes dataPulse{0%,100%{text-shadow:0 0 8px rgba(0,229,255,.28);-webkit-filter:brightness(1)}50%{text-shadow:0 0 16px rgba(0,229,255,.72);-webkit-filter:brightness(1.16)}}
@keyframes shimmerX{0%{transform:translateX(-120%);opacity:0}18%,82%{opacity:.75}100%{transform:translateX(120%);opacity:0}}
@keyframes radarSweep{0%{transform:rotate(0deg)}100%{transform:rotate(360deg)}}
@keyframes tideRipple{0%{background-position:0 0,0 0}100%{background-position:70px 0,-70px 0}}
@keyframes waveSlide{0%{transform:translateX(-45%)}100%{transform:translateX(0)}}
@keyframes tableScan{0%,100%{background-color:transparent}50%{background-color:rgba(0,229,255,.10)}}
@keyframes chartAura{0%,100%{opacity:.22;transform:translateX(-8%)}50%{opacity:.48;transform:translateX(8%)}}
.content{height:calc(100vh - 56px);padding:16px;display:flex;flex-direction:column;position:relative;overflow:hidden;}
.row-main{display:flex;flex:7;min-height:0;gap:16px;}
.row-bottom{display:flex;flex:3;min-height:0;gap:16px;}
.card{
  position:relative;min-width:0;min-height:0;overflow:hidden;padding:16px;border-radius:12px;
  background:rgba(15,21,40,0.86);border:1px solid rgba(0,229,255,0.16);box-shadow:0 8px 24px rgba(0,0,0,0.55),0 0 22px rgba(0,229,255,0.18);
  animation:borderGlow 5s ease-in-out infinite;display:flex;flex-direction:column;
}
.card::before{content:"";position:absolute;top:0;right:0;bottom:0;left:0;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,229,255,.012) 2px,rgba(0,229,255,.012) 4px);pointer-events:none;z-index:1;}
.card::after{content:"";position:absolute;left:0;right:0;top:0;height:42%;background:linear-gradient(180deg,transparent,rgba(0,229,255,.08),transparent);pointer-events:none;z-index:1;animation:scanLine 7s linear infinite;}
.right-stack-card{display:flex;flex-direction:column;gap:0;border:none;background:none;box-shadow:none;animation:none;padding:0;overflow:visible;min-height:0;flex:2;}
.right-stack-card::before,.right-stack-card::after{display:none;}
.right-stack-card>.card{border-radius:12px;height:100%;}
.left-stack-card{display:flex;flex-direction:column;gap:16px;border:none;background:none;box-shadow:none;animation:none;padding:0;overflow:visible;min-height:0;min-width:0;flex:1;}
.left-stack-card::before,.left-stack-card::after{display:none;}
.left-stack-card>.card{border-radius:12px;margin-right:0;}
.left-stack-card>.weather-card{flex:0 0 auto;min-height:0;}
.left-stack-card>.alarm-list-card{flex:1;min-height:0;}
.map-card{min-width:0;width:100%;}
.alarm-bar{position:relative;z-index:20;display:flex;align-items:center;height:30px;padding:0 16px;background:rgba(255,43,43,.18);border-bottom:1px solid rgba(255,43,43,.35);overflow:hidden;}
.alarm-bar-label{flex:0 0 auto;font-size:var(--fs-label);font-weight:800;color:#ff2b2b;text-shadow:0 0 8px rgba(255,43,43,.5);letter-spacing:.08em;margin-right:12px;}
.alarm-marquee{flex:1;overflow:hidden;white-space:nowrap;position:relative;}
.alarm-marquee-inner{display:inline-block;padding-left:100%;animation:marqueeScroll 24s linear infinite;font-size:var(--fs-label);color:rgba(232,234,246,.9);}
@keyframes marqueeScroll{0%{transform:translateX(0)}100%{transform:translateX(-100%)}}
.sd-alarm-bar{position:relative;z-index:20;display:flex;align-items:center;height:32px;padding:0 16px;background:linear-gradient(180deg,rgba(255,171,0,.10),rgba(255,171,0,.04));border-bottom:1px solid rgba(255,171,0,.28);overflow:hidden;}
.sd-alarm-bar::before{content:"";position:absolute;top:0;left:0;right:0;bottom:0;background:repeating-linear-gradient(90deg,transparent,transparent 30px,rgba(255,171,0,.03) 30px,rgba(255,171,0,.03) 60px);pointer-events:none;}
.sd-alarm-bar-label{position:relative;z-index:2;flex:0 0 auto;display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:4px;font-size:var(--fs-label);font-weight:800;color:#ffab40;background:rgba(255,171,0,.12);border:1px solid rgba(255,171,0,.35);text-shadow:0 0 8px rgba(255,171,0,.5);letter-spacing:.12em;margin-right:14px;box-shadow:0 0 12px rgba(255,171,0,.15),inset 0 0 8px rgba(255,171,0,.08);}
.sd-alarm-bar-label::before{content:"";width:6px;height:6px;border-radius:50%;background:#ffab40;box-shadow:0 0 8px #ffab40;animation:sdWarnBlink 1.2s ease-in-out infinite;}
@keyframes sdWarnBlink{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.7)}}
.sd-alarm-marquee{position:relative;z-index:2;flex:1;overflow:hidden;white-space:nowrap;}
.sd-alarm-marquee-inner{display:inline-block;padding-left:100%;animation:marqueeScroll 45s linear infinite;font-size:var(--fs-label);color:rgba(232,234,246,.88);}
.sd-alarm-marquee:hover .sd-alarm-marquee-inner{animation-play-state:paused;}
.sd-alarm-item{display:inline-block;margin-right:50px;cursor:pointer;padding:2px 0;transition:all .2s;}
.sd-alarm-item:hover{text-shadow:0 0 8px currentColor;}
.sd-alarm-item.item-blue{color:#42a5f5;}
.sd-alarm-item.item-yellow{color:#ffca28;}
.sd-alarm-item.item-orange{color:#ffab40;}
.sd-alarm-item.item-red{color:#ff5252;}
.sd-alarm-item .lvl-blue{display:inline-block;padding:2px 8px;border-radius:4px;font-size:calc(var(--fs-label) - 1px);font-weight:700;background:rgba(33,150,243,.55);color:#fff;border:1px solid rgba(100,181,246,.9);margin-right:6px;text-shadow:0 1px 2px rgba(0,0,0,.4);box-shadow:0 0 6px rgba(33,150,243,.3);}
.sd-alarm-item .lvl-yellow{display:inline-block;padding:2px 8px;border-radius:4px;font-size:calc(var(--fs-label) - 1px);font-weight:700;background:rgba(255,193,7,.7);color:#fff;border:1px solid rgba(255,213,79,.95);margin-right:6px;text-shadow:0 1px 2px rgba(0,0,0,.4);box-shadow:0 0 6px rgba(255,193,7,.35);}
.sd-alarm-item .lvl-orange{display:inline-block;padding:2px 8px;border-radius:4px;font-size:calc(var(--fs-label) - 1px);font-weight:700;background:rgba(255,152,0,.75);color:#fff;border:1px solid rgba(255,167,38,.95);margin-right:6px;text-shadow:0 1px 2px rgba(0,0,0,.4);box-shadow:0 0 6px rgba(255,152,0,.4);}
.sd-alarm-item .lvl-red{display:inline-block;padding:2px 8px;border-radius:4px;font-size:calc(var(--fs-label) - 1px);font-weight:700;background:rgba(244,67,54,.8);color:#fff;border:1px solid rgba(255,82,82,.95);margin-right:6px;text-shadow:0 1px 2px rgba(0,0,0,.4);box-shadow:0 0 8px rgba(244,67,54,.5);}
.cma-alarm-bar{position:relative;z-index:20;display:flex;align-items:center;height:30px;padding:0 16px;background:linear-gradient(180deg,rgba(255,43,43,.10),rgba(255,43,43,.03));border-bottom:1px solid rgba(255,43,43,.25);overflow:hidden;}
.cma-alarm-bar::before{content:"";position:absolute;top:0;left:0;right:0;bottom:0;background:repeating-linear-gradient(90deg,transparent,transparent 30px,rgba(255,43,43,.025) 30px,rgba(255,43,43,.025) 60px);pointer-events:none;}
.cma-alarm-bar.bar-blue{background:linear-gradient(180deg,rgba(33,150,243,.10),rgba(33,150,243,.03));border-bottom-color:rgba(33,150,243,.25);}
.cma-alarm-bar.bar-blue::before{background:repeating-linear-gradient(90deg,transparent,transparent 30px,rgba(33,150,243,.025) 30px,rgba(33,150,243,.025) 60px);}
.cma-alarm-bar.bar-yellow{background:linear-gradient(180deg,rgba(255,193,7,.10),rgba(255,193,7,.03));border-bottom-color:rgba(255,193,7,.25);}
.cma-alarm-bar.bar-yellow::before{background:repeating-linear-gradient(90deg,transparent,transparent 30px,rgba(255,193,7,.025) 30px,rgba(255,193,7,.025) 60px);}
.cma-alarm-bar.bar-orange{background:linear-gradient(180deg,rgba(255,152,0,.10),rgba(255,152,0,.03));border-bottom-color:rgba(255,152,0,.25);}
.cma-alarm-bar.bar-orange::before{background:repeating-linear-gradient(90deg,transparent,transparent 30px,rgba(255,152,0,.025) 30px,rgba(255,152,0,.025) 60px);}
.cma-alarm-bar.bar-red{background:linear-gradient(180deg,rgba(255,43,43,.10),rgba(255,43,43,.03));border-bottom-color:rgba(255,43,43,.25);}
.cma-alarm-bar.bar-red::before{background:repeating-linear-gradient(90deg,transparent,transparent 30px,rgba(255,43,43,.025) 30px,rgba(255,43,43,.025) 60px);}
.cma-alarm-bar-label{position:relative;z-index:2;flex:0 0 auto;display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:4px;font-size:var(--fs-label);font-weight:800;color:#42a5f5;background:rgba(33,150,243,.12);border:1px solid rgba(33,150,243,.35);text-shadow:0 0 8px rgba(33,150,243,.5);letter-spacing:.12em;margin-right:10px;}
.cma-alarm-bar-label::before{content:"";width:6px;height:6px;border-radius:50%;background:#42a5f5;box-shadow:0 0 8px #42a5f5;animation:cmaWarnBlink 1.2s ease-in-out infinite;}
.cma-alarm-bar-time{position:relative;z-index:2;flex:0 0 auto;font-size:calc(var(--fs-label) - 1px);color:rgba(232,234,246,.45);margin-right:14px;}
@keyframes cmaWarnBlink{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.7)}}
.cma-alarm-marquee{position:relative;z-index:2;flex:1;overflow:hidden;white-space:nowrap;}
.cma-alarm-marquee-inner{display:inline-block;padding-left:100%;animation:marqueeScroll 90s linear infinite;font-size:var(--fs-label);color:rgba(232,234,246,.88);}
.cma-alarm-marquee:hover .cma-alarm-marquee-inner{animation-play-state:paused;}
.cma-alarm-item{display:inline-block;margin-right:60px;cursor:pointer;padding:2px 0;transition:all .2s;}
.cma-alarm-item:hover{text-shadow:0 0 8px currentColor;}
.cma-alarm-item.item-blue{color:#42a5f5;}
.cma-alarm-item.item-yellow{color:#ffca28;}
.cma-alarm-item.item-orange{color:#ffab40;}
.cma-alarm-item.item-red{color:#ff5252;}
.cma-alarm-item.item-green{color:#00e676;}
.cma-alarm-item .lvl-blue{display:inline-block;padding:2px 8px;border-radius:4px;font-size:calc(var(--fs-label) - 1px);font-weight:700;background:rgba(33,150,243,.55);color:#fff;border:1px solid rgba(100,181,246,.9);margin-right:6px;text-shadow:0 1px 2px rgba(0,0,0,.4);box-shadow:0 0 6px rgba(33,150,243,.3);}
.cma-alarm-item .lvl-yellow{display:inline-block;padding:2px 8px;border-radius:4px;font-size:calc(var(--fs-label) - 1px);font-weight:700;background:rgba(255,193,7,.7);color:#fff;border:1px solid rgba(255,213,79,.95);margin-right:6px;text-shadow:0 1px 2px rgba(0,0,0,.4);box-shadow:0 0 6px rgba(255,193,7,.35);}
.cma-alarm-item .lvl-orange{display:inline-block;padding:2px 8px;border-radius:4px;font-size:calc(var(--fs-label) - 1px);font-weight:700;background:rgba(255,152,0,.75);color:#fff;border:1px solid rgba(255,167,38,.95);margin-right:6px;text-shadow:0 1px 2px rgba(0,0,0,.4);box-shadow:0 0 6px rgba(255,152,0,.4);}
.cma-alarm-item .lvl-red{display:inline-block;padding:2px 8px;border-radius:4px;font-size:calc(var(--fs-label) - 1px);font-weight:700;background:rgba(244,67,54,.8);color:#fff;border:1px solid rgba(255,82,82,.95);margin-right:6px;text-shadow:0 1px 2px rgba(0,0,0,.4);box-shadow:0 0 8px rgba(244,67,54,.5);}
.cma-alarm-item .lvl-green{display:inline-block;padding:2px 8px;border-radius:4px;font-size:calc(var(--fs-label) - 1px);font-weight:700;background:rgba(0,230,118,.6);color:#fff;border:1px solid rgba(105,240,174,.9);margin-right:6px;text-shadow:0 1px 2px rgba(0,0,0,.4);box-shadow:0 0 6px rgba(0,230,118,.35);}
.left-stack-card>.sea-card{flex:1;min-height:0;}
.corner{position:absolute;width:18px;height:18px;border-color:#00e5ff;opacity:.72;z-index:2;}
.tl{display:none}.tr{right:8px;top:8px;border-right:1px solid;border-top:1px solid}
.bl{left:8px;bottom:8px;border-left:1px solid;border-bottom:1px solid}.br{right:8px;bottom:8px;border-right:1px solid;border-bottom:1px solid}
.module-title{
  position:relative;z-index:2;display:flex;align-items:center;justify-content:space-between;gap:10px;
  color:rgba(232,234,246,0.66);font-size:var(--fs-module-title);font-weight:700;letter-spacing:.12em;text-transform:uppercase;
  padding-bottom:10px;margin-bottom:12px;border-bottom:1px solid rgba(255,255,255,.12);
  min-width:0;overflow:hidden;
}
.module-title::before{content:"";width:3px;height:14px;border-radius:2px;background:#00e5ff;box-shadow:0 0 8px rgba(0,229,255,.65);margin-right:6px;animation:titleSpark 2.4s ease-in-out infinite;}
.module-title span:first-child{display:flex;align-items:center;color:rgba(232,234,246,0.66);min-width:0;line-height:1.3;}
.module-title small{font-size:var(--fs-label);color:rgba(232,234,246,.70);letter-spacing:0;text-transform:none;font-weight:600;line-height:1.3;max-width:45%;}
.module-title small.update-time{font-size:var(--fs-label);color:rgba(232,234,246,.45);font-weight:400;max-width:35%;flex-shrink:0;}
.module-unavailable{position:relative;z-index:2;text-align:center;padding:32px 16px;color:rgba(232,234,246,.35);font-size:var(--fs-value);font-weight:600;letter-spacing:.08em;}
.data-value{color:#00e5ff;text-shadow:0 0 10px rgba(0,229,255,.35);font-weight:800;}
.metric-grid{position:relative;z-index:2;display:flex;flex-wrap:wrap;flex:1;align-content:flex-start;}
.metric-grid.three{height:calc(100% - 40px);}
.metric{
  position:relative;overflow:hidden;min-height:54px;border-radius:9px;padding:10px;background:rgba(6,10,20,.74);
  border:1px solid rgba(255,255,255,.08);display:flex;flex-direction:column;justify-content:center;
}
.metric::after,.temp-card::after,.mini-box::after,.extra-box::after{content:"";position:absolute;top:0;right:0;bottom:0;left:0;background:linear-gradient(100deg,transparent,rgba(0,229,255,.12),transparent);transform:translateX(-120%);animation:shimmerX 6.5s ease-in-out infinite;pointer-events:none;}
.metric:nth-child(2)::after,.mini-box:nth-child(2)::after,.extra-box:nth-child(2)::after{animation-delay:.7s}
.metric:nth-child(3)::after,.extra-box:nth-child(3)::after{animation-delay:1.4s}
.metric:nth-child(4)::after,.extra-box:nth-child(4)::after{animation-delay:2.1s}
.metric:nth-child(5)::after,.extra-box:nth-child(5)::after{animation-delay:2.8s}
.metric:nth-child(6)::after,.extra-box:nth-child(6)::after{animation-delay:3.5s}
.metric .label{font-size:var(--fs-label);color:rgba(232,234,246,.78);margin-bottom:6px;line-height:1.35;font-weight:600;letter-spacing:.03em;}
.metric strong{position:relative;z-index:1;font-size:var(--fs-value);color:#00e5ff;text-shadow:0 0 8px rgba(0,229,255,.32);white-space:nowrap;animation:dataPulse 4.8s ease-in-out infinite;}
.wind-compass-wrap{position:relative;z-index:1;display:flex;align-items:center;gap:8px;min-width:0;}
.wind-compass{position:relative;flex:0 0 34px;width:34px;height:34px;border-radius:50%;border:1px solid rgba(0,229,255,.42);background:radial-gradient(circle,rgba(0,229,255,.16),rgba(6,10,20,.72));box-shadow:inset 0 0 10px rgba(0,229,255,.16),0 0 12px rgba(0,229,255,.14);}
.wind-compass::before{content:"N";position:absolute;top:1px;left:50%;transform:translateX(-50%);font-size:8px;color:rgba(232,234,246,.72);font-weight:800;}
.wind-needle{position:absolute;left:50%;top:50%;width:3px;height:22px;margin-left:-1.5px;margin-top:-11px;transform:rotate(0deg);transition:transform .9s cubic-bezier(.2,.8,.2,1);transform-origin:50% 50%;}
.wind-needle::before{content:"";position:absolute;left:50%;top:0;transform:translateX(-50%);width:0;height:0;border-left:5px solid transparent;border-right:5px solid transparent;border-bottom:13px solid #ffab00;-webkit-filter:drop-shadow(0 0 7px rgba(255,171,0,.8));}
.wind-needle::after{content:"";position:absolute;left:50%;bottom:0;transform:translateX(-50%);width:0;height:0;border-left:4px solid transparent;border-right:4px solid transparent;border-top:10px solid rgba(0,229,255,.68);}
.wind-text{min-width:0;font-size:var(--fs-value);color:#00e5ff;font-weight:800;text-shadow:0 0 8px rgba(0,229,255,.32);white-space:nowrap;}
.temp-strip{position:relative;z-index:2;margin-bottom:12px;display:flex;flex-wrap:wrap;min-width:0;}
.temp-card{position:relative;overflow:hidden;min-width:0;border-radius:9px;padding:10px;background:rgba(6,10,20,.74);border:1px solid rgba(255,255,255,.08);}
.temp-card .label{font-size:var(--fs-label);color:rgba(232,234,246,.78);font-weight:600;margin-bottom:5px;line-height:1.35;}
.temp-card .value{position:relative;z-index:1;font-size:var(--fs-value);line-height:1.05;color:#00e5ff;font-weight:800;text-shadow:0 0 8px rgba(0,229,255,.34);white-space:nowrap;animation:dataPulse 4.2s ease-in-out infinite;}
.temp-card.primary{width:100%;display:flex;align-items:center;justify-content:space-between;gap:10px;padding:9px 12px;}
.temp-card.primary .label{margin-bottom:0;}
.temp-card.primary .value{font-size:var(--fs-large);flex:0 0 auto;}
.weather-hero{position:relative;z-index:2;margin-bottom:12px;min-width:0;overflow:hidden;display:flex;align-items:stretch;gap:12px;}
.weather-hero-left{flex:1;display:flex;align-items:center;gap:12px;min-width:0;padding:10px 12px;border-radius:9px;background:rgba(6,10,20,.74);border:1px solid rgba(255,255,255,.08);}
.weather-hero-right{flex:1;display:flex;flex-direction:column;justify-content:center;min-width:0;padding:10px 12px;border-radius:9px;background:rgba(6,10,20,.74);border:1px solid rgba(255,255,255,.08);}
.weather-hero-right .label{font-size:var(--fs-label);color:rgba(232,234,246,.66);font-weight:600;margin-bottom:6px;}
.weather-current{flex:1;display:flex;align-items:center;justify-content:space-between;gap:12px;min-width:0;}
.weather-current-text{min-width:0;overflow:hidden;flex:1;}
.weather-current-label{font-size:var(--fs-label);color:rgba(232,234,246,.66);font-weight:600;margin-top:4px;}
.weather-current-temp{flex:0 0 auto;font-size:var(--fs-large);line-height:1.05;color:#00e5ff;font-weight:800;text-shadow:0 0 8px rgba(0,229,255,.34);white-space:nowrap;animation:dataPulse 4.2s ease-in-out infinite;}
.weather-icon{position:relative;flex:0 0 46px;width:46px;height:46px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:rgba(6,10,20,.70);border:1px solid rgba(255,255,255,.08);box-shadow:inset 0 0 16px rgba(0,229,255,.10),0 0 18px rgba(0,229,255,.08);overflow:hidden;}
.weather-icon::before,.weather-icon::after{content:"";position:absolute;display:block;}
.weather-text-wrap{min-width:0;overflow:hidden;}
.weather-icon.sunny::before{width:20px;height:20px;border-radius:50%;background:#ffeb3b;box-shadow:0 0 18px rgba(255,235,59,.85);}
.weather-icon.sunny::after{width:34px;height:34px;border-radius:50%;border:2px dashed rgba(255,235,59,.72);animation:sunSpin 8s linear infinite;}
.weather-icon.cloudy::before,.weather-icon.rainy::before,.weather-icon.storm::before,.weather-icon.snow::before{width:30px;height:16px;left:8px;top:17px;border-radius:16px;background:linear-gradient(180deg,#d8f6ff,#7fb8d5);box-shadow:9px -8px 0 -1px #b9e6f5,-9px -5px 0 -3px #e7fbff;animation:floatCloud 3s ease-in-out infinite;}
.weather-icon.cloudy::after{width:38px;height:10px;left:4px;bottom:9px;border-radius:999px;background:rgba(0,229,255,.18);-webkit-filter:blur(4px);}
.weather-icon.rainy::after{width:3px;height:13px;left:15px;top:30px;border-radius:3px;background:#00e5ff;box-shadow:9px -4px 0 #00e5ff,18px 1px 0 #00e5ff;animation:rainFall 1.1s linear infinite;}
.weather-icon.storm::after{width:14px;height:20px;left:19px;top:25px;background:#ffeb3b;clip-path:polygon(42% 0,100% 0,61% 44%,92% 44%,30% 100%,45% 55%,10% 55%);animation:lightning 1.8s linear infinite;}
.weather-icon.fog::before,.weather-icon.fog::after{left:8px;right:8px;height:3px;border-radius:999px;background:rgba(232,234,246,.75);box-shadow:0 9px 0 rgba(232,234,246,.52),0 18px 0 rgba(232,234,246,.38);animation:fogMove 2.8s ease-in-out infinite;}
.weather-icon.fog::before{top:12px}.weather-icon.fog::after{top:18px;animation-delay:.8s;opacity:.6;}
.weather-icon.snow::after{content:"✦";left:12px;top:27px;color:#e8f8ff;font-size:12px;text-shadow:11px -3px 0 #e8f8ff,20px 4px 0 #e8f8ff;animation:snowFall 1.9s linear infinite;}
.status-big{position:relative;z-index:2;display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;}
.status-big>div:first-child{min-width:0;padding-right:8px;}
.status-badge{display:inline-flex;align-items:center;padding:6px 16px;border-radius:999px;background:rgba(0,229,255,.13);border:1px solid rgba(0,229,255,.35);color:#00e5ff;font-size:var(--fs-badge);font-weight:700;animation:badgePulse 2s ease-in-out infinite;text-shadow:0 0 10px rgba(0,229,255,.5);letter-spacing:.06em;}
.status-badge.rising{background:linear-gradient(135deg,rgba(255,171,0,.2),rgba(255,235,59,.12));border-color:rgba(255,171,0,.5);color:#ffd54f;text-shadow:0 0 12px rgba(255,171,0,.6);animation:badgePulseRising 1.8s ease-in-out infinite;}
.status-badge.falling{background:linear-gradient(135deg,rgba(0,229,255,.2),rgba(0,188,212,.12));border-color:rgba(0,229,255,.5);color:#4dd0e1;text-shadow:0 0 12px rgba(0,229,255,.6);animation:badgePulse 2s ease-in-out infinite;}
.status-text{font-size:var(--fs-label);color:rgba(232,234,246,0.66);margin-top:6px;line-height:1.35;}
.level-number{font-size:var(--fs-large);line-height:1.05;white-space:nowrap;animation:dataPulse 3.8s ease-in-out infinite;}
.level-number small{font-size:var(--fs-value);color:rgba(232,234,246,0.66);margin-left:3px;}
.level-number-wrap{display:flex;flex-direction:column;align-items:flex-end;gap:4px;flex:0 0 auto;}
.level-number-label{font-size:var(--fs-label);color:rgba(232,234,246,.55);font-weight:600;letter-spacing:.05em;}
.mini-tide{position:relative;z-index:2;display:flex;margin-bottom:0;}
.mini-box{position:relative;overflow:hidden;background:rgba(6,10,20,.72);border:1px solid rgba(255,255,255,.08);border-radius:9px;padding:7px 8px;}
.mini-box .label{font-size:var(--fs-label);color:rgba(232,234,246,.78);margin-bottom:3px;font-weight:600;letter-spacing:.03em;}
.mini-box .value{font-size:var(--fs-value);color:#00e5ff;font-weight:800;text-shadow:0 0 8px rgba(0,229,255,.32);white-space:nowrap;}
.tide-status-row{position:relative;z-index:2;display:flex;flex:1;gap:8px;min-height:0;}
.tide-status-left{position:relative;z-index:2;display:flex;flex-direction:column;flex:1;min-width:0;min-height:0;}
.tide-status-left .status-big{margin-bottom:6px;flex:1;}
.tide-status-left .mini-tide{margin-bottom:0;flex-shrink:0;}
.tide-extra{position:relative;z-index:2;display:flex;flex-direction:column;flex:1;min-width:0;min-height:0;gap:6px;}
.extra-row{position:relative;z-index:2;display:flex;gap:6px;flex:1;min-height:0;}
.extra-row .extra-box{flex:1;display:flex;flex-direction:column;align-items:flex-start;justify-content:center;padding:6px 10px;min-height:0;}
.tide-extra .extra-box .label{margin-bottom:4px;flex:0 0 auto;margin-right:0;}
.tide-extra .extra-box .value{flex:0 0 auto;text-align:left;}
.tide-card{display:flex;flex-direction:column;}
.tide-card .module-title{padding-bottom:6px;margin-bottom:6px;}
.extra-box{position:relative;overflow:hidden;min-width:0;border-radius:9px;padding:4px 8px;background:rgba(6,10,20,.72);border:1px solid rgba(255,255,255,.08);}
.extra-box .label{font-size:var(--fs-label);color:rgba(232,234,246,.78);font-weight:600;margin-bottom:0;line-height:1.35;}
.extra-box .value{position:relative;z-index:1;font-size:var(--fs-value);color:#00e5ff;font-weight:800;text-shadow:0 0 8px rgba(0,229,255,.32);white-space:nowrap;animation:dataPulse 5.2s ease-in-out infinite;}
.map-shell{position:relative;z-index:2;height:calc(100% - 42px);border-radius:8px;overflow:hidden;border:1px solid rgba(0,229,255,.14);background:#060a14;}
.map-shell::before{content:"";position:absolute;z-index:3;left:50%;top:50%;width:120%;height:120%;transform-origin:0 0;pointer-events:none;background:conic-gradient(from 0deg,rgba(0,229,255,.18),rgba(0,229,255,.04) 22deg,transparent 54deg,transparent 360deg);animation:radarSweep 10s linear infinite;}
.map-shell::after{content:"";position:absolute;top:0;right:0;bottom:0;left:0;z-index:3;pointer-events:none;background-image:linear-gradient(rgba(0,229,255,.026) 1px,transparent 1px),linear-gradient(90deg,rgba(0,229,255,.026) 1px,transparent 1px);background-size:50px 50px,50px 50px;}
.typhoon-frame{position:absolute;top:0;right:0;bottom:0;left:0;width:100%;height:100%;border:0;-webkit-filter:saturate(1) brightness(1) contrast(1);}
.map-label{position:absolute;z-index:4;pointer-events:none;font-size:9px;color:rgba(232,234,246,.42);font-family:Consolas,monospace;text-shadow:0 0 6px rgba(0,0,0,.8);}
.map-label.n40{top:10px;left:12px}.map-label.n30{top:50%;left:12px;transform:translateY(-50%)}.map-label.n20{bottom:10px;left:12px}
.map-label.e125{bottom:10px;left:25%}.map-label.e130{bottom:10px;left:50%;transform:translateX(-50%)}.map-label.e135{bottom:10px;right:78px}
.map-actions{position:absolute;right:12px;bottom:12px;z-index:5;display:flex;gap:8px;}
.btn{display:inline-flex;align-items:center;justify-content:center;border:1px solid rgba(0,229,255,0.34);background:rgba(15,21,40,.88);color:#00e5ff;border-radius:999px;padding:7px 12px;font-size:var(--fs-label);text-decoration:none;cursor:pointer;box-shadow:0 0 12px rgba(0,229,255,.12);}
.mobile-map-open{display:none;position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);z-index:6;min-width:220px;text-align:center;border:1px solid rgba(0,229,255,0.34);background:rgba(10,14,26,.90);color:#00e5ff;border-radius:14px;padding:12px 16px;font-size:var(--fs-value);font-weight:800;text-decoration:none;box-shadow:0 0 22px rgba(0,229,255,.28);}
.chart-card{display:flex;flex-direction:column;}
#tideChart{position:relative;z-index:2;flex:1;min-height:0;width:100%;border-radius:8px;background:radial-gradient(circle at 50% 50%,rgba(0,229,255,.08),transparent 42%),rgba(6,10,20,.58);border:1px solid rgba(0,229,255,.14);overflow:hidden;}
#tideChart::before{content:"";position:absolute;top:0;right:0;bottom:0;left:0;pointer-events:none;background:linear-gradient(90deg,transparent,rgba(0,229,255,.10),transparent);animation:chartAura 6s ease-in-out infinite;z-index:1;}
.sea-card{display:flex;flex-direction:column;}
.sea-card::before{background:repeating-radial-gradient(ellipse at 50% 110%,rgba(0,229,255,.07) 0 2px,transparent 3px 12px);animation:tideRipple 8s linear infinite;}
.sea-metric-grid{position:relative;z-index:2;display:grid;grid-template-columns:repeat(3,1fr);gap:6px;flex:0 0 auto;}
.sea-metric-grid .metric{margin:0;min-height:46px;padding:6px 10px;}
.sea-metric-grid .metric .label{font-size:var(--fs-label);margin-bottom:3px;}
.sea-metric-grid .metric strong{font-size:var(--fs-value);}
.sea-metric-grid .metric strong.safe{color:#00e676;}
.sea-metric-grid .metric strong.bad{color:#ff5252;}
.sea-metric-grid .metric strong.warn{color:#ffca28;}
.beach-list{position:relative;z-index:2;margin-top:8px;padding-top:8px;border-top:1px solid rgba(0,229,255,.1);flex:1;min-height:0;display:flex;flex-direction:column;}
.beach-list-title{font-size:clamp(10px,0.72vw,13px);color:rgba(232,234,246,.55);font-weight:600;margin-bottom:6px;flex-shrink:0;letter-spacing:.05em;}
.beach-list-body{display:grid;grid-template-columns:repeat(3,1fr);gap:5px;overflow-y:auto;padding-right:2px;flex:1;min-height:0;}
.beach-item{display:flex;flex-direction:column;gap:3px;padding:7px 10px;border-radius:6px;background:rgba(6,10,20,.55);border:1px solid rgba(0,229,255,.08);font-size:clamp(10px,0.68vw,12px);}
.beach-item-top{display:flex;align-items:center;justify-content:space-between;gap:6px;}
.beach-item .beach-name{color:rgba(232,234,246,.9);font-weight:600;font-size:clamp(11px,0.75vw,13px);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;}
.beach-item .beach-status{flex-shrink:0;font-weight:700;font-size:clamp(10px,0.65vw,11px);}
.beach-item .beach-status.ok{color:#00e676;}
.beach-item .beach-status.warn{color:#ffca28;}
.beach-item .beach-status.bad{color:#ff5252;}
.beach-item-detail{display:flex;gap:10px;color:rgba(232,234,246,.55);font-size:clamp(9px,0.6vw,11px);}
.beach-item-detail span{white-space:nowrap;}
.beach-list-body::-webkit-scrollbar{width:3px;}
.beach-list-body::-webkit-scrollbar-thumb{background:rgba(0,229,255,.2);border-radius:2px;}
.right-bottom-stack{display:flex;flex-direction:column;gap:16px;flex:2;min-height:0;}
.right-bottom-stack>.card{margin-right:0;}
.right-bottom-stack>.chart-card{flex:1;min-height:0;}
.bottom-left-stack{display:flex;flex-direction:column;gap:0;flex:1;min-height:0;border:none;background:none;box-shadow:none;animation:none;padding:0;overflow:visible;}
.bottom-left-stack::before,.bottom-left-stack::after{display:none;}
.bottom-left-stack>.tide-card{flex:1;min-height:0;margin-right:0;}
.alarm-list-card{display:flex;flex-direction:column;flex:1;min-height:0;}
.alarm-list-container{position:relative;z-index:2;flex:1;min-height:0;overflow-y:auto;padding-right:4px;}
.alarm-list-empty{text-align:center;color:rgba(232,234,246,.35);padding:30px 0;font-size:var(--fs-label);}
.alarm-list-item{position:relative;display:flex;align-items:flex-start;gap:10px;padding:10px 12px;margin-bottom:8px;border-radius:8px;background:rgba(6,10,20,.74);border:1px solid rgba(255,255,255,.08);cursor:pointer;transition:all .25s;}
.alarm-list-item:hover{background:rgba(6,10,20,.92);border-color:rgba(255,171,0,.3);transform:translateX(2px);}
.alarm-list-item:last-child{margin-bottom:0;}
.alarm-list-item .alarm-level-tag{flex:0 0 auto;padding:3px 8px;border-radius:4px;font-size:calc(var(--fs-label) - 1px);font-weight:700;}
.alarm-list-item .alarm-level-tag.blue{background:rgba(33,150,243,.55);color:#fff;border:1px solid rgba(100,181,246,.9);text-shadow:0 1px 2px rgba(0,0,0,.4);box-shadow:0 0 6px rgba(33,150,243,.3);}
.alarm-list-item .alarm-level-tag.yellow{background:rgba(255,193,7,.7);color:#fff;border:1px solid rgba(255,213,79,.95);text-shadow:0 1px 2px rgba(0,0,0,.4);box-shadow:0 0 6px rgba(255,193,7,.35);}
.alarm-list-item .alarm-level-tag.orange{background:rgba(255,152,0,.75);color:#fff;border:1px solid rgba(255,167,38,.95);text-shadow:0 1px 2px rgba(0,0,0,.4);box-shadow:0 0 6px rgba(255,152,0,.4);}
.alarm-list-item .alarm-level-tag.red{background:rgba(244,67,54,.8);color:#fff;border:1px solid rgba(255,82,82,.95);text-shadow:0 1px 2px rgba(0,0,0,.4);box-shadow:0 0 8px rgba(244,67,54,.5);}
.alarm-list-item .alarm-item-body{flex:1;min-width:0;}
.alarm-list-item .alarm-item-title{font-size:var(--fs-value);color:#e8eaf6;font-weight:600;line-height:1.4;margin-bottom:4px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;transition:color .2s;}
.alarm-list-item.item-blue .alarm-item-title{color:#42a5f5;}
.alarm-list-item.item-yellow .alarm-item-title{color:#ffca28;}
.alarm-list-item.item-orange .alarm-item-title{color:#ffab40;}
.alarm-list-item.item-red .alarm-item-title{color:#ff5252;}
.alarm-list-item .alarm-item-meta{display:flex;align-items:center;gap:10px;font-size:calc(var(--fs-label) - 1px);color:rgba(232,234,246,.55);}
.alarm-list-item .alarm-item-type{color:#00e5ff;}
.alarm-list-item .alarm-item-time{color:rgba(232,234,246,.45);}
.alarm-list-container::-webkit-scrollbar{width:4px;}
.alarm-list-container::-webkit-scrollbar-track{background:rgba(255,255,255,.04);border-radius:2px;}
.alarm-list-container::-webkit-scrollbar-thumb{background:rgba(0,229,255,.3);border-radius:2px;}
.alarm-list-container::-webkit-scrollbar-thumb:hover{background:rgba(0,229,255,.5);}
.tide-table-wrap{position:relative;z-index:2;flex:1;min-height:0;overflow:hidden;}
table{width:100%;height:100%;border-collapse:collapse;background:rgba(6,10,20,.72);}
tr:nth-child(2){animation:tableScan 4.2s ease-in-out infinite}
tr:nth-child(3){animation:tableScan 4.2s ease-in-out infinite .7s}
tr:nth-child(4){animation:tableScan 4.2s ease-in-out infinite 1.4s}
tr:nth-child(5){animation:tableScan 4.2s ease-in-out infinite 2.1s}
th,td{border:1px solid rgba(0,229,255,.15);text-align:center;padding:6px 4px;font-size:var(--fs-table);color:#e8eaf6;vertical-align:middle;line-height:1.35;}
th{background:rgba(0,229,255,.12);color:#00e5ff;font-weight:800;}
.high{background:rgba(255,171,0,.12);color:#ffab00;font-weight:800;text-shadow:0 0 8px rgba(255,171,0,.25);}
.low{background:rgba(0,230,118,.10);color:#00e676;font-weight:800;text-shadow:0 0 8px rgba(0,230,118,.25);}
.high td{color:#ffab00;}
.low td{color:#00e676;}
.safe{color:#00e676!important}.bad{color:#ff5252!important}.warning{color:#ffab00!important}
.alarm-modal{position:fixed;top:0;left:0;right:0;bottom:0;z-index:9999;display:flex;align-items:center;justify-content:center;}
.alarm-modal-mask{position:absolute;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.65);backdrop-filter:blur(4px);}
.alarm-modal-box{position:relative;z-index:2;width:560px;max-width:90vw;max-height:75vh;background:linear-gradient(180deg,rgba(10,20,40,.98),rgba(5,10,25,.98));border:1px solid rgba(0,229,255,.25);border-radius:8px;box-shadow:0 0 40px rgba(0,229,255,.12),0 10px 40px rgba(0,0,0,.5);display:flex;flex-direction:column;overflow:hidden;}
.alarm-modal-box::before{content:"";position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,rgba(0,229,255,.6),transparent);}
.alarm-modal-header{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid rgba(0,229,255,.15);flex:0 0 auto;}
.alarm-modal-title{font-size:var(--fs-value);font-weight:700;color:#e8eaf6;}
.alarm-modal-close{background:transparent;border:none;color:rgba(232,234,246,.5);font-size:16px;cursor:pointer;padding:4px 8px;border-radius:4px;transition:all .2s;}
.alarm-modal-close:hover{color:#ff5252;background:rgba(255,82,82,.1);}
.alarm-modal-body{flex:1;min-height:0;overflow-y:auto;padding:16px;line-height:1.7;font-size:var(--fs-label);color:rgba(232,234,246,.85);word-break:break-word;}
.alarm-modal-loading{text-align:center;color:rgba(232,234,246,.4);padding:30px 0;}
.alarm-modal-body p{margin:0 0 12px 0;}
.alarm-modal-footer{display:flex;align-items:center;justify-content:space-between;padding:10px 16px;border-top:1px solid rgba(0,229,255,.1);flex:0 0 auto;}
.alarm-modal-time{font-size:var(--fs-label);color:rgba(232,234,246,.45);}
.alarm-modal-link{font-size:var(--fs-label);color:#42a5f5;text-decoration:none;padding:4px 10px;border:1px solid rgba(66,165,245,.3);border-radius:4px;transition:all .2s;position:relative;z-index:10;}
.alarm-modal-link:hover{background:rgba(66,165,245,.1);}
.alarm-modal-link.disabled{color:rgba(232,234,246,.3);border-color:rgba(232,234,246,.15);cursor:not-allowed;pointer-events:none;}
.content>.row-main,.content>.row-bottom{margin-bottom:16px;}.content>.row-bottom{margin-bottom:0;}.row-main>.card,.row-bottom>.card{margin-right:0;}.row-main>.card:last-child,.row-bottom>.card:last-child{margin-right:0;}.row-main>.left-stack-card:nth-child(1){flex:1;}.row-main>.right-stack-card:nth-child(2){flex:2;display:flex;}.row-bottom>.bottom-left-stack:nth-child(1){flex:1;}.row-bottom>.right-bottom-stack{flex:2;}.metric-grid>.metric{width:calc(50% - 5px);margin-right:10px;margin-bottom:10px;}.metric-grid>.metric:nth-child(2n){margin-right:0;}.metric-grid.three>.metric{width:calc(33.33% - 7px);}.metric-grid.three>.metric:nth-child(3n){margin-right:0;}.temp-strip>.temp-card{width:calc(50% - 4px);margin-right:8px;margin-bottom:8px;}.temp-strip>.temp-card:nth-child(2n){margin-right:0;}.mini-tide>.mini-box{flex:1;margin-right:12px;}.mini-tide>.mini-box:last-child{margin-right:0;}.tide-extra>.extra-box{width:calc(33.33% - 6px);margin-right:8px;margin-bottom:8px;}.tide-extra>.extra-box:nth-child(3n){margin-right:0;}.map-card{padding:12px;}@media (max-width:1079px){
  html,body{overflow:auto}
  .app{height:auto;min-height:100vh;overflow:visible}
}
html.mobile .app{height:auto;min-height:100vh;overflow:visible}
html.mobile{overflow:auto}
html.mobile .topbar{height:auto;min-height:48px;flex:0 0 auto;flex-wrap:wrap;gap:4px 8px;padding:calc(8px + 0px) 10px 6px;align-items:center;position:relative;z-index:10}
html.mobile .brand{width:100%;justify-content:center;gap:6px}
html.mobile .brand-code{font-size:12px;letter-spacing:.12em}
html.mobile .brand-title{font-size:12px;max-width:68vw}
html.mobile .live{order:1;width:100%;justify-content:center;font-size:12px;gap:6px}
html.mobile .top-actions{order:2;width:100%;justify-content:center;gap:6px;flex-wrap:wrap}
html.mobile .sound-btn,html.mobile .refresh-btn,html.mobile .day-btn{font-size:12px;padding:5px 10px;min-width:0;justify-content:center}
html.mobile .sound-btn span{display:none}
html.mobile .refresh-btn{min-width:auto}
html.mobile .top-clock{order:3;width:100%;text-align:center}
html.mobile .top-date{font-size:12px;white-space:normal;line-height:1.5;text-align:center;max-width:100%;display:block;}
html.mobile .top-time{font-size:24px;letter-spacing:.10em;line-height:1.05;margin-top:1px;text-align:center;}
html.mobile .content{padding:10px 10px 16px;height:auto;display:block;overflow:visible;position:static}
html.mobile .row-main,html.mobile .row-bottom{display:block}
html.mobile .left-stack-card{display:block;gap:0;}
html.mobile .left-stack-card>.card{margin:10px 0;}
html.mobile .card{margin:10px 0;min-height:180px;padding:12px;}
html.mobile .map-card{min-height:420px}
html.mobile .chart-card{min-height:280px}
html.mobile .alarm-bar{display:none}
html.mobile .sd-alarm-bar{display:none}
html.mobile .cma-alarm-bar{display:none}
html.mobile .map-shell{height:60vh;min-height:360px}
html.mobile .mobile-map-open{display:block}
html.mobile .map-actions{left:12px;right:12px;justify-content:center}
html.mobile .tide-status-row{display:block}
html.mobile .tide-status-left{margin-bottom:10px}
html.mobile .tide-extra{display:block}
html.mobile .extra-row{display:flex;gap:6px;margin-bottom:6px;}
html.mobile .extra-row .extra-box{flex:1;width:50%;}
html.mobile .tide-extra .extra-box{width:auto;flex:1;}
html.mobile .module-title{font-size:13px;padding-bottom:8px;margin-bottom:10px;}
html.mobile .module-title small{font-size:11px;}
html.mobile .metric .label{font-size:12px;}
html.mobile .metric strong{font-size:15px;}
html.mobile .temp-card .label{font-size:12px;}
html.mobile .temp-card .value{font-size:14px;}
html.mobile .temp-card.primary .value{font-size:22px;}
html.mobile .weather-current-temp{font-size:22px;}
html.mobile .weather-hero-right .label{font-size:12px;}
html.mobile .status-badge{font-size:14px;padding:5px 12px;}
html.mobile .level-number{font-size:24px;}
html.mobile .level-number small{font-size:14px;}
html.mobile .mini-box .label{font-size:11px;}
html.mobile .mini-box .value{font-size:13px;}
html.mobile .extra-box .label{font-size:11px;}
html.mobile .extra-box .value{font-size:12px;}
html.mobile th,html.mobile td{font-size:11px;padding:5px 3px;}
html.mobile .beach-list-title{font-size:12px;}
html.mobile .beach-item{font-size:12px;padding:8px 10px;gap:4px;}
html.mobile .beach-item .beach-name{font-size:13px;}
html.mobile .beach-item .beach-status{font-size:11px;}
html.mobile .beach-item-detail{font-size:11px;gap:12px;}
html.mobile .beach-list-body{gap:6px;grid-template-columns:repeat(2,1fr);}</style>
</head>
<body>
<div class="app">
  <header class="topbar">
    <div class="brand">
      <span class="brand-code">QINGDAO NO.6</span>
      <span class="brand-title">青岛第六海水浴场 · 实时监测大屏</span>
    </div>
    <div class="live"><span class="live-dot"></span><span>实时在线</span><span id="globalUpdate">数据更新 --</span></div>
    <div class="top-actions">
      <button id="todayBtn" class="day-btn active" onclick="switchForecastDay(0)">今日</button>
      <button id="tomorrowBtn" class="day-btn" onclick="switchForecastDay(1)">明日</button>
      <button id="refreshBtn" class="refresh-btn" onclick="refreshAllData()">🔄 刷新</button>
      <button id="soundBtn" class="sound-btn" onclick="toggleSound()">🔇 <span>声音关</span></button>
    </div>
    <div class="top-clock"><div id="dateText" class="top-date">--</div><div id="nowTime" class="top-time">--:--:--</div></div>
  </header>

  <div class="cma-alarm-bar" id="cmaAlarmBar">
    <div class="cma-alarm-bar-label">气象预警</div>
    <span class="cma-alarm-bar-time" id="cmaAlarmTime">更新 --</span>
    <div class="cma-alarm-marquee">
      <div class="cma-alarm-marquee-inner" id="cmaAlarmMarquee">
        <span class="cma-alarm-item">暂无预警信息</span>
      </div>
    </div>
  </div>

  <main class="content">
    <section class="row-main">
      <div class="left-stack-card">
      <div class="card weather-card" id="weatherCard">
        <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
        <div class="module-title"><span>实时天气与风况</span><small id="weatherTime">--</small></div>
        <div class="weather-hero">
          <div class="weather-hero-left">
            <div id="weatherIcon" class="weather-icon cloudy"></div>
            <div class="weather-current">
              <div class="weather-current-text">
                <div id="weatherText" style="font-size:clamp(16px,1.3vw,24px);color:#e8eaf6;font-weight:800;line-height:1.3;">--</div>
                <div id="airTempLabel" class="weather-current-label">当前气温</div>
              </div>
              <div id="airTemp" class="weather-current-temp">--</div>
            </div>
          </div>
          <div class="weather-hero-right">
            <div class="label">风向风级</div>
            <div class="wind-compass-wrap"><div class="wind-compass"><div id="windNeedle" class="wind-needle"></div></div><div id="windDirection" class="wind-text">--</div></div>
          </div>
        </div>
        <div class="temp-strip">
          <div class="temp-card"><div id="tempRangeLabel" class="label">今日温度</div><div id="tempRange" class="value">--</div></div>
          <div class="temp-card"><div class="label">湿度</div><div id="humidity" class="value">--</div></div>
        </div>
      </div>

      <div class="card sea-card" id="seaCard">
        <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
        <div class="module-title"><span>海况数据</span><small id="waveTime">--</small></div>
        <div class="sea-metric-grid">
          <div class="metric"><div class="label">近海浪高</div><strong id="offshoreWaveHeight">--</strong></div>
          <div class="metric"><div class="label">浪况等级</div><strong id="waveLevel">--</strong></div>
          <div class="metric"><div class="label">浴场水温</div><strong id="waterTemp">--</strong></div>
          <div class="metric"><div class="label">下海提示</div><strong id="swimTip">--</strong></div>
          <div class="metric"><div class="label">紫外线指数</div><strong id="uvIndex">--</strong></div>
        </div>
        <div class="beach-list" id="beachList" style="display:none;">
          <div class="beach-list-title">主要浴场概览</div>
          <div class="beach-list-body"></div>
        </div>
      </div>
      </div>

      <div class="right-stack-card">
      <div class="card map-card">
        <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
        <div class="module-title"><span>台风路径与云图</span><small>北海预报减灾中心</small></div>
        <div class="map-shell">
          <iframe id="typhoonFrame" class="typhoon-frame" title="台风路径与云图" loading="eager" src="https://www.bhyb.org.cn/typhoon/"></iframe>
          <span class="map-label n40">40°N</span>
          <span class="map-label n30">30°N</span>
          <span class="map-label n20">20°N</span>
          <span class="map-label e125">125°E</span>
          <span class="map-label e130">130°E</span>
          <span class="map-label e135">135°E</span>
          <a class="mobile-map-open" href="https://www.bhyb.org.cn/typhoon/" target="_blank" rel="noopener">手机端打开台风图</a>
          <div class="map-actions">
            <button class="btn" onclick="reloadTyphoonFrame()">刷新图层</button>
            <a class="btn" href="https://www.bhyb.org.cn/typhoon/" target="_blank">浏览器打开</a>
          </div>
        </div>
      </div>
      </div>
    </section>

    <section class="row-bottom">
      <div class="bottom-left-stack">
      <div class="card tide-card" id="tideCard">
        <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
        <div class="module-title"><span>潮汐状态</span><small id="tideUpdate">--</small></div>
        <div class="tide-status-row">
          <div class="tide-status-left">
            <div class="status-big" id="tideCardContent">
              <div>
                <span id="tideBadge" class="status-badge">等待加载</span>
                <div id="tideStatusText" class="status-text">正在获取青岛潮汐数据</div>
              </div>
              <div class="level-number-wrap">
                <div class="level-number-label">当前潮高</div>
                <div class="level-number data-value"><span id="currentLevel">--</span><small>cm</small></div>
              </div>
            </div>
            <div class="mini-tide">
              <div class="mini-box"><div class="label">下次高潮</div><div id="nextHigh" class="value">--</div></div>
              <div class="mini-box"><div class="label">下次低潮</div><div id="nextLow" class="value">--</div></div>
            </div>
          </div>
          <div class="tide-extra">
            <div class="extra-row">
              <div class="extra-box"><div class="label">当前趋势</div><div id="tideTrend" class="value">--</div></div>
              <div class="extra-box"><div class="label">当前潮段</div><div id="tidePhase" class="value">--</div></div>
            </div>
            <div class="extra-row">
              <div class="extra-box"><div class="label">潮段进度</div><div id="tideProgress" class="value">--</div></div>
              <div class="extra-box"><div class="label">今日潮差</div><div id="tideRange" class="value">--</div></div>
            </div>
            <div class="extra-row">
              <div class="extra-box"><div class="label">距低潮</div><div id="lowDelta" class="value">--</div></div>
              <div class="extra-box"><div class="label">距高潮</div><div id="highDelta" class="value">--</div></div>
            </div>
          </div>
        </div>
      </div>
      </div>

      <div class="right-bottom-stack">
        <div class="card chart-card" id="chartCard">
          <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
          <div class="module-title"><span id="chartTitle">青岛今日潮汐曲线</span><small id="chartSource">全球潮汐平台</small><small id="chartTime" class="update-time">--</small></div>
          <div id="tideChart">等待曲线数据</div>
        </div>
      </div>
    </section>
  </main>
</div>

<div id="alarmModal" class="alarm-modal" style="display:none;">
  <div class="alarm-modal-mask" onclick="closeAlarmModal()"></div>
  <div class="alarm-modal-box">
    <div class="alarm-modal-header">
      <span class="alarm-modal-title" id="alarmModalTitle">预警详情</span>
      <button class="alarm-modal-close" onclick="closeAlarmModal()">✕</button>
    </div>
    <div class="alarm-modal-body" id="alarmModalBody">
      <div class="alarm-modal-loading">加载中...</div>
    </div>
    <div class="alarm-modal-footer">
      <span id="alarmModalTime" class="alarm-modal-time">--</span>
      <a id="alarmModalLink" href="#" target="_blank" rel="noopener noreferrer" class="alarm-modal-link" onclick="event.stopPropagation();">查看原文</a>
    </div>
  </div>
</div>

<script>
// Polyfills for Android 4.4.2 (Chrome 30)
if (!String.prototype.includes) { String.prototype.includes = function(s) { return this.indexOf(s) >= 0; }; }
if (!String.prototype.padStart) { String.prototype.padStart = function(len, ch) { var s = String(this); while (s.length < len) s = (ch || "0") + s; return s; }; }
var tideRawData=null, tideChart=null, lastChartRaw=null, lastChartSite=null, lastChartPoints=[], lastTideList=[], resizeTimer=null, lastTideRising=null, soundEnabled=false, audioCtx=null, selectedDayOffset=0, tomorrowTideList=[], tomorrowTideReady=false;
var $=function(id){return document.getElementById(id);};
function setText(id,text){var el=$(id); if(el) el.textContent=(text===null||text===undefined||text==="")?"--":text;}
function lunarText(d){
  try{
    if(typeof Intl==="undefined"||!Intl.DateTimeFormat)return "农历 --";
    function zhNum(n){
      var digit=["零","一","二","三","四","五","六","七","八","九"];
      n=parseInt(n,10);
      if(isNaN(n))return "";
      if(n<=10)return n===10?"十":digit[n];
      if(n<20)return "十"+digit[n%10];
      if(n<100){
        var ten=Math.floor(n/10), one=n%10;
        return digit[ten]+"十"+(one?digit[one]:"");
      }
      return String(n).replace(/\d/g,function(x){return digit[parseInt(x,10)];});
    }
    function lunarDayText(n){
      var names=["初一","初二","初三","初四","初五","初六","初七","初八","初九","初十","十一","十二","十三","十四","十五","十六","十七","十八","十九","二十","廿一","廿二","廿三","廿四","廿五","廿六","廿七","廿八","廿九","三十"];
      return names[n-1]||zhNum(n);
    }
    var fmt=null;
    try{
      fmt=new Intl.DateTimeFormat("zh-CN-u-ca-chinese",{month:"long",day:"numeric"});
    }catch(e1){
      fmt=new Intl.DateTimeFormat("zh-u-ca-chinese",{month:"long",day:"numeric"});
    }
    if(!fmt)return "农历 --";
    var text="";
    if(typeof fmt.formatToParts==="function"){
      var parts=fmt.formatToParts(d),month="",day="";
      for(var i=0;i<parts.length;i++){
        if(parts[i].type==="month")month=parts[i].value;
        if(parts[i].type==="day")day=parts[i].value;
      }
      text=(month||"")+(day||"");
    }
    if(!text)text=fmt.format(d);
    text=String(text||"").replace(/\s+/g,"");
    if(!text||text==="InvalidDate")return "农历 --";
    text=text.replace(/(\d+)月/g,function(_,n){
      var map={1:"正",11:"冬",12:"腊"};
      n=parseInt(n,10);
      return (map[n]||zhNum(n))+"月";
    });
    text=text.replace(/(\d+)日?/g,function(_,n){ return lunarDayText(parseInt(n,10)); });
    return "农历 "+text;
  }catch(e){
    return "农历 --";
  }
}
function nowParts(){
  var d=new Date();
  var week=["星期日","星期一","星期二","星期三","星期四","星期五","星期六"][d.getDay()];
  var lunar=lunarText(d);
  return {
    time: String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0")+":"+String(d.getSeconds()).padStart(2,"0"),
    date: d.getFullYear()+"年"+String(d.getMonth()+1).padStart(2,"0")+"月"+String(d.getDate()).padStart(2,"0")+"日 "+week+" · "+lunar
  };
}
function updateClock(){var p=nowParts();setText("nowTime",p.time);setText("dateText",p.date);}
function selectedDate(){
  var d=new Date();
  d.setDate(d.getDate()+selectedDayOffset);
  return d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0");
}
function selectedDayLabel(){
  return selectedDayOffset===0?"今日":"明日";
}
function apiUrl(path){
  return path+"?date="+encodeURIComponent(selectedDate());
}
function updateDayButtons(){
  var today=$("todayBtn"), tomorrow=$("tomorrowBtn");
  if(today){if(selectedDayOffset===0){today.classList.add("active");}else{today.classList.remove("active");}}
  if(tomorrow){if(selectedDayOffset===1){tomorrow.classList.add("active");}else{tomorrow.classList.remove("active");}}
  setText("chartTitle","青岛"+selectedDayLabel()+"潮汐曲线");
  setText("airTempLabel",selectedDayOffset===0?"当前气温":"明日最高");
  setText("tempRangeLabel",selectedDayLabel()+"温度");
}
function switchForecastDay(offset){
  selectedDayOffset=offset;
  lastTideRising=null;
  var els=document.querySelectorAll(".module-unavailable");
  for(var i=0;i<els.length;i++){els[i].parentNode.removeChild(els[i]);}
  updateDayButtons();
  refreshAllData();
}
function fetchJSON(url,timeout,cb){
  timeout=timeout||20000;
  var sep=url.indexOf("?")>=0?"&":"?";
  var xhr=new XMLHttpRequest();
  xhr.open("GET",url+sep+"_t="+Date.now(),true);
  xhr.timeout=timeout;
  xhr.onload=function(){
    if(xhr.status>=200&&xhr.status<300){
      try{cb(null,JSON.parse(xhr.responseText));}catch(e){cb(e);}
    }else{cb(new Error("HTTP "+xhr.status));}
  };
  xhr.onerror=function(){cb(new Error("Network error"));};
  xhr.ontimeout=function(){cb(new Error("Timeout"));};
  xhr.send();
}
function reloadTyphoonFrame(){
  var f=$("typhoonFrame"); if(f) f.src="https://www.bhyb.org.cn/typhoon/?t="+Date.now();
}
function formatHHMM(s){
  if(!s||s==="-")return "--";
  if(String(s).indexOf(":")>=0){var parts=String(s).split(":");var h=parts[0],m=parts[1];return String(parseInt(h,10)).padStart(2,"0")+":"+String(parseInt(m,10)).padStart(2,"0");}
  var raw=String(s).replace(":","").padStart(4,"0"); return raw.slice(0,2)+":"+raw.slice(2,4);
}
function timeToMin(s){
  if(!s||s==="-")return null; var t=formatHHMM(s); var parts=t.split(":"); var h=Number(parts[0]),m=Number(parts[1]);
  if(isNaN(h)||isNaN(m))return null; return h*60+m;
}
function parseTidePointTime(s){
  if(s===null||s===undefined||s==="")return null; var text=String(s);
  if(text.indexOf(":")>=0){var parts=text.split(":");var hh=parts[0],mm=parts[1];var h=parseInt(hh,10),m=parseInt(mm,10);if(isNaN(h)||isNaN(m))return null;return {label:String(h).padStart(2,"0")+":"+String(m).padStart(2,"0"),minute:h*60+m};}
  var h=parseInt(text,10); if(isNaN(h))return null; return {label:String(h).padStart(2,"0")+":00",minute:h*60};
}
function todayKey(){return selectedDate();}
function normalizeDateKey(s){
  if(!s)return ""; var datePart=String(s).split(" ")[0].replace(/-/g,"/"); var p=datePart.split("/");
  if(p.length<3)return ""; return p[0]+"-"+String(p[1]).padStart(2,"0")+"-"+String(p[2]).padStart(2,"0");
}
function windLevelText(speedText){
  var speed=parseFloat(String(speedText||"").replace(/[^\d.]/g,""));
  if(isNaN(speed))return "--";
  var levels=[
    [1,"0级 静风"],[5,"1级 软风"],[11,"2级 轻风"],[19,"3级 微风"],
    [28,"4级 和风"],[38,"5级 清风"],[49,"6级 强风"],[61,"7级 疾风"],
    [74,"8级 大风"],[88,"9级 烈风"],[102,"10级 狂风"],[117,"11级 暴风"],[Infinity,"12级 飓风"]
  ];
  var found=null;
  for(var i=0;i<levels.length;i++){if(speed<=levels[i][0]){found=levels[i];break;}}
  return (found||levels[levels.length-1])[1];
}
function parseWindDegree(degreeText){
  var degree=parseFloat(String(degreeText||"").replace(/[^\d.]/g,""));
  if(isNaN(degree))return null;
  return ((degree%360)+360)%360;
}
function waveLevelText(waveText){
  var wave=parseFloat(String(waveText||"").replace(/[^\d.]/g,""));
  if(isNaN(wave))return "--";
  if(wave<0.3)return "平静";
  if(wave<0.8)return "轻浪";
  if(wave<1.5)return "中浪";
  if(wave<2.5)return "大浪";
  return "风浪大";
}
function waterComfortText(tempText){
  var temp=parseFloat(String(tempText||"").replace(/[^\d.]/g,""));
  if(isNaN(temp))return "--";
  if(temp<18)return "偏冷";
  if(temp<22)return "较凉";
  if(temp<=28)return "舒适";
  if(temp<=31)return "偏暖";
  return "较热";
}
function seaRiskText(waveText, swimTip){
  var wave=parseFloat(String(waveText||"").replace(/[^\d.]/g,""));
  var tip=String(swimTip||"");
  if(tip.indexOf("不")>=0||tip.indexOf("禁")>=0||tip.indexOf("危险")>=0)return "谨慎下海";
  if(!isNaN(wave)&&wave>=1.5)return "风浪偏大";
  if(!isNaN(wave)&&wave>=0.8)return "注意浪涌";
  if(tip.indexOf("适宜")>=0)return "风险较低";
  return tip&&tip!=="--"?tip:"关注海况";
}
function weatherIconClass(weatherText){
  var text=String(weatherText||"");
  if(text.indexOf("雷")>=0)return "storm";
  if(text.indexOf("雨")>=0)return "rainy";
  if(text.indexOf("雪")>=0)return "snow";
  if(text.indexOf("雾")>=0||text.indexOf("霾")>=0)return "fog";
  if(text.indexOf("晴")>=0)return "sunny";
  return "cloudy";
}
function renderWeather(obj,updateTime){
  setText("weatherTime","更新 "+(updateTime||"--")); setText("weatherText",obj&&obj.weather);
  var icon=$("weatherIcon"); if(icon) icon.className="weather-icon "+weatherIconClass(obj&&obj.weather);
  setText("airTemp",obj&&obj.temperature);
  setText("tempRange",obj&&obj.temperature_range);
  setText("humidity",obj&&obj.humidity);
  var windText=(obj&&obj.wind_direction?obj.wind_direction:"--")+" "+(obj?windLevelText(obj.wind_speed):"--");
  setText("windDirection",windText);
  var windDegree=parseWindDegree(obj&&obj.wind_direction_degree);
  var needle=$("windNeedle"); if(needle&&windDegree!==null) needle.style.transform="rotate("+windDegree+"deg)";
}
// 计算紫外线指数（基于天气、季节和时间估算）
function estimateUVIndex(){
  var now=new Date();
  var hour=now.getHours();
  var month=now.getMonth()+1;
  var weatherText=($("weatherText")&&$("weatherText").textContent)||"";
  var base=3;
  // 季节影响（夏季最高）
  if(month>=6&&month<=8)base=8;
  else if(month===5||month===9)base=6;
  else if(month===4||month===10)base=4;
  else if(month===3||month===11)base=2;
  else base=1;
  // 天气影响
  if(weatherText.indexOf("晴")>=0)base*=1.2;
  else if(weatherText.indexOf("多云")>=0)base*=0.8;
  else if(weatherText.indexOf("阴")>=0)base*=0.5;
  else if(weatherText.indexOf("雨")>=0||weatherText.indexOf("雪")>=0)base*=0.2;
  // 时间影响（中午最强）
  var hourFactor=1;
  if(hour>=10&&hour<=14)hourFactor=1.2;
  else if(hour>=8&&hour<10)hourFactor=0.8;
  else if(hour>14&&hour<=16)hourFactor=0.8;
  else if(hour>=6&&hour<8)hourFactor=0.4;
  else if(hour>16&&hour<=18)hourFactor=0.4;
  else hourFactor=0.1;
  var uv=Math.round(base*hourFactor*10)/10;
  return Math.max(0,Math.min(11,uv));
}
// 紫外线等级描述
function uvLevelText(uv){
  if(uv<=2)return "弱";
  if(uv<=4)return "较弱";
  if(uv<=6)return "中等";
  if(uv<=8)return "较强";
  if(uv<=10)return "强";
  return "很强";
}
function renderWave(obj,updateTime){
  var offshore=$("offshoreWaveHeight"), offshoreText=offshore?offshore.textContent:"";
  var levelWave=offshoreText&&offshoreText!=="--"?offshoreText:(obj&&obj.wave_height);
  setText("waveTime","更新 "+(updateTime||"--")); setText("waterTemp",obj&&obj.water_temp);
  setText("waveLevel",obj?waveLevelText(levelWave):"--");
  setText("swimTip",obj&&obj.swim_tip); var el=$("swimTip"); if(el) el.className=obj&&obj.swim_tip==="适宜"?"safe":(obj&&obj.swim_tip?"bad":"");
  // 紫外线指数
  var uvEl=$("uvIndex");
  if(uvEl){
    var uv=estimateUVIndex();
    var uvText=uv.toFixed(1)+" · "+uvLevelText(uv);
    uvEl.textContent=uvText;
    uvEl.className=uv>=6?"warn":(uv>=3?"":"safe");
  }
  // 浴场列表（增强版：显示浪高、水温）
  var beachList=$("beachList");
  var beachBody=beachList?beachList.querySelector(".beach-list-body"):null;
  if(beachBody&&obj&&obj.beaches&&obj.beaches.length>0){
    beachList.style.display="flex";
    var html="";
    for(var i=0;i<Math.min(obj.beaches.length,9);i++){
      var b=obj.beaches[i];
      var statusCls="warn";
      var statusText=b.swim_tip||"--";
      if(b.swim_tip==="适宜")statusCls="ok";
      else if(b.swim_tip==="不适宜")statusCls="bad";
      html+='<div class="beach-item">';
      html+='<div class="beach-item-top">';
      html+='<span class="beach-name">'+b.name+'</span>';
      html+='<span class="beach-status '+statusCls+'">'+statusText+'</span>';
      html+='</div>';
      html+='<div class="beach-item-detail">';
      html+='<span>🌊 '+(b.wave_height||'--')+'</span>';
      html+='<span>🌡 '+(b.water_temp||'--')+'</span>';
      html+='</div>';
      html+='</div>';
    }
    beachBody.innerHTML=html;
  }else if(beachList){
    beachList.style.display="none";
  }
}
function tideCurrentStatus(){
  var now=new Date();
  var hm=now.getHours()*60+now.getMinutes();
  var list=lastTideList||[];
  if(!list.length)return "";
  // 找当前之后的第一个潮
  var next=null, prev=null;
  for(var i=0;i<list.length;i++){
    var m=timeToMin(list[i].t);
    if(m===null)continue;
    if(m>hm){next=list[i];break;}
    prev=list[i];
  }
  if(!next&&prev)next=list[0];
  if(!next)return "";
  return (next.type==="高潮"?"涨潮中→":"落潮中→")+next.type;
}
function renderTide(res,upTime){
  tideRawData=res; setText("tideUpdate","更新 "+(upTime||"--")); setText("globalUpdate","数据更新 "+(upTime||"--"));
  if(!res||!res.data||!Array.isArray(res.data.rows)){ return; }
  var item=res.data.rows[0]; if(!item){ return;}
  var list=[
    {t:item.FIRSTHIGHTIME,l:item.FIRSTHIGHLEVEL,type:"高潮",cls:"high"},
    {t:item.SECONDHIGHTIME,l:item.SECONDHEIGHTLEVEL,type:"高潮",cls:"high"},
    {t:item.FIRSTLOWTIME,l:item.FIRSTLOWLEVEL,type:"低潮",cls:"low"},
    {t:item.SECONDLOWTIME,l:item.SECONDLOWLEVEL,type:"低潮",cls:"low"}
  ].filter(function(i){return i.t&&i.t!=="-";}).sort(function(a,b){return (timeToMin(a.t)!=null?timeToMin(a.t):9999)-(timeToMin(b.t)!=null?timeToMin(b.t):9999);});
  lastTideList=list;
  calcTideStatus(list);
}
function interpolateCurrentLevel(points, nowMin){
  var curve=(points||[]).filter(function(p){return p.minute!==null&&!isNaN(p.value);}).sort(function(a,b){return a.minute-b.minute;});
  if(!curve.length)return null;
  if(nowMin<=curve[0].minute)return Math.round(curve[0].value);
  if(nowMin>=curve[curve.length-1].minute)return Math.round(curve[curve.length-1].value);
  for(var i=1;i<curve.length;i++){
    var prev=curve[i-1], next=curve[i];
    if(nowMin>=prev.minute&&nowMin<=next.minute){
      var ratio=(nowMin-prev.minute)/Math.max(1,next.minute-prev.minute);
      return Math.round(prev.value+(next.value-prev.value)*ratio);
    }
  }
  return null;
}
function calcTideStatus(list){
  if(document.querySelector("#tideCard .module-unavailable"))return;
  var now=new Date(), nowMin=selectedDayOffset===0 ? now.getHours()*60+now.getMinutes() : 0;
  if(selectedDayOffset===0){
    var statusTime=String(now.getMonth()+1).padStart(2,"0")+"-"+String(now.getDate()).padStart(2,"0")+" "+String(now.getHours()).padStart(2,"0")+":"+String(now.getMinutes()).padStart(2,"0");
    setText("tideUpdate","状态 "+statusTime);
  }
  var pts=list.map(function(x){return {min:timeToMin(x.t),height:Number(x.l),type:x.type,time:formatHHMM(x.t)};}).filter(function(p){return p.min!==null&&!isNaN(p.height);}).sort(function(a,b){return a.min-b.min;});
  if(pts.length<2){setText("tideBadge","数据不足");setText("tideStatusText","潮位数据不足");return;}
  var curve=(lastChartPoints||[]).filter(function(p){return p.minute!==null&&!isNaN(p.value);}).sort(function(a,b){return a.minute-b.minute;});

  // 如果是今日模式且有明日数据，将明日第一个高低潮加入计算（用于末段进度）
  var hasTomorrowNext=false;
  if(selectedDayOffset===0 && tomorrowTideList.length>0){
    var tomPts=tomorrowTideList.map(function(x){return {min:timeToMin(x.t)+1440,height:Number(x.l),type:x.type,time:formatHHMM(x.t),isTomorrow:true};}).filter(function(p){return p.min!==null&&!isNaN(p.height);}).sort(function(a,b){return a.min-b.min;});
    if(tomPts.length>0){
      pts=pts.concat(tomPts);
      hasTomorrowNext=true;
    }
  }

  var prev=pts[0],next=pts[pts.length-1];
  var futureExtrema=pts.filter(function(p){return p.min>nowMin;});
  if(futureExtrema.length){
    next=futureExtrema[0];
    var prevArr=pts.filter(function(p){return p.min<=nowMin;}); prev=prevArr.length?prevArr[prevArr.length-1]:pts[0];
  }else{
    prev=pts[pts.length-1];
    next=null;
  }
  var rising=false;
  if(curve.length>=2){
    var cPrev=curve[0], cNext=curve[1];
    if(nowMin>=curve[curve.length-1].minute){
      cPrev=curve[curve.length-2];
      cNext=curve[curve.length-1];
    }else{
      for(var i=1;i<curve.length;i++){
        if(nowMin<=curve[i].minute){cPrev=curve[i-1];cNext=curve[i];break;}
      }
    }
    rising=cNext.value-cPrev.value>0;
  }else if(next){
    rising=next.height-prev.height>0;
  }
  setText("tideBadge",selectedDayOffset===0 ? (rising?"涨潮中":"退潮中") : "明日预报");
  var badgeEl=$("tideBadge");
  if(badgeEl){
    badgeEl.classList.remove("rising","falling");
    if(selectedDayOffset===0){
      badgeEl.classList.add(rising?"rising":"falling");
    }
  }
  setText("tideTrend",rising?"水位上升":"水位下降");
  if(selectedDayOffset===0&&lastTideRising!==null&&rising&&!lastTideRising&&soundEnabled){playRisingSound();}
  lastTideRising=rising;
  if(next){
    var nextPrefix=next.isTomorrow?"明日 ":"";
    setText("tideStatusText",nextPrefix+"下一次"+next.type+" "+next.time+"，潮位 "+next.height+" cm");
    setText("nextTideDelta",formatDuration(next.min-nowMin));
    if(prev&&prev.min<nowMin&&prev.min<next.min){
      setText("tidePhase",prev.type+"→"+next.type);
      var phaseTotal=Math.max(1,next.min-prev.min);
      var phaseProgress=Math.max(0,Math.min(100,Math.round((nowMin-prev.min)/phaseTotal*100)));
      setText("tideProgress",phaseProgress+"%");
    }else{
      setText("tidePhase","等待"+next.type);
      setText("tideProgress","0%");
    }
  }else{
    setText("tideStatusText",selectedDayLabel()+"后续无高低潮预报");
    setText("nextTideDelta",selectedDayLabel()+"无");
    setText("tidePhase",selectedDayLabel()+"末段");
    setText("tideProgress","--");
  }
  var currentLevel=interpolateCurrentLevel(curve,nowMin);
  setText("currentLevel",currentLevel!==null?currentLevel:"--");
  var highs=pts.filter(function(p){return p.type==="高潮";}), lows=pts.filter(function(p){return p.type==="低潮";});
  var nextHigh=null,nextLow=null;
  for(var hi=0;hi<highs.length;hi++){if(highs[hi].min>nowMin){nextHigh=highs[hi];break;}}
  for(var li=0;li<lows.length;li++){if(lows[li].min>nowMin){nextLow=lows[li];break;}}

  // 如果今日没有更多高低潮，且是今日模式，则从明日数据中取第一个
  if(selectedDayOffset===0 && tomorrowTideList.length>0){
    var tomHighs=tomorrowTideList.filter(function(p){return p.type==="高潮";});
    var tomLows=tomorrowTideList.filter(function(p){return p.type==="低潮";});
    if(!nextHigh && tomHighs.length>0){
      nextHigh={t:tomHighs[0].t,l:tomHighs[0].l,type:"高潮",min:timeToMin(tomHighs[0].t)+1440,isTomorrow:true};
    }
    if(!nextLow && tomLows.length>0){
      nextLow={t:tomLows[0].t,l:tomLows[0].l,type:"低潮",min:timeToMin(tomLows[0].t)+1440,isTomorrow:true};
    }
  }

  var nextHighText=nextHigh?((nextHigh.isTomorrow?"明日 ":"")+(nextHigh.time||formatHHMM(nextHigh.t))):"--";
  var nextLowText=nextLow?((nextLow.isTomorrow?"明日 ":"")+(nextLow.time||formatHHMM(nextLow.t))):"--";
  setText("nextHigh",nextHighText);
  setText("nextLow",nextLowText);
  var highDeltaText="--";
  var lowDeltaText="--";
  if(nextHigh){
    if(nextHigh.isTomorrow){
      var minLeft=nextHigh.min-nowMin;
      highDeltaText=formatDuration(minLeft);
    }else{
      highDeltaText=formatDuration(nextHigh.min-nowMin);
    }
  }else{
    highDeltaText=selectedDayLabel()+"无";
  }
  if(nextLow){
    if(nextLow.isTomorrow){
      var minLeftL=nextLow.min-nowMin;
      lowDeltaText=formatDuration(minLeftL);
    }else{
      lowDeltaText=formatDuration(nextLow.min-nowMin);
    }
  }else{
    lowDeltaText=selectedDayLabel()+"无";
  }
  setText("highDelta",highDeltaText);
  setText("lowDelta",lowDeltaText);
  renderTideSummary(pts);
}
function initAudio(){
  if(!audioCtx) audioCtx=new (window.AudioContext||window.webkitAudioContext)();
}
function playRisingSound(){
  try{
    initAudio();
    if(audioCtx.state==="suspended") audioCtx.resume();
    var t=audioCtx.currentTime;
    var osc1=audioCtx.createOscillator(); var g1=audioCtx.createGain();
    osc1.type="sine"; osc1.frequency.setValueAtTime(523,t); osc1.frequency.exponentialRampToValueAtTime(784,t+0.15);
    g1.gain.setValueAtTime(0.08,t); g1.gain.exponentialRampToValueAtTime(0.001,t+0.5);
    osc1.connect(g1); g1.connect(audioCtx.destination); osc1.start(t); osc1.stop(t+0.5);
    var osc2=audioCtx.createOscillator(); var g2=audioCtx.createGain();
    osc2.type="sine"; osc2.frequency.setValueAtTime(659,t+0.18); osc2.frequency.exponentialRampToValueAtTime(1047,t+0.35);
    g2.gain.setValueAtTime(0.06,t+0.18); g2.gain.exponentialRampToValueAtTime(0.001,t+0.65);
    osc2.connect(g2); g2.connect(audioCtx.destination); osc2.start(t+0.18); osc2.stop(t+0.65);
  }catch(e){}
}
function toggleSound(){
  soundEnabled=!soundEnabled; initAudio();
  var btn=$("soundBtn"); if(btn){
    btn.innerHTML=soundEnabled?'🔊 <span>声音开</span>':'🔇 <span>声音关</span>';
  }
}
function formatDuration(minutes){
  if(minutes===null||minutes===undefined||minutes<0)return "--";
  var h=Math.floor(minutes/60), m=minutes%60;
  if(h<=0)return m+"分钟";
  return h+"小时"+(m>0?m+"分":"");
}
function renderTideSummary(pts){
  if(!pts||!pts.length){setText("tideRange","--");return;}
  var heights=pts.map(function(p){return p.height;}).filter(function(v){return !isNaN(v);});
  if(!heights.length){setText("tideRange","--");return;}
  var max=Math.max.apply(null,heights), min=Math.min.apply(null,heights);
  setText("tideRange",Math.round(max-min)+" cm");
}
function parseChartPoints(rawArr){
  var arr=Array.isArray(rawArr)?rawArr:[]; var all=arr.map(function(it){
    var parsed=parseTidePointTime(it&&it.TIDETIME), val=Number(it&&it.TIDEHEIGHT||0); if(!parsed)return null;
    return {dateKey:normalizeDateKey(it&&it.TIDEDATE||""),label:parsed.label,minute:parsed.minute,value:isNaN(val)?0:val,pointType:it&&it.POINT_TYPE||"hour",extremaType:it&&it.EXTREMA_TYPE||""};
  }).filter(Boolean);
  var day=todayKey(); var points=all.filter(function(p){return p.dateKey===day;}); if(points.length===0&&all.length>0)points=all.filter(function(p){return p.dateKey===all[0].dateKey;});
  return points.sort(function(a,b){return a.minute-b.minute;});
}
function adaptiveChartFont(base,min,max){var scale=Math.min(window.innerWidth/1280,window.innerHeight/760);return Math.max(min,Math.min(max,Math.round(base*scale)));}
function initChart(){
  if(typeof echarts==="undefined")return false; if(!tideChart){tideChart=echarts.init($("tideChart")); window.addEventListener("resize",function(){tideChart.resize();clearTimeout(resizeTimer);resizeTimer=setTimeout(function(){if(lastChartRaw)renderChart(lastChartRaw,"",lastChartSite);},160);});}
  return true;
}
function renderChart(rawArr,msg,site){
  lastChartRaw=Array.isArray(rawArr)?rawArr:null; lastChartSite=site||null; var points=parseChartPoints(rawArr);
  lastChartPoints=points;
  if(lastTideList.length) calcTideStatus(lastTideList);
  setText("chartSource",site&&site.code?site.name+"("+site.code+")":"全球潮汐平台");
  if(!points.length){$("tideChart").innerText=msg||"暂无实时曲线数据";return;}
  if(!initChart()){ $("tideChart").innerText="ECharts 加载中"; return; }
  var axisFont=adaptiveChartFont(10,8,13), markFont=adaptiveChartFont(9,7,11), nameFont=adaptiveChartFont(11,9,14);
  var maxVal=Math.max.apply(null,points.map(function(p){return p.value;}));
  var isMobileView=document.documentElement.classList.contains("mobile");
  var gridLeft=isMobileView?26:44, gridRight=isMobileView?26:44, gridTop=isMobileView?32:42, gridBottom=isMobileView?28:34;
  var markData=points.filter(function(p){return p.pointType==="extrema";}).map(function(p){return {name:p.extremaType,coord:[p.label,p.value],value:p.value,labelText:p.extremaType+" "+p.label+"\n"+p.value+"cm",itemStyle:{color:p.extremaType==="高潮"?"#ffab00":"#00e676"},label:{formatter:function(params){return params.data.labelText;},color:p.extremaType==="高潮"?"#ffab00":"#00e676",fontSize:markFont,fontWeight:"bold",lineHeight:markFont+1,position:"right",distance:4,offset:[0,-14]}};});
  tideChart.setOption({
    backgroundColor:"transparent",
    tooltip:{trigger:"axis",formatter:function(p){return "时间："+p[0].axisValue+"<br>潮高："+p[0].value+" cm";},backgroundColor:"rgba(15,21,40,.94)",borderColor:"rgba(0,229,255,.35)",textStyle:{color:"#e8eaf6",fontSize:axisFont}},
    grid:{left:gridLeft,right:gridRight,top:gridTop,bottom:gridBottom,containLabel:true},
    xAxis:{type:"category",data:points.map(function(p){return p.label;}),axisLabel:{rotate:0,interval:2,fontSize:axisFont,margin:6,color:"rgba(232,234,246,.65)"},axisLine:{lineStyle:{color:"rgba(0,229,255,.28)"}},axisTick:{lineStyle:{color:"rgba(0,229,255,.22)"}}},
    yAxis:{name:"潮高(cm)",type:"value",max:Math.ceil((maxVal+35)/50)*50,nameTextStyle:{fontSize:nameFont,color:"rgba(232,234,246,.65)"},axisLabel:{fontSize:axisFont,color:"rgba(232,234,246,.65)"},axisLine:{lineStyle:{color:"rgba(0,229,255,.28)"}},splitLine:{lineStyle:{color:"rgba(255,255,255,.07)"}}},
    series:[{name:"潮高",type:"line",data:points.map(function(p){return p.value;}),smooth:true,symbolSize:4,itemStyle:{color:"#00e5ff"},lineStyle:{color:"#00e5ff",width:2.4,shadowBlur:8,shadowColor:"rgba(0,229,255,.45)"},areaStyle:{color:{type:"linear",colorStops:[{offset:0,color:"rgba(0,229,255,.26)"},{offset:1,color:"rgba(0,229,255,.03)"}]}},markPoint:{symbol:"circle",symbolSize:18,data:markData},markLine:{symbol:"none",silent:true,data:[],lineStyle:{color:"#ff5252",width:1.5,type:"solid",shadowBlur:6,shadowColor:"rgba(255,82,82,.5)"},label:{show:true,formatter:"现在",color:"#ff5252",fontSize:markFont,fontWeight:"bold",position:"end",distance:[4,0],backgroundColor:"rgba(6,10,20,.8)",padding:[2,6,2,6],borderRadius:3}}}]
  },true);
  // 启动当前时间标线更新（明日模式下不显示）
  if(selectedDayOffset===0){
    startNowMarkLine();
  }else{
    if(nowMarkLineTimer){clearInterval(nowMarkLineTimer);nowMarkLineTimer=null;}
    if(tideChart)tideChart.setOption({series:[{markLine:{data:[]}}]});
  }
}
var nowMarkLineTimer=null;
function startNowMarkLine(){
  if(!tideChart||!lastChartPoints||!lastChartPoints.length)return;
  if(selectedDayOffset!==0)return;
  updateNowMarkLine();
  if(nowMarkLineTimer)clearInterval(nowMarkLineTimer);
  nowMarkLineTimer=setInterval(updateNowMarkLine,60000);
}
function updateNowMarkLine(){
  if(!tideChart||!lastChartPoints||!lastChartPoints.length)return;
  if(selectedDayOffset!==0){
    tideChart.setOption({series:[{markLine:{data:[]}}]});
    return;
  }
  var now=new Date();
  var nowHm=("0"+now.getHours()).slice(-2)+":"+("0"+now.getMinutes()).slice(-2);
  var points=lastChartPoints;
  // 找到当前时间在x轴上的位置（在两个数据点之间插值）
  var nowIdx=-1;
  var nowRatio=0;
  for(var i=0;i<points.length-1;i++){
    var t1=parseChartTime(points[i].label);
    var t2=parseChartTime(points[i+1].label);
    var tNow=parseChartTime(nowHm);
    // 处理跨天的情况
    if(t2<t1)t2+=24*60;
    if(tNow<t1)tNow+=24*60;
    if(tNow>=t1&&tNow<=t2){
      nowIdx=i;
      nowRatio=(tNow-t1)/(t2-t1);
      break;
    }
  }
  if(nowIdx<0){
    // 当前时间不在数据范围内，隐藏标线
    tideChart.setOption({series:[{markLine:{data:[]}}]});
    return;
  }
  // 计算当前潮高（插值）
  var v1=points[nowIdx].value;
  var v2=points[nowIdx+1].value;
  var nowVal=v1+(v2-v1)*nowRatio;
  // 用xAxis的category索引定位标线
  tideChart.setOption({series:[{markLine:{data:[{xAxis:nowIdx+nowRatio,label:{formatter:"现在 "+nowHm}}]}}]});
}
function parseChartTime(hm){
  var parts=hm.split(":");
  return parseInt(parts[0])*60+parseInt(parts[1]);
}
function clearModuleUnavailable(cardId){var card=$(cardId);if(card){var h=card.querySelector(".module-unavailable");if(h)h.parentNode.removeChild(h);}}
function setAllText(ids, text){for(var i=0;i<ids.length;i++){setText(ids[i],text);}}
function showWeatherUnavailable(){
  clearModuleUnavailable("weatherCard");
  setAllText(["airTemp","tempRange","humidity","windDirection"],"未知");
  setText("weatherTime","暂无明日数据"); setText("weatherText","未知");
  var icon=$("weatherIcon"); if(icon) icon.className="weather-icon";
}
function showWaveUnavailable(){
  clearModuleUnavailable("seaCard");
  setAllText(["offshoreWaveHeight","waterTemp","waveLevel","swimTip","seaRisk"],"未知");
  setText("waveTime","暂无明日数据");
}
function showTideUnavailable(){
  clearModuleUnavailable("tideCard");
  setAllText(["tideBadge","tideStatusText","nextHigh","nextLow","currentLevel","tideTrend","tidePhase","tideProgress","highDelta","lowDelta","tideRange"],"未知");
  setText("tideUpdate","暂无明日数据"); setText("globalUpdate","数据更新 --");
}
function showChartUnavailable(){
  clearModuleUnavailable("chartCard");
  setText("chartTime","暂无明日数据");
  var tc=$("tideChart"); if(tc) tc.innerHTML='<div class="module-unavailable">暂无明日数据</div>';
}
function loadWeather(){fetchJSON(apiUrl("/api/weather"),20000,function(e,r){if(r&&r.tomorrow_unavailable){showWeatherUnavailable();return;}if(r&&r.data)renderWeather(r.data,r.updateTime);});}
function loadWave(){fetchJSON(apiUrl("/api/wave"),20000,function(e,r){if(r&&r.tomorrow_unavailable){showWaveUnavailable();return;}if(r&&r.data)renderWave(r.data,r.updateTime);});}
function loadOffshoreWave(){fetchJSON(apiUrl("/api/offshore_wave"),20000,function(e,r){if(r&&r.tomorrow_unavailable){setText("offshoreWaveHeight","未知");return;}if(r&&r.data){setText("offshoreWaveHeight",r.data.wave_height);setText("waveLevel",waveLevelText(r.data.wave_height));var swimTip=$("swimTip");setText("seaRisk",seaRiskText(r.data.wave_height,swimTip?swimTip.textContent:""));}});}
function loadAlarm(){fetchJSON(apiUrl("/api/alarm"),20000,function(e,r){});}
function loadSdAlarm(){fetchJSON("/api/sd_alarm",45000,function(e,r){
  var listCard=$("alarmListCard");var container=$("alarmListContainer");var timeEl=$("alarmListTime");
  if(!r||e||!r.data||!Array.isArray(r.data)||r.data.length===0){
    if(container)container.innerHTML='<div class="alarm-list-empty">暂无预警信息</div>';
    if(timeEl)timeEl.textContent=r&&r.updateTime?r.updateTime:"--";
    return;
  }
  // 列表形式展示
  var html="";
  for(var i=0;i<r.data.length;i++){
    var item=r.data[i];
    var levelCls="blue";
    var levelText="蓝色";
    var lv=(item.level||"")+(item.title||"");
    if(lv.indexOf("红色")>=0){levelCls="red";levelText="红色";}
    else if(lv.indexOf("橙色")>=0){levelCls="orange";levelText="橙色";}
    else if(lv.indexOf("黄色")>=0){levelCls="yellow";levelText="黄色";}
    var type=item.type||"气象预警";
    var title=item.title||"--";
    var pubTime=item.publish_time||"--";
    var idx=i;
    html+='<div class="alarm-list-item item-'+levelCls+'" onclick="openAlarmModal('+idx+')">';
    html+='<span class="alarm-level-tag '+levelCls+'">'+levelText+'</span>';
    html+='<div class="alarm-item-body">';
    html+='<div class="alarm-item-title">'+title+'</div>';
    html+='<div class="alarm-item-meta">';
    html+='<span class="alarm-item-type">'+type+'</span>';
    html+='<span class="alarm-item-time">'+pubTime+'</span>';
    html+='</div>';
    html+='</div>';
    html+='</div>';
  }
  if(container)container.innerHTML=html;
  if(timeEl)timeEl.textContent=r.updateTime||"--";
  // 缓存数据供弹窗使用
  window._alarmData=r.data;
});}

// 统一格式化预警时间为 YYYY-MM-DD HH:MM
function formatAlarmTime(t){
  if(!t||t==="--")return "";
  var s=String(t).trim();
  if(!s)return "";
  s=s.replace(/年|月|\/|\./g,"-").replace(/日/g," ").replace(/时/g,":").replace(/分/g,"").replace(/\s+/g," ").trim();
  var parts=s.split(" ");
  var d=parts[0]?parts[0].split("-"):[];
  if(d.length<3)return t;
  var y=parseInt(d[0]),mo=parseInt(d[1]),da=parseInt(d[2]);
  if(isNaN(y)||isNaN(mo)||isNaN(da))return t;
  var yStr=String(y),moStr=String(mo).padStart(2,"0"),daStr=String(da).padStart(2,"0");
  var timePart="";
  if(parts.length>1&&parts[1]){
    var tp=parts[1].split(":");
    var h=parseInt(tp[0])||0,m=parseInt(tp[1])||0;
    timePart=" "+String(h).padStart(2,"0")+":"+String(m).padStart(2,"0");
  }
  return yStr+"-"+moStr+"-"+daStr+timePart;
}

function loadCmaAlarm(){
  var marquee=$("cmaAlarmMarquee");
  var bar=$("cmaAlarmBar");
  var timeEl=$("cmaAlarmTime");
  if(!marquee)return;
  var cmaData=[],sdData=[],qdData=[],cmaDone=false,sdDone=false,qdDone=false,cmaTime="",sdTime="",qdTime="";
  function tryRender(){
    if(!cmaDone||!sdDone||!qdDone)return;
    var allData=[];
    for(var i=0;i<cmaData.length;i++){allData.push(cmaData[i]);}
    for(var j=0;j<sdData.length;j++){allData.push(sdData[j]);}
    for(var m=0;m<qdData.length;m++){allData.push(qdData[m]);}
    // 按发布时间倒序排序（最新的在前）
    function parseAlarmTime(t){
      if(!t||t==="--")return -1;
      var s=String(t).trim();
      if(!s)return -1;
      // 统一格式：替换斜杠、年月日等分隔符
      s=s.replace(/年|月|\/|\./g,"-").replace(/日/g," ").replace(/时/g,":").replace(/\s+/g," ").trim();
      // 提取日期和时间部分
      var dateStr="",timeStr="";
      var sp=s.split(" ");
      dateStr=sp[0]||"";
      if(sp.length>1)timeStr=sp[1]||"";
      var d=dateStr.split("-");
      if(d.length<3)return -1;
      var y=parseInt(d[0]),mo=parseInt(d[1]),da=parseInt(d[2]);
      if(isNaN(y)||isNaN(mo)||isNaN(da))return -1;
      var h=0,mi=0,se=0;
      if(timeStr){
        var t2=timeStr.split(":");
        h=parseInt(t2[0])||0;
        mi=parseInt(t2[1])||0;
        se=parseInt(t2[2])||0;
      }
      return new Date(y,mo-1,da,h,mi,se).getTime();
    }
    allData.sort(function(a,b){
      var ta=parseAlarmTime(a.publish_time);
      var tb=parseAlarmTime(b.publish_time);
      if(ta<0&&tb<0)return 0;
      if(ta<0)return 1;
      if(tb<0)return -1;
      return tb-ta;
    });
    // 只保留最近3天的预警
    var threeDaysAgo=Date.now()-3*24*60*60*1000;
    allData=allData.filter(function(item){
      var t=parseAlarmTime(item.publish_time);
      return t<0||t>=threeDaysAgo;
    });
    if(allData.length===0){
      marquee.innerHTML='<span class="cma-alarm-item item-blue">暂无预警信息</span>';
      if(bar)bar.style.display="none";
      if(timeEl)timeEl.textContent="更新 --";
      window._cmaAlarmData=[];
      return;
    }
    if(bar)bar.style.display="flex";
    var levelOrder={red:4,orange:3,yellow:2,blue:1,green:0};
    var maxLevel="blue";
    var html="";
    for(var k=0;k<allData.length;k++){
      var item=allData[k];
      var levelCls="blue";
      var levelText="蓝色";
      var lv=(item.level||"")+(item.title||"");
      if(lv.indexOf("红色")>=0){levelCls="red";levelText="红色";}
      else if(lv.indexOf("橙色")>=0){levelCls="orange";levelText="橙色";}
      else if(lv.indexOf("黄色")>=0){levelCls="yellow";levelText="黄色";}
      else if(lv.indexOf("解除")>=0){levelCls="green";levelText="解除";}
      else if(lv.indexOf("消息")>=0){levelCls="blue";levelText="消息";}
      if(levelOrder[levelCls]>levelOrder[maxLevel])maxLevel=levelCls;
      var title=item.title||"--";
      var pubTime=formatAlarmTime(item.publish_time);
      var source=item.source?"<small style=\"opacity:.45;margin-left:6px;\">["+item.source+"]</small>":"";
      html+='<span class="cma-alarm-item item-'+levelCls+'" onclick="openCmaAlarmModal('+k+')">';
      html+='<span class="lvl-'+levelCls+'">'+levelText+'</span>';
      html+=title;
      if(pubTime)html+=' <small style="opacity:.55;margin-left:4px;">'+pubTime+'</small>';
      html+=source;
      html+='</span>';
    }
    if(bar){
      bar.classList.remove("bar-blue","bar-yellow","bar-orange","bar-red");
      bar.classList.add("bar-"+maxLevel);
    }
    marquee.innerHTML=html;
    window._cmaAlarmData=allData;
    var latestTime=cmaTime||sdTime||qdTime;
    if(timeEl)timeEl.textContent="更新 "+(latestTime||"--");
  }
  fetchJSON("/api/cma_alarm",30000,function(e,r){
    cmaDone=true;
    if(!e&&r&&r.data&&Array.isArray(r.data)){
      cmaData=r.data;
      cmaTime=r.updateTime||"";
    }
    tryRender();
  });
  fetchJSON("/api/sd_alarm",45000,function(e,r){
    sdDone=true;
    if(!e&&r&&r.data&&Array.isArray(r.data)){
      sdData=r.data.map(function(it){if(!it.source)it.source="山东省气象台";return it;});
      sdTime=r.updateTime||"";
    }
    tryRender();
  });
  fetchJSON("/api/alarm",30000,function(e,r){
    qdDone=true;
    if(!e&&r&&r.data&&Array.isArray(r.data)){
      qdData=r.data.map(function(it){it.source="青岛海洋预报台";return it;});
      qdTime=r.updateTime||"";
    }
    tryRender();
  });
}

function openCmaAlarmModal(index){
  var data=window._cmaAlarmData||[];
  var item=data[index];
  if(!item){return;}
  var modal=$("alarmModal");
  if(!modal){return;}
  modal.style.display="flex";
  $("alarmModalTitle").textContent=item.title||"预警详情";
  $("alarmModalTime").textContent=formatAlarmTime(item.publish_time)||"--";
  var linkEl=$("alarmModalLink");
  if(item.url){
    _currentAlarmUrl=item.url;
    linkEl.href=item.url;
    linkEl.target="_blank";
    linkEl.rel="noopener noreferrer";
    linkEl.classList.remove("disabled");
  }else{
    _currentAlarmUrl="";
    linkEl.href="javascript:void(0)";
    linkEl.classList.add("disabled");
  }
  var bodyHtml="";
  bodyHtml+="<p><strong>预警类型：</strong>"+(item.type||"气象预警")+"</p>";
  bodyHtml+="<p><strong>预警级别：</strong>"+(item.level||"--")+"</p>";
  bodyHtml+="<p><strong>发布时间：</strong>"+(item.publish_time||"--")+"</p>";
  if(item.source)bodyHtml+="<p><strong>信息来源：</strong>"+item.source+"</p>";
  bodyHtml+="<div id=\"alarmDetailContent\" style=\"margin-top:12px;padding-top:12px;border-top:1px solid rgba(255,255,255,.1);color:rgba(232,234,246,.75);line-height:1.8;\"><em style=\"color:rgba(232,234,246,.4);\">正在加载详情...</em></div>";
  $("alarmModalBody").innerHTML=bodyHtml;
  
  // 青岛海洋预报台docx文件直接提示查看原文
  var isDocx=item.url&&(item.url.indexOf("Alermfile.aspx")>=0||item.url.toLowerCase().indexOf(".docx")>=0||item.url.toLowerCase().indexOf(".doc")>=0);
  if(isDocx){
    $("alarmDetailContent").innerHTML="<p style=\"color:rgba(232,234,246,.7);\">预警详情为文档格式，请点击下方「查看原文」按钮打开。</p>";
    return;
  }
  // 加载预警详情内容
  if(item.url){
    var detailUrl="/api/cma_alarm_detail?url="+encodeURIComponent(item.url);
    fetch(detailUrl,{cache:"no-cache"})
      .then(function(r){return r.json();})
      .then(function(res){
        if(res&&res.success&&res.data&&res.data.content){
          var content=res.data.content;
          // 处理换行：先统一换行符，再转换为HTML段落
          content=content.replace(/\\r\\n/g,"\\n").replace(/\\r/g,"\\n");
          var paras=content.split(/\\n\\s*\\n/).filter(function(p){return p.trim().length>0;});
          if(paras.length>1){
            content=paras.map(function(p){return "<p>"+p.replace(/\\n/g,"<br>")+"</p>";}).join("");
          }else{
            content="<p>"+content.replace(/\\n/g,"<br>")+"</p>";
          }
          $("alarmDetailContent").innerHTML=content;
        }else{
          $("alarmDetailContent").innerHTML="<p style=\"color:rgba(232,234,246,.4);\">暂无法获取详情内容，请点击下方查看原文链接</p>";
        }
      })
      .catch(function(){
        $("alarmDetailContent").innerHTML="<p style=\"color:rgba(232,234,246,.4);\">详情加载失败，请点击下方查看原文链接</p>";
      });
  }else{
    $("alarmDetailContent").innerHTML="<p style=\"color:rgba(232,234,246,.4);\">暂无详情内容</p>";
  }
}

var _alarmData=[];
function openAlarmModal(index){
  var data=window._alarmData||[];
  var item=data[index];
  if(!item){return;}
  var modal=$("alarmModal");
  if(!modal){return;}
  modal.style.display="flex";
  $("alarmModalTitle").textContent=item.title||"预警详情";
  $("alarmModalTime").textContent=formatAlarmTime(item.publish_time)||"--";
  _currentAlarmUrl=item.url||"";
  var sdLinkEl=$("alarmModalLink");
  if(item.url){
    sdLinkEl.href=item.url;
    sdLinkEl.target="_blank";
    sdLinkEl.rel="noopener noreferrer";
    sdLinkEl.classList.remove("disabled");
  }else{
    sdLinkEl.href="javascript:void(0)";
    sdLinkEl.classList.add("disabled");
  }
  $("alarmModalBody").innerHTML='<div class="alarm-modal-loading">加载中...</div>';
  // 获取详情内容
  var url="/api/sd_alarm_detail?url="+encodeURIComponent(item.url||"");
  fetchJSON(url,15000,function(e,r){
    if(!r||e||!r.data){
      $("alarmModalBody").innerHTML='<div style="text-align:center;color:rgba(232,234,246,.5);padding:20px 0;">加载失败，请点击"查看原文"查看</div>';
      return;
    }
    var content=r.data.content||"暂无详情内容";
    // 将换行转换为段落
    var paragraphs=content.split(/\n\s*\n/).filter(function(p){return p.trim().length>0;});
    var html="";
    for(var i=0;i<paragraphs.length;i++){
      html+="<p>"+paragraphs[i].replace(/\n/g,"<br>")+"</p>";
    }
    if(r.data.title&&r.data.title!==item.title){
      $("alarmModalTitle").textContent=r.data.title;
    }
    if(r.data.pub_time&&r.data.pub_time!=="--"){
      $("alarmModalTime").textContent=formatAlarmTime(r.data.pub_time);
    }
    $("alarmModalBody").innerHTML=html;
  });
}

function closeAlarmModal(){
  var modal=$("alarmModal");
  if(modal){modal.style.display="none";}
}

// 打开预警原文链接
var _currentAlarmUrl="";
function openAlarmOriginal(){
  if(!_currentAlarmUrl||_currentAlarmUrl==="#"||_currentAlarmUrl==="javascript:void(0)")return;
  window.open(_currentAlarmUrl,"_blank");
}

// ESC键关闭弹窗
document.addEventListener("keydown",function(e){
  if(e.key==="Escape"){closeAlarmModal();}
});
function loadTide(){fetchJSON(apiUrl("/api/tide"),20000,function(e,r){if(!r||e)return;if(r&&r.tomorrow_unavailable){showTideUnavailable();return;}if(r&&r.data)renderTide(r,r.updateTime);});
  // 预加载明日潮汐数据，用于计算次日高低潮
  if(selectedDayOffset===0){
    var tomorrowUrl="/api/tide?date="+(function(){var d=new Date();d.setDate(d.getDate()+1);return d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0");})();
    fetchJSON(tomorrowUrl,20000,function(e,r){
      tomorrowTideReady=true;
      if(!r||e||!r.data||!r.data.rows||!r.data.rows.length){tomorrowTideList=[];return;}
      var item=r.data.rows[0];
      tomorrowTideList=[
        {t:item.FIRSTHIGHTIME,l:item.FIRSTHIGHLEVEL,type:"高潮",cls:"high"},
        {t:item.SECONDHIGHTIME,l:item.SECONDHEIGHTLEVEL,type:"高潮",cls:"high"},
        {t:item.FIRSTLOWTIME,l:item.FIRSTLOWLEVEL,type:"低潮",cls:"low"},
        {t:item.SECONDLOWTIME,l:item.SECONDLOWLEVEL,type:"低潮",cls:"low"}
      ].filter(function(i){return i.t&&i.t!=="-";}).sort(function(a,b){return (timeToMin(a.t)!=null?timeToMin(a.t):9999)-(timeToMin(b.t)!=null?timeToMin(b.t):9999);});
    });
  }
}
function loadChart(){fetchJSON(apiUrl("/api/tideChart"),20000,function(e,r){if(!r||e){renderChart([],"潮汐曲线加载失败",null);return;}if(r&&r.tomorrow_unavailable){showChartUnavailable();return;}setText("chartTime","更新 "+(r.updateTime||"--"));renderChart(r.chart,r.msg,r.site);});}
function refreshAllData(){
  var btn=$("refreshBtn");
  if(btn){btn.disabled=true;btn.textContent="刷新中...";}
  loadTide();loadChart();loadWeather();loadWave();loadOffshoreWave();loadCmaAlarm();
  setTimeout(function(){
    reloadTyphoonFrame();
    if(btn)btn.textContent="刷新完成";
    setTimeout(function(){if(btn){btn.textContent="🔄 刷新";btn.disabled=false;}},1200);
  },2000);
}
function boot(){
  // 检测 User-Agent 判断是否移动端
  var ua=navigator.userAgent||"";
  var isMobile=/(Android|iPhone|iPad|iPod|Mobile|Opera Mini|IEMobile|WPDesktop|BlackBerry|BB10|SymbianOS|Series60|Windows Phone)/i.test(ua);
  // Android 平板（无 Mobile 标记）通过屏幕尺寸辅助判断
  if(!isMobile&&/Android/i.test(ua)){isMobile=Math.max(screen.width,screen.height)<1080;}
  if(isMobile){document.documentElement.className+=" mobile";}
  updateClock(); setInterval(updateClock,1000);
  updateDayButtons();
  loadTide(); loadChart(); loadWeather(); loadWave(); loadOffshoreWave(); loadCmaAlarm(); loadSdAlarm();
  setInterval(loadTide,60*60*1000); setInterval(loadChart,6*60*60*1000); setInterval(loadWeather,10*60*1000); setInterval(loadWave,60*60*1000); setInterval(loadOffshoreWave,60*60*1000); setInterval(loadCmaAlarm,5*60*1000); setInterval(loadSdAlarm,5*60*1000);
  setInterval(function(){if(lastTideList.length)calcTideStatus(lastTideList);},60*1000);
  // 每分钟更新海况卡片中的紫外线指数
  setInterval(function(){
    var uvEl=$("uvIndex");
    if(uvEl){
      var uv=estimateUVIndex();
      uvEl.textContent=uv.toFixed(1)+" · "+uvLevelText(uv);
      uvEl.className=uv>=6?"warn":(uv>=3?"":"safe");
    }
  },60*1000);
  setTimeout(function(){if(lastChartRaw)renderChart(lastChartRaw,"",lastChartSite);},1000);
}
boot();
</script>
</body>
</html>
"""


class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_server():
    global server
    try:
        server = ThreadingTCPServer(("", PORT), MyHandler)
        print(f"===== Web 服务启动 0.0.0.0:{PORT} =====")
        server.serve_forever()
    except Exception as e:
        print("【服务启动异常】", e)


def stop_server():
    global server
    if server is not None:
        server.shutdown()
        server.server_close()
        server = None


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
        "青岛潮汐与第六海水浴场天气海况",
        url=f"http://127.0.0.1:{PORT}",
        width=1280,
        height=760,
        resizable=True,
        maximized=True,
    )
    webview.start()
    stop_server()
    