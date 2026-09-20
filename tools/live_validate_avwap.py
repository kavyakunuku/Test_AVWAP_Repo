"""Live 15-min AVWAP + NIFTY/BANKNIFTY universe check against Dhan.

Does NOT place orders. Uses DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN (or config).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.config import load_config
from common.utils import IST, now_ist
from dhan.client import DhanREST
from dhan.instruments import fo_index_universe, load_master_bundle
from dhan.market_data import DhanMarketData
from dhan.option_chain import PacedChainClient
from market.universe import find_atm, select_monthly_expiry, select_weekly_expiries
from strategy.avwap import AvwapState


def vwap(candles, kind="typical"):
    pv = vol = 0.0
    for c in candles:
        tp = c.close if kind == "close" else (c.high + c.low + c.close) / 3.0
        pv += tp * c.volume
        vol += c.volume
    return None if vol <= 0 else pv / vol


def main() -> int:
    cfg = load_config()
    cid = os.environ.get("DHAN_CLIENT_ID") or cfg["dhan"]["client_id"]
    tok = os.environ.get("DHAN_ACCESS_TOKEN") or cfg["dhan"]["access_token"]
    if not cid or not tok:
        print("Need Dhan credentials")
        return 1
    rest = DhanREST(cid, tok, cfg["dhan"].get("rest_base_url", "https://api.dhan.co"))
    md = DhanMarketData(rest)
    chain = PacedChainClient(rest)
    cache = cfg.get("storage", {}).get("instrument_cache_dir", "data/instruments")
    print("Loading instrument master...")
    bundle = load_master_bundle(rest, cache, 12, 250)
    idx = fo_index_universe(bundle.contracts)
    print("Index underlyings in master:", idx)
    for name in ("NIFTY", "BANKNIFTY"):
        print(f"  {name} in master: {name in idx}  uid={bundle.underlying_ids.get(name)}")
        if name not in idx:
            print(f"FAIL: {name} missing from F&O master — cannot trade")
            return 2

    now = now_ist()
    today = now.date()
    from_dt = (now - timedelta(days=90)).replace(hour=9, minute=0, second=0, microsecond=0)
    print(f"\nHistory window {from_dt.date()} -> {today}  (15-min only)")

    for u in ("NIFTY", "BANKNIFTY"):
        uid = int(bundle.underlying_ids[u])
        expiries = chain.expiry_list(uid)
        if u == "NIFTY":
            legs = select_weekly_expiries(today, expiries, 2)
        else:
            m = select_monthly_expiry(today, expiries, 24)
            legs = [m] if m else []
        print(f"\n=== {u} legs={ [str(x) for x in legs] }  (all expiries sample {expiries[:6]}) ===")
        if not legs:
            print(f"FAIL: no expiry legs for {u}")
            return 3
        for exp in legs:
            ch = chain.chain(uid, str(exp))
            print(f"  chain {exp} spot={ch.spot} strikes={len(ch.strikes)}")
            if not ch.spot or not ch.strikes:
                print("  FAIL: empty chain")
                return 4
            atm = find_atm(ch.spot, ch.strikes)
            opt = ch.ce.get(atm) or ch.pe.get(atm)
            if not opt:
                print("  FAIL: no ATM contract")
                return 5
            candles = md.intraday_candles(
                opt.security_id, from_dt, now, 15, instrument="OPTIDX"
            )
            print(f"  ATM {atm} {opt.option_type} id={opt.security_id}  candles={len(candles)}")
            if not candles:
                print("  FAIL: no 15-min candles")
                return 6
            first, last = candles[0], candles[-1]
            print(f"  first={datetime.fromtimestamp(first.ts, IST)}  last={datetime.fromtimestamp(last.ts, IST)}")
            print(f"  volume={sum(c.volume for c in candles)}")
            typical = vwap(candles, "typical")
            close_v = vwap(candles, "close")
            st = AvwapState(security_id=opt.security_id)
            for c in candles:
                st.update(c)
            print(f"  AVWAP typical (engine)={st.last_avwap:.6f}")
            print(f"  AVWAP typical (recompute)={typical:.6f}")
            print(f"  AVWAP close-weighted (many charts)={close_v:.6f}")
            if abs(st.last_avwap - typical) > 1e-9:
                print("  FAIL: engine AVWAP != recomputed typical")
                return 7
            delta = abs(typical - close_v) / typical * 100 if typical else 0
            print(f"  typical vs close-weighted gap={delta:.3f}%  "
                  f"(if your chart uses close-AVWAP this is the expected mismatch)")
            # session VWAP of last day vs lifetime — the usual dashboard-vs-chart gap
            last_day = datetime.fromtimestamp(last.ts, IST).date()
            day_bars = [c for c in candles if datetime.fromtimestamp(c.ts, IST).date() == last_day]
            sess = vwap(day_bars, "typical")
            if sess and typical:
                gap = abs(sess - typical) / typical * 100
                print(f"  session VWAP (today only)={sess:.6f}  vs lifetime AVWAP gap={gap:.2f}%")
                print("  >>> If the chart is session VWAP / daily reset, it WILL NOT match the dashboard.")
    print("\nOK: NIFTY + BANKNIFTY are tradeable; 15-min AVWAP math is internally consistent.")
    print("Signals fire only on completed 15-min candles (prev close>=AVWAP and close<AVWAP).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
