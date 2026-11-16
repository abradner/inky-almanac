"""The provider boundary: business logic sees domain data, never a provider.

Only modules under almanac/providers/ may import a concrete provider
module, and only app.py (the composition root) composes them, through the
providers package (registry) and its shared plumbing (base, cache).
"""

import ast
from pathlib import Path

import pytest

ALMANAC = Path(__file__).resolve().parents[1] / "almanac"
PROVIDERS = ALMANAC / "providers"
# What business logic may import from the providers package: the registry
# (the package itself), the interfaces, and the shared transport/cache.
ALLOWED = {"almanac.providers", "almanac.providers.base", "almanac.providers.cache"}
# Strings that only make sense inside an adapter: provider names and source
# field formats. Business logic must not branch on or build these.
PROVIDER_STRINGS = (
    "open-meteo", "openmeteo", "temperature_2m", "precipitation_probability", "weather_code",
    "rain_trace", "rainfall_24hr", "wind_spd_kmh", "gust_kmh", "air_temperature", "wmo-id",
    "commons", "wikimedia", "hazardwatch", "rfs.nsw", "bom.gov.au", "IDN", "ERA5", "ARPANSA",
    "Bureau of Meteorology",
)


def business_modules():
    return sorted(p for p in ALMANAC.glob("*.py"))


def _imports(path, root=ALMANAC.parent):
    """Fully qualified almanac.* modules a file imports (relative resolved)."""
    tree = ast.parse(path.read_text(), str(path))
    rel = path.relative_to(root).with_suffix("")
    package = list(rel.parts[:-1])  # e.g. ["almanac"] or ["almanac", "providers"]
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            out.add(mod)
            # "from . import providers" / "from .providers import openmeteo"
            out |= {f"{mod}.{a.name}" for a in node.names}
    return {m for m in out if m.startswith("almanac")}


def provider_imports(path, root=ALMANAC.parent):
    """Concrete provider modules this file imports."""
    hits = set()
    for m in _imports(path, root):
        if not m.startswith("almanac.providers."):
            continue
        module = ".".join(m.split(".")[:3])  # almanac.providers.<module>[.<name>]
        if module not in ALLOWED and (PROVIDERS / f"{module.split('.')[2]}.py").exists():
            hits.add(module)
    return hits


@pytest.mark.parametrize("path", business_modules(), ids=lambda p: p.name)
def test_business_logic_never_imports_a_provider_module(path):
    if path.name == "app.py":
        pytest.skip("app.py is the composition root")
    assert provider_imports(path) == set()


def test_the_composition_root_composes_through_the_registry():
    # app.py may name the registry and the shared plumbing, not an adapter.
    assert provider_imports(ALMANAC / "app.py") == set()


def _code_strings(path):
    """String literals in a module, docstrings excluded."""
    tree = ast.parse(path.read_text(), str(path))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docs.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


@pytest.mark.parametrize("path", [p for p in business_modules() if p.name != "config.py"], ids=lambda p: p.name)
def test_business_logic_has_no_provider_names_or_field_formats(path):
    # config.py is exempt: it names the default adapters and stations.
    found = [(s, word) for s in _code_strings(path) for word in PROVIDER_STRINGS if word.lower() in s.lower()]
    assert found == []


def test_the_probes_can_fail(tmp_path):
    """Positive controls: the checks above do catch a violation."""
    probe = tmp_path / "almanac" / "probe.py"
    probe.parent.mkdir()
    probe.write_text('from .providers import openmeteo\nfrom .providers.bom_obs import parse\n'
                     'from .providers import cache\nX = "Open-Meteo"\n')
    assert provider_imports(probe, tmp_path) == {"almanac.providers.openmeteo", "almanac.providers.bom_obs"}
    assert any("open-meteo" in s.lower() for s in _code_strings(probe))


def test_every_registered_provider_resolves_and_lives_under_providers():
    from almanac import providers

    for kind, names in providers.REGISTRY.items():
        for name in names:
            obj = providers.resolve(kind, name)
            assert obj is not None, (kind, name)
    with pytest.raises(ValueError, match="unknown forecast provider"):
        providers.resolve("forecast", "nope")
