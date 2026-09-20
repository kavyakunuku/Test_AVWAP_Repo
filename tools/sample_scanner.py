"""One-shot 15-min ANCHORED VWAP scanner for NIFTY + BANKNIFTY (no orders).

This is NOT session VWAP. AVWAP is cumulative from a fixed anchor candle
and never resets at 09:15.

Default --days 10: anchor = first completed 15m bar in the last 10 calendar
days (easy to drop the same anchor on a chart). Also prints lifetime AVWAP
from the first tradable bar in the fetch (strategy rule).

Usage:
  DHAN_CLIENT_ID=... DHAN_ACCESS_TOKEN=... python tools/sample_scanner.py --days 10
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.config import load_config
from common.utils import IST, candle_start_for, epoch, from_epoch, now_ist
from dhan.client import DhanREST
from dhan.instruments import load_master_bundle
from dhan.market_data import DhanMarketData
from dhan.option_chain import PacedChainClient
from market.universe import (
    ce_universe_strikes,
    find_atm,
    pe_universe_strikes,
    select_monthly_expiry,
    select_weekly_expiries,
)
from strategy.avwap import AvwapState
from strategy import rules


def _avwap(candles):
    st = AvwapState(security_id=candles[0].security_id if candles else "")
    prev_c = prev_a = None
    for c in candles:
        prev_c, prev_a = st.last_close, st.last_avwap
        st.update(c)
    return st, prev_c, prev_a


def trigger(st, prev_c, prev_a, last):
    if st.last_avwap is None or last is None:
        return "n/a"
    if rules.is_entry_cross(prev_c, prev_a, last.close, st.last_avwap):
        return "ENTRY (cross below)"
    if rules.is_exit_close(last.close, st.last_avwap):
        return "EXIT if short (close above)"
    d = (last.close - st.last_avwap) / st.last_avwap * 100
    if last.close >= st.last_avwap:
        return f"above {d:+.2f}% (need cross below for entry)"
    return f"below {d:+.2f}% (re-arm above first)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10,
                    help="Anchor AVWAP at the first 15m bar of this lookback (default 10)")
    args = ap.parse_args()

    cfg = load_config()
    cid = os.environ.get("DHAN_CLIENT_ID") or cfg["dhan"]["client_id"]
    tok = os.environ.get("DHAN_ACCESS_TOKEN") or cfg["dhan"]["access_token"]
    if not cid or not tok:
        print("Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN")
        return 1

    rest = DhanREST(cid, tok, cfg["dhan"].get("rest_base_url", "https://api.dhan.co"))
    md = DhanMarketData(rest)
    chain_api = PacedChainClient(rest)
    cache = cfg.get("storage", {}).get("instrument_cache_dir", "data/instruments")
    print("Loading instrument master…")
    bundle = load_master_bundle(rest, cache, 12, 250)

    now = now_ist()
    today = now.date()
    from_dt = (now - timedelta(days=max(args.days, 1))).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    itm_n = int(cfg.get("strategy", {}).get("itm_strikes_per_side", 4))
    cur = candle_start_for(now, 15)
    drop_ts = epoch(cur) if cur is not None else None

    rows = []
    print(f"Clock {now.strftime('%Y-%m-%d %H:%M IST')}  15-minute ANCHORED VWAP (not session VWAP)")
    print(f"Anchor window: first 15m bar on/after {from_dt.date()}  (last {args.days} days)")
    print("Formula: AVWAP = Σ((H+L+C)/3 * V) / ΣV  — cumulative, never resets daily\n")

    for u, weekly_n in (("NIFTY", 2), ("BANKNIFTY", 0)):
        uid = bundle.underlying_ids.get(u)
        if not uid:
            print(f"SKIP {u}: no underlying id")
            continue
        expiries = chain_api.expiry_list(int(uid))
        if weekly_n:
            legs = select_weekly_expiries(today, expiries, weekly_n)
        else:
            m = select_monthly_expiry(today, expiries, 24)
            legs = [m] if m else []
        print(f"=== {u} legs {[str(x) for x in legs]} ===")
        for exp in legs:
            ch = chain_api.chain(int(uid), str(exp))
            if not ch.spot or not ch.strikes:
                print(f"  empty chain {exp}")
                continue
            atm = find_atm(ch.spot, ch.strikes)
            ce_s = ce_universe_strikes(atm, ch.strikes, itm_n)
            pe_s = pe_universe_strikes(atm, ch.strikes, itm_n)
            print(f"  {exp} spot={ch.spot:.2f} ATM={atm:g}  CE={ce_s} PE={pe_s}")
            jobs = []
            for s in ce_s:
                o = ch.ce.get(s)
                if o:
                    jobs.append((s, "CE", o.security_id))
            for s in pe_s:
                o = ch.pe.get(s)
                if o:
                    jobs.append((s, "PE", o.security_id))
            for strike, side, sid in jobs:
                time.sleep(1.0)
                candles = md.intraday_candles(sid, from_dt, now, 15, instrument="OPTIDX")
                candles = sorted(candles, key=lambda c: c.ts)
                if drop_ts is not None:
                    candles = [c for c in candles if c.ts < drop_ts]
                if not candles:
                    print(f"    {u} {strike:g} {side}: NO 15m candles")
                    continue
                st, prev_c, prev_a = _avwap(candles)
                last = candles[-1]
                last_ist = from_epoch(last.ts).strftime("%Y-%m-%d %H:%M")
                anchor = from_epoch(st.anchor_ts).strftime("%Y-%m-%d %H:%M") if st.anchor_ts else "-"
                dltp = None
                if st.last_avwap:
                    dltp = (last.close - st.last_avwap) / st.last_avwap * 100
                row = {
                    "underlying": u,
                    "expiry": str(exp),
                    "strike": strike,
                    "type": side,
                    "security_id": sid,
                    "n_candles": len(candles),
                    "anchor_ist": anchor,
                    "last_candle_ist": last_ist,
                    "last_close": round(last.close, 2),
                    "avwap": round(st.last_avwap, 4) if st.last_avwap else None,
                    "delta_pct": round(dltp, 3) if dltp is not None else None,
                    "trigger": trigger(st, prev_c, prev_a, last),
                    "cum_volume": int(st.cumulative_volume or 0),
                }
                rows.append(row)
                print(
                    f"    {u:10} {str(exp)} {strike:8g} {side}  "
                    f"close={row['last_close']:8.2f}  AVWAP={row['avwap']:10.4f}  "
                    f"Δ={row['delta_pct']:+7.2f}%  bars={len(candles):4d}  "
                    f"anchor={anchor}  last={last_ist}  {row['trigger']}"
                )

    os.makedirs("data", exist_ok=True)
    out = os.path.join("data", "sample_scanner_15m.csv")
    if rows:
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nWrote {len(rows)} rows -> {out}")
        print("On the chart: same contract, 15m, AVWAP anchored at 'anchor_ist', typical (H+L+C)/3, no daily reset.")
    else:
        print("No scanner rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
