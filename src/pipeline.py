"""Extract NYC traffic, match historical weather, and load MySQL for Tableau."""

from __future__ import annotations

import argparse

import pandas as pd

from src.config import WEATHER_LOCATIONS, Settings
from src.extract import fetch_traffic, fetch_weather
from src.load_mysql import count_view_rows, load
from src.transform import (
    aggregate_traffic,
    assign_weather_locations,
    clean_traffic,
    join_weather,
)


def _print_summary(
    raw_rows: int,
    clean_rows: int,
    joined: pd.DataFrame,
) -> None:
    matched = joined["temperature_c"].notna()
    weather_known = joined["rain_mm"].notna()
    raining = weather_known & joined["rain_mm"].gt(0)
    dry = weather_known & joined["rain_mm"].eq(0)
    print("\nJOIN SUMMARY")
    print(f"Raw traffic rows: {raw_rows}")
    print(f"Clean traffic rows: {clean_rows}")
    print(f"Hourly joined rows: {len(joined)}")
    print(f"Hours with weather: {int(matched.sum())}")
    print(f"Hours missing weather: {int((~matched).sum())}")
    print(f"Rain hours: {int(raining.sum())}")

    by_borough = (
        joined.assign(raining=raining)
        .groupby("borough", dropna=False)
        .agg(links=("link_id", "nunique"), hours=("hour_start", "size"), rain_hours=("raining", "sum"))
    )
    print("\nBY BOROUGH")
    print(by_borough.to_string())

    if raining.any() and dry.any():
        rain_time = joined.loc[raining, "avg_travel_time_sec"].mean()
        dry_time = joined.loc[dry, "avg_travel_time_sec"].mean()
        rain_speed = joined.loc[raining, "avg_speed_mph"].mean()
        dry_speed = joined.loc[dry, "avg_speed_mph"].mean()
        print(f"Avg travel time, rain hours: {rain_time:.1f} sec")
        print(f"Avg travel time, dry hours: {dry_time:.1f} sec")
        print(f"Avg speed, rain hours: {rain_speed:.1f} mph")
        print(f"Avg speed, dry hours: {dry_speed:.1f} mph")

    preview = joined[
        [
            "hour_start",
            "link_name",
            "borough",
            "avg_speed_mph",
            "avg_travel_time_sec",
            "temperature_c",
            "rain_mm",
        ]
    ].head(3)
    print("\nSAMPLE JOINED ROWS")
    print(preview.to_string(index=False))


def run(settings: Settings, skip_load: bool = False) -> pd.DataFrame:
    settings.validate_dates()
    print(
        "Question: for the selected NYC DOT links, how do hourly rain and "
        "temperature line up with average speed and travel time?"
    )
    print(
        f"Window: {settings.start_date} to {settings.end_date} | "
        f"links: {', '.join(settings.link_ids)}"
    )

    raw_traffic = fetch_traffic(settings)
    clean = clean_traffic(raw_traffic, settings.traffic_timezone)
    traffic_hourly = assign_weather_locations(aggregate_traffic(clean))
    # Only request weather for the boroughs these links are actually in.
    needed = set(traffic_hourly["location_id"].dropna().astype(int))
    weather = fetch_weather(
        settings,
        [location for location in WEATHER_LOCATIONS if location.location_id in needed],
    )
    joined = join_weather(traffic_hourly, weather)
    _print_summary(len(raw_traffic), len(clean), joined)

    if skip_load:
        print("\nSkipped MySQL load.")
        return joined

    load(settings, traffic_hourly, weather)
    view_rows = count_view_rows(settings)
    print(
        f"\nMySQL load complete. {view_rows} rows in "
        f"{settings.mysql_database}.vw_hourly_weather_traffic"
    )
    print(
        "Tableau: connect to MySQL at "
        f"{settings.mysql_host}:{settings.mysql_port}, "
        f"database {settings.mysql_database}, and use that view."
    )
    return joined


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Join NYC DOT traffic with Open-Meteo weather and load MySQL."
    )
    parser.add_argument("--start", dest="start_date", help="Inclusive start date, YYYY-MM-DD")
    parser.add_argument("--end", dest="end_date", help="Inclusive end date, YYYY-MM-DD")
    parser.add_argument(
        "--links",
        help="Comma-separated NYC DOT link ids. Defaults to the cleaned sample link.",
    )
    parser.add_argument(
        "--skip-load",
        action="store_true",
        help="Build the join without writing to MySQL.",
    )
    args = parser.parse_args()

    settings = Settings.from_env()
    if args.start_date or args.end_date or args.links:
        settings = Settings(
            start_date=args.start_date or settings.start_date,
            end_date=args.end_date or settings.end_date,
            link_ids=tuple(
                part.strip()
                for part in (args.links or ",".join(settings.link_ids)).split(",")
                if part.strip()
            ),
            mysql_host=settings.mysql_host,
            mysql_port=settings.mysql_port,
            mysql_user=settings.mysql_user,
            mysql_password=settings.mysql_password,
            mysql_database=settings.mysql_database,
        )
    run(settings, skip_load=args.skip_load)


if __name__ == "__main__":
    main()
