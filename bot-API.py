"""Fase 1 — tarik data Binance: session reuse, bookTicker sekali, candle bertahap 4h→1h→15m."""
import time
import requests
from concurrent.futures import ThreadPoolExecutor
from shared import env, MAX_WORKERS

BINANCE_HOSTS = [h.strip() for h in env(
    "BINANCE_HOSTS",
    "https://api.binance.com,https://api1.binance.com,https://api2.binance.com,"
    "https://api3.binance.com,https://data-api.binance.vision").split(",")]
TIMEOUT = env("BINANCE_TIMEOUT", 8, float)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "scouting-bot/1.0"})
_good_host = [None]


def _get(path, params=None):
    hosts = BINANCE_HOSTS
    if _good_host[0]:
        hosts = [_good_host[0]] + [h for h in BINANCE_HOSTS if h != _good_host[0]]
    last = "unknown"
    for host in hosts:
        try:
            r = SESSION.get(host + path, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                _good_host[0] = host
                return r.json()
            last = f"{host} HTTP {r.status_code}"
        except requests.RequestException as e:
            last = f"{host} {e}"
    raise RuntimeError(f"Binance semua host gagal ({last})")


def fetch_tickers():
    out = {}
    for t in _get("/api/v3/ticker/24hr"):
        s = t["symbol"]
        if s.endswith("USDT"):
            out[s] = {"quote_vol": float(t["quoteVolume"]),
                      "change_pct": float(t["priceChangePercent"]),
                      "last": float(t["lastPrice"])}
    return out


def fetch_book_spreads():
    """Satu request untuk semua pair (pengganti depth per symbol)."""
    out = {}
    for b in _get("/api/v3/ticker/bookTicker"):
        s = b["symbol"]
        if not s.endswith("USDT"):
            continue
        bid, ask = float(b["bidPrice"]), float(b["askPrice"])
        if bid > 0 and ask > 0:
            out[s] = (ask - bid) / ((ask + bid) / 2) * 100
    return out


def fetch_spread_pct(symbol):
    """Cadangan: spread per koin dari depth (dipakai jika bookTicker gagal)."""
    d = _get("/api/v3/depth", {"symbol": symbol, "limit": 5})
    bid, ask = float(d["bids"][0][0]), float(d["asks"][0][0])
    return (ask - bid) / ((ask + bid) / 2) * 100


def fetch_klines(symbol, interval, limit=300, start_ms=None, end_ms=None):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_ms:
        params["startTime"] = start_ms
    if end_ms:
        params["endTime"] = end_ms
    rows = _get("/api/v3/klines", params)
    return [{"open_time": r[0], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
             "c": float(r[4]), "v": float(r[5]), "close_time": r[6],
             "taker_buy": float(r[9])} for r in rows]


def closed_only(candles):
    now_ms = int(time.time() * 1000)
    return [c for c in candles if c["close_time"] < now_ms]


def fetch_btc_4h():
    return closed_only(fetch_klines("BTCUSDT", "4h", 250))


HISTORY = env("KLINE_HISTORY", 250, int)   # riwayat per timeframe (pemanasan EMA/MACD/BB; bobot request tetap)
MIN_CANDLES = env("MIN_CANDLES", 80, int)  # di bawah ini indikator belum stabil -> koin dilewati


def spreads_for(symbols, spreads):
    """{symbol: spread%} dari bookTicker; hanya yang tidak ada di sana diambil via depth."""
    out = {s: spreads[s] for s in symbols if s in spreads}
    missing = [s for s in symbols if s not in spreads]
    if missing:
        def one(sym):
            try:
                return sym, fetch_spread_pct(sym)
            except Exception as e:
                print(f"[API] spread {sym} gagal: {e}")
                return sym, None
        with ThreadPoolExecutor(MAX_WORKERS) as ex:
            out.update({s: v for s, v in ex.map(one, missing) if v is not None})
    return out


def stage(symbols, interval):
    """Tarik candle TERTUTUP satu timeframe untuk banyak simbol paralel.
    Dipanggil bertahap (4h -> 1h -> 15m) hanya untuk simbol yang lolos tahap sebelumnya,
    jadi request tidak terbuang untuk koin yang sudah pasti gugur.
    Return {symbol: candles}, hanya yang datanya cukup (>= MIN_CANDLES)."""
    def one(sym):
        try:
            cs = closed_only(fetch_klines(sym, interval, HISTORY))
            return sym, (cs if len(cs) >= MIN_CANDLES else None)
        except Exception as e:
            print(f"[API] skip {sym} {interval}: {e}")
            return sym, None

    if not symbols:
        return {}
    with ThreadPoolExecutor(MAX_WORKERS) as ex:
        return {s: c for s, c in ex.map(one, symbols) if c}
