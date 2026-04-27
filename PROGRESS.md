# Trading Assistant — Progress Log

## Status: In Progress
Last updated: 2026-04-26

---

## Completed

### Market Data (`scripts/market_data.py`)
- Connects to IBKR (auto-tries all 4 ports)
- Fetches any symbol — auto-detects Futures / Index / Stock/ETF
- Default symbols: ES + SPY; UI allows custom selection
- Timeframes: 15min, 2H, Daily, Weekly, Monthly
- Indicators: MA8/20/50/200, SuperTrend (10,3), RSI14, ATR14 (daily only)
- Overextension metrics (% from MAs) with soft flags
- Sideways/consolidation detection on Weekly (10w + 15w range %)
- Structural levels: last 5 days / weeks / months / quarters
- Murrey Math levels per TF (+2/8, +1/8, -1/8, -2/8, zone, band expansion detection)
- Cross-TF FBD/FBO detection (daily bar tests weekly/monthly/quarterly levels)
- Saves to `data/market_snapshot.json` — merges on re-fetch, per-symbol `_fetched_at` timestamp

### Scoring Engine (`scripts/scoring.py`)
- Bull/Bear score (-10 to +10) per TF with full breakdown
- Reversal score (0-10) per TF with direction (too high / too low)
- Bias labels: Strong Bull / Bull / Mild Bull / Neutral / Mild Bear / Bear / Strong Bear
- Summary: tf_alignment, conviction (high/good/medium/conflicted), top_reversal_tf
- Murrey zone scoring in reversal (±2-3 pts for overshoot zones, band expansion)
- FBD/FBO scoring in reversal

### Options Data (`scripts/options_data.py`)
- Reuses `options_report.py` functions (resolve_symbol_config, batch streaming, OI, connection)
- Picks nearest expiry + next-week Friday expiry (or manual override via `--expiry`)
- Extracts: call wall, put wall, gamma flip, max pain, PCR, ATM IV, straddle, top strikes
- Saves to `data/options_snapshot.json`
- Merges expiries on re-fetch; expired expiries auto-pruned; forced expiry replaces symbol

### Prompt Builder (`tools/prompt_builder.py`)
- Full snapshot → structured LLM prompt (Monthly → Weekly → Daily → 2H → 15min)
- Murrey Math section per TF
- FBD/FBO signals per TF
- Options block per symbol (call wall, put wall, gamma flip, max pain, PCR, IV, straddle)
- LLM instructed to use options levels as key S/R, address reversal flags, address conflicts

### Telegram (`tools/telegram.py`)
- `send_message()` — HTML parse mode
- `format_snapshot()` — bias summary with close price, per-TF scores, reversal warnings, FBD/FBO
- `get_ai_signal()` — compact entry/stop/target via dedicated system prompt
- `notify_snapshot()` — sends market summary then per-symbol AI signal
- Two toggles in UI: notify on IBKR fetch / notify on AI analysis

### Streamlit UI (`app.py`)
- Sidebar: symbol multiselect + custom input, IBKR port, Fetch / Fetch Options / Run Analysis buttons
- Telegram toggles (fetch + AI analysis separately)
- Scheduler config section (save to file, picked up by standalone scheduler)
- Tab 1 — Market Snapshot:
  - Per-symbol tabs with freshness badge (Just updated / Recent / Stale)
  - Narrative card: bias headline, alignment, reversal warnings, FBD/FBO rows
  - Key signals HTML table (TF | Bias | Key Signals)
  - Options card (collapsed by default): ≤2 expiries side-by-side, 3+ as tabs
  - Per-TF expanders with score, breakdown captions, BB, Murrey Math, structural level tables
- Tab 2 — Analysis & Chat: LLM analysis + follow-up chat with full context

### Scheduler (`run_scheduler.py` + `start.bat`)
- Standalone script, independent of Streamlit
- Config saved to `data/scheduler_config.json` via Streamlit UI
- Multiple jobs — each job has its own symbols + times (e.g. SPX EOD only, ES intraday)
- Market days toggle: Sun–Fri (futures) or Mon–Fri (equities)
- All times in ET (America/New_York), handles DST automatically
- IBKR health check before each run — sends Telegram warning if not reachable
- Logging via Python logging + APScheduler DEBUG — nothing silent
- `start.bat` launches Streamlit + scheduler in two separate terminal windows

---

## Pending

### 1. Alert context by schedule timing
- **Intraday alerts** (e.g. 09:45, 10:30, 12:00): focus on entry signals, momentum, short-term levels, intraday bias
- **EOD alerts** (e.g. 15:30): focus on closing structure, daily bias recap, swing setup for next day
- **Overnight/futures open** (e.g. 18:00 Sun, 23:10): overnight range, gap risk, key levels to watch
- Implementation: each job in scheduler config gets an optional `context` tag (`intraday` / `eod` / `overnight`)
- Prompt builder + Telegram formatter use the context tag to adjust what they emphasize
- Per-job context selector in Streamlit scheduler UI

### 2. SQLite — Analysis History
- Create `trading.db` with `reports` table
- Schema: id, timestamp, symbols (JSON), analysis text, provider, signals (JSON)
- Save every AI analysis run to DB
- Add "Past Reports" section in Streamlit to browse history

### 3. Data Quality Improvements
- VWAP on 15min/2H timeframes
- Volume vs 20-day average (confirms/weakens signals)
- Relative strength vs SPY for QQQ, SMH etc.
- Structured LLM output (JSON with entry, stop, T1/T2/T3)

---

## Architecture

```
IBKR
  ├── scripts/market_data.py     → data/market_snapshot.json
  └── scripts/options_data.py    → data/options_snapshot.json
                                          │
                                tools/prompt_builder.py
                                          │
                                tools/ai_client.py (Groq → Google)
                                          │
                               ┌──────────┴──────────┐
                          trading.db (pending)    tools/telegram.py
                                          │
                               app.py (Streamlit UI)
                                          │
                               run_scheduler.py (standalone)
```

## Key Design Decisions
- Snapshot JSON: current state only, always merged (not replaced) on re-fetch
- Options JSON: expiries merged across fetches; expired dates auto-pruned; forced expiry replaces
- FBD/FBO: cross-TF only (daily bar tests weekly/monthly level — not same-TF)
- Murrey Math: lookback = min(96, len(df)) bars — TF-specific, matches Pine Script
- Scheduler: standalone process, config-driven, multiple jobs with independent symbols+times
- Telegram: two separate toggles (fetch vs AI analysis); scheduler has its own toggle
- All schedule times in ET; Sun–Fri for futures, Mon–Fri for equities

## Stack
| Layer | Tool |
|---|---|
| Data | IBKR via ib_insync |
| AI | Groq (llama-3.3-70b) → Groq (llama-3.1-8b) → Google Gemini |
| Storage | JSON (market/options snapshots) + SQLite pending (analysis history) |
| UI | Streamlit |
| Scheduler | APScheduler (BlockingScheduler, standalone) |
| Notifications | Telegram Bot API |

## Files
```
stock-automation/
  app.py                        # Streamlit UI
  run_scheduler.py              # Standalone scheduler
  start.bat                     # Launch Streamlit + scheduler together
  options_report.py             # Full options chain report (existing)
  scripts/
    market_data.py              # IBKR fetch + indicators + Murrey + FBD/FBO
    scoring.py                  # Bull/Bear + reversal scoring engine
    options_data.py             # Options snapshot fetcher (reuses options_report.py)
  tools/
    ai_client.py                # LLM client (Groq → Google fallback)
    prompt_builder.py           # Snapshot → LLM prompt (incl. options)
    telegram.py                 # Telegram notifications
  data/
    market_snapshot.json        # Current market state
    options_snapshot.json       # Current options state
    scheduler_config.json       # Scheduler jobs config (written by UI)
  .env                          # API keys + Telegram tokens
```
