"""
raanu.clock — the timezones this application reasons in
========================================================
US/Eastern is the one that matters: the market opens and closes on it, and
every scheduling decision is made in it. Europe/Berlin and Asia/Kolkata are
presentation only — the owner reads the dashboard in one and the reports in
the other.

Kept in one module so scheduling logic never re-derives a timezone locally
and drifts, and so DST is handled by ZoneInfo rather than a fixed offset.
"""

from zoneinfo import ZoneInfo

US_EAST = ZoneInfo("US/Eastern")
BERLIN = ZoneInfo("Europe/Berlin")
IST = ZoneInfo("Asia/Kolkata")
