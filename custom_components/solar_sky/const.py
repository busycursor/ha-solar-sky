"""Constants for Solar Sky."""

from __future__ import annotations

DOMAIN = "solar_sky"
VERSION = "0.9.0"

CONF_POWER = "power_entity"
CONF_TILT = "tilt"
CONF_AZIMUTH = "azimuth"
CONF_TEMPERATURE = "temperature_entity"
CONF_WEATHER = "weather_entity"
CONF_WIND_DIRECTION = "wind_direction_entity"
CONF_WIND_SPEED = "wind_speed_entity"
CONF_BACKFILL = "backfill"

STORAGE_VERSION = 1
SAVE_DELAY = 120  # seconds

CARD_URL = f"/{DOMAIN}/solar-sky-card.js"
CARD_FILE = "frontend/solar-sky-card.js"
