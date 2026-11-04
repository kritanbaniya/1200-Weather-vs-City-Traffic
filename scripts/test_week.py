"""
Test: fetch 1 week of NYC DOT traffic (all segments, averaged by hour on
Socrata's server) + Open-Meteo hourly weather per borough, then join.

Run from the repo root:
    python3 scripts/test_week.py

Optional: set a free NYC Open Data app token to avoid throttling
    export NYC_APP_TOKEN=your_token
"""

import os
import time
from datetime import date, timedelta

import pandas as pd
import requests

START = date(2026, 10, 8)   # week includes the Oct 11 rainstorm
DAYS = 7

TRAFFIC_URL = "https://data.cityofnewyork.us/resource/i4gi-tjb9.json"
WEATHER_URL = "https://archive-api.open-meteo.com/v1/archive"

# Rough borough centers (one weather point per borough for this test)
BOROUGHS = {
    "Manhattan":     (40.7831, -73.9712),
    "Brooklyn":      (40.6782, -73.9442),
    "Queens":        (40.7282, -73.7949),
    "Bronx":         (40.8448, -73.8648),
    "Staten Island": (40.5795, -74.1502),
}

HEADERS = {"X-App-Token": os.environ["NYC_APP_TOKEN"]} if os.getenv("NYC_APP_TOKEN") else {}

# Socrata does the hourly aggregation. Columns are text, so cast with ::number.
SELECT = (
    "link_id, link_name, borough, "
    "date_trunc_ymd(data_as_of) AS day, "
    "date_extract_hh(data_as_of) AS hour, "
    "avg(speed::number) AS avg_speed, "
    "median(speed::number) AS median_speed, "
    "min(speed::number) AS min_speed, "
    "avg(travel_time::number) AS avg_travel_time, "
    "max(travel_time::number) AS max_travel_time, "
    "count(*) AS readings"
)
GROUP = "link_id, link_name, borough, day, hour"


def fetch_traffic(start: date, end: date) -> pd.DataFrame:
    """Hourly averages for all segments, start <= data_as_of < end."""
    params = {
        "$select": SELECT,
        "$where": (
            "status = '0' AND speed::number > 0 "
            f"AND data_as_of >= '{start}T00:00:00' AND data_as_of < '{end}T00:00:00'"
        ),
        "$group": GROUP,
        "$limit": 50000,
    }
    r = requests.get(TRAFFIC_URL, params=params, headers=HEADERS, timeout=300)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")
    return pd.DataFrame(r.json())


def fetch_weather(start: date, end: date) -> pd.DataFrame:
    params = {
        "latitude": ",".join(str(lat) for lat, _ in BOROUGHS.values()),
        "longitude": ",".join(str(lon) for _, lon in BOROUGHS.values()),
        "start_date": str(start),
        "end_date": str(end),
        "hourly": "temperature_2m,precipitation,rain,snowfall,weather_code,wind_speed_10m",
        "timezone": "America/New_York",
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
    }
    r = requests.get(WEATHER_URL, params=params, timeout=120)
    r.raise_for_status()
    payload = r.json()
    if isinstance(payload, dict):      # single location comes back as a dict
        payload = [payload]
    frames = []
    for borough, loc in zip(BOROUGHS, payload):
        df = pd.DataFrame(loc["hourly"])
        df["borough"] = borough
        df["grid_lat"], df["grid_lon"] = loc["latitude"], loc["longitude"]
        frames.append(df)
    w = pd.concat(frames, ignore_index=True)
    # Precip stamped 15:00 = total for 14:00-15:00 -> label it 14:00
    w["hour_start"] = pd.to_datetime(w["time"]) - pd.Timedelta(hours=1)
    return w.drop(columns="time")


def main() -> None:
    end = START + timedelta(days=DAYS)
    print(f"Window: {START} to {end - timedelta(days=1)} (all segments)\n")

    # --- Test A: one request per day ---------------------------------------
    print("TEST A: one request per day")
    frames, t_total = [], time.time()
    for i in range(DAYS):
        d = START + timedelta(days=i)
        t = time.time()
        df = fetch_traffic(d, d + timedelta(days=1))
        print(f"  {d}: {len(df):>5} hourly rows in {time.time() - t:5.1f}s")
        frames.append(df)
    traffic = pd.concat(frames, ignore_index=True)
    a_secs = time.time() - t_total
    print(f"  total: {len(traffic)} rows in {a_secs:.1f}s\n")

    # --- Test B: whole week in one request --------------------------------
    print("TEST B: whole week in one request")
    t = time.time()
    try:
        week = fetch_traffic(START, end)
        print(f"  {len(week)} rows in {time.time() - t:.1f}s\n")
    except Exception as e:
        print(f"  FAILED after {time.time() - t:.1f}s -> {e}\n")

    # --- Clean types -------------------------------------------------------
    for c in ["hour", "avg_speed", "median_speed", "min_speed",
              "avg_travel_time", "max_travel_time", "readings"]:
        traffic[c] = pd.to_numeric(traffic[c], errors="coerce")
    traffic["hour_start"] = pd.to_datetime(traffic["day"]) + pd.to_timedelta(traffic["hour"], unit="h")

    # --- Weather + join ----------------------------------------------------
    t = time.time()
    weather = fetch_weather(START, end)
    print(f"Weather: {len(weather)} rows ({len(BOROUGHS)} boroughs) in {time.time() - t:.1f}s")
    print(weather.groupby("borough")[["grid_lat", "grid_lon"]].first(), "\n")

    joined = traffic.merge(weather, on=["borough", "hour_start"], how="left")

    # --- Summary -----------------------------------------------------------
    print("SUMMARY")
    print(f"  Segments: {traffic['link_id'].nunique()}")
    print(f"  Segments per borough:\n{traffic.groupby('borough')['link_id'].nunique().to_string()}")
    print(f"  Hourly rows: {len(joined)}  | missing weather: {joined['precipitation'].isna().sum()}")
    print(f"  Readings per hour: mean {traffic['readings'].mean():.1f}, "
          f"hours with < 6 readings: {(traffic['readings'] < 6).sum()}")

    rain = joined["precipitation"] > 0
    print(f"\n  Rain hours: {rain.sum()} | dry hours: {(~rain).sum()}")
    print(f"  Avg speed  rain: {joined.loc[rain, 'avg_speed'].mean():.1f} mph | "
          f"dry: {joined.loc[~rain, 'avg_speed'].mean():.1f} mph")
    print(f"  Avg travel time  rain: {joined.loc[rain, 'avg_travel_time'].mean():.0f}s | "
          f"dry: {joined.loc[~rain, 'avg_travel_time'].mean():.0f}s")
    print("  (raw comparison, not controlled for time of day)\n")

    print("SAMPLE ROWS")
    print(joined[["hour_start", "borough", "link_name", "avg_speed", "readings",
                  "temperature_2m", "precipitation"]].head(8).to_string(index=False))

    # --- Projection --------------------------------------------------------
    days_full = (date(2026, 11, 3) - date(2025, 1, 1)).days
    est = a_secs / DAYS * days_full
    print(f"\nPROJECTION for 2025-01-01 -> today ({days_full} days, 1 request/day):")
    print(f"  ~{est / 60:.0f} min sequential, ~{len(traffic) / DAYS * days_full:,.0f} hourly rows")


if __name__ == "__main__":
    main()
