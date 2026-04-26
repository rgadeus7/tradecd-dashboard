"""
Telegram notifications for trading signals.
Set TELEGRAM_ENABLED=false in .env to disable all notifications.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
ENABLED   = os.getenv("TELEGRAM_ENABLED", "true").strip().lower() == "true"

TF_ORDER  = ["monthly", "weekly", "daily", "2H", "15min"]
TF_LABELS = {"monthly": "Monthly", "weekly": "Weekly", "daily": "Daily",
             "2H": "2H", "15min": "15min"}

SIGNAL_SYSTEM_PROMPT = """You are a concise trading signal generator.
Given market snapshot data, output ONLY the following format — no extra commentary:

BIAS: [BULL/BEAR/NEUTRAL] | Conviction: [high/medium/low]

BULL SETUP (if bullish):
Entry: [price and reason]
Stop: [price] (-[points/pct] risk)
T1: [price] | T2: [price] | T3: [price]

REVERSAL SETUP (only if reversal score >= 5):
Direction: [long/short]
Entry: [price and trigger]
Stop: [price]
T1: [price] | T2: [price]

KEY LEVELS: [2-3 most important support/resistance levels]
WATCH: [one line — main risk or condition to watch]

Keep it under 200 words. Use actual price numbers from the data."""


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
    """Send a message to Telegram. Returns True on success."""
    if not ENABLED:
        print("Telegram disabled (TELEGRAM_ENABLED=false)")
        return False
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
    """Format bias summary for all fetched symbols."""
    all_syms = snapshot.get("symbols", {})
    syms     = symbols or list(all_syms.keys())
    ts       = snapshot.get("timestamp", "")[:16].replace("T", " ")
    lines    = [f"<b>Market Update — {ts} UTC</b>"]

    for sym in syms:
        sym_data = all_syms.get(sym)
        if not sym_data:
            continue

        summary   = sym_data.get("summary", {})
        bias      = summary.get("overall_bias", "neutral")
        conv      = summary.get("conviction", "")
        align     = summary.get("tf_alignment", "")
        tf_scores = summary.get("tf_scores", {})

        bias_icon = "🟢" if bias == "bullish" else "🔴" if bias == "bearish" else "⚪"
        lines.append("")
        lines.append(f"{bias_icon} <b>{sym}</b> — {align} · {conv} conviction")

        # Per-TF scores
        score_parts = []
        for tf in TF_ORDER:
            sc = tf_scores.get(tf)
            if sc is not None:
                score_parts.append(f"{TF_LABELS[tf]}: {_bias_label(sc)} ({sc:+d})")
        if score_parts:
            lines.append("  " + "  |  ".join(score_parts))

        # Reversal risk
        high_rev = [
            tf for tf in TF_ORDER
            if isinstance(sym_data.get(tf), dict)
            and (sym_data[tf].get("reversal_score") or 0) >= 4
        ]
        if high_rev:
            rv_parts = [
                f"{TF_LABELS[tf]} ({_rev_label(sym_data[tf]['reversal_score'])} {sym_data[tf]['reversal_score']}/10)"
                for tf in high_rev
            ]
            lines.append(f"  ⚠ Reversal: {' · '.join(rv_parts)}")

        # FBD / FBO
        for tf in TF_ORDER:
            tf_data = sym_data.get(tf)
            if not isinstance(tf_data, dict):
                continue
            fbd = tf_data.get("fbd_levels", [])
            fbo = tf_data.get("fbo_levels", [])
            if fbd:
                levels = ", ".join(
                    f"{r['level']:.2f}({r['source']})" if isinstance(r, dict) else f"{r:.2f}"
                    for r in fbd
                )
                lines.append(f"  ✅ FBD ({TF_LABELS[tf]}): {levels}")
            if fbo:
                levels = ", ".join(
                    f"{r['level']:.2f}({r['source']})" if isinstance(r, dict) else f"{r:.2f}"
                    for r in fbo
                )
                lines.append(f"  🚫 FBO ({TF_LABELS[tf]}): {levels}")

    return "\n".join(lines)


def get_ai_signal(snapshot: dict, symbols: list | None = None) -> str | None:
    """Run AI analysis and return a short signal string, or None on failure."""
    try:
        import sys
        from pathlib import Path
        root = str(Path(__file__).parent.parent)
        if root not in sys.path:
            sys.path.insert(0, root)
        from tools.prompt_builder import build_prompt
        from tools.ai_client import chat

        prompt = build_prompt(snapshot, symbols=symbols)
        messages = [
            {"role": "system", "content": SIGNAL_SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ]
        reply, provider = chat(messages)
        if provider == "none":
            return None
        return reply
    except Exception as e:
        print(f"AI signal generation failed: {e}")
        return None


def notify_snapshot(snapshot: dict, symbols: list | None = None) -> None:
    """Send bias summary + AI signal to Telegram."""
    if not ENABLED:
        print("Telegram disabled — skipping notification")
        return

    # 1. Send bias summary
    summary_text = format_snapshot(snapshot, symbols)
    send_message(summary_text)

    # 2. Send AI signal for each symbol separately
    all_syms = snapshot.get("symbols", {})
    syms = symbols or list(all_syms.keys())
    for sym in syms:
        sym_data = all_syms.get(sym)
        if not sym_data:
            continue
        # Build a single-symbol snapshot for the AI call
        single = {**snapshot, "symbols": {sym: sym_data}}
        signal = get_ai_signal(single, symbols=[sym])
        if signal:
            send_message(f"<b>AI Signal — {sym}</b>\n\n{signal}")
