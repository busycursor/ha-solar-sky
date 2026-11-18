"""Config flow for Solar Sky."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .const import (
    CONF_AZIMUTH,
    CONF_BACKFILL,
    CONF_POWER,
    CONF_TEMPERATURE,
    CONF_TILT,
    CONF_WEATHER,
    CONF_WIND_DIRECTION,
    CONF_WIND_SPEED,
    DOMAIN,
)
from .engine.learner import default_orientation
from .engine.physics import Site

OPTIONAL = (CONF_TEMPERATURE, CONF_WEATHER, CONF_WIND_DIRECTION, CONF_WIND_SPEED)


def _forecast_solar_plane(hass: HomeAssistant) -> tuple[float, float] | None:
    """Tilt/azimuth of the first Forecast.Solar plane, if that integration is set up."""
    for entry in hass.config_entries.async_entries("forecast_solar"):
        for sub in entry.subentries.values():
            if sub.subentry_type == "plane" and "declination" in sub.data and "azimuth" in sub.data:
                return float(sub.data["declination"]), float(sub.data["azimuth"])
        if "declination" in entry.options and "azimuth" in entry.options:
            return float(entry.options["declination"]), float(entry.options["azimuth"])
    return None


def _defaults(hass: HomeAssistant) -> tuple[float, float]:
    plane = _forecast_solar_plane(hass)
    if plane:
        return plane
    return default_orientation(Site(hass.config.latitude, hass.config.longitude))


def _schema(hass: HomeAssistant, current: dict[str, Any], first: bool) -> vol.Schema:
    tilt, az = _defaults(hass)
    sel_sensor = lambda dc: selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class=dc))  # noqa: E731

    def opt(key: str) -> vol.Optional:
        return vol.Optional(key, description={"suggested_value": current.get(key)})

    fields: dict[Any, Any] = {}
    if first:
        fields[vol.Required(CONF_POWER, default=current.get(CONF_POWER, vol.UNDEFINED))] = sel_sensor(SensorDeviceClass.POWER)
    fields.update(
        {
            vol.Required(CONF_TILT, default=current.get(CONF_TILT, round(tilt))): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=90, step=1, unit_of_measurement="°", mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(CONF_AZIMUTH, default=current.get(CONF_AZIMUTH, round(az))): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=359, step=1, unit_of_measurement="°", mode=selector.NumberSelectorMode.BOX
                )
            ),
            opt(CONF_TEMPERATURE): sel_sensor(SensorDeviceClass.TEMPERATURE),
            opt(CONF_WEATHER): selector.EntitySelector(selector.EntitySelectorConfig(domain="weather")),
            opt(CONF_WIND_DIRECTION): sel_sensor(SensorDeviceClass.WIND_DIRECTION),
            opt(CONF_WIND_SPEED): sel_sensor(SensorDeviceClass.WIND_SPEED),
        }
    )
    if first:
        fields[vol.Required(CONF_BACKFILL, default=True)] = selector.BooleanSelector()
    return vol.Schema(fields)


def _validate(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if CONF_POWER in data:
        st = hass.states.get(data[CONF_POWER])
        if st is None:
            errors[CONF_POWER] = "entity_not_found"
        elif st.attributes.get("unit_of_measurement") not in ("W", "kW", "MW", "mW", "GW"):
            errors[CONF_POWER] = "not_power"
    return errors


class SolarSkyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pick the PV power sensor and the plane."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(self.hass, user_input)
            if not errors:
                await self.async_set_unique_id(user_input[CONF_POWER])
                self._abort_if_unique_id_configured()
                st = self.hass.states.get(user_input[CONF_POWER])
                name = st.name if st and st.name else "Solar Sky"
                data = {k: v for k, v in user_input.items() if v not in (None, "")}
                return self.async_create_entry(title=f"Solar Sky ({name})", data=data)
        return self.async_show_form(step_id="user", data_schema=_schema(self.hass, user_input or {}, True), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> SolarSkyOptionsFlow:
        return SolarSkyOptionsFlow()


class SolarSkyOptionsFlow(OptionsFlowWithReload):
    """Change the plane or the optional inputs. A new plane rebuilds the table from stored clear days."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        current = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            # optional fields left empty are removed, not kept from the original data
            opts = {k: v for k, v in user_input.items() if v not in (None, "")}
            for k in OPTIONAL:
                opts.setdefault(k, None)
            return self.async_create_entry(data=opts)
        return self.async_show_form(step_id="init", data_schema=_schema(self.hass, current, False))
