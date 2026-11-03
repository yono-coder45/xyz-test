"""Fase 2 — buang stablecoin & token leverage, filter volume, irisan wajib dengan Pionex."""
import time
import requests
from shared import env, VOL_MIN, VOL_MAX, MAX_PAIRS

PIONEX_HOSTS = list(dict.fromkeys([
    env("PIONEX_API_HOST_PRIMARY", "https://api.pionex.com"),
    env("PIONEX_API_HOST_SECONDARY", "https://api.pionex.com"),
]))
RETRY = env("PIONEX_FALLBACK_RETRY", 2, int)
BACKOFF = env("PIONEX_FALLBACK_BACKOFF_MS", 500, int) / 1000
CG_KEY = env("COINGECKO_API_KEY", "")
STABLE = {"USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI", "USDP", "USDD", "PYUSD", "USDE",
          "EUR", "TRY", "BRL"}
LEVERAGED = ("UP", "DOWN", "BULL", "BEAR")


class PionexDown(Exception):
    pass


def _pionex_symbols(host):
    r = requests.get(f"{host}/api/v1/common/symbols", timeout=10)
    r.raise_for_status()
    syms = r.json()["data"]["symbols"]
    return {s["symbol"].split("_")[0] for s in syms if s["symbol"].endswith("_USDT")}


def _coingecko_pionex():
    headers = {"x-cg-demo-api-key": CG_KEY} if CG_KEY else {}
    bases = set()
    for page in range(1, 6):
        r = requests.get("https://api.coingecko.com/api/v3/exchanges/pionex/tickers",
                         params={"page": page}, headers=headers, timeout=15)
        r.raise_for_status()
        tks = r.json().get("tickers", [])
        if not tks:
            break
        bases |= {t["base"] for t in tks if t.get("target") == "USDT"}
    return bases


def fetch_pionex_bases():
    chain = [(f"pionex_api_{i + 1}", (lambda h=h: _pionex_symbols(h)))
             for i, h in enumerate(PIONEX_HOSTS)]
    chain.append(("coingecko", _coingecko_pionex))
    errors = []
    for name, fn in chain:
        for _ in range(RETRY):
            try:
                bases = fn()
                if bases:
                    return name, bases
                errors.append(f"{name}: kosong")
            except Exception as e:
                errors.append(f"{name}: {e}")
            time.sleep(BACKOFF)
    raise PionexDown("; ".join(errors)[:900])


def bad_base(base):
    return base in STABLE or base.endswith(LEVERAGED)


def select_pairs(tickers, bases):
    rows = []
    for pair, t in tickers.items():
        base = pair[:-4]
        if base in bases and not bad_base(base) and VOL_MIN <= t["quote_vol"] <= VOL_MAX:
            rows.append((t["quote_vol"], pair))
    rows.sort(reverse=True)
    return [p for _, p in rows[:MAX_PAIRS]]


def run(tickers):
    source, bases = fetch_pionex_bases()
    pairs = select_pairs(tickers, bases)
    print(f"[filter] sumber={source} pair={len(pairs)}")
    return source, pairs
