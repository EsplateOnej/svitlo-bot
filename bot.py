#!/usr/bin/env python3
"""
Svitlo bot: Telegram notifications about power outages in Kyiv (YASNO schedule)
+ real check of whether there is power at home (TCP check of the home router).

Runs once per launch (GitHub Actions cron every 5 min). State lives in state.json.
Uses only the Python standard library.

Environment variables (set as GitHub Secrets / Variables):
  TG_TOKEN        - bot token from @BotFather                       (required)
  TG_CHAT_ID      - your chat id (the bot writes only here)         (required)
  QUEUE           - your queue/group, e.g. "3.1"                    (required)
  HOME_HOST       - public IP or DDNS name of the home router       (optional)
  HOME_PORT       - open TCP port on the router, default 80         (optional)
  REMIND_MINUTES  - warn this many minutes before a change, def 30  (optional)
  YASNO_REGION    - default 25 (Kyiv)
  YASNO_DSO       - default 902 (DTEK Kyiv electric networks)
  POWER_SOURCE    - how to detect power at home: "router" (default if HOME_HOST)
                    or "ecoflow" (AC input of the EcoFlow station)
  ECOFLOW_ACCESS_KEY / ECOFLOW_SECRET_KEY / ECOFLOW_SN  - EcoFlow developer API
  ECOFLOW_HOST    - default https://api-e.ecoflow.com (Europe)
  ECOFLOW_NAME, ECOFLOW_CAPACITY_WH                      - optional
  MARSTEK_EMAIL / MARSTEK_PASSWORD                      - Marstek app account
  MARSTEK_CAPACITY_WH (default 5120), MARSTEK_MIN_SOC (default 10)
  LOW_LEVELS      - low-charge alert levels, default "20,10"
  STATION_DEBUG=1 - print raw station fields to the log
  MOCK_STATIONS   - path to a local JSON with stations (testing)
  DRY_RUN=1       - print messages instead of sending
  MOCK_SCHEDULE   - path to a local JSON file instead of the YASNO API (testing)
"""
import hashlib
import hmac
import json
import os
import random
import socket
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Kyiv")
STATE_FILE = os.environ.get("STATE_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

TG_TOKEN = os.environ.get("TG_TOKEN", "")
CHAT_ID = os.environ.get("TG_CHAT_ID", "")
QUEUE = os.environ.get("QUEUE", "").strip()
HOME_HOST = os.environ.get("HOME_HOST", "").strip()
HOME_PORT = int(os.environ.get("HOME_PORT") or 80)   # 0 = ping
REMIND = int(os.environ.get("REMIND_MINUTES") or 30)
REGION = os.environ.get("YASNO_REGION") or "25"
DSO = os.environ.get("YASNO_DSO") or "902"
DRY_RUN = os.environ.get("DRY_RUN") == "1"
MOCK = os.environ.get("MOCK_SCHEDULE")

YASNO_URL = (
    f"https://app.yasno.ua/api/blackout-service/public/shutdowns/"
    f"regions/{REGION}/dsos/{DSO}/planned-outages"
)
UA = {"User-Agent": "Mozilla/5.0 (svitlo-bot)", "Accept": "application/json"}

STATUS_TEXT = {
    "ScheduleApplies": "график действует",
    "WaitingForSchedule": "ожидается график",
    "EmergencyShutdowns": "⚠️ аварийные отключения (график не действует)",
}


# ---------------------------------------------------------------- helpers
def now():
    return datetime.now(TZ)


def hm(minutes):
    minutes = int(minutes)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def dur(td):
    m = int(td.total_seconds() // 60)
    h, m = divmod(m, 60)
    return f"{h} ч {m:02d} мин" if h else f"{m} мин"


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


def http_json(url, data=None, timeout=20):
    body = None
    headers = dict(UA)
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def send(text, reply_to=None, _orig=None):
    if DRY_RUN or not TG_TOKEN:
        print("---- MESSAGE ----\n" + text + "\n-----------------")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    body = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_to:
        body["reply_to_message_id"] = reply_to
    for attempt in range(3):
        try:
            http_json(url, body)
            return
        except Exception as e:  # noqa: BLE001
            print(f"send failed ({attempt + 1}/3): {e}", file=sys.stderr)
            time.sleep(3)
    if not reply_to:                       # keep it and retry on the next check
        _OUTBOX.append(_orig or {"text": text, "ts": time.time()})


_OUTBOX = []


def flush_outbox(state):
    """Resend notifications that failed earlier (Telegram/network hiccup)."""
    pending = state.pop("outbox", []) + _OUTBOX[:]
    _OUTBOX.clear()
    for m in pending:
        if time.time() - m["ts"] > 6 * 3600:
            continue                       # too old to be useful
        late = datetime.fromtimestamp(m["ts"], TZ)
        send(f"⌛ <i>(с опозданием, {late:%H:%M})</i>\n" + m["text"], _orig=m)


# ---------------------------------------------------------------- schedule
def fetch_schedule():
    if MOCK:
        with open(MOCK, encoding="utf-8") as f:
            data = json.load(f)
    else:
        last = None
        for _ in range(3):
            try:
                data = http_json(YASNO_URL)
                break
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(5)
        else:
            raise RuntimeError(f"YASNO API недоступен: {last}")
    if QUEUE not in data:
        raise KeyError(f"Очередь {QUEUE} не найдена. Есть: {', '.join(sorted(data))}")
    return data[QUEUE]


def day_date(day):
    """'2025-11-05T00:00:00+02:00' -> date in Kyiv time."""
    return datetime.fromisoformat(day["date"]).astimezone(TZ).date()


def outages(day):
    """List of (start_min, end_min) of definite outages, adjacent slots merged."""
    res = []
    for s in sorted(day.get("slots", []), key=lambda s: s["start"]):
        if s.get("type") != "Definite":
            continue
        if res and res[-1][1] >= s["start"]:
            res[-1][1] = max(res[-1][1], s["end"])
        else:
            res.append([s["start"], s["end"]])
    return [tuple(x) for x in res]


def format_day(title, day):
    if not day:
        return f"<b>{title}</b>: нет данных"
    d = day_date(day).strftime("%d.%m")
    status = day.get("status", "")
    lines = [f"<b>{title} ({d})</b> — {STATUS_TEXT.get(status, status)}"]
    if status == "ScheduleApplies":
        offs = outages(day)
        if offs:
            total = sum(e - s for s, e in offs)
            for s, e in offs:
                lines.append(f"  🔴 {hm(s)}–{hm(e)}")
            lines.append(f"  Без света: {total // 60} ч {total % 60:02d} мин")
        else:
            lines.append("  🟢 отключений не запланировано")
    return "\n".join(lines)


def format_schedule(g):
    return (f"📅 <b>График, очередь {QUEUE}</b>\n\n"
            + format_day("Сегодня", g.get("today")) + "\n\n"
            + format_day("Завтра", g.get("tomorrow")))


def schedule_fingerprint(g):
    parts = []
    for key in ("today", "tomorrow"):
        day = g.get(key)
        if day:
            parts.append([day.get("date"), day.get("status"), outages(day)])
    return hashlib.sha1(json.dumps(parts).encode()).hexdigest()[:12]


def absolute_events(g):
    """All definite outages of today+tomorrow as (start_dt, end_dt), merged across midnight."""
    ev = []
    for key in ("today", "tomorrow"):
        day = g.get(key)
        if not day or day.get("status") != "ScheduleApplies":
            continue
        base = datetime.combine(day_date(day), datetime.min.time(), TZ)
        for s, e in outages(day):
            st, en = base + timedelta(minutes=s), base + timedelta(minutes=e)
            if ev and ev[-1][1] >= st:
                ev[-1] = (ev[-1][0], max(ev[-1][1], en))
            else:
                ev.append((st, en))
    return ev


DAILY_AT = os.environ.get("DAILY_AT", "07:30").strip()   # "" = no morning summary


def check_schedule(state):
    try:
        g = fetch_schedule()
    except Exception:
        # API down: still send reminders from the last known schedule, then report the error
        if state.get("last_schedule"):
            schedule_reminders(state, state["last_schedule"])
        raise
    msgs = []

    # 1) schedule published / changed — compared per calendar date, so the
    #    midnight shift "tomorrow -> today" is not reported as a change
    old_days = state.get("days")
    first_run = old_days is None and "schedule_fp" not in state
    silent = old_days is None and not first_run      # upgrade from older state format
    old_days = old_days or {}
    new_days = {}
    if first_run:
        msgs.append(format_schedule(g))
    for key, title in (("today", "сегодня"), ("tomorrow", "завтра")):
        day = g.get(key)
        if not day:
            continue
        d = day_date(day).isoformat()
        cur = {"status": day.get("status"), "offs": [list(x) for x in outages(day)]}
        new_days[d] = cur
        old = old_days.get(d)
        if first_run or silent or old == cur:
            continue
        label = f"{title} ({day_date(day):%d.%m})"
        if cur["status"] == "EmergencyShutdowns":
            if not old or old["status"] != "EmergencyShutdowns":
                msgs.append(f"🚨 <b>Аварийные отключения</b> на {label}!\n"
                            f"Графики сейчас не действуют, свет могут отключить в любой момент.")
        elif old is None or (old["status"] == "WaitingForSchedule" and cur["status"] == "ScheduleApplies"):
            if cur["status"] == "ScheduleApplies":
                msgs.append(f"📅 Вышел график на {label}\n\n" + format_day(title.capitalize(), day))
        elif old["status"] == "EmergencyShutdowns" and cur["status"] == "ScheduleApplies":
            msgs.append(f"✅ Аварийные отключения отменены, снова действует график\n\n"
                        + format_day(title.capitalize(), day))
        elif cur["status"] == "ScheduleApplies":
            msgs.append(f"🔄 <b>График на {label} изменился</b>\n\n" + format_day(title.capitalize(), day))
    state["days"] = new_days
    state.pop("schedule_fp", None)

    for m in msgs:
        send(m)
    state["last_schedule"] = {k: g.get(k) for k in ("today", "tomorrow")}
    schedule_reminders(state, g)

    # 3) morning summary (also shows that the bot is alive)
    t = now()
    if DAILY_AT and state.get("daily_sent") != t.date().isoformat() and f"{t:%H:%M}" >= DAILY_AT:
        if state.get("daily_sent") is not None:      # not on the very first run
            send("☀️ <b>Доброе утро!</b>\n\n" + format_day("Сегодня", g.get("today")))
        state["daily_sent"] = t.date().isoformat()


def schedule_reminders(state, g):
    msgs = []
    # 2) reminders before off / on, and a notice at the moment itself
    t = now()
    sent = set(state.get("reminded", []))
    for st, en in absolute_events(g):
        for kind, moment in (("off", st), ("on", en)):
            key = f"{kind}:{moment.isoformat()}"
            left = (moment - t).total_seconds() / 60
            if key not in sent and 0 <= left <= REMIND:
                if kind == "off":
                    msgs.append(f"⏳ Через {int(left)} мин по графику <b>отключение</b> "
                                f"({moment:%H:%M}–{en:%H:%M}, очередь {QUEUE})")
                else:
                    msgs.append(f"💡 Через {int(left)} мин по графику должны "
                                f"<b>включить</b> свет ({moment:%H:%M})")
                sent.add(key)
            now_key = f"{kind}-now:{moment.isoformat()}"
            if now_key not in sent and -5 <= left < 0.5:
                if kind == "off":
                    msgs.append(f"🔴 По графику <b>сейчас отключение</b> до {en:%H:%M}")
                else:
                    msgs.append(f"🟢 По графику свет <b>должны дать сейчас</b> ({moment:%H:%M})")
                sent.add(now_key)
    # keep only recent reminder keys
    cutoff = (t - timedelta(days=2)).isoformat()
    state["reminded"] = sorted(k for k in sent if k.split(":", 1)[1] >= cutoff)
    for m in msgs:
        send(m)


# ---------------------------------------------------------------- power stations
EF_AK = os.environ.get("ECOFLOW_ACCESS_KEY", "").strip()
EF_SK = os.environ.get("ECOFLOW_SECRET_KEY", "").strip()
EF_SN = os.environ.get("ECOFLOW_SN", "").strip()
EF_HOST = os.environ.get("ECOFLOW_HOST") or "https://api-e.ecoflow.com"
EF_NAME = os.environ.get("ECOFLOW_NAME") or "EcoFlow"
EF_CAP = float(os.environ.get("ECOFLOW_CAPACITY_WH") or 0)
MS_EMAIL = os.environ.get("MARSTEK_EMAIL", "").strip()
MS_PWD = os.environ.get("MARSTEK_PASSWORD", "")
MS_HOST = os.environ.get("MARSTEK_HOST") or "https://eu.hamedata.com"
MS_CAP = float(os.environ.get("MARSTEK_CAPACITY_WH") or 5120)   # Venus E 3.0 = 5.12 kWh
MS_MIN_SOC = float(os.environ.get("MARSTEK_MIN_SOC") or 10)
LOW_LEVELS = sorted({int(x) for x in (os.environ.get("LOW_LEVELS") or "50,40,30,20,10").split(",") if x.strip()},
                    reverse=True)
POWER_SOURCE = (os.environ.get("POWER_SOURCE") or ("router" if HOME_HOST else "")).lower()
STATION_DEBUG = os.environ.get("STATION_DEBUG") == "1"
MOCK_STATIONS = os.environ.get("MOCK_STATIONS")

# EcoFlow models use different field names, so we try several
EF_KEYS = {
    "soc": ["pd.soc", "bms_bmsStatus.soc", "bms_emsStatus.lcdShowSoc", "cmsBattSoc", "bmsBattSoc", "soc"],
    "in": ["pd.wattsInSum", "powInSumW", "inv.inputWatts", "inputWatts"],
    "out": ["pd.wattsOutSum", "powOutSumW", "inv.outputWatts", "outputWatts"],
    "chg": ["bms_emsStatus.chgRemainTime", "cmsChgRemTime", "bmsChgRemTime", "ems.chgRemainTime"],
    "dsg": ["bms_emsStatus.dsgRemainTime", "cmsDsgRemTime", "bmsDsgRemTime", "ems.dsgRemainTime"],
    "remain": ["pd.remainTime"],
    "ac_v": ["inv.acInVol", "acInVol"],
    "ac_flag": ["plugInInfoAcInFlag"],
    "ac_w": ["powGetAcIn", "inv.acInWatts"],
}


def pick(data, names):
    for n in names:
        v = data.get(n)
        if isinstance(v, (int, float)):
            return v
    return None


def ecoflow_quota(sn):
    params = {"sn": sn}
    nonce, ts = str(random.randint(100000, 999999)), str(int(time.time() * 1000))
    q = "&".join(f"{k}={params[k]}" for k in sorted(params))
    to_sign = f"{q}&accessKey={EF_AK}&nonce={nonce}&timestamp={ts}"
    sign = hmac.new(EF_SK.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"{EF_HOST}/iot-open/sign/device/quota/all?{q}",
        headers={**UA, "accessKey": EF_AK, "nonce": nonce, "timestamp": ts, "sign": sign})
    with urllib.request.urlopen(req, timeout=20) as r:
        resp = json.loads(r.read().decode())
    if str(resp.get("code")) != "0":
        raise RuntimeError(f"EcoFlow: {resp.get('message')} (code {resp.get('code')})")
    return resp.get("data") or {}


def ecoflow_stations():
    res = []
    sns = [s.strip() for s in EF_SN.split(",") if s.strip()]
    for i, sn in enumerate(sns):
        name = EF_NAME if len(sns) == 1 else f"{EF_NAME} {i + 1}"
        d = ecoflow_quota(sn)
        if STATION_DEBUG:
            print(f"EcoFlow {name} fields:", json.dumps(d, ensure_ascii=False, sort_keys=True))
        soc = pick(d, EF_KEYS["soc"])
        if soc is None:
            raise RuntimeError(f"EcoFlow {name}: не нашёл уровень заряда (запусти со STATION_DEBUG=1)")
        p_in, p_out = pick(d, EF_KEYS["in"]) or 0, pick(d, EF_KEYS["out"]) or 0
        chg, dsg = pick(d, EF_KEYS["chg"]), pick(d, EF_KEYS["dsg"])
        rem = pick(d, EF_KEYS["remain"])
        if rem is not None and chg is None and dsg is None:   # older models: + charging, - discharging
            chg, dsg = (rem, None) if rem > 0 else (None, -rem)
        charging = p_in > p_out + 5
        remain = chg if charging else dsg
        if remain is not None and (remain <= 0 or remain >= 5999):   # 5999 = "unknown"
            remain = None
        if remain is None and EF_CAP:
            remain = estimate(soc, EF_CAP, 0, p_in - p_out if charging else p_out - p_in, charging)
        # grid presence: AC input voltage (mV or V), AC-plugged flag or AC input watts
        grid = None
        v = pick(d, EF_KEYS["ac_v"])
        if v is not None:
            grid = (v / 1000 if v > 1000 else v) > 100
        elif pick(d, EF_KEYS["ac_flag"]) is not None:
            grid = pick(d, EF_KEYS["ac_flag"]) == 1
        elif pick(d, EF_KEYS["ac_w"]) is not None:
            grid = pick(d, EF_KEYS["ac_w"]) > 0
        res.append({"name": name, "soc": round(soc), "in": round(p_in), "out": round(p_out),
                    "charging": charging, "remain": remain, "grid": grid, "brand": "ecoflow"})
    return res


_ms_token = None
_ms_raw = []    # last raw device list (for the /marstek command)


def marstek_login():
    pwd = hashlib.md5(MS_PWD.encode()).hexdigest()
    login_q = urllib.parse.urlencode({"pwd": pwd, "mailbox": MS_EMAIL})
    req = urllib.request.Request(f"{MS_HOST}/app/Solar/v2_get_device.php?{login_q}",
                                 data=b"", headers=UA, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            token = json.loads(r.read().decode()).get("token")
    except Exception as e:  # noqa: BLE001  (never leak the URL with credentials)
        raise RuntimeError(f"Marstek: ошибка входа ({type(e).__name__})") from None
    if not token:
        raise RuntimeError("Marstek: вход не удался — проверь email/пароль")
    return token


def marstek_stations():
    global _ms_token, _ms_raw
    devices = None
    for _ in range(2):                     # reuse the token; log in again if it expired
        if not _ms_token:
            _ms_token = marstek_login()
        try:
            resp = http_json(f"{MS_HOST}/ems/api/v1/getDeviceList?"
                             + urllib.parse.urlencode({"token": _ms_token}))
        except Exception:  # noqa: BLE001
            resp = {}
        if isinstance(resp.get("data"), list):
            devices = [d for d in resp["data"] if isinstance(d, dict) and "soc" in d]
            break
        _ms_token = None
    if devices is None:
        raise RuntimeError("Marstek: облако не вернуло список устройств")
    _ms_raw = [{k: v for k, v in d.items() if k not in ("sn", "devid", "mac", "token")} for d in devices]
    if STATION_DEBUG:
        safe = [{k: v for k, v in d.items() if k not in ("sn", "devid", "mac")} for d in devices]
        print("Marstek devices:", json.dumps(safe, ensure_ascii=False))
    res = []
    for d in devices:
        soc = float(d.get("soc") or 0)
        chg, dsg = float(d.get("charge") or 0), float(d.get("discharge") or 0)
        charging = chg > dsg + 5
        item = {"name": d.get("name") or "Marstek", "soc": round(soc), "in": round(chg),
                "out": round(dsg), "charging": charging, "grid": None, "brand": "marstek",
                "remain": estimate(soc, MS_CAP, MS_MIN_SOC, chg if charging else dsg, charging)}
        # The battery reports to the cloud only every ~30 min when idle, so show the data age
        # and treat it as "no connection" only after 6 hours of silence.
        try:
            age = time.time() - float(d.get("report_time"))
            if 0 <= age < 30 * 86400:
                item["age_min"] = age / 60
                if age > 6 * 3600:
                    item["stale"] = datetime.fromtimestamp(float(d["report_time"]), TZ).strftime("%d.%m %H:%M")
        except (TypeError, ValueError):
            pass
        if float(d.get("load") or 0) > 0:
            item["load"] = round(float(d["load"]))
        res.append(item)
    return res


def estimate(soc, cap_wh, min_soc, watts, charging):
    """Minutes to full (charging) or to min_soc (discharging); None if no load."""
    if not watts or watts < 5:
        return None
    wh = (100 - soc) / 100 * cap_wh if charging else max(soc - min_soc, 0) / 100 * cap_wh
    return wh / watts * 60


_stations_cache = None


def get_stations():
    """Returns (stations, errors). Fetched at most once per run."""
    global _stations_cache
    if _stations_cache is None:
        st, errs = [], []
        if MOCK_STATIONS:
            with open(MOCK_STATIONS, encoding="utf-8") as f:
                st = json.load(f)
        else:
            if EF_AK and EF_SK and EF_SN:
                try:
                    st += ecoflow_stations()
                except Exception as e:  # noqa: BLE001
                    errs.append(f"EcoFlow: {e}")
            if MS_EMAIL and MS_PWD:
                try:
                    st += marstek_stations()
                except Exception as e:  # noqa: BLE001
                    errs.append(f"Marstek: {e}")
        _stations_cache = (st, errs)
    return _stations_cache


def stations_configured():
    return bool(MOCK_STATIONS or (EF_AK and EF_SK and EF_SN) or (MS_EMAIL and MS_PWD))


def format_station(s):
    icon = "🪫" if s["soc"] <= (LOW_LEVELS[-1] if LOW_LEVELS else 10) else "🔋"
    line = f"{icon} <b>{s['name']}</b>: {s['soc']}%"
    if s.get("stale"):
        return line + f" (нет связи, данные от {s['stale']})"
    if s["charging"]:
        line += f" · ⚡ заряжается +{s['in']} Вт"
        if s["out"] > 5:
            line += f", нагрузка {s['out']} Вт"
        if s.get("remain"):
            line += f" · до полной {dur(timedelta(minutes=s['remain']))}"
    elif s["out"] > 5:
        line += f" · нагрузка {s['out']} Вт"
        if s.get("remain"):
            line += f" · хватит на ~{dur(timedelta(minutes=s['remain']))}"
    else:
        line += " · ожидание"
    if s.get("load"):
        line += f" · потребление {s['load']} Вт"
    if s.get("age_min", 0) >= 10:
        line += f" <i>(данные {dur(timedelta(minutes=s['age_min']))} назад)</i>"
    return line


def format_stations():
    st, errs = get_stations()
    lines = [format_station(s) for s in st] + [f"⚠️ {e}" for e in errs]
    return "\n".join(lines) if lines else "Станции не настроены"


MODE_MIN_W = 30   # below this the battery counts as idle


def station_mode_change(modes, s):
    """Notify when a station starts/stops charging or discharging (seen 2 checks in a row)."""
    n, soc = s["name"], s["soc"]
    if s["in"] >= MODE_MIN_W and s["in"] > s["out"]:
        mode = "chg"
    elif s["out"] >= MODE_MIN_W:
        mode = "dsg"
    else:
        mode = "idle"
    m = modes.get(n)
    if m is None:
        modes[n] = {"mode": mode, "cand": None}
        return
    if mode == m["mode"]:
        m["cand"] = None
        return
    if m.get("cand") != mode:              # first sighting: wait one more check
        m["cand"] = mode
        return
    old, m["mode"], m["cand"] = m["mode"], mode, None
    rem = s.get("remain")
    if mode == "chg":
        extra = f" · до полной ~{dur(timedelta(minutes=rem))}" if rem else ""
        send(f"⚡ <b>{n}</b> начала заряжаться: +{s['in']} Вт, {soc}%{extra}")
    elif mode == "dsg":
        extra = f" · хватит на ~{dur(timedelta(minutes=rem))}" if rem else ""
        send(f"🔋 <b>{n}</b> разряжается: −{s['out']} Вт, {soc}%{extra}")
    elif old == "chg":
        if soc < 99:                       # at 100% the "заряжена до 100%" message covers it
            send(f"⏸ <b>{n}</b> перестала заряжаться ({soc}%)")
    else:
        send(f"⏸ <b>{n}</b> перестала разряжаться ({soc}%)")


def check_stations(state):
    if not stations_configured():
        return
    st, errs = get_stations()
    if errs and not st:
        raise RuntimeError("; ".join(errs))
    # snapshot for the Cloudflare worker's /power (rounded so state.json changes rarely)
    state["stations"] = [{"name": s["name"], "brand": s.get("brand"), "soc": s["soc"],
                          "in": round(s["in"] / 10) * 10, "out": round(s["out"] / 10) * 10,
                          "charging": s["charging"], "stale": s.get("stale"),
                          "remain": round(s["remain"] / 10) * 10 if s.get("remain") else None}
                         for s in st]
    alerts = state.setdefault("st_alert", {})
    full = state.setdefault("st_full", {})
    modes = state.setdefault("st_mode", {})
    for s in st:
        if s.get("stale"):
            continue
        n, soc = s["name"], s["soc"]
        first_time = n not in full
        station_mode_change(modes, s)
        # low charge: alert once per level, reset after recharging above top level + 5%
        crossed = [lv for lv in LOW_LEVELS if soc <= lv]
        last = alerts.get(n)
        if crossed and (last is None or min(crossed) < last):
            if not first_time:
                rem = f"\nХватит примерно на {dur(timedelta(minutes=s['remain']))}" \
                    if s.get("remain") and not s["charging"] else ""
                send(f"🪫 <b>{n}</b>: заряд упал до {soc}%{rem}")
            alerts[n] = min(crossed)
        elif LOW_LEVELS and soc > LOW_LEVELS[0] + 5:
            alerts.pop(n, None)
        # full charge
        if soc >= 100 and not full.get(n):
            if not first_time:
                send(f"🔋 <b>{n}</b> заряжена до 100%")
            full[n] = True
        elif soc < 95 or first_time:
            full[n] = soc >= 100


# ---------------------------------------------------------------- home power
def home_online():
    """HOME_PORT=0 -> ICMP ping (works on a VPS), otherwise a TCP connect."""
    import subprocess
    for _ in range(3):
        if HOME_PORT == 0:
            r = subprocess.run(["ping", "-c", "1", "-W", "3", HOME_HOST],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0:
                return True
        else:
            try:
                with socket.create_connection((HOME_HOST, HOME_PORT), timeout=6):
                    return True
            except OSError:
                pass
        time.sleep(4)
    return False


def grid_online():
    """True/False = is there power at home; None = can't tell."""
    if POWER_SOURCE == "ecoflow":
        st, _ = get_stations()
        flags = [s["grid"] for s in st if s.get("brand") == "ecoflow" and s.get("grid") is not None]
        return any(flags) if flags else None
    if POWER_SOURCE == "router" and HOME_HOST:
        return home_online()
    return None


def scheduled_window(state, t, slack=15):
    """Planned outage (start, end) that covers moment t (± slack minutes), if any."""
    g = state.get("last_schedule")
    if not g:
        return None
    pad = timedelta(minutes=slack)
    for st, en in absolute_events(g):
        if st - pad <= t <= en + pad:
            return st, en
    return None


def check_expected(state):
    """15-45 min after a planned change, warn if reality doesn't match the schedule."""
    h, g = state.get("home"), state.get("last_schedule")
    if not POWER_SOURCE or not h or not g:
        return
    t = now()
    done = set(state.get("checked", []))
    for st, en in absolute_events(g):
        for kind, moment in (("off", st), ("on", en)):
            key = f"{kind}:{moment.isoformat()}"
            ago = (t - moment).total_seconds() / 60
            if key in done or not 15 <= ago <= 45:
                continue
            done.add(key)
            if kind == "off" and h["online"]:
                send(f"🤔 По графику свет должны были отключить в {moment:%H:%M}, но он есть")
            elif kind == "on" and not h["online"]:
                send(f"⏳ По графику свет должны были дать в {moment:%H:%M}, но его пока нет")
    cutoff = (t - timedelta(days=2)).isoformat()
    state["checked"] = sorted(k for k in done if k.split(":", 1)[1] >= cutoff)


def check_home(state):
    if not POWER_SOURCE:
        return
    online = grid_online()
    if online is None:
        raise RuntimeError(f"не удалось определить наличие света (POWER_SOURCE={POWER_SOURCE})")
    t = now()
    prev = state.get("home")
    if prev is None:
        state["home"] = {"online": online, "since": t.isoformat()}
        send(f"🏠 Мониторинг дома запущен: сейчас свет {'есть ✅' if online else 'не определяется ❌'}")
        return
    if prev["online"] == online:
        return
    since = datetime.fromisoformat(prev["since"])
    ev = scheduled_window(state, t)
    if online:
        note = ""
        if ev and t < ev[1] - timedelta(minutes=15):
            note = f"\nРаньше графика (по графику до {ev[1]:%H:%M})"
        send(f"💡 <b>Свет появился</b> в {t:%H:%M}\nНе было: {dur(t - since)}{note}")
    else:
        note = (f"По графику: {ev[0]:%H:%M}–{ev[1]:%H:%M}" if ev
                else "⚠️ Не по графику — возможно, аварийное отключение")
        msg = f"🔌 <b>Свет пропал</b> около {t:%H:%M}\nБыл: {dur(t - since)}\n{note}"
        if stations_configured():
            msg += "\n\n" + format_stations()
        send(msg)
    state["home"] = {"online": online, "since": t.isoformat()}


# ---------------------------------------------------------------- air raid alerts
ALERTS_TOKEN = os.environ.get("ALERTS_TOKEN", "").strip()
ALERT_UID = os.environ.get("ALERT_UID") or "31"          # 31 = м. Київ (alerts.in.ua)
ALERT_NAME = os.environ.get("ALERT_NAME") or "м. Київ"


def check_alerts(state):
    """alerts.in.ua IoT endpoint: "A" = alert, "P" = partial, "N" = no alert."""
    if not ALERTS_TOKEN:
        return
    url = (f"https://api.alerts.in.ua/v1/iot/active_air_raid_alerts/{ALERT_UID}.json?"
           + urllib.parse.urlencode({"token": ALERTS_TOKEN}))
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001  (don't leak the token in the URL)
        raise RuntimeError(f"alerts.in.ua недоступен ({type(e).__name__})") from None
    active = str(raw).strip().upper() in ("A", "P")
    t = now()
    prev = state.get("alert")
    if prev is None:
        state["alert"] = {"active": active, "since": t.isoformat()}
        return
    if prev["active"] == active:
        return
    since = datetime.fromisoformat(prev["since"])
    if active:
        send(f"🚨 <b>Повітряна тривога</b> — {ALERT_NAME}\nПочаток: {t:%H:%M}. Пройдіть в укриття!")
    else:
        send(f"✅ <b>Відбій тривоги</b> — {ALERT_NAME}\nТривала: {dur(t - since)}")
    state["alert"] = {"active": active, "since": t.isoformat()}


# ---------------------------------------------------------------- commands
HELP = ("Команды:\n/graf — график отключений\n/status — есть ли сейчас свет дома\n"
        "/power — заряд станций")


def command_reply(state, text):
    """Text of the reply to a command, or None if it isn't ours."""
    global _stations_cache
    cmd = (text or "").strip().split()[0].split("@")[0].lower() if (text or "").strip() else ""
    if cmd in ("/graf", "/schedule"):
        try:
            g = fetch_schedule()
        except Exception:  # noqa: BLE001
            g = state.get("last_schedule")
        return format_schedule(g) if g else "Графика пока нет"
    if cmd == "/power":
        _stations_cache = None          # fresh data on demand
        return "🔋 <b>Станции</b>\n" + format_stations()
    if cmd == "/status":
        h = state.get("home")
        if not h:
            return "Мониторинг света дома пока не настроен"
        since = datetime.fromisoformat(h["since"])
        return (f"{'💡 Свет есть' if h['online'] else '🔌 Света нет'} "
                f"уже {dur(now() - since)} (с {since:%d.%m %H:%M})")
    if cmd == "/marstek":
        _stations_cache = None
        get_stations()
        if not _ms_raw:
            return "Нет данных Marstek"
        body = json.dumps(_ms_raw, ensure_ascii=False, indent=1)[:3500]
        return "<b>Данные Marstek из облака</b>\n<pre>" + body.replace("<", "&lt;") + "</pre>"
    if cmd in ("/start", "/help"):
        return HELP
    return None


def handle_commands(state):
    """One-shot mode: reply to commands received since the last run."""
    if not TG_TOKEN or DRY_RUN:
        return
    offset = state.get("tg_offset", 0)
    try:
        upd = http_json(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?"
                        + urllib.parse.urlencode({"offset": offset, "timeout": 0}))
    except Exception as e:  # noqa: BLE001
        print(f"getUpdates failed: {e}", file=sys.stderr)
        return
    for u in upd.get("result", []):
        state["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) == str(CHAT_ID):
            reply = command_reply(state, msg.get("text"))
            if reply:
                send(reply, reply_to=msg.get("message_id"))


# ---------------------------------------------------------------- checks
def run_checks(state, steps):
    flush_outbox(state)
    errors = []
    for step in steps:
        try:
            step(state)
        except Exception as e:  # noqa: BLE001
            print(f"{step.__name__}: {e}", file=sys.stderr)
            errors.append(f"{step.__name__}: {e}")
    # warn only about problems that last 15+ minutes, at most once per 6 hours
    if not errors:
        state.pop("err_since", None)
    else:
        since = datetime.fromisoformat(state.setdefault("err_since", now().isoformat()))
        last = state.get("last_error_notice")
        if now() - since >= timedelta(minutes=15) and (
                not last or now() - datetime.fromisoformat(last) > timedelta(hours=6)):
            send("⚠️ Ошибка бота:\n" + "\n".join(errors))
            state["last_error_notice"] = now().isoformat()
    if _OUTBOX:
        state["outbox"] = _OUTBOX[:]
        _OUTBOX.clear()


def main():
    """One-shot mode (GitHub Actions): run all checks once."""
    if not QUEUE:
        sys.exit("Укажи QUEUE (например 3.1)")
    if not DRY_RUN and not (TG_TOKEN and CHAT_ID):
        print("TG_TOKEN / TG_CHAT_ID ещё не заданы — пропускаю запуск")
        return
    state = load_state()
    run_checks(state, (check_schedule, check_home, check_expected, check_stations, check_alerts))
    save_state(state)


# ---------------------------------------------------------------- server mode
STATION_EVERY = int(os.environ.get("STATION_EVERY_MIN") or 2)


def serve():
    """Always-on mode (VPS): checks every minute, instant replies to commands."""
    import threading
    global _stations_cache
    if not (QUEUE and TG_TOKEN and CHAT_ID):
        sys.exit("Нужны QUEUE, TG_TOKEN и TG_CHAT_ID")
    api = f"https://api.telegram.org/bot{TG_TOKEN}"
    http_json(f"{api}/deleteWebhook", {"drop_pending_updates": False})
    http_json(f"{api}/setMyCommands", {"commands": [
        {"command": "graf", "description": "График отключений"},
        {"command": "status", "description": "Есть ли сейчас свет дома"},
        {"command": "power", "description": "Заряд станций"}]})
    lock = threading.Lock()
    state = load_state()
    print("svitlo-bot: server mode started", flush=True)

    def checker():
        last_st = None
        while True:
            t = now()
            steps = [check_schedule, check_home, check_expected, check_alerts]
            if last_st is None or t - last_st >= timedelta(minutes=STATION_EVERY):
                steps.append(check_stations)
                last_st = t
            try:
                with lock:
                    global _stations_cache
                    _stations_cache = None
                    run_checks(state, steps)
                    save_state(state)
            except Exception as e:  # noqa: BLE001  (never let the checker die)
                print(f"checker error: {e}", file=sys.stderr, flush=True)
            time.sleep(max(5, 60 - datetime.now().second))

    worker = threading.Thread(target=checker, daemon=True)
    worker.start()

    offset = 0
    while True:
        if not worker.is_alive():          # let systemd restart the whole bot
            sys.exit("checker thread stopped")
        try:
            upd = http_json(f"{api}/getUpdates?" + urllib.parse.urlencode(
                {"offset": offset, "timeout": 50, "allowed_updates": '["message"]'}), timeout=60)
        except Exception as e:  # noqa: BLE001
            print(f"getUpdates failed: {e}", file=sys.stderr, flush=True)
            time.sleep(5)
            continue
        for u in upd.get("result", []):
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                continue
            with lock:
                try:
                    reply = command_reply(state, msg.get("text"))
                except Exception as e:  # noqa: BLE001
                    reply = f"⚠️ Не получилось: {e}"
            if reply:
                send(reply, reply_to=msg.get("message_id"))


if __name__ == "__main__":
    serve() if "--serve" in sys.argv else main()
