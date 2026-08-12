"""True statistical-release macro data (GDP growth, inflation, unemployment)
for the Macro tab — as opposed to the market-price proxies in regions.py.

Pulled live from fincept-qt/scripts/worldbank_data.py, which wraps the World
Bank Open Data API (https://api.worldbank.org/v2) — official, government-
reported figures aggregated by the World Bank, not derived from security
prices. No API key required, unlike fincept-qt/scripts/fred_data.py (needs
FRED_API_KEY) — chosen so this runs with zero setup.

These are genuinely periodic releases (most countries report GDP growth
and inflation annually, some sub-annually) — "real time" here means every
call hits the live World Bank API fresh (nothing hardcoded or mocked), not
that the underlying figures themselves update every second. The server
caches the result for a while (see server.py's --stats-interval) purely to
avoid re-querying an API that only publishes new figures a few times a
year.
"""

import os
import sys
import time
from datetime import datetime

_SCRIPTS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "fincept-qt", "scripts")
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import worldbank_data  # fincept-qt/scripts/worldbank_data.py

# Representative countries per region — the same regions the Macro tab's
# market-signal groups use (regions.py), so the two sections line up.
STAT_COUNTRIES = {
    "US": [("USA", "United States")],
    "EU": [("DEU", "Germany"), ("FRA", "France"), ("ITA", "Italy"), ("ESP", "Spain")],
    "Baltics": [("EST", "Estonia"), ("LVA", "Latvia"), ("LTU", "Lithuania")],
    "Asia": [("CHN", "China"), ("JPN", "Japan"), ("IND", "India")],
    "Global": [("WLD", "World")],
}

# (World Bank indicator code, display label, unit)
STAT_INDICATORS = [
    (worldbank_data.GDP_GROWTH, "GDP Growth", "%"),
    (worldbank_data.INFLATION, "Inflation (CPI)", "%"),
    (worldbank_data.UNEMPLOYMENT, "Unemployment", "%"),
]


def _all_country_codes():
    seen = set()
    out = []
    for entries in STAT_COUNTRIES.values():
        for code, _name in entries:
            if code not in seen:
                seen.add(code)
                out.append(code)
    return out


def _latest_by_country(items):
    """Group World Bank observations by country, keep the most recent
    non-null value per country (the API returns nulls for years not yet
    reported, so a naive "first row" pick would show blanks)."""
    by_country = {}
    for item in items or []:
        cc = item.get("country_id")
        if not cc:
            continue
        by_country.setdefault(cc, []).append(item)
    latest = {}
    for cc, obs in by_country.items():
        obs_sorted = sorted(obs, key=lambda o: o.get("date") or "", reverse=True)
        for o in obs_sorted:
            if o.get("value") is not None:
                latest[cc] = o
                break
    return latest


def get_statistics(years_back=6):
    """One live World Bank API call per indicator (batched across every
    country used anywhere in STAT_COUNTRIES), then reshaped into the same
    region groups the market-signal Macro tab uses.
    """
    country_codes = _all_country_codes()
    current_year = datetime.now().year
    date_range = f"{current_year - years_back}:{current_year}"

    per_indicator_latest = {}  # indicator_code -> {country_code: {value,date}}
    errors = []
    for code, label, unit in STAT_INDICATORS:
        result = worldbank_data.get_indicators(";".join(country_codes), code, date_range, per_page=2000)
        if result.get("error"):
            errors.append(f"{label}: {result['error']}")
            per_indicator_latest[code] = {}
            continue
        per_indicator_latest[code] = _latest_by_country(result.get("data"))

    groups = {}
    for region, countries in STAT_COUNTRIES.items():
        rows = []
        for code, name in countries:
            indicators = []
            for ind_code, label, unit in STAT_INDICATORS:
                obs = per_indicator_latest.get(ind_code, {}).get(code)
                if obs is None:
                    indicators.append({"code": ind_code, "label": label, "unit": unit, "value": None, "date": None})
                else:
                    indicators.append({
                        "code": ind_code, "label": label, "unit": unit,
                        "value": obs.get("value"), "date": obs.get("date"),
                    })
            rows.append({"country_code": code, "country_name": name, "indicators": indicators})
        groups[region] = rows

    return {
        "groups": groups,
        "indicator_labels": [{"code": c, "label": l, "unit": u} for c, l, u in STAT_INDICATORS],
        "source": "World Bank Open Data (api.worldbank.org)",
        "as_of": int(time.time()),
        "error": "; ".join(errors) if errors else None,
    }
