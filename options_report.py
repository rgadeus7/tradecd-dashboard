"""
Options Report — equities, indices, and futures options (any symbol with config below)
Usage:
    python options_report.py SPY
    python options_report.py AAPL
    python options_report.py SPX
    python options_report.py ES
    python options_report.py SPY --expiry 20260406          (single expiry)
    python options_report.py SPY --expiry 20260406,20260417 (multiple)
    python options_report.py SPY --date 20260402            (historical close for that date)
    python options_report.py ES  --loop 5                   (refresh every 5 min)
    python options_report.py SPY --port 4001                (force live gateway port)

Notes on --date:
    - If the date equals the previous trading session, reqTickers .close is used (no extra calls).
    - For older dates, reqHistoricalData with 1-hour bars is used (daily bars require OPRA EOD
      subscription and are blocked on IBKR).  IBKR paces historical requests at ~60/10 min so
      a small delay is inserted between contract requests.
"""

import os
import sys, re, json, math, random, time as _time, subprocess
import threading
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

import webbrowser
from ib_insync import IB, Stock, Index, Future, Option, FuturesOption, util

# ── symbol buckets + overrides ────────────────────────────────────────────────
# Any equity / index root not listed in FUTURES_UNDERLYINGS uses STK_DEFAULTS.
# Any root in FUTURES_UNDERLYINGS uses FUT_DEFAULTS (front-month future + FOP chain).
# SYMBOL_OVERRIDES merges on top of the chosen bucket (strike step, exchange, IV, etc.).

STK_DEFAULTS = {
    "type":        "STK",
    "exchange":    "SMART",
    "currency":    "USD",
    "fallback_iv": 0.22,
    "batch":       50,
    "strike_step": 1,
    # Comma-separated IB generic ticks for OPT snapshot (reqMktData); adds to baseline NBBO/greeks.
    # 100 option volume by side, 101 open interest, 106 implied vol — see ib_insync IB.reqMktData doc.
    "snapshot_generic_ticks": "100,101,106",
}

FUT_DEFAULTS = {
    "type":        "FUT",
    "exchange":    "CME",
    "currency":    "USD",
    "fallback_iv": 0.18,
    "batch":       50,
    "strike_step": 5,
    "snapshot_generic_ticks": "100,101,106",
}

# Cash / index underlyings (SPX, NDX, …) — use IBKR secType IND + Index(), not Stock().
IND_DEFAULTS = {
    "type":        "IND",
    "exchange":    "CBOE",
    "currency":    "USD",
    "fallback_iv": 0.18,
    "batch":       50,
    "strike_step": 5,
    "snapshot_generic_ticks": "100,101,106",
}

# Underlyings resolved as futures (add roots you trade). Others default to stock/index OPT.
FUTURES_UNDERLYINGS = frozenset({
    "ES", "MES", "NQ", "MNQ", "RTY", "M2K", "YM", "MYM",
    "CL", "NG", "GC", "SI", "HG", "ZB", "ZN", "ZF", "ZT", "6E", "6B", "6J",
})

# Cash index roots (IND contract). Override "exchange" in SYMBOL_OVERRIDES if IBKR uses
# a different venue (e.g. NDX → NASDAQ, RUT → ICE).
INDEX_UNDERLYINGS = frozenset({
    "SPX", "NDX", "RUT", "DJX", "VIX", "RVX",
})

# Per-symbol merges onto STK_DEFAULTS or FUT_DEFAULTS. Use {"type": "FUT"} / {"type": "STK"}
# to force bucket when a root is ambiguous.
SYMBOL_OVERRIDES = {
    "SPY": {"fallback_iv": 0.20},
    "ES":  {"exchange": "CME", "strike_step": 5,  "fallback_iv": 0.18},
    "NQ":  {"exchange": "CME", "strike_step": 25, "fallback_iv": 0.20},
    # Index options — exchange must match IBKR index listing
    "SPX": {"strike_step": 5},
    "NDX": {"exchange": "NASDAQ", "strike_step": 25},
    "RUT": {"exchange": "ICE", "strike_step": 5},
    # Example non-CME future (uncomment / edit as needed):
    # "GC": {"exchange": "COMEX", "strike_step": 10},
}


def resolve_symbol_config(sym: str) -> dict:
    u = sym.upper()
    ovr = dict(SYMBOL_OVERRIDES.get(u, {}))
    explicit = (ovr.get("type") or "").upper()
    if explicit == "FUT":
        base = FUT_DEFAULTS
    elif explicit == "STK":
        base = STK_DEFAULTS
    elif explicit == "IND":
        base = IND_DEFAULTS
    elif u in FUTURES_UNDERLYINGS:
        base = FUT_DEFAULTS
    elif u in INDEX_UNDERLYINGS:
        base = IND_DEFAULTS
    else:
        base = STK_DEFAULTS
    cfg = {**base, **ovr}
    cfg["type"] = str(cfg.get("type", "STK")).upper()
    return cfg


def _req_option_batch_streaming(ib: IB, contracts, generic_tick_list: str):
    """Streaming ``reqMktData`` with generic ticks (100/101/106, etc.).

    IB Error 321: *Snapshot* + non-empty generic tick list is **not** allowed for OPT.
    We open brief streaming subscriptions, ``ib.sleep``, read accumulated ticks, then cancel.
    """
    contracts = tuple(contracts)
    tickers = [
        ib.reqMktData(c, genericTickList=generic_tick_list, snapshot=False)
        for c in contracts
    ]
    # Allow time for NBBO, model greeks, and generic OI/vol ticks (scales with batch size).
    wait_s = min(12.0, max(2.0, 1.5 + 0.07 * len(contracts)))
    ib.sleep(wait_s)
    for c in contracts:
        ib.cancelMktData(c)
    return tickers


def _oi_from_ticker(t, c):
    raw = t.callOpenInterest if c.right == "C" else t.putOpenInterest
    if raw is None:
        return None
    try:
        v = float(raw)
        if math.isnan(v) or v <= 0:
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


# ── User Configuration ────────────────────────────────────────────────────────
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", str(Path(os.path.expanduser("~")) / "Desktop" / "myapps" / "ibkr")))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

IBKR_HOST = "127.0.0.1"
IBKR_PORTS = {
    "live_gateway":   4001,
    "paper_gateway":  4002,
    "live_tws":       7496,
    "paper_tws":      7497,
}
IBKR_TIMEOUT = 5
CLIENT_ID_RANGE = (100, 999)

DEFAULT_SERVE_INTERVAL = 5  # minutes
DEFAULT_LOOP_INTERVAL = 5   # minutes
DEFAULT_HTTP_PORT = 5000
DEFAULT_RANGE_SD = 2       # 2 = +/-2SD chain pull; set to 1 for smaller batches
DEFAULT_BATCH = 40         # controls reqTickers contract chunks (was 20)

MARKET_DATA_TYPE_FROZEN = 2
MARKET_DATA_TYPE_LIVE = 1
MARKET_DATA_TIMEOUT = 15
BATCH_DELAY = 0.2          # inter-batch pause (was 0.5); IBKR snapshot has no strict pacing
HIST_DELAY = 0.35
MAX_CONTRACTS_PER_EXPIRY = 100

MARKET_OPEN_HOUR = 9
MARKET_OPEN_MIN = 30
MARKET_CLOSE_HOUR = 16
MARKET_CLOSE_MIN = 0

# ── CLI state (defaults; overridden by _apply_cli_args when run as __main__) ──
symbol         = os.environ.get("OPTIONS_REPORT_SYMBOL", "SPY").upper()
loop_mins      = None
forced_expiry  = None   # list of YYYYMMDD strings, or None = auto-pick
hist_date      = None   # YYYYMMDD string for historical close fetch, or None = live
force_port     = None
serve_mode     = False
serve_interval = DEFAULT_SERVE_INTERVAL
http_port      = DEFAULT_HTTP_PORT
range_sd       = DEFAULT_RANGE_SD
batch_size     = DEFAULT_BATCH


def _apply_cli_args(argv):
    global symbol, loop_mins, forced_expiry, hist_date, force_port
    global serve_mode, serve_interval, http_port, range_sd, batch_size
    loop_mins = None
    forced_expiry = None
    hist_date = None
    force_port = None
    serve_mode = False
    serve_interval = DEFAULT_SERVE_INTERVAL
    http_port = DEFAULT_HTTP_PORT
    range_sd = DEFAULT_RANGE_SD
    batch_size = DEFAULT_BATCH
    symbol = os.environ.get("OPTIONS_REPORT_SYMBOL", "SPY").upper()

    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--loop" and i + 1 < len(argv):
            loop_mins = int(argv[i + 1]); i += 1
        elif a == "--expiry" and i + 1 < len(argv):
            forced_expiry = [e.strip() for e in argv[i + 1].split(",")]; i += 1
        elif a == "--date" and i + 1 < len(argv):
            hist_date = argv[i + 1].strip().replace("-", ""); i += 1
        elif a == "--port" and i + 1 < len(argv):
            force_port = int(argv[i + 1]); i += 1
        elif a == "--serve":
            serve_mode = True
        elif a == "--interval" and i + 1 < len(argv):
            serve_interval = int(argv[i + 1]); i += 1
        elif a == "--http-port" and i + 1 < len(argv):
            http_port = int(argv[i + 1]); i += 1
        elif a == "--sd" and i + 1 < len(argv):
            range_sd = float(argv[i + 1]); i += 1
        elif a == "--batch" and i + 1 < len(argv):
            batch_size = int(argv[i + 1]); i += 1
        elif not a.startswith("--"):
            symbol = a.upper()
        else:
            print(f"Unknown option: {a}", file=sys.stderr)
            sys.exit(2)
        i += 1

    if not re.fullmatch(r"[A-Za-z0-9.]+", symbol):
        print(f"Invalid symbol '{symbol}' (use letters, digits, dots only).",
              file=sys.stderr)
        sys.exit(1)


def _refresh_symbol_cfg():
    global cfg
    cfg = resolve_symbol_config(symbol)


cfg = resolve_symbol_config(symbol)


def _print_run_banner():
    print(f"Running report for: {symbol}  [{cfg['type']} {cfg['exchange']}, strike_step={cfg['strike_step']}]"
          + (f"  expiry={forced_expiry}" if forced_expiry else "")
          + (f"  date={hist_date}"       if hist_date     else "")
          + (f"  loop={loop_mins}m"      if loop_mins     else "")
          + (f"  serve (interval={serve_interval}m, port={http_port})" if serve_mode else "")
          + (f"  range_sd={range_sd}" if range_sd else "")
          + (f"  batch={batch_size}" if batch_size else ""))
    if serve_mode:
        print(f"[serve] serve_mode=True interval={serve_interval}m http_port={http_port} "
              f"range_sd={range_sd} batch={batch_size}")


# ── Global state for live dashboard ─────────────────────────────────────────
_state = {"html": "", "last_updated_ms": 0, "spot": None}
_state_lock = threading.Lock()

# ── Dashboard functions ───────────────────────────────────────────────────────
def _waiting_html():
    return ("<html><head><meta charset='utf-8'></head>"
            "<body style='background:#e8ecf3;color:#24292f;font-family:sans-serif;padding:40px'>"
            "<h2 style='color:#57606a'>⏳ Fetching data… refresh in a moment.</h2></body></html>")

class _DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/data.json":
            with _state_lock:
                last_upd = _state["last_updated_ms"]
            payload = json.dumps({"last_updated_ms": last_upd}).encode()
            self._send(200, "application/json", payload)

        elif self.path in ("/", "/index.html"):
            with _state_lock:
                html     = _state["html"] or _waiting_html()
                last_upd = _state["last_updated_ms"]
            html = _inject_polling_js(html, serve_interval, last_upd)
            self._send(200, "text/html", html.encode())
        else:
            self._send(404, "text/plain", b"Not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass

def _inject_polling_js(html_str, interval_mins, current_ts=0):
    # Initialise lastMs from the timestamp baked into the page at serve time.
    # If the server has newer data (timestamp changed), do a full reload so the
    # browser receives a fresh HTML document including the <head> CSS.
    polling_script = f"""
<script>
(function(){{
  var lastMs = {current_ts};
  function poll(){{
    fetch('/data.json').then(r => r.json()).then(d => {{
      if(d.last_updated_ms && d.last_updated_ms !== lastMs){{
        lastMs = d.last_updated_ms;
        window.location.reload();
      }}
    }}).catch(e => console.error('Polling error:', e));
  }}
  setInterval(poll, 2000);
}})();
</script>
"""
    return html_str.replace("</body>", polling_script + "</body>")

def _poller(underlying_sym, interval_mins):
    # Give the HTTP server a head start to bind to the port
    _time.sleep(1) 
    while True:
        try:
            print(f"[poller] Starting data fetch for {underlying_sym}...")
            # Call run_once and unpack all 10 returned values
            (expiry_results, all_data, spot, bid, ask,
             local_symbol, is_futures, ts_str, account_label, data_label) = run_once(persist_connection=True)
            
            # Build the HTML string
            html_str = build_html(
                expiry_results, spot, bid, ask, local_symbol, is_futures,
                ts_str, account_label, data_label, auto_refresh=False)
            
            with _state_lock:
                _state["html"] = html_str
                _state["last_updated_ms"] = int(_time.time() * 1000)
                _state["spot"] = spot
            
            print(f"[poller] Success: Updated {underlying_sym} at {ts_str}")
        
        except Exception as e:
            print(f"[poller] Critical Error: {type(e).__name__}: {e}")
            traceback.print_exc()
            with _state_lock:
                _state["html"] = (
                    "<html><head><meta charset='utf-8'></head>"
                    "<body style='background:#e8ecf3;color:#92400e;"
                    "font-family:sans-serif;padding:40px'>"
                    f"<h2>Poller error: {type(e).__name__}</h2>"
                    f"<pre style='color:#24292f;white-space:pre-wrap'>{e}</pre>"
                    "</body></html>")
        
        print(f"[poller] Sleeping for {interval_mins} minutes...")
        _time.sleep(interval_mins * 60)

# ── helpers ───────────────────────────────────────────────────────────────────
def _ok(v):
    """True if v is a real, positive, non-nan number."""
    try:
        return v is not None and not math.isnan(float(v)) and float(v) > 0
    except (TypeError, ValueError):
        return False


def _safe_price(last, close):
    """Return first valid positive non-nan value from last / close."""
    if _ok(last):  return last
    if _ok(close): return close
    return None


def nth_friday(year, month, n=3):
    from calendar import monthrange
    count = 0
    for d in range(1, monthrange(year, month)[1] + 1):
        if datetime(year, month, d).weekday() == 4:
            count += 1
            if count == n:
                return datetime(year, month, d).date()
    return None


def prev_trading_day(ref_date):
    """Return the most recent weekday before (or equal to) ref_date."""
    d = ref_date
    while d.weekday() >= 5:   # 5=Sat, 6=Sun
        d -= timedelta(days=1)
    return d


# ── persistent connection management ───────────────────────────────────────────────
_shared_ib            = None
_shared_account_label = "Unknown"


def _connect_fresh():
    """Open a fresh IBKR connection; cache it globally."""
    global _shared_ib, _shared_account_label
    util.startLoop()
    ib = IB()
    client_id = random.randint(*CLIENT_ID_RANGE)
    ports = [(force_port, "forced")] if force_port else [
        (4001, "Live Gateway"), (4002, "Paper Gateway"),
        (7496, "Live TWS"),     (7497, "Paper TWS")]
    for port, label in ports:
        try:
            ib.connect(IBKR_HOST, port, clientId=client_id, timeout=IBKR_TIMEOUT)
            _shared_account_label = label
            print(f"Connected via {label} port {port} (clientId={client_id})"
                  f" — {ib.managedAccounts()}")
            _shared_ib = ib
            return ib, label
        except Exception as e:
            print(f"  Port {port} ({label}): {e}")
    raise RuntimeError("Could not connect to IBKR Gateway/TWS on any port.")


def _ensure_ib():
    """Return a live IB connection, reconnecting if the socket dropped."""
    global _shared_ib
    if _shared_ib is not None and _shared_ib.isConnected():
        print(f"Reusing connection ({_shared_account_label})")
        return _shared_ib, _shared_account_label
    return _connect_fresh()


def _release_ib():
    """Disconnect and clear the cached connection."""
    global _shared_ib
    if _shared_ib is not None:
        try:
            _shared_ib.disconnect()
        except Exception:
            pass
        _shared_ib = None
    print("Disconnected.")


# ── fetch core (sync, IB connection thread only) ───────────────────────────────────
def _fetch_all_sync(ib, account_label):
    """All IBKR data fetching using sync ib methods on the connection thread.

    Blocking ib.* calls use util.run() on the thread's asyncio loop; the socket
    is bound to that loop. Worker threads must not call ib.* (Stage 1 and 2
    are sequential).

    Pipeline (live mode):
      Stage 1 — reqContractDetails per expiry (sequential)
      Stage 2 — brief streaming reqMktData per batch (NBBO/greeks + generic ticks; snapshot incompatible with generics on OPT)
      Stage 3 — build rows per expiry
    Historical mode: sequential per-contract (IBKR pacing enforced).
    """
    ib.reqMarketDataType(2)
    print("Market data type: Frozen (2) — session volume available after close")

    today = datetime.now(timezone.utc).date()

    # ── resolve hist_date ────────────────────────────────────────────────────────────────────────────
    use_hist_api = False
    if hist_date:
        hd = datetime.strptime(hist_date, "%Y%m%d").date()
        prev_sess = prev_trading_day(today - timedelta(days=1))
        if hd >= prev_sess:
            print(f"--date {hist_date} is previous session ({prev_sess}); "
                  "using reqTickers.close (no extra API calls)")
        else:
            print(f"--date {hist_date} is older than previous session; "
                  "using reqHistoricalData (1-hr bars, IBKR-paced)")
            use_hist_api = True
    else:
        hd = None

    hist_end_dt = f"{hist_date} 16:00:00 US/Eastern" if hist_date else None
    data_label  = (f"Historical close {hist_date[:4]}-{hist_date[4:6]}-{hist_date[6:]}"
                   if hist_date else "Live snapshot")

    # ── underlying + spot ─────────────────────────────────────────────────────────────────────────────
    if cfg["type"] in ("STK", "IND"):
        if cfg["type"] == "IND":
            underlying = Index(symbol, cfg["exchange"], cfg["currency"])
        else:
            underlying = Stock(symbol, cfg["exchange"], cfg["currency"])
        ib.qualifyContracts(underlying)

        if use_hist_api:
            bars = ib.reqHistoricalData(
                underlying, endDateTime=hist_end_dt, durationStr="1 D",
                barSizeSetting="1 hour", whatToShow="TRADES",
                useRTH=True, formatDate=1)
            if not bars:
                raise RuntimeError(f"No historical data for {symbol} on {hist_date}")
            spot = bars[-1].close
            bid = ask = None
        else:
            try:
                [spot_t] = ib.reqTickers(underlying)
            except Exception as e:
                print(f"Error fetching spot: {e}")
                spot_t = None
            if spot_t:
                bid, ask = spot_t.bid, spot_t.ask
                if hist_date:
                    spot = spot_t.close if _ok(spot_t.close) else spot_t.marketPrice()
                else:
                    raw = spot_t.marketPrice()
                    spot = raw if _ok(raw) else (spot_t.close if _ok(spot_t.close) else None)
            else:
                bid = ask = spot = None

        sec_type_for_chain = cfg["type"]
        con_id = underlying.conId
        local_symbol = symbol

    else:  # FUT
        fut_generic = Future(symbol, exchange=cfg["exchange"], currency=cfg["currency"])
        details     = ib.reqContractDetails(fut_generic)
        front_exp, underlying = sorted(
            [(datetime.strptime(d.contract.lastTradeDateOrContractMonth[:8], "%Y%m%d").date(),
              d.contract)
             for d in details
             if datetime.strptime(d.contract.lastTradeDateOrContractMonth[:8], "%Y%m%d").date()
             >= today],
            key=lambda x: x[0]
        )[0]
        ib.qualifyContracts(underlying)

        if use_hist_api:
            bars = ib.reqHistoricalData(
                underlying, endDateTime=hist_end_dt, durationStr="1 D",
                barSizeSetting="1 hour", whatToShow="TRADES",
                useRTH=True, formatDate=1)
            if not bars:
                raise RuntimeError(f"No historical data for {symbol} on {hist_date}")
            spot = bars[-1].close
            bid = ask = None
        else:
            ib.RequestTimeout = 10
            try:
                [spot_t] = ib.reqTickers(underlying)
            except Exception as e:
                print(f"Error fetching spot: {e}")
                spot_t = None
            if spot_t:
                bid, ask = spot_t.bid, spot_t.ask
                if hist_date:
                    spot = _safe_price(spot_t.close, spot_t.last)
                else:
                    spot = _safe_price(spot_t.last, spot_t.close)
                if spot is None:
                    if _ok(bid) and _ok(ask):
                        spot = round((float(bid) + float(ask)) / 2, 2)
                    elif _ok(bid):
                        spot = float(bid)
                    elif _ok(ask):
                        spot = float(ask)
            else:
                bid = ask = spot = None

        sec_type_for_chain = "FUT"
        con_id = underlying.conId
        local_symbol = underlying.localSymbol

    print(f"{local_symbol}  spot={spot}  bid={bid}  ask={ask}  [{data_label}]")

    if spot is None:
        raise RuntimeError(
            f"Cannot determine spot price for {local_symbol}. "
            "IBKR returned no last/close/bid/ask — market may be closed or "
            "data subscription missing.")

    # ── option chain ──────────────────────────────────────────────────────────────────────────────
    chains      = ib.reqSecDefOptParams(
        symbol,
        cfg["exchange"] if cfg["type"] == "FUT" else "",
        sec_type_for_chain, con_id)
    chain       = max(chains, key=lambda c: len(c.expirations))
    expirations = sorted(
        [e for e in chain.expirations
         if (datetime.strptime(e[:8], "%Y%m%d").date() - today).days >= 0])
    print(f"Chain: {len(expirations)} expiries available")

    def dte(exp):
        return max((datetime.strptime(exp[:8], "%Y%m%d").date() - today).days, 1)

    def pick_expirations():
        if forced_expiry:
            valid = []
            for fe in forced_expiry:
                match = next((e for e in expirations if e[:8] == fe[:8]), None)
                if match:
                    valid.append(match)
                else:
                    print(f"  WARNING: expiry {fe} not found in chain — skipping")
            return valid if valid else expirations[:1]

        exp_dates = {e: datetime.strptime(e[:8], "%Y%m%d").date() for e in expirations}
        QTR = {3, 6, 9, 12}

        def closest_to_opex(target, window=5):
            nearby = [e for e in expirations
                      if abs((exp_dates[e] - target).days) <= window
                      and exp_dates[e] > today]
            return min(nearby, key=lambda e: abs((exp_dates[e] - target).days)) if nearby else None

        def label_exp(e):
            d = exp_dates[e]
            mf3 = nth_friday(d.year, d.month, n=3)
            near_mf3 = mf3 and abs((d - mf3).days) <= 5
            return ("qtr-opex" if (d.month in QTR and near_mf3) else
                    "monthly"  if near_mf3 else "weekly/0DTE")

        candidates = []
        if expirations:
            candidates.append(expirations[0])

        y, m = today.year, today.month
        for _ in range(12):
            mf = nth_friday(y, m, n=3)
            if mf and mf > today:
                e = closest_to_opex(mf)
                if e: candidates.append(e)
                break
            m += 1
            if m > 12: m = 1; y += 1

        y, m = today.year, today.month
        for _ in range(15):
            if m in QTR:
                mf = nth_friday(y, m, n=3)
                if mf and mf > today:
                    e = closest_to_opex(mf)
                    if e: candidates.append(e)
                    break
            m += 1
            if m > 12: m = 1; y += 1

        seen, result = set(), []
        for e in candidates:
            if e and e not in seen:
                seen.add(e); result.append(e)

        if len(expirations) <= 3 and len(result) < len(expirations):
            result = expirations

        labels = [f"{e}({label_exp(e)})" for e in result]
        print(f"  Auto-selected: {', '.join(labels)}")
        return result

    selected_exps = pick_expirations()
    print(f"Selected {len(selected_exps)} expiries: {selected_exps}")

    is_futures = cfg["type"] == "FUT"

    # ======================================================================
    # Stage 1 — reqContractDetails: connection thread only (no worker pool)
    # Stage 2 — streaming reqMktData (generic ticks; IB disallows snapshot+generics on OPT)
    # Stage 3 — build rows
    # Historical: sequential per-contract (IBKR pacing enforced)
    # ======================================================================

    # Stage 1 — reqContractDetails (must not use worker threads — see docstring)
    def _qualify(expiry):
        days_e = dte(expiry)
        em1_e  = spot * cfg["fallback_iv"] * math.sqrt(days_e / 365)
        lo_e, hi_e = spot - em1_e * range_sd, spot + em1_e * range_sd
        if cfg["type"] in ("STK", "IND"):
            template = Option(symbol, expiry, exchange="SMART")
        else:
            template = FuturesOption(symbol, expiry, exchange=cfg["exchange"])
        try:
            details   = ib.reqContractDetails(template)
        except Exception as e:
            print(f"  [Stage1] {expiry}: reqContractDetails failed: {e}")
            return expiry, [], days_e, round(em1_e, 2), round(em1_e * 2, 2)
        all_c     = [d.contract for d in details]
        qualified = [c for c in all_c if lo_e <= c.strike <= hi_e]
        print(f"  {expiry} DTE={days_e}: {len(all_c)} total, "
              f"{len(qualified)} in {range_sd}SD ({lo_e:.1f}-{hi_e:.1f})")
        return expiry, qualified, days_e, round(em1_e, 2), round(em1_e * 2, 2)

    print("Stage 1: fetching contract details for each expiry…")
    qualified_results = [_qualify(exp) for exp in selected_exps]

    valid_exps = [(e, q, d, m1, m2)
                  for e, q, d, m1, m2 in qualified_results if q]

    if not valid_exps:
        raise RuntimeError("No contracts found in 2SD range for any selected expiry.")

    if not use_hist_api:
        # Stage 2 — streaming reqMktData per batch (NBBO/greeks + ticks 100,101,106)
        ib.reqMarketDataType(2)
        ib.RequestTimeout = MARKET_DATA_TIMEOUT
        batch_n = max(5, min(60, batch_size))
        if batch_size > 60:
            print("  Note: --batch capped at 60 contracts per IB request batch.")
        ticks_arg = cfg.get("snapshot_generic_ticks", "101")

        def _fetch_batch(args):
            expiry, batch, batch_idx, n_batches = args
            print(f"    [{expiry}] batch {batch_idx}/{n_batches}: "
                  f"{len(batch)} contracts "
                  f"{batch[0].strike:.1f}–{batch[-1].strike:.1f}")
            for attempt in range(2):
                try:
                    return expiry, _req_option_batch_streaming(ib, batch, ticks_arg)
                except Exception as e:
                    print(f"    [{expiry}] batch {batch_idx} "
                          f"attempt {attempt+1}: {e}")
                    if attempt == 0:
                        ib.sleep(BATCH_DELAY * 2)
            results = []
            for c in batch:
                try:
                    results.extend(
                        _req_option_batch_streaming(ib, (c,), ticks_arg))
                except Exception as e:
                    print(f"      [{expiry}] {c.strike}{c.right}: {e}")
            return expiry, results

        # Build flat batch list across all expiries
        all_batch_args = []
        for expiry, qualified, *_ in valid_exps:
            batches = [qualified[j:j + batch_n]
                       for j in range(0, len(qualified), batch_n)]
            for idx, batch in enumerate(batches):
                all_batch_args.append((expiry, batch, idx + 1, len(batches)))

        total_batches = len(all_batch_args)
        print(f"Stage 2: {total_batches} batches across "
              f"{len(valid_exps)} expiries (stream + {ticks_arg})…")

        batch_results = [_fetch_batch(a) for a in all_batch_args]

        # Reassemble per-expiry ticker lists
        ticker_map = {e: [] for e, *_ in valid_exps}
        for expiry, tickers in batch_results:
            if tickers:
                ticker_map[expiry].extend(tickers)

        # Stage 3 — build rows per expiry ────────────────────────────────────────────────
        expiry_results = []
        for expiry, qualified, days, em1, em2 in valid_exps:
            tickers = ticker_map.get(expiry, [])
            if not tickers:
                print(f"    -> {expiry}: no tickers — skipping")
                continue

            q_strikes = sorted(set(c.strike for c in qualified))
            atm_s     = min(q_strikes, key=lambda s: abs(s - spot))
            atm_tol   = cfg["strike_step"] * 2
            live_iv   = next(
                (g.impliedVol for t in tickers
                 if abs(t.contract.strike - atm_s) <= atm_tol
                 for g in [t.modelGreeks or t.lastGreeks] if g and g.impliedVol),
                None)
            iv = live_iv or cfg["fallback_iv"]
            is_fallback_iv = (iv == cfg["fallback_iv"])

            rows = []
            for t in sorted(tickers, key=lambda t: (t.contract.strike, t.contract.right)):
                c = t.contract
                g = t.modelGreeks or t.lastGreeks
                price = (_safe_price(t.last, t.close) if cfg["type"] == "FUT"
                         else (t.last if _ok(t.last) else
                               (t.close if _ok(t.close) else None)))  # STK / IND
                rows.append({
                    "strike": c.strike, "right": c.right, "conId": c.conId,
                    "bid":    t.bid    if _ok(t.bid)    else None,
                    "ask":    t.ask    if _ok(t.ask)    else None,
                    "price":  price,
                    "volume": int(t.volume) if t.volume and t.volume > 0 else None,
                    "oi":     _oi_from_ticker(t, c),
                    "iv":     round(g.impliedVol * 100, 2) if g and g.impliedVol else None,
                    "delta":  round(g.delta,  3) if g and g.delta  else None,
                    "gamma":  round(g.gamma,  6) if g and g.gamma  else None,
                    "theta":  round(g.theta,  3) if g and g.theta  else None,
                    "vega":   round(g.vega,   3) if g and g.vega   else None,
                })

            with_vol = sum(1 for r in rows if r["volume"])
            with_oi = sum(1 for r in rows if r.get("oi"))
            print(f"    -> {expiry}: {len(rows)} rows  "
                  f"prices={sum(1 for r in rows if r['price'])}  "
                  f"vol={with_vol}  oi={with_oi}  IV={iv*100:.1f}%")
            expiry_results.append((expiry, rows, iv, em1, em2, days, is_fallback_iv))

    else:
        # ── historical path: sequential per-contract (IBKR pacing) ───────────────────
        expiry_results = []
        for expiry, qualified, days, em1, em2 in valid_exps:
            rows  = []
            total = len(qualified)
            for idx, c in enumerate(
                    sorted(qualified, key=lambda c: (c.strike, c.right))):
                try:
                    bars = ib.reqHistoricalData(
                        c, endDateTime=hist_end_dt, durationStr="1 D",
                        barSizeSetting="1 hour", whatToShow="TRADES",
                        useRTH=True, formatDate=1)
                except Exception as e:
                    print(f"    hist {idx+1}/{total}  {c.strike}{c.right}: {e}")
                    bars = []
                price  = bars[-1].close  if bars else None
                volume = bars[-1].volume if bars else None
                if (idx + 1) % 10 == 0 or (idx + 1) == total:
                    print(f"    hist {idx+1}/{total}  {c.strike}{c.right}  "
                          f"price={price}  vol={volume}")
                rows.append({
                    "strike": c.strike, "right": c.right, "conId": c.conId,
                    "bid": None, "ask": None,
                    "price":  price,
                    "volume": int(volume) if volume and volume > 0 else None,
                    "oi":     None,
                    "iv": None, "delta": None, "gamma": None,
                    "theta": None, "vega": None,
                })
                _time.sleep(HIST_DELAY)

            with_vol = sum(1 for r in rows if r["volume"])
            print(f"    -> {expiry}: {len(rows)} rows  "
                  f"prices={sum(1 for r in rows if r['price'])}  vol={with_vol}")
            expiry_results.append(
                (expiry, rows, cfg["fallback_iv"], em1, em2, days, True))

    # ── save JSON ─────────────────────────────────────────────────────────────────────────────────
    all_data = {
        "symbol": symbol, "local_symbol": local_symbol,
        "spot": spot, "bid": bid, "ask": ask,
        "timestamp": str(datetime.now(timezone.utc)),
        "data_label": data_label,
        "expiries": {}
    }
    for exp, rows, iv, em1, em2, days, is_fallback_iv in expiry_results:
        all_data["expiries"][exp] = {
            "expiry": exp, "dte": days, "iv_used": iv,
            "range_lo": round(spot - em2, 2), "range_hi": round(spot + em2, 2),
            "contracts": rows,
            "is_fallback_iv": is_fallback_iv,
        }

    json_path = OUTPUT_DIR / f"{symbol.lower()}_options_2sd.json"
    with open(json_path, "w") as f:
        json.dump(all_data, f, indent=2)
    print(f"\nJSON -> {json_path}")

    ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (expiry_results, all_data, spot, bid, ask,
            local_symbol, is_futures, ts_str, account_label, data_label)


# ── public API ───────────────────────────────────────────────────────────────────────────────────
def run_once(persist_connection=False):
    """Connect (or reuse) IBKR, fetch all option data, save JSON.

    persist_connection=True keeps the IB socket open for the next call;
    used in --serve and --loop modes to avoid reconnect overhead.
    """
    ib, account_label = _ensure_ib()
    try:
        return _fetch_all_sync(ib, account_label)
    finally:
        if not persist_connection:
            _release_ib()


# ── HTML builder (no IB connection needed) ────────────────────────────────────
def fmt(v, d=2):
    return f"{v:.{d}f}" if v is not None else "—"


def build_html(expiry_results, spot, bid, ask, local_symbol, is_futures,
               ts_str, account_label, data_label, auto_refresh=False,
               write_html=False):
    accent = "#f97316" if is_futures else "#facc15"
    title  = f"{local_symbol} {'Futures ' if is_futures else ''}Options Report"

    refresh_tag  = '<meta http-equiv="refresh" content="60">' if auto_refresh else ""
    is_hist      = hist_date is not None
    is_live_data = not is_hist

    def build_section(exp, rows, iv, em1, em2, days, is_fallback_iv=False):
        calls   = {r["strike"]: r for r in rows if r["right"] == "C"}
        puts    = {r["strike"]: r for r in rows if r["right"] == "P"}
        strikes = sorted(set(calls) | set(puts))
        if not strikes:
            return f"<section><p style='padding:20px;color:#57606a'>No data for {exp}</p></section>"
        atm_s = min(strikes, key=lambda s: abs(s - spot))

        total_cv = sum(r["volume"] or 0 for r in rows if r["right"] == "C")
        total_pv = sum(r["volume"] or 0 for r in rows if r["right"] == "P")
        pcr      = round(total_pv / total_cv, 2) if total_cv else None
        has_vol  = (total_cv + total_pv) > 0

        # OI-based PCR (more meaningful than volume PCR)
        total_coi = sum(r["oi"] or 0 for r in rows if r["right"] == "C")
        total_poi = sum(r["oi"] or 0 for r in rows if r["right"] == "P")
        pcr_oi    = round(total_poi / total_coi, 2) if total_coi else None
        has_oi    = (total_coi + total_poi) > 0

        top_calls = sorted([(s, calls[s]["volume"]) for s in strikes if calls.get(s, {}).get("volume")], key=lambda x: -x[1])[:5]
        top_puts  = sorted([(s, puts[s]["volume"])  for s in strikes if puts.get(s,  {}).get("volume")], key=lambda x: -x[1])[:5]
        top_calls_oi = sorted(
            [(s, calls[s]["oi"]) for s in strikes if calls.get(s, {}).get("oi")],
            key=lambda x: -x[1])[:5]
        top_puts_oi = sorted(
            [(s, puts[s]["oi"]) for s in strikes if puts.get(s, {}).get("oi")],
            key=lambda x: -x[1])[:5]

        def max_pain():
            """Use OI if available (more accurate), fall back to volume."""
            best, best_loss = None, float("inf")
            for target in strikes:
                # prefer OI, fall back to volume
                loss  = sum(max(0, target - s) * (calls[s].get("oi") or calls[s].get("volume") or 0) for s in strikes if s in calls)
                loss += sum(max(0, s - target) * (puts[s].get("oi")  or puts[s].get("volume")  or 0) for s in strikes if s in puts)
                if loss < best_loss: best_loss, best = loss, target
            return best

        mp = max_pain()

        # ── Gamma Exposure (GEX) ──────────────────────────────────────────────
        def _gex_weight(side_dict, s):
            r = side_dict.get(s, {})
            return r.get("oi") or r.get("volume") or 0

        call_gex = {s: (calls[s].get("gamma") or 0) * _gex_weight(calls, s) * 100
                    for s in strikes if s in calls}
        put_gex = {s: (puts[s].get("gamma") or 0) * _gex_weight(puts, s) * 100
                    for s in strikes if s in puts}
        net_gex = {s: call_gex.get(s, 0) - put_gex.get(s, 0) for s in strikes}

        has_gex = any(v != 0 for v in net_gex.values())

        # Classic walls: highest call GEX at/above spot; highest put GEX at/below spot.
        # (put_gex stores positive |γ|×OI×100; some texts use negative put leg — not needed here.)
        call_wall_s = max(
            (s for s in call_gex if s >= spot and call_gex[s] > 0),
            key=lambda s: call_gex[s], default=None)
        put_wall_s = max(
            (s for s in put_gex if s <= spot and put_gex[s] > 0),
            key=lambda s: put_gex[s], default=None)

        # ── Gamma Flip ────────────────────────────────────────────────────────
        sorted_strikes = sorted(net_gex.keys())
        gamma_flip_s = None
        is_approx_flip = False

        for i in range(1, len(sorted_strikes)):
            s1 = sorted_strikes[i - 1]
            s2 = sorted_strikes[i]
            g1, g2 = net_gex[s1], net_gex[s2]
            if g1 * g2 < 0:
                denom = abs(g1) + abs(g2)
                gamma_flip_s = (
                    s1 + (s2 - s1) * (abs(g1) / denom) if denom > 0 else (s1 + s2) / 2)
                break

        if gamma_flip_s is None and has_gex:
            gamma_flip_s = min(
                (s for s in net_gex if net_gex[s] != 0),
                key=lambda s: abs(net_gex[s]),
                default=None,
            )
            is_approx_flip = gamma_flip_s is not None

        cl = calls.get(atm_s, {}).get("price")
        pl = puts.get(atm_s,  {}).get("price")
        straddle = round(cl + pl, 2) if cl and pl else None

        skew_put_s  = min(strikes, key=lambda s: abs(s - (spot - em1)))
        skew_call_s = min(strikes, key=lambda s: abs(s - (spot + em1)))
        spiv = puts.get(skew_put_s,   {}).get("iv")
        sciv = calls.get(skew_call_s, {}).get("iv")
        skew = round(spiv - sciv, 2) if spiv and sciv else None

        pcr_display = pcr_oi if has_oi else pcr
        pcr_cls = (
            "bearish" if pcr_display and pcr_display > 1.3
            else "bullish" if pcr_display and pcr_display < 0.7 else "")
        exp_fmt = f"{exp[:4]}-{exp[4:6]}-{exp[6:8]}"
        _exp_dt = datetime.strptime(exp[:8], "%Y%m%d")
        exp_display = _exp_dt.strftime("%b ") + str(_exp_dt.day) + _exp_dt.strftime(", %Y")

        def sentiment():
            s = []
            if pcr_display and pcr_display > 1.1:
                s.append("put-heavy flow")
            if pcr_display and pcr_display < 0.9:
                s.append("call-heavy flow")
            if skew and skew > 3:   s.append("elevated put skew")
            if skew and skew < 0:   s.append("call skew (unusual)")
            return "Neutral / balanced" if not s else " + ".join(s).capitalize()

        atm_call = calls.get(atm_s, {})
        spread   = round(atm_call["ask"] - atm_call["bid"], 2) if atm_call.get("bid") and atm_call.get("ask") else None
        unit     = "pts" if is_futures else ""

        iv_color = '#1a7f37' if iv != cfg['fallback_iv'] else '#9a6700'
        iv_note  = 'Live IV' if iv != cfg['fallback_iv'] else ('N/A (hist mode)' if is_hist else 'Est. fallback')

        metrics = f"""
    <div class="metrics-grid">
      <div class="metric metric--gex metric--gex-lead"><div class="ml">&#9650; Call Wall (Resistance)</div>
        <div class="mv" style="color:#1a7f37">{f'{call_wall_s:.1f}' if call_wall_s else '—'}</div>
        <div class="ms">{'${:.0f} above spot'.format(call_wall_s - spot) if call_wall_s else 'No GEX data'}</div></div>
      <div class="metric"><div class="ml">&#9660; Put Wall (Support)</div>
        <div class="mv" style="color:#0f766e">{f'{put_wall_s:.1f}' if put_wall_s else '—'}</div>
        <div class="ms">{'${:.0f} below spot'.format(spot - put_wall_s) if put_wall_s else 'No GEX data'}</div></div>
      <div class="metric">
        <div class="ml">&#9672; Gamma Flip</div>
        <div class="mv" style="color:#8250df">
          {f'{gamma_flip_s:.1f}' if gamma_flip_s is not None else '—'}
        </div>
        <div class="ms">
          {'Above = stable (positive gamma)' if gamma_flip_s is not None and spot > gamma_flip_s 
           else 'Below = volatile (negative gamma)' if gamma_flip_s is not None 
           else 'No clear gamma flip detected in this expiry'}
          {('<span style="opacity:.85"> · Approx. (nearest strike to zero net GEX)</span>'
            if is_approx_flip and gamma_flip_s is not None else "")}
        </div>
      </div>
      <div class="metric metric--atm"><div class="ml">ATM IV</div>
        <div class="mv" style="color:{iv_color}">{iv*100:.1f}%</div>
        <div class="ms">{iv_note}</div></div>
      <div class="metric metric--atm"><div class="ml">ATM Bid / Ask</div>
        <div class="mv">{fmt(atm_call.get('bid'))} / {fmt(atm_call.get('ask'))}</div>
        <div class="ms">Spread: {'$'+str(spread) if spread else '—'}</div></div>
      <div class="metric metric--expmove"><div class="ml">Expected Move 1SD</div>
        <div class="mv">&#177;{'${:.2f}'.format(em1) if not is_futures else '{:.0f} pts'.format(em1)}</div>
        <div class="ms">{spot-em1:.{'0' if is_futures else '1'}f} &ndash; {spot+em1:.{'0' if is_futures else '1'}f}</div></div>
      <div class="metric metric--expmove"><div class="ml">2SD Range</div>
        <div class="mv">{spot-em2:.{'0' if is_futures else '1'}f} &ndash; {spot+em2:.{'0' if is_futures else '1'}f}</div>
        <div class="ms">~95% probability range</div></div>
      <div class="metric metric--atm"><div class="ml">ATM Straddle</div>
        <div class="mv">{'$'+str(straddle) if straddle else '—'}</div>
        <div class="ms">Break-even: &#177;{'$'+str(straddle) if straddle else '—'} {unit}</div></div>
      <div class="metric"><div class="ml">Max Pain ({'OI' if has_oi else 'Vol'}-wtd)</div>
        <div class="mv">{mp:.{'0' if is_futures else '1'}f}</div>
        <div class="ms">{'Above' if mp and mp > spot else 'Below'} spot by {abs(mp-spot):.{'0' if is_futures else '1'}f} {unit if mp else ''}</div></div>
      <div class="metric {pcr_cls}"><div class="ml">Put/Call Ratio ({'OI' if has_oi else 'Vol'})</div>
        <div class="mv">{pcr_display if pcr_display else '—'}</div>
        <div class="ms">{'Bearish lean' if pcr_cls=='bearish' else 'Bullish lean' if pcr_cls=='bullish' else 'Neutral'} &nbsp;|&nbsp; {f'P {total_poi:,} / C {total_coi:,} OI' if has_oi else f'P {total_pv:,} / C {total_cv:,}'}</div></div>
      <div class="metric"><div class="ml">Vol Skew (1SD P-C)</div>
        <div class="mv">{('+'+str(skew)) if skew and skew>0 else str(skew) if skew else '—'}</div>
        <div class="ms">Put IV minus Call IV at &#177;1SD</div></div>
    </div>"""

        def top_bar_rows(items, color, side_data, *, bar_cap=220, value_key="volume"):
            """Bar chart for volume or OI. Max-pain row included when that field exists."""
            _label = "open interest" if value_key == "oi" else "volume"
            if not items:
                return f"<p class='no-data'>No {_label} data for this expiry</p>"

            display = list(items)
            if mp and not any(s == mp for s, _ in display):
                mp_v = side_data.get(mp, {}).get(value_key)
                if mp_v:
                    display.append((mp, mp_v))

            max_v = max(v for _, v in display)
            out = ""
            for s, v in display:
                w   = max(4, int(bar_cap * v / max_v))
                tag = "ATM" if s == atm_s else ("MaxPain" if s == mp else "")
                tag_cls = " tv-tag--atm" if tag == "ATM" else ""
                extra_style = "border-left:2px solid #0969da;" if s == mp and tag == "MaxPain" else ""
                out += f"""<div class="tv-row" style="{extra_style}">
              <span class="tv-strike">{s:.{'0' if is_futures else '1'}f} <span class="tv-tag{tag_cls}">{tag}</span></span>
              <div class="tv-bar" style="width:{w}px;background:{color}"></div>
              <span class="tv-num">{v:,}</span></div>"""
            return out

        _sdec = 0 if is_futures else 1

        def _strike_teaser(pairs, n=3):
            if not pairs:
                return "—"
            return ", ".join(f"{s:.{_sdec}f}" for s, _ in pairs[:n])

        vol_teaser = f"Top strikes — calls: {_strike_teaser(top_calls)} · puts: {_strike_teaser(top_puts)}"
        oi_teaser = f"Top strikes — calls: {_strike_teaser(top_calls_oi)} · puts: {_strike_teaser(top_puts_oi)}"

        top_vols = f"""
    <details class="detail-secondary">
      <summary class="detail-summary">
        <span class="detail-summary-title">Top strike volume</span>
        <span class="detail-summary-hint">{vol_teaser}</span>
      </summary>
      <div class="detail-body">
    <div class="tv-wrap">
      <div class="tv-col"><div class="tv-hdr call-color">Top Call Volume</div>{top_bar_rows(top_calls,'var(--bar-call-fill)', calls, bar_cap=148)}</div>
      <div class="tv-col"><div class="tv-hdr put-color">Top Put Volume</div>{top_bar_rows(top_puts,'var(--bar-put-fill)', puts, bar_cap=148)}</div>
    </div>
      </div>
    </details>"""

        top_oi = ""
        if has_oi:
            top_oi = f"""
    <details class="detail-secondary">
      <summary class="detail-summary">
        <span class="detail-summary-title">Top strike open interest</span>
        <span class="detail-summary-hint">{oi_teaser}</span>
      </summary>
      <div class="detail-body">
    <div class="tv-wrap">
      <div class="tv-col"><div class="tv-hdr call-color">Top Call OI</div>{top_bar_rows(top_calls_oi,'var(--bar-call-fill)', calls, bar_cap=148, value_key='oi')}</div>
      <div class="tv-col"><div class="tv-hdr put-color">Top Put OI</div>{top_bar_rows(top_puts_oi,'var(--bar-put-fill)', puts, bar_cap=148, value_key='oi')}</div>
    </div>
      </div>
    </details>"""

        # ── GEX chart ─────────────────────────────────────────────────────────
        def gex_chart():
            if not has_gex:
                return ""
            if not has_oi:
                return (
                    "<div style='padding:12px 20px;border-top:1px solid var(--border);"
                    "color:#57606a;font-size:12px'>&#9675; GEX chart unavailable — OI data "
                    "requires a live market session.</div>")
            # Top 6 strikes by abs net GEX near spot
            top_gex = sorted(net_gex.items(), key=lambda x: -abs(x[1]))[:8]
            top_gex = sorted(top_gex, key=lambda x: x[0])  # re-sort by strike
            max_abs = max(abs(v) for _, v in top_gex) or 1
            _flip_strike = (
                min(strikes, key=lambda x: abs(x - gamma_flip_s))
                if gamma_flip_s is not None else None)
            rows_html = ""
            for s, v in top_gex:
                bar_w  = max(4, int(128 * abs(v) / max_abs))
                bar_bg = "var(--bar-call-fill)" if v >= 0 else "var(--bar-put-fill)"
                num_c  = "var(--gb)" if v >= 0 else "var(--rb)"
                sign   = "+" if v >= 0 else ""
                tag    = ""
                if s == call_wall_s:  tag = "Call Wall"
                elif s == put_wall_s: tag = "Put Wall"
                elif _flip_strike is not None and s == _flip_strike:
                    tag = "Gamma Flip"
                elif s == atm_s:      tag = "ATM"
                tag_cls = " tv-tag--atm" if tag == "ATM" else ""
                rows_html += f"""<div class="tv-row">
              <span class="tv-strike">{s:.{'0' if is_futures else '1'}f} <span class="tv-tag{tag_cls}">{tag}</span></span>
              <div class="tv-bar" style="width:{bar_w}px;background:{bar_bg}"></div>
              <span class="tv-num" style="color:{num_c}">{sign}{v:,.0f}</span></div>"""
            return f"""
    <details class="detail-secondary">
      <summary class="detail-summary">
        <span class="detail-summary-title">Net GEX by strike</span>
        <span class="detail-summary-hint">Shape of exposure — call wall, put wall &amp; gamma flip are in the tiles above</span>
      </summary>
      <div class="detail-body">
    <div class="gex-wrap">
      <div class="gex-hdr">
        <span style="color:#8250df;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.7px">
          &#9672; Net GEX by Strike (gamma &times; {'OI' if has_oi else 'volume'} &times; 100)
          {'&#9679; Using live OI' if has_oi else '&#9675; Market closed — using volume (OI unavailable after hours)'}
        </span>
        <span style="color:#57606a;font-size:10px;margin-left:12px">
          <span style="color:#1a7f37">green</span>/<span style="color:#0f766e">teal</span> = walls
          &nbsp;|&nbsp; above gamma flip = stable, below = trending
        </span>
      </div>
      <div class="gex-bars">{rows_html}</div>
    </div>
      </div>
    </details>"""

        gex_section = gex_chart()

        # status pill
        if is_hist:
            pill = f'<span class="hist-pill">&#9650; Historical {hist_date[:4]}-{hist_date[4:6]}-{hist_date[6:]}</span>'
        elif has_vol:
            pill = '<span class="live-pill">&#9679; LIVE</span>'
        else:
            pill = '<span class="ah-pill">Close Prices</span>'

        _sent = sentiment()
        return f"""
    <section class="{'fallback-used' if is_fallback_iv else ''}">
      <div class="sec-hdr">
        <div class="sec-title">
          <h2>{exp_display}</h2>
          <span class="badge">{days} DTE</span>
          {pill}
          <span class="sec-sentiment {pcr_cls}">{_sent}</span>
        </div>
      </div>
      {metrics}
      {top_vols}
      {top_oi}
      {gex_section}
    </section>"""

    sections = "".join(build_section(*a) for a in expiry_results)

    # data-source banner (only when historical)
    hist_banner = ""
    if is_hist:
        hist_banner = f"""
<div class="hist-banner">
  &#9650; Historical data: prices and volume reflect the session ending {hist_date[:4]}-{hist_date[4:6]}-{hist_date[6:]} 16:00 ET
  &nbsp;|&nbsp; IV shown uses fallback estimate (no live Greeks in historical mode)
</div>"""

    _logo_rest = " · Futures options" if is_futures else " · Options"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
{refresh_tag}
<title>{title}</title>
<style>
:root{{
  --bg:#f4f5f7;--bg2:#ffffff;--bg3:#eceff3;
  --text:#24292f;--mu:#57606a;--border:#d8dee4;
  --gb:#1a7f37;--rb:#0f766e;--blue:#0969da;--purple:#8250df;
  --bar-call-fill:#bcd2c4;--bar-put-fill:#b8d5ce;
  --yellow:#9a6700;--accent:{accent};
  --date-mv:#6639ba;--em-mv:#0969da;--em-ms:#4a5f8a;
  --atm-mv:#7c3aed;--atm-ms:#5b4d8c;
  --itm-c-bg:#ddf4e3;--itm-p-bg:#ccfbf1;--atm-bg:#fff8c5;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
html{{background:var(--bg)}}
body{{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:13px;min-height:100vh}}
header{{background:var(--bg2);border-bottom:1px solid var(--border);padding:16px 28px;
        box-shadow:0 1px 0 rgba(15,23,42,.04)}}
.hd-left{{min-width:0}}
.logo{{font-size:22px;font-weight:800;line-height:1.2}}
.logo .sym{{color:var(--accent)}}
.logo-rest{{font-weight:600;font-size:17px;color:var(--text)}}
.hd-sub{{font-size:12px;color:var(--mu);margin-top:4px}}
.hist-banner{{background:#fff8c5;border-bottom:2px solid #bf8700;
             padding:9px 32px;font-size:12px;color:#9a6700;font-weight:500}}
main{{max-width:1500px;margin:0 auto;padding:24px 20px}}
section{{margin-bottom:44px;border:1px solid var(--border);border-radius:10px;overflow:hidden;
         background:var(--bg2);box-shadow:0 1px 2px rgba(15,23,42,.05)}}
.fallback-used{{background:#fff8c5}}
.sec-hdr{{background:var(--bg3);padding:14px 20px;border-bottom:1px solid var(--border)}}
.sec-title{{display:flex;align-items:center;flex-wrap:wrap;gap:10px 14px}}
.sec-title h2{{font-size:17px;font-weight:600;color:var(--date-mv);letter-spacing:0.01em}}
.sec-sentiment{{font-size:13px;font-weight:600;margin-left:8px;padding:5px 14px;border-radius:20px;
  background:var(--bg3);border:1px solid var(--border);color:var(--yellow);line-height:1.2}}
.sec-sentiment.bullish{{background:#dafbe1;color:var(--gb);border-color:rgba(26,127,55,.35)}}
.sec-sentiment.bearish{{background:#e6f5f2;color:var(--rb);border-color:rgba(15,118,110,.35)}}
.metric--gex-lead{{box-shadow:inset 2px 0 0 rgba(9,105,218,0.2)}}
.metric--expmove .mv{{color:var(--em-mv)!important}}
.metric--expmove .ms{{color:var(--em-ms)}}
.metric--atm .mv{{color:var(--atm-mv)}}
.metric--atm .ms{{color:var(--atm-ms)}}
.tv-tag--atm{{color:var(--atm-mv);font-weight:700}}
.badge{{background:var(--bg3);color:var(--mu);font-size:11px;padding:3px 10px;border-radius:20px;border:1px solid var(--border)}}
.live-pill{{background:#dafbe1;color:#1a7f37;font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px}}
.ah-pill{{background:#fff8c5;color:#9a6700;font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px}}
.hist-pill{{background:#fff8c5;color:#9a6700;border:1px solid #bf8700;
            font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px}}
.metrics-grid{{display:flex;flex-wrap:nowrap;gap:1px;background:var(--border);
               border-bottom:1px solid var(--border);overflow-x:auto;-webkit-overflow-scrolling:touch}}
.metric{{background:var(--bg2);padding:12px 14px;flex:1 1 0;min-width:118px;max-width:200px}}
.ml{{font-size:10px;color:var(--mu);text-transform:uppercase;letter-spacing:.7px;margin-bottom:3px}}
.mv{{font-size:18px;font-weight:500;color:var(--yellow);line-height:1.35;letter-spacing:0.01em}}
.ms{{font-size:12px;color:var(--mu);margin-top:4px;line-height:1.4}}
.bullish .mv{{color:var(--gb)}}.bearish .mv{{color:var(--rb)}}
.tv-wrap{{display:flex;gap:1px;background:var(--border);border-bottom:1px solid var(--border)}}
.tv-col{{flex:1;background:var(--bg3);padding:13px 20px}}
.tv-hdr{{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.7px;margin-bottom:10px}}
.call-color{{color:var(--gb)}}.put-color{{color:var(--rb)}}
.tv-row{{display:flex;align-items:center;gap:10px;margin-bottom:7px}}
.tv-strike{{font-size:13px;font-weight:500;width:110px;flex-shrink:0;color:var(--text);letter-spacing:0.01em}}
.tv-tag{{font-size:10px;color:var(--yellow)}}
.tv-bar{{height:14px;border-radius:4px;flex-shrink:0;
        box-shadow:inset 0 0 0 1px rgba(36,41,47,.08)}}
.tv-num{{font-size:13px;font-weight:500;color:var(--text)}}
.no-data{{color:var(--mu);font-size:11px;padding:4px 0}}
.detail-secondary{{border-bottom:1px solid var(--border)}}
.detail-secondary>summary.detail-summary{{
  list-style:none;cursor:pointer;padding:10px 18px 10px 20px;background:var(--bg3);color:var(--mu);
  font-size:12px;display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 14px;user-select:none;
  border-top:1px solid var(--border)}}
.detail-secondary>summary.detail-summary::-webkit-details-marker{{display:none}}
.detail-secondary>summary .detail-summary-title{{
  font-weight:600;color:var(--text);font-size:12px;display:inline-flex;align-items:center;gap:7px}}
.detail-secondary>summary .detail-summary-title::before{{
  content:'';width:0;height:0;border-left:6px solid var(--mu);border-top:4.5px solid transparent;
  border-bottom:4.5px solid transparent;transition:transform .12s ease;opacity:.75;flex-shrink:0}}
.detail-secondary[open]>summary .detail-summary-title::before{{transform:rotate(90deg)}}
.detail-summary-hint{{font-size:11px;color:var(--mu);font-weight:400;flex:1 1 220px;line-height:1.45}}
.detail-body{{padding:0;background:var(--bg2)}}
.detail-secondary .tv-wrap{{border-bottom:none}}
.detail-secondary .tv-col{{padding:10px 16px 12px}}
.detail-secondary .tv-hdr{{margin-bottom:7px;font-size:9px;opacity:.95}}
.detail-secondary .tv-row{{margin-bottom:5px;gap:8px}}
.detail-secondary .tv-strike{{font-size:12px;width:100px}}
.detail-secondary .tv-bar{{height:9px;border-radius:3px;
        box-shadow:inset 0 0 0 1px rgba(36,41,47,.06)}}
.detail-secondary .tv-num{{font-size:12px}}
.detail-secondary .gex-wrap{{background:var(--bg2);border-bottom:none;padding:10px 16px 12px}}
.detail-secondary .gex-hdr{{margin-bottom:7px;line-height:1.45}}
.detail-secondary .gex-hdr span{{font-size:9px!important}}
.gex-wrap{{background:var(--bg2);border-bottom:1px solid var(--border);padding:13px 20px}}
.gex-hdr{{margin-bottom:10px}}
.gex-bars{{display:flex;flex-wrap:wrap;gap:6px 24px}}
.gex-bars .tv-row{{min-width:260px}}
footer{{text-align:center;color:var(--mu);padding:16px 24px;font-size:12px;font-weight:400;
        line-height:1.65;border-top:1px solid var(--border);background:var(--bg2)}}
</style>
</head>
<body>
<header>
  <div class="hd-left">
    <div class="logo"><span class="sym">{local_symbol}</span><span class="logo-rest">{_logo_rest}</span></div>
    <div class="hd-sub">{data_label}</div>
  </div>
</header>
{hist_banner}
<main>{sections}</main>
<footer>2SD covers ~95% of expected outcomes based on current IV</footer>
</body>
</html>"""

    if write_html:
        html_path = OUTPUT_DIR / f"{symbol.lower()}_options_2sd.html"
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"HTML -> {html_path}")
        return str(html_path)

    return html


# ── main entry point ──────────────────────────────────────────────────────────

def _run_cli():
    if serve_mode:
        print(f"[serve] Starting HTTP server on http://localhost:{http_port}...")
        with _state_lock:
            _state["html"] = _waiting_html()
            _state["last_updated_ms"] = 0

        poller_thread = threading.Thread(
            target=_poller,
            args=(symbol, serve_interval),
            daemon=True
        )
        poller_thread.start()
        print(f"[serve] Poller daemon started (interval={serve_interval} min)")

        _time.sleep(0.5)
        webbrowser.open(f"http://localhost:{http_port}")
        print(f"[serve] Browser opened at http://localhost:{http_port}")

        print(f"[serve] HTTP server listening... (Ctrl+C to stop)")
        try:
            server = HTTPServer(("127.0.0.1", http_port), _DashboardHandler)
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n[serve] Stopped.")
        finally:
            _release_ib()

    elif loop_mins:
        first = True
        while True:
            (expiry_results, all_data, spot, bid, ask,
             local_symbol, is_futures, ts_str, account_label, data_label) = run_once()
            html_path = build_html(
                expiry_results, spot, bid, ask, local_symbol, is_futures,
                ts_str, account_label, data_label, auto_refresh=True,
                write_html=True)
            if first:
                subprocess.Popen(["start", "", html_path], shell=True)
                first = False
            print(f"Sleeping {loop_mins}m until next refresh...")
            try:
                _time.sleep(loop_mins * 60)
            except KeyboardInterrupt:
                print("\nLoop interrupted.")
                _release_ib()
                break
    else:
        (expiry_results, all_data, spot, bid, ask,
         local_symbol, is_futures, ts_str, account_label, data_label) = run_once()
        html_path = build_html(
            expiry_results, spot, bid, ask, local_symbol, is_futures,
            ts_str, account_label, data_label, auto_refresh=False,
            write_html=True)
        subprocess.Popen(["start", "", html_path], shell=True)


if __name__ == "__main__":
    _apply_cli_args(sys.argv[1:])
    _refresh_symbol_cfg()
    _print_run_banner()
    _run_cli()
