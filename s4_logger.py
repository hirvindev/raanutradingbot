"""
s4_logger.py — records the S4 options-flow signal WITHOUT trading it
=====================================================================
S4's premise: unusual call activity in the weekly expiry is a bullish tell.
That premise is untested, and it cannot be tested the way S1/S2/S3 were —
`backtest.py` replays daily OHLCV history, and there is no historical option
chain in this stack (yfinance does not serve it; Alpaca's options history needs
OPRA plus a paid plan). So the only way to get evidence is to record the signal
forward, day by day, and wait.

That is all this module does. It places no orders and touches no trading path.

The experiment
--------------
Every run records the day's ranked candidates and, on later runs, fills in what
those names actually did 1, 5 and 20 trading days on. Two cohorts are tagged so
that two separate questions get answered:

  FLOW      — ranked on option flow alone
  FLOW+TECH — the same names that ALSO passed the MACD / RSI / volume / trend
              check

If FLOW+TECH does no better than FLOW, the technical filter is decoration and
the flow is doing the work. If FLOW alone does no better than the universe
baseline, the premise is wrong and no filter will save it. Both outcomes are
worth knowing and neither costs anything to find out.

A baseline is recorded alongside every cohort: the equal-weight forward return
of the whole scanned universe over the same window. Without it a "+2% in 5 days"
result is unreadable — the entire market may have risen 2.5%.

Why call SHARE and not raw call volume
--------------------------------------
Measured on 2026-08-12 against this project's 472-name universe, the top of the
raw-call-volume ranking was MU (48.3% call share), QQQ (44.0%) and PLTR (46.6%)
— three names where PUTS outnumbered calls. Raw volume ranks liquidity, so it
returns the same mega caps every day and can point at bearish flow while
appearing bullish. Call share discriminates and actually varies day to day.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from datadir import state_path

log = logging.getLogger("raanu.s4log")

LOG_PATH = state_path("s4_flow_log.json")

# A name needs real two-sided activity before its call share means anything.
# At 500 contracts a single block trade sets the ratio.
MIN_TOTAL_OPTION_VOL = 5_000

# Call share at or above this is "lopsided enough to be worth recording".
MIN_CALL_SHARE = 0.65

TOP_N = 5
FORWARD_DAYS = (1, 5, 20)


# ---------- technical confirmation (the MACD / RSI / volume check) ----------
def _tech_context(df: Optional[pd.DataFrame]) -> dict:
    """MACD, RSI, volume and trend for one name. Never raises."""
    out = {"ok": False, "reason": "no data"}
    if df is None or df.empty or len(df) < 210:
        return out
    try:
        close = df["Close"].astype(float)
        vol = df["Volume"].astype(float)

        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rsi = float((100 - 100 / (1 + gain / loss.replace(0, np.nan))).iloc[-1])

        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal = macd_line.ewm(span=9, adjust=False).mean()
        macd_bull = bool(macd_line.iloc[-1] > signal.iloc[-1])
        macd_fresh = bool(macd_bull and macd_line.iloc[-2] <= signal.iloc[-2])

        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1])
        price = float(close.iloc[-1])
        uptrend = bool(price > ema200 and ema50 > ema200)

        vol_ratio = float(vol.iloc[-1] / vol.rolling(20).mean().iloc[-1])
        up_day = bool(close.iloc[-1] > close.iloc[-2])

        # The user's stated filter: MACD bullish, RSI not already overbought,
        # volume confirming, and the name in a confirmed uptrend. The trend
        # gate is this project's standing rule — every other engine caps or
        # rejects names below the 200-day, and options flow is no reason to
        # make an exception.
        passed = bool(macd_bull and 40 <= rsi <= 70 and vol_ratio >= 1.0
                      and up_day and uptrend)

        return {
            "ok": True, "price": round(price, 2), "rsi": round(rsi, 1),
            "macd_bull": macd_bull, "macd_fresh": macd_fresh,
            "vol_ratio": round(vol_ratio, 2), "up_day": up_day,
            "uptrend": uptrend, "passed": passed,
        }
    except Exception as e:
        return {"ok": False, "reason": str(e)}


# ---------- persistence ----------
def _load() -> dict:
    if LOG_PATH.exists():
        try:
            return json.loads(LOG_PATH.read_text())
        except Exception:
            log.warning("[S4] flow log unreadable — starting a new one")
    return {"runs": []}


def _save(data: dict):
    try:
        LOG_PATH.write_text(json.dumps(data, indent=2, default=str))
    except Exception as e:
        log.error(f"[S4] could not write flow log: {e}")


# ---------- forward returns ----------
def _fill_forward_returns(data: dict, closes: dict[str, pd.Series]):
    """Fill in what each recorded name did, once enough sessions have passed.

    Returns are measured from the close of the recording day, because that is
    when the signal was known. Anything measured from an earlier price would be
    lookahead — the same discipline backtest.py applies.
    """
    for run in data.get("runs", []):
        run_day = str(run.get("date", ""))[:10]
        for rec in run.get("picks", []) + run.get("baseline_sample", []):
            s = closes.get(rec.get("ticker"))
            if s is None or s.empty:
                continue
            idx = s.index[s.index.astype(str).str[:10] <= run_day]
            if len(idx) == 0:
                continue
            i0 = s.index.get_loc(idx[-1])
            base = float(s.iloc[i0])
            if base <= 0:
                continue
            fwd = rec.setdefault("fwd", {})
            for d in FORWARD_DAYS:
                key = f"d{d}"
                if fwd.get(key) is not None:
                    continue                      # already filled; never revise
                if i0 + d < len(s):
                    fwd[key] = round((float(s.iloc[i0 + d]) / base - 1) * 100, 2)


def run_once(universe: Optional[list[str]] = None) -> dict:
    """Scan option flow, record the ranked names, backfill past forward returns."""
    from options_flow import scan_call_flow, weekly_expiry, stock_only
    from scanner import FALLBACK_UNIVERSE
    from strategy import batch_download

    # Single stocks only — index and sector ETF options are a different
    # instrument (see options_flow.ETF_SYMBOLS).
    universe = stock_only(universe or FALLBACK_UNIVERSE)
    expiry = weekly_expiry()
    flow = scan_call_flow(universe, expiry)

    liquid = [f for f in flow.values() if f["total_vol"] >= MIN_TOTAL_OPTION_VOL]
    ranked = sorted(liquid, key=lambda x: -x["call_share"])
    shortlist = [f for f in ranked if f["call_share"] >= MIN_CALL_SHARE][:TOP_N]

    # Price history for the shortlist plus a baseline sample of the universe.
    # The baseline is what makes the result readable: a cohort return only means
    # something against what the market did over the identical window.
    baseline_tickers = universe[::7][:60]
    need = sorted({f["ticker"] for f in shortlist} | set(baseline_tickers))
    frames = batch_download(need)
    closes = {t: df["Close"].astype(float)
              for t, df in frames.items() if df is not None and not df.empty}

    picks = []
    for f in shortlist:
        tech = _tech_context(frames.get(f["ticker"]))
        picks.append({
            "ticker": f["ticker"],
            "call_vol": f["call_vol"], "put_vol": f["put_vol"],
            "total_vol": f["total_vol"], "call_share": f["call_share"],
            "call_put_ratio": f["call_put_ratio"],
            "top_call_contract": f["top_call_contract"],
            "cohort": "flow+tech" if tech.get("passed") else "flow",
            "tech": tech,
            "fwd": {},
        })

    data = _load()
    data["runs"].append({
        "date": datetime.now(timezone.utc).date().isoformat(),
        "ts": datetime.now(timezone.utc).isoformat(),
        "expiry": expiry.isoformat(),
        "feed": (shortlist[0]["feed"] if shortlist else None),
        "scanned": len(universe),
        "with_activity": len(flow),
        "liquid": len(liquid),
        "picks": picks,
        "baseline_sample": [{"ticker": t, "fwd": {}} for t in baseline_tickers
                            if t in closes],
    })

    _fill_forward_returns(data, closes)
    _save(data)

    log.info(f"[S4] flow logged — {len(picks)} candidates "
             f"({sum(1 for p in picks if p['cohort']=='flow+tech')} passed tech), "
             f"{len(data['runs'])} runs on file")
    return data["runs"][-1]


def summary() -> dict:
    """Average forward returns per cohort, against the universe baseline."""
    data = _load()
    buckets: dict[str, dict[str, list]] = {}

    for run in data.get("runs", []):
        for rec in run.get("picks", []):
            b = buckets.setdefault(rec.get("cohort", "flow"), {})
            for d in FORWARD_DAYS:
                v = (rec.get("fwd") or {}).get(f"d{d}")
                if v is not None:
                    b.setdefault(f"d{d}", []).append(v)
        for rec in run.get("baseline_sample", []):
            b = buckets.setdefault("baseline", {})
            for d in FORWARD_DAYS:
                v = (rec.get("fwd") or {}).get(f"d{d}")
                if v is not None:
                    b.setdefault(f"d{d}", []).append(v)

    out = {}
    for cohort, per_day in buckets.items():
        out[cohort] = {
            k: {"n": len(v), "avg_pct": round(sum(v) / len(v), 2),
                "win_rate": round(sum(1 for x in v if x > 0) / len(v) * 100, 1)}
            for k, v in sorted(per_day.items()) if v
        }

    return {
        "runs": len(data.get("runs", [])),
        "first_run": data["runs"][0]["date"] if data.get("runs") else None,
        "last_run": data["runs"][-1]["date"] if data.get("runs") else None,
        "cohorts": out,
        # Stated rather than left to be inferred: a handful of observations
        # cannot separate a real edge from noise, and the temptation to read
        # one is exactly what this log exists to resist.
        "verdict": _verdict(out),
    }


def _verdict(cohorts: dict) -> str:
    n = min([cohorts.get(c, {}).get("d5", {}).get("n", 0)
             for c in ("flow", "baseline")] or [0])
    if n < 30:
        return (f"Not enough data — {n} 5-day observations. "
                "Needs ~30+ per cohort before the comparison means anything.")
    flow = cohorts.get("flow", {}).get("d5", {}).get("avg_pct")
    base = cohorts.get("baseline", {}).get("d5", {}).get("avg_pct")
    if flow is None or base is None:
        return "Incomplete cohorts."
    edge = round(flow - base, 2)
    return (f"5-day: flow {flow:+.2f}% vs baseline {base:+.2f}% "
            f"(edge {edge:+.2f}pp over {n} observations)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.ERROR)
    run = run_once()
    print(json.dumps({k: v for k, v in run.items() if k != "baseline_sample"},
                     indent=2)[:2000])
    print("\nSUMMARY:", json.dumps(summary(), indent=2))
