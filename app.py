import json
import subprocess
import sys
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
    if SNAPSHOT_PATH.exists():
        with open(SNAPSHOT_PATH) as f:
            return json.load(f)
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


# ── Snapshot display ───────────────────────────────────────────────────────────
def render_tf_card(tf, tf_data):
    label      = TF_LABELS.get(tf, tf)
    close      = tf_data.get("close")
    st_val     = tf_data.get("supertrend")
    st_dir     = tf_data.get("supertrend_dir", "")
    rsi        = tf_data.get("rsi")
    atr        = tf_data.get("atr14")
    overext    = tf_data.get("overextension", {})
    sideways   = tf_data.get("sideways", {})
    flags      = overext.get("flags", [])
    c10        = sideways.get("consolidating_10w", False)
    c15        = sideways.get("consolidating_15w", False)

    with st.expander(f"**{label}** — Close: {_price(close)}  |  SuperTrend: {st_dir.upper() if st_dir else '—'}  |  RSI: {_price(rsi)}", expanded=(tf == "daily")):

        # Warnings at top
        if flags:
            st.warning("⚠ Reversal Risk: " + ", ".join(f.replace("_", " ").title() for f in flags))
        if c10 or c15:
            r10 = sideways.get("range_10w_pct")
            r15 = sideways.get("range_15w_pct")
            msg = "⚠ Sideways / Consolidation — "
            parts = []
            if r10: parts.append(f"10w range: {_pct(r10)}")
            if r15: parts.append(f"15w range: {_pct(r15)}")
            st.warning(msg + " | ".join(parts) + " — breakout or breakdown likely")

        # Key metrics row
        cols = st.columns(4)
        cols[0].metric("Close",       _price(close))
        cols[1].metric("SuperTrend",  _price(st_val), delta=st_dir, delta_color="normal" if st_dir == "bullish" else "inverse")
        cols[2].metric("RSI(14)",     _price(rsi))
        if atr:
            cols[3].metric("ATR(14)", _price(atr))

        # MAs
        ma_cols = st.columns(3)
        for i, ma in enumerate(["ma20", "ma50", "ma200"]):
            val = tf_data.get(ma)
            pct = overext.get(f"pct_from_{ma}")
            if val is not None:
                ma_cols[i].metric(ma.upper(), _price(val), delta=_pct(pct) if pct else None,
                                  delta_color="normal" if (pct or 0) > 0 else "inverse")

        # Overextension detail
        if overext and any(k != "flags" for k in overext):
            st.caption("Distance from key levels: " + "  |  ".join(
                f"{k.replace('pct_from_','').upper()}: {_pct(v)}"
                for k, v in overext.items() if k != "flags" and v is not None
            ))

        # Structural levels
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

    last_fetched = snapshot.get("last_fetched", [])
    tabs = st.tabs(list(symbols.keys()))
    for tab, (sym, tf_map) in zip(tabs, symbols.items()):
        with tab:
            fetched_at = tf_map.get("_fetched_at", "")[:19].replace("T", " ")
            fresh = sym in last_fetched
            if fetched_at:
                label = f"Fetched: {fetched_at} UTC"
                if fresh:
                    st.success(f"Just updated — {label}")
                else:
                    st.warning(f"Stale — {label}  (not in last fetch)")
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
