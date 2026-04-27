"""
Builds a structured LLM prompt from the market snapshot JSON.
Highlights overextension and sideways/consolidation risks prominently.
"""

import json
from pathlib import Path

SNAPSHOT_PATH         = Path(__file__).parent.parent / "data" / "market_snapshot.json"
OPTIONS_SNAPSHOT_PATH = Path(__file__).parent.parent / "data" / "options_snapshot.json"

TF_LABELS = {
    "15min":   "15-Minute",
    "2H":      "2-Hour",
    "daily":   "Daily",
    "weekly":  "Weekly",
    "monthly": "Monthly",
}

# ── Analysis profiles ──────────────────────────────────────────────────────────
PROFILES = {
    "intraday": {
        "label":      "Intraday",
        "timeframes": ["15min", "2H"],
        "instructions": [
            "You are analyzing for INTRADAY trading (same-day entries and exits).",
            "1. Intraday bias (bullish / bearish / neutral) based on 15min and 2H structure",
            "2. Key intraday support and resistance levels",
            "3. Specific entry level and trigger (e.g. break above X, bounce from Y)",
            "4. Stop loss — tight, use 15min ATR or nearest S/R",
            "5. Intraday targets T1 and T2 — realistic for the session",
            "6. Options gamma levels (call wall, put wall, gamma flip) as magnetic levels",
            "7. Flag any reversal risk or overextension on 15min or 2H",
            "",
            "Rules:",
            "- Focus on price action today — ignore multi-week structure",
            "- Keep it tight and actionable — entries must be specific levels, not ranges",
            "- If 15min and 2H conflict, state which takes precedence and why",
            "- Be concise — bullet points only",
        ],
    },
    "swing": {
        "label":      "Swing",
        "timeframes": ["daily", "weekly", "monthly"],
        "instructions": [
            "You are analyzing for SWING trading (multi-day to multi-week holds).",
            "1. Overall directional bias across Daily / Weekly / Monthly",
            "2. Key swing support and resistance levels (structural highs/lows)",
            "3. Entry level — ideal pullback zone or breakout trigger",
            "4. Stop loss — use Daily ATR14 for sizing",
            "5. Swing targets T1, T2, T3 — based on weekly/monthly structure",
            "6. MA alignment (MA20/50/200) — confirm or warn against the trade",
            "7. Reversal risk — flag overextension or consolidation breakout risk",
            "8. Failed breakdown/breakout signals if present",
            "",
            "Rules:",
            "- Think in terms of days to weeks, not hours",
            "- Weekly and Monthly context takes precedence over Daily noise",
            "- If multiple timeframes conflict, state the conflict explicitly",
            "- Be concise — bullet points only",
        ],
    },
    "overnight": {
        "label":      "Overnight / Futures Open",
        "timeframes": ["2H", "daily"],
        "instructions": [
            "You are analyzing for OVERNIGHT and FUTURES OPEN positioning.",
            "1. Overnight bias — likely direction heading into next session",
            "2. Gap risk — is price near a level that could cause a gap open?",
            "3. Key levels to watch at the open (support, resistance, overnight range)",
            "4. Options levels (call wall, put wall, gamma flip) — where will price be pinned or repelled?",
            "5. If bullish open: first resistance. If bearish open: first support.",
            "6. Any reversal risk or overextension building overnight",
            "",
            "Rules:",
            "- Focus on the next 12-18 hours, not multi-day",
            "- Overnight range and 2H structure are most important",
            "- Be concise — bullet points only",
        ],
    },
    "full": {
        "label":      "Full Analysis",
        "timeframes": ["monthly", "weekly", "daily", "2H", "15min"],
        "instructions": [
            "You are a quantitative trading analyst. Analyze the data below and provide:",
            "1. Overall directional bias (bullish / bearish / neutral) with reasoning",
            "2. Key support and resistance levels (from structural highs/lows)",
            "3. Entry level and trigger condition",
            "4. Stop loss — use ATR14 from daily timeframe for sizing",
            "5. Target levels (T1, T2, T3)",
            "6. Failed breakdown / breakout signals if any levels were recently violated",
            "7. Reversal risk assessment — flag if price is overextended or in consolidation",
            "8. If options data is present: reference call wall, put wall, and gamma flip as key S/R levels",
            "",
            "Rules:",
            "- If ⚠ REVERSAL RISK or ⚠ CONSOLIDATING flags are present, address them explicitly",
            "- If multiple timeframes conflict, state the conflict and which TF takes precedence",
            "- Options levels (call wall, put wall, gamma flip) are strong magnetic levels — use them",
            "- Be concise — use bullet points",
        ],
    },
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

    # Murrey Math
    mm = tf_data.get("murrey", {})
    if mm:
        mzone = mm.get("zone_label", "Normal")
        lines.append(
            f"  Murrey Math: {mzone}  |  "
            f"8/8: {_price(mm.get('eight_eight'))}  4/8: {_price(mm.get('four_eight'))}  "
            f"0/8: {_price(mm.get('zero_eight'))}  +1/8: {_price(mm.get('plus_18'))}  "
            f"-1/8: {_price(mm.get('minus_18'))}"
        )

    # FBD / FBO signals
    fbd = tf_data.get("fbd_levels", [])
    fbo = tf_data.get("fbo_levels", [])
    if fbd:
        levels = ", ".join(f"{r['level']:.2f} ({r['source']})" for r in fbd)
        lines.append(f"  ✓ FAILED BREAKDOWN (bullish): failed to break below {levels}")
    if fbo:
        levels = ", ".join(f"{r['level']:.2f} ({r['source']})" for r in fbo)
        lines.append(f"  ✗ FAILED BREAKOUT (bearish): failed to break above {levels}")

    # Structural levels
    struct = _structural_levels(tf_data)
    if struct:
        lines.append(struct)

    return "\n".join(lines)


def _options_block(sym, sym_opts):
    lines = [f"\n── {sym} Options ──────────────────────────────────────────"]
    spot  = sym_opts.get("spot")
    if spot:
        lines.append(f"  Spot: {_price(spot)}")
    for exp_key, m in sorted(sym_opts.get("expiries", {}).items()):
        lines.append(
            f"\n  Expiry: {m.get('expiry_fmt', exp_key)}  DTE={m.get('dte', '?')}"
        )
        cw  = m.get("call_wall")
        pw  = m.get("put_wall")
        gf  = m.get("gamma_flip")
        mp  = m.get("max_pain")
        pcr = m.get("pcr")
        iv  = m.get("iv_pct")
        std = m.get("straddle")
        above = m.get("above_gamma_flip")
        gf_note = (" (above → stable)" if above else " (below → volatile)") if above is not None else ""
        lines.append(f"    Call Wall (resistance): {_price(cw) if cw else 'n/a'}")
        lines.append(f"    Put Wall  (support):    {_price(pw) if pw else 'n/a'}")
        lines.append(f"    Gamma Flip: {_price(gf) if gf else 'n/a'}{gf_note}")
        lines.append(f"    Max Pain:   {_price(mp) if mp else 'n/a'}")
        lines.append(f"    PCR ({m.get('pcr_basis','oi')}): {pcr if pcr else 'n/a'}  "
                     f"IV: {iv}%  Straddle: {_price(std) if std else 'n/a'}")
        tcs = m.get("top_call_strikes", [])
        tps = m.get("top_put_strikes",  [])
        if tcs:
            lines.append(f"    Top call strikes: {', '.join(str(s) for s in tcs)}")
        if tps:
            lines.append(f"    Top put strikes:  {', '.join(str(s) for s in tps)}")
    return "\n".join(lines)


# ── Main builder ───────────────────────────────────────────────────────────────
def build_prompt(snapshot=None, extra_context="", symbols=None, profile="full"):
    """
    Build the full LLM prompt from snapshot dict or file.
    symbols: list of tickers to include e.g. ["ES", "SPY"]
    profile: "intraday" | "swing" | "overnight" | "full"
    """
    if snapshot is None:
        if not SNAPSHOT_PATH.exists() or SNAPSHOT_PATH.stat().st_size == 0:
            return "No market data available — fetch from IBKR first."
        try:
            with open(SNAPSHOT_PATH) as f:
                snapshot = json.load(f)
        except json.JSONDecodeError:
            return "Snapshot file is corrupt — fetch from IBKR to regenerate."

    timestamp   = snapshot.get("timestamp", "unknown")
    all_symbols = snapshot.get("symbols", {})

    prof = PROFILES.get(profile, PROFILES["full"])
    tfs  = prof["timeframes"]

    if symbols:
        symbols_data = {s: all_symbols[s] for s in symbols if s in all_symbols}
        missing = [s for s in symbols if s not in all_symbols]
        if missing:
            print(f"Warning: symbols not in snapshot: {missing}")
    else:
        symbols_data = all_symbols

    prompt_parts = [
        f"Market Snapshot — {timestamp}  [{prof['label']} Profile]",
        "=" * 60,
        "",
    ] + prof["instructions"] + ["=" * 60]

    # Load options snapshot if available
    opts_snap = None
    if OPTIONS_SNAPSHOT_PATH.exists() and OPTIONS_SNAPSHOT_PATH.stat().st_size > 0:
        try:
            with open(OPTIONS_SNAPSHOT_PATH) as f:
                opts_snap = json.load(f)
        except json.JSONDecodeError:
            pass

    for sym, tf_map in symbols_data.items():
        prompt_parts.append(f"\n{'=' * 20} {sym} {'=' * 20}")
        for tf in tfs:
            tf_data = tf_map.get(tf)
            if tf_data:
                prompt_parts.append(_tf_block(tf, tf_data))

        # Options section for this symbol
        if opts_snap:
            sym_opts = opts_snap.get("symbols", {}).get(sym)
            if sym_opts:
                prompt_parts.append(_options_block(sym, sym_opts))

    if extra_context:
        prompt_parts += ["", "─" * 40, "Additional context:", extra_context]

    return "\n".join(prompt_parts)


def build_messages(snapshot=None, extra_context="", symbols=None, profile="full"):
    """Return messages list ready for ai_client.chat()."""
    from tools.ai_client import SYSTEM_PROMPT
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": build_prompt(snapshot, extra_context, symbols, profile)},
    ]


if __name__ == "__main__":
    print(build_prompt())
