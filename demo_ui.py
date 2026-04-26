"""
Demo UI — layout options with dummy data.
Run with: streamlit run demo_ui.py
"""
import streamlit as st

st.set_page_config(page_title="UI Layout Demo", layout="wide")

# ── Dummy data ─────────────────────────────────────────────────────────────────
SYMBOL = "ES"
TFS = [
    {"tf": "Monthly", "bias": "BULL", "score": 7,  "reversal": 1,  "rv_label": "LOW",  "rv_dir": "none",    "close": 5320.25, "signals": ["MA50>MA200", "ST bullish", "Price>MA200"]},
    {"tf": "Weekly",  "bias": "BULL", "score": 5,  "reversal": 4,  "rv_label": "MED",  "rv_dir": "too high",  "close": 5318.50, "signals": ["ST bullish", "RSI overbought 71", "Wick BB 2.5SD"]},
    {"tf": "Daily",   "bias": "BEAR", "score": -2, "reversal": 6,  "rv_label": "MED",  "rv_dir": "too high",  "close": 5310.00, "signals": ["Price < MA200", "ST bearish", "FBO at 5350"]},
    {"tf": "2H",      "bias": "BULL", "score": 3,  "reversal": 2,  "rv_label": "LOW",  "rv_dir": "none",    "close": 5312.75, "signals": ["ST bullish", "RSI>50"]},
    {"tf": "15min",   "bias": "BEAR", "score": -4, "reversal": 7,  "rv_label": "HIGH", "rv_dir": "too high",  "close": 5308.00, "signals": ["ST bearish", "FBO at 5320", "BB 3SD breach"]},
]

SCORE_FACTORS = {
    "Daily": [
        ("▲", "MA8 > MA20", +1), ("▲", "MA20 > MA50", +1), ("▼", "MA50 < MA200", -1),
        ("▼", "Price < MA200", -1), ("▼", "SuperTrend bearish", -2),
        ("▲", "RSI > 50 (54.2)", +1), ("▼", "RSI < RSI MA (54.2 < 57.1)", -2), ("▲", "BB upper half (61%)", +1),
    ]
}
REV_FACTORS = {
    "Daily": [
        ("▲", "Wick above BB 2.5SD upper (5340.00)", 2),
        ("▲", "RSI overbought (71.3)", 1),
        ("▲", "FBO at 5350.00", 2),
        ("▲", "Overextended above MA50 (+12.3%)", 1),
    ]
}

def _score_color(score):
    if score >= 5:   return "#1a7f37"
    if score >= 2:   return "#4caf50"
    if score >= 0:   return "#8bc34a"
    if score >= -2:  return "#ff9800"
    if score >= -5:  return "#f44336"
    return "#b71c1c"

def _badge(text, bg, fg="#fff", size="0.8rem"):
    return f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:4px;font-size:{size};font-weight:600">{text}</span>'

def _rv_color(label):
    return {"LOW": "#4caf50", "MED": "#ff9800", "HIGH": "#f44336"}.get(label, "#888")

# ══════════════════════════════════════════════════════════════════════════════
st.title("UI Layout Demo — ES  (dummy data)")
option = st.radio("Choose layout", ["Option A — Score Table", "Option B — Score Chips", "Option C — Narrative Card", "Option D — Two Panel", "Option A+C Combined (Recommended)"], horizontal=True)
st.divider()

# ── Option A ───────────────────────────────────────────────────────────────────
if option == "Option A — Score Table":
    st.subheader("ES — Score Summary Table")
    rows = []
    for t in TFS:
        rows.append({
            "TF":       t["tf"],
            "Bias":     t["bias"],
            "Score":    f"{t['score']:+d} / 10",
            "Reversal": f"{t['rv_label']} {t['reversal']}/10",
            "Key Signals": ",  ".join(t["signals"]),
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)

    st.divider()
    st.caption("Click a timeframe expander for full detail")
    for t in TFS:
        with st.expander(f"**{t['tf']}** — Close: {t['close']:.2f}  |  Score: {t['score']:+d}  |  Reversal: {t['rv_label']} {t['reversal']}/10"):
            c1, c2 = st.columns(2)
            c1.metric("Bull/Bear Score", f"{t['score']:+d}/10", delta=t["bias"].lower(),
                      delta_color="normal" if t["score"] > 0 else "inverse")
            c2.metric("Reversal Risk", f"{t['reversal']}/10 {t['rv_label']}",
                      delta_color="inverse" if t["reversal"] >= 7 else "off")
            if t["tf"] in SCORE_FACTORS:
                parts = [f"{icon} {r} ({p:+d})" for icon, r, p in SCORE_FACTORS[t["tf"]]]
                st.caption("Score: " + "  |  ".join(parts))
            if t["tf"] in REV_FACTORS:
                parts = [f"{icon} {r} (+{p})" for icon, r, p in REV_FACTORS[t["tf"]]]
                st.caption("Reversal: " + "  |  ".join(parts))

# ── Option B ───────────────────────────────────────────────────────────────────
elif option == "Option B — Score Chips":
    st.subheader("ES — Timeframe Scores")

    cols = st.columns(5)
    for col, t in zip(cols, TFS):
        bg = _score_color(t["score"])
        rv_bg = _rv_color(t["rv_label"])
        with col:
            rev_badge = _badge(f"Rev {t['reversal']}/10", rv_bg)
            st.markdown(
                f'<div style="border:1px solid #333;border-radius:8px;padding:10px;text-align:center">'
                f'<div style="font-size:0.85rem;color:#aaa">{t["tf"]}</div>'
                f'<div style="font-size:1.6rem;font-weight:700;color:{bg}">{t["score"]:+d}</div>'
                f'<div style="font-size:0.8rem;color:{bg}">{t["bias"]}</div>'
                f'<div style="margin-top:6px">{rev_badge}</div>'
                f'<div style="font-size:0.7rem;color:#999;margin-top:4px">{t["close"]:.2f}</div>'
                f'</div>',
                unsafe_allow_html=True
            )

    st.divider()
    for t in TFS:
        with st.expander(f"**{t['tf']}** detail"):
            st.write("  |  ".join(t["signals"]))
            if t["tf"] in SCORE_FACTORS:
                parts = [f"{icon} {r} ({p:+d})" for icon, r, p in SCORE_FACTORS[t["tf"]]]
                st.caption("Score: " + "  |  ".join(parts))

# ── Option C ───────────────────────────────────────────────────────────────────
elif option == "Option C — Narrative Card":
    bull_tfs = [t["tf"] for t in TFS if t["bias"] == "BULL"]
    bear_tfs = [t["tf"] for t in TFS if t["bias"] == "BEAR"]
    high_rev = [t for t in TFS if t["reversal"] >= 4]
    fbo_tfs  = [t["tf"] for t in TFS if any("FBO" in s for s in t["signals"])]
    fbd_tfs  = [t["tf"] for t in TFS if any("FBD" in s for s in t["signals"])]

    bias_label = "Bullish" if len(bull_tfs) > len(bear_tfs) else "Bearish"
    align = f"{max(len(bull_tfs), len(bear_tfs))}/5 TFs agree"

    st.markdown(f"### {SYMBOL} — {bias_label} bias, {align}")

    trend_line = f"**Trend:** {', '.join(bull_tfs)} bullish" + (f" · {', '.join(bear_tfs)} bearish" if bear_tfs else "")
    st.markdown(trend_line)

    if high_rev:
        rev_parts = [f"{t['tf']} ({t['rv_label']} {t['reversal']}/10)" for t in high_rev]
        st.warning("**Reversal risk:** " + " · ".join(rev_parts))

    if fbo_tfs:
        st.error(f"**FBO detected** on {', '.join(fbo_tfs)} — price rejected above key highs")
    if fbd_tfs:
        st.success(f"**FBD detected** on {', '.join(fbd_tfs)} — price rejected below key lows (bullish)")

    st.divider()
    for t in TFS:
        with st.expander(f"**{t['tf']}** — {t['close']:.2f}  ·  Score {t['score']:+d}  ·  Rev {t['rv_label']} {t['reversal']}/10"):
            if t["tf"] in SCORE_FACTORS:
                parts = [f"{icon} {r} ({p:+d})" for icon, r, p in SCORE_FACTORS[t["tf"]]]
                st.caption("Score: " + "  |  ".join(parts))
            if t["tf"] in REV_FACTORS:
                parts = [f"{icon} {r} (+{p})" for icon, r, p in REV_FACTORS[t["tf"]]]
                st.caption("Reversal: " + "  |  ".join(parts))

# ── Option D ───────────────────────────────────────────────────────────────────
elif option == "Option D — Two Panel":
    left, right = st.columns([3, 2])

    with left:
        st.subheader("TF Scores")
        rows = [{"TF": t["tf"], "Bias": t["bias"], "Score": f"{t['score']:+d}/10",
                 "Reversal": f"{t['rv_label']} {t['reversal']}/10"} for t in TFS]
        st.dataframe(rows, use_container_width=True, hide_index=True)

    with right:
        st.subheader("Notable Flags")
        flags = []
        for t in TFS:
            if t["reversal"] >= 4:
                flags.append(f"**{t['tf']}** — Rev {t['rv_label']} {t['reversal']}/10 ({t['rv_dir']})")
            for s in t["signals"]:
                if any(kw in s for kw in ["FBO", "FBD", "oversold", "overbought", "breach"]):
                    flags.append(f"**{t['tf']}** — {s}")
        if flags:
            for f in flags:
                st.markdown(f"• {f}")
        else:
            st.success("No notable flags — clean trend")

    st.divider()
    for t in TFS:
        with st.expander(f"**{t['tf']}** — {t['close']:.2f}  ·  Score {t['score']:+d}  ·  Rev {t['rv_label']}"):
            st.write("  |  ".join(t["signals"]))

# ── Option A+C Combined ────────────────────────────────────────────────────────
elif option == "Option A+C Combined (Recommended)":
    bull_tfs = [t["tf"] for t in TFS if t["bias"] == "BULL"]
    bear_tfs = [t["tf"] for t in TFS if t["bias"] == "BEAR"]
    high_rev = [t for t in TFS if t["reversal"] >= 4]
    fbo_tfs  = [t["tf"] for t in TFS if any("FBO" in s for s in t["signals"])]
    fbd_tfs  = [t["tf"] for t in TFS if any("FBD" in s for s in t["signals"])]
    bias_label = "Bullish" if len(bull_tfs) > len(bear_tfs) else "Bearish"

    # Narrative card
    st.markdown(f"### {SYMBOL} — {bias_label}, {max(len(bull_tfs), len(bear_tfs))}/5 TFs agree")
    st.markdown(f"**Trend:** {', '.join(bull_tfs)} bullish" + (f"  ·  {', '.join(bear_tfs)} bearish" if bear_tfs else ""))
    if high_rev:
        rev_parts = [f"{t['tf']} ({t['rv_label']} {t['reversal']}/10)" for t in high_rev]
        st.warning("**Reversal risk:** " + " · ".join(rev_parts))
    if fbo_tfs:
        st.error(f"**FBO:** {', '.join(fbo_tfs)}")
    if fbd_tfs:
        st.success(f"**FBD:** {', '.join(fbd_tfs)}")

    st.divider()

    # Compact score table — key signals only
    rows = [{"TF": t["tf"], "Key Signals": "  ·  ".join(t["signals"])} for t in TFS]
    st.dataframe(rows, use_container_width=True, hide_index=True)

    st.divider()

    # Expanders — no score bar in title, just score number
    for t in TFS:
        title_bias = "🟢" if t["bias"] == "BULL" else "🔴"
        rv_icon = "🔴" if t["rv_label"] == "HIGH" else "🟡" if t["rv_label"] == "MED" else "🟢"
        with st.expander(f"{title_bias} **{t['tf']}** — {t['close']:.2f}  ·  Score {t['score']:+d}/10  ·  Rev {rv_icon} {t['reversal']}/10"):
            c1, c2 = st.columns(2)
            c1.metric("Bull/Bear Score", f"{t['score']:+d}/10",
                      delta=t["bias"].lower(),
                      delta_color="normal" if t["score"] > 0 else "inverse")
            c2.metric("Reversal Risk", f"{t['reversal']}/10 {t['rv_label']}",
                      delta=t["rv_dir"] if t["rv_dir"] != "none" else None,
                      delta_color="inverse" if t["reversal"] >= 7 else "off")

            if t["tf"] in SCORE_FACTORS:
                parts = [f"{icon} {r} ({p:+d})" for icon, r, p in SCORE_FACTORS[t["tf"]]]
                st.caption("Score: " + "  |  ".join(parts))
            if t["tf"] in REV_FACTORS:
                parts = [f"{icon} {r} (+{p})" for icon, r, p in REV_FACTORS[t["tf"]]]
                st.caption("Reversal: " + "  |  ".join(parts))
