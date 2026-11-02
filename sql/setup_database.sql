CREATE DATABASE IF NOT EXISTS weather_traffic;

CREATE USER IF NOT EXISTS 'weather'@'localhost' IDENTIFIED BY 'weather';
CREATE USER IF NOT EXISTS 'weather'@'127.0.0.1' IDENTIFIED BY 'weather';

GRANT ALL PRIVILEGES ON weather_traffic.* TO 'weather'@'localhost';
GRANT ALL PRIVILEGES ON weather_traffic.* TO 'weather'@'127.0.0.1';

FLUSH PRIVILEGES;
