"""
scanner.py — XETRA / GETTEX momentum scanner
==============================================
Scans German exchange stocks (DAX 40 + MDAX top picks) using
Yahoo Finance .DE tickers, scores them with our strategy engine,
and maps to US ADR equivalents for Alpaca execution where available.
"""

import logging
from typing import Optional
from strategy import score_ticker

log = logging.getLogger("raanu.scanner")

# ── XETRA / GETTEX universe (DAX 40 + MDAX selection) ──────────────────────
XETRA_UNIVERSE = [
    # DAX 40
    "SAP.DE", "SIE.DE", "ALV.DE", "MRK.DE", "DTE.DE",
    "VOW3.DE", "BMW.DE", "BAS.DE", "BAYN.DE", "DBK.DE",
    "MBG.DE", "ADS.DE", "DHL.DE", "RWE.DE", "EOAN.DE",
    "IFX.DE", "FRE.DE", "HEN3.DE", "BEI.DE", "CON.DE",
    "VNA.DE", "MTX.DE", "SHL.DE", "QIA.DE", "HEI.DE",
    "ENR.DE", "DHER.DE", "HAG.DE", "SY1.DE", "G1A.DE",
    "MUV2.DE", "ZAL.DE", "1COV.DE", "AIR.DE", "PUM.DE",
    "EVT.DE", "SRT3.DE", "HFG.DE", "SDAX.DE",
    # MDAX / high momentum picks
    "AFX.DE", "NDX1.DE", "VBK.DE", "GXI.DE", "WAF.DE",
    "BC8.DE", "AIXA.DE", "DUE.DE", "KGX.DE", "EMG.DE",
    # US mega-caps listed on XETRA
    "APC.DE", "MSF.DE", "AMZN.DE", "GOOGL.DE", "META.DE",
    "NVDA.DE", "TSLA.DE", "AMD.DE", "NFLX.DE", "PYPL.DE",
]

# XETRA ticker → US-listed equivalent for Alpaca execution
# None = no liquid US ADR, alert only
XETRA_TO_US_ADR: dict[str, Optional[str]] = {
    "SAP.DE":  "SAP",    # SAP SE — NYSE
    "SIE.DE":  "SIEGY",  # Siemens — OTC
    "ALV.DE":  "ALIZY",  # Allianz — OTC
    "MRK.DE":  "MKKGY",  # Merck KGaA — OTC
    "DTE.DE":  "DTEGY",  # Deutsche Telekom — OTC
    "VOW3.DE": "VWAGY",  # Volkswagen — OTC
    "BMW.DE":  "BMWYY",  # BMW — OTC
    "BAS.DE":  "BASFY",  # BASF — OTC
    "BAYN.DE": "BAYRY",  # Bayer — OTC
    "DBK.DE":  "DB",     # Deutsche Bank — NYSE
    "MBG.DE":  "MBGYY",  # Mercedes-Benz — OTC
    "ADS.DE":  "ADDYY",  # Adidas — OTC
    "DHL.DE":  "DPSGY",  # DHL Group (fmr Deutsche Post) — OTC
    "RWE.DE":  "RWEOY",  # RWE — OTC
    "EOAN.DE": "EONGY",  # E.ON — OTC
    "IFX.DE":  "IFNNY",  # Infineon — OTC
    "FRE.DE":  "FSNUY",  # Fresenius — OTC
    "HEN3.DE": "HENKY",  # Henkel — OTC
    "CON.DE":  "CTTAY",  # Continental — OTC
    "AIR.DE":  "EADSY",  # Airbus — OTC
    "PUM.DE":  "PUMSY",  # Puma — OTC
    "MUV2.DE": "MURGY",  # Munich Re — OTC
    "QIA.DE":  "QIAGF",  # Qiagen — OTC
    "ZAL.DE":  None,     # Zalando — no liquid ADR
    "DHER.DE": None,     # Delivery Hero — no liquid ADR
    "VNA.DE":  None,     # Vonovia — no liquid ADR
    "1COV.DE": None,     # Covestro — no liquid ADR
    "NDX1.DE": None,
    "AIXA.DE": None,
    # US mega-caps — trade directly on Alpaca by their US ticker
    "APC.DE":  "AAPL",
    "MSF.DE":  "MSFT",
    "AMZN.DE": "AMZN",
    "GOOGL.DE":"GOOGL",
    "META.DE": "META",
    "NVDA.DE": "NVDA",
    "TSLA.DE": "TSLA",
    "AMD.DE":  "AMD",
    "NFLX.DE": "NFLX",
    "PYPL.DE": "PYPL",
}


def find_top_picks(n: int = 3) -> list[dict]:
    """
    Score the XETRA/GETTEX universe and return top N momentum picks.
    Each result includes `us_adr` field for Alpaca execution.
    """
    log.info(f"Scanning {len(XETRA_UNIVERSE)} XETRA/GETTEX candidates...")

    results = []
    for ticker in XETRA_UNIVERSE:
        try:
            r = score_ticker(ticker)
            if not r.get("ok"):
                continue
            score = r.get("score", 0)
            if score < 60:
                continue
            r["us_adr"] = XETRA_TO_US_ADR.get(ticker)
            r["exchange"] = "XETRA/GETTEX"
            results.append(r)
        except Exception as e:
            log.debug(f"Scan error {ticker}: {e}")

    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    log.info(f"Scanner done — {len(results)} candidates above threshold, returning top {n}")
    return results[:n]


def get_universe_summary() -> dict:
    """Quick info about the scan universe."""
    with_adr = sum(1 for v in XETRA_TO_US_ADR.values() if v)
    return {
        "exchange":       "XETRA / GETTEX",
        "total_stocks":   len(XETRA_UNIVERSE),
        "with_us_adr":    with_adr,
        "without_us_adr": len(XETRA_UNIVERSE) - with_adr,
    }
