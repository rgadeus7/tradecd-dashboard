# Trading Assistant — Project Brief

## Goal
Build a local AI-powered trading assistant that:
- Runs scheduled market analysis (pre-market, post-market)
- Uses free AI APIs (Groq + Google Gemini) for analysis
- Stores results in SQLite
- Shows a Streamlit chat UI for viewing reports and asking follow-up questions
- Sends Telegram notifications when analysis is ready

---

## Stack

| Layer | Tool | Notes |
|---|---|---|
| Scheduler | APScheduler | Runs Python trading scripts on cron |
| AI Analysis | Groq API (primary) | Free tier, fast, OpenAI-compatible |
| AI Fallback | Google Gemini Flash | Free tier fallback if Groq limit hit |
| Storage | SQLite | Local, no setup needed |
| UI | Streamlit | Chat interface + report viewer |
| Notifications | Telegram Bot API | Push alerts to phone |

---

## Project Structure

```
trading-assistant/
├── scheduler.py          # APScheduler + AI analysis jobs
├── app.py                # Streamlit UI + chat
├── tools/
│   ├── ai_client.py      # Groq + Google fallback logic
│   └── telegram.py       # Telegram notification helper
├── scripts/              # Existing trading scripts go here
│   ├── gex_report.py     # GEX analysis (already built)
│   ├── options_chain.py  # Options chain fetcher
│   └── ibkr_positions.py # IBKR position fetcher
├── trading.db            # SQLite (auto-created)
├── .env                  # API keys
└── requirements.txt
```

---

## Requirements

```
# requirements.txt
apscheduler
streamlit
openai
python-telegram-bot
python-dotenv
```

---

## Environment Variables

```
# .env
GROQ_API_KEY=your_groq_key           # https://console.groq.com
GOOGLE_API_KEY=your_google_key       # https://aistudio.google.com
TELEGRAM_BOT_TOKEN=your_bot_token    # from @BotFather on Telegram
TELEGRAM_CHAT_ID=your_chat_id        # message your bot once to get this
```

---

## AI Client with Fallback (`tools/ai_client.py`)

```python
from openai import OpenAI
from dotenv import load_dotenv
import os

load_dotenv()

PROVIDERS = [
    {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": os.getenv("GROQ_API_KEY"),
        "model": "deepseek-r1-distill-llama-70b"  # best reasoning on Groq free tier
    },
    {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key": os.getenv("GOOGLE_API_KEY"),
        "model": "gemini-2.0-flash"  # fast + free fallback
    }
]

SYSTEM_PROMPT = """You are a quantitative trading analyst specializing in 
options market structure. When analyzing data:
1. Identify key gamma levels (call wall, put support, gamma flip point)
2. State directional bias clearly (bullish/bearish/neutral)
3. List specific price levels to watch
4. Note any unusual options activity or flow
5. Be concise — bullet points preferred over paragraphs
"""

def analyze(data: str, context: str = "") -> str:
    """Send data to AI for analysis. Falls back to Google if Groq limit hit."""
    for provider in PROVIDERS:
        try:
            client = OpenAI(
                base_url=provider["base_url"],
                api_key=provider["api_key"]
            )
            response = client.chat.completions.create(
                model=provider["model"],
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"{context}\n\nData:\n{data}"}
                ],
                max_tokens=1000
            )
            return response.choices[0].message.content
        except Exception as e:
            print(f"Provider {provider['base_url']} failed: {e}")
            continue
    return "Error: All AI providers failed. Check API keys and rate limits."

def chat(messages: list) -> str:
    """Multi-turn chat for Streamlit follow-up questions."""
    for provider in PROVIDERS:
        try:
            client = OpenAI(
                base_url=provider["base_url"],
                api_key=provider["api_key"]
            )
            response = client.chat.completions.create(
                model=provider["model"],
                messages=messages,
                max_tokens=800
            )
            return response.choices[0].message.content
        except Exception:
            continue
    return "Error: All providers failed."
```

---

## Telegram Helper (`tools/telegram.py`)

```python
import asyncio
from telegram import Bot
from dotenv import load_dotenv
import os

load_dotenv()

async def _send(message: str):
    bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))
    # Telegram max message length is 4096 chars
    chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
    for chunk in chunks:
        await bot.send_message(
            chat_id=os.getenv("TELEGRAM_CHAT_ID"),
            text=chunk,
            parse_mode="Markdown"
        )

def send(message: str):
    asyncio.run(_send(message))
```

---

## Scheduler (`scheduler.py`)

```python
import subprocess, sqlite3
from datetime import datetime
from apscheduler.schedulers.blocking import BlockingScheduler
from tools.ai_client import analyze
from tools.telegram import send

DB = "trading.db"

def init_db():
    conn = sqlite3.connect(DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT,
            type TEXT,
            ticker TEXT,
            raw_data TEXT,
            ai_analysis TEXT
        )
    """)
    conn.commit()
    conn.close()

def run_script(script: str, *args) -> str:
    """Run a trading script and return its stdout."""
    result = subprocess.run(
        ["python", f"scripts/{script}"] + list(args),
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        return f"Script error: {result.stderr}"
    return result.stdout

def save_report(report_type: str, ticker: str, raw: str, analysis: str):
    conn = sqlite3.connect(DB)
    conn.execute(
        "INSERT INTO reports (time, type, ticker, raw_data, ai_analysis) VALUES (?,?,?,?,?)",
        (datetime.now().isoformat(), report_type, ticker, raw, analysis)
    )
    conn.commit()
    conn.close()

def run_analysis(ticker: str, report_type: str, context: str):
    print(f"Running {report_type} analysis for {ticker}...")

    # Fetch raw data from your scripts
    gex = run_script("gex_report.py", ticker)
    options = run_script("options_chain.py", ticker)
    combined = f"GEX Report:\n{gex}\n\nOptions Chain:\n{options}"

    # AI analyzes it
    analysis = analyze(data=combined, context=context)

    # Save to DB
    save_report(report_type, ticker, combined, analysis)

    # Push to Telegram
    send(f"*{report_type.upper()} — {ticker}*\n\n{analysis[:3500]}")
    print(f"{report_type} analysis complete for {ticker}")

scheduler = BlockingScheduler(timezone="US/Eastern")

@scheduler.scheduled_job('cron', day_of_week='mon-fri', hour=8, minute=30)
def morning_scan():
    for ticker in ["SPY", "QQQ"]:
        run_analysis(ticker, "pre-market", "Market opens in 30 minutes. Focus on key levels.")

@scheduler.scheduled_job('cron', day_of_week='mon-fri', hour=12, minute=0)
def midday_scan():
    run_analysis("SPY", "midday", "Midday check. Assess if morning bias held.")

@scheduler.scheduled_job('cron', day_of_week='mon-fri', hour=16, minute=15)
def eod_scan():
    run_analysis("SPY", "end-of-day", "EOD recap. What happened, what to watch tomorrow.")

if __name__ == "__main__":
    init_db()
    print("Scheduler started. Press Ctrl+C to stop.")
    scheduler.start()
```

---

## Streamlit UI (`app.py`)

```python
import streamlit as st
import sqlite3
import pandas as pd
from tools.ai_client import chat, analyze
from tools.telegram import send
import subprocess

st.set_page_config(page_title="Trading Assistant", layout="wide")
st.title("Trading Assistant")

DB = "trading.db"

def get_reports(limit=10):
    try:
        conn = sqlite3.connect(DB)
        df = pd.read_sql(
            "SELECT id, time, type, ticker, ai_analysis FROM reports ORDER BY time DESC LIMIT ?",
            conn, params=(limit,)
        )
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()

def get_report_detail(report_id: int):
    conn = sqlite3.connect(DB)
    row = conn.execute(
        "SELECT * FROM reports WHERE id=?", (report_id,)
    ).fetchone()
    conn.close()
    return row

# --- Sidebar: Reports ---
with st.sidebar:
    st.subheader("Reports")
    reports = get_reports()

    if reports.empty:
        st.info("No reports yet. Start the scheduler.")
    else:
        for _, row in reports.iterrows():
            label = f"{row['type']} — {row['ticker']} ({row['time'][:16]})"
            if st.button(label, key=f"r_{row['id']}"):
                st.session_state.selected_report = row['id']
                st.session_state.messages = []  # reset chat on new report

    st.divider()

    # Manual trigger
    st.subheader("Run Now")
    ticker = st.text_input("Ticker", value="SPY")
    if st.button("Run Analysis Now"):
        with st.spinner("Fetching data and analyzing..."):
            gex = subprocess.run(
                ["python", "scripts/gex_report.py", ticker],
                capture_output=True, text=True
            ).stdout
            analysis = analyze(gex, context="On-demand analysis requested.")
            st.success("Done!")
            # Save and notify
            conn = sqlite3.connect(DB)
            from datetime import datetime
            conn.execute(
                "INSERT INTO reports (time, type, ticker, raw_data, ai_analysis) VALUES (?,?,?,?,?)",
                (datetime.now().isoformat(), "on-demand", ticker, gex, analysis)
            )
            conn.commit()
            conn.close()
            send(f"*ON-DEMAND — {ticker}*\n\n{analysis[:3500]}")
            st.rerun()

# --- Main Panel: Selected Report + Chat ---
if "selected_report" not in st.session_state and not reports.empty:
    st.session_state.selected_report = reports.iloc[0]['id']

if "selected_report" in st.session_state:
    report = get_report_detail(st.session_state.selected_report)
    if report:
        st.subheader(f"{report[2].upper()} — {report[3]} @ {report[1][:16]}")
        st.info(report[5])  # ai_analysis

        with st.expander("Raw Data"):
            st.code(report[4])

        st.divider()
        st.subheader("Ask Follow-up Questions")

        # Initialize chat with report as context
        if "messages" not in st.session_state or not st.session_state.messages:
            st.session_state.messages = [
                {
                    "role": "system",
                    "content": f"You are a trading analyst. The user is asking about this analysis:\n\n{report[5]}\n\nRaw data:\n{report[4][:2000]}"
                }
            ]

        # Show chat history
        for msg in st.session_state.messages[1:]:
            st.chat_message(msg["role"]).write(msg["content"])

        if prompt := st.chat_input("Ask about gamma levels, bias, risks..."):
            st.session_state.messages.append({"role": "user", "content": prompt})
            st.chat_message("user").write(prompt)

            with st.chat_message("assistant"):
                with st.spinner("Thinking..."):
                    reply = chat(st.session_state.messages)
                st.write(reply)
                st.session_state.messages.append({"role": "assistant", "content": reply})
else:
    st.info("No reports yet. Start the scheduler or run an analysis manually.")
```

---

## How to Run

```bash
# Install dependencies
pip install apscheduler streamlit openai python-telegram-bot python-dotenv

# Copy your trading scripts into scripts/
# Fill in .env with API keys

# Terminal 1: Start scheduler (runs analysis on schedule)
python scheduler.py

# Terminal 2: Start UI
streamlit run app.py
```

---

## Free Tier Limits (as of April 2026)

| Provider | Model | Daily Limit |
|---|---|---|
| Groq | DeepSeek R1 Distill 70B | 14,400 req/day |
| Groq | Llama 4 Scout | 14,400 req/day |
| Google | Gemini 2.0 Flash | 1,500 req/day |

Scheduled runs (3x/day × 2 tickers = ~6 calls/day) comfortably within both free tiers.

---

## Telegram Bot Setup (5 minutes)

1. Message `@BotFather` on Telegram
2. Send `/newbot` → give it a name → get your `BOT_TOKEN`
3. Message your new bot once
4. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
5. Copy the `chat.id` from the response → that's your `CHAT_ID`
6. Add both to `.env`

---

## Notes for Claude Code

- The `scripts/` folder contains existing Python scripts for GEX/options/IBKR — do not modify them, just call them via `subprocess.run`
- Add error handling around subprocess calls (timeout=60, check returncode)
- SQLite DB auto-creates on first run
- Streamlit reruns on every interaction — keep state in `st.session_state`
- For Telegram, use `asyncio.run()` to call async bot methods from sync scheduler
- The AI system prompt in `tools/ai_client.py` can be customized for your specific trading style
