"""Clean traffic readings and align them to hourly weather."""

from __future__ import annotations

import pandas as pd

from src.config import location_for_borough

# NYC DOT uses status 0 for a normal reading. -101 showed up as a sensor flag
# with zero speed and zero travel time in the sample week.


def clean_traffic(
    traffic: pd.DataFrame, timezone: str = "America/New_York"
) -> pd.DataFrame:
    """Clean NYC DOT readings whose unoffset timestamps are New York local time."""
    cleaned = traffic.copy()
    cleaned["data_as_of"] = pd.to_datetime(cleaned["data_as_of"], errors="coerce")
    # Validate the source timestamps against the documented local timeline while
    # retaining naive local values for MySQL DATETIME and the Open-Meteo join.
    localized = cleaned["data_as_of"].dt.tz_localize(
        timezone, ambiguous="NaT", nonexistent="shift_forward"
    )
    cleaned["data_as_of"] = localized.dt.tz_localize(None)
    cleaned["speed"] = pd.to_numeric(cleaned["speed"], errors="coerce")
    cleaned["travel_time"] = pd.to_numeric(cleaned["travel_time"], errors="coerce")
    status = pd.to_numeric(cleaned["status"], errors="coerce")

    cleaned = cleaned.loc[status.eq(0)].copy()
    cleaned = cleaned.dropna(subset=["data_as_of", "speed", "link_id"])
    cleaned = cleaned.loc[cleaned["speed"] >= 0]
    cleaned = cleaned.drop_duplicates(subset=["link_id", "data_as_of"])
    cleaned = cleaned.sort_values(["link_id", "data_as_of"])
    return cleaned.reset_index(drop=True)


def aggregate_traffic(traffic: pd.DataFrame) -> pd.DataFrame:
    """Roll ~5 minute readings up to the hour so they match hourly weather."""
    hourly_source = traffic.copy()
    hourly_source["hour_start"] = hourly_source["data_as_of"].dt.floor("h")
    hourly = (
        hourly_source.groupby(["link_id", "hour_start"], as_index=False)
        .agg(
            link_name=("link_name", "first"),
            borough=("borough", "first"),
            avg_speed_mph=("speed", "mean"),
            avg_travel_time_sec=("travel_time", "mean"),
            reading_count=("speed", "size"),
        )
        .sort_values(["link_id", "hour_start"])
        .reset_index(drop=True)
    )
    hourly["avg_speed_mph"] = hourly["avg_speed_mph"].round(2)
    hourly["avg_travel_time_sec"] = hourly["avg_travel_time_sec"].round(2)
    return hourly


def assign_weather_locations(traffic_hourly: pd.DataFrame) -> pd.DataFrame:
    """Tag each road link with its borough's weather point.

    A link whose borough is missing or unrecognised gets a null location_id,
    so its weather stays unknown instead of borrowing another borough's.
    """
    tagged = traffic_hourly.copy()
    tagged["location_id"] = pd.array(
        [
            location.location_id if location else None
            for location in map(location_for_borough, tagged["borough"])
        ],
        dtype="Int64",
    )
    unmatched = tagged.loc[tagged["location_id"].isna(), "link_id"].unique()
    if len(unmatched):
        print(
            "No weather point for links with unknown borough: "
            + ", ".join(map(str, unmatched))
        )
    return tagged


def join_weather(traffic_hourly: pd.DataFrame, weather: pd.DataFrame) -> pd.DataFrame:
    """Left-join the weather from each link's own borough point, hour by hour."""
    weather_columns = weather[
        ["location_id", "hour_start", "temperature_c", "rain_mm", "precipitation_mm"]
    ].astype({"location_id": "Int64"})
    joined = traffic_hourly.merge(
        weather_columns, on=["location_id", "hour_start"], how="left"
    )
    # Nullable integer preserves missing rainfall as unknown rather than dry.
    joined["is_raining"] = (
        joined["rain_mm"]
        .gt(0)
        .where(joined["rain_mm"].notna())
        .astype("Int64")
    )
    joined["temperature_f"] = (joined["temperature_c"] * 9 / 5 + 32).round(2)
    return joined
