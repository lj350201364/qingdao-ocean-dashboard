import datetime
import base64
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request


SHANGHAI_TZ = datetime.timezone(datetime.timedelta(hours=8))


def now_cn():
    return datetime.datetime.now(SHANGHAI_TZ)


def normalize_clock(value, default):
    text = str(value or default).strip()
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", text)
    if not match:
        raise ValueError(f"时间格式不正确：{text}，请使用 HH:MM")
    return f"{match.group(1)}:{match.group(2)}"


def notification_window_status(settings, current=None):
    schedule = settings.get("notification_schedule") or {}
    enabled = schedule.get("enabled") is True
    start = normalize_clock(schedule.get("start"), "08:00")
    end = normalize_clock(schedule.get("end"), "18:00")
    current = current or now_cn()
    minute = current.hour * 60 + current.minute
    start_minute = int(start[:2]) * 60 + int(start[3:])
    end_minute = int(end[:2]) * 60 + int(end[3:])
    if not enabled or start_minute == end_minute:
        active = True
    elif start_minute < end_minute:
        active = start_minute <= minute < end_minute
    else:
        active = minute >= start_minute or minute < end_minute
    return {"enabled": enabled, "start": start, "end": end, "active": active}


def dotted_get(data, path):
    current = data
    for part in str(path).split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def numeric(value):
    if value is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else None


def conservative_wave_height(value):
    values = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", str(value or ""))]
    return max(values) if values else None


def angular_delta(a, b):
    return abs((float(a) - float(b) + 180) % 360 - 180)


def wind_relation(source_degree, shore_inward_degree):
    if source_degree is None or shore_inward_degree is None:
        return "unknown"
    # 气象风向表示风从哪里吹来；+180 转换为风吹向的方向。
    toward = (float(source_degree) + 180) % 360
    delta = angular_delta(toward, float(shore_inward_degree))
    if delta <= 45:
        return "onshore"
    if delta <= 90:
        return "cross_onshore"
    if delta < 135:
        return "cross_offshore"
    return "offshore"


def wind_level(speed_kmh):
    if speed_kmh is None:
        return "--"
    limits = [
        (1, "0级 静风"), (5, "1级 软风"), (11, "2级 轻风"),
        (19, "3级 微风"), (28, "4级 和风"), (38, "5级 清风"),
        (49, "6级 强风"), (61, "7级 疾风"), (74, "8级 大风"),
        (88, "9级 烈风"), (102, "10级 狂风"), (117, "11级 暴风"),
        (math.inf, "12级 飓风"),
    ]
    for limit, text in limits:
        if speed_kmh <= limit:
            return text
    return "--"


def format_direction(degree):
    if degree is None:
        return "--"
    names = ["北风", "东北风", "东风", "东南风", "南风", "西南风", "西风", "西北风"]
    return names[int((float(degree) + 22.5) // 45) % 8]


def condition_matches(condition, snapshot, previous_snapshot=None):
    actual = dotted_get(snapshot, condition.get("field"))
    expected = condition.get("value")
    operator = condition.get("operator", "equals")
    if operator == "equals":
        return actual == expected
    if operator == "not_equals":
        return actual != expected
    if operator == "in":
        return actual in (expected or [])
    if operator == "not_in":
        return actual not in (expected or [])
    if operator in ("gt", "gte", "lt", "lte"):
        left, right = numeric(actual), numeric(expected)
        if left is None or right is None:
            return False
        return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[operator]
    if operator == "between":
        left = numeric(actual)
        if left is None or not isinstance(expected, list) or len(expected) != 2:
            return False
        return numeric(expected[0]) <= left <= numeric(expected[1])
    if operator in ("crosses_up", "crosses_down"):
        previous = dotted_get(previous_snapshot or {}, condition.get("field"))
        before, current, threshold = numeric(previous), numeric(actual), numeric(expected)
        if None in (before, current, threshold):
            return False
        return before < threshold <= current if operator == "crosses_up" else before > threshold >= current
    return False


def group_matches(group, snapshot, previous_snapshot=None):
    if not isinstance(group, dict):
        return False
    all_conditions = group.get("all") or []
    any_conditions = group.get("any") or []
    if all_conditions and not all(condition_matches(c, snapshot, previous_snapshot) for c in all_conditions):
        return False
    if any_conditions and not any(condition_matches(c, snapshot, previous_snapshot) for c in any_conditions):
        return False
    return bool(all_conditions or any_conditions)


def render_message(template, snapshot):
    aliases = {
        "潮汐状态": "tide.phase_text",
        "潮段进度": "tide.progress",
        "当前潮高": "tide.level_cm",
        "风向": "wind.direction",
        "风级": "wind.level",
        "风速": "wind.speed_kmh",
        "近海浪高": "wave.height_m",
    }
    def replace(match):
        path = aliases.get(match.group(1), match.group(1))
        value = dotted_get(snapshot, path)
        return "--" if value is None else str(value)
    return re.sub(r"\{([^{}]+)\}", replace, str(template or ""))


def canonical_tide_segment(snapshot):
    """把新旧快照中的相对潮段统一成跨午夜稳定的绝对时间 ID。"""
    tide = dotted_get(snapshot, "tide") or {}
    previous = tide.get("previous_extrema") or {}
    following = tide.get("next_extrema") or {}
    generated_at = snapshot.get("generated_at") if isinstance(snapshot, dict) else None
    try:
        generated = datetime.datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        generated = now_cn()

    def endpoint(item):
        time_match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(item.get("time", "")).strip())
        offset = numeric(item.get("offset"))
        if not time_match or offset is None:
            return None
        date_value = generated.date() + datetime.timedelta(days=int(offset))
        return f"{date_value.isoformat()}T{int(time_match.group(1)):02d}:{time_match.group(2)}"

    previous_at = endpoint(previous)
    following_at = endpoint(following)
    if previous_at and following_at:
        return f"{previous_at}->{following_at}"
    return tide.get("segment_id")


class NotificationManager:
    def __init__(self, config_path, db_path, snapshot_provider):
        self.config_path = config_path
        self.db_path = db_path
        self.snapshot_provider = snapshot_provider
        self.secret_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "notification_secrets.json")
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = threading.RLock()
        self.previous_snapshot = None
        self.match_since = {}
        self.last_snapshot = None
        self.last_error = ""
        self.last_run = "--"
        self._init_db()

    def _load_secrets(self):
        try:
            with open(self.secret_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}

    def _secret(self, name):
        if not name:
            return ""
        return os.environ.get(name, "") or str(self._load_secrets().get(name, ""))

    def save_config(self, incoming, secret_values=None):
        if not isinstance(incoming, dict) or not isinstance(incoming.get("settings"), dict):
            raise ValueError("配置格式不正确")
        roles = incoming.get("roles")
        rules = incoming.get("rules")
        if not isinstance(roles, dict) or not roles:
            raise ValueError("至少需要一个角色")
        if not isinstance(rules, list):
            raise ValueError("规则必须是数组")
        known_operators = {"equals", "gte", "lte", "gt", "lt", "in", "not_in", "changed_to"}
        for rule in rules:
            if not isinstance(rule, dict) or not str(rule.get("id", "")).strip():
                raise ValueError("每条规则都必须有唯一 ID")
            for condition in (rule.get("conditions") or {}).get("all", []):
                if condition.get("operator") not in known_operators:
                    raise ValueError(f"规则 {rule.get('id')} 包含不支持的操作符")
        if len({rule["id"] for rule in rules}) != len(rules):
            raise ValueError("规则 ID 不能重复")
        incoming["settings"]["interval_seconds"] = max(30, int(incoming["settings"].get("interval_seconds", 60)))
        schedule = incoming["settings"].get("notification_schedule") or {}
        incoming["settings"]["notification_schedule"] = {
            "enabled": schedule.get("enabled") is True,
            "start": normalize_clock(schedule.get("start"), "08:00"),
            "end": normalize_clock(schedule.get("end"), "18:00"),
        }
        temp_path = self.config_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(incoming, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, self.config_path)
        if isinstance(secret_values, dict):
            allowed = {value.get(key) for value in roles.values() for key in ("webhook_env", "secret_env", "mention_mobiles_env") if value.get(key)}
            allowed.update({incoming["settings"].get("default_webhook_env"), incoming["settings"].get("default_secret_env")})
            secrets = self._load_secrets()
            for key, value in secret_values.items():
                if key in allowed:
                    if str(value).strip():
                        secrets[key] = str(value).strip()
                    elif value == "":
                        secrets.pop(key, None)
            secret_temp = self.secret_path + ".tmp"
            with open(secret_temp, "w", encoding="utf-8") as handle:
                json.dump(secrets, handle, ensure_ascii=False, indent=2)
            os.replace(secret_temp, self.secret_path)
        return self.public_config()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS notification_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    rule_id TEXT NOT NULL,
                    rule_name TEXT NOT NULL,
                    tide_segment_id TEXT,
                    roles TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    sent_at TEXT
                )
            """)

    def load_config(self):
        with open(self.config_path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="ocean-notification-engine", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)

    def _loop(self):
        if self.stop_event.wait(10):
            return
        while not self.stop_event.is_set():
            try:
                self.evaluate_once()
            except Exception as exc:
                self.last_error = repr(exc)
                print("【通知规则引擎】异常：", repr(exc))
            try:
                interval = max(30, int(self.load_config().get("settings", {}).get("interval_seconds", 60)))
            except Exception:
                interval = 60
            self.stop_event.wait(interval)

    def evaluate_once(self):
        with self.lock:
            # 记录每次检查尝试，而不是只记录成功取得快照的检查，便于状态页准确诊断。
            self.last_run = now_cn().isoformat(timespec="seconds")
            config = self.load_config()
            settings = config.get("settings") or {}
            if not settings.get("enabled", True):
                return {"skipped": "disabled"}
            window = notification_window_status(settings)
            if not window["active"]:
                self.last_error = ""
                self.match_since.clear()
                self.previous_snapshot = None
                return {"skipped": "outside_notification_hours", "notification_schedule": window}
            active_rules = [rule for rule in (config.get("rules") or []) if rule.get("enabled")]
            if not active_rules:
                self.last_error = ""
                return {"skipped": "no_enabled_rules"}
            snapshot = self.snapshot_provider(config)
            self.last_snapshot = snapshot
            self.last_error = ""
            fired = []
            for rule in active_rules:
                rule_id = rule.get("id") or "unnamed"
                matched = group_matches(rule.get("conditions"), snapshot, self.previous_snapshot)
                if not matched:
                    self.match_since.pop(rule_id, None)
                    continue
                first_match = self.match_since.setdefault(rule_id, time.time())
                stable_seconds = max(0, float(rule.get("stable_for_minutes", 0)) * 60)
                if time.time() - first_match < stable_seconds:
                    continue
                event = self._build_event(rule, config, snapshot)
                if self._create_event(event):
                    self._deliver_event(event, config)
                    fired.append(event["event_key"])
            self.previous_snapshot = snapshot
            return {"fired": fired, "snapshot": snapshot}

    def _build_event(self, rule, config, snapshot):
        dedupe = rule.get("deduplication", "per_tide_segment")
        segment_id = canonical_tide_segment(snapshot) or now_cn().strftime("%Y-%m-%d")
        suffix = segment_id if dedupe == "per_tide_segment" else now_cn().strftime("%Y-%m-%d-%H")
        return {
            "event_key": f"{rule.get('id')}:{suffix}",
            "rule_id": rule.get("id", "unnamed"),
            "rule_name": rule.get("name", rule.get("id", "未命名规则")),
            "segment_id": segment_id,
            "roles": rule.get("roles") or [],
            "message": render_message(rule.get("message"), snapshot),
            "snapshot": snapshot,
            "deduplication": dedupe,
        }

    def _create_event(self, event):
        with self._connect() as conn:
            if event.get("deduplication") == "per_tide_segment":
                # 兼容升级前已经写入数据库的相对 offset 潮段 ID，防止部署修复后
                # 当前跨午夜潮段因 ID 格式变化再补发一次。
                previous_events = conn.execute(
                    "SELECT tide_segment_id,snapshot_json FROM notification_events WHERE rule_id=? ORDER BY id DESC LIMIT 50",
                    (event["rule_id"],),
                ).fetchall()
                for previous_event in previous_events:
                    if previous_event["tide_segment_id"] == event["segment_id"]:
                        return False
                    try:
                        previous_snapshot = json.loads(previous_event["snapshot_json"])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if canonical_tide_segment(previous_snapshot) == event["segment_id"]:
                        return False
            try:
                conn.execute(
                    "INSERT INTO notification_events(event_key,rule_id,rule_name,tide_segment_id,roles,message,status,snapshot_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (event["event_key"], event["rule_id"], event["rule_name"], event["segment_id"],
                     json.dumps(event["roles"], ensure_ascii=False), event["message"], "pending",
                     json.dumps(event["snapshot"], ensure_ascii=False), now_cn().isoformat(timespec="seconds")),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def _deliver_event(self, event, config):
        settings = config.get("settings") or {}
        if settings.get("dry_run", True):
            self._mark_event(event["event_key"], "dry_run", "", 0)
            print(f"【通知演练】{event['rule_name']} -> {','.join(event['roles'])}: {event['message']}")
            return
        deliveries = {}
        for role_id in event["roles"]:
            role = (config.get("roles") or {}).get(role_id) or {}
            webhook = self._secret(role.get("webhook_env", "")) or self._secret(settings.get("default_webhook_env", "DINGTALK_WEBHOOK_URL"))
            secret = self._secret(role.get("secret_env", "")) or self._secret(settings.get("default_secret_env", "DINGTALK_SECRET"))
            mobiles_text = self._secret(role.get("mention_mobiles_env", ""))
            mobiles = [x.strip() for x in mobiles_text.split(",") if x.strip()]
            if webhook:
                item = deliveries.setdefault((webhook, secret), {"mobiles": [], "role_names": []})
                item["mobiles"].extend(mobiles)
                item["role_names"].append(role.get("name", role_id))
        if not deliveries:
            self._mark_event(event["event_key"], "skipped_no_webhook", "未配置钉钉 Webhook", 0)
            return
        errors = []
        attempts = 0
        for (webhook, secret), target in deliveries.items():
            last_error = None
            for retry_index in range(3):
                attempts += 1
                try:
                    self._send_dingtalk(webhook, secret, event, target, settings)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if retry_index < 2:
                        time.sleep(2 ** retry_index)
            if last_error is not None:
                errors.append(repr(last_error))
        if errors:
            self._mark_event(event["event_key"], "failed", "; ".join(errors), attempts)
        else:
            self._mark_event(event["event_key"], "sent", "", attempts, sent=True)

    def _send_dingtalk(self, webhook, secret, event, target, settings):
        role_names = "、".join(target["role_names"])
        prefix = settings.get("message_prefix", "海况通知")
        content = f"【{prefix} · {event['rule_name']}】\n通知角色：{role_names}\n\n{event['message']}\n\n时间：{now_cn().strftime('%Y-%m-%d %H:%M')}"
        payload = {
            "msgtype": "text",
            "text": {"content": content},
            "at": {"atMobiles": sorted(set(target["mobiles"])), "isAtAll": False},
        }
        if secret:
            timestamp = str(int(time.time() * 1000))
            digest = hmac.new(secret.encode("utf-8"), f"{timestamp}\n{secret}".encode("utf-8"), hashlib.sha256).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("ascii"))
            webhook = f"{webhook}{'&' if '?' in webhook else '?'}timestamp={timestamp}&sign={sign}"
        request = urllib.request.Request(
            webhook,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(request, timeout=12) as response:
            result = json.loads(response.read().decode("utf-8", errors="replace"))
        if result.get("errcode") not in (None, 0):
            raise RuntimeError(result.get("errmsg") or f"钉钉返回错误 {result.get('errcode')}")

    def _mark_event(self, event_key, status, error, attempts, sent=False):
        with self._connect() as conn:
            conn.execute(
                "UPDATE notification_events SET status=?,error=?,attempts=?,sent_at=? WHERE event_key=?",
                (status, error, attempts, now_cn().isoformat(timespec="seconds") if sent else None, event_key),
            )

    def logs(self, limit=50):
        limit = max(1, min(200, int(limit)))
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM notification_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def public_config(self):
        config = self.load_config()
        settings = config.get("settings") or {}
        settings["default_webhook_configured"] = bool(self._secret(settings.get("default_webhook_env", "DINGTALK_WEBHOOK_URL")))
        settings["default_secret_configured"] = bool(self._secret(settings.get("default_secret_env", "DINGTALK_SECRET")))
        for role in (config.get("roles") or {}).values():
            role["role_webhook_configured"] = bool(self._secret(role.get("webhook_env", "")))
            role["role_secret_configured"] = bool(self._secret(role.get("secret_env", "")))
            role["webhook_configured"] = bool(self._secret(role.get("webhook_env", "")) or self._secret(settings.get("default_webhook_env", "DINGTALK_WEBHOOK_URL")))
            role["secret_configured"] = bool(self._secret(role.get("secret_env", "")) or self._secret(settings.get("default_secret_env", "DINGTALK_SECRET")))
            role["mobiles_configured"] = bool(self._secret(role.get("mention_mobiles_env", "")))
        return config

    def send_test(self, role_id):
        config = self.load_config()
        role = (config.get("roles") or {}).get(role_id)
        if role is None:
            raise ValueError("角色不存在")
        settings = dict(config.get("settings") or {})
        webhook = self._secret(role.get("webhook_env", "")) or self._secret(settings.get("default_webhook_env", "DINGTALK_WEBHOOK_URL"))
        secret = self._secret(role.get("secret_env", "")) or self._secret(settings.get("default_secret_env", "DINGTALK_SECRET"))
        if not webhook:
            raise ValueError("该角色尚未配置专属或默认 Webhook")
        mobiles = [x.strip() for x in self._secret(role.get("mention_mobiles_env", "")).split(",") if x.strip()]
        event = {"event_key": "manual-test", "rule_name": "配置测试", "roles": [role_id], "message": "钉钉通知通道连接成功。这是一条人工触发的测试消息。"}
        self._send_dingtalk(webhook, secret, event, {"mobiles": mobiles, "role_names": [role.get("name", role_id)]}, settings)
        return {"role": role_id, "message": "测试消息已提交"}

    def status(self):
        try:
            schedule = notification_window_status((self.load_config().get("settings") or {}))
        except Exception:
            schedule = {"enabled": False, "start": "08:00", "end": "18:00", "active": True}
        return {
            "running": bool(self.thread and self.thread.is_alive()),
            "last_run": self.last_run,
            "last_error": self.last_error,
            "last_snapshot": self.last_snapshot,
            "notification_schedule": schedule,
        }
