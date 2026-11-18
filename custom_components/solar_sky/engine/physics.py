"""Clear-sky physics for a fixed PV plane, numpy only.

These are re-implementations of the few pvlib pieces the model needs:

- sun position: NOAA solar calculator equations (about 0.01 deg against SPA for current dates)
- clear-sky irradiance: Ineichen-Perez with Kasten-Young air mass and Spencer extraterrestrial irradiance
- plane of array: isotropic sky diffuse plus ground-reflected (albedo 0.25)
- glass reflection: the "physical" incidence angle modifier (Fresnel + absorption), applied to the beam only
- cell temperature: Sandia (SAPM) model, close-mount glass/glass

The result is a relative clear-sky power shape. Its absolute scale, and every site-specific deviation
from it (shading, soiling, the true Linke turbidity), is learned from data by the engine.

All functions take arrays of UNIX timestamps (seconds, UTC) and return float64 arrays.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

DEG = np.pi / 180.0


@dataclass(frozen=True, slots=True)
class Site:
    """Where the array is."""

    latitude: float
    longitude: float
    elevation_m: float = 0.0


@dataclass(frozen=True, slots=True)
class SunPosition:
    """Sun position arrays. Angles in degrees; azimuth clockwise from north."""

    elevation: np.ndarray  # apparent (refraction corrected)
    azimuth: np.ndarray
    zenith: np.ndarray  # apparent
    solar_noon: np.ndarray  # UNIX seconds of the nearest local solar noon


def _julian_century(ts: np.ndarray) -> np.ndarray:
    return (ts / 86400.0 + 2440587.5 - 2451545.0) / 36525.0


def _sun_terms(jc: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return (declination [rad], equation of time [minutes])."""
    l0 = np.mod(280.46646 + jc * (36000.76983 + jc * 0.0003032), 360.0)
    m = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    ecc = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    mr = m * DEG
    c = (
        np.sin(mr) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
        + np.sin(2 * mr) * (0.019993 - 0.000101 * jc)
        + np.sin(3 * mr) * 0.000289
    )
    omega = (125.04 - 1934.136 * jc) * DEG
    app_long = (l0 + c - 0.00569 - 0.00478 * np.sin(omega)) * DEG
    mean_obliq = 23.0 + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0) / 60.0
    obliq = (mean_obliq + 0.00256 * np.cos(omega)) * DEG
    decl = np.arcsin(np.sin(obliq) * np.sin(app_long))
    y = np.tan(obliq / 2) ** 2
    l0r = l0 * DEG
    eqt = (
        4.0
        / DEG
        * (
            y * np.sin(2 * l0r)
            - 2 * ecc * np.sin(mr)
            + 4 * ecc * y * np.sin(mr) * np.cos(2 * l0r)
            - 0.5 * y * y * np.sin(4 * l0r)
            - 1.25 * ecc * ecc * np.sin(2 * mr)
        )
    )
    return decl, eqt


def _refraction(el: np.ndarray) -> np.ndarray:
    """NOAA atmospheric refraction correction, degrees."""
    out = np.zeros_like(el)
    t = np.tan(np.clip(el, -89.0, 89.0) * DEG)
    a = (el > 5) & (el <= 85)
    out[a] = 58.1 / t[a] - 0.07 / t[a] ** 3 + 0.000086 / t[a] ** 5
    b = (el > -0.575) & (el <= 5)
    e = el[b]
    out[b] = 1735 + e * (-518.2 + e * (103.4 + e * (-12.79 + e * 0.711)))
    c = el <= -0.575
    out[c] = -20.772 / t[c]
    return out / 3600.0


def sun_position(ts: np.ndarray, site: Site) -> SunPosition:
    """Apparent sun position for UNIX timestamps."""
    ts = np.asarray(ts, dtype=float)
    decl, eqt = _sun_terms(_julian_century(ts))
    minutes = np.mod(ts, 86400.0) / 60.0
    tst = np.mod(minutes + eqt + 4.0 * site.longitude, 1440.0)
    ha = (tst / 4.0 - 180.0) * DEG
    lat = site.latitude * DEG
    cosz = np.clip(np.sin(lat) * np.sin(decl) + np.cos(lat) * np.cos(decl) * np.cos(ha), -1.0, 1.0)
    zen = np.arccos(cosz)
    with np.errstate(invalid="ignore", divide="ignore"):
        cos_az = (np.sin(lat) * cosz - np.sin(decl)) / (np.cos(lat) * np.sin(zen))
    az0 = np.arccos(np.clip(np.nan_to_num(cos_az), -1.0, 1.0)) / DEG
    az = np.where(ha > 0, np.mod(az0 + 180.0, 360.0), np.mod(540.0 - az0, 360.0))
    el_true = 90.0 - zen / DEG
    el = el_true + _refraction(el_true)
    noon = ts - np.mod(ts, 86400.0) + (720.0 - 4.0 * site.longitude - eqt) * 60.0
    noon += 86400.0 * np.round((ts - noon) / 86400.0)  # the nearest noon, not the one on the UTC date
    return SunPosition(elevation=el, azimuth=az, zenith=90.0 - el, solar_noon=noon)


def _day_of_year(ts: np.ndarray) -> np.ndarray:
    days = np.floor(ts / 86400.0).astype("int64")
    years = days.astype("datetime64[D]").astype("datetime64[Y]")
    return (days - years.astype("datetime64[D]").astype("int64") + 1).astype(float)


def clear_sky(ts: np.ndarray, sun: SunPosition, site: Site, linke: float = 3.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Ineichen-Perez clear-sky (GHI, DNI, DHI) in W/m2."""
    zen = sun.zenith
    cosz = np.cos(zen * DEG)
    doy = _day_of_year(np.asarray(ts, dtype=float))
    b = 2 * np.pi * doy / 365.0
    dni_extra = 1367.0 * (
        1.00011 + 0.034221 * np.cos(b) + 0.00128 * np.sin(b) + 0.000719 * np.cos(2 * b) + 0.000077 * np.sin(2 * b)
    )
    up = zen < 90.0
    zc = np.where(up, zen, 89.9)
    am_rel = 1.0 / (np.cos(zc * DEG) + 0.50572 * (6.07995 + 90.0 - zc) ** -1.6364)
    pressure = 101325.0 * (1 - 2.25577e-5 * site.elevation_m) ** 5.25588
    am = am_rel * pressure / 101325.0
    alt = site.elevation_m
    fh1, fh2 = np.exp(-alt / 8000.0), np.exp(-alt / 1250.0)
    cg1, cg2 = 5.09e-5 * alt + 0.868, 3.92e-5 * alt + 0.0387
    tl = linke
    ghi = cg1 * dni_extra * cosz * np.fmax(np.exp(-cg2 * am * (fh1 + fh2 * (tl - 1))), 0)
    bb = 0.664 + 0.163 / fh1
    bnci = dni_extra * np.fmax(bb * np.exp(-0.09 * am * (tl - 1)), 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        bnci2 = ghi * np.clip((1 - (0.1 - 0.2 * np.exp(-tl)) / (0.1 + 0.882 / fh1)) / cosz, 0, 1e20)
    dni = np.fmin(bnci, bnci2)
    dhi = ghi - dni * cosz
    zero = ~up
    for a in (ghi, dni, dhi):
        a[zero] = 0.0
    return ghi, dni, dhi


def angle_of_incidence(sun: SunPosition, tilt: float, azimuth: float) -> np.ndarray:
    """Angle between the sun and the panel normal, degrees."""
    z = sun.zenith * DEG
    c = np.cos(z) * np.cos(tilt * DEG) + np.sin(z) * np.sin(tilt * DEG) * np.cos((sun.azimuth - azimuth) * DEG)
    return np.arccos(np.clip(c, -1.0, 1.0)) / DEG


def iam_physical(aoi: np.ndarray, n: float = 1.526, k: float = 4.0, thickness: float = 0.002) -> np.ndarray:
    """Fresnel reflection + glass absorption, relative to normal incidence."""
    cos1 = np.clip(np.cos(aoi * DEG), 1e-6, 1.0)
    sin1 = np.sqrt(1 - cos1**2)
    sin2 = sin1 / n
    cos2 = np.sqrt(1 - sin2**2)
    rs = ((cos1 - n * cos2) / (cos1 + n * cos2)) ** 2
    rp = ((cos2 - n * cos1) / (cos2 + n * cos1)) ** 2
    r0 = ((1 - n) / (1 + n)) ** 2
    att = np.exp(-k * thickness / cos2)
    iam = ((1 - rs) * att + (1 - rp) * att) / 2 / ((1 - r0) * np.exp(-k * thickness))
    return np.where(aoi < 90.0, iam, 0.0)


def plane_power_shape(
    ts: np.ndarray,
    site: Site,
    tilt: float,
    azimuth: float,
    air_temp_c: np.ndarray | float | None = None,
    sun: SunPosition | None = None,
    linke: float = 3.0,
) -> tuple[np.ndarray, np.ndarray, SunPosition]:
    """Clear-sky effective irradiance on the plane, temperature corrected (W/m2).

    Returns (model, aoi, sun). Measured power / model is a capacity scale in W per W/m2.
    """
    ts = np.asarray(ts, dtype=float)
    if sun is None:
        sun = sun_position(ts, site)
    ghi, dni, dhi = clear_sky(ts, sun, site, linke)
    aoi = angle_of_incidence(sun, tilt, azimuth)
    ct = np.cos(tilt * DEG)
    direct = np.fmax(dni * np.cos(aoi * DEG), 0.0) * iam_physical(aoi)
    diffuse = dhi * (1 + ct) / 2 + ghi * 0.25 * (1 - ct) / 2
    poa = np.fmax(direct + diffuse, 0.0)
    t_air = 20.0 if air_temp_c is None else np.nan_to_num(np.asarray(air_temp_c, dtype=float), nan=20.0)
    t_cell = poa * np.exp(-2.98 - 0.0471 * 1.0) + t_air + poa / 1000.0 * 1.0
    model = poa * (1 - 0.0047 * (t_cell - 25.0))
    return model, aoi, sun
