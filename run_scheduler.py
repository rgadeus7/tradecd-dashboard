"""
Standalone scheduler — runs independently of the Streamlit app.

Reads config from data/scheduler_config.json (written by the Streamlit UI).
Starts APScheduler and blocks until Ctrl+C.

Usage:
    python run_scheduler.py

Config file is auto-reloaded before each job run, so changes made in the
Streamlit UI take effect on the next scheduled tick without restarting.
"""

import json
import logging
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("apscheduler").setLevel(logging.DEBUG)

ROOT        = Path(__file__).parent
CONFIG_PATH = ROOT / "data" / "scheduler_config.json"
ET          = ZoneInfo("America/New_York")

IBKR_PORTS  = [4001, 4002, 7496, 7497]
IBKR_HOST   = "127.0.0.1"


def _ibkr_reachable(port: int | None = None) -> tuple[bool, int | None]:
    """Try each IBKR port; return (reachable, port)."""
    ports = [port] if port else IBKR_PORTS
    for p in ports:
        try:
            with socket.create_connection((IBKR_HOST, p), timeout=2):
                return True, p
        except OSError:
            pass
    return False, None


def _telegram_warn(text: str):
    try:
        sys.path.insert(0, str(ROOT))
        from tools.telegram import send_message
        send_message(text)
    except Exception as e:
        print(f"  [telegram] {e}")


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _notify(symbols: list[str], profile: str, now: str):
    """Send market summary + profile-aware AI analysis to Telegram."""
    try:
        import json as _json
        from tools.prompt_builder import build_messages, PROFILES
        from tools.ai_client import chat
        from tools.telegram import send_message, format_snapshot

        snap_path = ROOT / "data" / "market_snapshot.json"
        if not snap_path.exists():
            return
        with open(snap_path) as f:
            snapshot = _json.load(f)

        # 1. Market summary
        send_message(format_snapshot(snapshot, symbols))

        # 2. Profile-aware AI analysis
        prof_label = PROFILES.get(profile, PROFILES["full"])["label"]
        messages   = build_messages(snapshot, symbols=symbols, profile=profile)
        reply, _   = chat(messages)
        send_message(f"<b>AI [{prof_label}] — {', '.join(symbols)} — {now}</b>\n\n{reply}")
        print(f"  [scheduler] Telegram sent ({prof_label})")
    except Exception as e:
        print(f"  [scheduler] Telegram error: {e}")


def _run_fetch(symbols: list[str], port: str | None, tg: bool, profile: str = "full"):
    # Re-read config in case port/telegram changed since last run
    cfg   = _load_config()
    tg    = cfg.get("telegram", tg)
    port  = cfg.get("port") or port

    if not symbols:
        print("[scheduler] No symbols configured — skipping")
        return

    now = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    print(f"\n[scheduler] {now}  Fetching {', '.join(symbols)}  [{profile}]")

    # IBKR health check
    reachable, live_port = _ibkr_reachable(int(port) if port else None)
    if not reachable:
        msg = f"⚠ Scheduled fetch skipped — IBKR Gateway not reachable ({now})"
        print(f"  {msg}")
        _telegram_warn(msg)
        return

    # Always pass --no-telegram to market_data.py — scheduler handles all notifications
    cmd = [sys.executable, str(ROOT / "scripts" / "market_data.py")] + symbols
    if live_port:
        cmd += ["--port", str(live_port)]
    cmd += ["--no-telegram"]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode == 0:
            print(f"  [scheduler] Fetch done ✓")
            if tg:
                _notify(symbols, profile, now)
        else:
            err = r.stderr[-300:]
            print(f"  [scheduler] ERROR:\n{err}")
            _telegram_warn(f"⚠ Scheduled fetch failed ({now})\n{err}")
    except subprocess.TimeoutExpired:
        print("  [scheduler] Timeout (5 min)")
        _telegram_warn(f"⚠ Scheduled fetch timed out ({now})")
    except Exception as e:
        print(f"  [scheduler] Exception: {e}")


def _build_scheduler(cfg: dict) -> BlockingScheduler:
    sched    = BlockingScheduler(timezone=ET)
    jobs     = cfg.get("jobs", [])
    days     = cfg.get("days", "sun-fri")   # "sun-fri" or "mon-fri"
    # APScheduler day names: mon tue wed thu fri sat sun
    dow      = "sun,mon,tue,wed,thu,fri" if days == "sun-fri" else "mon,tue,wed,thu,fri"
    dow_label = "Sun–Fri" if days == "sun-fri" else "Mon–Fri"
    total    = 0

    for ji, job in enumerate(jobs):
        syms    = job.get("symbols", [])
        times   = job.get("times_et", [])
        profile = job.get("profile", "full")
        for t in times:
            h, m = t.strip().split(":")
            sched.add_job(
                _run_fetch,
                trigger=CronTrigger(day_of_week=dow,
                                    hour=int(h), minute=int(m), timezone=ET),
                args=[syms, cfg.get("port"), cfg.get("telegram", True), profile],
                id=f"job{ji}_{t.replace(':', '')}",
                name=f"{t} ET [{profile}] — {', '.join(syms)}",
            )
            total += 1
        print(f"  Job {ji+1}: {', '.join(syms)}  @  {', '.join(times)} ET  [{profile}]")

    print(f"[scheduler] {total} trigger(s) registered  ({dow_label})")
    return sched


def main():
    print("=" * 50)
    print("  Stock Automation — Standalone Scheduler")
    print("=" * 50)

    cfg = _load_config()
    if not cfg:
        print(f"\nNo config found at {CONFIG_PATH}")
        print("Configure the schedule in the Streamlit app first, then re-run.")
        sys.exit(1)

    symbols = cfg.get("symbols", [])
    days = cfg.get("days", "sun-fri")
    print(f"  Days     : {'Sun–Fri (futures)' if days == 'sun-fri' else 'Mon–Fri (equities)'}")
    for i, job in enumerate(cfg.get("jobs", []), 1):
        print(f"  Job {i}: {', '.join(job.get('symbols', []))}  @  {', '.join(job.get('times_et', []))} ET")
    print(f"  Telegram : {'on' if cfg.get('telegram', True) else 'off'}")
    print()

    sched = _build_scheduler(cfg)

    print("Scheduler running. Press Ctrl+C to stop.\n")
    try:
        sched.start()
    except KeyboardInterrupt:
        print("\n[scheduler] Stopped.")


if __name__ == "__main__":
    main()
