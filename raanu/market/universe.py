"""
raanu.market.universe — the curated tradable universe and ticker names
=======================================================================
**Strategy-driven, not brute-force.** A single curated, liquid quality list
rather than the full Alpaca universe (thousands of illiquid names the
strategies would never trade). A big scan is unnecessary when only uptrend
pullbacks, stage-2 breakouts and leader dips are ever bought.

Every ticker is US-listed and directly executable on Alpaca, and every one
of the 77 high-beta/momentum additions was verified ``tradable`` **and**
``fractionable``. Those additions run a median daily ATR of ~6.5% vs ~4.7%
for the original list, and 53 of the 77 exceed 5% — they are only viable
alongside ATR-scaled stops and risk-based sizing. In backtest the expansion
helped under a 2.5xATR stop (+6.38% vs -0.04%) and hurt under the old fixed
3% stop (-32.10% vs -22.19%). Do not revert to fixed-percentage stops while
these are in the universe.

``get_universe()`` (a live fetch of every Alpaca asset) used to live here.
It had not been called by anything since scanning moved to this curated
list, so it was removed rather than left as a second, untested definition
of "the universe".
"""

from __future__ import annotations

import logging

import httpx

from raanu import config

log = logging.getLogger("raanu.market.universe")

_asset_names: dict[str, str] = {}


TICKER_NAMES = {
    # Mega-cap tech
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA", "GOOGL": "Alphabet",
    "META": "Meta Platforms", "AMZN": "Amazon", "TSLA": "Tesla", "AMD": "AMD",
    "NFLX": "Netflix", "AVGO": "Broadcom",
    # Semiconductors
    "TXN": "Texas Instruments", "QCOM": "Qualcomm", "MU": "Micron", "AMAT": "Applied Materials",
    "LRCX": "Lam Research", "KLAC": "KLA Corp", "SNPS": "Synopsys", "CDNS": "Cadence Design",
    "INTC": "Intel", "ADI": "Analog Devices", "MRVL": "Marvell Technology", "MCHP": "Microchip Tech",
    "ON": "ON Semiconductor", "SWKS": "Skyworks", "MPWR": "Monolithic Power", "ENTG": "Entegris",
    "ACLS": "Axcelis Technologies", "WOLF": "Wolfspeed",
    # Software / Cloud / Cybersecurity
    "ORCL": "Oracle", "CRM": "Salesforce", "ADBE": "Adobe", "NOW": "ServiceNow",
    "WDAY": "Workday", "TEAM": "Atlassian", "OKTA": "Okta", "ZM": "Zoom Video",
    "DOCU": "DocuSign", "SNOW": "Snowflake", "DDOG": "Datadog", "CRWD": "CrowdStrike",
    "ZS": "Zscaler", "NET": "Cloudflare", "PANW": "Palo Alto Networks", "FTNT": "Fortinet",
    "PLTR": "Palantir", "HUBS": "HubSpot", "PAYC": "Paycom", "ADP": "ADP",
    "PAYX": "Paychex", "EPAM": "EPAM Systems", "GLOB": "Globant", "TENB": "Tenable",
    "QLYS": "Qualys", "VRNS": "Varonis", "S": "SentinelOne", "ESTC": "Elastic",
    # Internet / Fintech / Platforms
    "UBER": "Uber", "LYFT": "Lyft", "ABNB": "Airbnb", "BKNG": "Booking Holdings",
    "EXPE": "Expedia", "DASH": "DoorDash", "HOOD": "Robinhood", "COIN": "Coinbase",
    "XYZ": "Block (fka Square)", "PYPL": "PayPal", "AFRM": "Affirm", "SOFI": "SoFi Technologies",
    "UPST": "Upstart", "EBAY": "eBay", "ETSY": "Etsy", "W": "Wayfair",
    "CHWY": "Chewy", "PINS": "Pinterest", "SNAP": "Snap", "RDDT": "Reddit",
    "TTD": "The Trade Desk", "ROKU": "Roku", "U": "Unity Software", "RBLX": "Roblox",
    "EA": "Electronic Arts", "TTWO": "Take-Two Interactive",
    # Hardware / Enterprise IT
    "IBM": "IBM", "CSCO": "Cisco", "HPQ": "HP Inc", "HPE": "Hewlett Packard Enterprise",
    "DELL": "Dell Technologies", "ACN": "Accenture", "NTAP": "NetApp", "ZBRA": "Zebra Technologies",
    # Healthcare / Pharma / Biotech
    "JNJ": "Johnson & Johnson", "UNH": "UnitedHealth", "PFE": "Pfizer", "MRK": "Merck",
    "ABBV": "AbbVie", "LLY": "Eli Lilly", "BMY": "Bristol-Myers Squibb", "AMGN": "Amgen",
    "GILD": "Gilead Sciences", "BIIB": "Biogen", "REGN": "Regeneron", "VRTX": "Vertex Pharma",
    "MRNA": "Moderna", "ISRG": "Intuitive Surgical", "MDT": "Medtronic", "BSX": "Boston Scientific",
    "ABT": "Abbott Labs", "BDX": "Becton Dickinson", "SYK": "Stryker", "EW": "Edwards Lifesciences",
    "IDXX": "IDEXX Labs", "DXCM": "DexCom", "VEEV": "Veeva Systems", "CVS": "CVS Health",
    "CI": "Cigna", "HUM": "Humana", "ELV": "Elevance Health", "MOH": "Molina Healthcare",
    "CNC": "Centene", "HCA": "HCA Healthcare", "TDOC": "Teladoc Health", "HIMS": "Hims & Hers",
    "PODD": "Insulet", "GEHC": "GE HealthCare", "RMD": "ResMed", "IQV": "IQVIA",
    "A": "Agilent Technologies", "MTD": "Mettler-Toledo", "INCY": "Incyte",
    "ALNY": "Alnylam Pharma", "SRPT": "Sarepta Therapeutics", "ACAD": "Acadia Pharma",
    "AXSM": "Axsome Therapeutics", "RARE": "Ultragenyx Pharma", "NTRA": "Natera",
    "ILMN": "Illumina", "ZBH": "Zimmer Biomet",
    # Financials
    "JPM": "JPMorgan Chase", "GS": "Goldman Sachs", "MS": "Morgan Stanley",
    "BAC": "Bank of America", "WFC": "Wells Fargo", "C": "Citigroup", "USB": "US Bancorp",
    "PNC": "PNC Financial", "TFC": "Truist Financial", "SCHW": "Charles Schwab",
    "BK": "Bank of New York Mellon", "STT": "State Street", "AXP": "American Express",
    "COF": "Capital One", "SYF": "Synchrony Financial", "ALLY": "Ally Financial",
    "V": "Visa", "MA": "Mastercard", "BX": "Blackstone", "KKR": "KKR & Co",
    "APO": "Apollo Global", "BLK": "BlackRock", "TROW": "T. Rowe Price",
    "ICE": "Intercontinental Exchange", "CME": "CME Group", "CBOE": "Cboe Global Markets",
    "SPGI": "S&P Global", "MCO": "Moody's", "MSCI": "MSCI", "RJF": "Raymond James",
    "SF": "Stifel Financial", "LAZ": "Lazard", "EVR": "Evercore", "NAVI": "Navient",
    "HBAN": "Huntington Bancshares", "RF": "Regions Financial", "CFG": "Citizens Financial",
    "FITB": "Fifth Third Bancorp", "KEY": "KeyCorp", "MTB": "M&T Bank", "ZION": "Zions Bancorp",
    # Consumer Discretionary
    "HD": "Home Depot", "LOW": "Lowe's", "TGT": "Target", "COST": "Costco",
    "WMT": "Walmart", "NKE": "Nike", "SBUX": "Starbucks", "MCD": "McDonald's",
    "YUM": "Yum! Brands", "CMG": "Chipotle", "DRI": "Darden Restaurants",
    "TXRH": "Texas Roadhouse", "EAT": "Brinker International", "HLT": "Hilton",
    "MAR": "Marriott", "WYNN": "Wynn Resorts", "MGM": "MGM Resorts", "LVS": "Las Vegas Sands",
    "F": "Ford", "GM": "General Motors", "RIVN": "Rivian", "LCID": "Lucid Group",
    "ANF": "Abercrombie & Fitch", "AEO": "American Eagle", "LULU": "Lululemon",
    "PVH": "PVH Corp", "RL": "Ralph Lauren", "TPR": "Tapestry",
    "GAP": "Gap", "URBN": "Urban Outfitters", "FIVE": "Five Below",
    "DG": "Dollar General", "DLTR": "Dollar Tree", "OLLI": "Ollie's Bargain Outlet",
    "BBWI": "Bath & Body Works", "RH": "RH (Restoration Hardware)", "WSM": "Williams-Sonoma",
    "BBY": "Best Buy",
    # Consumer Staples
    "PG": "Procter & Gamble", "KO": "Coca-Cola", "PEP": "PepsiCo", "PM": "Philip Morris",
    "MO": "Altria", "MDLZ": "Mondelez", "GIS": "General Mills", "CAG": "Conagra Brands",
    "HRL": "Hormel Foods", "MKC": "McCormick", "CHD": "Church & Dwight", "CL": "Colgate-Palmolive",
    "CLX": "Clorox", "KMB": "Kimberly-Clark", "EL": "Estee Lauder", "ULTA": "Ulta Beauty",
    "SFM": "Sprouts Farmers Market", "KR": "Kroger", "ACI": "Albertsons", "COTY": "Coty",
    # Energy
    "XOM": "ExxonMobil", "CVX": "Chevron", "COP": "ConocoPhillips", "EOG": "EOG Resources",
    "DVN": "Devon Energy", "MPC": "Marathon Petroleum", "VLO": "Valero Energy",
    "PSX": "Phillips 66", "OXY": "Occidental Petroleum", "SLB": "Schlumberger",
    "HAL": "Halliburton", "BKR": "Baker Hughes", "FANG": "Diamondback Energy",
    "CTRA": "Coterra Energy", "APA": "APA Corp", "OKE": "ONEOK", "KMI": "Kinder Morgan",
    "WMB": "Williams Companies", "ET": "Energy Transfer", "EPD": "Enterprise Products",
    "MPLX": "MPLX LP", "TRGP": "Targa Resources", "AM": "Antero Midstream",
    # Industrials / Aerospace / Defense
    "BA": "Boeing", "RTX": "RTX Corp", "LMT": "Lockheed Martin", "NOC": "Northrop Grumman",
    "GD": "General Dynamics", "HII": "Huntington Ingalls", "TDG": "TransDigm",
    "HEI": "HEICO", "TXT": "Textron", "LHX": "L3Harris Technologies",
    "CAT": "Caterpillar", "DE": "Deere & Co", "EMR": "Emerson Electric",
    "HON": "Honeywell", "GE": "GE Aerospace", "MMM": "3M", "ITW": "Illinois Tool Works",
    "PH": "Parker-Hannifin", "ETN": "Eaton Corp", "ROP": "Roper Technologies",
    "CARR": "Carrier Global", "OTIS": "Otis Worldwide", "AME": "AMETEK",
    "LDOS": "Leidos", "BAH": "Booz Allen Hamilton", "SAIC": "SAIC",
    "CACI": "CACI International", "GNRC": "Generac", "XYL": "Xylem", "FTV": "Fortive",
    "UAL": "United Airlines", "DAL": "Delta Air Lines", "AAL": "American Airlines",
    "LUV": "Southwest Airlines", "ALK": "Alaska Air", "FDX": "FedEx", "UPS": "UPS",
    "XPO": "XPO Logistics", "ODFL": "Old Dominion Freight", "JBHT": "J.B. Hunt Transport",
    # Materials
    "FCX": "Freeport-McMoRan", "NEM": "Newmont", "GOLD": "Barrick Gold",
    "AEM": "Agnico Eagle Mines", "WPM": "Wheaton Precious Metals", "ALB": "Albemarle",
    "SQM": "Sociedad Quimica y Minera", "LAC": "Lithium Americas", "MP": "MP Materials",
    "LIN": "Linde", "APD": "Air Products", "DD": "DuPont", "DOW": "Dow Inc",
    "ECL": "Ecolab", "IFF": "International Flavors", "NTR": "Nutrien", "MOS": "Mosaic",
    "CF": "CF Industries", "FMC": "FMC Corp", "EMN": "Eastman Chemical",
    # Utilities
    "NEE": "NextEra Energy", "DUK": "Duke Energy", "SO": "Southern Company",
    "AEP": "American Electric Power", "EXC": "Exelon", "SRE": "Sempra",
    "PCG": "PG&E", "ED": "Consolidated Edison", "XEL": "Xcel Energy",
    "WEC": "WEC Energy", "ETR": "Entergy", "PPL": "PPL Corp",
    "CNP": "CenterPoint Energy", "AES": "AES Corp", "NI": "NiSource", "EVRG": "Evergy",
    # Communication Services
    "T": "AT&T", "VZ": "Verizon", "TMUS": "T-Mobile US", "CMCSA": "Comcast",
    "CHTR": "Charter Communications", "DIS": "Walt Disney", "WBD": "Warner Bros Discovery",
    "FOXA": "Fox Corp", "NYT": "New York Times",
    # Real Estate / REITs
    "AMT": "American Tower", "CCI": "Crown Castle", "EQIX": "Equinix",
    "PLD": "Prologis", "SPG": "Simon Property Group", "O": "Realty Income",
    "VICI": "VICI Properties", "WELL": "Welltower", "VTR": "Ventas",
    "EQR": "Equity Residential", "AVB": "AvalonBay", "MAA": "Mid-America Apartment",
    "NLY": "Annaly Capital", "AGNC": "AGNC Investment",
    # Mid-cap Tech
    "MELI": "MercadoLibre", "SE": "Sea Limited", "SHOP": "Shopify", "SPOT": "Spotify",
    "NTNX": "Nutanix", "FIVN": "Five9", "APPF": "AppFolio", "PCTY": "Paylocity",
    "MNDY": "monday.com", "BILL": "BILL Holdings",
    "BRZE": "Braze", "GTLB": "GitLab",
    # Mid-cap Healthcare
    "JAZZ": "Jazz Pharmaceuticals", "TECH": "Bio-Techne", "PRGO": "Perrigo",
    "SUPN": "Supernus Pharma", "HALO": "Halozyme", "IRTC": "iRhythm Technologies",
    "MMSI": "Merit Medical", "NVCR": "NovoCure",
    # ETFs
    "SPY": "SPDR S&P 500 ETF", "QQQ": "Invesco QQQ (Nasdaq-100)", "IWM": "iShares Russell 2000",
    "GLD": "SPDR Gold Shares", "SLV": "iShares Silver Trust",
    "XLE": "Energy Select SPDR", "XLF": "Financial Select SPDR", "XLV": "Health Care Select SPDR",
    "XLI": "Industrial Select SPDR", "XLK": "Technology Select SPDR", "XLC": "Communication Services SPDR",
    "XLY": "Consumer Discretionary SPDR", "XLP": "Consumer Staples SPDR", "XLRE": "Real Estate Select SPDR",
    "XLB": "Materials Select SPDR", "XLU": "Utilities Select SPDR", "ARKK": "ARK Innovation ETF",
}





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
    "UBER", "LYFT", "ABNB", "BKNG", "EXPE", "DASH", "HOOD", "COIN", "XYZ", "PYPL",
    "AFRM", "SOFI", "UPST", "EBAY", "ETSY", "W", "CHWY", "PINS", "SNAP", "RDDT",
    "TTD", "ROKU", "U", "RBLX", "EA", "TTWO",
    # Hardware / Enterprise IT
    "IBM", "CSCO", "HPQ", "HPE", "DELL", "ACN", "NTAP", "ZBRA",
    # Healthcare / Pharma / Biotech
    "JNJ", "UNH", "PFE", "MRK", "ABBV", "LLY", "BMY", "AMGN", "GILD", "BIIB",
    "REGN", "VRTX", "MRNA", "ISRG", "MDT", "BSX", "ABT", "BDX", "SYK", "EW",
    "IDXX", "DXCM", "VEEV", "CVS", "CI", "HUM", "ELV", "MOH", "CNC", "HCA",
    "TDOC", "HIMS", "PODD", "GEHC", "RMD", "IQV", "A", "MTD", "INCY",
    "ALNY", "SRPT", "ACAD", "AXSM", "RARE", "NTRA", "ILMN", "ZBH",
    # Financials
    "JPM", "GS", "MS", "BAC", "WFC", "C", "USB", "PNC", "TFC", "SCHW",
    "BK", "STT", "AXP", "COF", "SYF", "ALLY", "V", "MA",
    "BX", "KKR", "APO", "BLK", "TROW", "ICE", "CME", "CBOE", "SPGI", "MCO", "MSCI",
    "RJF", "SF", "LAZ", "EVR", "NAVI",
    "HBAN", "RF", "CFG", "FITB", "KEY", "MTB", "ZION",
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
    "MNDY", "BILL", "BRZE", "GTLB",
    # Mid-cap Healthcare
    "JAZZ", "TECH", "PRGO", "SUPN", "HALO", "IRTC", "MMSI", "NVCR",
    # ETFs
    "SPY", "QQQ", "IWM", "GLD", "SLV",
    "XLE", "XLF", "XLV", "XLI", "XLK", "XLC", "XLY", "XLP", "XLRE", "XLB", "XLU",
    "ARKK",

    # ── High-beta / momentum expansion ───────────────────────────────────────
    # Every symbol below was verified tradable AND fractionable on Alpaca.
    # These run a median daily ATR of ~6.5% vs ~4.7% for the names above, and
    # 53 of the 77 exceed 5%. They are only safe to trade with ATR-scaled stops
    # and risk-based position sizing — a fixed % stop sits far inside their
    # normal daily range, and equal-dollar sizing would risk many multiples of
    # a quiet name's position. See backtest.py PortfolioConfig.sizing_mode.
    # Top-100 gaps
    "INTU", "TMO", "DHR", "PGR", "TJX", "UNP", "CB", "ANET", "APH",
    "MCK", "ADSK", "ORLY", "CTAS", "MSI", "NSC", "CSX", "AON", "SHW",
    # Crypto miners / digital assets (highest ATR in the universe)
    "HIVE", "MARA", "RIOT", "CLSK", "CIFR", "WULF", "IREN", "CORZ", "BTDR",
    "MSTR", "GLXY",
    # AI / quantum / data
    "BBAI", "IONQ", "RGTI", "QBTS", "SOUN", "AI", "TEM", "NBIS", "CRWV", "APP",
    # Space / eVTOL
    "RKLB", "ASTS", "LUNR", "ACHR", "JOBY", "RDW",
    # Fintech / consumer momentum
    "CVNA", "TOST", "DAVE", "LMND",
    # Nuclear / power
    "OKLO", "SMR", "VST", "TLN", "NRG", "CEG", "GEV",
    # Semis / hardware
    "SMCI", "ARM", "ALAB", "CRDO", "NVTS", "AEHR", "INDI", "AMKR", "POWL",
    # Software / internet
    "PATH", "MDB", "ASAN", "DKNG",
    # EV / clean energy
    "NIO", "XPEV", "LI", "QS", "CHPT", "ENPH", "FSLR", "RUN",
]


def _load_asset_names() -> dict[str, str]:
    """Bulk-fetch symbol -> company name from Alpaca, cached for the process.

    Name lookup used to be a side effect of the removed ``get_universe()``,
    so this dict stayed empty and every ticker outside the hand-written
    ``TICKER_NAMES`` above rendered as its bare symbol.
    """
    global _asset_names
    if _asset_names:
        return _asset_names

    if not (config.alpaca_key() and config.alpaca_secret()):
        return {}

    base = config.broker_base().removesuffix("/v2")
    try:
        resp = httpx.get(
            f"{base}/v2/assets",
            headers={"APCA-API-KEY-ID": config.alpaca_key(),
                     "APCA-API-SECRET-KEY": config.alpaca_secret()},
            params={"asset_class": "us_equity", "status": "active"},
            timeout=30,
        )
        resp.raise_for_status()
        _asset_names = {a["symbol"]: a.get("name") or a["symbol"] for a in resp.json()}
        log.info(f"Loaded {len(_asset_names)} asset names from Alpaca")
    except Exception as e:
        log.warning(f"Could not load asset names: {e}")
        return {}
    return _asset_names


def get_ticker_name(ticker: str) -> str:
    """Company name for a ticker, or the ticker itself if unknown."""
    return TICKER_NAMES.get(ticker) or _load_asset_names().get(ticker) or ticker


def reset_name_cache() -> None:
    global _asset_names
    _asset_names = {}


def get_universe_summary() -> dict:
    return {"exchange": "US", "total_stocks": len(scannable_universe())}


# Tickers Yahoo Finance returns no data for. They score nothing either way,
# but they are not free: yfinance retries each one individually inside the
# batch call, and CloudWatch showed those retries burning ~20s per download
# on every single run.
#
# That cost used to be amortised across one long sequential scan. Under the
# sharded scan it is worse — wall-clock is the SLOWEST shard, so one dud
# ticker holds up the whole result.
#
# Kept here rather than deleted from FALLBACK_UNIVERSE on purpose: these are
# real, liquid companies (BNY Mellon, Coterra) that belong in the universe
# and may start resolving again. Re-check with
# ``python -c "import yfinance; print(yfinance.download('BK CTRA', period='5d'))"``
# and delete from this set if data comes back.
KNOWN_NO_DATA = frozenset({"BK", "CTRA"})


def scannable_universe() -> list[str]:
    """The universe a scan should actually download — the curated list minus
    tickers currently returning no data."""
    return [t for t in FALLBACK_UNIVERSE if t not in KNOWN_NO_DATA]
