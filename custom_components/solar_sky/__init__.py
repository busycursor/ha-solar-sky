"""Solar Sky: cloud cover and a sky view from your solar panels' output."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components import frontend, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .const import CARD_FILE, CARD_URL, CONF_WEATHER, CONF_WIND_DIRECTION, CONF_WIND_SPEED, DOMAIN, VERSION
from .runtime import SolarSkyRuntime

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type SolarSkyConfigEntry = ConfigEntry[SolarSkyRuntime]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the card and register the websocket command once."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(Path(__file__).parent / CARD_FILE), cache_headers=False)]
    )
    frontend.add_extra_js_url(hass, f"{CARD_URL}?v={VERSION}")
    websocket_api.async_register_command(hass, ws_info)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SolarSkyConfigEntry) -> bool:
    runtime = SolarSkyRuntime(hass, entry)
    await runtime.async_start()
    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_create_background_task(hass, runtime.async_bootstrap_if_needed(), f"{DOMAIN} bootstrap")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SolarSkyConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_stop()
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    from homeassistant.helpers.storage import Store

    from .const import STORAGE_VERSION

    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()


def _entity(hass: HomeAssistant, entry_id: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry_id}_{key}")


@websocket_api.websocket_command({vol.Required("type"): "solar_sky/info", vol.Optional("entry_id"): str})
@callback
def ws_info(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Everything the card needs: which statistics to read, and the learned horizon."""
    entries = [e for e in hass.config_entries.async_loaded_entries(DOMAIN)]
    if msg.get("entry_id"):
        entries = [e for e in entries if e.entry_id == msg["entry_id"]]
    out = []
    for e in entries:
        rt: SolarSkyRuntime = e.runtime_data
        opts = rt.opts
        wind_dir = opts.get(CONF_WIND_DIRECTION) or (
            _entity(hass, e.entry_id, "wind_direction") if opts.get(CONF_WEATHER) else None
        )
        wind_spd = opts.get(CONF_WIND_SPEED) or (_entity(hass, e.entry_id, "wind_speed") if opts.get(CONF_WEATHER) else None)
        spd_state = hass.states.get(wind_spd) if wind_spd else None
        out.append(
            {
                "entry_id": e.entry_id,
                "title": e.title,
                "cloud_cover": _entity(hass, e.entry_id, "cloud_cover"),
                "weather_cloud_cover": _entity(hass, e.entry_id, "weather_cloud_coverage") if opts.get(CONF_WEATHER) else None,
                "wind_direction": wind_dir,
                "wind_speed": wind_spd,
                "wind_speed_unit": spd_state.attributes.get("unit_of_measurement") if spd_state else None,
                "horizon": rt.learner.horizon(),
                "tilt": rt.learner.tilt,
                "azimuth": rt.learner.azimuth,
                "progress": rt.progress,
                "status": rt.bootstrap_status,
            }
        )
    connection.send_result(msg["id"], out)
