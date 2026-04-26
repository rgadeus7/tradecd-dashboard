# Options Report — IBKR Data Pipeline

Connects to IBKR Gateway, fetches options data, and generates a dark-themed HTML report
with Greeks, GEX walls, Max Pain, Put/Call Ratio, and volume charts.

---

## Prerequisites

- **IBKR Gateway or TWS** running with API enabled
- **Python** (Anaconda recommended)
- **ib_insync** installed: `pip install ib_insync`

### Enable API in Gateway/TWS
`Configure → API → Settings`
- ✅ Enable ActiveX and Socket Clients
- ✅ Allow connections from localhost only
- Socket port: **4001** (live) or **4002** (paper)
- ❌ Read-Only API — must be OFF for market data

---

## Usage

```
python options_report.py [SYMBOL] [OPTIONS]
```

### Symbols

Pass any **equity or index** root (e.g. `AAPL`, `SPX`) or any **future** root listed in `FUTURES_UNDERLYINGS` inside `options_report.py`. Each run uses:

- **`STK_DEFAULTS`** — SMART stock/index options (OPT), strike step $1 by default  
- **`FUT_DEFAULTS`** — CME-style futures options (FOP), strike step $5 by default  
- **`SYMBOL_OVERRIDES`** — per-symbol tweaks (strike step, `exchange`, `fallback_iv`, or `"type": "STK"` / `"FUT"` to force bucket)

Add new futures roots to `FUTURES_UNDERLYINGS`.

**Index underlyings** (`SPX`, `NDX`, `RUT`, …) are listed in `INDEX_UNDERLYINGS` and use IBKR **secType `IND`** (`Index` contract), not `Stock`. Default index exchange is **CBOE**; `NDX` / `RUT` overrides use **NASDAQ** / **ICE** in `SYMBOL_OVERRIDES`. If qualification fails, adjust `exchange` for that symbol in the config.

Default ticker if you omit the positional: `OPTIONS_REPORT_SYMBOL` env var, else `SPY`.

### Examples

```bash
# SPY — auto-pick next expiry + monthly OPEX + quarterly OPEX
python options_report.py SPY

# Other equities / indices (config-driven)
python options_report.py AAPL
python options_report.py SPX

# ES — S&P 500 futures options
python options_report.py ES

# NQ — Nasdaq 100 futures options
python options_report.py NQ

# Specific expiry date(s)
python options_report.py SPY --expiry 20260406
python options_report.py SPY --expiry 20260406,20260417
python options_report.py NQ  --expiry 20260409

# Historical close — get prices/volume as of a past date
python options_report.py SPY --expiry 20260406 --date 20260402
python options_report.py NQ  --expiry 20260409 --date 20260402

# Auto-refresh every 5 minutes (browser reloads too)
python options_report.py SPY --loop 5
python options_report.py ES  --loop 2
python options_report.py NQ  --loop 5

# Force a specific port
python options_report.py SPY --port 4002   # paper gateway
python options_report.py SPY --port 4001   # live gateway
```

---

## How Expiries Are Auto-Selected (no --expiry flag)

| Slot | Logic | Example (running Apr 3) |
|------|-------|------------------------|
| **Next expiry** | Nearest date in chain | Apr 6 (0DTE on that day) |
| **Monthly OPEX** | 3rd Friday of nearest month | Apr 17 |
| **Quarterly OPEX** | 3rd Friday of Mar/Jun/Sep/Dec | Jun 18 (shifted from Jun 19 — Juneteenth) |

Holiday shifts are handled automatically (±5 day window around 3rd Friday).
For ES, if the chain has fewer than 3 expirations, all available ones are used.

---

## Report Sections

### Metrics Grid (per expiry)
| Metric | What it means |
|--------|---------------|
| **ATM IV** | Implied volatility at the money — live when market open, fallback estimate after hours |
| **ATM Bid/Ask** | ATM call bid/ask and spread |
| **Expected Move 1SD** | ±1 standard deviation range (~68% probability) |
| **2SD Range** | ±2 standard deviation range (~95% probability) |
| **ATM Straddle** | Cost to buy ATM call + put = market's priced expected move |
| **Max Pain (OI/Vol-wtd)** | Price where option writers lose least — uses OI when market open, volume after hours |
| **Put/Call Ratio (OI/Vol)** | Total put ÷ call weight. >1.3 = bearish lean, <0.7 = bullish lean |
| **Vol Skew** | Put IV minus Call IV at ±1SD — positive = puts more expensive (normal/fearful) |
| **Call Wall** | Strike above spot with highest GEX — dealer resistance level |
| **Put Wall** | Strike below spot with highest GEX — dealer support level |
| **Gamma Flip** | Strike where net GEX ≈ 0. Above = stable/pinning. Below = trending/volatile |

### Top Volume Bar Charts
Top 5 call + put strikes by volume, with ATM and MaxPain labels always shown.

### Net GEX Chart
`gamma × OI × 100` per strike (falls back to volume when market closed).
Green bars = net positive (call wall / resistance).
Red bars = net negative (put wall / support).

---

## Symbol Differences

| | SPY | ES | NQ |
|---|---|---|---|
| Contract type | OPT (stock option) | FOP (futures option) | FOP (futures option) |
| Underlying | SPY stock | Front-month ES future | Front-month NQ future |
| Strike step | $1 | $5 | $25 |
| OI tick | 22 | 101 | 101 |
| Multiplier | 100 shares | 50 points | 20 points |
| Market hours | 9:30–16:00 ET | ~24hrs (Sun–Fri) | ~24hrs (Sun–Fri) |
| Volume frozen | After 16:00 ET | Active nearly 24hrs | Active nearly 24hrs |
| Fallback IV | 20% | 18% | 20% |

---

## Data Notes

| Data | Market Open | After Hours |
|------|-------------|-------------|
| Spot price | Live last trade | Previous session close |
| Bid / Ask | Live | Not available (-1) |
| Volume | Live intraday | Frozen (today's final via `reqMarketDataType(2)`) |
| Open Interest | Live via tick 22/101 | Not served by IBKR after hours |
| IV / Greeks | Live model | Frozen cache (may be unavailable) |
| Max Pain / GEX | OI-weighted | Volume-weighted (labels update automatically) |

> **Note:** Daily historical bars for options are blocked without an OPRA EOD subscription.
> Use `--date YYYYMMDD` which fetches 1-hour bars (these work without the subscription).

---

## Output Files

```
spy_options_2sd.json    — raw data for SPY
spy_options_2sd.html    — report for SPY (opens automatically)
es_options_2sd.json     — raw data for ES
es_options_2sd.html     — report for ES (opens automatically)
nq_options_2sd.json     — raw data for NQ
nq_options_2sd.html     — report for NQ (opens automatically)
```

---

## Troubleshooting

| Error | Fix |
|-------|-----|
| `ConnectionRefusedError` | Make sure Gateway/TWS is running. Live = port 4001, Paper = 4002 |
| `spot=nan` | Market closed and no close price — check data subscription |
| `0 contracts` | Spot is nan so 2SD filter excludes everything — spot price issue |
| `vol=0` everywhere | Enable frozen data: already set in code. May also need Read-Only OFF in Gateway |
| OI always 0 | Market is closed — OI only streams during live session |
| Error 10197 | Competing live session — script uses random clientId to avoid this |
