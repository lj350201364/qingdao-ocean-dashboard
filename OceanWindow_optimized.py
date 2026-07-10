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
    "weather": None,
    "refresh": {
        "tide_table": "--",
        "tide_chart": "--",
        "wave": "--",
        "weather": "--",
    },
}


def _tz():
    """固定返回 Asia/Shanghai 时区，避免服务器时区不一致。"""
    return datetime.timezone(datetime.timedelta(hours=8))

def _now():
    return datetime.datetime.now(_tz())


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

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        routes = {
            "/api/tide": self.handle_tide,
            "/api/tideChart": self.handle_tide_chart,
            "/api/wave": self.handle_wave,
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
<script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
<style>
:root{
  --brand:#00e5ff;
  --brand-rgb:0,229,255;
  --secondary:#1a73e8;
  --accent:#7c4dff;
  --success:#00e676;
  --warning:#ffab00;
  --error:#ff5252;
  --bg:#0a0e1a;
  --bg2:#0f1528;
  --card:rgba(15,21,40,.86);
  --card2:rgba(20,28,55,.90);
  --border:rgba(0,229,255,.16);
  --border2:rgba(0,229,255,.34);
  --text:#e8eaf6;
  --muted:rgba(232,234,246,.66);
  --dim:rgba(232,234,246,.42);
  --shadow:0 8px 24px rgba(0,0,0,.55);
  --glow:0 0 22px rgba(0,229,255,.18);
  --radius:12px;
}
*{box-sizing:border-box;margin:0;padding:0;font-family:"Microsoft YaHei","PingFang SC",Arial,sans-serif;}
html,body{width:100%;height:100%;overflow:hidden;background:var(--bg);color:var(--text);}
body{
  background:
    radial-gradient(circle at 50% 0%,rgba(0,229,255,.10),transparent 34%),
    linear-gradient(rgba(0,229,255,.032) 1px,transparent 1px),
    linear-gradient(90deg,rgba(0,229,255,.032) 1px,transparent 1px),
    var(--bg);
  background-size:auto,40px 40px,40px 40px,auto;
}
.app{width:100vw;height:100vh;display:flex;flex-direction:column;overflow:hidden;}
.topbar{
  height:56px;flex:0 0 56px;display:flex;align-items:center;justify-content:space-between;
  padding:0 24px;background:linear-gradient(180deg,rgba(10,14,26,.97),rgba(10,14,26,.78));
  border-bottom:1px solid var(--border);box-shadow:0 2px 18px rgba(0,0,0,.42);z-index:10;
}
.brand{display:flex;align-items:center;gap:12px;min-width:0;overflow:hidden;}
.brand-code{font-size:13px;letter-spacing:.16em;color:var(--brand);text-shadow:0 0 12px rgba(var(--brand-rgb),.5);font-weight:700;}
.brand-title{font-size:14px;color:var(--muted);font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.live{display:flex;align-items:center;gap:8px;color:var(--success);font-size:12px;white-space:nowrap;min-width:0;}
.live-dot{width:8px;height:8px;border-radius:50%;background:var(--success);box-shadow:0 0 10px var(--success);animation:pulse 1.6s infinite;}
.top-actions{display:flex;align-items:center;gap:8px;white-space:nowrap;}
.sound-btn,.refresh-btn,.day-btn{display:inline-flex;align-items:center;gap:4px;border:1px solid var(--border2);background:rgba(15,21,40,.88);color:var(--brand);border-radius:999px;padding:5px 10px;font-size:12px;cursor:pointer;box-shadow:0 0 12px rgba(var(--brand-rgb),.12);white-space:nowrap;}
.refresh-btn{background:rgba(0,229,255,.12);}
.day-btn.active{background:rgba(0,229,255,.24);color:#fff;border-color:rgba(0,229,255,.55);}
.top-clock{text-align:right;}
.top-date{font-size:12px;color:rgba(232,234,246,.72);white-space:nowrap;}
.top-time{font-size:20px;letter-spacing:.16em;color:var(--brand);text-shadow:0 0 12px rgba(var(--brand-rgb),.45);font-weight:700;}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.45;transform:scale(.72)}}
@keyframes scanLine{0%{transform:translateY(-100%);opacity:0}18%,82%{opacity:.45}100%{transform:translateY(100%);opacity:0}}
@keyframes borderGlow{0%,100%{box-shadow:var(--shadow),0 0 16px rgba(0,229,255,.12)}50%{box-shadow:var(--shadow),0 0 28px rgba(0,229,255,.24)}}
@keyframes titleSpark{0%,100%{opacity:.75;filter:drop-shadow(0 0 4px rgba(0,229,255,.45))}50%{opacity:1;filter:drop-shadow(0 0 10px rgba(0,229,255,.85))}}
@keyframes floatCloud{0%,100%{transform:translateX(-3px)}50%{transform:translateX(4px)}}
@keyframes sunSpin{to{transform:rotate(360deg)}}
@keyframes rainFall{0%{transform:translateY(-7px);opacity:0}30%{opacity:.9}100%{transform:translateY(14px);opacity:0}}
@keyframes lightning{0%,78%,100%{opacity:.25;filter:drop-shadow(0 0 2px #ffeb3b)}82%,88%{opacity:1;filter:drop-shadow(0 0 12px #ffeb3b)}}
@keyframes fogMove{0%,100%{transform:translateX(-5px);opacity:.45}50%{transform:translateX(6px);opacity:.85}}
@keyframes snowFall{0%{transform:translateY(-5px) rotate(0deg);opacity:.2}50%{opacity:1}100%{transform:translateY(12px) rotate(180deg);opacity:.15}}
@keyframes dataPulse{0%,100%{text-shadow:0 0 8px rgba(0,229,255,.28);filter:brightness(1)}50%{text-shadow:0 0 16px rgba(0,229,255,.72);filter:brightness(1.16)}}
@keyframes shimmerX{0%{transform:translateX(-120%);opacity:0}18%,82%{opacity:.75}100%{transform:translateX(120%);opacity:0}}
@keyframes radarSweep{0%{transform:rotate(0deg)}100%{transform:rotate(360deg)}}
@keyframes tideRipple{0%{background-position:0 0,0 0}100%{background-position:70px 0,-70px 0}}
@keyframes waveSlide{0%{transform:translateX(-45%)}100%{transform:translateX(0)}}
@keyframes tableScan{0%,100%{background-color:transparent}50%{background-color:rgba(0,229,255,.10)}}
@keyframes chartAura{0%,100%{opacity:.22;transform:translateX(-8%)}50%{opacity:.48;transform:translateX(8%)}}
.content{height:calc(100vh - 56px);padding:16px;display:grid;grid-template-rows:minmax(0,2.55fr) minmax(250px,1.15fr);gap:16px;position:relative;overflow:hidden;}
.row-main{display:grid;grid-template-columns:1fr 2fr 1fr;gap:16px;min-height:0;}
.row-bottom{display:grid;grid-template-columns:1fr 2fr 1fr;gap:16px;min-height:0;}
.card{
  position:relative;min-width:0;min-height:0;overflow:hidden;padding:16px;border-radius:var(--radius);
  background:var(--card);border:1px solid var(--border);box-shadow:var(--shadow),var(--glow);
  backdrop-filter:blur(12px);animation:borderGlow 5s ease-in-out infinite;
}
.card::before{content:"";position:absolute;inset:0;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,229,255,.012) 2px,rgba(0,229,255,.012) 4px);pointer-events:none;z-index:1;}
.card::after{content:"";position:absolute;left:0;right:0;top:0;height:42%;background:linear-gradient(180deg,transparent,rgba(0,229,255,.08),transparent);pointer-events:none;z-index:1;animation:scanLine 7s linear infinite;}
.corner{position:absolute;width:18px;height:18px;border-color:var(--brand);opacity:.72;z-index:2;}
.tl{display:none}.tr{right:8px;top:8px;border-right:1px solid;border-top:1px solid}
.bl{left:8px;bottom:8px;border-left:1px solid;border-bottom:1px solid}.br{right:8px;bottom:8px;border-right:1px solid;border-bottom:1px solid}
.module-title{
  position:relative;z-index:2;display:flex;align-items:center;justify-content:space-between;gap:10px;
  color:var(--muted);font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
  padding-bottom:10px;margin-bottom:12px;border-bottom:1px solid rgba(255,255,255,.12);
  min-width:0;overflow:hidden;
}
.module-title::before{content:"";width:3px;height:14px;border-radius:2px;background:var(--brand);box-shadow:0 0 8px rgba(var(--brand-rgb),.65);margin-right:6px;animation:titleSpark 2.4s ease-in-out infinite;}
.module-title span:first-child{display:flex;align-items:center;color:var(--muted);min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.module-title small{font-size:12px;color:rgba(232,234,246,.70);letter-spacing:0;text-transform:none;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:45%;}
.module-title small.update-time{font-size:10px;color:rgba(232,234,246,.45);font-weight:400;max-width:35%;flex-shrink:0;}
.module-unavailable{position:relative;z-index:2;text-align:center;padding:32px 16px;color:rgba(232,234,246,.35);font-size:13px;font-weight:600;letter-spacing:.08em;}
.data-value{color:var(--brand);text-shadow:0 0 10px rgba(var(--brand-rgb),.35);font-weight:800;}
.metric-grid{position:relative;z-index:2;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;}
.metric-grid.three{grid-template-columns:repeat(3,minmax(0,1fr));grid-template-rows:repeat(2,minmax(0,1fr));height:calc(100% - 40px);}
.metric{
  position:relative;overflow:hidden;min-height:54px;border-radius:9px;padding:10px;background:rgba(6,10,20,.74);
  border:1px solid rgba(255,255,255,.08);display:flex;flex-direction:column;justify-content:center;
}
.metric::after,.temp-card::after,.mini-box::after,.extra-box::after{content:"";position:absolute;inset:0;background:linear-gradient(100deg,transparent,rgba(0,229,255,.12),transparent);transform:translateX(-120%);animation:shimmerX 6.5s ease-in-out infinite;pointer-events:none;}
.metric:nth-child(2)::after,.mini-box:nth-child(2)::after,.extra-box:nth-child(2)::after{animation-delay:.7s}
.metric:nth-child(3)::after,.extra-box:nth-child(3)::after{animation-delay:1.4s}
.metric:nth-child(4)::after,.extra-box:nth-child(4)::after{animation-delay:2.1s}
.metric:nth-child(5)::after,.extra-box:nth-child(5)::after{animation-delay:2.8s}
.metric:nth-child(6)::after,.extra-box:nth-child(6)::after{animation-delay:3.5s}
.metric .label{font-size:clamp(12px,.78vw,14px);color:rgba(232,234,246,.78);margin-bottom:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600;letter-spacing:.03em;}
.metric strong{position:relative;z-index:1;font-size:clamp(14px,1vw,18px);color:var(--brand);text-shadow:0 0 8px rgba(var(--brand-rgb),.32);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;animation:dataPulse 4.8s ease-in-out infinite;}
.wind-compass-wrap{position:relative;z-index:1;display:flex;align-items:center;gap:8px;min-width:0;}
.wind-compass{position:relative;flex:0 0 34px;width:34px;height:34px;border-radius:50%;border:1px solid rgba(0,229,255,.42);background:radial-gradient(circle,rgba(0,229,255,.16),rgba(6,10,20,.72));box-shadow:inset 0 0 10px rgba(0,229,255,.16),0 0 12px rgba(0,229,255,.14);}
.wind-compass::before{content:"N";position:absolute;top:1px;left:50%;transform:translateX(-50%);font-size:8px;color:rgba(232,234,246,.72);font-weight:800;}
.wind-needle{position:absolute;left:50%;top:50%;width:3px;height:22px;margin-left:-1.5px;margin-top:-11px;transform:rotate(0deg);transition:transform .9s cubic-bezier(.2,.8,.2,1);transform-origin:50% 50%;}
.wind-needle::before{content:"";position:absolute;left:50%;top:0;transform:translateX(-50%);width:0;height:0;border-left:5px solid transparent;border-right:5px solid transparent;border-bottom:13px solid var(--warning);filter:drop-shadow(0 0 7px rgba(255,171,0,.8));}
.wind-needle::after{content:"";position:absolute;left:50%;bottom:0;transform:translateX(-50%);width:0;height:0;border-left:4px solid transparent;border-right:4px solid transparent;border-top:10px solid rgba(0,229,255,.68);}
.wind-text{min-width:0;font-size:clamp(13px,.9vw,16px);color:var(--brand);font-weight:800;text-shadow:0 0 8px rgba(var(--brand-rgb),.32);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.temp-strip{position:relative;z-index:2;margin-bottom:12px;display:grid;grid-template-columns:1fr 1fr;grid-template-rows:auto auto;gap:8px;min-width:0;}
.temp-card{position:relative;overflow:hidden;min-width:0;border-radius:9px;padding:10px;background:rgba(6,10,20,.74);border:1px solid rgba(255,255,255,.08);}
.temp-card .label{font-size:clamp(11px,.72vw,13px);color:rgba(232,234,246,.78);font-weight:600;margin-bottom:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.temp-card .value{position:relative;z-index:1;font-size:clamp(14px,.95vw,18px);line-height:1.05;color:var(--brand);font-weight:800;text-shadow:0 0 8px rgba(var(--brand-rgb),.34);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;animation:dataPulse 4.2s ease-in-out infinite;}
.temp-card.primary{grid-column:1 / -1;display:flex;align-items:center;justify-content:space-between;gap:10px;padding:9px 12px;}
.temp-card.primary .label{margin-bottom:0;}
.temp-card.primary .value{font-size:clamp(24px,1.9vw,34px);flex:0 0 auto;}
.weather-hero{position:relative;z-index:2;margin-bottom:12px;min-width:0;overflow:hidden;display:flex;align-items:center;gap:12px;}
.weather-icon{position:relative;flex:0 0 46px;width:46px;height:46px;border-radius:50%;display:grid;place-items:center;background:rgba(6,10,20,.70);border:1px solid rgba(255,255,255,.08);box-shadow:inset 0 0 16px rgba(0,229,255,.10),0 0 18px rgba(0,229,255,.08);overflow:hidden;}
.weather-icon::before,.weather-icon::after{content:"";position:absolute;display:block;}
.weather-text-wrap{min-width:0;overflow:hidden;}
.weather-icon.sunny::before{width:20px;height:20px;border-radius:50%;background:#ffeb3b;box-shadow:0 0 18px rgba(255,235,59,.85);}
.weather-icon.sunny::after{width:34px;height:34px;border-radius:50%;border:2px dashed rgba(255,235,59,.72);animation:sunSpin 8s linear infinite;}
.weather-icon.cloudy::before,.weather-icon.rainy::before,.weather-icon.storm::before,.weather-icon.snow::before{width:30px;height:16px;left:8px;top:17px;border-radius:16px;background:linear-gradient(180deg,#d8f6ff,#7fb8d5);box-shadow:9px -8px 0 -1px #b9e6f5,-9px -5px 0 -3px #e7fbff;animation:floatCloud 3s ease-in-out infinite;}
.weather-icon.cloudy::after{width:38px;height:10px;left:4px;bottom:9px;border-radius:999px;background:rgba(0,229,255,.18);filter:blur(4px);}
.weather-icon.rainy::after{width:3px;height:13px;left:15px;top:30px;border-radius:3px;background:#00e5ff;box-shadow:9px -4px 0 #00e5ff,18px 1px 0 #00e5ff;animation:rainFall 1.1s linear infinite;}
.weather-icon.storm::after{width:14px;height:20px;left:19px;top:25px;background:#ffeb3b;clip-path:polygon(42% 0,100% 0,61% 44%,92% 44%,30% 100%,45% 55%,10% 55%);animation:lightning 1.8s linear infinite;}
.weather-icon.fog::before,.weather-icon.fog::after{left:8px;right:8px;height:3px;border-radius:999px;background:rgba(232,234,246,.75);box-shadow:0 9px 0 rgba(232,234,246,.52),0 18px 0 rgba(232,234,246,.38);animation:fogMove 2.8s ease-in-out infinite;}
.weather-icon.fog::before{top:12px}.weather-icon.fog::after{top:18px;animation-delay:.8s;opacity:.6;}
.weather-icon.snow::after{content:"✦";left:12px;top:27px;color:#e8f8ff;font-size:12px;text-shadow:11px -3px 0 #e8f8ff,20px 4px 0 #e8f8ff;animation:snowFall 1.9s linear infinite;}
.status-big{position:relative;z-index:2;display:flex;align-items:center;justify-content:space-between;margin-bottom:18px;}
.status-big>div:first-child{min-width:0;padding-right:10px;}
.status-badge{display:inline-flex;align-items:center;padding:4px 10px;border-radius:999px;background:rgba(0,229,255,.13);border:1px solid rgba(0,229,255,.28);color:var(--brand);font-size:13px;font-weight:700;animation:dataPulse 3.6s ease-in-out infinite;}
.status-text{font-size:clamp(12px,.82vw,15px);color:var(--muted);margin-top:8px;line-height:1.45;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
.level-number{font-size:clamp(30px,2.2vw,42px);line-height:1.05;white-space:nowrap;flex:0 0 auto;animation:dataPulse 3.8s ease-in-out infinite;}
.level-number small{font-size:16px;color:var(--muted);margin-left:4px;}
.mini-tide{position:relative;z-index:2;display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px;}
.mini-box{position:relative;overflow:hidden;background:rgba(6,10,20,.72);border:1px solid rgba(255,255,255,.08);border-radius:9px;padding:10px;}
.mini-box .label{font-size:clamp(12px,.78vw,14px);color:rgba(232,234,246,.78);margin-bottom:5px;font-weight:600;letter-spacing:.03em;}
.mini-box .value{font-size:clamp(16px,1.1vw,20px);color:var(--brand);font-weight:800;text-shadow:0 0 8px rgba(var(--brand-rgb),.32);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.tide-extra{position:relative;z-index:2;margin-top:8px;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;}
.extra-box{position:relative;overflow:hidden;min-width:0;border-radius:9px;padding:9px;background:rgba(6,10,20,.72);border:1px solid rgba(255,255,255,.08);}
.extra-box .label{font-size:clamp(11px,.72vw,13px);color:rgba(232,234,246,.78);font-weight:600;margin-bottom:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.extra-box .value{position:relative;z-index:1;font-size:clamp(14px,.9vw,17px);color:var(--brand);font-weight:800;text-shadow:0 0 8px rgba(var(--brand-rgb),.32);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;animation:dataPulse 5.2s ease-in-out infinite;}
.map-card{padding:12px;}
.map-shell{position:relative;z-index:2;height:calc(100% - 42px);border-radius:8px;overflow:hidden;border:1px solid rgba(0,229,255,.14);background:#060a14;}
.map-shell::before{content:"";position:absolute;z-index:3;left:50%;top:50%;width:120%;height:120%;transform-origin:0 0;pointer-events:none;background:conic-gradient(from 0deg,rgba(0,229,255,.18),rgba(0,229,255,.04) 22deg,transparent 54deg,transparent 360deg);mix-blend-mode:screen;animation:radarSweep 10s linear infinite;}
.map-shell::after{content:"";position:absolute;inset:0;z-index:3;pointer-events:none;background-image:linear-gradient(rgba(0,229,255,.026) 1px,transparent 1px),linear-gradient(90deg,rgba(0,229,255,.026) 1px,transparent 1px);background-size:50px 50px,50px 50px;}
.typhoon-frame{position:absolute;inset:0;width:100%;height:100%;border:0;filter:saturate(1) brightness(1) contrast(1);}
.map-label{position:absolute;z-index:4;pointer-events:none;font-size:9px;color:rgba(232,234,246,.42);font-family:Consolas,monospace;text-shadow:0 0 6px rgba(0,0,0,.8);}
.map-label.n40{top:10px;left:12px}.map-label.n30{top:50%;left:12px;transform:translateY(-50%)}.map-label.n20{bottom:10px;left:12px}
.map-label.e125{bottom:10px;left:25%}.map-label.e130{bottom:10px;left:50%;transform:translateX(-50%)}.map-label.e135{bottom:10px;right:78px}
.map-actions{position:absolute;right:12px;bottom:12px;z-index:5;display:flex;gap:8px;}
.btn{display:inline-flex;align-items:center;justify-content:center;border:1px solid var(--border2);background:rgba(15,21,40,.88);color:var(--brand);border-radius:999px;padding:7px 12px;font-size:12px;text-decoration:none;cursor:pointer;box-shadow:0 0 12px rgba(var(--brand-rgb),.12);}
.mobile-map-open{display:none;position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);z-index:6;min-width:220px;text-align:center;border:1px solid var(--border2);background:rgba(10,14,26,.90);color:var(--brand);border-radius:14px;padding:12px 16px;font-size:15px;font-weight:800;text-decoration:none;box-shadow:0 0 22px rgba(var(--brand-rgb),.28);}
.chart-card{display:flex;flex-direction:column;}
#tideChart{position:relative;z-index:2;flex:1;min-height:0;width:100%;border-radius:8px;background:radial-gradient(circle at 50% 50%,rgba(0,229,255,.08),transparent 42%),rgba(6,10,20,.58);border:1px solid rgba(0,229,255,.14);overflow:hidden;}
#tideChart::before{content:"";position:absolute;inset:0;pointer-events:none;background:linear-gradient(90deg,transparent,rgba(0,229,255,.10),transparent);animation:chartAura 6s ease-in-out infinite;z-index:1;}
.sea-card{display:flex;flex-direction:column;}
.sea-card::before{background:repeating-radial-gradient(ellipse at 50% 110%,rgba(0,229,255,.07) 0 2px,transparent 3px 12px);animation:tideRipple 8s linear infinite;}
.tide-table-wrap{position:relative;z-index:2;height:calc(100% - 40px);overflow:hidden;}
table{width:100%;height:100%;border-collapse:collapse;background:rgba(6,10,20,.72);}
tr:nth-child(2){animation:tableScan 4.2s ease-in-out infinite}
tr:nth-child(3){animation:tableScan 4.2s ease-in-out infinite .7s}
tr:nth-child(4){animation:tableScan 4.2s ease-in-out infinite 1.4s}
tr:nth-child(5){animation:tableScan 4.2s ease-in-out infinite 2.1s}
th,td{border:1px solid rgba(0,229,255,.15);text-align:center;padding:6px 4px;font-size:clamp(11px,.72vw,13px);color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
th{background:rgba(0,229,255,.12);color:var(--brand);font-weight:800;}
.high{background:rgba(255,171,0,.12);color:var(--warning);font-weight:800;text-shadow:0 0 8px rgba(255,171,0,.25);}
.low{background:rgba(0,230,118,.10);color:var(--success);font-weight:800;text-shadow:0 0 8px rgba(0,230,118,.25);}
.high td{color:var(--warning);}
.low td{color:var(--success);}
.safe{color:var(--success)!important}.bad{color:var(--error)!important}.warning{color:var(--warning)!important}
@media (max-width:1079px){
  html,body{overflow:auto}
  .app{height:auto;min-height:100vh;overflow:visible}
  .topbar{height:auto;min-height:48px;flex:0 0 auto;flex-wrap:wrap;gap:4px 8px;padding:calc(8px + env(safe-area-inset-top,0px)) 10px 6px;align-items:center;position:relative;z-index:10}
  .brand{width:100%;justify-content:center;gap:6px}
  .brand-code{font-size:11px;letter-spacing:.12em}
  .brand-title{font-size:11px;max-width:68vw}
  .live{order:1;width:100%;justify-content:center;font-size:11px;gap:6px}
  .top-actions{order:2;width:100%;justify-content:center;gap:6px;flex-wrap:wrap}
  .sound-btn,.refresh-btn,.day-btn{font-size:11px;padding:4px 8px;min-width:0;justify-content:center}
  .sound-btn span{display:none}
  .refresh-btn{min-width:auto}
  .top-clock{order:3;width:100%;text-align:center}
  .top-date{font-size:11px;white-space:normal;line-height:1.2}
  .top-time{font-size:22px;letter-spacing:.10em;line-height:1.05;margin-top:1px}
  .content{padding:10px 10px 16px;height:auto;display:block;overflow:visible;position:static}
  .row-main,.row-bottom{display:block}
  .card{margin:10px 0;min-height:220px}
  .map-card{min-height:520px}.chart-card{min-height:320px}
  .map-shell{height:68vh;min-height:420px}
  .mobile-map-open{display:block}
  .map-actions{left:12px;right:12px;justify-content:center}
}
</style>
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

  <main class="content">
    <section class="row-main">
      <div class="card tide-card" id="tideCard">
        <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
        <div class="module-title"><span>潮汐状态</span><small id="tideUpdate">--</small></div>
        <div class="status-big" id="tideCardContent">
          <div>
            <span id="tideBadge" class="status-badge">等待加载</span>
            <div id="tideStatusText" class="status-text">正在获取青岛潮汐数据</div>
          </div>
          <div class="level-number data-value"><span id="currentLevel">--</span><small>cm</small></div>
        </div>
        <div class="mini-tide">
          <div class="mini-box"><div class="label">下次高潮</div><div id="nextHigh" class="value">--</div></div>
          <div class="mini-box"><div class="label">下次低潮</div><div id="nextLow" class="value">--</div></div>
        </div>
        <div class="tide-extra">
          <div class="extra-box"><div class="label">当前趋势</div><div id="tideTrend" class="value">--</div></div>
          <div class="extra-box"><div class="label">当前潮段</div><div id="tidePhase" class="value">--</div></div>
          <div class="extra-box"><div class="label">潮段进度</div><div id="tideProgress" class="value">--</div></div>
          <div class="extra-box"><div class="label">距高潮</div><div id="highDelta" class="value">--</div></div>
          <div class="extra-box"><div class="label">距低潮</div><div id="lowDelta" class="value">--</div></div>
          <div class="extra-box"><div class="label">今日潮差</div><div id="tideRange" class="value">--</div></div>
        </div>
      </div>

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

      <div class="card weather-card" id="weatherCard">
        <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
        <div class="module-title"><span>实时天气与风况</span><small id="weatherTime">--</small></div>
        <div class="temp-strip">
          <div class="temp-card primary"><div id="airTempLabel" class="label">当前气温</div><div id="airTemp" class="value">--</div></div>
          <div class="temp-card"><div id="tempRangeLabel" class="label">今日温度</div><div id="tempRange" class="value">--</div></div>
          <div class="temp-card"><div id="apparentTempLabel" class="label">体感温度</div><div id="apparentTemp" class="value">--</div></div>
        </div>
        <div class="weather-hero">
          <div id="weatherIcon" class="weather-icon cloudy"></div>
          <div class="weather-text-wrap">
            <div id="weatherText" style="font-size:20px;color:var(--text);font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">--</div>
            <div style="font-size:12px;color:rgba(232,234,246,.66);font-weight:600;">实时气象动画</div>
          </div>
        </div>
        <div class="metric-grid">
          <div class="metric"><div class="label">湿度</div><strong id="humidity">--</strong></div>
          <div class="metric"><div class="label">风速</div><strong id="windSpeed">--</strong></div>
          <div class="metric"><div class="label">风向</div><div class="wind-compass-wrap"><div class="wind-compass"><div id="windNeedle" class="wind-needle"></div></div><div id="windDirection" class="wind-text">--</div></div></div>
          <div class="metric"><div class="label">阵风</div><strong id="windGusts">--</strong></div>
          <div class="metric"><div class="label">气象时间</div><strong id="weatherSourceTime">--</strong></div>
          <div class="metric"><div class="label">风级</div><strong id="windLevel">--</strong></div>
        </div>
      </div>
    </section>

    <section class="row-bottom">
      <div class="card sea-card" id="seaCard">
        <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
        <div class="module-title"><span>海况数据</span><small id="waveTime">--</small></div>
        <div class="metric-grid three">
          <div class="metric"><div class="label">浪高</div><strong id="waveHeight">--</strong></div>
          <div class="metric"><div class="label">浪况等级</div><strong id="waveLevel">--</strong></div>
          <div class="metric"><div class="label">水温</div><strong id="waterTemp">--</strong></div>
          <div class="metric"><div class="label">水温体感</div><strong id="waterComfort">--</strong></div>
          <div class="metric"><div class="label">下海提示</div><strong id="swimTip">--</strong></div>
          <div class="metric"><div class="label">风险提示</div><strong id="seaRisk">--</strong></div>
        </div>
      </div>

      <div class="card chart-card" id="chartCard">
        <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
        <div class="module-title"><span id="chartTitle">青岛今日潮汐曲线</span><small id="chartSource">全球潮汐平台</small><small id="chartTime" class="update-time">--</small></div>
        <div id="tideChart">等待曲线数据</div>
      </div>

      <div class="card table-card" id="tableCard">
        <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
        <div class="module-title"><span>高低潮位表</span><small id="tableDateLabel">青岛站</small><small id="tableTime" class="update-time">--</small></div>
        <div id="mainBox" class="tide-table-wrap">加载青岛高低潮表...</div>
      </div>
    </section>
  </main>
</div>

<script>
let tideRawData=null, tideChart=null, lastChartRaw=null, lastChartSite=null, lastChartPoints=[], lastTideList=[], resizeTimer=null, lastTideRising=null, soundEnabled=false, audioCtx=null, selectedDayOffset=0;
const $=id=>document.getElementById(id);
function setText(id,text){const el=$(id); if(el) el.textContent=(text===null||text===undefined||text==="")?"--":text;}
function lunarText(d){
  try{
    const text=new Intl.DateTimeFormat("zh-CN-u-ca-chinese",{month:"long",day:"numeric"}).format(d);
    return `农历 ${text}`;
  }catch(e){
    return "农历 --";
  }
}
function nowParts(){
  const d=new Date();
  const week=["星期日","星期一","星期二","星期三","星期四","星期五","星期六"][d.getDay()];
  const lunar=lunarText(d);
  return {
    time:`${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")}:${String(d.getSeconds()).padStart(2,"0")}`,
    date:`${d.getFullYear()}年${String(d.getMonth()+1).padStart(2,"0")}月${String(d.getDate()).padStart(2,"0")}日 ${week} · ${lunar}`
  };
}
function updateClock(){const p=nowParts();setText("nowTime",p.time);setText("dateText",p.date);}
function selectedDate(){
  const d=new Date();
  d.setDate(d.getDate()+selectedDayOffset);
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
}
function selectedDayLabel(){
  return selectedDayOffset===0?"今日":"明日";
}
function apiUrl(path){
  return `${path}?date=${encodeURIComponent(selectedDate())}`;
}
function updateDayButtons(){
  const today=$("todayBtn"), tomorrow=$("tomorrowBtn");
  if(today)today.classList.toggle("active",selectedDayOffset===0);
  if(tomorrow)tomorrow.classList.toggle("active",selectedDayOffset===1);
  setText("chartTitle",`青岛${selectedDayLabel()}潮汐曲线`);
  setText("tableDateLabel",`青岛站 · ${selectedDayLabel()}`);
  setText("airTempLabel",selectedDayOffset===0?"当前气温":"明日最高");
  setText("tempRangeLabel",`${selectedDayLabel()}温度`);
  setText("apparentTempLabel",selectedDayOffset===0?"体感温度":"明日最低");
}
function switchForecastDay(offset){
  selectedDayOffset=offset;
  lastTideRising=null;
  // 切换时清除"暂无明日数据"提示
  document.querySelectorAll(".module-unavailable").forEach(el=>el.remove());
  updateDayButtons();
  refreshAllData();
}
function fetchJSON(url,timeout=20000){
  const sep=url.includes("?")?"&":"?";
  const c=new AbortController(); const t=setTimeout(()=>c.abort(),timeout);
  return fetch(url+sep+"_t="+Date.now(),{signal:c.signal,cache:"no-store"}).then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json();}).finally(()=>clearTimeout(t));
}
function reloadTyphoonFrame(){
  const f=$("typhoonFrame"); if(f) f.src="https://www.bhyb.org.cn/typhoon/?t="+Date.now();
}
function formatHHMM(s){
  if(!s||s==="-")return "--";
  if(String(s).includes(":")){const [h,m]=String(s).split(":");return `${String(parseInt(h,10)).padStart(2,"0")}:${String(parseInt(m,10)).padStart(2,"0")}`;}
  const raw=String(s).replace(":","").padStart(4,"0"); return `${raw.slice(0,2)}:${raw.slice(2,4)}`;
}
function timeToMin(s){
  if(!s||s==="-")return null; const t=formatHHMM(s); const [h,m]=t.split(":").map(Number);
  if(Number.isNaN(h)||Number.isNaN(m))return null; return h*60+m;
}
function parseTidePointTime(s){
  if(s===null||s===undefined||s==="")return null; const text=String(s);
  if(text.includes(":")){const [hh,mm]=text.split(":");const h=parseInt(hh,10),m=parseInt(mm,10);if(Number.isNaN(h)||Number.isNaN(m))return null;return {label:`${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}`,minute:h*60+m};}
  const h=parseInt(text,10); if(Number.isNaN(h))return null; return {label:`${String(h).padStart(2,"0")}:00`,minute:h*60};
}
function todayKey(){return selectedDate();}
function normalizeDateKey(s){
  if(!s)return ""; const datePart=String(s).split(" ")[0].replace(/-/g,"/"); const p=datePart.split("/");
  if(p.length<3)return ""; return `${p[0]}-${String(p[1]).padStart(2,"0")}-${String(p[2]).padStart(2,"0")}`;
}
function windLevelText(speedText){
  const speed=parseFloat(String(speedText||"").replace(/[^\d.]/g,""));
  if(Number.isNaN(speed))return "--";
  const levels=[
    [1,"0级 静风"],[5,"1级 软风"],[11,"2级 轻风"],[19,"3级 微风"],
    [28,"4级 和风"],[38,"5级 清风"],[49,"6级 强风"],[61,"7级 疾风"],
    [74,"8级 大风"],[88,"9级 烈风"],[102,"10级 狂风"],[117,"11级 暴风"],[Infinity,"12级 飓风"]
  ];
  return (levels.find(([limit])=>speed<=limit)||levels[levels.length-1])[1];
}
function parseWindDegree(degreeText){
  const degree=parseFloat(String(degreeText||"").replace(/[^\d.]/g,""));
  if(Number.isNaN(degree))return null;
  return ((degree%360)+360)%360;
}
function waveLevelText(waveText){
  const wave=parseFloat(String(waveText||"").replace(/[^\d.]/g,""));
  if(Number.isNaN(wave))return "--";
  if(wave<0.3)return "平静";
  if(wave<0.8)return "轻浪";
  if(wave<1.5)return "中浪";
  if(wave<2.5)return "大浪";
  return "风浪大";
}
function waterComfortText(tempText){
  const temp=parseFloat(String(tempText||"").replace(/[^\d.]/g,""));
  if(Number.isNaN(temp))return "--";
  if(temp<18)return "偏冷";
  if(temp<22)return "较凉";
  if(temp<=28)return "舒适";
  if(temp<=31)return "偏暖";
  return "较热";
}
function seaRiskText(waveText, swimTip){
  const wave=parseFloat(String(waveText||"").replace(/[^\d.]/g,""));
  const tip=String(swimTip||"");
  if(tip.includes("不")||tip.includes("禁")||tip.includes("危险"))return "谨慎下海";
  if(!Number.isNaN(wave)&&wave>=1.5)return "风浪偏大";
  if(!Number.isNaN(wave)&&wave>=0.8)return "注意浪涌";
  if(tip.includes("适宜"))return "风险较低";
  return tip&&tip!=="--"?tip:"关注海况";
}
function weatherIconClass(weatherText){
  const text=String(weatherText||"");
  if(text.includes("雷"))return "storm";
  if(text.includes("雨"))return "rainy";
  if(text.includes("雪"))return "snow";
  if(text.includes("雾")||text.includes("霾"))return "fog";
  if(text.includes("晴"))return "sunny";
  return "cloudy";
}
function renderWeather(obj,updateTime){
  setText("weatherTime",`更新 ${updateTime||"--"}`); setText("weatherText",obj&&obj.weather);
  const icon=$("weatherIcon"); if(icon) icon.className=`weather-icon ${weatherIconClass(obj&&obj.weather)}`;
  setText("airTemp",obj&&obj.temperature); setText("apparentTemp",obj&&obj.apparent_temperature);
  setText("tempRange",obj&&obj.temperature_range);
  setText("humidity",obj&&obj.humidity); setText("windSpeed",obj&&obj.wind_speed);
  setText("windDirection",obj?`${obj.wind_direction} ${obj.wind_direction_degree}`:"--");
  const windDegree=parseWindDegree(obj&&obj.wind_direction_degree);
  const needle=$("windNeedle"); if(needle&&windDegree!==null) needle.style.transform=`rotate(${windDegree}deg)`;
  setText("windGusts",obj&&obj.wind_gusts); setText("weatherSourceTime",obj&&obj.source_time?obj.source_time.replace("T"," "):"--");
  setText("windLevel",obj?windLevelText(obj.wind_speed):"--");
}
function renderWave(obj,updateTime){
  setText("waveTime",`更新 ${updateTime||"--"}`); setText("waveHeight",obj&&obj.wave_height); setText("waterTemp",obj&&obj.water_temp);
  setText("waveLevel",obj?waveLevelText(obj.wave_height):"--");
  setText("waterComfort",obj?waterComfortText(obj.water_temp):"--");
  setText("swimTip",obj&&obj.swim_tip); const el=$("swimTip"); if(el) el.className=obj&&obj.swim_tip==="适宜"?"safe":(obj&&obj.swim_tip?"bad":"");
  setText("seaRisk",obj?seaRiskText(obj.wave_height,obj.swim_tip):"--");
}
function renderTide(res,upTime){
  tideRawData=res; setText("tideUpdate",`更新 ${upTime||"--"}`); setText("globalUpdate",`数据更新 ${upTime||"--"}`); setText("tableTime","更新 "+(upTime||"--"));
  if(!res||!res.data||!Array.isArray(res.data.rows)){ $("mainBox").innerHTML="潮汐数据为空"; return; }
  const item=res.data.rows[0]; if(!item){$("mainBox").innerHTML="无青岛高低潮预报"; return;}
  const list=[
    {t:item.FIRSTHIGHTIME,l:item.FIRSTHIGHLEVEL,type:"高潮",cls:"high"},
    {t:item.SECONDHIGHTIME,l:item.SECONDHEIGHTLEVEL,type:"高潮",cls:"high"},
    {t:item.FIRSTLOWTIME,l:item.FIRSTLOWLEVEL,type:"低潮",cls:"low"},
    {t:item.SECONDLOWTIME,l:item.SECONDLOWLEVEL,type:"低潮",cls:"low"}
  ].filter(i=>i.t&&i.t!=="-").sort((a,b)=>(timeToMin(a.t)??9999)-(timeToMin(b.t)??9999));
  let html=`<table><tr><th>潮型</th><th>潮时</th><th>潮位</th></tr>`;
  list.forEach(r=>html+=`<tr class="${r.cls}"><td>${r.type}</td><td>${formatHHMM(r.t)}</td><td>${r.l==="-"?"无":r.l} cm</td></tr>`);
  html+=`</table>`; $("mainBox").innerHTML=html;
  lastTideList=list;
  calcTideStatus(list);
}
function interpolateCurrentLevel(points, nowMin){
  const curve=(points||[]).filter(p=>p.minute!==null&&!Number.isNaN(p.value)).sort((a,b)=>a.minute-b.minute);
  if(!curve.length)return null;
  if(nowMin<=curve[0].minute)return Math.round(curve[0].value);
  if(nowMin>=curve[curve.length-1].minute)return Math.round(curve[curve.length-1].value);
  for(let i=1;i<curve.length;i++){
    const prev=curve[i-1], next=curve[i];
    if(nowMin>=prev.minute&&nowMin<=next.minute){
      const ratio=(nowMin-prev.minute)/Math.max(1,next.minute-prev.minute);
      return Math.round(prev.value+(next.value-prev.value)*ratio);
    }
  }
  return null;
}
function calcTideStatus(list){
  if(document.querySelector("#tideCard .module-unavailable"))return;
  const now=new Date(), nowMin=selectedDayOffset===0 ? now.getHours()*60+now.getMinutes() : 0;
  if(selectedDayOffset===0){
    const statusTime=`${String(now.getMonth()+1).padStart(2,"0")}-${String(now.getDate()).padStart(2,"0")} ${String(now.getHours()).padStart(2,"0")}:${String(now.getMinutes()).padStart(2,"0")}`;
    setText("tideUpdate",`状态 ${statusTime}`);
  }
  const pts=list.map(x=>({min:timeToMin(x.t),height:Number(x.l),type:x.type,time:formatHHMM(x.t)})).filter(p=>p.min!==null&&!Number.isNaN(p.height)).sort((a,b)=>a.min-b.min);
  if(pts.length<2){setText("tideBadge","数据不足");setText("tideStatusText","潮位数据不足");return;}
  const curve=(lastChartPoints||[]).filter(p=>p.minute!==null&&!Number.isNaN(p.value)).sort((a,b)=>a.minute-b.minute);
  let prev=pts[0],next=pts[pts.length-1];
  const futureExtrema=pts.filter(p=>p.min>nowMin);
  if(futureExtrema.length){
    next=futureExtrema[0];
    prev=pts.filter(p=>p.min<=nowMin).pop() || pts[0];
  }else{
    prev=pts[pts.length-1];
    next=null;
  }
  let rising=false;
  if(curve.length>=2){
    let cPrev=curve[0], cNext=curve[1];
    if(nowMin>=curve[curve.length-1].minute){
      cPrev=curve[curve.length-2];
      cNext=curve[curve.length-1];
    }else{
      for(let i=1;i<curve.length;i++){
        if(nowMin<=curve[i].minute){cPrev=curve[i-1];cNext=curve[i];break;}
      }
    }
    rising=cNext.value-cPrev.value>0;
  }else if(next){
    rising=next.height-prev.height>0;
  }
  setText("tideBadge",selectedDayOffset===0 ? (rising?"涨潮中":"退潮中") : "明日预报");
  setText("tideTrend",rising?"水位上升":"水位下降");
  if(selectedDayOffset===0&&lastTideRising!==null&&rising&&!lastTideRising&&soundEnabled){playRisingSound();}
  lastTideRising=rising;
  if(next){
    setText("tideStatusText",`${selectedDayLabel()}下一次${next.type} ${next.time}，潮位 ${next.height} cm`);
    setText("nextTideDelta",formatDuration(next.min-nowMin));
    if(prev&&prev.min<nowMin&&prev.min<next.min){
      setText("tidePhase",`${prev.type}→${next.type}`);
      const phaseTotal=Math.max(1,next.min-prev.min);
      const phaseProgress=Math.max(0,Math.min(100,Math.round((nowMin-prev.min)/phaseTotal*100)));
      setText("tideProgress",`${phaseProgress}%`);
    }else{
      setText("tidePhase",`等待${next.type}`);
      setText("tideProgress","0%");
    }
  }else{
    setText("tideStatusText",`${selectedDayLabel()}后续无高低潮预报`);
    setText("nextTideDelta",`${selectedDayLabel()}无`);
    setText("tidePhase",`${selectedDayLabel()}末段`);
    setText("tideProgress","--");
  }
  const currentLevel=interpolateCurrentLevel(curve,nowMin);
  setText("currentLevel",currentLevel!==null?currentLevel:"--");
  const highs=pts.filter(p=>p.type==="高潮"), lows=pts.filter(p=>p.type==="低潮");
  const nextHigh=highs.find(p=>p.min>nowMin), nextLow=lows.find(p=>p.min>nowMin);
  setText("nextHigh",(nextHigh||{}).time||"--");
  setText("nextLow",(nextLow||{}).time||"--");
  setText("highDelta",nextHigh?formatDuration(nextHigh.min-nowMin):`${selectedDayLabel()}无`);
  setText("lowDelta",nextLow?formatDuration(nextLow.min-nowMin):`${selectedDayLabel()}无`);
  renderTideSummary(pts);
}
function initAudio(){
  if(!audioCtx) audioCtx=new (window.AudioContext||window.webkitAudioContext)();
}
function playRisingSound(){
  try{
    initAudio();
    if(audioCtx.state==="suspended") audioCtx.resume();
    const t=audioCtx.currentTime;
    const osc1=audioCtx.createOscillator(); const g1=audioCtx.createGain();
    osc1.type="sine"; osc1.frequency.setValueAtTime(523,t); osc1.frequency.exponentialRampToValueAtTime(784,t+0.15);
    g1.gain.setValueAtTime(0.08,t); g1.gain.exponentialRampToValueAtTime(0.001,t+0.5);
    osc1.connect(g1); g1.connect(audioCtx.destination); osc1.start(t); osc1.stop(t+0.5);
    const osc2=audioCtx.createOscillator(); const g2=audioCtx.createGain();
    osc2.type="sine"; osc2.frequency.setValueAtTime(659,t+0.18); osc2.frequency.exponentialRampToValueAtTime(1047,t+0.35);
    g2.gain.setValueAtTime(0.06,t+0.18); g2.gain.exponentialRampToValueAtTime(0.001,t+0.65);
    osc2.connect(g2); g2.connect(audioCtx.destination); osc2.start(t+0.18); osc2.stop(t+0.65);
  }catch(e){}
}
function toggleSound(){
  soundEnabled=!soundEnabled; initAudio();
  const btn=$("soundBtn"); if(btn){
    btn.innerHTML=soundEnabled?'🔊 <span>声音开</span>':'🔇 <span>声音关</span>';
  }
}
function formatDuration(minutes){
  if(minutes===null||minutes===undefined||minutes<0)return "--";
  const h=Math.floor(minutes/60), m=minutes%60;
  if(h<=0)return `${m}分钟`;
  return `${h}小时${m>0?m+"分":""}`;
}
function renderTideSummary(pts){
  if(!pts||!pts.length){setText("tideRange","--");return;}
  const heights=pts.map(p=>p.height).filter(v=>!Number.isNaN(v));
  if(!heights.length){setText("tideRange","--");return;}
  const max=Math.max(...heights), min=Math.min(...heights);
  setText("tideRange",`${Math.round(max-min)} cm`);
}
function parseChartPoints(rawArr){
  const arr=Array.isArray(rawArr)?rawArr:[]; const all=arr.map(it=>{
    const parsed=parseTidePointTime(it&&it.TIDETIME), val=Number(it&&it.TIDEHEIGHT||0); if(!parsed)return null;
    return {dateKey:normalizeDateKey(it&&it.TIDEDATE||""),label:parsed.label,minute:parsed.minute,value:Number.isNaN(val)?0:val,pointType:it&&it.POINT_TYPE||"hour",extremaType:it&&it.EXTREMA_TYPE||""};
  }).filter(Boolean);
  const day=todayKey(); let points=all.filter(p=>p.dateKey===day); if(points.length===0&&all.length>0)points=all.filter(p=>p.dateKey===all[0].dateKey);
  return points.sort((a,b)=>a.minute-b.minute);
}
function adaptiveChartFont(base,min,max){const scale=Math.min(window.innerWidth/1280,window.innerHeight/760);return Math.max(min,Math.min(max,Math.round(base*scale)));}
function initChart(){
  if(typeof echarts==="undefined")return false; if(!tideChart){tideChart=echarts.init($("tideChart")); window.addEventListener("resize",()=>{tideChart.resize();clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(lastChartRaw)renderChart(lastChartRaw,"",lastChartSite)},160);});}
  return true;
}
function renderChart(rawArr,msg,site){
  lastChartRaw=Array.isArray(rawArr)?rawArr:null; lastChartSite=site||null; const points=parseChartPoints(rawArr);
  lastChartPoints=points;
  if(lastTideList.length) calcTideStatus(lastTideList);
  setText("chartSource",site&&site.code?`${site.name}(${site.code})`:"全球潮汐平台");
  if(!points.length){$("tideChart").innerText=msg||"暂无实时曲线数据";return;}
  if(!initChart()){ $("tideChart").innerText="ECharts 加载中"; return; }
  const axisFont=adaptiveChartFont(10,8,13), markFont=adaptiveChartFont(9,7,11), nameFont=adaptiveChartFont(11,9,14);
  const maxVal=Math.max(...points.map(p=>p.value));
  const markData=points.filter(p=>p.pointType==="extrema").map(p=>({name:p.extremaType,coord:[p.label,p.value],value:p.value,labelText:`${p.extremaType} ${p.label}\n${p.value}cm`,itemStyle:{color:p.extremaType==="高潮"?"#ffab00":"#00e676"},label:{formatter:params=>params.data.labelText,color:p.extremaType==="高潮"?"#ffab00":"#00e676",fontSize:markFont,fontWeight:"bold",lineHeight:markFont+1,position:"right",distance:4,offset:[0,-14]}}));
  tideChart.setOption({
    backgroundColor:"transparent",
    tooltip:{trigger:"axis",formatter:p=>`时间：${p[0].axisValue}<br>潮高：${p[0].value} cm`,backgroundColor:"rgba(15,21,40,.94)",borderColor:"rgba(0,229,255,.35)",textStyle:{color:"#e8eaf6",fontSize:axisFont}},
    grid:{left:44,right:44,top:42,bottom:34,containLabel:true},
    xAxis:{type:"category",data:points.map(p=>p.label),axisLabel:{rotate:0,interval:2,fontSize:axisFont,margin:6,color:"rgba(232,234,246,.65)"},axisLine:{lineStyle:{color:"rgba(0,229,255,.28)"}},axisTick:{lineStyle:{color:"rgba(0,229,255,.22)"}}},
    yAxis:{name:"潮高(cm)",type:"value",max:Math.ceil((maxVal+35)/50)*50,nameTextStyle:{fontSize:nameFont,color:"rgba(232,234,246,.65)"},axisLabel:{fontSize:axisFont,color:"rgba(232,234,246,.65)"},axisLine:{lineStyle:{color:"rgba(0,229,255,.28)"}},splitLine:{lineStyle:{color:"rgba(255,255,255,.07)"}}},
    series:[{name:"潮高",type:"line",data:points.map(p=>p.value),smooth:true,symbolSize:4,itemStyle:{color:"#00e5ff"},lineStyle:{color:"#00e5ff",width:2.4,shadowBlur:8,shadowColor:"rgba(0,229,255,.45)"},areaStyle:{color:{type:"linear",colorStops:[{offset:0,color:"rgba(0,229,255,.26)"},{offset:1,color:"rgba(0,229,255,.03)"}]}},markPoint:{symbol:"circle",symbolSize:18,data:markData}}]
  },true);
}
function clearModuleUnavailable(cardId){const card=$(cardId);if(card){const h=card.querySelector(".module-unavailable");if(h)h.remove();}}
function setAllText(ids, text){ids.forEach(id=>setText(id,text));}
function showWeatherUnavailable(){
  clearModuleUnavailable("weatherCard");
  setAllText(["airTemp","apparentTemp","tempRange","humidity","windSpeed","windDirection","windGusts","weatherSourceTime","windLevel"],"未知");
  setText("weatherTime","暂无明日数据"); setText("weatherText","未知");
  const icon=$("weatherIcon"); if(icon) icon.className="weather-icon";
}
function showWaveUnavailable(){
  clearModuleUnavailable("seaCard");
  setAllText(["waveHeight","waterTemp","waveLevel","waterComfort","swimTip","seaRisk"],"未知");
  setText("waveTime","暂无明日数据");
}
function showTideUnavailable(){
  clearModuleUnavailable("tideCard");
  setAllText(["tideBadge","tideStatusText","nextHigh","nextLow","currentLevel","tideTrend","tidePhase","tideProgress","highDelta","lowDelta","tideRange"],"未知");
  setText("tideUpdate","暂无明日数据"); setText("globalUpdate","数据更新 --"); setText("tableTime","暂无明日数据");
  $("mainBox").innerHTML='<div class="module-unavailable">暂无明日数据</div>';
}
function showChartUnavailable(){
  clearModuleUnavailable("chartCard");
  setText("chartTime","暂无明日数据");
  const tc=$("tideChart"); if(tc) tc.innerHTML='<div class="module-unavailable">暂无明日数据</div>';
}
async function loadWeather(){try{const r=await fetchJSON(apiUrl("/api/weather"));if(r&&r.tomorrow_unavailable){showWeatherUnavailable();return;}if(r&&r.data)renderWeather(r.data,r.updateTime);}catch(e){}}
async function loadWave(){try{const r=await fetchJSON(apiUrl("/api/wave"));if(r&&r.tomorrow_unavailable){showWaveUnavailable();return;}if(r&&r.data)renderWave(r.data,r.updateTime);}catch(e){}}
async function loadTide(){try{const r=await fetchJSON(apiUrl("/api/tide"));if(r&&r.tomorrow_unavailable){showTideUnavailable();return;}if(r&&r.data)renderTide(r,r.updateTime);}catch(e){$("mainBox").innerHTML="潮汐数据加载失败";}}
async function loadChart(){try{const r=await fetchJSON(apiUrl("/api/tideChart"));if(r&&r.tomorrow_unavailable){showChartUnavailable();return;}setText("chartTime","更新 "+(r.updateTime||"--"));renderChart(r.chart,r.msg,r.site);}catch(e){renderChart([],"潮汐曲线加载失败",null);}}
async function refreshAllData(){
  const btn=$("refreshBtn");
  if(btn){btn.disabled=true;btn.textContent="刷新中...";}
  try{
    await Promise.allSettled([loadTide(),loadChart(),loadWeather(),loadWave()]);
    reloadTyphoonFrame();
    if(btn)btn.textContent="刷新完成";
    setTimeout(()=>{if(btn){btn.textContent="🔄 刷新";btn.disabled=false;}},1200);
  }catch(e){
    if(btn){btn.textContent="刷新失败";setTimeout(()=>{btn.textContent="🔄 刷新";btn.disabled=false;},1600);}
  }
}
function boot(){
  updateClock(); setInterval(updateClock,1000);
  updateDayButtons();
  loadTide(); loadChart(); loadWeather(); loadWave();
  setInterval(loadTide,60*60*1000); setInterval(loadChart,6*60*60*1000); setInterval(loadWeather,10*60*1000); setInterval(loadWave,60*60*1000);
  setInterval(()=>{if(lastTideList.length)calcTideStatus(lastTideList)},60*1000);
  setTimeout(()=>{if(lastChartRaw)renderChart(lastChartRaw,"",lastChartSite)},1000);
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
