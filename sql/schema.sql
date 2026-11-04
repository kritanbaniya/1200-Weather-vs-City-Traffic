-- Analytic tables for hourly NYC traffic joined to per-borough weather.
-- Tableau should connect to vw_hourly_weather_traffic.

CREATE TABLE IF NOT EXISTS locations (
    location_id INT PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    latitude DECIMAL(8, 5) NOT NULL,
    longitude DECIMAL(8, 5) NOT NULL
);

CREATE TABLE IF NOT EXISTS road_links (
    link_id VARCHAR(32) PRIMARY KEY,
    link_name VARCHAR(255) NOT NULL,
    borough VARCHAR(64) NULL,
    location_id INT NULL,
    CONSTRAINT fk_road_link_location
        FOREIGN KEY (location_id) REFERENCES locations (location_id)
);

CREATE TABLE IF NOT EXISTS weather_hourly (
    location_id INT NOT NULL,
    hour_start DATETIME NOT NULL,
    temperature_c DECIMAL(6, 2) NULL,
    rain_mm DECIMAL(8, 3) NULL,
    precipitation_mm DECIMAL(8, 3) NULL,
    PRIMARY KEY (location_id, hour_start),
    CONSTRAINT fk_weather_location
        FOREIGN KEY (location_id) REFERENCES locations (location_id)
);

CREATE TABLE IF NOT EXISTS traffic_hourly (
    link_id VARCHAR(32) NOT NULL,
    hour_start DATETIME NOT NULL,
    avg_speed_mph DECIMAL(8, 2) NULL,
    avg_travel_time_sec DECIMAL(10, 2) NULL,
    reading_count INT NOT NULL,
    PRIMARY KEY (link_id, hour_start),
    CONSTRAINT fk_traffic_link
        FOREIGN KEY (link_id) REFERENCES road_links (link_id)
);

CREATE OR REPLACE VIEW vw_hourly_weather_traffic AS
SELECT
    t.hour_start,
    DATE(t.hour_start) AS observation_date,
    HOUR(t.hour_start) AS hour_of_day,
    DAYNAME(t.hour_start) AS day_name,
    t.link_id,
    r.link_name,
    r.borough,
    l.name AS weather_location,
    t.avg_speed_mph,
    t.avg_travel_time_sec,
    t.reading_count,
    w.temperature_c,
    ROUND(w.temperature_c * 9 / 5 + 32, 2) AS temperature_f,
    w.rain_mm,
    w.precipitation_mm,
    CASE
        WHEN w.rain_mm IS NULL THEN NULL
        WHEN w.rain_mm > 0 THEN 1
        ELSE 0
    END AS is_raining
FROM traffic_hourly AS t
INNER JOIN road_links AS r
    ON r.link_id = t.link_id
LEFT JOIN locations AS l
    ON l.location_id = r.location_id
LEFT JOIN weather_hourly AS w
    ON w.hour_start = t.hour_start
    AND w.location_id = r.location_id;
