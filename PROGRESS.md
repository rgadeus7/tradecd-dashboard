# Trading Assistant — Progress Log

## Status: In Progress
Last updated: 2026-04-25

---

## Completed

### AI Chat (Streamlit)
- `app.py` — Streamlit UI with two tabs: Market Snapshot + Analysis & Chat
- `tools/ai_client.py` — Groq (llama-3.3-70b-versatile) primary, Groq (llama-3.1-8b-instant) fallback, Google Gemini fallback
- Basic chat working and tested

### Market Data (`scripts/market_data.py`)
- Connects to IBKR (auto-tries all 4 ports)
- Fetches any symbol — auto-detects Futures / Index / Stock/ETF
- Default symbols: ES + SPY; UI allows custom selection (QQQ, SMH, etc.)
- Timeframes: 15min, 2H, Daily, Weekly, Monthly
- Indicators:
  - MA 20 / 50 / 200 (all TFs; Monthly = MA20 only)
  - SuperTrend (10, 3.0 ATR) — all TFs
  - RSI 14 — all TFs
  - ATR 14 — Daily only
- Overextension metrics (% from MA20/50/200 + SuperTrend) with soft flags
- Sideways/consolidation detection on Weekly (10w + 15w range %)
- Structural levels: last 5 days / weeks / months / quarters (H, L, C per bar)
- Saves to `data/market_snapshot.json` — merges on re-fetch (existing symbols preserved)
- Per-symbol `_fetched_at` timestamp so stale data is visible in UI

### Prompt Builder (`tools/prompt_builder.py`)
- Formats full snapshot into structured LLM prompt
- Monthly → Weekly → Daily → 2H → 15min order
- Highlights ⚠ reversal risk flags and ⚠ sideways/consolidation inline
- LLM instructed to address all warning flags explicitly
- `build_messages()` returns ready-to-send message list for `ai_client.chat()`

### Streamlit UI (`app.py`)
- Sidebar: symbol multiselect (presets + custom), IBKR port, Fetch + Run Analysis buttons
- Tab 1 — Market Snapshot: per-symbol tabs, per-TF expanders, metrics, MA deltas, warnings, structural level tables
- Tab 2 — Analysis & Chat: LLM analysis output + follow-up chat with full context
- Stale/fresh badge per symbol based on last fetch

---

## Pending (build in this order)

### 1. SQLite — Analysis History
- Create `trading.db` with `reports` table
- Schema: id, timestamp, symbols (JSON), analysis text, provider, signals (JSON)
- Save every AI analysis run to DB
- Add "Past Reports" section in Streamlit to browse history

### 2. Scheduler (`scheduler.py`)
- APScheduler — BlockingScheduler, US/Eastern timezone
- Jobs:
  - Pre-market: Mon-Fri 08:30 ET — fetch ES + SPY, run analysis
  - Midday:     Mon-Fri 12:00 ET — fetch ES + SPY, run analysis
  - EOD:        Mon-Fri 16:15 ET — fetch ES + SPY, run analysis
- Each job: fetch market data → build prompt → LLM → save to SQLite → send Telegram
- Run as separate process alongside Streamlit

### 3. Telegram (`tools/telegram.py`)
- Send analysis summary to phone on each scheduled run
- Chunk messages > 4000 chars
- Include: symbol, timeframe bias, key levels, entry/stop/targets
- Requires: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env

### 4. Options Data Integration (revisit later)
- `options_report.py` already fetches full options chain from IBKR (GEX, gamma flip, call/put walls, max pain, PCR, IV)
- Needs to be wired into the pipeline:
  - Run options fetch alongside market data fetch
  - Save options snapshot to `data/options_snapshot.json` (per symbol, merged like market data)
  - Add options section to `prompt_builder.py` — gamma levels + bias context
  - Show options data in Streamlit UI (Tab 3 or within symbol expander)
- Symbols to support: ES, SPX, SPY, QQQ (whatever user selects)

### 5. Prompt & Data Quality Improvements (revisit later)
- Structured LLM output (JSON with entry, stop, T1/T2/T3) instead of free text
- Multi-timeframe alignment score (e.g. "4/5 TFs bullish")
- Market session context in prompt (pre-market / regular / after-hours)
- Previous day high/low/close as key intraday reference levels
- VWAP on 15min/2H timeframes
- Volume vs 20-day average (confirms/weakens signals)
- Relative strength vs SPY for QQQ, SMH etc.
- Side-by-side symbol comparison in prompt

---

## Architecture

```
IBKR
  ├── scripts/market_data.py   → data/market_snapshot.json
  └── options_report.py        → data/options_snapshot.json (pending)
                                        │
                              tools/prompt_builder.py
                                        │
                              tools/ai_client.py (Groq → Google)
                                        │
                              ┌─────────┴──────────┐
                         trading.db            Telegram
                              │
                         app.py (Streamlit UI)
```

## Key Design Decisions
- Snapshot JSON: current state only, always merged (not replaced) on re-fetch
- SQLite: analysis history only (not market data — IBKR always has fresh bars)
- LLM called only on: scheduled runs + manual "Run Analysis" + follow-up chat
- Overextension thresholds: >7% MA20, >10% MA50, >15% MA200, >5% SuperTrend (soft flags)
- Sideways thresholds: <5% range over 10w, <6% over 15w (soft flags)
- SuperTrend: period=10, multiplier=3.0
- All MAs: simple rolling mean

## Stack
| Layer | Tool |
|---|---|
| Data | IBKR via ib_insync |
| AI | Groq (llama-3.3-70b) → Groq (llama-3.1-8b) → Google Gemini |
| Storage | JSON (market data) + SQLite (analysis history) |
| UI | Streamlit |
| Scheduler | APScheduler |
| Notifications | Telegram Bot API |

## Files
```
stock-automation/
  app.py                      # Streamlit UI
  scheduler.py                # (pending)
  scripts/
    market_data.py            # IBKR data fetch + indicators
  tools/
    ai_client.py              # LLM client with fallback chain
    prompt_builder.py         # Snapshot → LLM prompt
    telegram.py               # (pending)
  data/
    market_snapshot.json      # Current market state
  trading.db                  # (pending) Analysis history
  .env                        # API keys
  requirements.txt
```
