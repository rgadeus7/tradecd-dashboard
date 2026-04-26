import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from tools.ai_client import SYSTEM_PROMPT, _get_active_providers, chat
from tools.prompt_builder import build_messages, build_prompt

st.set_page_config(page_title="Trading Assistant", layout="wide")

SNAPSHOT_PATH = Path("data/market_snapshot.json")

TF_ORDER  = ["monthly", "weekly", "daily", "2H", "15min"]
TF_LABELS = {"15min": "15-Min", "2H": "2-Hour", "daily": "Daily",
             "weekly": "Weekly", "monthly": "Monthly"}

# ── Helpers ────────────────────────────────────────────────────────────────────
def load_snapshot():
    if not SNAPSHOT_PATH.exists():
        return None
    try:
        with open(SNAPSHOT_PATH) as f:
            content = f.read().strip()
            return json.loads(content) if content else None
    except json.JSONDecodeError:
        return None


def _price(v):
    return f"{v:.2f}" if v is not None else "—"


def _pct(v):
    if v is None:
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.2f}%"


def _dir_color(direction):
    if direction == "bullish":
        return "green"
    if direction == "bearish":
        return "red"
    return "gray"



def _rev_label(score: int) -> str:
    if score >= 7: return "HIGH"
    if score >= 4: return "MEDIUM"
    return "LOW"


def _bias_label(score: int) -> str:
    if score >= 9:  return "Strong Bull"
    if score >= 5:  return "Bull"
    if score >= 1:  return "Mild Bull"
    if score == 0:  return "Neutral"
    if score >= -4: return "Mild Bear"
    if score >= -8: return "Bear"
    return "Strong Bear"


# ── Helpers for key signals summary ───────────────────────────────────────────
def _tf_key_signals(tf, tf_data):
    """Return a short list of notable signals for the summary table."""
    signals = []
    st_dir  = tf_data.get("supertrend_dir", "")
    rsi     = tf_data.get("rsi")
    rsi_ma  = tf_data.get("rsi_ma")
    overext = tf_data.get("overextension", {})
    flags   = overext.get("flags", [])
    sideways = tf_data.get("sideways", {})
    bb      = tf_data.get("bb", {})
    bb_pos  = bb.get("position")
    close   = tf_data.get("close") or 0

    if st_dir:
        signals.append(f"SuperTrend {st_dir}")
    if rsi is not None and rsi_ma is not None:
        if rsi >= 70:   signals.append(f"RSI overbought ({rsi:.0f})")
        elif rsi <= 30: signals.append(f"RSI oversold ({rsi:.0f})")
        elif rsi > rsi_ma: signals.append(f"RSI > MA ({rsi:.0f})")
        else:              signals.append(f"RSI < MA ({rsi:.0f})")

    ma200 = tf_data.get("ma200")
    if ma200:
        signals.append("Price > MA200" if close > ma200 else "Price < MA200")

    for flag in flags:
        signals.append(flag.replace("_", " ").title())

    if sideways.get("consolidating_15w"):
        signals.append("Consolidating 15w")
    elif sideways.get("consolidating_10w"):
        signals.append("Consolidating 10w")

    bb_upper_3 = bb.get("upper_3")
    bb_lower_3 = bb.get("lower_3")
    if bb_upper_3 and close >= bb_upper_3:  signals.append("Above BB 3SD")
    elif bb_pos and bb_pos > 80:            signals.append(f"BB high ({bb_pos:.0f}%)")
    if bb_lower_3 and close <= bb_lower_3:  signals.append("Below BB 3SD")
    elif bb_pos and bb_pos < 20:            signals.append(f"BB low ({bb_pos:.0f}%)")

    fbd = tf_data.get("fbd_levels", [])
    fbo = tf_data.get("fbo_levels", [])
    for r in fbd:
        if isinstance(r, dict): signals.append(f"FBD {_price(r['level'])} ({r['source']})")
        else: signals.append(f"FBD {_price(r)}")
    for r in fbo:
        if isinstance(r, dict): signals.append(f"FBO {_price(r['level'])} ({r['source']})")
        else: signals.append(f"FBO {_price(r)}")

    return signals


# ── Snapshot display ───────────────────────────────────────────────────────────
def render_tf_card(tf, tf_data):
    label    = TF_LABELS.get(tf, tf)
    close    = tf_data.get("close")
    st_val   = tf_data.get("supertrend")
    st_dir   = tf_data.get("supertrend_dir", "")
    rsi      = tf_data.get("rsi")
    rsi_ma   = tf_data.get("rsi_ma")
    atr      = tf_data.get("atr14")
    overext  = tf_data.get("overextension", {})
    sideways = tf_data.get("sideways", {})
    flags    = overext.get("flags", [])
    c10      = sideways.get("consolidating_10w", False)
    c15      = sideways.get("consolidating_15w", False)
    bb       = tf_data.get("bb", {})
    bb_score = tf_data.get("bull_bear_score")
    rv_score = tf_data.get("reversal_score")
    rv_dir   = tf_data.get("reversal_dir", "")

    bias_icon = "🟢" if (bb_score or 0) > 0 else "🔴" if (bb_score or 0) < 0 else "⚪"
    rv_icon   = "🔴" if rv_score and rv_score >= 7 else "🟡" if rv_score and rv_score >= 4 else "🟢"
    bias_lbl  = _bias_label(bb_score) if bb_score is not None else ""
    score_str = f"  {bias_lbl} ({bb_score:+d})" if bb_score is not None else ""
    rev_str   = f"  Rev {rv_icon} {rv_score}/10" if rv_score is not None else ""
    expander_label = f"{bias_icon} **{label}** — {_price(close)}{score_str}{rev_str}"

    with st.expander(expander_label, expanded=(tf == "daily")):

        # Scores + breakdowns
        if bb_score is not None:
            sc_cols = st.columns(2)
            bias_color = "normal" if bb_score > 0 else "inverse" if bb_score < 0 else "off"
            sc_cols[0].metric("Bull/Bear Score", f"{bb_score:+d} / 10",
                              delta=_bias_label(bb_score),
                              delta_color=bias_color)
            if rv_score is not None:
                rv_color = "inverse" if rv_score >= 7 else "off"
                sc_cols[1].metric("Reversal Risk",
                                  f"{rv_score}/10 {_rev_label(rv_score)}",
                                  delta=rv_dir if rv_dir != "none" else None,
                                  delta_color=rv_color)

        bb_breakdown = tf_data.get("bull_bear_breakdown", [])
        if bb_breakdown:
            parts = [f"{'▲' if r['points'] > 0 else '▼'} {r['reason']} ({r['points']:+d})"
                     for r in bb_breakdown]
            st.caption("Score: " + "  |  ".join(parts))

        rv_breakdown = tf_data.get("reversal_breakdown", [])
        if rv_breakdown:
            side_icon = {"up": "▲", "dn": "▼", "both": "↔"}
            parts = [f"{side_icon.get(r['side'],'')} {r['reason']} (+{r['points']})"
                     for r in rv_breakdown]
            st.caption("Reversal: " + "  |  ".join(parts))

        st.divider()

        # Alerts
        if c10 or c15:
            r10 = sideways.get("range_10w_pct")
            r15 = sideways.get("range_15w_pct")
            parts = []
            if r10: parts.append(f"10w range: {_pct(r10)}")
            if r15: parts.append(f"15w range: {_pct(r15)}")
            st.warning("Sideways / Consolidation — " + " | ".join(parts) + " — breakout or breakdown likely")

        fbd_levels = tf_data.get("fbd_levels", [])
        fbo_levels = tf_data.get("fbo_levels", [])
        if fbd_levels:
            parts = ", ".join(f"{_price(r['level'])} ({r['source']})" if isinstance(r, dict) else _price(r) for r in fbd_levels)
            st.success(f"FBD — failed to break below: {parts}")
        if fbo_levels:
            parts = ", ".join(f"{_price(r['level'])} ({r['source']})" if isinstance(r, dict) else _price(r) for r in fbo_levels)
            st.error(f"FBO — failed to break above: {parts}")

        # Key metrics
        cols = st.columns(4)
        cols[0].metric("Close",      _price(close))
        cols[1].metric("SuperTrend", _price(st_val),
                       delta=st_dir,
                       delta_color="normal" if st_dir == "bullish" else "inverse")
        cols[2].metric("RSI(14)",    _price(rsi),
                       delta=f"MA {_price(rsi_ma)}",
                       delta_color="normal" if (rsi or 0) > (rsi_ma or 0) else "inverse")
        if atr:
            cols[3].metric("ATR(14)", _price(atr))

        ma_cols = st.columns(4)
        for i, ma in enumerate(["ma8", "ma20", "ma50", "ma200"]):
            val = tf_data.get(ma)
            pct = overext.get(f"pct_from_{ma}")
            if val is not None:
                ma_cols[i].metric(ma.upper(), _price(val),
                                  delta=_pct(pct) if pct else None,
                                  delta_color="normal" if (pct or 0) > 0 else "inverse")

        if bb:
            st.caption(
                f"BB — Basis: {_price(bb.get('basis'))}  |  "
                f"2SD: {_price(bb.get('lower_2'))} / {_price(bb.get('upper_2'))}  |  "
                f"2.5SD: {_price(bb.get('lower_25'))} / {_price(bb.get('upper_25'))}  |  "
                f"3SD: {_price(bb.get('lower_3'))} / {_price(bb.get('upper_3'))}  |  "
                f"Position: {_price(bb.get('position'))}%  |  "
                f"Width: {_price(bb.get('width_pct'))}%"
            )

        if overext and any(k != "flags" for k in overext):
            st.caption("Distance from MAs: " + "  |  ".join(
                f"{k.replace('pct_from_','').upper()}: {_pct(v)}"
                for k, v in overext.items() if k != "flags" and v is not None
            ))

        for key, label_str in [
            ("last_5_days",     "Last 5 Days"),
            ("last_5_weeks",    "Last 5 Weeks"),
            ("last_5_months",   "Last 5 Months"),
            ("last_5_quarters", "Last 5 Quarters"),
        ]:
            bars = tf_data.get(key)
            if not bars:
                continue
            st.markdown(f"**{label_str}**")
            rows = []
            for b in bars:
                date = b.get("date") or b.get("quarter", "")
                rows.append({"Date/Period": date, "High": b["high"],
                             "Low": b["low"], "Close": b["close"]})
            st.dataframe(rows, use_container_width=True, hide_index=True)


def render_snapshot(snapshot):
    symbols = snapshot.get("symbols", {})
    ts      = snapshot.get("timestamp", "")[:19].replace("T", " ")
    st.caption(f"Last fetched: {ts} UTC")

    tabs = st.tabs(list(symbols.keys()))
    for tab, (sym, tf_map) in zip(tabs, symbols.items()):
        with tab:
            # Freshness banner
            fetched_at_raw = tf_map.get("_fetched_at", "")
            fetched_at     = fetched_at_raw[:19].replace("T", " ")
            if fetched_at_raw:
                try:
                    dt = datetime.fromisoformat(fetched_at_raw[:19])
                    age_mins = (datetime.utcnow() - dt).total_seconds() / 60
                except ValueError:
                    age_mins = 9999
                label = f"Fetched: {fetched_at} UTC"
                if age_mins <= 30:
                    st.success(f"Just updated — {label}")
                elif age_mins <= 120:
                    st.info(f"Recent — {label}")
                else:
                    st.warning(f"Stale — {label}")

            summary = tf_map.get("summary")
            if summary:
                bias      = summary.get("overall_bias", "neutral")
                conv      = summary.get("conviction", "")
                align     = summary.get("tf_alignment", "")
                rev_sc    = summary.get("top_reversal_score", 0)
                rev_tf    = summary.get("top_reversal_tf", "")
                tf_scores = summary.get("tf_scores", {})

                # ── Narrative card ─────────────────────────────────────────────
                bull_tfs = summary.get("bullish_tfs", [])
                bear_tfs = summary.get("bearish_tfs", [])
                bias_icon = "🟢" if bias == "bullish" else "🔴" if bias == "bearish" else "⚪"
                top_score = max((tf_map.get(tf, {}).get("bull_bear_score", 0) for tf in TF_ORDER if isinstance(tf_map.get(tf), dict)), default=0)
                st.markdown(f"### {bias_icon} {sym} — {align}  ·  {conv} conviction")

                trend_parts = []
                if bull_tfs:
                    trend_parts.append(f"Bullish: {', '.join(TF_LABELS.get(t, t) for t in bull_tfs)}")
                if bear_tfs:
                    trend_parts.append(f"Bearish: {', '.join(TF_LABELS.get(t, t) for t in bear_tfs)}")
                if trend_parts:
                    st.markdown("  ·  ".join(trend_parts))

                # Reversal warnings per TF
                high_rev_tfs = [
                    tf for tf in TF_ORDER
                    if isinstance(tf_map.get(tf), dict) and (tf_map[tf].get("reversal_score") or 0) >= 4
                ]
                if high_rev_tfs:
                    rev_parts = []
                    for tf in high_rev_tfs:
                        rv = tf_map[tf].get("reversal_score", 0)
                        lbl = _rev_label(rv)
                        rev_parts.append(f"{TF_LABELS.get(tf, tf)} ({lbl} {rv}/10)")
                    st.warning("Reversal risk: " + "  ·  ".join(rev_parts))

                # FBD / FBO rows across all TFs
                fbd_rows = [
                    (TF_LABELS.get(tf, tf), tf_map[tf].get("fbd_levels", []))
                    for tf in TF_ORDER
                    if isinstance(tf_map.get(tf), dict) and tf_map[tf].get("fbd_levels")
                ]
                fbo_rows = [
                    (TF_LABELS.get(tf, tf), tf_map[tf].get("fbo_levels", []))
                    for tf in TF_ORDER
                    if isinstance(tf_map.get(tf), dict) and tf_map[tf].get("fbo_levels")
                ]
                for tf_label, levels in fbd_rows:
                    parts = ", ".join(f"{_price(r['level'])} ({r['source']})" if isinstance(r, dict) else _price(r) for r in levels)
                    st.success(f"FBD ({tf_label}) — failed to break below: {parts}")
                for tf_label, levels in fbo_rows:
                    parts = ", ".join(f"{_price(r['level'])} ({r['source']})" if isinstance(r, dict) else _price(r) for r in levels)
                    st.error(f"FBO ({tf_label}) — failed to break above: {parts}")

                st.divider()

                # ── Key signals table ──────────────────────────────────────────
                table_rows = []
                for tf in TF_ORDER:
                    tf_data = tf_map.get(tf)
                    if not isinstance(tf_data, dict) or "close" not in tf_data:
                        continue
                    signals = _tf_key_signals(tf, tf_data)
                    bb_sc = tf_data.get("bull_bear_score")
                    table_rows.append({
                        "TF":     TF_LABELS.get(tf, tf),
                        "Bias":   _bias_label(bb_sc) if bb_sc is not None else "—",
                        "Key Signals": "  ·  ".join(signals) if signals else "—",
                    })
                if table_rows:
                    rows_html = "".join(
                        f"<tr>"
                        f"<td style='width:90px;white-space:nowrap;padding:4px 8px;color:#555'>{r['TF']}</td>"
                        f"<td style='width:110px;white-space:nowrap;padding:4px 8px'>{r['Bias']}</td>"
                        f"<td style='padding:4px 8px;color:#444'>{r['Key Signals']}</td>"
                        f"</tr>"
                        for r in table_rows
                    )
                    st.markdown(
                        f"<table style='width:100%;border-collapse:collapse;font-size:0.85rem'>"
                        f"<thead><tr>"
                        f"<th style='width:90px;text-align:left;padding:4px 8px;border-bottom:1px solid #ddd;color:#888'>TF</th>"
                        f"<th style='width:110px;text-align:left;padding:4px 8px;border-bottom:1px solid #ddd;color:#888'>Bias</th>"
                        f"<th style='text-align:left;padding:4px 8px;border-bottom:1px solid #ddd;color:#888'>Key Signals</th>"
                        f"</tr></thead>"
                        f"<tbody>{rows_html}</tbody>"
                        f"</table>",
                        unsafe_allow_html=True
                    )

                st.divider()

            for tf in TF_ORDER:
                tf_data = tf_map.get(tf)
                if tf_data and isinstance(tf_data, dict) and "close" in tf_data:
                    render_tf_card(tf, tf_data)


# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("Trading Assistant")

    # Provider status
    st.subheader("AI Providers")
    active = _get_active_providers()
    if active:
        for p in active:
            st.success(f"✓ {p['name']} ({p['model']})")
    else:
        st.error("No API keys configured — add GROQ_API_KEY or GOOGLE_API_KEY to .env")

    st.divider()

    # Fetch market data
    st.subheader("Market Data")

    PRESET_SYMBOLS = ["ES", "SPY", "QQQ", "SMH", "IWM", "GLD", "TLT"]
    selected = st.multiselect(
        "Symbols", PRESET_SYMBOLS,
        default=["ES", "SPY"],
        help="Select presets or type custom tickers below"
    )
    custom = st.text_input("Add custom tickers (comma separated)", placeholder="AAPL, NVDA")
    if custom:
        selected += [s.strip().upper() for s in custom.split(",") if s.strip()]
    symbols = list(dict.fromkeys(selected))  # deduplicate, preserve order

    port = st.text_input("IBKR Port (optional)", placeholder="4001")

    if st.button("Fetch from IBKR", use_container_width=True, disabled=not symbols):
        cmd = [sys.executable, "scripts/market_data.py"] + symbols
        if port:
            cmd += ["--port", port]
        with st.spinner(f"Fetching {', '.join(symbols)}..."):
            result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            st.success(f"Fetched: {', '.join(symbols)}")
            st.rerun()
        else:
            st.error("Fetch failed")
            st.code(result.stderr[-1000:])

    st.divider()

    # Run AI analysis
    st.subheader("AI Analysis")

    snapshot_for_ui = load_snapshot()
    available_syms  = list(snapshot_for_ui.get("symbols", {}).keys()) if snapshot_for_ui else []

    if available_syms:
        analysis_syms = st.multiselect(
            "Symbols to analyse",
            available_syms,
            default=available_syms[:2],  # default to first two
            help="Only selected symbols are sent to the LLM"
        )
    else:
        analysis_syms = []
        st.caption("No snapshot yet — fetch data first")

    extra_ctx = st.text_area("Extra context (optional)",
                             placeholder="e.g. FOMC tomorrow, earnings today...")

    if st.button("Run Analysis", use_container_width=True, type="primary"):
        if not SNAPSHOT_PATH.exists():
            st.error("No snapshot yet — fetch market data first")
        elif not analysis_syms:
            st.error("Select at least one symbol to analyse")
        else:
            with st.spinner(f"Analysing {', '.join(analysis_syms)}..."):
                snapshot = load_snapshot()
                messages = build_messages(snapshot, extra_ctx, symbols=analysis_syms)
                reply, provider = chat(messages)
            st.session_state.analysis          = reply
            st.session_state.analysis_provider = provider
            st.session_state.analysis_messages = messages
            st.session_state.analysis_symbols  = analysis_syms
            st.session_state.chat_messages     = []
            st.rerun()

    st.divider()

    # Telegram toggle
    st.subheader("Telegram")
    tg_default = os.getenv("TELEGRAM_ENABLED", "true").strip().lower() == "true"
    tg_enabled = st.toggle("Send Telegram notifications", value=tg_default)
    if tg_enabled != tg_default:
        os.environ["TELEGRAM_ENABLED"] = "true" if tg_enabled else "false"
        import tools.telegram as _tg
        _tg.ENABLED = tg_enabled

    st.divider()
    if st.button("Clear Chat", use_container_width=True):
        st.session_state.chat_messages = []
        st.rerun()


# ── Main panel ─────────────────────────────────────────────────────────────────
snapshot = load_snapshot()

market_tab, analysis_tab = st.tabs(["Market Snapshot", "Analysis & Chat"])

# ── Tab 1: Market Snapshot ─────────────────────────────────────────────────────
with market_tab:
    if snapshot:
        render_snapshot(snapshot)
    else:
        st.info("No market data yet. Click **Fetch from IBKR** in the sidebar.")

# ── Tab 2: Analysis & Chat ─────────────────────────────────────────────────────
with analysis_tab:
    analysis = st.session_state.get("analysis")

    if analysis:
        provider        = st.session_state.get("analysis_provider", "")
        analysis_symbols = st.session_state.get("analysis_symbols", [])
        label           = f"AI Analysis — {', '.join(analysis_symbols)}" if analysis_symbols else "AI Analysis"
        st.subheader(label)
        st.markdown(analysis)
        if provider and provider != "none":
            st.caption(f"via {provider}")

        st.divider()
        st.subheader("Follow-up Questions")

        # Chat history
        if "chat_messages" not in st.session_state:
            st.session_state.chat_messages = []

        for msg in st.session_state.chat_messages:
            st.chat_message(msg["role"]).write(msg["content"])

        if prompt := st.chat_input("Ask about the analysis..."):
            # Build conversation: system + original analysis exchange + chat history
            base = st.session_state.get("analysis_messages", [
                {"role": "system", "content": SYSTEM_PROMPT}
            ])
            full_history = base + [
                {"role": "assistant", "content": analysis}
            ] + st.session_state.chat_messages + [
                {"role": "user", "content": prompt}
            ]

            st.session_state.chat_messages.append({"role": "user", "content": prompt})
            st.chat_message("user").write(prompt)

            with st.chat_message("assistant"):
                with st.spinner("Thinking..."):
                    reply, provider = chat(full_history)
                st.write(reply)
                if provider != "none":
                    st.caption(f"via {provider}")

            st.session_state.chat_messages.append({"role": "assistant", "content": reply})

    else:
        st.info("No analysis yet. Fetch market data then click **Run Analysis** in the sidebar.")
        if snapshot:
            with st.expander("Preview prompt that will be sent to LLM"):
                st.code(build_prompt(snapshot), language="text")
