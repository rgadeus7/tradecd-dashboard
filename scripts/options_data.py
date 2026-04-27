"""
Options data fetcher — saves key metrics for 2 expiries per symbol:
  1. Nearest upcoming expiry
  2. Next week's Friday expiry

Output: data/options_snapshot.json

Usage:
    python scripts/options_data.py SPY ES
    python scripts/options_data.py SPY --port 4001
"""

import io
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from ib_insync import Option, FuturesOption, util
import options_report as _or

OUTPUT_PATH = ROOT / "data" / "options_snapshot.json"


def _next_week_friday():
    """Return the Friday of next calendar week."""
    today = datetime.now(timezone.utc).date()
    days_until_friday = (4 - today.weekday()) % 7
    this_friday = today + timedelta(days=days_until_friday)
    next_friday = this_friday + timedelta(days=7)
    return next_friday


def _pick_expiries(expirations: list[str]) -> list[str]:
    """Pick nearest expiry + next week Friday expiry."""
    today     = datetime.now(timezone.utc).date()
    nwf       = _next_week_friday()
    exp_dates = {e: datetime.strptime(e[:8], "%Y%m%d").date() for e in expirations}

    upcoming = [e for e in expirations if exp_dates[e] >= today]
    if not upcoming:
        return expirations[:1]
    nearest = upcoming[0]

    nwf_exp = min(upcoming, key=lambda e: abs((exp_dates[e] - nwf).days))

    result = [nearest]
    if nwf_exp != nearest:
        result.append(nwf_exp)

    labels = {nearest: "nearest", nwf_exp: "next-week-friday"}
    print(f"  Expiries: " + ", ".join(f"{e} ({labels.get(e, '')})" for e in result))
    return result


def _extract_metrics(rows, spot, days, em1, em2, cfg, expiry):
    """Extract key option metrics from a list of option row dicts."""
    calls   = {r["strike"]: r for r in rows if r["right"] == "C"}
    puts    = {r["strike"]: r for r in rows if r["right"] == "P"}
    strikes = sorted(set(calls) | set(puts))
    if not strikes:
        return None

    atm_s = min(strikes, key=lambda s: abs(s - spot))

    total_cv  = sum(r["volume"] or 0 for r in rows if r["right"] == "C")
    total_pv  = sum(r["volume"] or 0 for r in rows if r["right"] == "P")
    total_coi = sum(r["oi"] or 0 for r in rows if r["right"] == "C")
    total_poi = sum(r["oi"] or 0 for r in rows if r["right"] == "P")
    has_oi    = (total_coi + total_poi) > 0
    pcr = (round(total_poi / total_coi, 2) if total_coi else
           round(total_pv  / total_cv,  2) if total_cv  else None)

    # Max pain
    best_mp, best_loss = None, float("inf")
    for target in strikes:
        loss  = sum(max(0, target - s) * (calls[s].get("oi") or calls[s].get("volume") or 0) for s in strikes if s in calls)
        loss += sum(max(0, s - target) * (puts[s].get("oi")  or puts[s].get("volume")  or 0) for s in strikes if s in puts)
        if loss < best_loss:
            best_loss, best_mp = loss, target

    # GEX
    def _gex_w(side, s):
        r = side.get(s, {})
        return r.get("oi") or r.get("volume") or 0

    call_gex = {s: (calls[s].get("gamma") or 0) * _gex_w(calls, s) * 100 for s in strikes if s in calls}
    put_gex  = {s: (puts[s].get("gamma")  or 0) * _gex_w(puts,  s) * 100 for s in strikes if s in puts}
    net_gex  = {s: call_gex.get(s, 0) - put_gex.get(s, 0) for s in strikes}
    has_gex  = any(v != 0 for v in net_gex.values())

    call_wall = (max((s for s in call_gex if s >= spot and call_gex[s] > 0),
                     key=lambda s: call_gex[s], default=None) if has_gex else None)
    put_wall  = (max((s for s in put_gex  if s <= spot and put_gex[s]  > 0),
                     key=lambda s: put_gex[s],  default=None) if has_gex else None)

    # Gamma flip
    gamma_flip = None
    for i in range(1, len(strikes)):
        s1, s2 = strikes[i - 1], strikes[i]
        g1, g2 = net_gex[s1], net_gex[s2]
        if g1 * g2 < 0:
            d = abs(g1) + abs(g2)
            gamma_flip = round(s1 + (s2 - s1) * (abs(g1) / d) if d > 0 else (s1 + s2) / 2, 2)
            break

    # ATM IV + straddle
    atm_tol = cfg["strike_step"] * 2
    atm_iv  = next(
        (r["iv"] for r in rows
         if abs(r["strike"] - atm_s) <= atm_tol and r["right"] == "C" and r.get("iv")),
        None)
    cl       = calls.get(atm_s, {}).get("price")
    pl       = puts.get(atm_s,  {}).get("price")
    straddle = round(cl + pl, 2) if cl and pl else None

    key       = "oi" if has_oi else "volume"
    top_calls = sorted([(s, calls[s][key]) for s in strikes if calls.get(s, {}).get(key)],
                       key=lambda x: -x[1])[:3]
    top_puts  = sorted([(s, puts[s][key])  for s in strikes if puts.get(s, {}).get(key)],
                       key=lambda x: -x[1])[:3]

    return {
        "expiry":            expiry,
        "expiry_fmt":        f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:8]}",
        "dte":               days,
        "spot":              round(spot, 2),
        "atm_strike":        atm_s,
        "iv_pct":            round(atm_iv, 1) if atm_iv else None,
        "straddle":          straddle,
        "em1":               round(em1, 2),
        "em2":               round(em2, 2),
        "range_lo":          round(spot - em2, 2),
        "range_hi":          round(spot + em2, 2),
        "call_wall":         call_wall,
        "put_wall":          put_wall,
        "gamma_flip":        gamma_flip,
        "max_pain":          best_mp,
        "pcr":               pcr,
        "pcr_basis":         "oi" if has_oi else "volume",
        "total_call_oi":     total_coi or None,
        "total_put_oi":      total_poi or None,
        "top_call_strikes":  [s for s, _ in top_calls],
        "top_put_strikes":   [s for s, _ in top_puts],
        "above_gamma_flip":  (spot > gamma_flip) if gamma_flip else None,
    }


def fetch_options(symbols, force_port=None, forced_expiry=None):
    if force_port:
        _or.force_port = force_port

    util.startLoop()
    ib, account_label = _or._ensure_ib()
    ib.reqMarketDataType(2)

    today  = datetime.now(timezone.utc).date()
    result = {}

    try:
        for sym in symbols:
            print(f"\n-- {sym} ----------------------------------")
            cfg = _or.resolve_symbol_config(sym)

            try:
                from ib_insync import IB, Stock, Index, Future
                if cfg["type"] == "IND":
                    from ib_insync import Index as _Index
                    underlying = _Index(sym, cfg["exchange"], cfg["currency"])
                elif cfg["type"] == "FUT":
                    underlying_generic = Future(sym, exchange=cfg["exchange"], currency=cfg["currency"])
                    details = ib.reqContractDetails(underlying_generic)
                    _, underlying = sorted(
                        [(datetime.strptime(d.contract.lastTradeDateOrContractMonth[:8], "%Y%m%d").date(),
                          d.contract)
                         for d in details
                         if datetime.strptime(d.contract.lastTradeDateOrContractMonth[:8], "%Y%m%d").date() >= today],
                        key=lambda x: x[0])[0]
                else:
                    underlying = Stock(sym, cfg["exchange"], cfg["currency"])

                ib.qualifyContracts(underlying)
                [spot_t] = ib.reqTickers(underlying)

                if cfg["type"] == "FUT":
                    spot       = float(spot_t.last if _or._ok(spot_t.last) else spot_t.close)
                    local_sym  = underlying.localSymbol
                else:
                    raw       = spot_t.marketPrice()
                    spot      = float(raw if _or._ok(raw) else spot_t.close)
                    local_sym = sym

                print(f"  Spot: {spot}  ({local_sym})")

                sec_type = cfg["type"]
                con_id   = underlying.conId
                chains   = ib.reqSecDefOptParams(sym, cfg["exchange"] if cfg["type"] == "FUT" else "", sec_type, con_id)
                chain    = max(chains, key=lambda c: len(c.expirations))
                exps     = sorted(e for e in chain.expirations
                                  if (datetime.strptime(e[:8], "%Y%m%d").date() - today).days >= 0)
                print(f"  Chain: {len(exps)} expiries")

                if forced_expiry:
                    selected = [e for fe in forced_expiry
                                for e in exps if e[:8] == fe[:8].replace("-", "")]
                    if not selected:
                        print(f"  WARNING: none of {forced_expiry} found in chain — falling back to auto")
                        selected = _pick_expiries(exps)
                else:
                    selected = _pick_expiries(exps)
                sym_result = {"spot": round(spot, 2), "local_symbol": local_sym, "expiries": {}}

                for expiry in selected:
                    exp_date = datetime.strptime(expiry[:8], "%Y%m%d").date()
                    days     = max((exp_date - today).days, 1)
                    em1      = spot * cfg["fallback_iv"] * math.sqrt(days / 365)
                    em2      = em1 * 2
                    lo, hi   = spot - em2, spot + em2

                    if cfg["type"] in ("STK", "IND"):
                        template = Option(sym, expiry, exchange="SMART")
                    else:
                        template = FuturesOption(sym, expiry, exchange=cfg["exchange"])

                    details  = ib.reqContractDetails(template)
                    all_c    = [d.contract for d in details]
                    filtered = [c for c in all_c if lo <= c.strike <= hi]
                    print(f"  {expiry} DTE={days}: {len(filtered)} contracts in 2SD range")

                    if not filtered:
                        continue

                    ticks = cfg.get("snapshot_generic_ticks", "100,101,106")
                    rows  = []
                    batch_n = 40
                    for i in range(0, len(filtered), batch_n):
                        batch   = filtered[i:i + batch_n]
                        tickers = _or._req_option_batch_streaming(ib, batch, ticks)
                        for t in tickers:
                            c = t.contract
                            g = t.modelGreeks or t.lastGreeks
                            price = (float(t.last) if _or._ok(t.last) else
                                     float(t.close) if _or._ok(t.close) else None)
                            rows.append({
                                "strike": c.strike,
                                "right":  c.right,
                                "price":  price,
                                "volume": int(t.volume) if t.volume and t.volume > 0 else None,
                                "oi":     _or._oi_from_ticker(t, c),
                                "iv":     round(g.impliedVol * 100, 2) if g and g.impliedVol else None,
                                "delta":  round(g.delta,  3) if g and g.delta  else None,
                                "gamma":  round(g.gamma,  6) if g and g.gamma  else None,
                            })

                    metrics = _extract_metrics(rows, spot, days, em1, em2, cfg, expiry)
                    if metrics:
                        sym_result["expiries"][expiry] = metrics
                        print(f"    call_wall={metrics['call_wall']}  put_wall={metrics['put_wall']}  "
                              f"gamma_flip={metrics['gamma_flip']}  max_pain={metrics['max_pain']}  "
                              f"pcr={metrics['pcr']}")

                result[sym] = sym_result

            except Exception as e:
                print(f"  ERROR for {sym}: {e}")
                import traceback; traceback.print_exc()

    finally:
        _or._release_ib()

    existing = {}
    if OUTPUT_PATH.exists() and OUTPUT_PATH.stat().st_size > 0:
        try:
            with open(OUTPUT_PATH) as f:
                existing = json.load(f)
        except json.JSONDecodeError:
            pass

    # Merge at expiry level — new expiries add to existing ones, don't replace them.
    # Exception: if expiries were explicitly forced, replace that symbol's expiries entirely
    # so you only see what you asked for.
    existing_syms = existing.get("symbols", {})
    for sym, sym_data in result.items():
        if sym in existing_syms and not forced_expiry:
            existing_syms[sym]["spot"]         = sym_data["spot"]
            existing_syms[sym]["local_symbol"] = sym_data["local_symbol"]
            existing_syms[sym].setdefault("expiries", {}).update(sym_data.get("expiries", {}))
        else:
            existing_syms[sym] = sym_data

    # Prune expired expiries from all symbols
    today_str = datetime.now(timezone.utc).date().strftime("%Y%m%d")
    for sym_data in existing_syms.values():
        sym_data["expiries"] = {
            k: v for k, v in sym_data.get("expiries", {}).items()
            if k[:8] >= today_str
        }

    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbols":   existing_syms,
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(snapshot, f, indent=2)
    print(f"\nOptions snapshot saved -> {OUTPUT_PATH}")
    return snapshot


if __name__ == "__main__":
    args    = sys.argv[1:]
    port    = int(args[args.index("--port") + 1]) if "--port" in args else None
    expiry  = [e.strip() for e in args[args.index("--expiry") + 1].split(",")] if "--expiry" in args else None
    syms    = [a.upper() for a in args if not a.startswith("--") and not a.lstrip("-").isdigit()]
    if not syms:
        syms = ["SPY", "ES"]
    fetch_options(syms, force_port=port, forced_expiry=expiry)
