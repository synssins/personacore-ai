"""PersonaCore reference plugin — weather over MCP stdio.

Spec 5.1 calls this plugin "living documentation": it is the worked example of
the plugin contract, so it is written to be read. Every non-obvious line here
exists because the contract or spec 7 asks for it.

What it demonstrates:

* A plugin is an ordinary MCP server. The core is just an MCP client; there is
  no PersonaCore SDK to import and nothing about this file is PersonaCore
  specific apart from where it reads its config from.
* Config lives beside the code, in this folder, and is read at startup
  (spec 5.1). The admin UI edits `config.toml`; a reload restarts the plugin.
* Everything the weather service returns is untrusted input (spec 7). Numbers
  are coerced and range-checked, the one text field is looked up in a local
  table by numeric code, and no string from the network is ever passed through
  to the persona verbatim.
* The service being down is a normal Tuesday, not an exception. The tool
  returns `available = false` and a sentence the persona can say out loud
  (spec 10). A traceback would reach the model as noise and the user as
  silence.

Run it by hand for a quick check:  python main.py
"""

from __future__ import annotations

import math
import re
import sys
import tomllib
from pathlib import Path
from typing import Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field, ValidationError

PLUGIN_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PLUGIN_DIR / "config.toml"

# The one host this plugin talks to. It is repeated in manifest.toml under
# permissions.network and the two must agree: the manifest is what the core
# enforces, this constant is what the code actually does.
API_HOST = "api.open-meteo.com"
API_URL = f"https://{API_HOST}/v1/forecast"

GEOCODE_HOST = "geocoding-api.open-meteo.com"
GEOCODE_URL = f"https://{GEOCODE_HOST}/v1/search"
NETWORK_HOSTS = (API_HOST, GEOCODE_HOST)

# "Jordan MN" finds nothing; "Jordan, Minnesota" finds it. Americans type the
# first form, so the abbreviation is expanded before asking rather than the
# person being told their own address does not exist.
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}

# Open-Meteo needs no account and no API key, which is exactly why the spec
# names it. Do not swap in a service that needs a credential — that would drag
# a secret into the reference plugin and stop it being the simple example.

MAX_RESPONSE_BYTES = 256 * 1024
"""A forecast is a few kilobytes. Anything vastly larger is a malfunction or an
attempt to make us chew through memory, so it is refused unread."""

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# WMO weather codes -> our own words. The API also returns human-readable text
# in some responses; we ignore it and look the numeric code up here instead, so
# the only English that reaches the persona is English we wrote (spec 7).
_CONDITIONS: dict[int, str] = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "heavy freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "heavy freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light showers",
    81: "showers",
    82: "violent showers",
    85: "light snow showers",
    86: "heavy snow showers",
    95: "thunderstorms",
    96: "thunderstorms with hail",
    99: "thunderstorms with heavy hail",
}


# -- configuration ---------------------------------------------------------
#
# The core does not validate config.toml: the plugin owns that shape, so the
# plugin checks it. Pydantic is used here only because it turns a typo into one
# readable sentence; plain `dict` handling would satisfy the contract too.


class ConfigError(RuntimeError):
    """config.toml is missing or does not make sense. Reported on stderr and by
    exiting non-zero, which is how a stdio plugin tells the core it cannot
    start; the message lands in the admin UI beside the plugin (spec 9)."""


class Location(BaseModel):
    label: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)

    aliases: list[str] = Field(default_factory=list)
    """Other names for this same place — a postcode, the town, a nickname.

    Added because refusing them was indefensible: a place configured as
    "home", labelled "Jordan", asked for by its own postcode, was answered
    with "I don't know that". The plugin already had the coordinates. Matching
    a name against a place the household set up is not a lookup, it is
    recognising something you were told.
    """

    def matches(self, wanted: str) -> bool:
        candidates = {self.label.casefold(), *(a.casefold() for a in self.aliases)}
        return wanted.casefold() in candidates


class WeatherConfig(BaseModel):
    default_location: str = "home"
    units: Literal["metric", "imperial"] = "metric"
    forecast_days: int = Field(default=3, ge=1, le=7)
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    locations: dict[str, Location] = Field(default_factory=dict)

    look_up_unknown_places: bool = True
    """Resolve a place nobody configured — a postcode, a town, a city and
    country — by asking the geocoding service.

    On by default, because the alternative is telling someone their own
    postcode does not exist while holding the coordinates for it. The trade is
    real and worth stating: with this on, a place name someone says can reach
    the lookup service. Turn it off and only configured locations answer."""

    @property
    def temperature_unit(self) -> str:
        return "°C" if self.units == "metric" else "°F"


def load_config(path: Path = CONFIG_PATH) -> WeatherConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{path.name} is missing — the plugin needs its own settings") from None
    except OSError as exc:
        raise ConfigError(f"{path.name} could not be read — {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name} is not valid TOML — {exc}") from None

    try:
        config = WeatherConfig.model_validate(raw.get("weather", {}))
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "weather"
        raise ConfigError(f"{path.name}: '{where}' {first['msg']}") from None

    if not config.locations:
        raise ConfigError(
            f"{path.name}: no locations are set up — add a [weather.locations.<name>] block"
        )
    if config.default_location not in config.locations:
        known = ", ".join(sorted(config.locations)) or "none"
        raise ConfigError(
            f"{path.name}: default_location {config.default_location!r} is not one of the "
            f"locations set up here ({known})"
        )
    return config


# -- what the tool gives back ----------------------------------------------


class DayForecast(BaseModel):
    date: str
    conditions: str
    high: float | None = None
    low: float | None = None
    precipitation_chance: int | None = None


class LocationMatch(BaseModel):
    """One candidate place, for the settings-page picker — ADR-0016.

    Deliberately the three things a `[weather.locations.<name>]` block needs and
    nothing else. The admin form's schema maps these keys onto its fields
    (`"fill": {"label": "label", "latitude": "latitude", …}`), so anything extra
    here would be data the core carries around and never uses.
    """

    label: str
    latitude: float
    longitude: float


class LocationSearchResult(BaseModel):
    """What `search_locations` gives back.

    Same shape as every other tool here: `available` says whether the answer is
    real, `summary` is a sentence, and the structured part is separate. The
    admin form reads `results`; a persona would read `summary`.
    """

    available: bool
    query: str
    summary: str
    results: list[LocationMatch] = Field(default_factory=list)


class ForecastResult(BaseModel):
    """Structured for the agent, `summary` for the persona's mouth.

    `available` is the honest bit: false means we have no forecast, and the
    agent can see that without parsing prose.
    """

    available: bool
    location: str
    units: str
    summary: str
    days: list[DayForecast] = Field(default_factory=list)


# -- talking to the service ------------------------------------------------


def new_client(timeout: float) -> httpx.AsyncClient:
    """One place that builds the HTTP client, so tests can swap the transport
    and so the settings below are stated once.

    `follow_redirects=False` is deliberate: the manifest allowlists exactly one
    host, and silently following a redirect somewhere else would make that
    declaration a lie.
    """
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False)


GARBLED = "The weather service sent back something I couldn't make sense of."
"""One sentence, reused: a reply we cannot parse and a reply that is absurdly
large are the same thing from where the listener is standing."""


class ServiceUnavailable(RuntimeError):
    """The forecast could not be fetched. Carries the sentence the persona says
    — no exception class names, no URLs, no stack."""



def expand_us_state(query: str) -> str | None:
    """"Jordan MN" -> "Jordan, Minnesota", or None if it does not look like one."""
    text = query.strip().rstrip(",")
    for separator in (",", " "):
        head, _, tail = text.rpartition(separator)
        code = tail.strip().upper()
        if head.strip() and code in US_STATES:
            return f"{head.strip().rstrip(',')}, {US_STATES[code]}"
    return None


async def ask_geocoder(config: WeatherConfig, name: str) -> list[dict[str, Any]]:
    """One call to the place-lookup service. Raises `ServiceUnavailable` only.

    Module level rather than nested inside `geocode` because two callers need
    it now — resolving a place someone spoke, and the admin's `search_locations`
    picker (ADR-0016). One function means one place where the timeout, the
    status check and the size limit live, and no chance of the picker quietly
    getting laxer rules than the runtime path.
    """
    params = {"name": name, "count": 5, "language": "en", "format": "json"}
    try:
        async with new_client(config.timeout_seconds) as client:
            response = await client.get(GEOCODE_URL, params=params)
    except httpx.HTTPError:
        raise ServiceUnavailable("I can't reach the place-lookup service right now.") from None
    if response.status_code != httpx.codes.OK:
        raise ServiceUnavailable(
            f"The place-lookup service turned me away (error {response.status_code})."
        )
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ServiceUnavailable("The place-lookup service sent something I can't read.")
    try:
        payload = response.json()
    except ValueError:
        raise ServiceUnavailable(
            "The place-lookup service sent something I can't read."
        ) from None
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    return [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []


def as_location(raw: dict[str, Any], fallback: str) -> Location | None:
    """One result from the service as a `Location`, or None.

    Untrusted input (spec 7): it came off the network. Nothing is executed, the
    coordinates are coerced and then bounded by the `Location` model itself, and
    the label is assembled from at most three fields and truncated.
    """
    try:
        latitude = float(raw["latitude"])
        longitude = float(raw["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    parts = [str(raw.get("name") or fallback).strip()]
    for key in ("admin1", "country"):
        value = raw.get(key)
        if value and str(value).strip() and str(value).strip() not in parts:
            parts.append(str(value).strip())
    try:
        return Location(label=", ".join(parts)[:120], latitude=latitude, longitude=longitude)
    except ValidationError:
        return None


async def search_places(config: WeatherConfig, query: str, limit: int = 5) -> list[Location]:
    """Every place matching a name or postcode, best first.

    Raises `ServiceUnavailable` if the lookup service itself is unreachable,
    so "I could not reach the service" is never reported as "no such place".
    """
    results = await ask_geocoder(config, query)
    if not results:
        expanded = expand_us_state(query)
        if expanded:
            results = await ask_geocoder(config, expanded)
    found = [as_location(raw, query) for raw in results[:limit]]
    return [place for place in found if place is not None]


async def geocode(config: WeatherConfig, query: str) -> Location | None:
    """Resolve a place name or postcode to coordinates, or None.

    The single best match, which is all the runtime path can use: nobody
    speaking to an assistant wants to be read a list of five towns.
    """
    places = await search_places(config, query, limit=1)
    return places[0] if places else None


async def fetch_forecast(config: WeatherConfig, location: Location) -> dict[str, Any]:
    """Fetch raw daily data. Raises `ServiceUnavailable` and nothing else."""
    params = {
        "latitude": location.latitude,
        "longitude": location.longitude,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "forecast_days": config.forecast_days,
        "timezone": "auto",
        "temperature_unit": "celsius" if config.units == "metric" else "fahrenheit",
        "precipitation_unit": "mm" if config.units == "metric" else "inch",
    }
    try:
        async with new_client(config.timeout_seconds) as client:
            response = await client.get(API_URL, params=params)
    except httpx.HTTPError:
        # Covers connect failures, DNS, timeouts, TLS, protocol errors. The
        # detail goes to our own log, never to the persona.
        raise ServiceUnavailable("I can't reach the weather service right now.") from None

    if response.status_code != httpx.codes.OK:
        raise ServiceUnavailable(
            f"The weather service turned me away (error {response.status_code})."
        )
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ServiceUnavailable(GARBLED)
    try:
        payload = response.json()
    except ValueError:
        raise ServiceUnavailable(GARBLED) from None
    if not isinstance(payload, dict):
        raise ServiceUnavailable(GARBLED)
    return payload


# -- untrusted-input handling ----------------------------------------------
#
# Everything below assumes the payload is hostile until proved otherwise: wrong
# types, missing keys, ragged arrays, absurd values, unexpected strings. None of
# it should be able to do more than shorten the forecast.


def _column(payload: dict[str, Any], key: str) -> list[Any]:
    daily = payload.get("daily")
    if not isinstance(daily, dict):
        return []
    values = daily.get(key)
    return values if isinstance(values, list) else []


def _number(values: list[Any], index: int, *, low: float, high: float) -> float | None:
    """A finite, in-range number at `index`, or None. Booleans are ints in
    Python, so they are excluded explicitly; strings are not coerced."""
    if index >= len(values):
        return None
    value = values[index]
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number) or not low <= number <= high:
        return None
    return round(number, 1)


def parse_days(payload: dict[str, Any], limit: int) -> list[DayForecast]:
    dates = _column(payload, "time")
    codes = _column(payload, "weather_code")
    highs = _column(payload, "temperature_2m_max")
    lows = _column(payload, "temperature_2m_min")
    rain = _column(payload, "precipitation_probability_max")

    days: list[DayForecast] = []
    for index, raw_date in enumerate(dates[:limit]):
        # The date is the only string we pass on, and only in this exact shape.
        if not isinstance(raw_date, str) or not _ISO_DATE.match(raw_date):
            continue
        code = _number(codes, index, low=0, high=99)
        chance = _number(rain, index, low=0, high=100)
        # An unknown code says "unsettled" rather than repeating the number at
        # the user: a code we do not recognise is a code we cannot describe.
        conditions = _CONDITIONS.get(int(code), "unsettled") if code is not None else "unsettled"
        days.append(
            DayForecast(
                date=raw_date,
                conditions=conditions,
                high=_number(highs, index, low=-150, high=150),
                low=_number(lows, index, low=-150, high=150),
                precipitation_chance=int(chance) if chance is not None else None,
            )
        )
    return days


def summarise(label: str, days: list[DayForecast], unit: str) -> str:
    """One sentence a persona can read out without editing."""
    if not days:
        return f"I got a reply about {label}, but no usable forecast in it."
    today = days[0]
    parts = [f"{label}: {today.conditions} today"]
    if today.high is not None and today.low is not None:
        parts.append(f"between {today.low}{unit} and {today.high}{unit}")
    elif today.high is not None:
        parts.append(f"up to {today.high}{unit}")
    if today.precipitation_chance is not None:
        parts.append(f"with a {today.precipitation_chance}% chance of rain")
    sentence = ", ".join(parts) + "."
    if len(days) > 1:
        following = "; ".join(f"{day.date} {day.conditions}" for day in days[1:])
        sentence += f" Then {following}."
    return sentence


# -- the MCP server --------------------------------------------------------


def build_server(config: WeatherConfig) -> MCPServer:
    """Build the server. Split out from `main()` so tests can hold one without
    starting a subprocess — worth copying into your own plugin.

    Every tool registered here must appear in manifest.toml under `[tools.…]`.
    The core will not call a tool it has no declared risk level for, so an
    undeclared tool is invisible rather than dangerously default-safe.
    """
    server = MCPServer(
        name="weather",
        version="1.0.0",
        instructions="Weather for locations the household has set up.",
    )

    @server.tool(
        name="get_forecast",
        description=(
            "Current conditions and a short forecast for anywhere. Pass whatever "
            "place the person named — a postcode, a town, a city and country, or "
            "a nickname the household configured. Omit `location` for the default."
        ),
    )
    async def get_forecast(location: str | None = None) -> ForecastResult:
        # The argument arrives from the model, which got it from a person
        # talking. It is untrusted, so it is not a lookup key until it matches
        # one: no free text ever reaches the network from here.
        asked = (location or config.default_location).strip()
        known = ", ".join(sorted(config.locations))

        # Configured places first: no network, and the household's own names
        # ("home", "the cabin") beat anything a gazetteer thinks they mean.
        place = config.locations.get(asked.casefold())
        if place is None:
            place = next((p for p in config.locations.values() if p.matches(asked)), None)

        # Then look it up. A postcode, a town, a city and country — whatever was
        # said. Refusing this while holding the coordinates for the very place
        # asked about is the behaviour that made the last assistant unbearable.
        if place is None and config.look_up_unknown_places:
            try:
                place = await geocode(config, asked)
            except ServiceUnavailable as exc:
                return ForecastResult(
                    available=False, location=asked, units=config.units, summary=str(exc)
                )

        if place is None:
            hint = (
                f" I know about: {known}."
                if not config.look_up_unknown_places
                else " Try a town and its state or country."
            )
            return ForecastResult(
                available=False,
                location=asked,
                units=config.units,
                summary=f"I couldn't find anywhere called {asked!r}.{hint}",
            )

        try:
            payload = await fetch_forecast(config, place)
        except ServiceUnavailable as exc:
            # Not re-raised: an outage is an outcome, not a crash. The persona
            # gets a sentence, the agent gets available=false (spec 10).
            return ForecastResult(
                available=False,
                location=place.label,
                units=config.units,
                summary=str(exc),
            )

        days = parse_days(payload, config.forecast_days)
        summary = summarise(place.label, days, config.temperature_unit)
        return ForecastResult(
            available=bool(days),
            location=place.label,
            units=config.units,
            summary=summary,
            days=days,
        )

    @server.tool(
        name="search_locations",
        description=(
            "Find the coordinates of a place by name, so a location can be set up. "
            "Pass a town, a postcode, or a town with its state or country; returns "
            "the matches with their coordinates."
        ),
    )
    async def search_locations(query: str) -> LocationSearchResult:
        # This is the tool config.schema.json nominates for the `locations`
        # setting (ADR-0016). It exists for one deliberate act by the person who
        # owns the system, on a string they typed into a form themselves — which
        # is why it does not consult `look_up_unknown_places`. That setting is
        # about what happens to something *a person said near a microphone*, and
        # refusing an admin their own settings page protects nobody.
        asked = query.strip()
        if not asked:
            return LocationSearchResult(
                available=False,
                query=asked,
                summary="Say which place to look for and I'll find its coordinates.",
            )
        try:
            places = await search_places(config, asked)
        except ServiceUnavailable as exc:
            # An outage is an outcome, not a crash (spec 10) — same as the
            # forecast path, so a settings page gets a sentence rather than a
            # protocol error it has to guess the meaning of.
            return LocationSearchResult(available=False, query=asked, summary=str(exc))
        if not places:
            return LocationSearchResult(
                available=True,
                query=asked,
                summary=(
                    f"I couldn't find anywhere called {asked!r}. Try a town with its "
                    "state or country."
                ),
            )
        return LocationSearchResult(
            available=True,
            query=asked,
            summary=f"{len(places)} place(s) match {asked!r}.",
            results=[
                LocationMatch(
                    label=place.label, latitude=place.latitude, longitude=place.longitude
                )
                for place in places
            ],
        )

    return server


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        # stdout belongs to the MCP protocol on a stdio plugin. Diagnostics go
        # to stderr or they corrupt the conversation.
        print(f"weather plugin cannot start: {exc}", file=sys.stderr)
        return 1
    build_server(config).run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
