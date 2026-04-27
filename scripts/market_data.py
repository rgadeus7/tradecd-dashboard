"""
Market data fetcher — ES futures + SPY across 5 timeframes.

Indicators per timeframe:
  15min / 2H / Daily / Weekly : MA20, MA50, MA200, SuperTrend(10,3), RSI14
  Monthly                      : MA20, SuperTrend, RSI14
  Daily only                   : ATR14

Structural levels:
  Daily   → last 5 days  (date, high, low, close)
  Weekly  → last 5 weeks
  Monthly → last 5 months + last 5 quarters (derived from monthly bars)

Output: data/market_snapshot.json  (always overwritten — current state only)

Usage:
    python scripts/market_data.py
    python scripts/market_data.py --port 4001
"""

import io
import json
import random
import sys
from datetime import date, datetime, timezone
from pathlib import Path

# Ensure UTF-8 output on Windows console
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
from ib_insync import IB, Future, Index, Stock, util

# ── Config ─────────────────────────────────────────────────────────────────────
IBKR_HOST = "127.0.0.1"
IBKR_TIMEOUT = 5
CLIENT_ID_RANGE = (200, 899)

DEFAULT_SYMBOLS = ["ES", "SPY"]

FUTURES_SYMBOLS = frozenset({
    "ES", "MES", "NQ", "MNQ", "RTY", "M2K", "YM", "MYM",
    "CL", "NG", "GC", "SI", "HG", "ZB", "ZN", "6E", "6B", "6J",
})
FUTURES_EXCHANGE = {
    "ES": "CME", "MES": "CME", "NQ": "CME", "MNQ": "CME",
    "RTY": "CME", "M2K": "CME", "YM": "CBOT", "MYM": "CBOT",
    "CL": "NYMEX", "NG": "NYMEX", "GC": "COMEX", "SI": "COMEX",
    "HG": "COMEX", "ZB": "CBOT", "ZN": "CBOT",
    "6E": "CME", "6B": "CME", "6J": "CME",
}
INDEX_SYMBOLS = frozenset({"SPX", "NDX", "RUT", "VIX", "DJX"})
INDEX_EXCHANGE = {"SPX": "CBOE", "NDX": "NASDAQ", "RUT": "ICE",
                  "VIX": "CBOE", "DJX": "CBOE"}

OUTPUT_PATH = Path(__file__).parent.parent / "data" / "market_snapshot.json"
OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

# barSize, duration, MAs to include (all SMA, MA200 only on 15min–weekly)
TIMEFRAMES = {
    "15min":   ("15 mins", "1 M",  ["ma8", "ma20", "ma50", "ma200"]),
    "2H":      ("2 hours", "3 M",  ["ma8", "ma20", "ma50", "ma200"]),
    "daily":   ("1 day",   "1 Y",  ["ma8", "ma20", "ma50", "ma200"]),
    "weekly":  ("1 week",  "5 Y",  ["ma8", "ma20", "ma50", "ma200"]),
    "monthly": ("1 month", "5 Y",  ["ma8", "ma20"]),
}


# ── IBKR connection ────────────────────────────────────────────────────────────
def connect_ibkr(force_port=None):
    util.startLoop()
    ib = IB()
    client_id = random.randint(*CLIENT_ID_RANGE)
    ports = [(force_port, "forced")] if force_port else [
        (4001, "Live Gateway"), (4002, "Paper Gateway"),
        (7496, "Live TWS"),     (7497, "Paper TWS"),
    ]
    for port, label in ports:
        try:
            ib.connect(IBKR_HOST, port, clientId=client_id, timeout=IBKR_TIMEOUT)
            print(f"Connected via {label}:{port} (clientId={client_id})")
            return ib
        except Exception as e:
            print(f"  {label}:{port} — {e}")
    raise RuntimeError("Could not connect to IBKR on any port.")


def resolve_contract(ib, sym):
    """Return (contract, what_to_show) for any symbol."""
    sym = sym.upper()

    if sym in FUTURES_SYMBOLS:
        exchange = FUTURES_EXCHANGE.get(sym, "CME")
        today    = date.today().strftime("%Y%m%d")
        details  = ib.reqContractDetails(Future(sym, exchange=exchange, currency="USD"))
        front    = sorted(
            (d.contract for d in details
             if d.contract.lastTradeDateOrContractMonth[:8] >= today),
            key=lambda c: c.lastTradeDateOrContractMonth
        )[0]
        ib.qualifyContracts(front)
        print(f"  {sym} -> futures front month: {front.localSymbol}")
        return front, "TRADES"

    if sym in INDEX_SYMBOLS:
        exchange = INDEX_EXCHANGE.get(sym, "CBOE")
        contract = Index(sym, exchange, "USD")
        ib.qualifyContracts(contract)
        print(f"  {sym} -> index ({exchange})")
        return contract, "MIDPOINT"

    # Default: stock/ETF
    contract = Stock(sym, "SMART", "USD")
    ib.qualifyContracts(contract)
    print(f"  {sym} -> stock/ETF")
    return contract, "TRADES"


# ── Bar fetching ───────────────────────────────────────────────────────────────
def fetch_bars(ib, contract, bar_size, duration, what="TRADES"):
    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting=bar_size,
        whatToShow=what,
        useRTH=False,
        keepUpToDate=False,
    )
    if not bars:
        return pd.DataFrame()

    rows = []
    for b in bars:
        # b.date is datetime for intraday, date for daily+
        dt = b.date if isinstance(b.date, datetime) else datetime(b.date.year, b.date.month, b.date.day)
        rows.append({"date": dt, "open": b.open, "high": b.high,
                     "low": b.low, "close": b.close, "volume": b.volume})

    df = pd.DataFrame(rows).set_index("date").sort_index()
    return df


# ── Indicators ─────────────────────────────────────────────────────────────────
def _atr(df, period=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, min_periods=period).mean()


def _rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(com=period - 1, min_periods=period).mean()
    return 100 - (100 / (1 + gain / loss))


def _supertrend(df, period=10, mult=3.0):
    atr = _atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2
    upper = hl2 + mult * atr
    lower = hl2 - mult * atr

    st  = [np.nan] * len(df)
    dir_ = [0] * len(df)

    closes = df["close"].values
    up = upper.values
    lo = lower.values

    for i in range(period, len(df)):
        if i == period:
            st[i]   = lo[i]
            dir_[i] = 1
            continue

        if dir_[i - 1] == 1:
            new_st = max(lo[i], st[i - 1])
            if closes[i] < new_st:
                st[i], dir_[i] = up[i], -1
            else:
                st[i], dir_[i] = new_st, 1
        else:
            new_st = min(up[i], st[i - 1])
            if closes[i] > new_st:
                st[i], dir_[i] = lo[i], 1
            else:
                st[i], dir_[i] = new_st, -1

    return pd.Series(st, index=df.index), pd.Series(dir_, index=df.index)


def add_indicators(df, mas, include_atr=False):
    df = df.copy()
    close = df["close"]

    # MAs — all SMA
    for ma in mas:
        period = int(ma.replace("ma", ""))
        df[ma] = close.rolling(period).mean()

    # RSI + RSI MA (9 EMA of RSI — industry standard signal line)
    df["rsi"]    = _rsi(close)
    df["rsi_ma"] = df["rsi"].ewm(span=9, min_periods=9).mean()

    # SuperTrend
    df["supertrend"], df["supertrend_dir"] = _supertrend(df)

    # Bollinger Bands — basis SMA20, three SD levels
    bb_basis      = close.rolling(20).mean()
    bb_std        = close.rolling(20).std()
    df["bb_basis"]    = bb_basis
    df["bb_upper_2"]  = bb_basis + 2.0 * bb_std
    df["bb_lower_2"]  = bb_basis - 2.0 * bb_std
    df["bb_upper_25"] = bb_basis + 2.5 * bb_std
    df["bb_lower_25"] = bb_basis - 2.5 * bb_std
    df["bb_upper_3"]  = bb_basis + 3.0 * bb_std
    df["bb_lower_3"]  = bb_basis - 3.0 * bb_std
    # BB width % — measures volatility expansion/contraction
    df["bb_width"]    = (df["bb_upper_2"] - df["bb_lower_2"]) / bb_basis * 100
    # BB position — where price sits within 2SD bands (0=lower, 100=upper, 50=middle)
    band_range        = df["bb_upper_2"] - df["bb_lower_2"]
    df["bb_position"] = ((close - df["bb_lower_2"]) / band_range * 100).clip(0, 100)

    if include_atr:
        df["atr14"] = _atr(df, 14)

    return df


# ── Structural levels ──────────────────────────────────────────────────────────
def _bar_records(df, n=5):
    subset = df.iloc[-n:]
    return [
        {
            "date":  str(idx.date()) if hasattr(idx, "date") else str(idx)[:10],
            "high":  round(float(row["high"]),  2),
            "low":   round(float(row["low"]),   2),
            "close": round(float(row["close"]), 2),
        }
        for idx, row in subset.iterrows()
    ]


def _quarterly(monthly_df, n=5):
    df = monthly_df.copy()
    df.index = pd.to_datetime(df.index)
    df["q"] = df.index.to_period("Q")
    grouped = df.groupby("q").agg(high=("high", "max"), low=("low", "min"), close=("close", "last"))
    return [
        {
            "quarter": str(q),
            "high":    round(float(r["high"]),  2),
            "low":     round(float(r["low"]),   2),
            "close":   round(float(r["close"]), 2),
        }
        for q, r in grouped.tail(n).iterrows()
    ]


# ── Snapshot builder ───────────────────────────────────────────────────────────
def _v(val):
    """Round float, return None if nan/missing."""
    try:
        f = float(val)
        return None if np.isnan(f) else round(f, 4)
    except (TypeError, ValueError):
        return None


def _pct_from(close, level):
    """% distance of close from a level. Positive = above, negative = below."""
    try:
        f = float(level)
        if np.isnan(f) or f == 0:
            return None
        return round((close - f) / f * 100, 2)
    except (TypeError, ValueError):
        return None


def _sideways(df, weeks):
    """Range of last N weekly bars as % of current close. Low % = consolidation."""
    subset = df.iloc[-weeks:]
    if subset.empty:
        return None
    rng = subset["high"].max() - subset["low"].min()
    close = float(df.iloc[-1]["close"])
    return round(rng / close * 100, 2) if close else None


def _murrey_math(df, frame=64, mult=1.5):
    """
    Calculate Murrey Math levels for the current bar.
    Translated from Pine Script lib_murrey by Nanda86.
    Returns a dict of key levels + zone, or None if insufficient data.
    """
    import math as _math
    lookback = min(int(round(frame * mult)), len(df))
    if len(df) < 8:
        return None

    # Body prices (ignore wicks) for range calculation
    u_price = df[["open", "close"]].max(axis=1)
    l_price = df[["open", "close"]].min(axis=1)

    v_high = float(u_price.iloc[-lookback:].max())
    v_low  = float(l_price.iloc[-lookback:].min())

    shift    = v_low < 0
    tmp_high = -v_low if shift else v_high
    tmp_low  = (-v_low - (v_high - v_low)) if shift else v_low

    log10 = _math.log(10)
    log8  = _math.log(8)
    log2  = _math.log(2)

    try:
        sf_var = _math.log(0.4 * tmp_high) / log10 - _math.floor(_math.log(0.4 * tmp_high) / log10)
        if tmp_high > 25:
            SR = (_math.exp(log10 * (_math.floor(_math.log(0.4 * tmp_high) / log10) + 1))
                  if sf_var > 0 else
                  _math.exp(log10 * _math.floor(_math.log(0.4 * tmp_high) / log10)))
        else:
            SR = 100 * _math.exp(log8 * _math.floor(_math.log(0.005 * tmp_high) / log8))

        n_var1 = _math.log(SR / (tmp_high - tmp_low)) / log8
        n_var2 = n_var1 - _math.floor(n_var1)
        N = 0.0 if n_var1 <= 0 else (_math.floor(n_var1) if n_var2 == 0 else _math.floor(n_var1) + 1)

        SI = SR * _math.exp(-N * log8)
        M  = _math.floor(1.0 / log2 * _math.log((tmp_high - tmp_low) / SI) + 1e-7)
        I  = round((tmp_high + tmp_low) * 0.5 / (SI * _math.exp((M - 1) * log2)))
        Bot = (I - 1) * SI * _math.exp((M - 1) * log2)
        Top = (I + 1) * SI * _math.exp((M - 1) * log2)

        do_shift = (tmp_high - Top > 0.25 * (Top - Bot)) or (Bot - tmp_low > 0.25 * (Top - Bot))
        ER = 1.0 if do_shift else 0.0
        MM = (M + 1 if M < 2 else 0.0) if ER == 1 else M
        NN = (N if M < 2 else N - 1) if ER == 1 else N

        if ER == 1:
            fSI  = SR * _math.exp(-NN * log8)
            fI   = round((tmp_high + tmp_low) * 0.5 / (fSI * _math.exp((MM - 1) * log2)))
            fBot = (fI - 1) * fSI * _math.exp((MM - 1) * log2)
            fTop = (fI + 1) * fSI * _math.exp((MM - 1) * log2)
        else:
            fSI, fBot, fTop = SI, Bot, Top

        inc    = (fTop - fBot) / 8.0
        abs_top = -(fBot - 3 * inc) if shift else fTop + 3 * inc

        levels = {
            "plus_28":    round(abs_top - 1 * inc, 4),   # +2/8 Extreme Overshoot
            "plus_18":    round(abs_top - 2 * inc, 4),   # +1/8 Overshoot
            "eight_eight":round(abs_top - 3 * inc, 4),   # 8/8 Ultimate Resistance
            "seven_eight":round(abs_top - 4 * inc, 4),   # 7/8 Weak / Stop & Reverse
            "six_eight":  round(abs_top - 5 * inc, 4),   # 6/8 Strong Pivot
            "five_eight": round(abs_top - 6 * inc, 4),   # 5/8 Top of Range
            "four_eight": round(abs_top - 7 * inc, 4),   # 4/8 Major S/R Pivot
            "three_eight":round(abs_top - 8 * inc, 4),   # 3/8 Bottom of Range
            "two_eight":  round(abs_top - 9 * inc, 4),   # 2/8 Strong Pivot
            "one_eight":  round(abs_top - 10 * inc, 4),  # 1/8 Weak / Stop & Reverse
            "zero_eight": round(abs_top - 11 * inc, 4),  # 0/8 Ultimate Support
            "minus_18":   round(abs_top - 12 * inc, 4),  # -1/8 Oversold
            "minus_28":   round(abs_top - 13 * inc, 4),  # -2/8 Extreme Oversold
            "increment":  round(inc, 4),
        }

        close = float(df.iloc[-1]["close"])
        zone  = (2.0 if close >= levels["plus_28"] else
                 1.0 if close >= levels["plus_18"] else
                -2.0 if close <= levels["minus_28"] else
                -1.0 if close <= levels["minus_18"] else 0.0)
        zone_label = ("Extreme Overshoot" if zone >= 2 else
                      "Overshoot"         if zone >= 1 else
                      "Extreme Oversold"  if zone <= -2 else
                      "Oversold"          if zone <= -1 else "Normal")

        levels["zone"]       = zone
        levels["zone_label"] = zone_label
        return levels

    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def _detect_fbd_fbo(current_bar, prior_bars):
    """
    Detect Failed Breakdown (FBD) and Failed Breakout (FBO) against a list of prior bar dicts.
    Returns (fbd_levels, fbo_levels) — lists of price levels where failure occurred.
    FBD: current low < prior low but current close > prior low  (bullish rejection)
    FBO: current high > prior high but current close < prior high (bearish rejection)
    """
    cur_low   = float(current_bar["low"])
    cur_high  = float(current_bar["high"])
    cur_close = float(current_bar["close"])

    fbd_levels = []
    fbo_levels = []

    for bar in prior_bars:
        pl = bar["low"]
        ph = bar["high"]

        if cur_low < pl and cur_close > pl:
            fbd_levels.append(round(pl, 4))
        if cur_high > ph and cur_close < ph:
            fbo_levels.append(round(ph, 4))

    return fbd_levels, fbo_levels


def build_tf_snapshot(tf, df):
    _, _, mas = TIMEFRAMES[tf]
    include_atr = (tf == "daily")

    df = add_indicators(df, mas, include_atr)
    last = df.iloc[-1]
    close = float(last["close"])

    snap = {"close": _v(close)}
    snap["current_high"] = _v(float(last["high"]))
    snap["current_low"]  = _v(float(last["low"]))

    for ma in mas:
        snap[ma] = _v(last.get(ma))

    snap["rsi"]            = _v(last.get("rsi"))
    snap["rsi_ma"]         = _v(last.get("rsi_ma"))
    snap["rsi_above_ma"]   = (
        bool(last.get("rsi", 0) > last.get("rsi_ma", 0))
        if last.get("rsi") is not None and last.get("rsi_ma") is not None else None
    )
    snap["supertrend"]     = _v(last.get("supertrend"))
    snap["supertrend_dir"] = (
        "bullish" if last.get("supertrend_dir") == 1
        else "bearish" if last.get("supertrend_dir") == -1
        else None
    )

    if include_atr:
        snap["atr14"] = _v(last.get("atr14"))

    # ── Bollinger Bands ────────────────────────────────────────────────────────
    snap["bb"] = {
        "basis":     _v(last.get("bb_basis")),
        "upper_2":   _v(last.get("bb_upper_2")),
        "lower_2":   _v(last.get("bb_lower_2")),
        "upper_25":  _v(last.get("bb_upper_25")),
        "lower_25":  _v(last.get("bb_lower_25")),
        "upper_3":   _v(last.get("bb_upper_3")),
        "lower_3":   _v(last.get("bb_lower_3")),
        "width_pct": _v(last.get("bb_width")),
        "position":  _v(last.get("bb_position")),  # 0=lower band, 100=upper band
    }
    snap["bb"] = {k: v for k, v in snap["bb"].items() if v is not None}

    # ── Overextension metrics ──────────────────────────────────────────────────
    overext = {}
    for ma in mas:
        val = last.get(ma)
        pct = _pct_from(close, val)
        if pct is not None:
            overext[f"pct_from_{ma}"] = pct
    st_pct = _pct_from(close, last.get("supertrend"))
    if st_pct is not None:
        overext["pct_from_supertrend"] = st_pct

    # Soft flags — MA, SuperTrend, and BB band breaches
    flags = []
    if overext.get("pct_from_ma200") is not None and abs(overext["pct_from_ma200"]) > 15:
        flags.append("overextended_from_ma200")
    if overext.get("pct_from_ma50") is not None and abs(overext["pct_from_ma50"]) > 10:
        flags.append("overextended_from_ma50")
    if overext.get("pct_from_ma20") is not None and abs(overext["pct_from_ma20"]) > 7:
        flags.append("overextended_from_ma20")
    if overext.get("pct_from_supertrend") is not None and abs(overext["pct_from_supertrend"]) > 5:
        flags.append("overextended_from_supertrend")

    bb_pos = last.get("bb_position")
    if bb_pos is not None:
        if close >= _v(last.get("bb_upper_3")) if last.get("bb_upper_3") else False:
            flags.append("above_bb_3sd")
        elif close >= _v(last.get("bb_upper_25")) if last.get("bb_upper_25") else False:
            flags.append("above_bb_25sd")
        elif close <= _v(last.get("bb_lower_3")) if last.get("bb_lower_3") else False:
            flags.append("below_bb_3sd")
        elif close <= _v(last.get("bb_lower_25")) if last.get("bb_lower_25") else False:
            flags.append("below_bb_25sd")

    if overext:
        snap["overextension"] = {**overext, "flags": flags}

    # ── Murrey Math levels ─────────────────────────────────────────────────────
    mm = _murrey_math(df)
    if mm:
        # Detect recent band expansion: compare current increment to prior frame
        mm_prior = _murrey_math(df.iloc[:-5]) if len(df) > 10 else None
        if mm_prior:
            cur_inc   = mm.get("increment", 0)
            prior_inc = mm_prior.get("increment", 0)
            if prior_inc and cur_inc > prior_inc * 1.05:
                # Bands expanded — check if current price was above prior 8/8 or +1/8
                close = float(df.iloc[-1]["close"])
                prior_88  = mm_prior.get("eight_eight", 0)
                prior_p18 = mm_prior.get("plus_18", 0)
                if close >= prior_p18:
                    mm["expanded"] = True
                    mm["prior_zone"] = "Extreme Overshoot" if close >= mm_prior.get("plus_28", float("inf")) else "Overshoot"
                    mm["prior_eight_eight"] = prior_88
                    mm["prior_plus_18"]     = prior_p18
                    mm["prior_plus_28"]     = mm_prior.get("plus_28")
        snap["murrey"] = mm

    # ── Sideways detection (weekly only) ──────────────────────────────────────
    if tf == "weekly":
        r10 = _sideways(df, 10)
        r15 = _sideways(df, 15)
        sideways = {
            "range_10w_pct": r10,
            "range_15w_pct": r15,
            "consolidating_10w": r10 is not None and r10 < 5,
            "consolidating_15w": r15 is not None and r15 < 6,
        }
        snap["sideways"] = {k: v for k, v in sideways.items() if v is not None}

    # ── Structural levels ──────────────────────────────────────────────────────
    if tf == "daily":
        snap["last_5_days"]     = _bar_records(df, 5)
    elif tf == "weekly":
        snap["last_5_weeks"]    = _bar_records(df, 5)
    elif tf == "monthly":
        snap["last_5_months"]   = _bar_records(df, 5)
        snap["last_5_quarters"] = _quarterly(df, 5)

    # Drop None values
    return {k: v for k, v in snap.items() if v is not None}


# ── Cross-TF FBD/FBO enrichment ───────────────────────────────────────────────
# Which higher-TF structural level keys to check for each target TF
_CROSS_TF_SOURCES = {
    "15min":   [("daily",   "last_5_days"),
                ("weekly",  "last_5_weeks")],
    "2H":      [("daily",   "last_5_days"),
                ("weekly",  "last_5_weeks"),
                ("monthly", "last_5_months"),
                ("monthly", "last_5_quarters")],
    "daily":   [("weekly",  "last_5_weeks"),
                ("monthly", "last_5_months"),
                ("monthly", "last_5_quarters")],
    "weekly":  [("monthly", "last_5_months"),
                ("monthly", "last_5_quarters")],
    "monthly": [("monthly", "last_5_months"),
                ("monthly", "last_5_quarters")],
}

_SOURCE_LABEL = {
    "last_5_days":     "Daily",
    "last_5_weeks":    "Weekly",
    "last_5_months":   "Monthly",
    "last_5_quarters": "Quarterly",
}


def enrich_cross_tf_fbd_fbo(sym_data: dict) -> None:
    """
    For each target TF, check its current bar against higher-TF structural levels.
    Stores fbd_levels / fbo_levels as list of {"level": float, "source": str}.
    Mutates sym_data in place.
    """
    for target_tf, sources in _CROSS_TF_SOURCES.items():
        tf_data = sym_data.get(target_tf)
        if not isinstance(tf_data, dict) or "close" not in tf_data:
            continue

        current_bar = {
            "low":   tf_data.get("current_low",  tf_data["close"]),
            "high":  tf_data.get("current_high", tf_data["close"]),
            "close": tf_data["close"],
        }

        fbd_levels = []
        fbo_levels = []

        for source_tf, level_key in sources:
            # For monthly self-check, exclude the current bar (last record = current month)
            bars = sym_data.get(source_tf, {}).get(level_key, [])
            if not bars:
                continue
            # If checking monthly against itself, skip the last bar (current period)
            if source_tf == target_tf:
                bars = bars[:-1]

            label = _SOURCE_LABEL.get(level_key, source_tf.title())
            fbd, fbo = _detect_fbd_fbo(current_bar, bars)
            for lvl in fbd:
                fbd_levels.append({"level": lvl, "source": label})
            for lvl in fbo:
                fbo_levels.append({"level": lvl, "source": label})

        # Deduplicate levels within 0.5% of each other (keep first occurrence)
        def _dedup(items):
            seen = []
            for item in items:
                lvl = item["level"]
                if not any(abs(lvl - s["level"]) / max(s["level"], 0.01) < 0.005 for s in seen):
                    seen.append(item)
            return seen

        if fbd_levels:
            tf_data["fbd_levels"] = _dedup(fbd_levels)
        if fbo_levels:
            tf_data["fbo_levels"] = _dedup(fbo_levels)


# ── Main ───────────────────────────────────────────────────────────────────────
def fetch_all(symbols=None, force_port=None, telegram=True):
    """
    Fetch market data for given symbols and save snapshot.
    symbols: list of tickers e.g. ["ES", "SPY", "QQQ", "SMH"]
             Defaults to DEFAULT_SYMBOLS if not provided.
    """
    if not symbols:
        symbols = DEFAULT_SYMBOLS
    symbols = [s.upper() for s in symbols]

    ib = connect_ibkr(force_port)
    ib.reqMarketDataType(2)

    try:
        result = {}

        for sym in symbols:
            print(f"\n-- {sym} ----------------------------------")
            try:
                contract, what = resolve_contract(ib, sym)
            except Exception as e:
                print(f"  Could not resolve contract for {sym}: {e}")
                continue

            sym_snap = {}
            for tf, (bar_size, duration, _) in TIMEFRAMES.items():
                print(f"  {tf}: {bar_size} x {duration}", end=" ... ", flush=True)
                try:
                    df = fetch_bars(ib, contract, bar_size, duration, what)
                    if df.empty:
                        print("no data")
                        continue
                    print(f"{len(df)} bars")
                    sym_snap[tf] = build_tf_snapshot(tf, df)
                except Exception as e:
                    print(f"ERROR -- {e}")
                    sym_snap[tf] = {}

            enrich_cross_tf_fbd_fbo(sym_snap)
            sym_snap["_fetched_at"] = datetime.now(timezone.utc).isoformat()
            result[sym] = sym_snap

        # Merge with existing snapshot — only update symbols we just fetched
        existing = {}
        if OUTPUT_PATH.exists() and OUTPUT_PATH.stat().st_size > 0:
            try:
                with open(OUTPUT_PATH) as f:
                    existing = json.load(f)
            except json.JSONDecodeError:
                print("Warning: existing snapshot was corrupt — starting fresh")

        existing_symbols = existing.get("symbols", {})
        existing_symbols.update(result)  # overwrite only fetched symbols

        snapshot = {
            "timestamp":    datetime.now(timezone.utc).isoformat(),
            "symbols":      existing_symbols,
            "last_fetched": symbols,
        }

        # Score all symbols after merge
        import sys as _sys
        _root = str(Path(__file__).parent.parent)
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        from scripts.scoring import score_snapshot
        from tools.telegram import notify_snapshot
        snapshot = score_snapshot(snapshot)

        with open(OUTPUT_PATH, "w") as f:
            json.dump(snapshot, f, indent=2)
        print(f"\nSnapshot saved -> {OUTPUT_PATH}  (symbols: {list(existing_symbols.keys())})")

        # Send Telegram notification for fetched symbols only
        if telegram:
            notify_snapshot(snapshot, symbols=symbols)

        return snapshot

    finally:
        ib.disconnect()
        print("Disconnected.")


if __name__ == "__main__":
    args = sys.argv[1:]
    port       = int(args[args.index("--port") + 1]) if "--port" in args else None
    no_tg      = "--no-telegram" in args
    syms       = [a for a in args if not a.startswith("--") and not a.lstrip("-").isdigit()]
    fetch_all(symbols=syms or None, force_port=port, telegram=not no_tg)
