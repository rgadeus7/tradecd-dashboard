"""
Scoring engine — bull/bear score + reversal score per timeframe.

Bull/Bear Score (-10 to +10):
  MA8 > MA20          ±1
  MA20 > MA50         ±1
  MA50 > MA200        ±1
  Price > MA200       ±1
  SuperTrend          ±2
  RSI > 50            ±1
  RSI > RSI MA        ±2
  BB position > 50    ±1
  Max per TF:         ±10

Reversal Score (0 to 10):
  BB band breaches, RSI extremes, MA overextension, sideways flags.
  Direction: "upside" (too high) or "downside" (too low/stretched).

Summary per symbol:
  tf_alignment  — how many TFs agree on direction e.g. "4/5 bullish"
  conviction    — high / medium / low / conflicted
  top_reversal  — which TF has highest reversal risk
"""

from __future__ import annotations


TF_ORDER = ["monthly", "weekly", "daily", "2H", "15min"]


# ── Bull/Bear scoring ──────────────────────────────────────────────────────────
def _bull_bear(tf_data: dict) -> tuple[int, list]:
    score     = 0
    breakdown = []

    def vote(condition, bull_reason, bear_reason, weight=1):
        nonlocal score
        if condition:
            score += weight
            breakdown.append({"reason": bull_reason, "points": weight})
        else:
            score -= weight
            breakdown.append({"reason": bear_reason, "points": -weight})

    close = tf_data.get("close") or 0
    ma8   = tf_data.get("ma8")
    ma20  = tf_data.get("ma20")
    ma50  = tf_data.get("ma50")
    ma200 = tf_data.get("ma200")
    st_dir = tf_data.get("supertrend_dir")
    rsi    = tf_data.get("rsi")
    rsi_ma = tf_data.get("rsi_ma")
    bb_pos = tf_data.get("bb", {}).get("position")

    if ma8 and ma20:
        vote(ma8 > ma20, "MA8 > MA20", "MA8 < MA20")
    if ma20 and ma50:
        vote(ma20 > ma50, "MA20 > MA50", "MA20 < MA50")
    if ma50 and ma200:
        vote(ma50 > ma200, "MA50 > MA200", "MA50 < MA200")
    if close and ma200:
        vote(close > ma200, "Price > MA200", "Price < MA200")

    if st_dir == "bullish":
        score += 2
        breakdown.append({"reason": "SuperTrend bullish", "points": 2})
    elif st_dir == "bearish":
        score -= 2
        breakdown.append({"reason": "SuperTrend bearish", "points": -2})

    if rsi is not None:
        vote(rsi > 50, f"RSI > 50 ({rsi:.1f})", f"RSI < 50 ({rsi:.1f})")
    if rsi is not None and rsi_ma is not None:
        vote(rsi > rsi_ma, f"RSI > RSI MA ({rsi:.1f} > {rsi_ma:.1f})",
             f"RSI < RSI MA ({rsi:.1f} < {rsi_ma:.1f})", weight=2)

    if bb_pos is not None:
        vote(bb_pos > 50, f"BB position upper half ({bb_pos:.0f}%)",
             f"BB position lower half ({bb_pos:.0f}%)")

    return max(-10, min(10, score)), breakdown


# ── Reversal scoring ───────────────────────────────────────────────────────────
def _reversal(tf_data: dict) -> tuple[int, str, list]:
    """Returns (score 0-10, direction 'upside'|'downside'|'none', breakdown list)."""
    score     = 0
    up_score  = 0
    dn_score  = 0
    breakdown = []   # list of {"reason": str, "points": int, "side": "up"|"dn"|"both"}

    def _add(side, pts, reason):
        nonlocal up_score, dn_score
        if side == "up":
            up_score += pts
        else:
            dn_score += pts
        breakdown.append({"reason": reason, "points": pts, "side": side})

    close  = tf_data.get("close") or 0
    high   = tf_data.get("current_high") or close
    low    = tf_data.get("current_low")  or close
    rsi    = tf_data.get("rsi")
    bb     = tf_data.get("bb", {})
    overxt = tf_data.get("overextension", {})
    flags  = overxt.get("flags", [])
    sw     = tf_data.get("sideways", {})

    # BB band breaches — check close AND high/low wick touches
    upper_3  = bb.get("upper_3")
    upper_25 = bb.get("upper_25")
    upper_2  = bb.get("upper_2")
    lower_3  = bb.get("lower_3")
    lower_25 = bb.get("lower_25")
    lower_2  = bb.get("lower_2")

    if upper_3  and close >= upper_3:    _add("up", 3, f"Close above BB 3SD upper ({upper_3:.2f})")
    elif upper_3  and high >= upper_3:   _add("up", 2, f"Wick above BB 3SD upper ({upper_3:.2f})")
    elif upper_25 and close >= upper_25: _add("up", 2, f"Close above BB 2.5SD upper ({upper_25:.2f})")
    elif upper_25 and high >= upper_25:  _add("up", 1, f"Wick above BB 2.5SD upper ({upper_25:.2f})")
    elif upper_2  and high >= upper_2:   _add("up", 1, f"Wick above BB 2SD upper ({upper_2:.2f})")
    elif bb.get("position", 50) > 80:    _add("up", 1, f"BB position high ({bb.get('position'):.0f}%)")

    if lower_3  and close <= lower_3:    _add("dn", 3, f"Close below BB 3SD lower ({lower_3:.2f})")
    elif lower_3  and low <= lower_3:    _add("dn", 2, f"Wick below BB 3SD lower ({lower_3:.2f})")
    elif lower_25 and close <= lower_25: _add("dn", 2, f"Close below BB 2.5SD lower ({lower_25:.2f})")
    elif lower_25 and low <= lower_25:   _add("dn", 1, f"Wick below BB 2.5SD lower ({lower_25:.2f})")
    elif lower_2  and low <= lower_2:    _add("dn", 1, f"Wick below BB 2SD lower ({lower_2:.2f})")
    elif bb.get("position", 50) < 20:    _add("dn", 1, f"BB position low ({bb.get('position'):.0f}%)")

    # RSI extremes
    if rsi is not None:
        if rsi >= 80:    _add("up", 2, f"RSI extremely overbought ({rsi:.1f})")
        elif rsi >= 70:  _add("up", 1, f"RSI overbought ({rsi:.1f})")
        if rsi <= 20:    _add("dn", 2, f"RSI extremely oversold ({rsi:.1f})")
        elif rsi <= 30:  _add("dn", 1, f"RSI oversold ({rsi:.1f})")

    # MA overextension flags
    if "overextended_from_ma200" in flags:
        pct = overxt.get("pct_from_ma200", 0)
        if pct > 0: _add("up", 1, f"Overextended above MA200 ({pct:+.1f}%)")
        else:       _add("dn", 1, f"Overextended below MA200 ({pct:+.1f}%)")
    if "overextended_from_ma50" in flags:
        pct = overxt.get("pct_from_ma50", 0)
        if pct > 0: _add("up", 1, f"Overextended above MA50 ({pct:+.1f}%)")
        else:       _add("dn", 1, f"Overextended below MA50 ({pct:+.1f}%)")

    # FBD / FBO
    fbd = tf_data.get("fbd_levels", [])
    fbo = tf_data.get("fbo_levels", [])
    if fbd:
        pts = min(len(fbd) * 2, 4)
        levels = ", ".join(f"{v:.2f}" for v in fbd)
        _add("dn", pts, f"FBD at {levels}")
    if fbo:
        pts = min(len(fbo) * 2, 4)
        levels = ", ".join(f"{v:.2f}" for v in fbo)
        _add("up", pts, f"FBO at {levels}")

    # Sideways / consolidation (adds to both sides equally)
    if sw.get("consolidating_15w"):
        score += 2
        breakdown.append({"reason": "Consolidating 15w (breakout/breakdown risk)", "points": 2, "side": "both"})
    elif sw.get("consolidating_10w"):
        score += 1
        breakdown.append({"reason": "Consolidating 10w", "points": 1, "side": "both"})

    up_score  = min(up_score,  10)
    dn_score  = min(dn_score,  10)
    base      = max(up_score, dn_score)
    total     = min(base + score, 10)

    if up_score > dn_score:
        direction = "too high"
    elif dn_score > up_score:
        direction = "too low"
    else:
        direction = "none"

    return total, direction, breakdown


# ── Conviction label ───────────────────────────────────────────────────────────
def _conviction(bullish_count: int, total: int) -> str:
    bearish_count = total - bullish_count
    majority = max(bullish_count, bearish_count)
    if majority == total:       return "high"
    if majority >= total * 0.8: return "good"
    if majority >= total * 0.6: return "medium"
    return "conflicted"


# ── Main scorer ────────────────────────────────────────────────────────────────
def score_symbol(sym_data: dict) -> dict:
    """
    Score all timeframes for one symbol.
    Returns enriched sym_data with scores added in-place + a 'summary' key.
    """
    tf_scores     = {}
    rev_scores    = {}
    bullish_tfs   = []
    bearish_tfs   = []
    available_tfs = [tf for tf in TF_ORDER if tf in sym_data and isinstance(sym_data[tf], dict) and "close" in sym_data[tf]]

    for tf in available_tfs:
        tf_data = sym_data[tf]

        bb_score, bb_breakdown           = _bull_bear(tf_data)
        rv_score, rv_dir, rv_breakdown  = _reversal(tf_data)

        tf_data["bull_bear_score"]      = bb_score
        tf_data["bull_bear_breakdown"]  = bb_breakdown
        tf_data["reversal_score"]       = rv_score
        tf_data["reversal_dir"]         = rv_dir
        tf_data["reversal_breakdown"]   = rv_breakdown

        tf_scores[tf]  = bb_score
        rev_scores[tf] = (rv_score, rv_dir)

        if bb_score > 0:
            bullish_tfs.append(tf)
        elif bb_score < 0:
            bearish_tfs.append(tf)

    total_tfs     = len(available_tfs)
    bullish_count = len(bullish_tfs)
    bearish_count = len(bearish_tfs)

    overall_bias = (
        "bullish" if bullish_count > bearish_count
        else "bearish" if bearish_count > bullish_count
        else "neutral"
    )

    alignment_count = bullish_count if overall_bias == "bullish" else bearish_count
    alignment_str   = f"{alignment_count}/{total_tfs} {overall_bias}"

    # TF with highest reversal risk
    top_rev_tf    = max(rev_scores, key=lambda t: rev_scores[t][0]) if rev_scores else None
    top_rev_score = rev_scores[top_rev_tf][0] if top_rev_tf else 0
    top_rev_dir   = rev_scores[top_rev_tf][1] if top_rev_tf else "none"

    rev_label = (
        "high"   if top_rev_score >= 7
        else "medium" if top_rev_score >= 4
        else "low"
    )

    sym_data["summary"] = {
        "tf_alignment":     alignment_str,
        "conviction":       _conviction(bullish_count, total_tfs),
        "overall_bias":     overall_bias,
        "bullish_tfs":      bullish_tfs,
        "bearish_tfs":      bearish_tfs,
        "top_reversal_tf":  top_rev_tf,
        "top_reversal_score": top_rev_score,
        "top_reversal_dir": top_rev_dir,
        "reversal_label":   rev_label,
        "tf_scores":        tf_scores,
    }

    return sym_data


def score_snapshot(snapshot: dict) -> dict:
    """Score all symbols in a snapshot. Mutates and returns snapshot."""
    for sym, sym_data in snapshot.get("symbols", {}).items():
        if isinstance(sym_data, dict):
            score_symbol(sym_data)
    return snapshot


# ── Telegram formatter ─────────────────────────────────────────────────────────
def _bar(score: int, width: int = 10) -> str:
    filled = abs(score)
    empty  = width - filled
    return "+" * filled + "." * empty if score >= 0 else "-" * filled + "." * empty


def format_telegram(snapshot: dict, symbols: list[str] | None = None) -> str:
    all_syms = snapshot.get("symbols", {})
    syms     = symbols or list(all_syms.keys())
    ts       = snapshot.get("timestamp", "")[:16].replace("T", " ")
    lines    = [f"Trading Signals — {ts} UTC", ""]

    for sym in syms:
        sym_data = all_syms.get(sym)
        if not sym_data:
            continue

        summary = sym_data.get("summary", {})
        bias    = summary.get("overall_bias", "neutral").upper()
        conv    = summary.get("conviction", "").upper()
        align   = summary.get("tf_alignment", "")
        rev_tf  = summary.get("top_reversal_tf", "")
        rev_sc  = summary.get("top_reversal_score", 0)
        rev_dir = summary.get("top_reversal_dir", "")
        rev_lbl = summary.get("reversal_label", "").upper()

        lines.append(f"{'='*28}")
        lines.append(f"{sym} — {bias} | {conv} conviction")
        lines.append(f"{'='*28}")

        # Per-TF scores
        tf_scores = summary.get("tf_scores", {})
        for tf in TF_ORDER:
            if tf not in tf_scores:
                continue
            sc    = tf_scores[tf]
            rv    = sym_data.get(tf, {}).get("reversal_score", 0)
            rv_d  = sym_data.get(tf, {}).get("reversal_dir", "")
            rev_flag = f" [REV:{rv}/10 {rv_d}]" if rv >= 4 else ""
            lines.append(f"  {tf:<8} {sc:+3d}  {_bar(sc)}{rev_flag}")

        lines.append("")
        lines.append(f"Alignment : {align}")

        # Reversal warning
        if rev_sc >= 4:
            lines.append(f"Reversal  : {rev_lbl} ({rev_sc}/10) on {rev_tf} [{rev_dir}]")
        else:
            lines.append(f"Reversal  : {rev_lbl} ({rev_sc}/10)")

        lines.append("")

    return "\n".join(lines).strip()


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    path = Path(__file__).parent.parent / "data" / "market_snapshot.json"
    if not path.exists() or path.stat().st_size == 0:
        print("No snapshot data found — fetch from IBKR first.")
        sys.exit(0)
    with open(path) as f:
        snap = json.load(f)

    snap = score_snapshot(snap)

    print(format_telegram(snap))
    print("\n--- Raw summary ---")
    for sym, data in snap.get("symbols", {}).items():
        print(f"\n{sym}:", json.dumps(data.get("summary"), indent=2))
