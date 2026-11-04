-- Schema for the multi-link, multi-year load (src/bulk_load.py).
--
-- These tables sit next to the original pipeline's tables (locations,
-- road_links, weather_hourly, traffic_hourly, vw_hourly_weather_traffic) in the
-- same weather_traffic database and never touch them.
--
-- Run once as an admin after sql/setup_database.sql:
--     mysql -u root < sql/full_year_schema.sql
-- src/bulk_load.py also applies this file on every run (it skips the
-- CREATE DATABASE and USE lines), so running it by hand is optional.
--
-- Every statement is idempotent: re-running it never drops data.
--
-- Time convention: every hour_start is New York local time (naive DATETIME),
-- matching NYC DOT's data_as_of. A row stamped 14:00 covers 14:00-14:59.

CREATE DATABASE IF NOT EXISTS weather_traffic CHARACTER SET utf8mb4;
USE weather_traffic;

-- Open-Meteo request points. point_type 'borough' = the five borough points used
-- now; 'link' is reserved for per-segment midpoints added later.
CREATE TABLE IF NOT EXISTS weather_points (
    point_id INT UNSIGNED NOT NULL,
    point_type ENUM('borough', 'link') NOT NULL,
    name VARCHAR(100) NOT NULL,
    borough VARCHAR(32) NULL,
    latitude DECIMAL(8, 5) NOT NULL,           -- what we asked for
    longitude DECIMAL(8, 5) NOT NULL,
    grid_latitude DECIMAL(8, 5) NULL,          -- grid cell Open-Meteo snapped to
    grid_longitude DECIMAL(8, 5) NULL,
    grid_elevation_m DECIMAL(7, 1) NULL,
    PRIMARY KEY (point_id)
);

-- One row per NYC DOT segment (highways, bridges, tunnels).
CREATE TABLE IF NOT EXISTS dot_links (
    link_id VARCHAR(16) NOT NULL,
    link_name VARCHAR(255) NULL,
    borough VARCHAR(32) NULL,
    owner VARCHAR(64) NULL,
    link_points TEXT NULL,                     -- raw "lat,lon lat,lon ..." path
    mid_latitude DECIMAL(8, 5) NULL,           -- middle vertex of the path, for maps
    mid_longitude DECIMAL(8, 5) NULL,
    weather_point_id INT UNSIGNED NULL,        -- which weather_points row it uses
    first_seen DATETIME NULL,                  -- earliest reading in loaded windows
    last_seen DATETIME NULL,                   -- latest reading in loaded windows
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (link_id),
    KEY idx_dot_links_borough (borough),
    CONSTRAINT fk_dot_links_weather_point
        FOREIGN KEY (weather_point_id) REFERENCES weather_points (point_id)
);

-- Hourly aggregate of valid DOT readings (status = '0' and speed > 0),
-- computed server-side by Socrata. ~5-minute readings -> up to ~12 per hour.
CREATE TABLE IF NOT EXISTS dot_traffic_hourly (
    link_id VARCHAR(16) NOT NULL,
    hour_start DATETIME NOT NULL,
    avg_speed_mph DECIMAL(6, 2) NULL,
    median_speed_mph DECIMAL(6, 2) NULL,
    min_speed_mph DECIMAL(6, 2) NULL,
    max_speed_mph DECIMAL(6, 2) NULL,
    avg_travel_time_sec DECIMAL(9, 2) NULL,
    max_travel_time_sec DECIMAL(9, 2) NULL,
    reading_count SMALLINT UNSIGNED NOT NULL,
    PRIMARY KEY (link_id, hour_start),
    KEY idx_dot_traffic_hour (hour_start),
    CONSTRAINT fk_dot_traffic_link
        FOREIGN KEY (link_id) REFERENCES dot_links (link_id)
);

-- Hourly Open-Meteo archive values per point.
-- precipitation/rain/snowfall are totals for hour_start..hour_start+1h
-- (Open-Meteo stamps them at the end of the hour; the loader shifts them).
-- temperature, wind and weather_code are the values at hour_start.
CREATE TABLE IF NOT EXISTS weather_point_hourly (
    point_id INT UNSIGNED NOT NULL,
    hour_start DATETIME NOT NULL,
    temperature_c DECIMAL(5, 2) NULL,
    precipitation_mm DECIMAL(6, 2) NULL,
    rain_mm DECIMAL(6, 2) NULL,
    snowfall_cm DECIMAL(6, 2) NULL,
    weather_code TINYINT UNSIGNED NULL,        -- WMO code
    wind_speed_kmh DECIMAL(5, 1) NULL,         -- at 10 m
    PRIMARY KEY (point_id, hour_start),
    KEY idx_weather_point_hour (hour_start),
    CONSTRAINT fk_weather_point_hourly_point
        FOREIGN KEY (point_id) REFERENCES weather_points (point_id)
);

-- Which date chunks have been loaded, so an interrupted run resumes and a
-- later backfill of earlier years only fetches what is missing.
CREATE TABLE IF NOT EXISTS load_chunks (
    source ENUM('traffic', 'weather') NOT NULL,
    chunk_start DATE NOT NULL,                 -- inclusive
    chunk_end DATE NOT NULL,                   -- inclusive
    rows_loaded INT UNSIGNED NOT NULL,
    seconds DECIMAL(8, 1) NULL,
    loaded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (source, chunk_start, chunk_end)
);

-- Tableau / analysis view: one row per link-hour with its weather.
CREATE OR REPLACE VIEW vw_traffic_weather_hourly AS
SELECT
    t.hour_start,
    DATE(t.hour_start) AS observation_date,
    HOUR(t.hour_start) AS hour_of_day,
    DAYOFWEEK(t.hour_start) AS day_of_week,    -- 1 = Sunday ... 7 = Saturday
    DAYNAME(t.hour_start) AS day_name,
    CASE WHEN DAYOFWEEK(t.hour_start) IN (1, 7) THEN 1 ELSE 0 END AS is_weekend,
    t.link_id,
    l.link_name,
    l.borough,
    l.mid_latitude,
    l.mid_longitude,
    t.avg_speed_mph,
    t.median_speed_mph,
    t.min_speed_mph,
    t.max_speed_mph,
    t.avg_travel_time_sec,
    t.max_travel_time_sec,
    t.reading_count,
    p.name AS weather_point,
    w.temperature_c,
    ROUND(w.temperature_c * 9 / 5 + 32, 1) AS temperature_f,
    w.precipitation_mm,
    ROUND(w.precipitation_mm / 25.4, 3) AS precipitation_in,
    w.rain_mm,
    w.snowfall_cm,
    w.weather_code,
    w.wind_speed_kmh,
    ROUND(w.wind_speed_kmh / 1.609344, 1) AS wind_speed_mph,
    CASE
        WHEN w.precipitation_mm IS NULL THEN NULL
        WHEN w.precipitation_mm > 0 THEN 1
        ELSE 0
    END AS is_precipitating,
    CASE
        WHEN w.snowfall_cm IS NULL THEN NULL
        WHEN w.snowfall_cm > 0 THEN 1
        ELSE 0
    END AS is_snowing
FROM dot_traffic_hourly AS t
INNER JOIN dot_links AS l
    ON l.link_id = t.link_id
LEFT JOIN weather_points AS p
    ON p.point_id = l.weather_point_id
LEFT JOIN weather_point_hourly AS w
    ON w.point_id = l.weather_point_id
    AND w.hour_start = t.hour_start;
