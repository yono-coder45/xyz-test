"""Helper bersama: konfigurasi, state JSON (lock + atomic + backup), Discord, meme."""
import os, json, shutil, fcntl, random, contextlib, importlib.util
from datetime import datetime, timezone, timedelta
import requests

UTC = timezone.utc
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "cool_down.json")
LOCK_PATH = STATE_PATH + ".lock"
MEMES_PATH = os.path.join(HERE, "memes.json")


def env(key, default, cast=str):
    v = os.environ.get(key)
    return cast(v) if v not in (None, "") else cast(default)


MODE = env("MODE", "normal").lower()
GEM = MODE == "gem"
MAX_PAIRS = env("MAX_PAIRS_GEM", 30, int) if GEM else env("MAX_PAIRS_NORMAL", 40, int)
VOL_MIN = env("MIN_VOLUME_GEM", 500000, float) if GEM else env("MIN_VOLUME_NORMAL", 5000000, float)
VOL_MAX = env("MAX_VOLUME_GEM", 5000000, float) if GEM else float("inf")
SPREAD_MAX = env("MAX_SPREAD_GEM", 0.50, float) if GEM else env("MAX_SPREAD_NORMAL", 0.10, float)
RISK_PCT = env("RISK_GEM", 0.5, float) if GEM else env("RISK_NORMAL", 1.0, float)
MAX_POS_PCT = env("MAX_POSITION_GEM", 1, float) if GEM else env("MAX_POSITION_NORMAL", 25, float)
CAPITAL = env("CAPITAL_USD", 1000, float)
WINDOW_H = env("COOLDOWN_HOURS", 6, float)
WINDOW_TOL_MIN = env("WINDOW_TOLERANCE_MIN", 30, float)
EVAL_MAX_DAYS = env("EVALUATED_MAX_AGE_DAYS", 7, float)
RR_MIN = env("RR_MIN", 1.5, float)
MAX_SIG = env("MAX_SIGNAL_PER_RUN", 5, int)
MAX_CAND = env("MAX_CANDIDATE_PER_RUN", 5, int)
SLIPPAGE = env("SLIPPAGE", 0.001, float)
FEE_RT = env("FEE_ROUNDTRIP", 0.002, float)
MAX_WORKERS = env("MAX_PARALLEL_WORKERS", 10, int)


def now_utc():
    return datetime.now(UTC)


def to_iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def load_module(filename, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def empty_state():
    return {
        "schema_version": 1, "updated_at": None, "run_id": None, "mode": MODE,
        "btc_4h_status": None, "pionex_source_used": None, "pairs_count": 0,
        "prune_policy": {"evaluated_max_age_days": EVAL_MAX_DAYS},
        "cooldown": {}, "pending": [], "evaluated": [], "last_batch": [],
    }


@contextlib.contextmanager
def state_lock():
    with open(LOCK_PATH, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def load_state():
    if not os.path.exists(STATE_PATH) and not os.path.exists(STATE_PATH + ".bak"):
        return empty_state()
    for p in (STATE_PATH, STATE_PATH + ".bak"):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[state] gagal baca {p}: {e}")
    raise SystemExit("cool_down.json dan .bak korup -> abort fase")


def save_state(st):
    st["updated_at"] = to_iso(now_utc())
    if os.path.exists(STATE_PATH):
        shutil.copyfile(STATE_PATH, STATE_PATH + ".bak")
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_PATH)


COLORS = {"blue": 0x3498DB, "green": 0x2ECC71, "red": 0xE74C3C,
          "yellow": 0xF1C40F, "grey": 0x95A5A6, "purple": 0x9B59B6}


def meme(key):
    try:
        with open(MEMES_PATH, encoding="utf-8") as f:
            pool = json.load(f).get(key, [])
    except Exception:
        pool = []
    return random.choice(pool) if pool else None


def make_embed(title, desc="", color="blue", meme_key=None, fields=None):
    e = {
        "title": title[:250],
        "description": desc[:3500],
        "color": COLORS[color],
        "timestamp": to_iso(now_utc()),
        "footer": {"text": f"Bot Scanner Multi-Timeframe • {MODE.upper()}"},
    }
    if fields:
        e["fields"] = [{"name": str(n)[:250], "value": (str(v)[:1000] or "-"), "inline": False}
                       for n, v in fields[:20]]
    url = meme(meme_key) if meme_key else None
    if url:
        e["image"] = {"url": url}
    return e


def _embed_size(e):
    s = len(e.get("title", "")) + len(e.get("description", ""))
    s += sum(len(f["name"]) + len(f["value"]) for f in e.get("fields", []))
    return s


def discord_send(embeds):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        print("[discord] DISCORD_WEBHOOK_URL kosong, skip kirim")
        return False
    batches, cur, size = [], [], 0
    for e in embeds:
        s = _embed_size(e)
        if cur and (len(cur) == 10 or size + s > 5500):
            batches.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += s
    if cur:
        batches.append(cur)
    ok = True
    for b in batches:
        try:
            r = requests.post(url, json={"embeds": b}, timeout=15)
            if r.status_code not in (200, 204):
                ok = False
                print(f"[discord] HTTP {r.status_code}: {r.text[:200]}")
        except requests.RequestException as e:
            ok = False
            print(f"[discord] error: {e}")
    return ok
