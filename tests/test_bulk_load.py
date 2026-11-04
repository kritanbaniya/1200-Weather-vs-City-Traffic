import unittest
from datetime import date

import pandas as pd

from src import bulk_load as bl


class ChunkTests(unittest.TestCase):
    def test_chunks_cover_window_without_gaps(self) -> None:
        parts = list(bl.chunks(date(2025, 10, 1), date(2025, 10, 20), 7))
        self.assertEqual(
            parts,
            [
                (date(2025, 10, 1), date(2025, 10, 7)),
                (date(2025, 10, 8), date(2025, 10, 14)),
                (date(2025, 10, 15), date(2025, 10, 20)),
            ],
        )

    def test_default_end_lags_archive(self) -> None:
        self.assertEqual(bl.default_end(date(2026, 11, 4)), date(2026, 10, 30))


class MidpointTests(unittest.TestCase):
    def test_middle_vertex_skips_truncated_pairs(self) -> None:
        points = "40.1,-73.9 40.70,-73.90 40.71,-73.91 40.72,-73.92 40.7"
        self.assertEqual(bl.midpoint(points), (40.71, -73.91))

    def test_missing_path(self) -> None:
        self.assertEqual(bl.midpoint(None), (None, None))
        self.assertEqual(bl.midpoint("garbage"), (None, None))


class TrafficTests(unittest.TestCase):
    def test_server_rows_become_typed_hours(self) -> None:
        rows = [
            {
                "link_id": "4616253",
                "day": "2025-10-01T00:00:00.000",
                "hour": "14",
                "avg_speed": "31.456",
                "median_speed": "31.0",
                "min_speed": "20",
                "max_speed": "40",
                "avg_travel_time": "120.333",
                "max_travel_time": "200",
                "readings": "12",
            }
        ]
        df = bl.build_traffic_hourly(rows)
        self.assertEqual(df.loc[0, "hour_start"], pd.Timestamp("2025-10-01 14:00"))
        self.assertEqual(df.loc[0, "avg_speed_mph"], 31.46)
        self.assertEqual(df.loc[0, "reading_count"], 12)

    def test_links_keep_latest_name_and_full_range(self) -> None:
        rows = [
            {"link_id": "1", "link_name": "Old", "borough": "Queens",
             "first_seen": "2025-10-01T00:00:00", "last_seen": "2025-10-03T00:00:00"},
            {"link_id": "1", "link_name": "New", "borough": "Queens",
             "first_seen": "2025-10-04T00:00:00", "last_seen": "2025-10-07T00:00:00"},
            {"link_id": "2", "link_name": "X", "borough": "Nowhere",
             "first_seen": "2025-10-01T00:00:00", "last_seen": "2025-10-01T00:00:00"},
        ]
        links = bl.build_links(rows).set_index("link_id")
        self.assertEqual(links.loc["1", "link_name"], "New")
        self.assertEqual(links.loc["1", "first_seen"], pd.Timestamp("2025-10-01"))
        self.assertEqual(links.loc["1", "weather_point_id"], 4)
        self.assertTrue(pd.isna(links.loc["2", "weather_point_id"]))


class WeatherTests(unittest.TestCase):
    def _hourly(self, start_utc: str, hours: int) -> dict:
        times = pd.date_range(start_utc, periods=hours, freq="h")
        return {
            "time": [t.strftime("%Y-%m-%dT%H:%M") for t in times],
            "temperature_2m": [float(i) for i in range(hours)],
            "precipitation": [float(i) for i in range(hours)],
            "rain": [float(i) for i in range(hours)],
            "snowfall": [0.0] * hours,
            "weather_code": [3] * hours,
            "wind_speed_10m": [10.0] * hours,
        }

    def test_totals_move_back_one_hour_and_times_are_local(self) -> None:
        # 2025-10-02 local (EDT, UTC-4) needs UTC 2025-10-01 .. 2025-10-03.
        hourly = self._hourly("2025-10-01T00:00", 72)
        df = bl.build_weather(1, hourly, date(2025, 10, 2), date(2025, 10, 2))
        self.assertEqual(len(df), 24)
        first = df.iloc[0]
        self.assertEqual(first["hour_start"], pd.Timestamp("2025-10-02 00:00"))
        # Local 00:00 EDT = UTC 04:00 -> index 28. Temperature is the value at
        # that hour; precipitation is the total stamped one hour later.
        self.assertEqual(first["temperature_c"], 28.0)
        self.assertEqual(first["precipitation_mm"], 29.0)

    def test_fall_back_day_has_no_duplicate_hours(self) -> None:
        hourly = self._hourly("2025-11-01T00:00", 96)
        df = bl.build_weather(1, hourly, date(2025, 11, 2), date(2025, 11, 2))
        self.assertFalse(df["hour_start"].duplicated().any())
        self.assertEqual(len(df), 24)

    def test_spring_forward_day_has_23_hours(self) -> None:
        hourly = self._hourly("2026-03-07T00:00", 96)
        df = bl.build_weather(1, hourly, date(2026, 3, 8), date(2026, 3, 8))
        self.assertEqual(len(df), 23)
        self.assertNotIn(pd.Timestamp("2026-03-08 02:00"), set(df["hour_start"]))


class SchemaTests(unittest.TestCase):
    def test_app_user_statements_skip_database_level_lines(self) -> None:
        statements = bl.schema_statements()
        self.assertFalse(any(s.upper().startswith(("CREATE DATABASE", "USE ")) for s in statements))
        self.assertTrue(any("vw_traffic_weather_hourly" in s for s in statements))
        # Never touches the original pipeline's tables.
        for table in ("road_links", "traffic_hourly ", "weather_hourly", "locations"):
            self.assertFalse(
                any(f"TABLE IF NOT EXISTS {table}" in s for s in statements), table
            )


if __name__ == "__main__":
    unittest.main()
