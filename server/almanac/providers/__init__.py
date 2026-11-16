"""External data providers, selected by config.

Each kind of data (see base.py for the interfaces) has a registry of
adapters by name; the ALMANAC_* settings in config.py pick one (or, for
hazards, several; "none" turns a kind off). `build(cfg, get_text)` returns
the selected adapters as one `Providers` bundle. This package is the only
place that names concrete provider modules; everything outside it sees
domain data (domain.py, hazards.Hazard), and only app.py composes.

To add one: write `providers/<name>.py` with a class satisfying the
interface, add it to REGISTRY below, and point the setting at it.
"""

import importlib
from dataclasses import dataclass, field

# kind -> {setting value: "module:Class"}. Imported lazily, so a fork can
# drop a provider (and its dependencies) without touching the rest.
REGISTRY = {
    "forecast": {"openmeteo": "openmeteo:OpenMeteoForecast"},
    "outlook": {"bom": "bom_precis:BomOutlook"},
    "observations": {"bom": "bom_obs:BomObservations"},
    "hazards": {
        "rfs": "rfs_incidents:RfsIncidents",
        "hazardwatch": "hazardwatch:HazardWatch",
        "bom": "bom_warnings:BomWarnings",
    },
    "warning_maps": {"bom": "bom_warnings:BomWarningMaps"},
    "fire_danger": {"rfs": "rfs_fire_danger:RfsFireDanger"},
    "air_quality": {"openmeteo": "openmeteo_air:OpenMeteoAir"},
    "photos": {"commons": "commons:CommonsPhotos"},
    "radar": {"bom": "bom_radar:BomRadar"},
    "normals": {"era5": "climate:NORMALS", "placeholder": "..normals:PLACEHOLDER"},
}

NONE = "none"


def resolve(kind, name):
    """The class (or object) registered as `name` for `kind`."""
    try:
        target = REGISTRY[kind][name]
    except KeyError:
        known = ", ".join(sorted(REGISTRY.get(kind, {}))) or "nothing"
        raise ValueError(f"unknown {kind} provider {name!r} (known: {known}, or {NONE!r})") from None
    module, attr = target.split(":")
    if module.startswith(".."):
        mod = importlib.import_module(module[1:], package=__package__.rpartition(".")[0])
    else:
        mod = importlib.import_module("." + module, package=__package__)
    return getattr(mod, attr)


def _one(kind, name, cfg, get_text):
    if not name or name == NONE:
        return None
    return resolve(kind, name)(cfg, get_text=get_text)


@dataclass
class Providers:
    forecast: object = None
    outlook: object = None
    observations: object = None
    hazards: list = field(default_factory=list)
    warning_maps: object = None
    fire_danger: object = None
    air_quality: object = None
    photos: object = None
    radar: object = None


def build(cfg, get_text=None):
    """The adapters cfg selects. Pass providers.cache.offline as get_text for
    the request path (cache only)."""
    return Providers(
        forecast=_one("forecast", cfg.forecast_provider, cfg, get_text),
        outlook=_one("outlook", cfg.outlook_provider, cfg, get_text),
        observations=_one("observations", cfg.observations_provider, cfg, get_text),
        hazards=[_one("hazards", n, cfg, get_text) for n in cfg.hazard_providers if n != NONE],
        warning_maps=_one("warning_maps", cfg.warning_map_provider, cfg, get_text),
        fire_danger=_one("fire_danger", cfg.fire_danger_provider, cfg, get_text),
        air_quality=_one("air_quality", cfg.air_quality_provider, cfg, get_text),
        photos=_one("photos", cfg.photo_provider, cfg, get_text),
        radar=_one("radar", cfg.radar_provider, cfg, get_text),
    )


def normals(name):
    """The normals object (normals.Normals) registered as `name`."""
    return resolve("normals", name)
