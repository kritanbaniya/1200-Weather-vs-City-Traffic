"""Load every NYC DOT link plus per-borough weather into the full-year tables.

Default window: 2025-10-01 through 5 days before today (the Open-Meteo archive
lags a few days). Earlier years can be backfilled later with --start/--end.

    python -m src.bulk_load
    python -m src.bulk_load --start 2024-10-01 --end 2025-09-30   # backfill
    python -m src.bulk_load --dry-run                              # no MySQL

Writes only the tables in sql/full_year_schema.sql; the original pipeline's
tables are left untouched. Each weekly traffic chunk and each weather chunk is
committed on its own and recorded in load_chunks, so an interrupted run picks
up where it stopped. Use --force to re-fetch chunks that are already loaded.
"""

from __future__ import annotations

import argparse
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd
import pymysql
import requests

from src.config import PROJECT_ROOT, Settings

SCHEMA_PATH = PROJECT_ROOT / "sql" / "full_year_schema.sql"

TRAFFIC_URL = "https://data.cityofnewyork.us/resource/i4gi-tjb9.json"
WEATHER_URL = "https://archive-api.open-meteo.com/v1/archive"
LOCAL_TZ = "America/New_York"

DEFAULT_START = date(2025, 10, 1)
ARCHIVE_LAG_DAYS = 5
TRAFFIC_CHUNK_DAYS = 7
WEATHER_CHUNK_DAYS = 92
PAGE_SIZE = 50_000
INSERT_BATCH = 5_000

# Rough NYC bounding box, used to discard truncated link_points vertices.
NYC_LAT = (40.40, 41.00)
NYC_LON = (-74.35, -73.60)


@dataclass(frozen=True)
class WeatherPoint:
    point_id: int
    borough: str
    name: str
    latitude: float
    longitude: float


# Same five points (and ids) as src/config.py WEATHER_LOCATIONS, kept separate
# so changes to the original pipeline can't silently shift this dataset.
BOROUGH_POINTS: tuple[WeatherPoint, ...] = (
    WeatherPoint(1, "Bronx", "Bronx - Cross Bronx Expwy / Castle Hill Ave", 40.83242, -73.85833),
    WeatherPoint(2, "Manhattan", "Manhattan - Midtown West Side", 40.76000, -73.99000),
    WeatherPoint(3, "Brooklyn", "Brooklyn - Gowanus Expwy / Sunset Park", 40.66000, -74.00000),
    WeatherPoint(4, "Queens", "Queens - Central Queens", 40.72820, -73.79490),
    WeatherPoint(5, "Staten Island", "Staten Island - Central", 40.57950, -74.15020),
)
_POINT_BY_BOROUGH = {p.borough.casefold(): p for p in BOROUGH_POINTS}

WEATHER_VARS = {
    # Open-Meteo name -> column, and whether it is a preceding-hour total.
    "temperature_2m": ("temperature_c", False),
    "precipitation": ("precipitation_mm", True),
    "rain": ("rain_mm", True),
    "snowfall": ("snowfall_cm", True),
    "weather_code": ("weather_code", False),
    "wind_speed_10m": ("wind_speed_kmh", False),
}
WEATHER_COLUMNS = ["point_id", "hour_start", *(c for c, _ in WEATHER_VARS.values())]

TRAFFIC_SELECT = (
    "link_id, "
    "date_trunc_ymd(data_as_of) AS day, "
    "date_extract_hh(data_as_of) AS hour, "
    "avg(speed::number) AS avg_speed, "
    "median(speed::number) AS median_speed, "
    "min(speed::number) AS min_speed, "
    "max(speed::number) AS max_speed, "
    "avg(travel_time::number) AS avg_travel_time, "
    "max(travel_time::number) AS max_travel_time, "
    "count(*) AS readings"
)
LINK_SELECT = (
    "link_id, link_name, borough, owner, link_points, "
    "min(data_as_of) AS first_seen, max(data_as_of) AS last_seen"
)
TRAFFIC_COLUMNS = [
    "link_id",
    "hour_start",
    "avg_speed_mph",
    "median_speed_mph",
    "min_speed_mph",
    "max_speed_mph",
    "avg_travel_time_sec",
    "max_travel_time_sec",
    "reading_count",
]
LINK_COLUMNS = [
    "link_id",
    "link_name",
    "borough",
    "owner",
    "link_points",
    "mid_latitude",
    "mid_longitude",
    "weather_point_id",
    "first_seen",
    "last_seen",
]


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #

def default_end(today: date | None = None) -> date:
    return (today or date.today()) - timedelta(days=ARCHIVE_LAG_DAYS)


def chunks(start: date, end: date, days: int) -> Iterator[tuple[date, date]]:
    """Inclusive (chunk_start, chunk_end) pairs covering start..end."""
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=days - 1), end)
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _get_json(url: str, params: dict, timeout: int = 300, attempts: int = 4) -> object:
    headers = {}
    token = os.getenv("NYC_APP_TOKEN")
    if token and url == TRAFFIC_URL:
        headers["X-App-Token"] = token
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=timeout)
            if response.status_code == 200:
                return response.json()
            last_error = RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
            if response.status_code < 500 and response.status_code != 429:
                break  # a bad query will not fix itself
        except requests.RequestException as error:
            last_error = error
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Request to {url} failed") from last_error


def _socrata_rows(params: dict) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        page = _get_json(TRAFFIC_URL, {**params, "$limit": PAGE_SIZE, "$offset": offset})
        if not isinstance(page, list):
            raise RuntimeError(f"NYC DOT returned {type(page).__name__}, expected a list")
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        offset += len(page)


def _window_where(start: date, end: date) -> str:
    return (
        f"data_as_of >= '{start:%Y-%m-%d}T00:00:00' "
        f"AND data_as_of < '{end + timedelta(days=1):%Y-%m-%d}T00:00:00'"
    )


# --------------------------------------------------------------------------- #
# Traffic
# --------------------------------------------------------------------------- #

def fetch_traffic_hourly(start: date, end: date) -> pd.DataFrame:
    rows = _socrata_rows(
        {
            "$select": TRAFFIC_SELECT,
            "$where": f"status = '0' AND speed::number > 0 AND {_window_where(start, end)}",
            "$group": "link_id, day, hour",
            "$order": "link_id, day, hour",
        }
    )
    return build_traffic_hourly(rows)


def build_traffic_hourly(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=TRAFFIC_COLUMNS)
    df = pd.DataFrame(rows)
    numeric = {
        "avg_speed": "avg_speed_mph",
        "median_speed": "median_speed_mph",
        "min_speed": "min_speed_mph",
        "max_speed": "max_speed_mph",
        "avg_travel_time": "avg_travel_time_sec",
        "max_travel_time": "max_travel_time_sec",
        "readings": "reading_count",
    }
    for source in numeric:
        if source not in df:
            df[source] = None
        df[source] = pd.to_numeric(df[source], errors="coerce")
    df = df.rename(columns=numeric)
    df["link_id"] = df["link_id"].astype(str).str.strip()
    df["hour_start"] = pd.to_datetime(df["day"], errors="coerce") + pd.to_timedelta(
        pd.to_numeric(df["hour"], errors="coerce"), unit="h"
    )
    df = df.dropna(subset=["hour_start", "reading_count"])
    df = df.loc[df["link_id"].ne("")]
    for column in TRAFFIC_COLUMNS[2:-1]:
        df[column] = df[column].round(2)
    df["reading_count"] = df["reading_count"].astype(int)
    return df[TRAFFIC_COLUMNS].sort_values(["link_id", "hour_start"]).reset_index(drop=True)


def fetch_links(start: date, end: date) -> pd.DataFrame:
    """Every link that reported in the window, valid or flagged."""
    rows = _socrata_rows(
        {
            "$select": LINK_SELECT,
            "$where": _window_where(start, end),
            "$group": "link_id, link_name, borough, owner, link_points",
            "$order": "link_id",
        }
    )
    return build_links(rows)


def midpoint(link_points: object) -> tuple[float | None, float | None]:
    """Middle vertex of a DOT 'lat,lon lat,lon ...' path, skipping broken pairs."""
    if not isinstance(link_points, str):
        return None, None
    vertices: list[tuple[float, float]] = []
    for token in link_points.split():
        parts = token.split(",")
        if len(parts) != 2:
            continue
        try:
            lat, lon = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        if NYC_LAT[0] <= lat <= NYC_LAT[1] and NYC_LON[0] <= lon <= NYC_LON[1]:
            vertices.append((lat, lon))
    if not vertices:
        return None, None
    lat, lon = vertices[len(vertices) // 2]
    return round(lat, 5), round(lon, 5)


def point_for_borough(borough: object) -> WeatherPoint | None:
    if not isinstance(borough, str):
        return None
    return _POINT_BY_BOROUGH.get(" ".join(borough.split()).casefold())


def build_links(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=LINK_COLUMNS)
    df = pd.DataFrame(rows)
    for column in ("link_name", "borough", "owner", "link_points"):
        if column not in df:
            df[column] = None
    df["link_id"] = df["link_id"].astype(str).str.strip()
    df["first_seen"] = pd.to_datetime(df["first_seen"], errors="coerce")
    df["last_seen"] = pd.to_datetime(df["last_seen"], errors="coerce")
    # A link can appear more than once if its name or path changed; keep the
    # most recent description and the full first..last range.
    seen = df.groupby("link_id").agg(first_seen=("first_seen", "min"), last_seen=("last_seen", "max"))
    latest = (
        df.sort_values("last_seen")
        .drop_duplicates("link_id", keep="last")
        .drop(columns=["first_seen", "last_seen"])
        .merge(seen, on="link_id")
    )
    mids = latest["link_points"].map(midpoint)
    latest["mid_latitude"] = [m[0] for m in mids]
    latest["mid_longitude"] = [m[1] for m in mids]
    points = [point_for_borough(b) for b in latest["borough"]]
    # Store the canonical spelling ("Staten island " -> "Staten Island").
    latest["borough"] = [p.borough if p else b for p, b in zip(points, latest["borough"])]
    latest["weather_point_id"] = [p.point_id if p else None for p in points]
    return latest[LINK_COLUMNS].sort_values("link_id").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Weather
# --------------------------------------------------------------------------- #

def fetch_weather(
    start: date, end: date, points: tuple[WeatherPoint, ...] = BOROUGH_POINTS
) -> tuple[pd.DataFrame, list[dict]]:
    """Hourly weather for all points in one request.

    Requested in UTC (no DST gaps or repeats) with a day of padding each side,
    then shifted and converted to New York local time and trimmed.
    """
    payload = _get_json(
        WEATHER_URL,
        {
            "latitude": ",".join(f"{p.latitude}" for p in points),
            "longitude": ",".join(f"{p.longitude}" for p in points),
            "start_date": f"{start - timedelta(days=1):%Y-%m-%d}",
            "end_date": f"{end + timedelta(days=1):%Y-%m-%d}",
            "hourly": ",".join(WEATHER_VARS),
            "timezone": "GMT",
        },
        timeout=120,
    )
    if isinstance(payload, dict):
        if payload.get("error"):
            raise RuntimeError(f"Open-Meteo error: {payload.get('reason')}")
        payload = [payload]
    if not isinstance(payload, list) or len(payload) != len(points):
        raise RuntimeError("Open-Meteo returned an unexpected number of locations")

    frames, grids = [], []
    for point, location in zip(points, payload):
        frames.append(build_weather(point.point_id, location["hourly"], start, end))
        grids.append(
            {
                "point_id": point.point_id,
                "grid_latitude": location.get("latitude"),
                "grid_longitude": location.get("longitude"),
                "grid_elevation_m": location.get("elevation"),
            }
        )
    return pd.concat(frames, ignore_index=True), grids


def build_weather(point_id: int, hourly: dict, start: date, end: date) -> pd.DataFrame:
    df = pd.DataFrame(hourly)
    missing = {"time", *WEATHER_VARS}.difference(df.columns)
    if missing:
        raise RuntimeError(f"Open-Meteo response missing {sorted(missing)}")
    df["utc"] = pd.to_datetime(df["time"])
    df = df.sort_values("utc").set_index("utc")
    df = df.reindex(pd.date_range(df.index.min(), df.index.max(), freq="h"))
    out = pd.DataFrame(index=df.index)
    for source, (column, preceding_hour_total) in WEATHER_VARS.items():
        values = pd.to_numeric(df[source], errors="coerce")
        # A total stamped 15:00 covers 14:00-15:00, so it belongs to hour 14.
        out[column] = values.shift(-1) if preceding_hour_total else values
    local = out.index.tz_localize("UTC").tz_convert(LOCAL_TZ).tz_localize(None)
    out = out.reset_index(drop=True)
    out.insert(0, "hour_start", local)
    out.insert(0, "point_id", point_id)
    # Fall-back night: two UTC hours map to local 01:00. DOT's hourly buckets
    # also merge them, so keep the first.
    out = out.drop_duplicates(["point_id", "hour_start"], keep="first")
    window = (out["hour_start"] >= pd.Timestamp(start)) & (
        out["hour_start"] < pd.Timestamp(end + timedelta(days=1))
    )
    out = out.loc[window].reset_index(drop=True)
    out["weather_code"] = out["weather_code"].round().astype("Int64")
    return out[WEATHER_COLUMNS]


# --------------------------------------------------------------------------- #
# MySQL
# --------------------------------------------------------------------------- #

def schema_statements(path=SCHEMA_PATH) -> list[str]:
    """Statements from the schema file, minus CREATE DATABASE / USE.

    The app user only has rights inside weather_traffic and is already
    connected to it.
    """
    statements, current = [], []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        current.append(line)
        if stripped.endswith(";"):
            statement = "\n".join(current).strip().removesuffix(";").strip()
            current = []
            head = statement.upper()
            if head.startswith("CREATE DATABASE") or head.startswith("USE "):
                continue
            statements.append(statement)
    if current:
        raise ValueError(f"Unterminated SQL statement in {path}")
    return statements


def connect(settings: Settings) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        database=settings.mysql_database,
        autocommit=False,
        charset="utf8mb4",
    )


def apply_schema(connection) -> None:
    with connection.cursor() as cursor:
        for statement in schema_statements():
            cursor.execute(statement)
        cursor.executemany(
            """
            INSERT INTO weather_points
                (point_id, point_type, name, borough, latitude, longitude)
            VALUES (%s, 'borough', %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                name = VALUES(name), borough = VALUES(borough),
                latitude = VALUES(latitude), longitude = VALUES(longitude)
            """,
            [(p.point_id, p.name, p.borough, p.latitude, p.longitude) for p in BOROUGH_POINTS],
        )
    connection.commit()


def _clean(value: object) -> object:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if hasattr(value, "item"):  # numpy scalar
        return value.item()
    return value


def _rows(df: pd.DataFrame, columns: list[str]) -> list[tuple]:
    return [tuple(_clean(v) for v in row) for row in df[columns].itertuples(index=False)]


def _insert(cursor, sql: str, rows: list[tuple]) -> None:
    for i in range(0, len(rows), INSERT_BATCH):
        cursor.executemany(sql, rows[i : i + INSERT_BATCH])


UPSERT_LINKS = """
INSERT INTO dot_links (
    link_id, link_name, borough, owner, link_points,
    mid_latitude, mid_longitude, weather_point_id, first_seen, last_seen
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    link_name = IF(VALUES(last_seen) >= last_seen OR last_seen IS NULL, VALUES(link_name), link_name),
    borough = IF(VALUES(last_seen) >= last_seen OR last_seen IS NULL, VALUES(borough), borough),
    owner = IF(VALUES(last_seen) >= last_seen OR last_seen IS NULL, VALUES(owner), owner),
    link_points = IF(VALUES(last_seen) >= last_seen OR last_seen IS NULL, VALUES(link_points), link_points),
    mid_latitude = IF(VALUES(last_seen) >= last_seen OR last_seen IS NULL, VALUES(mid_latitude), mid_latitude),
    mid_longitude = IF(VALUES(last_seen) >= last_seen OR last_seen IS NULL, VALUES(mid_longitude), mid_longitude),
    weather_point_id = COALESCE(weather_point_id, VALUES(weather_point_id)),
    first_seen = LEAST(COALESCE(first_seen, VALUES(first_seen)), COALESCE(VALUES(first_seen), first_seen)),
    last_seen = GREATEST(COALESCE(last_seen, VALUES(last_seen)), COALESCE(VALUES(last_seen), last_seen))
"""

UPSERT_TRAFFIC = """
INSERT INTO dot_traffic_hourly (
    link_id, hour_start, avg_speed_mph, median_speed_mph, min_speed_mph,
    max_speed_mph, avg_travel_time_sec, max_travel_time_sec, reading_count
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    avg_speed_mph = VALUES(avg_speed_mph),
    median_speed_mph = VALUES(median_speed_mph),
    min_speed_mph = VALUES(min_speed_mph),
    max_speed_mph = VALUES(max_speed_mph),
    avg_travel_time_sec = VALUES(avg_travel_time_sec),
    max_travel_time_sec = VALUES(max_travel_time_sec),
    reading_count = VALUES(reading_count)
"""

UPSERT_WEATHER = """
INSERT INTO weather_point_hourly (
    point_id, hour_start, temperature_c, precipitation_mm, rain_mm,
    snowfall_cm, weather_code, wind_speed_kmh
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    temperature_c = VALUES(temperature_c),
    precipitation_mm = VALUES(precipitation_mm),
    rain_mm = VALUES(rain_mm),
    snowfall_cm = VALUES(snowfall_cm),
    weather_code = VALUES(weather_code),
    wind_speed_kmh = VALUES(wind_speed_kmh)
"""

LOG_CHUNK = """
INSERT INTO load_chunks (source, chunk_start, chunk_end, rows_loaded, seconds)
VALUES (%s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    rows_loaded = VALUES(rows_loaded), seconds = VALUES(seconds),
    loaded_at = CURRENT_TIMESTAMP
"""


def loaded_chunks(connection, source: str) -> set[tuple[date, date]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT chunk_start, chunk_end FROM load_chunks WHERE source = %s", (source,)
        )
        return {(row[0], row[1]) for row in cursor.fetchall()}


def store_traffic(connection, links: pd.DataFrame, hourly: pd.DataFrame, chunk, seconds) -> None:
    # Hourly rows need their link row first (foreign key). The metadata query
    # covers every reading in the window, so this is only a safety net.
    missing = sorted(set(hourly["link_id"]) - set(links["link_id"]))
    if missing:
        print(f"  warning: no metadata for links {missing[:5]}; adding bare rows")
        stub = pd.DataFrame({"link_id": missing}).reindex(columns=LINK_COLUMNS)
        links = pd.concat([links, stub], ignore_index=True)
    try:
        with connection.cursor() as cursor:
            _insert(cursor, UPSERT_LINKS, _rows(links, LINK_COLUMNS))
            _insert(cursor, UPSERT_TRAFFIC, _rows(hourly, TRAFFIC_COLUMNS))
            cursor.execute(LOG_CHUNK, ("traffic", chunk[0], chunk[1], len(hourly), seconds))
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def store_weather(connection, weather: pd.DataFrame, grids: list[dict], chunk, seconds) -> None:
    try:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                UPDATE weather_points
                SET grid_latitude = %s, grid_longitude = %s, grid_elevation_m = %s
                WHERE point_id = %s
                """,
                [
                    (g["grid_latitude"], g["grid_longitude"], g["grid_elevation_m"], g["point_id"])
                    for g in grids
                ],
            )
            _insert(cursor, UPSERT_WEATHER, _rows(weather, WEATHER_COLUMNS))
            cursor.execute(LOG_CHUNK, ("weather", chunk[0], chunk[1], len(weather), seconds))
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def print_summary(connection) -> None:
    queries = {
        "links": "SELECT COUNT(*) FROM dot_links",
        "links with traffic": "SELECT COUNT(DISTINCT link_id) FROM dot_traffic_hourly",
        "traffic link-hours": "SELECT COUNT(*) FROM dot_traffic_hourly",
        "traffic range": "SELECT CONCAT(MIN(hour_start), ' .. ', MAX(hour_start)) FROM dot_traffic_hourly",
        "weather point-hours": "SELECT COUNT(*) FROM weather_point_hourly",
        "view rows missing weather": (
            "SELECT COUNT(*) FROM vw_traffic_weather_hourly WHERE temperature_c IS NULL"
        ),
    }
    print("\nDATABASE SUMMARY")
    with connection.cursor() as cursor:
        for label, sql in queries.items():
            cursor.execute(sql)
            print(f"  {label}: {cursor.fetchone()[0]}")
        cursor.execute(
            """
            SELECT COALESCE(l.borough, '(none)'), COUNT(DISTINCT t.link_id), COUNT(*)
            FROM dot_traffic_hourly t JOIN dot_links l USING (link_id)
            GROUP BY l.borough ORDER BY 1
            """
        )
        for borough, links, hours in cursor.fetchall():
            print(f"  {borough}: {links} links, {hours} link-hours")


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #

def run(start: date, end: date, *, force=False, dry_run=False, traffic=True, weather=True) -> None:
    if end < start:
        raise ValueError("--end must be on or after --start")
    print(f"Window: {start} .. {end}  (traffic in {TRAFFIC_CHUNK_DAYS}-day chunks)")

    connection = None
    done_traffic: set = set()
    done_weather: set = set()
    if not dry_run:
        connection = connect(Settings.from_env())
        apply_schema(connection)
        if not force:
            done_traffic = loaded_chunks(connection, "traffic")
            done_weather = loaded_chunks(connection, "weather")

    try:
        if weather:
            for chunk in chunks(start, end, WEATHER_CHUNK_DAYS):
                if chunk in done_weather:
                    print(f"Weather {chunk[0]} .. {chunk[1]}: already loaded, skipping")
                    continue
                began = time.time()
                frame, grids = fetch_weather(*chunk)
                seconds = round(time.time() - began, 1)
                if connection:
                    store_weather(connection, frame, grids, chunk, seconds)
                print(f"Weather {chunk[0]} .. {chunk[1]}: {len(frame)} point-hours in {seconds}s")

        if traffic:
            all_chunks = list(chunks(start, end, TRAFFIC_CHUNK_DAYS))
            for number, chunk in enumerate(all_chunks, start=1):
                label = f"Traffic [{number}/{len(all_chunks)}] {chunk[0]} .. {chunk[1]}"
                if chunk in done_traffic:
                    print(f"{label}: already loaded, skipping")
                    continue
                began = time.time()
                links = fetch_links(*chunk)
                hourly = fetch_traffic_hourly(*chunk)
                seconds = round(time.time() - began, 1)
                if connection:
                    store_traffic(connection, links, hourly, chunk, seconds)
                print(
                    f"{label}: {len(hourly)} link-hours, "
                    f"{hourly['link_id'].nunique()} links with valid data, "
                    f"{len(links)} reporting, {seconds}s"
                )

        if connection:
            print_summary(connection)
            print(
                "\nTableau: MySQL "
                f"{connection.host}:{connection.port}, database weather_traffic, "
                "view vw_traffic_weather_hourly"
            )
    finally:
        if connection:
            connection.close()


def _date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--start", type=_date, default=DEFAULT_START, help="YYYY-MM-DD, inclusive")
    parser.add_argument(
        "--end", type=_date, default=None,
        help=f"YYYY-MM-DD, inclusive (default: today minus {ARCHIVE_LAG_DAYS} days)",
    )
    parser.add_argument("--force", action="store_true", help="Re-fetch chunks already loaded")
    parser.add_argument("--dry-run", action="store_true", help="Fetch only; don't touch MySQL")
    parser.add_argument("--traffic-only", action="store_true")
    parser.add_argument("--weather-only", action="store_true")
    args = parser.parse_args()
    run(
        args.start,
        args.end or default_end(),
        force=args.force,
        dry_run=args.dry_run,
        traffic=not args.weather_only,
        weather=not args.traffic_only,
    )


if __name__ == "__main__":
    main()
