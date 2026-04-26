"""
Telegram notifications for trading signals.
Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from .env
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

TF_ORDER  = ["monthly", "weekly", "daily", "2H", "15min"]
TF_LABELS = {"monthly": "Monthly", "weekly": "Weekly", "daily": "Daily",
             "2H": "2H", "15min": "15min"}


def _bias_label(score: int) -> str:
    if score >= 9:  return "Strong Bull"
    if score >= 5:  return "Bull"
    if score >= 1:  return "Mild Bull"
    if score == 0:  return "Neutral"
    if score >= -4: return "Mild Bear"
    if score >= -8: return "Bear"
    return "Strong Bear"


def _rev_label(score: int) -> str:
    if score >= 7: return "HIGH"
    if score >= 4: return "MED"
    return "LOW"


def send_message(text: str) -> bool:
    """Send a plain text message to Telegram. Returns True on success."""
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if not resp.ok:
            print(f"Telegram error: {resp.text}")
        return resp.ok
    except Exception as e:
        print(f"Telegram send failed: {e}")
        return False


def format_snapshot(snapshot: dict, symbols: list | None = None) -> str:
    """Format a market snapshot into a Telegram message."""
    all_syms  = snapshot.get("symbols", {})
    syms      = symbols or list(all_syms.keys())
    ts        = snapshot.get("timestamp", "")[:16].replace("T", " ")
    lines     = [f"<b>Trading Signals — {ts} UTC</b>"]

    for sym in syms:
        sym_data = all_syms.get(sym)
        if not sym_data:
            continue

        summary = sym_data.get("summary", {})
        bias    = summary.get("overall_bias", "neutral")
        conv    = summary.get("conviction", "")
        align   = summary.get("tf_alignment", "")
        rev_sc  = summary.get("top_reversal_score", 0)
        rev_tf  = summary.get("top_reversal_tf", "")
        tf_scores = summary.get("tf_scores", {})

        bias_icon = "🟢" if bias == "bullish" else "🔴" if bias == "bearish" else "⚪"
        lines.append("")
        lines.append(f"{bias_icon} <b>{sym}</b> — {align} · {conv} conviction")

        # Per-TF scores
        score_parts = []
        for tf in TF_ORDER:
            sc = tf_scores.get(tf)
            if sc is not None:
                lbl = TF_LABELS.get(tf, tf)
                score_parts.append(f"{lbl}: {_bias_label(sc)} ({sc:+d})")
        if score_parts:
            lines.append("  " + "  |  ".join(score_parts))

        # Reversal risk
        high_rev = [
            tf for tf in TF_ORDER
            if isinstance(sym_data.get(tf), dict)
            and (sym_data[tf].get("reversal_score") or 0) >= 4
        ]
        if high_rev:
            rv_parts = []
            for tf in high_rev:
                rv = sym_data[tf].get("reversal_score", 0)
                rv_parts.append(f"{TF_LABELS.get(tf, tf)} ({_rev_label(rv)} {rv}/10)")
            lines.append(f"  ⚠ Reversal risk: {' · '.join(rv_parts)}")

        # FBD / FBO signals
        for tf in TF_ORDER:
            tf_data = sym_data.get(tf)
            if not isinstance(tf_data, dict):
                continue
            fbd = tf_data.get("fbd_levels", [])
            fbo = tf_data.get("fbo_levels", [])
            if fbd:
                levels = ", ".join(
                    f"{r['level']:.2f} ({r['source']})" if isinstance(r, dict) else f"{r:.2f}"
                    for r in fbd
                )
                lines.append(f"  ✅ FBD ({TF_LABELS.get(tf, tf)}) failed to break below: {levels}")
            if fbo:
                levels = ", ".join(
                    f"{r['level']:.2f} ({r['source']})" if isinstance(r, dict) else f"{r:.2f}"
                    for r in fbo
                )
                lines.append(f"  🚫 FBO ({TF_LABELS.get(tf, tf)}) failed to break above: {levels}")

    return "\n".join(lines)


def notify_snapshot(snapshot: dict, symbols: list | None = None) -> bool:
    """Format and send snapshot summary to Telegram."""
    text = format_snapshot(snapshot, symbols)
    return send_message(text)
