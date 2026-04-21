#!/usr/bin/env python3
"""Fetch today's HRRR, NAM, and GFS forecasts for a point and push a summary
of their agreement (or disagreement) on temperature and precipitation to ntfy.

Config via env vars:
  NTFY_TOPIC           required  ntfy topic to publish to
  LAT, LON             required  point of interest (CONUS for HRRR/NAM)
  LOCATION_NAME        optional  human label shown in the notification
  TEMP_TOLERANCE_F     optional  max allowed spread of daily high/low (°F)
  PRECIP_TOLERANCE_IN  optional  max allowed spread of daily precip total (in)
  NTFY_SERVER          optional  override for self-hosted ntfy (default ntfy.sh)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
MODELS = ("ncep_hrrr_conus", "ncep_nam_conus", "gfs_seamless")
MODEL_LABEL = {
    "ncep_hrrr_conus": "HRRR",
    "ncep_nam_conus": "NAM ",
    "gfs_seamless": "GFS ",
}

# A day is considered "rainy" if total precip meets this threshold.
RAIN_THRESHOLD_IN = 0.05
DEFAULT_TEMP_TOL_F = 3.0
DEFAULT_PRECIP_TOL_IN = 0.1


@dataclass
class DayForecast:
    high_f: float
    low_f: float
    precip_in: float

    @property
    def rain(self) -> bool:
        return self.precip_in >= RAIN_THRESHOLD_IN


def http_get_json(url: str, params: dict) -> dict:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(full, timeout=30) as r:
        return json.loads(r.read())


def fetch(lat: float, lon: float, model: str) -> DayForecast:
    data = http_get_json(
        OPEN_METEO_URL,
        {
            "latitude": lat,
            "longitude": lon,
            "models": model,
            "hourly": "temperature_2m,precipitation",
            "temperature_unit": "fahrenheit",
            "precipitation_unit": "inch",
            "timezone": "auto",
            "forecast_days": 1,
            "past_days": 0,
        },
    )
    hourly = data["hourly"]
    temps = [t for t in hourly["temperature_2m"] if t is not None]
    precs = [p for p in hourly["precipitation"] if p is not None]
    if not temps or not precs:
        raise RuntimeError(f"{model}: no hourly data returned")
    return DayForecast(
        high_f=max(temps),
        low_f=min(temps),
        precip_in=sum(precs),
    )


def build_summary(
    forecasts: dict[str, DayForecast],
    failures: list[str],
    location: str,
    temp_tol: float,
    precip_tol: float,
) -> tuple[str, str, str]:
    highs = [f.high_f for f in forecasts.values()]
    lows = [f.low_f for f in forecasts.values()]
    precips = [f.precip_in for f in forecasts.values()]
    rains = [f.rain for f in forecasts.values()]

    high_spread = max(highs) - min(highs)
    low_spread = max(lows) - min(lows)
    precip_spread = max(precips) - min(precips)

    temp_aligned = max(high_spread, low_spread) <= temp_tol
    categorical_aligned = len(set(rains)) == 1
    magnitude_aligned = precip_spread <= precip_tol
    precip_aligned = categorical_aligned and magnitude_aligned

    overall_aligned = temp_aligned and precip_aligned and not failures
    verdict = "aligned" if overall_aligned else "disagree"
    title = f"{location}: models {verdict}"

    lines = []
    temp_icon = "OK  " if temp_aligned else "WARN"
    lines.append(
        f"[{temp_icon}] High {min(highs):.0f}-{max(highs):.0f}F "
        f"(spread {high_spread:.1f}F, tol {temp_tol:.0f}F)"
    )
    lines.append(
        f"       Low  {min(lows):.0f}-{max(lows):.0f}F "
        f"(spread {low_spread:.1f}F)"
    )

    if categorical_aligned:
        rain_verdict = "all expect rain" if rains[0] else "none expect rain"
        precip_icon = "OK  " if magnitude_aligned else "WARN"
    else:
        yes = sum(rains)
        rain_verdict = f"{yes}/{len(rains)} expect rain"
        precip_icon = "WARN"
    lines.append(
        f"[{precip_icon}] Precip: {rain_verdict} "
        f"(spread {precip_spread:.2f}in, tol {precip_tol:.2f}in)"
    )

    lines.append("")
    for model, f in forecasts.items():
        lines.append(
            f"  {MODEL_LABEL[model]}  hi {f.high_f:4.0f}F  "
            f"lo {f.low_f:4.0f}F  precip {f.precip_in:.2f}in"
        )
    if failures:
        lines.append("")
        lines.append(f"  (failed to fetch: {', '.join(failures)})")

    body = "\n".join(lines)
    priority = "default" if overall_aligned else "high"
    return title, body, priority


def send_ntfy(server: str, topic: str, title: str, body: str, priority: str) -> None:
    req = urllib.request.Request(
        f"{server.rstrip('/')}/{topic}",
        data=body.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": priority,
            "Tags": "partly_sunny_rain",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def main() -> int:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("ERROR: NTFY_TOPIC env var is required", file=sys.stderr)
        return 2

    lat_s = os.environ.get("LAT", "").strip()
    lon_s = os.environ.get("LON", "").strip()
    if not lat_s or not lon_s:
        print("ERROR: LAT and LON env vars are required", file=sys.stderr)
        return 2
    lat = float(lat_s)
    lon = float(lon_s)

    location = os.environ.get("LOCATION_NAME", "").strip() or f"{lat:.2f},{lon:.2f}"
    temp_tol = float(os.environ.get("TEMP_TOLERANCE_F") or DEFAULT_TEMP_TOL_F)
    precip_tol = float(os.environ.get("PRECIP_TOLERANCE_IN") or DEFAULT_PRECIP_TOL_IN)
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip()

    forecasts: dict[str, DayForecast] = {}
    failures: list[str] = []
    for model in MODELS:
        try:
            forecasts[model] = fetch(lat, lon, model)
        except Exception as e:
            print(f"WARN: {model} failed: {e}", file=sys.stderr)
            failures.append(MODEL_LABEL[model].strip())

    if len(forecasts) < 2:
        title = f"{location}: weather check failed"
        body = f"Could not fetch enough models today ({len(forecasts)}/3)."
        if failures:
            body += f"\nFailures: {', '.join(failures)}"
        send_ntfy(server, topic, title, body, "high")
        print(title)
        print(body)
        return 1

    title, body, priority = build_summary(
        forecasts, failures, location, temp_tol, precip_tol
    )
    print(title)
    print(body)
    send_ntfy(server, topic, title, body, priority)
    return 0


if __name__ == "__main__":
    sys.exit(main())
