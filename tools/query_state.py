"""
tools/query_state.py — ad-hoc analysis over stored state
=========================================================
The composite-key model exists so questions like "what did S3 do last month"
are a key-range read rather than a full-log walk. This is the CLI over that.

    python -m tools.query_state entities
    python -m tools.query_state trades --since 2026-08-01 --strategy s3
    python -m tools.query_state trades --action SELL --format csv > sells.csv
    python -m tools.query_state picks  --strategy s2 --matured
    python -m tools.query_state pnl                       # realized P&L by strategy
    python -m tools.query_state scores                    # do higher scores earn more?
    python -m tools.query_state sizes                     # item sizes vs the 400KB cap

Runs against whatever backend the environment selects, so the same commands
work on a local `.env` checkout and against DynamoDB with
`STATE_BACKEND=dynamodb STATE_TABLE=...`.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict

from raanu import state
from raanu.state import keys


def _rows(pk, **kw):
    return [r.data for r in state.query(pk, **kw)]


def _emit(rows: list[dict], fmt: str, columns: list[str] | None = None) -> None:
    if not rows:
        print("(no rows)")
        return
    if fmt == "json":
        print(json.dumps(rows, indent=2, default=str))
        return
    cols = columns or sorted({k for r in rows for k in r if not isinstance(r[k], (dict, list))})
    if fmt == "csv":
        writer = csv.DictWriter(sys.stdout, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))
    print(f"\n{len(rows)} row(s)")


def cmd_entities(_args) -> None:
    """What is stored, and how much of it."""
    print(f"{'entity':10s} {'items':>7s}   {'bytes':>10s}   largest item")
    for pk in keys.ALL_ENTITIES:
        records = state.query(pk)
        if not records:
            continue
        sizes = [(state.estimate_size(pk, r.sk, r.data), r.sk) for r in records]
        total = sum(s for s, _ in sizes)
        big, big_sk = max(sizes)
        print(f"{pk:10s} {len(records):7d}   {total:10,d}   {big:6,d} B  {big_sk[:44]}")


def cmd_trades(args) -> None:
    filters = {}
    if args.strategy:
        filters["strategy"] = args.strategy
    if args.action:
        filters["action"] = args.action.upper()
    rows = _rows(keys.TRADE, sk_gte=args.since, sk_lte=args.until,
                 filters=filters or None, limit=args.limit)
    _emit(rows, args.format,
          ["timestamp", "action", "ticker", "strategy", "notional_usd",
           "entry_price", "exit_price", "realized_pnl", "return_pct", "exit_reason"])


def cmd_picks(args) -> None:
    filters = {}
    if args.strategy:
        filters["strategy"] = args.strategy
    if args.matured:
        filters["matured"] = True
    rows = _rows(keys.PICK, sk_gte=args.since, sk_lte=args.until,
                 filters=filters or None, limit=args.limit)
    for r in rows:
        for d in (1, 5, 20):
            r[f"d{d}"] = (r.get("fwd") or {}).get(f"d{d}")
            r[f"spy{d}"] = (r.get("spy") or {}).get(f"d{d}")
    _emit(rows, args.format,
          ["date", "strategy", "ticker", "score", "price_at_pick",
           "d1", "spy1", "d5", "spy5", "d20", "spy20", "matured"])


def cmd_pnl(args) -> None:
    """Realized P&L by strategy — the numbers Kelly sizes from."""
    rows = _rows(keys.TRADE, sk_gte=args.since, filters={"action": "SELL"})
    by = defaultdict(list)
    for r in rows:
        if r.get("realized_pnl") is not None:
            by[r.get("strategy") or "unknown"].append(float(r["realized_pnl"]))
    if not by:
        print("(no closed trades yet)")
        return
    out = []
    for strat, pnls in sorted(by.items()):
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
        out.append({
            "strategy": strat,
            "n": len(pnls),
            "win_rate": round(100 * len(wins) / len(pnls), 1),
            "net": round(sum(pnls), 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(-avg_loss, 2),
            # Payoff below 1 with a sub-50% win rate is the losing combination.
            "payoff": round(avg_win / avg_loss, 2) if avg_loss else None,
        })
    _emit(out, args.format,
          ["strategy", "n", "win_rate", "net", "avg_win", "avg_loss", "payoff"])
    print("\nExpectancy, not win rate, decides profitability: a high win rate is")
    print("trivially bought by taking profits early, and you pay for every point.")


def cmd_scores(args) -> None:
    """Do higher scores earn higher forward returns? The whole point of picks_log."""
    rows = _rows(keys.PICK, sk_gte=args.since)
    bands = ((90, 200, "90+"), (80, 90, "80-89"), (70, 80, "70-79"), (0, 70, "60-69"))
    out = []
    for lo, hi, label in bands:
        sub = [r for r in rows if lo <= (r.get("score") or 0) < hi]
        if not sub:
            continue
        row = {"band": label, "n": len(sub)}
        for d in (1, 5, 20):
            vals = [(r.get("fwd") or {}).get(f"d{d}") for r in sub]
            spy = [(r.get("spy") or {}).get(f"d{d}") for r in sub]
            vals = [v for v in vals if v is not None]
            spy = [v for v in spy if v is not None]
            row[f"d{d}"] = round(sum(vals) / len(vals), 2) if vals else None
            row[f"edge{d}"] = (round(sum(vals) / len(vals) - sum(spy) / len(spy), 2)
                               if vals and spy else None)
        out.append(row)
    _emit(out, args.format, ["band", "n", "d1", "edge1", "d5", "edge5", "d20", "edge20"])
    matured = sum(1 for r in rows if (r.get("fwd") or {}).get("d5") is not None)
    if matured < 30:
        print(f"\n{matured} picks have a 5-day result. Needs ~30 before the bands")
        print("mean anything — a handful cannot separate an edge from noise.")


def cmd_sizes(_args) -> None:
    """How close anything is to DynamoDB's 400KB item ceiling."""
    from raanu.state.backends import MAX_ITEM_BYTES
    worst = []
    for pk in keys.ALL_ENTITIES:
        for r in state.query(pk):
            worst.append((state.estimate_size(pk, r.sk, r.data), pk, r.sk))
    if not worst:
        print("(nothing stored)")
        return
    worst.sort(reverse=True)
    print(f"guard {MAX_ITEM_BYTES:,d} B / hard limit 409,600 B\n")
    print(f"{'bytes':>9s}  {'% guard':>8s}  entity     sort key")
    for size, pk, sk in worst[:15]:
        print(f"{size:9,d}  {100*size/MAX_ITEM_BYTES:7.2f}%  {pk:10s} {sk[:52]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, dated=True):
        p.add_argument("--format", choices=("table", "json", "csv"), default="table")
        p.add_argument("--limit", type=int)
        if dated:
            p.add_argument("--since", help="inclusive sort-key lower bound, e.g. 2026-08-01")
            p.add_argument("--until", help="inclusive sort-key upper bound")

    sub.add_parser("entities", help="what is stored and how much").set_defaults(fn=cmd_entities)

    p = sub.add_parser("trades", help="query the trade log")
    p.add_argument("--strategy"); p.add_argument("--action", choices=("BUY", "SELL", "buy", "sell"))
    common(p); p.set_defaults(fn=cmd_trades)

    p = sub.add_parser("picks", help="query recorded picks")
    p.add_argument("--strategy"); p.add_argument("--matured", action="store_true")
    common(p); p.set_defaults(fn=cmd_picks)

    p = sub.add_parser("pnl", help="realized P&L by strategy")
    common(p); p.set_defaults(fn=cmd_pnl)

    p = sub.add_parser("scores", help="forward returns by score band")
    common(p); p.set_defaults(fn=cmd_scores)

    sub.add_parser("sizes", help="item sizes vs the 400KB cap").set_defaults(fn=cmd_sizes)

    args = ap.parse_args()
    for attr in ("since", "until", "limit", "format"):
        if not hasattr(args, attr):
            setattr(args, attr, None if attr != "format" else "table")
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
