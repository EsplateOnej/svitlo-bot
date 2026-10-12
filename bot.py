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
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

TG_TOKEN = os.environ.get("TG_TOKEN", "")
CHAT_ID = os.environ.get("TG_CHAT_ID", "")
QUEUE = os.environ.get("QUEUE", "").strip()
HOME_HOST = os.environ.get("HOME_HOST", "").strip()
HOME_PORT = int(os.environ.get("HOME_PORT") or 80)
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


def send(text):
    if DRY_RUN or not TG_TOKEN:
        print("---- MESSAGE ----\n" + text + "\n-----------------")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            http_json(url, {"chat_id": CHAT_ID, "text": text,
                            "parse_mode": "HTML", "disable_web_page_preview": True})
            return
        except Exception as e:  # noqa: BLE001
            print(f"send failed ({attempt + 1}/3): {e}", file=sys.stderr)
            time.sleep(3)


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


def check_schedule(state):
    g = fetch_schedule()
    msgs = []

    # 1) schedule published / changed
    fp = schedule_fingerprint(g)
    if state.get("schedule_fp") != fp:
        prefix = "🔄 График изменился!\n\n" if state.get("schedule_fp") else ""
        msgs.append(prefix + format_schedule(g))
        state["schedule_fp"] = fp

    # 2) reminders before off / on
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
    # keep only recent reminder keys
    cutoff = (t - timedelta(days=2)).isoformat()
    state["reminded"] = sorted(k for k in sent if k.split(":", 1)[1] >= cutoff)
    state["last_schedule"] = {k: g.get(k) for k in ("today", "tomorrow")}
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
LOW_LEVELS = sorted({int(x) for x in (os.environ.get("LOW_LEVELS") or "20,10").split(",") if x.strip()},
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


def marstek_stations():
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
    resp = http_json(f"{MS_HOST}/ems/api/v1/getDeviceList?" + urllib.parse.urlencode({"token": token}))
    devices = [d for d in (resp.get("data") or []) if isinstance(d, dict) and "soc" in d]
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
        rt = d.get("report_time")
        try:
            rt = float(rt)
            if rt > 1e9 and time.time() - rt > 30 * 60:
                item["stale"] = datetime.fromtimestamp(rt, TZ).strftime("%d.%m %H:%M")
        except (TypeError, ValueError):
            pass
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
    return line


def format_stations():
    st, errs = get_stations()
    lines = [format_station(s) for s in st] + [f"⚠️ {e}" for e in errs]
    return "\n".join(lines) if lines else "Станции не настроены"


def check_stations(state):
    if not stations_configured():
        return
    st, errs = get_stations()
    if errs and not st:
        raise RuntimeError("; ".join(errs))
    alerts = state.setdefault("st_alert", {})
    full = state.setdefault("st_full", {})
    for s in st:
        if s.get("stale"):
            continue
        n, soc = s["name"], s["soc"]
        first_time = n not in full
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
    for _ in range(3):
        try:
            with socket.create_connection((HOME_HOST, HOME_PORT), timeout=6):
                return True
        except OSError:
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
    if online:
        send(f"💡 <b>Свет появился</b> в {t:%H:%M}\nНе было: {dur(t - since)}")
    else:
        msg = f"🔌 <b>Свет пропал</b> около {t:%H:%M}\nБыл: {dur(t - since)}"
        if stations_configured():
            msg += "\n\n" + format_stations()
        send(msg)
    state["home"] = {"online": online, "since": t.isoformat()}


# ---------------------------------------------------------------- commands
HELP = ("Команды:\n/graf — график отключений\n/status — есть ли сейчас свет дома\n"
        "/power — заряд станций")


def handle_commands(state):
    """Reply to commands (answered on the next run, up to ~5-15 min delay)."""
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
        if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
            continue
        text = (msg.get("text") or "").split("@")[0].strip().lower()
        if text in ("/graf", "/schedule"):
            g = state.get("last_schedule")
            send(format_schedule(g) if g else "Графика пока нет")
        elif text == "/power":
            send("🔋 <b>Станции</b>\n" + format_stations())
        elif text == "/status":
            h = state.get("home")
            if not h:
                send("Мониторинг света дома не настроен")
            else:
                since = datetime.fromisoformat(h["since"])
                send(f"{'💡 Свет есть' if h['online'] else '🔌 Света нет'} "
                     f"уже {dur(now() - since)} (с {since:%d.%m %H:%M})")
        elif text in ("/start", "/help"):
            send(HELP)


# ---------------------------------------------------------------- main
def main():
    if not QUEUE:
        sys.exit("Укажи QUEUE (например 3.1)")
    if not DRY_RUN and not (TG_TOKEN and CHAT_ID):
        print("TG_TOKEN / TG_CHAT_ID ещё не заданы — пропускаю запуск")
        return
    state = load_state()
    errors = []
    for step in (handle_commands, check_schedule, check_home, check_stations):
        try:
            step(state)
        except Exception as e:  # noqa: BLE001
            print(f"{step.__name__}: {e}", file=sys.stderr)
            errors.append(f"{step.__name__}: {e}")
    # warn about problems at most once per 6 hours
    if errors:
        last = state.get("last_error_notice")
        if not last or now() - datetime.fromisoformat(last) > timedelta(hours=6):
            send("⚠️ Ошибка бота:\n" + "\n".join(errors))
            state["last_error_notice"] = now().isoformat()
    save_state(state)


if __name__ == "__main__":
    main()
