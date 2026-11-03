"""Fase 1–3 — API → filter Pionex → analisis multi-timeframe (4H/1H/15M) → simpan ke cool_down.json.

Skema: Bias 4H (4H Bearish = veto) → Momentum 1H (wajib Bullish) → Trigger 15M → matriks keputusan
→ filter risiko. Hanya sinyal BULLISH yang dikirim (Pionex spot). Semua candle sudah tertutup.
Data diambil bertahap (4H → 1H → 15M) dan hanya untuk koin yang lolos tahap sebelumnya.
"""
import time
import uuid
import statistics
from datetime import timedelta
from shared import (SPREAD_MAX, RR_MIN, MAX_SIG, MAX_CAND, SLIPPAGE, FEE_RT, RISK_PCT,
                    MAX_POS_PCT, CAPITAL, WINDOW_H, MODE, state_lock, load_state, save_state,
                    now_utc, to_iso, from_iso, make_embed, discord_send, load_module)

api = load_module("bot-API.py", "bot_api")
flt = load_module("bot-filter-pionex.py", "bot_filter")

# ── Parameter skema (ubah di sini jika perlu) ─────────────────────────────────
TRIG_WINDOW = 3                        # 3 kejadian trigger 15M boleh terjadi dalam N candle terakhir
BB_MID_LOW, BB_MID_HIGH = 0.40, 0.60   # %B 1H di zona ini = "tengah band" → sinyal dilemahkan
ATR_SL, ATR_TP = 1.5, 3.0              # SL/TP berbasis ATR(14) 15m
RES_MIN_R = 1.5                        # resistance terdekat harus > 1.5R dari entry, kalau tidak → tunggu


# ── Indikator (dihitung penuh sekali per timeframe) ───────────────────────────
def ema(vals, p):
    k = 2 / (p + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def sma_series(vals, p):
    out, s = [None] * len(vals), 0.0
    for i, x in enumerate(vals):
        s += x
        if i >= p:
            s -= vals[i - p]
        if i >= p - 1:
            out[i] = s / p
    return out


def bollinger(closes, p=20, dev=2):
    w = closes[-p:]
    m = sum(w) / p
    sd = statistics.pstdev(w)
    return m, m - dev * sd, m + dev * sd


def _rsi(ag, al):
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def rsi_series(closes, p=14):
    """RSI Wilder."""
    out = [None] * len(closes)
    if len(closes) <= p:
        return out
    gains, losses = [0.0], [0.0]
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[1:p + 1]) / p
    al = sum(losses[1:p + 1]) / p
    out[p] = _rsi(ag, al)
    for i in range(p + 1, len(closes)):
        ag = (ag * (p - 1) + gains[i]) / p
        al = (al * (p - 1) + losses[i]) / p
        out[i] = _rsi(ag, al)
    return out


def macd_hist_series(closes, fast=12, slow=26, sig=9):
    macd = [a - b for a, b in zip(ema(closes, fast), ema(closes, slow))]
    signal = ema(macd, sig)
    return [m - s for m, s in zip(macd, signal)]


def atr(cs, p=14):
    trs = []
    for i in range(1, len(cs)):
        h, l, pc = cs[i]["h"], cs[i]["l"], cs[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < p:
        return None
    a = sum(trs[:p]) / p
    for t in trs[p:]:
        a = (a * (p - 1) + t) / p
    return a


def pivots(cs, k=3):
    lows, highs = [], []
    for i in range(k, len(cs) - k):
        seg = cs[i - k:i + k + 1]
        if cs[i]["l"] == min(c["l"] for c in seg):
            lows.append(cs[i]["l"])
        if cs[i]["h"] == max(c["h"] for c in seg):
            highs.append(cs[i]["h"])
    return lows, highs


def indicators(cs):
    c = [x["c"] for x in cs]
    return {"c": c, "rsi": rsi_series(c, 14), "hist": macd_hist_series(c),
            "ma5": sma_series(c, 5), "ma20": sma_series(c, 20)}


# ── Langkah skema ─────────────────────────────────────────────────────────────
def bias_4h(ind):
    """Bias 4H dari candle 4H tertutup terakhir."""
    r, h, c, ma20 = ind["rsi"][-1], ind["hist"][-1], ind["c"][-1], ind["ma20"][-1]
    if r > 60 and h > 0 and c > ma20:
        return "Bullish"
    if r < 40 and h < 0 and c < ma20:
        return "Bearish"
    return "Netral"


def momentum_1h(ind):
    """Momentum 1H: close terakhir vs close 4 candle lalu, MA5 vs MA20, RSI vs 50."""
    c, r = ind["c"], ind["rsi"][-1]
    ma5, ma20 = ind["ma5"][-1], ind["ma20"][-1]
    if c[-1] > c[-5] and ma5 > ma20 and r > 50:
        return "Bullish"
    if c[-1] < c[-5] and ma5 < ma20 and r < 50:
        return "Bearish"
    return "Netral"


def trigger_15m(ind):
    """Return (label, indeks_candle_trigger). Tiga kejadian (RSI lewat 50 naik, histogram MACD
    negatif→positif, close menembus MA20 ke atas) harus terjadi dalam TRIG_WINDOW candle
    tertutup terakhir, dan kondisinya masih berlaku di candle terakhir."""
    c, rsi, hist, ma20 = ind["c"], ind["rsi"], ind["hist"], ind["ma20"]
    n, r = len(c), rsi[-1]
    if r > 70:
        return "Overbought", None
    if r < 30:
        return "Oversold", None
    ev = {"rsi": None, "macd": None, "ma": None}
    for j in range(n - TRIG_WINDOW, n):
        if rsi[j - 1] <= 50 < rsi[j]:
            ev["rsi"] = j
        if hist[j - 1] <= 0 < hist[j]:
            ev["macd"] = j
        if c[j - 1] <= ma20[j - 1] and c[j] > ma20[j]:
            ev["ma"] = j
    if None not in ev.values() and r > 50 and hist[-1] > 0 and c[-1] > ma20[-1]:
        return "Bullish", max(ev.values())
    return "Netral", None


def vol_ratio(cs, i):
    """Volume candle i dibanding rata-rata 20 candle sebelumnya."""
    base = [x["v"] for x in cs[max(0, i - 20):i]]
    avg = sum(base) / len(base) if base else 0
    return cs[i]["v"] / avg if avg else 1.0


def decide(b4, m1, t15, rsi_1h, vol_weak, bb_mid):
    """Matriks keputusan + filter risiko (bullish saja). 1H wajib Bullish.
    Return (type, kesimpulan) atau None (tunggu / no trade / bearish → tidak dikirim)."""
    if b4 == "Bearish" or m1 != "Bullish" or t15 not in ("Bullish", "Overbought"):
        return None
    hot = t15 == "Overbought" or rsi_1h > 70      # jangan entry baru tanpa pullback
    if b4 == "Bullish":
        if hot:
            return "candidate", "Bullish, tetapi tunggu pullback"
        if vol_weak or bb_mid:                     # volume rendah / harga di tengah band
            return "candidate", "Potensi bullish (sinyal lemah, butuh konfirmasi)"
        return "signal", "Sinyal bullish kuat"
    if hot or t15 != "Bullish":
        return None
    return "candidate", "Potensi bullish (4H belum searah), butuh konfirmasi"


def levels(k1h, k15, price):
    """Entry/SL/TP (ATR 15m) + support/resistance (pivot 1H). None jika tidak layak."""
    a = atr(k15, 14)
    if not a:
        return None
    entry = price * (1 + SLIPPAGE)
    sl, tp = entry - ATR_SL * a, entry + ATR_TP * a
    sl_pct, tp_pct = (entry - sl) / entry * 100, (tp - entry) / entry * 100
    rr = (tp_pct - FEE_RT * 100) / (sl_pct + FEE_RT * 100)
    if rr < RR_MIN:
        return None
    lows, highs = pivots(k1h, 3)
    sup = max([x for x in lows if x < price], default=None)
    res = min([x for x in highs if x > price], default=None)
    if res is not None and res - entry < RES_MIN_R * (entry - sl):
        return None                                  # ruang naik sempit → tunggu
    pos = min(CAPITAL * RISK_PCT / 100 / (sl_pct / 100), CAPITAL * MAX_POS_PCT / 100)
    return {"entry": entry, "sl": sl, "tp": tp, "rr": rr, "pos": pos, "sup": sup, "res": res}


def scan(pairs, spreads):
    """Tiga tahap bertahap. Return (hasil, statistik_gugur, candle_4h_per_koin)."""
    st = {"pairs": len(pairs)}
    sp = api.spreads_for(pairs, spreads)
    pairs = [p for p in pairs if sp.get(p) is not None and sp[p] <= SPREAD_MAX]
    st["lolos_spread"] = len(pairs)

    # Tahap 1 — 4H: Bearish gugur (veto), Bullish/Netral lanjut
    d4 = api.stage(pairs, "4h")
    bias = {}
    for sym, cs in d4.items():
        b = bias_4h(indicators(cs))
        if b != "Bearish":
            bias[sym] = b
    st["lolos_4h"] = len(bias)

    # Tahap 2 — 1H: wajib Bullish
    mom, d1 = {}, api.stage(list(bias), "1h")
    for sym, cs in d1.items():
        ind = indicators(cs)
        if momentum_1h(ind) == "Bullish":
            lo, up = bollinger(ind["c"], 20, 2)[1:]
            pb = (ind["c"][-1] - lo) / (up - lo) if up > lo else 0.5
            mom[sym] = (ind["rsi"][-1], BB_MID_LOW <= pb <= BB_MID_HIGH)
    st["lolos_1h"] = len(mom)

    # Tahap 3 — 15M trigger + keputusan + level
    out, d15 = [], api.stage(list(mom), "15m")
    for sym, k15 in d15.items():
        ind = indicators(k15)
        t, ti = trigger_15m(ind)
        rsi_1h, bb_mid = mom[sym]
        vr = vol_ratio(k15, ti) if ti is not None else 1.0
        dec = decide(bias[sym], "Bullish", t, rsi_1h, vr < 1.0, bb_mid)
        if not dec:
            continue
        lv = levels(d1[sym], k15, k15[-1]["c"])
        if not lv:
            continue
        out.append({"symbol": sym[:-4], "pair": sym, "type": dec[0], "kesimpulan": dec[1],
                    "bias_4h": bias[sym], "momentum_1h": "Bullish", "trigger_15m": t,
                    "vol_ratio": vr, "close": k15[-1]["c"], **lv})
    st["lolos_15m"] = len(out)
    return out, st, d4


def build_item(r, now):
    return {
        "symbol": r["symbol"], "pair": r["pair"], "mode": MODE, "type": r["type"],
        "created_at": to_iso(now), "expires_at": to_iso(now + timedelta(hours=WINDOW_H)),
        "pionex_verified": True, "bias_4h": r["bias_4h"], "momentum_1h": r["momentum_1h"],
        "trigger_15m": r["trigger_15m"], "kesimpulan": r["kesimpulan"],
        "vol_ratio": round(r["vol_ratio"], 2), "rr": round(r["rr"], 2),
        "price_at_signal": r["close"], "entry": r["entry"], "sl": r["sl"], "tp": r["tp"],
        "posisi_usd": round(r["pos"], 2), "support": r["sup"], "resistance": r["res"],
        "checked_until": int(now.timestamp() * 1000),
        "high_seen": r["close"], "low_seen": r["close"], "high_minute": 0, "low_minute": 0,
        "last_price": r["close"], "hit": None, "touched_support": False,
        "touched_resistance": False, "reported_at": None, "result": None,
    }


def clear_last_batch():
    with state_lock():
        st = load_state()
        st["last_batch"], st["pairs_count"] = [], 0
        save_state(st)


def main():
    run_id = uuid.uuid4().hex[:8]
    clear_last_batch()  # reset dulu, supaya report tidak mengulang item run sebelumnya
    print(f"[run {run_id}] mode={MODE}")

    t0 = time.perf_counter()
    try:
        tickers = api.fetch_tickers()
    except Exception as e:
        discord_send([make_embed("🚨 BINANCE DOWN",
                                 f"Ambil ticker gagal, run dibatalkan.\n```{str(e)[:900]}```",
                                 "red")])
        print(f"[abort] ticker: {e}")
        return
    try:
        source, pairs = flt.run(tickers)
    except flt.PionexDown as e:
        discord_send([make_embed("🚨 PIONEX DOWN",
                                 f"Semua sumber Pionex gagal, run dibatalkan.\n```{e}```",
                                 "red", "pionex_down")])
        clear_last_batch()
        print(f"[abort] {e}")
        return

    t1 = time.perf_counter()
    try:
        spreads = api.fetch_book_spreads()
    except Exception as e:
        print(f"[spread] bookTicker gagal, pakai depth per koin: {e}")
        spreads = {}
    results, stats, d4 = scan(pairs, spreads)
    print(f"[timing] collect+analisis {time.perf_counter() - t1:.1f}s | gugur bertahap: {stats}")

    # Status BTC 4H: pakai candle BTC yang sudah ditarik tahap 4H (tanpa request dobel)
    btc_status = "unknown"
    try:
        bk = d4.get("BTCUSDT") or api.fetch_btc_4h()
        c4 = [c["c"] for c in bk]
        btc_status = "above_EMA200_4H" if c4[-1] > ema(c4, 200)[-1] else "below_EMA200_4H"
    except Exception as e:
        print(f"[btc] gagal: {e}")

    key = lambda r: (r["vol_ratio"], r["rr"])
    signals = sorted([r for r in results if r["type"] == "signal"], key=key, reverse=True)[:MAX_SIG]
    cands = sorted([r for r in results if r["type"] == "candidate"], key=key, reverse=True)[:MAX_CAND]

    now = now_utc()
    with state_lock():
        st = load_state()
        active = {(p["pair"], p["type"]) for p in st["pending"]}
        new_items = []
        for r in signals + cands:
            if (r["pair"], r["type"]) in active:
                continue
            item = build_item(r, now)
            st["pending"].append(item)
            new_items.append(item)
            cd = st["cooldown"].get(r["symbol"])
            if not cd or from_iso(cd) <= now:
                st["cooldown"][r["symbol"]] = to_iso(now + timedelta(hours=WINDOW_H))
        st["last_batch"] = [{"pair": i["pair"], "type": i["type"], "created_at": i["created_at"]}
                            for i in new_items]
        st.update(run_id=run_id, mode=MODE, btc_4h_status=btc_status,
                  pionex_source_used=source, pairs_count=len(pairs))
        save_state(st)
    print(f"[timing] total {time.perf_counter() - t0:.1f}s")
    print(f"[analisis] sinyal={len(signals)} kandidat={len(cands)} baru={len(new_items)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        clear_last_batch()
        discord_send([make_embed("🚨 ANALISIS ERROR",
                                 f"Run dihentikan karena error.\n```{str(e)[:900]}```",
                                 "red")])
        raise
