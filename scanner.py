"""
scanner.py — Dynamic US market scanner
=======================================
Fetches all active, tradable US equities from Alpaca's assets API at first
scan. Results are cached for the server lifetime. Falls back to a curated
500-ticker list if the API call fails.

Tickers are split into chunks of CHUNK_SIZE for yfinance batch downloads so
the SSE stream can push results progressively instead of waiting for all data.
"""

import logging
import os
from typing import Optional

import httpx
from strategy import score_from_df, batch_download

log = logging.getLogger("raanu.scanner")

CHUNK_SIZE = 250  # tickers per yfinance batch call (~5-8s each)

# ── Lazy universe cache ───────────────────────────────────────────────────────
_universe_cache: Optional[list[str]] = None
_universe_names: dict[str, str] = {}   # ticker → company name


def get_universe() -> list[str]:
    """Return all active tradable US equities from Alpaca. Cached after first call.
    Raises RuntimeError if the fetch fails — caller must handle and surface the error."""
    global _universe_cache, _universe_names
    if _universe_cache is not None:
        return _universe_cache

    api_key    = os.getenv("ALPACA_API_KEY", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY", "")
    mode       = os.getenv("ALPACA_MODE", "paper").lower()
    base_url   = "https://paper-api.alpaca.markets" if mode == "paper" else "https://api.alpaca.markets"

    if not api_key or not secret_key:
        raise RuntimeError("Alpaca API keys not configured — cannot fetch universe")

    resp = httpx.get(
        f"{base_url}/v2/assets",
        headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key},
        params={"asset_class": "us_equity", "status": "active"},
        timeout=30,
    )
    resp.raise_for_status()
    assets = resp.json()

    filtered = [
        a for a in assets
        if a.get("tradable")
        and a.get("fractionable")               # liquid enough for reliable yfinance data
        and a.get("exchange") not in ("OTC",)   # skip pure OTC stocks
        and "." not in a["symbol"]              # skip BRK.B style
        and "/" not in a["symbol"]              # skip crypto-style
        and len(a["symbol"]) <= 5               # skip warrants/rights (AAPLW etc.)
    ]

    seen: set[str] = set()
    tickers: list[str] = []
    for a in filtered:
        sym = a["symbol"]
        if sym not in seen:
            seen.add(sym)
            tickers.append(sym)
            _universe_names[sym] = a.get("name", sym)

    tickers.sort()
    log.info(f"Alpaca universe: {len(tickers)} tradable US equities fetched")
    _universe_cache = tickers
    return _universe_cache


def get_ticker_name(ticker: str) -> str:
    """Return the company name for a ticker, or the ticker itself if unknown."""
    return _universe_names.get(ticker, ticker)


def reset_universe_cache():
    """Force a fresh fetch on the next scan (e.g. called at midnight)."""
    global _universe_cache, _universe_names
    _universe_cache = None
    _universe_names = {}


# ── Test universe for force/dry-run scans ────────────────────────────────────
TEST_UNIVERSE = ["AAPL", "NVDA", "MSFT", "GOOGL", "META"]


# ── Curated list kept only for reference / manual testing ────────────────────
FALLBACK_UNIVERSE = [
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA", "AMD", "NFLX", "AVGO",
    # Semiconductors
    "TXN", "QCOM", "MU", "AMAT", "LRCX", "KLAC", "SNPS", "CDNS", "INTC",
    "ADI", "MRVL", "MCHP", "ON", "SWKS", "MPWR", "ENTG", "ACLS", "WOLF",
    # Software / Cloud / Cybersecurity
    "ORCL", "CRM", "ADBE", "NOW", "WDAY", "TEAM", "OKTA", "ZM", "DOCU", "SNOW",
    "DDOG", "CRWD", "ZS", "NET", "PANW", "FTNT", "PLTR", "HUBS", "PAYC", "ADP",
    "PAYX", "EPAM", "GLOB", "TENB", "QLYS", "VRNS", "S", "ESTC",
    # Internet / Fintech / Platforms
    "UBER", "LYFT", "ABNB", "BKNG", "EXPE", "DASH", "HOOD", "COIN", "SQ", "PYPL",
    "AFRM", "SOFI", "UPST", "EBAY", "ETSY", "W", "CHWY", "PINS", "SNAP", "RDDT",
    "TTD", "ROKU", "U", "RBLX", "EA", "TTWO",
    # Hardware / Enterprise IT
    "IBM", "CSCO", "HPQ", "HPE", "DELL", "ACN", "NTAP", "PSTG", "ZBRA",
    # Healthcare / Pharma / Biotech
    "JNJ", "UNH", "PFE", "MRK", "ABBV", "LLY", "BMY", "AMGN", "GILD", "BIIB",
    "REGN", "VRTX", "MRNA", "ISRG", "MDT", "BSX", "ABT", "BDX", "SYK", "EW",
    "IDXX", "DXCM", "VEEV", "CVS", "CI", "HUM", "ELV", "MOH", "CNC", "HCA",
    "TDOC", "HIMS", "PODD", "HOLX", "GEHC", "RMD", "IQV", "A", "MTD", "INCY",
    "ALNY", "SRPT", "ACAD", "AXSM", "RARE", "NTRA", "EXAS", "ILMN", "ZBH",
    # Financials
    "JPM", "GS", "MS", "BAC", "WFC", "C", "USB", "PNC", "TFC", "SCHW",
    "BK", "STT", "AXP", "COF", "SYF", "ALLY", "V", "MA",
    "BX", "KKR", "APO", "BLK", "TROW", "ICE", "CME", "CBOE", "SPGI", "MCO", "MSCI",
    "RJF", "SF", "LAZ", "EVR", "NAVI",
    "HBAN", "RF", "CFG", "FITB", "KEY", "MTB", "ZION", "CMA",
    # Consumer Discretionary
    "HD", "LOW", "TGT", "COST", "WMT", "NKE", "SBUX", "MCD", "YUM", "CMG",
    "DRI", "TXRH", "EAT", "HLT", "MAR", "WYNN", "MGM", "LVS",
    "F", "GM", "RIVN", "LCID", "ANF", "AEO", "LULU", "PVH", "RL", "TPR",
    "GAP", "URBN", "FIVE", "DG", "DLTR", "OLLI", "BBWI", "RH", "WSM", "BBY",
    # Consumer Staples
    "PG", "KO", "PEP", "PM", "MO", "MDLZ", "GIS", "CAG", "HRL",
    "MKC", "CHD", "CL", "CLX", "KMB", "EL", "ULTA", "SFM", "KR", "ACI", "COTY",
    # Energy
    "XOM", "CVX", "COP", "EOG", "DVN", "MPC", "VLO", "PSX", "OXY",
    "SLB", "HAL", "BKR", "FANG", "CTRA", "APA", "OKE", "KMI", "WMB",
    "ET", "EPD", "MPLX", "TRGP", "AM",
    # Industrials / Aerospace / Defense
    "BA", "RTX", "LMT", "NOC", "GD", "HII", "TDG", "HEI", "TXT", "LHX",
    "CAT", "DE", "EMR", "HON", "GE", "MMM", "ITW", "PH", "ETN", "ROP",
    "CARR", "OTIS", "AME", "LDOS", "BAH", "SAIC", "CACI", "GNRC", "XYL", "FTV",
    "UAL", "DAL", "AAL", "LUV", "ALK", "FDX", "UPS", "XPO", "ODFL", "JBHT",
    # Materials
    "FCX", "NEM", "GOLD", "AEM", "WPM", "ALB", "SQM", "LAC", "MP",
    "LIN", "APD", "DD", "DOW", "ECL", "IFF", "NTR", "MOS", "CF", "FMC", "EMN",
    # Utilities
    "NEE", "DUK", "SO", "AEP", "EXC", "SRE", "PCG", "ED", "XEL", "WEC",
    "ETR", "PPL", "CNP", "AES", "NI", "EVRG",
    # Communication Services
    "T", "VZ", "TMUS", "CMCSA", "CHTR", "DIS", "WBD", "FOXA", "NYT",
    # Real Estate / REITs
    "AMT", "CCI", "EQIX", "PLD", "SPG", "O", "VICI", "WELL", "VTR", "EQR",
    "AVB", "MAA", "NLY", "AGNC",
    # Mid-cap Tech
    "MELI", "SE", "SHOP", "SPOT", "NTNX", "FIVN", "APPF", "PCTY",
    "JAMF", "MNDY", "BILL", "ZI", "BRZE", "GTLB",
    # Mid-cap Healthcare
    "JAZZ", "INCY", "TECH", "PRGO", "SUPN", "HALO", "IRTC", "MMSI", "NVCR",
    # ETFs
    "SPY", "QQQ", "IWM", "GLD", "SLV",
    "XLE", "XLF", "XLV", "XLI", "XLK", "XLC", "XLY", "XLP", "XLRE", "XLB", "XLU",
    "ARKK",
]


def find_top_picks(n: int = 3, max_stocks: Optional[int] = None) -> list[dict]:
    """
    Auto-trader scan — uses the curated FALLBACK_UNIVERSE (~500 stocks) for speed
    and reliability. Does NOT call the Alpaca assets API so it never blocks startup.
    The full dynamic universe is only used by the browser SSE scan stream.
    """
    universe = TEST_UNIVERSE[:max_stocks] if max_stocks else FALLBACK_UNIVERSE
    log.info(f"Auto-trader scanning {len(universe)} curated stocks...")

    results = []
    for i in range(0, len(universe), CHUNK_SIZE):
        chunk = universe[i:i + CHUNK_SIZE]
        data  = batch_download(chunk)
        log.info(f"Chunk {i//CHUNK_SIZE + 1}: {len(data)}/{len(chunk)} tickers returned data")
        for ticker in chunk:
            try:
                r = score_from_df(ticker, data.get(ticker))
                if r.get("ok") and r.get("score", 0) >= 60:
                    results.append(r)
            except Exception as e:
                log.debug(f"Score error {ticker}: {e}")

    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    log.info(f"Auto-trader scan done — {len(results)} picks above threshold, returning top {n}")
    return results[:n]


def get_universe_summary() -> dict:
    u = get_universe()
    return {"exchange": "US", "total_stocks": len(u)}
