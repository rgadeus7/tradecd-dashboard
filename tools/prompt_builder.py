"""
Builds a structured LLM prompt from the market snapshot JSON.
Highlights overextension and sideways/consolidation risks prominently.
"""

import json
from pathlib import Path

SNAPSHOT_PATH = Path(__file__).parent.parent / "data" / "market_snapshot.json"

TF_LABELS = {
    "15min":   "15-Minute",
    "2H":      "2-Hour",
    "daily":   "Daily",
    "weekly":  "Weekly",
    "monthly": "Monthly",
}

# ── Formatters ─────────────────────────────────────────────────────────────────
def _pct(v):
    if v is None:
        return "n/a"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.2f}%"


def _price(v):
    return f"{v:.2f}" if v is not None else "n/a"


def _dir(v):
    return v.upper() if v else "n/a"


def _overextension_section(overext, tf_label):
    if not overext:
        return ""

    lines = []
    flags = overext.get("flags", [])

    for key, val in overext.items():
        if key == "flags":
            continue
        ma_label = key.replace("pct_from_", "").upper()
        direction = "above" if val > 0 else "below"
        lines.append(f"  {ma_label}: {_pct(val)} {direction}")

    block = f"  Price Distance from Key Levels ({tf_label}):\n" + "\n".join(lines)

    if flags:
        warnings = [f.replace("_", " ").upper() for f in flags]
        block += f"\n  ⚠ REVERSAL RISK FLAGS: {', '.join(warnings)}"

    return block


def _sideways_section(sideways):
    if not sideways:
        return ""

    r10 = sideways.get("range_10w_pct")
    r15 = sideways.get("range_15w_pct")
    c10 = sideways.get("consolidating_10w", False)
    c15 = sideways.get("consolidating_15w", False)

    lines = []
    if r10 is not None:
        lines.append(f"  10-week range: {_pct(r10)} of price{'  ⚠ CONSOLIDATING' if c10 else ''}")
    if r15 is not None:
        lines.append(f"  15-week range: {_pct(r15)} of price{'  ⚠ CONSOLIDATING' if c15 else ''}")

    if c10 or c15:
        lines.append("  ⚠ SIDEWAYS RISK: Price has been compressing — breakout or breakdown likely.")

    return "  Sideways / Consolidation Check:\n" + "\n".join(lines)


def _structural_levels(tf_data):
    lines = []

    for key, label in [
        ("last_5_days",     "Last 5 Days"),
        ("last_5_weeks",    "Last 5 Weeks"),
        ("last_5_months",   "Last 5 Months"),
        ("last_5_quarters", "Last 5 Quarters"),
    ]:
        bars = tf_data.get(key)
        if not bars:
            continue
        lines.append(f"  {label}:")
        for b in bars:
            date = b.get("date") or b.get("week") or b.get("month") or b.get("quarter", "")
            lines.append(f"    {date}  H:{_price(b['high'])}  L:{_price(b['low'])}  C:{_price(b['close'])}")

    return "\n".join(lines)


def _tf_block(tf, tf_data):
    label = TF_LABELS.get(tf, tf)
    close = tf_data.get("close")
    st    = tf_data.get("supertrend")
    st_dir = tf_data.get("supertrend_dir")
    rsi   = tf_data.get("rsi")
    atr   = tf_data.get("atr14")

    lines = [f"\n── {label} ──────────────────────────────────────────"]
    lines.append(f"  Close: {_price(close)}")

    # MAs
    ma_parts = []
    for ma in ["ma20", "ma50", "ma200"]:
        v = tf_data.get(ma)
        if v is not None:
            ma_parts.append(f"{ma.upper()}: {_price(v)}")
    if ma_parts:
        lines.append(f"  {' | '.join(ma_parts)}")

    # SuperTrend + RSI + ATR
    lines.append(f"  SuperTrend: {_price(st)} ({_dir(st_dir)})")
    lines.append(f"  RSI(14): {_price(rsi)}")
    if atr is not None:
        lines.append(f"  ATR(14): {_price(atr)}")

    # Overextension
    overext = tf_data.get("overextension")
    if overext:
        lines.append(_overextension_section(overext, label))

    # Sideways (weekly only)
    sideways = tf_data.get("sideways")
    if sideways:
        lines.append(_sideways_section(sideways))

    # FBD / FBO signals
    fbd = tf_data.get("fbd_levels", [])
    fbo = tf_data.get("fbo_levels", [])
    if fbd:
        levels = ", ".join(f"{v:.2f}" for v in fbd)
        lines.append(f"  ✓ FAILED BREAKDOWN (bullish): price rejected below {levels}")
    if fbo:
        levels = ", ".join(f"{v:.2f}" for v in fbo)
        lines.append(f"  ✗ FAILED BREAKOUT (bearish): price rejected above {levels}")

    # Structural levels
    struct = _structural_levels(tf_data)
    if struct:
        lines.append(struct)

    return "\n".join(lines)


# ── Main builder ───────────────────────────────────────────────────────────────
def build_prompt(snapshot=None, extra_context="", symbols=None):
    """
    Build the full LLM prompt from snapshot dict or file.
    symbols: list of tickers to include e.g. ["ES", "SPY"]
             If None, all symbols in the snapshot are included.
    Returns a string ready to send as the user message.
    """
    if snapshot is None:
        if not SNAPSHOT_PATH.exists() or SNAPSHOT_PATH.stat().st_size == 0:
            return "No market data available — fetch from IBKR first."
        try:
            with open(SNAPSHOT_PATH) as f:
                snapshot = json.load(f)
        except json.JSONDecodeError:
            return "Snapshot file is corrupt — fetch from IBKR to regenerate."

    timestamp      = snapshot.get("timestamp", "unknown")
    all_symbols    = snapshot.get("symbols", {})

    # Filter to requested symbols only
    if symbols:
        symbols_data = {s: all_symbols[s] for s in symbols if s in all_symbols}
        missing = [s for s in symbols if s not in all_symbols]
        if missing:
            print(f"Warning: symbols not in snapshot: {missing}")
    else:
        symbols_data = all_symbols

    symbols = symbols_data

    prompt_parts = [
        f"Market Snapshot — {timestamp}",
        "=" * 60,
        "",
        "You are a quantitative trading analyst. Analyze the data below and provide:",
        "1. Overall directional bias (bullish / bearish / neutral) with reasoning",
        "2. Key support and resistance levels (from structural highs/lows)",
        "3. Entry level and trigger condition",
        "4. Stop loss — use ATR14 from daily timeframe for sizing",
        "5. Target levels (T1, T2, T3)",
        "6. Failed breakdown / breakout signals if any levels were recently violated",
        "7. Reversal risk assessment — flag if price is overextended or in consolidation",
        "",
        "Rules:",
        "- If ⚠ REVERSAL RISK or ⚠ CONSOLIDATING flags are present, address them explicitly",
        "- If multiple timeframes conflict, state the conflict and which TF takes precedence",
        "- Be concise — use bullet points",
        "=" * 60,
    ]

    for sym, tf_map in symbols_data.items():
        prompt_parts.append(f"\n{'=' * 20} {sym} {'=' * 20}")
        for tf in ["monthly", "weekly", "daily", "2H", "15min"]:
            tf_data = tf_map.get(tf)
            if tf_data:
                prompt_parts.append(_tf_block(tf, tf_data))

    if extra_context:
        prompt_parts += ["", "─" * 40, "Additional context:", extra_context]

    return "\n".join(prompt_parts)


def build_messages(snapshot=None, extra_context="", symbols=None):
    """Return messages list ready for ai_client.chat()."""
    from tools.ai_client import SYSTEM_PROMPT
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": build_prompt(snapshot, extra_context, symbols)},
    ]


if __name__ == "__main__":
    print(build_prompt())
