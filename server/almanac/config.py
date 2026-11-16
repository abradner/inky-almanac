"""Runtime settings, all overridable from the environment."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name, default):
    return os.environ.get(name, default)


def _list(name, default):
    return tuple(a.strip() for a in _env(name, default).split(",") if a.strip())


@dataclass(frozen=True)
class Config:
    place: str = field(default_factory=lambda: _env("ALMANAC_PLACE", "Sydney"))
    latitude: float = field(default_factory=lambda: float(_env("ALMANAC_LAT", "-33.87")))
    longitude: float = field(default_factory=lambda: float(_env("ALMANAC_LON", "151.21")))
    timezone: str = field(default_factory=lambda: _env("ALMANAC_TZ", "Australia/Sydney"))
    # Refreshes happen at HH:01 for every hour in [first_hour, last_hour].
    first_hour: int = field(default_factory=lambda: int(_env("ALMANAC_FIRST_HOUR", "6")))
    last_hour: int = field(default_factory=lambda: int(_env("ALMANAC_LAST_HOUR", "22")))
    # From this hour on, the weather panel shows tomorrow.
    tomorrow_from_hour: int = field(default_factory=lambda: int(_env("ALMANAC_TOMORROW_FROM", "18")))
    # Before this hour the header leads with the overnight low; from it until
    # tomorrow_from_hour, with today's high.
    high_leads_from: int = field(default_factory=lambda: int(_env("ALMANAC_HIGH_LEADS_FROM", "9")))
    cache_dir: Path = field(default_factory=lambda: Path(_env("ALMANAC_CACHE", "/var/cache/almanac")))
    port: int = field(default_factory=lambda: int(_env("ALMANAC_PORT", "8077")))
    # NSW RFS fire district for the fire danger badge (empty = off).
    fire_district: str = field(default_factory=lambda: _env("ALMANAC_FIRE_DISTRICT", "Greater Sydney Region"))
    # BOM town forecast (IDN11060 area name) used for the daily numbers.
    bom_town: str = field(default_factory=lambda: _env("ALMANAC_BOM_TOWN", "Sydney"))
    # Hazards: RFS/SES warnings within this many km of home get a badge.
    hazard_radius_km: float = field(default_factory=lambda: float(_env("ALMANAC_HAZARD_RADIUS_KM", "30")))
    # BOM warning titles must name one of these areas (comma separated)...
    bom_warning_areas: tuple = field(
        default_factory=lambda: tuple(
            a.strip() for a in _env("ALMANAC_BOM_AREAS", "Sydney Metropolitan,Metropolitan,Greater Sydney,Hawkesbury,Nepean,Parramatta River").split(",") if a.strip()
        )
    )
    # ...and none of these (marine and surf warnings don't matter inland).
    bom_warning_ignore: tuple = field(
        default_factory=lambda: tuple(
            a.strip() for a in _env("ALMANAC_BOM_IGNORE", "Surf,Marine,Coastal Waters,Ocean,Sheep Graziers").split(",") if a.strip()
        )
    )
    # Providers: which adapter supplies each kind of data ("none" turns a
    # kind off). See almanac/providers/__init__.py for the registered names.
    forecast_provider: str = field(default_factory=lambda: _env("ALMANAC_FORECAST", "openmeteo"))
    outlook_provider: str = field(default_factory=lambda: _env("ALMANAC_OUTLOOK", "bom"))
    observations_provider: str = field(default_factory=lambda: _env("ALMANAC_OBSERVATIONS", "bom"))
    hazard_providers: tuple = field(default_factory=lambda: _list("ALMANAC_HAZARDS", "rfs,hazardwatch,bom"))
    warning_map_provider: str = field(default_factory=lambda: _env("ALMANAC_WARNING_MAPS", "bom"))
    fire_danger_provider: str = field(default_factory=lambda: _env("ALMANAC_FIRE_DANGER", "rfs"))
    air_quality_provider: str = field(default_factory=lambda: _env("ALMANAC_AIR_QUALITY", "openmeteo"))
    photo_provider: str = field(default_factory=lambda: _env("ALMANAC_PHOTOS", "commons"))
    normals_provider: str = field(default_factory=lambda: _env("ALMANAC_NORMALS", "era5"))
    # Rain radar for radar mode (button C). The product is BOM's IDR<site><range>:
    # IDR714 is Sydney (Terrey Hills) at its 64 km range.
    radar_provider: str = field(default_factory=lambda: _env("ALMANAC_RADAR", "bom"))
    radar_product: str = field(default_factory=lambda: _env("ALMANAC_RADAR_PRODUCT", "IDR714"))
    # Observations (BOM station WMO ids, from the state's 10-minute feed):
    # the station that describes home, stand-ins for when it is silent, and
    # coastal stations a southerly change reaches first.
    obs_station: str = field(default_factory=lambda: _env("ALMANAC_OBS_STATION", "94760"))  # Horsley Park
    # Sydney Olympic Park, Bankstown
    obs_crosscheck: tuple = field(default_factory=lambda: _list("ALMANAC_OBS_CROSSCHECK", "95765,94765"))
    # Kurnell, Sydney Airport, Fort Denison, Observatory Hill
    obs_leading: tuple = field(default_factory=lambda: _list("ALMANAC_OBS_LEADING", "95756,94767,94769,94768"))
    # The state's observations product (IDN60920 is New South Wales).
    obs_product: str = field(default_factory=lambda: _env("ALMANAC_OBS_PRODUCT", "IDN60920"))
    # USB mode: serial device of a frame plugged into this machine (empty = off).
    usb_device: str = field(default_factory=lambda: _env("ALMANAC_USB_DEVICE", ""))
    # Wikimedia asks API clients for a descriptive User-Agent with a contact URL.
    user_agent: str = field(
        default_factory=lambda: _env("ALMANAC_USER_AGENT", "inky-almanac/1.0 (https://github.com/abradner/inky-almanac; personal e-ink frame)")
    )
