"""Fase 4 — update pending, cek SL/TP & support/resistance, kirim Discord, evaluasi, pruning, lepas cooldown."""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from shared import (MODE, WINDOW_TOL_MIN, EVAL_MAX_DAYS, MAX_SIG, MAX_CAND, MAX_WORKERS, state_lock,
                    load_state, save_state, now_utc, to_iso, from_iso, make_embed,
                    discord_send, load_module)

api = load_module("bot-API.py", "bot_api")
TOL = timedelta(minutes=WINDOW_TOL_MIN)
MONITOR_MAX = 3


def fmt(x):
    return "-" if x is None else f"{x:.6g}"


def chg(now_p, base):
    return (now_p - base) / base * 100 if base else 0.0


def update_item(it, now):
    created_ms = int(from_iso(it["created_at"]).timestamp() * 1000)
    end = min(now, from_iso(it["expires_at"]))
    end_ms = int(end.timestamp() * 1000)
    start = max(it["checked_until"], created_ms)
    if start >= end_ms:
        return
    raw = api.fetch_klines(it["pair"], "1m", 1000, start_ms=start, end_ms=end_ms)
    for c in raw:
        if not (c["open_time"] >= start and c["close_time"] < end_ms):
            continue
        minute = int((c["open_time"] - created_ms) // 60000)
        if c["h"] > it["high_seen"]:
            it["high_seen"], it["high_minute"] = c["h"], minute
        if c["l"] < it["low_seen"]:
            it["low_seen"], it["low_minute"] = c["l"], minute
        if it["support"] and c["l"] <= it["support"]:
            it["touched_support"] = True
        if it["resistance"] and c["h"] >= it["resistance"]:
            it["touched_resistance"] = True
        if it["hit"] is None:
            if c["l"] <= it["sl"]:
                it["hit"] = "SL"
            elif c["h"] >= it["tp"]:
                it["hit"] = "TP"
        it["last_price"] = c["c"]
    it["checked_until"] = end_ms


def card(title, desc, color, x, meme_key=None, extra=None):
    """Kartu koin: entry, SL, TP, support, resistance (+ extra), semua sebaris."""
    fields = [
        ("🎯 Entry", fmt(x["entry"])),
        ("🛑 SL", fmt(x["sl"])),
        ("🏁 TP", fmt(x["tp"])),
        ("🟢 Support", fmt(x["support"])),
        ("🔴 Resistance", fmt(x["resistance"])),
    ] + (extra or [])
    e = make_embed(title, desc, color, meme_key, fields=fields)
    for f in e.get("fields", []):
        f["inline"] = True
    return e


def high_low_val(x, price, minute):
    """Nilai high/low: harga, persen dari harga saat muncul, dan menit ke-berapa."""
    base = x["price_at_signal"]
    pct = (price - base) / base * 100 if base else 0.0
    return f"{fmt(price)} ({pct:+.2f}%)\n⏱ menit ke-{minute}"


def analisis_text(x):
    """Bias/Momentum/Trigger + kesimpulan + Skenario Jika (level kunci = support 1H, cadangan SL)."""
    head = (f"**{x.get('kesimpulan', '-')}**\n"
            f"4H **{x.get('bias_4h', '-')}** · 1H **{x.get('momentum_1h', '-')}** · "
            f"15M **{x.get('trigger_15m', '-')}**")
    lv = fmt(x["support"] if x.get("support") else x["sl"])
    tgt = f"resistance {fmt(x['resistance'])}" if x.get("resistance") else f"TP {fmt(x['tp'])}"
    return (f"{head}\n"
            f"• Jika harga bertahan di atas {lv} → skenario lanjutan bullish (target {tgt})\n"
            f"• Jika harga gagal di {lv} → skenario koreksi\n"
            "• Jika sinyal berubah → analisis diulang dari candle tertutup berikutnya")


def build_embeds(st, keep, finals, new_keys, now):
    fresh = [it for it in keep if (it["pair"], it["created_at"]) in new_keys]
    sig_new = [x for x in fresh if x["type"] == "signal"][:MAX_SIG]
    cand_new = [x for x in fresh if x["type"] == "candidate"][:MAX_CAND]

    if sig_new:
        status = f"🚦 SINYAL: **{len(sig_new)} sinyal baru** jam ini"
    else:
        status = "🚦 SINYAL: **tidak ada sinyal** jam ini"

    e = [make_embed(
        "🤖 SCAN MULTI-TIMEFRAME 4H/1H/15M • LAPORAN JAM INI",
        f"Mode **{MODE.upper()}** · BTC 4H **{st.get('btc_4h_status') or '-'}**\n{status}",
        "blue", "info",
        fields=[("🔎 Pair lolos filter", st.get("pairs_count", 0)),
                ("📡 Sumber Pionex", st.get("pionex_source_used") or "-"),
                ("⏳ Sedang dipantau", f"{len(keep)} (tampil {min(len(keep), MONITOR_MAX)} terbaru)"),
                ("🟡 Kandidat jam ini", len(cand_new))])]

    # 1. SINYAL BARU, atau penjelasan kenapa tidak ada
    if sig_new:
        for it in sig_new:
            e.append(card(
                f"🚀 SINYAL BARU • {it['symbol']}",
                f"{analisis_text(it)}\nPantau 6 jam 👀",
                "green", it, meme_key="sinyal",
                extra=[("⚖️ RR", str(it["rr"])), ("💵 Posisi", f"${it['posisi_usd']}")]))
    else:
        e.append(make_embed(
            "😐 Tidak ada sinyal jam ini",
            "Tidak ada koin yang lolos Bias 4H + Momentum 1H + Trigger 15M "
            "+ filter risiko + spread.\n"
            "Kandidat di bawah adalah koin yang potensial tapi belum cukup kuat.",
            "grey", "kosong"))

    # 2. KANDIDAT (selalu tampil, bukan sinyal)
    if cand_new:
        e.append(make_embed(
            "🟡 KANDIDAT (bukan sinyal, butuh konfirmasi)",
            f"{len(cand_new)} koin potensial, belum memenuhi syarat sinyal kuat.",
            "yellow", "kandidat"))
        for it in cand_new:
            e.append(card(
                f"🟡 KANDIDAT • {it['symbol']}",
                analisis_text(it),
                "yellow", it, meme_key="kandidat",
                extra=[("⚖️ RR", str(it["rr"]))]))
    else:
        e.append(make_embed(
            "🟡 KANDIDAT",
            "Tidak ada kandidat jam ini.",
            "grey", "kosong"))

    # 3. PEMANTAUAN (maks 3 terbaru)
    shown = sorted(keep, key=lambda x: x["created_at"], reverse=True)[:MONITOR_MAX]
    for x in shown:
        mins = max(0, int((from_iso(x["expires_at"]) - now).total_seconds() // 60))
        s_tag = "✅" if x["touched_support"] else "⬜"
        r_tag = "✅" if x["touched_resistance"] else "⬜"
        desc = (f"💰 Sekarang **{fmt(x['last_price'])}** "
                f"({chg(x['last_price'], x['price_at_signal']):+.2f}%)\n"
                f"⏱ Sisa **{mins} menit** · Kena: **{x['hit'] or 'belum'}**\n"
                f"Sentuh support {s_tag} · resistance {r_tag}")
        e.append(card(
            f"📡 PEMANTAUAN • {x['symbol']}", desc, "purple", x, meme_key="pemantauan",
            extra=[("🔺 High", high_low_val(x, x["high_seen"], x["high_minute"])),
                   ("🔻 Low", high_low_val(x, x["low_seen"], x["low_minute"]))]))

    # 4. EVALUASI (hanya saat ada window selesai)
    if finals:
        tp = [x for x in finals if x["result"] == "TP"]
        sl = [x for x in finals if x["result"] == "SL"]
        tk = [x for x in finals if x["result"] == "TIDAK_KENA"]
        avg = sum(x["perubahan_6j_pct"] for x in finals) / len(finals)
        win = len(tp) / len(finals) * 100
        color = "green" if len(tp) > len(sl) else "red" if len(sl) > len(tp) else "grey"
        meme_key = "tp" if tp else ("sl" if sl else "kosong")

        e.append(make_embed(
            "🏁 EVALUASI WINDOW 6 JAM",
            f"TP **{len(tp)}** · SL **{len(sl)}** · TIDAK_KENA **{len(tk)}**\n"
            f"Winrate **{win:.0f}%** · rata-rata **{avg:+.2f}%**",
            color, meme_key))

        for x in finals:
            e.append({
                "title": f"{x['symbol']} • {x['result']} • {x['type'].upper()}"[:250],
                "description": "",
                "color": {"TP": 0x2ECC71, "SL": 0xE74C3C}.get(x["result"], 0x95A5A6),
                "fields": [
                    {"name": "🕒 Jam muncul",
                     "value": from_iso(x["created_at"]).strftime("%d %b %Y · %H:%M UTC"),
                     "inline": True},
                    {"name": "💰 Harga saat muncul", "value": fmt(x["price_at_signal"]), "inline": True},
                    {"name": "📍 Harga sekarang",
                     "value": f"{fmt(x['last_price'])} ({x['perubahan_6j_pct']:+.2f}%)", "inline": True},
                    {"name": "🔺 Harga tertinggi tersentuh",
                     "value": high_low_val(x, x["high_seen"], x["high_minute"]), "inline": True},
                    {"name": "🔻 Harga terendah tersentuh",
                     "value": high_low_val(x, x["low_seen"], x["low_minute"]), "inline": True},
                    {"name": "🎯 Hasil",
                     "value": f"{x['result']} · SL {fmt(x['sl'])} · TP {fmt(x['tp'])}", "inline": True},
                ],
                "timestamp": to_iso(now_utc()),
                "footer": {"text": f"Bot Scanner Multi-Timeframe • {MODE.upper()}"},
            })

    return e


def main():
    now = now_utc()
    with state_lock():
        st = load_state()
        t0 = time.perf_counter()

        def _safe(it):
            try:
                update_item(it, now)
            except Exception as err:
                print(f"[report] {it['pair']} gagal update: {err}")

        with ThreadPoolExecutor(MAX_WORKERS) as pool:
            list(pool.map(_safe, st["pending"]))
        print(f"[timing] update pending {time.perf_counter() - t0:.1f}s ({len(st['pending'])} item)")

        keep, done = [], []
        for it in st["pending"]:
            (done if from_iso(it["expires_at"]) - TOL <= now else keep).append(it)

        finals = []
        for it in done:
            f = dict(it)
            f["result"] = it["hit"] or "TIDAK_KENA"
            f["perubahan_6j_pct"] = round(chg(it["last_price"], it["price_at_signal"]), 2)
            finals.append(f)

        new_keys = {(b["pair"], b["created_at"]) for b in st.get("last_batch", [])}
        has_webhook = bool(os.environ.get("DISCORD_WEBHOOK_URL"))
        ok = discord_send(build_embeds(st, keep, finals, new_keys, now)) or not has_webhook

        if ok:
            for it in keep + done:
                it["reported_at"] = to_iso(now)
            for f in finals:
                f["evaluated_at"] = to_iso(now)
            st["pending"] = keep
            st["evaluated"].extend(finals)

            # Window selesai -> cooldown koin dilepas, koin boleh masuk scan lagi
            keep_syms = {x["symbol"] for x in keep}
            for it in done:
                if it["symbol"] not in keep_syms:
                    st["cooldown"].pop(it["symbol"], None)
        else:
            st["pending"] = keep + done

        st["cooldown"] = {k: v for k, v in st["cooldown"].items() if from_iso(v) > now}
        cutoff = now - timedelta(days=EVAL_MAX_DAYS)
        st["evaluated"] = [x for x in st["evaluated"] if from_iso(x["evaluated_at"]) >= cutoff]
        save_state(st)
    print(f"[report] pantau={len(keep)} evaluasi={len(finals)} kirim={ok}")


if __name__ == "__main__":
    main()
