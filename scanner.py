"""
scanner.py — US-listed momentum scanner
========================================
Scans US-listed stocks (including ADRs of European giants) using
yfinance batch downloads. All tickers are directly executable on Alpaca.
"""

import logging
from typing import Optional
from strategy import score_from_df, batch_download

log = logging.getLogger("raanu.scanner")

# ── Full scan universe — all US-listed and executable on Alpaca ──────────────
UNIVERSE = [
    # US mega-caps
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA", "AMD", "NFLX", "PYPL",
    # European blue-chips (US ADRs / NYSE-listed)
    "SAP",    # SAP SE
    "SIEGY",  # Siemens
    "ALIZY",  # Allianz
    "DTEGY",  # Deutsche Telekom
    "BAYRY",  # Bayer
    "DB",     # Deutsche Bank
    "ADDYY",  # Adidas
    "RWEOY",  # RWE
    "EONGY",  # E.ON
    "IFNNY",  # Infineon
    "EADSY",  # Airbus
    "MURGY",  # Munich Re
    "VWAGY",  # Volkswagen
    "BASFY",  # BASF
    "HENKY",  # Henkel
    "PUMSY",  # Puma
    "CTTAY",  # Continental
    "FSNUY",  # Fresenius
    # US financials & tech
    "JPM", "GS", "MS", "BAC", "V", "MA",
    "ORCL", "CRM", "ADBE", "INTC", "QCOM",
    # ETFs for broad signals
    "SPY", "QQQ", "IWM",
]

TEST_UNIVERSE = ["AAPL", "NVDA", "MSFT", "GOOGL", "META"]


def find_top_picks(n: int = 3, max_stocks: Optional[int] = None) -> list[dict]:
    """
    Batch-download all US tickers in one yfinance call, score each,
    return top N above threshold. Pass max_stocks for quick test runs.
    """
    universe = TEST_UNIVERSE[:max_stocks] if max_stocks else UNIVERSE
    mode = f"TEST ({len(universe)} stocks)" if max_stocks else f"{len(universe)} stocks"
    log.info(f"Scanning {mode} via batch download...")

    data = batch_download(universe)
    log.info(f"Download done — {len(data)}/{len(universe)} tickers returned data")

    results = []
    for ticker in universe:
        try:
            r = score_from_df(ticker, data.get(ticker))
            if not r.get("ok") or r.get("score", 0) < 60:
                continue
            r["us_adr"]  = ticker   # already a US ticker
            r["exchange"] = "US"
            results.append(r)
        except Exception as e:
            log.debug(f"Score error {ticker}: {e}")

    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    log.info(f"Scanner done — {len(results)} above threshold, returning top {n}")
    return results[:n]


def get_universe_summary() -> dict:
    return {
        "exchange":    "US / ADR",
        "total_stocks": len(UNIVERSE),
    }
