"""Create the MySQL schema and replace the loaded date window."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pymysql

from src.config import PROJECT_ROOT, WEATHER_LOCATIONS, Settings

SCHEMA_PATH = PROJECT_ROOT / "sql" / "schema.sql"


def _sql_statements(path: Path) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        current.append(line)
        if stripped.endswith(";"):
            statement = "\n".join(current).rstrip().removesuffix(";").strip()
            if statement:
                statements.append(statement)
            current = []
    if current:
        raise ValueError(f"Unterminated SQL statement in {path}")
    return statements


def _connect(settings: Settings) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        database=settings.mysql_database,
        autocommit=False,
        charset="utf8mb4",
    )


def _none_if_na(value: object) -> object:
    if value is None or pd.isna(value):
        return None
    return value


def _executemany(
    cursor: pymysql.cursors.Cursor,
    sql: str,
    rows: list[tuple],
) -> None:
    if rows:
        cursor.executemany(sql, rows)


def _apply_schema(cursor: pymysql.cursors.Cursor) -> None:
    """Create tables, upgrade databases from the single-location layout, then views."""
    statements = _sql_statements(SCHEMA_PATH)
    views = [s for s in statements if s.upper().startswith("CREATE OR REPLACE VIEW")]
    for statement in statements:
        if statement not in views:
            cursor.execute(statement)

    # Databases created before per-borough weather have no road_links.location_id.
    cursor.execute(
        """
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 'road_links'
          AND column_name = 'location_id'
        """
    )
    if cursor.fetchone()[0] == 0:
        cursor.execute("ALTER TABLE road_links ADD COLUMN location_id INT NULL")
        cursor.execute(
            """
            ALTER TABLE road_links
            ADD CONSTRAINT fk_road_link_location
            FOREIGN KEY (location_id) REFERENCES locations (location_id)
            """
        )

    for statement in views:
        cursor.execute(statement)


def load(
    settings: Settings,
    traffic_hourly: pd.DataFrame,
    weather: pd.DataFrame,
) -> None:
    """Reload road links, weather hours, and traffic hours for this run."""
    connection = _connect(settings)
    try:
        with connection.cursor() as cursor:
            _apply_schema(cursor)

            _executemany(
                cursor,
                """
                INSERT INTO locations (location_id, name, latitude, longitude)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    name = VALUES(name),
                    latitude = VALUES(latitude),
                    longitude = VALUES(longitude)
                """,
                [
                    (
                        location.location_id,
                        location.name,
                        location.latitude,
                        location.longitude,
                    )
                    for location in WEATHER_LOCATIONS
                ],
            )

            links = (
                traffic_hourly[["link_id", "link_name", "borough", "location_id"]]
                .drop_duplicates(subset=["link_id"])
                .itertuples(index=False)
            )
            _executemany(
                cursor,
                """
                INSERT INTO road_links (link_id, link_name, borough, location_id)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    link_name = VALUES(link_name),
                    borough = VALUES(borough),
                    location_id = VALUES(location_id)
                """,
                [
                    (
                        str(link.link_id),
                        link.link_name,
                        _none_if_na(link.borough),
                        None if pd.isna(link.location_id) else int(link.location_id),
                    )
                    for link in links
                ],
            )

            # Weather labels are shifted one hour earlier because Open-Meteo
            # stamps accumulated rain at the end of its observation interval.
            hour_start = datetime.strptime(
                settings.start_date, "%Y-%m-%d"
            ) - timedelta(hours=1)
            hour_end = settings.end_date + " 23:59:59"
            location_ids = sorted({int(value) for value in weather["location_id"]})
            if location_ids:
                location_placeholders = ", ".join(["%s"] * len(location_ids))
                cursor.execute(
                    f"""
                    DELETE FROM weather_hourly
                    WHERE location_id IN ({location_placeholders})
                      AND hour_start BETWEEN %s AND %s
                    """,
                    (*location_ids, hour_start, hour_end),
                )
            _executemany(
                cursor,
                """
                INSERT INTO weather_hourly (
                    location_id, hour_start, temperature_c, rain_mm, precipitation_mm
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                [
                    (
                        int(row.location_id),
                        row.hour_start.to_pydatetime(),
                        _none_if_na(row.temperature_c),
                        _none_if_na(row.rain_mm),
                        _none_if_na(row.precipitation_mm),
                    )
                    for row in weather.itertuples(index=False)
                ],
            )

            link_ids = [str(link_id) for link_id in settings.link_ids]
            placeholders = ", ".join(["%s"] * len(link_ids))
            cursor.execute(
                f"""
                DELETE FROM traffic_hourly
                WHERE link_id IN ({placeholders})
                  AND hour_start BETWEEN %s AND %s
                """,
                (*link_ids, settings.start_date + " 00:00:00", hour_end),
            )
            _executemany(
                cursor,
                """
                INSERT INTO traffic_hourly (
                    link_id, hour_start, avg_speed_mph,
                    avg_travel_time_sec, reading_count
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                [
                    (
                        str(row.link_id),
                        row.hour_start.to_pydatetime(),
                        _none_if_na(row.avg_speed_mph),
                        _none_if_na(row.avg_travel_time_sec),
                        int(row.reading_count),
                    )
                    for row in traffic_hourly.itertuples(index=False)
                ],
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def count_view_rows(settings: Settings) -> int:
    connection = _connect(settings)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM vw_hourly_weather_traffic")
            row = cursor.fetchone()
        return int(row[0]) if row else 0
    finally:
        connection.close()
