#!/usr/bin/env python3
"""MLB Daily Pitch Environment V54.

Windows-local MLB collector. This script creates one self-contained run directory
and exchanges state with later stages only through JSON and CSV artifacts. It
never launches another Python script or subprocess.

V54 corrections:
- Active Spin is acquired per scheduled MLBAM pitcher from Baseball Savant's
  player-scoped serverVals.spinAxis payload. No aggregate leaderboard is used.
- America/New_York governs ET display, including EDT/EST and UTC offset.
- Open-Meteo wind_direction_10m is meteorological wind FROM; vectors use the
  opposite wind TO bearing before park-relative resolution.
- Retractable roof votes are evaluated only in a verified game context and use
  the source-specific rules supplied for SportspredictApp, WeatherMLB,
  RotoWire, and dedicated roof-status pages.
- run_status.json and run_manifest.json are written before exit. Any FATAL
  finding produces status FAIL and process exit code 1.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import math
import re
import shutil
import sys
import webbrowser
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from pandas.errors import EmptyDataError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

VERSION = "54.0.0"
MLB_API = "https://statsapi.mlb.com/api/v1"
SAVANT_CSV = "https://baseballsavant.mlb.com/statcast_search/csv"
SAVANT_PLAYER_URL = "https://baseballsavant.mlb.com/savant-player"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ET = ZoneInfo("America/New_York")
R_D = 287.05
R_V = 461.495
G = 9.80665
RHO_REF = 1.2041
LOG = logging.getLogger("mlb_pitch_environment_v54")

OUTDOOR_CLASSES = {"OUTDOOR"}
FIXED_CLASSES = {"FIXEDENCLOSED", "FIXED_ENCLOSED", "FIXED ENCLOSED"}
RETRACTABLE_CLASSES = {"RETRACTABLE"}

BRANDING = {
    "Daikin Park": "Minute Maid Park",
    "UNIQLO Field at Dodger Stadium": "Dodger Stadium",
    "Rate Field": "Guaranteed Rate Field",
    "loanDepot Park": "loanDepot park",
    "T Mobile Park": "T-Mobile Park",
    "TMobile Park": "T-Mobile Park",
}
PITCH_CODE_TO_NAME = {
    "FF": "4-Seam Fastball", "SI": "Sinker", "FC": "Cutter", "SL": "Slider",
    "ST": "Sweeper", "CU": "Curveball", "KC": "Knuckle Curve", "CH": "Changeup",
    "FS": "Splitter", "FO": "Forkball", "SC": "Screwball", "KN": "Knuckleball",
    "EP": "Eephus", "PO": "Pitch Out", "FA": "Fastball", "SV": "Slurve", "CS": "Slow Curve",
}
MASCOT_ALIASES = {
    "diamondbacks": ["diamondbacks", "dbacks", "d backs"],
    "red sox": ["red sox", "redsox"],
    "white sox": ["white sox", "whitesox"],
    "blue jays": ["blue jays", "bluejays", "jays"],
    "athletics": ["athletics"],
}
GENERIC_ROOF_SOURCES = {
    "sportspredictapp": ("https://sportspredictapp.com/mlb/weather", "SPORTSPREDICT"),
    "weathermlb": ("https://weathermlb.com/", "WEATHERMLB"),
    "rotowire": ("https://www.rotowire.com/baseball/weather.php", "ROTOWIRE"),
}
DEDICATED_ROOF_SOURCES = {
    "Rogers Centre": "https://isthedomeopen.com/",
    "American Family Field": "https://istheroofopen.com/american-family-field/",
    "Chase Field": "https://istheroofopen.com/chase-field/",
    "Globe Life Field": "https://istheroofopen.com/globe-life-field/",
    "Minute Maid Park": "https://istheroofopen.com/minute-maid-park/",
    "loanDepot park": "https://istheroofopen.com/loandepot-park/",
    "T-Mobile Park": "https://istheroofopen.com/t-mobile-park/",
}
OPEN_ROOF_RX = re.compile(
    r"\b(?:roof\s+(?:is\s+)?open|open\s+roof|likely\s+(?:keep\s+)?open|"
    r"will\s+keep\s+(?:the\s+)?roof\s+open|favor(?:s)?\s+an?\s+open\s+roof)\b", re.I
)
CLOSED_ROOF_RX = re.compile(
    r"\b(?:roof\s+(?:is\s+)?closed|closed\s+roof|likely\s+closed|"
    r"will\s+keep\s+(?:the\s+)?roof\s+closed|keep(?:ing)?\s+(?:the\s+)?roof\s+closed)\b", re.I
)
WEATHER_ANCHOR_RX = re.compile(
    r"\b(?:temp(?:erature)?|wind|humidity|dew\s*point|mph|km/?h|degrees?)\b|°", re.I
)


@dataclass
class Finding:
    severity: str
    code: str
    entity: str
    message: str
    action: str


class Health:
    COLUMNS = ["severity", "code", "entity", "message", "action"]

    def __init__(self) -> None:
        self.rows: list[Finding] = []

    def add(self, severity: str, code: str, entity: Any = "", message: str = "", action: str = "") -> None:
        finding = Finding(severity, code, str(entity), message, action)
        self.rows.append(finding)
        logger = LOG.error if severity == "FATAL" else LOG.warning if severity == "WARN" else LOG.info
        logger("%s %s [%s] %s", severity, code, entity, message)

    def fatal(self) -> bool:
        return any(row.severity == "FATAL" for row in self.rows)

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame([asdict(row) for row in self.rows], columns=self.COLUMNS)


def norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def compact(value: Any) -> str:
    return norm(value).replace(" ", "")


def first_column(frame: pd.DataFrame, *names: str) -> str | None:
    return next((name for name in names if name in frame.columns), None)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def write_csv_contract(frame: pd.DataFrame | None, path: Path, columns: list[str]) -> None:
    output = frame.copy() if frame is not None else pd.DataFrame()
    for column in columns:
        if column not in output.columns:
            output[column] = pd.NA
    output.loc[:, columns].to_csv(path, index=False)


def et_display(iso_utc: str | None) -> str | None:
    if not iso_utc:
        return None
    try:
        local = pd.to_datetime(iso_utc, utc=True).to_pydatetime().astimezone(ET)
        offset = local.utcoffset() or timedelta(0)
        total_minutes = int(offset.total_seconds() // 60)
        sign = "+" if total_minutes >= 0 else "-"
        hours, minutes = divmod(abs(total_minutes), 60)
        return local.strftime("%Y-%m-%d %I:%M %p ") + f"{local.tzname() or 'ET'} (UTC{sign}{hours:02d}:{minutes:02d})"
    except Exception:
        return None


def make_session() -> requests.Session:
    client = requests.Session()
    retries = Retry(
        total=4, connect=4, read=4, backoff_factor=0.75,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
    )
    client.mount("https://", HTTPAdapter(max_retries=retries))
    client.headers.update({
        "User-Agent": "MLB-Pitch-Environment-V54/1.0 (+local Windows pipeline)",
        "Accept": "text/html,application/json,text/csv,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return client


def get_json(client: requests.Session, url: str, health: Health, code: str,
             params: dict[str, Any] | None = None, timeout: int = 45) -> dict[str, Any] | None:
    try:
        response = client.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        health.add("WARN", code, url, f"{type(exc).__name__}: {exc}", "Source abstains; inspect logs and raw artifacts.")
        return None


def get_text(client: requests.Session, url: str, raw_dir: Path, label: str,
             health: Health) -> tuple[str, Path | None, int | None]:
    try:
        response = client.get(url, timeout=60)
        response.raise_for_status()
        raw_path = raw_dir / f"{label}.html"
        raw_path.write_text(response.text, encoding="utf-8")
        return response.text, raw_path, response.status_code
    except Exception as exc:
        health.add("WARN", "TEXT_FETCH_FAILED", label, f"{type(exc).__name__}: {exc}", "Source abstains.")
        return "", None, None


def canonical_venue(value: Any) -> str:
    return BRANDING.get(str(value), str(value))


def autodetect_park_reference() -> Path:
    script_dir = Path(__file__).resolve().parent
    candidates = [
        script_dir / "mlb_park_reference_full_corrected_v3.csv",
        Path.cwd() / "mlb_park_reference_full_corrected_v3.csv",
        script_dir / "mlb_park_reference_verified.csv",
        Path.cwd() / "mlb_park_reference_verified.csv",
    ]
    match = next((path for path in candidates if path.exists()), None)
    if match is None:
        raise FileNotFoundError("Park reference CSV not found beside the script or in the current working directory.")
    return match


def load_parks(path: Path) -> pd.DataFrame:
    parks = pd.read_csv(path)
    parks.columns = [compact(column) for column in parks.columns]
    required = {"venuename", "team", "lat", "lon", "ballparkelevationm", "rooftype", "homeplatebearingdeg"}
    missing = sorted(required - set(parks.columns))
    if missing:
        raise ValueError(f"Park reference missing required columns: {missing}")
    for column in ("lat", "lon", "ballparkelevationm", "homeplatebearingdeg", "stationelevationm"):
        if column in parks.columns:
            parks[column] = pd.to_numeric(parks[column], errors="coerce")
    parks["roofclass"] = parks["rooftype"].map(lambda value: norm(value).replace(" ", "").upper())
    valid_roofs = OUTDOOR_CLASSES | FIXED_CLASSES | RETRACTABLE_CLASSES
    invalid = parks.loc[~parks.roofclass.isin(valid_roofs), "rooftype"].dropna().unique().tolist()
    if invalid:
        raise ValueError(f"Unsupported roof classes in park reference: {invalid}")
    return parks.rename(columns={"team": "park_home_team"})


def fetch_schedule(client: requests.Session, requested_date: str, health: Health) -> pd.DataFrame:
    payload = get_json(
        client, f"{MLB_API}/schedule", health, "SCHEDULE_FETCH_FAILED",
        {"sportId": 1, "date": requested_date, "hydrate": "probablePitcher,venue,team"},
    )
    rows: list[dict[str, Any]] = []
    for day in (payload or {}).get("dates", []):
        for game in day.get("games", []):
            if game.get("gameType") != "R":
                continue
            teams = game.get("teams", {})
            for side in ("away", "home"):
                team_block = teams.get(side, {})
                probable = team_block.get("probablePitcher") or {}
                rows.append({
                    "game_pk": game.get("gamePk"),
                    "game_datetime_utc": game.get("gameDate"),
                    "venue_name_api": (game.get("venue") or {}).get("name"),
                    "home_team": ((teams.get("home") or {}).get("team") or {}).get("name"),
                    "away_team": ((teams.get("away") or {}).get("team") or {}).get("name"),
                    "side": side,
                    "team": (team_block.get("team") or {}).get("name"),
                    "team_id": (team_block.get("team") or {}).get("id"),
                    "pitcher_id": probable.get("id"),
                    "pitcher_name": probable.get("fullName"),
                    "pitcher_resolution_method": "MLB_SCHEDULE" if probable.get("id") else "UNRESOLVED",
                })
    return pd.DataFrame(rows)


def join_parks(games: pd.DataFrame, parks: pd.DataFrame, health: Health) -> pd.DataFrame:
    result_rows: list[dict[str, Any]] = []
    game_columns = list(games.columns)
    for _, game in games.iterrows():
        venue_key = norm(canonical_venue(game.get("venue_name_api")))
        candidates = parks[parks.venuename.map(norm).eq(venue_key)]
        join_method = "EXACT_VENUE_NAME"
        if len(candidates) != 1:
            candidates = parks[parks.park_home_team.map(norm).eq(norm(game.get("home_team")))]
            join_method = "EXACT_HOME_TEAM_FALLBACK"
        if len(candidates) != 1:
            health.add(
                "FATAL", "MISSING_VERIFIED_PARK", game.get("game_pk"),
                f"venue={game.get('venue_name_api')}; home={game.get('home_team')}",
                "Correct park reference data or BRANDING alias.",
            )
            continue
        record = candidates.iloc[0].to_dict()
        record.update({column: game.get(column) for column in game_columns})
        record["park_join_method"] = join_method
        result_rows.append(record)
    return pd.DataFrame(result_rows)


def refresh_unresolved_starters(client: requests.Session, joined: pd.DataFrame, health: Health) -> pd.DataFrame:
    output = joined.copy()
    for index, row in output[output.pitcher_id.isna()].iterrows():
        payload = get_json(
            client, f"{MLB_API}/game/{int(row.game_pk)}/feed/live", health, "GAME_FEED_FETCH_FAILED"
        ) or {}
        probable = (((payload.get("gameData") or {}).get("teams") or {}).get(row.side) or {}).get("probablePitcher") or {}
        if probable.get("id"):
            output.at[index, "pitcher_id"] = probable.get("id")
            output.at[index, "pitcher_name"] = probable.get("fullName")
            output.at[index, "pitcher_resolution_method"] = "MLB_GAME_FEED"
        else:
            health.add(
                "WARN", "STARTER_STILL_PENDING_FROM_UPSTREAM", row.game_pk,
                f"{row.team} starter is not published by upstream MLB schedule/feed data.",
                "Game environment remains valid; pitcher physics is skipped for this side.",
            )
    return output


def saturation_vapor_pressure_pa(temperature_c: float) -> float:
    return 610.94 * math.exp(17.625 * temperature_c / (temperature_c + 243.04))


def density_terms(temperature_c: float, humidity_pct: float, pressure_hpa: float) -> dict[str, float]:
    vapor_pressure = max(0.0, min(1.0, humidity_pct / 100.0)) * saturation_vapor_pressure_pa(temperature_c)
    temperature_k = temperature_c + 273.15
    pressure_pa = pressure_hpa * 100.0
    dry_pressure = pressure_pa - vapor_pressure
    dry_density = dry_pressure / (R_D * temperature_k)
    vapor_density = vapor_pressure / (R_V * temperature_k)
    air_density = dry_density + vapor_density
    return {
        "temperature_c": temperature_c,
        "temperature_k": temperature_k,
        "relative_humidity_pct": humidity_pct,
        "pressure_hpa": pressure_hpa,
        "pressure_pa": pressure_pa,
        "saturation_vapor_pressure_pa": saturation_vapor_pressure_pa(temperature_c),
        "vapor_pressure_pa": vapor_pressure,
        "dry_air_partial_pressure_pa": dry_pressure,
        "dry_air_density_kg_m3": dry_density,
        "vapor_density_kg_m3": vapor_density,
        "air_density_kg_m3": air_density,
        "density_ratio_to_ref": air_density / RHO_REF,
    }


def wet_bulb_c(temperature_c: float, humidity_pct: float) -> float:
    return (
        temperature_c * math.atan(0.151977 * math.sqrt(humidity_pct + 8.313659))
        + math.atan(temperature_c + humidity_pct)
        - math.atan(humidity_pct - 1.676331)
        + 0.00391838 * humidity_pct ** 1.5 * math.atan(0.023101 * humidity_pct)
        - 4.686035
    )


def fetch_ambient(client: requests.Session, row: pd.Series, raw_dir: Path, health: Health) -> dict[str, Any]:
    game_time = pd.to_datetime(row.game_datetime_utc, utc=True)
    days_ahead = (game_time.date() - datetime.now(timezone.utc).date()).days
    params = {
        "latitude": row.lat,
        "longitude": row.lon,
        "hourly": "temperature_2m,relative_humidity_2m,surface_pressure,wind_speed_10m,wind_direction_10m",
        "timezone": "UTC",
        "forecast_days": max(3, min(16, days_ahead + 2)),
    }
    payload = get_json(client, OPEN_METEO_URL, health, "AMBIENT_FETCH_FAILED", params)
    if not payload:
        return {}
    write_json(raw_dir / f"ambient_{row.game_pk}.json", payload)
    hourly = payload.get("hourly") or {}
    timestamps = pd.to_datetime(hourly.get("time", []), utc=True)
    if len(timestamps) == 0:
        health.add("WARN", "AMBIENT_EMPTY_HOURLY", row.game_pk, "Open-Meteo returned no hourly timestamps.", "Skip weather branches.")
        return {}
    selected_index = int(np.argmin(abs(timestamps - game_time)))
    delta_minutes = abs((timestamps[selected_index] - game_time).total_seconds()) / 60.0
    if delta_minutes > 90:
        health.add(
            "WARN", "AMBIENT_FORECAST_TIME_GAP", row.game_pk,
            f"Nearest forecast hour is {delta_minutes:.0f} minutes from first pitch.",
            "Review requested date and Open-Meteo forecast coverage.",
        )
    try:
        temperature_c = float(hourly["temperature_2m"][selected_index])
        humidity_pct = float(hourly["relative_humidity_2m"][selected_index])
        pressure_hpa = float(hourly["surface_pressure"][selected_index])
        wind_speed_kmh = float(hourly["wind_speed_10m"][selected_index])
        wind_from_deg = float(hourly["wind_direction_10m"][selected_index])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        health.add("WARN", "AMBIENT_SCHEMA_INVALID", row.game_pk, f"{type(exc).__name__}: {exc}", "Skip weather branches.")
        return {}
    terms = density_terms(temperature_c, humidity_pct, pressure_hpa)
    return {
        "ambient_temperature_c": temperature_c,
        "ambient_relative_humidity_pct": humidity_pct,
        "ambient_surface_pressure_hpa": pressure_hpa,
        "ambient_wind_speed_kmh": wind_speed_kmh,
        "ambient_wind_direction_deg_from": wind_from_deg,
        "ambient_air_density_kg_m3": terms["air_density_kg_m3"],
        "ambient_density_ratio_to_ref": terms["density_ratio_to_ref"],
        "ambient_wetbulb_c": wet_bulb_c(temperature_c, humidity_pct),
        "forecast_valid_time_utc": str(timestamps[selected_index]),
        "forecast_time_delta_minutes": round(delta_minutes, 1),
    }


def wind_vectors(wind_speed_kmh: float, wind_from_deg: float, homeplate_bearing_deg: float) -> dict[str, float]:
    """Convert meteorological wind FROM bearing to a movement TO vector."""
    wind_to_deg = (float(wind_from_deg) + 180.0) % 360.0
    relative_deg = (wind_to_deg - float(homeplate_bearing_deg)) % 360.0
    relative_rad = math.radians(relative_deg)
    return {
        "wind_direction_deg_to": wind_to_deg,
        "wind_to_cf_angle_deg": relative_deg,
        "wind_out_to_cf_kmh": float(wind_speed_kmh) * math.cos(relative_rad),
        "wind_cross_kmh": float(wind_speed_kmh) * math.sin(relative_rad),
    }


def outdoor_branch(row: pd.Series, weather: dict[str, Any]) -> dict[str, Any]:
    vectors = wind_vectors(
        weather["ambient_wind_speed_kmh"], weather["ambient_wind_direction_deg_from"], row.homeplatebearingdeg
    )
    return {
        "branch": "OUTDOOR",
        "air_environment_basis": "LOCAL_OUTDOOR_AMBIENT",
        "temperature_source": "OPEN_METEO_FORECAST",
        "pressure_source": "OPEN_METEO_SURFACE_PRESSURE",
        "temperature_c": weather["ambient_temperature_c"],
        "relative_humidity_pct": weather["ambient_relative_humidity_pct"],
        "pressure_hpa": weather["ambient_surface_pressure_hpa"],
        "air_density_kg_m3": weather["ambient_air_density_kg_m3"],
        "density_ratio_to_ref": weather["ambient_density_ratio_to_ref"],
        "density_uncertainty_kg_m3": 0.01,
        "external_wind_speed_kmh": weather["ambient_wind_speed_kmh"],
        "external_wind_direction_deg_from": weather["ambient_wind_direction_deg_from"],
        "wind_model_status": "APPLICABLE_OUTDOOR",
        "forecast_valid_time_utc": weather["forecast_valid_time_utc"],
        "forecast_time_delta_minutes": weather["forecast_time_delta_minutes"],
        **vectors,
    }


def enclosed_branch(row: pd.Series, weather: dict[str, Any]) -> dict[str, Any]:
    temperature_c = weather["ambient_temperature_c"]
    station_elevation = row.get("stationelevationm")
    source_elevation = float(station_elevation) if pd.notna(station_elevation) else float(row.ballparkelevationm)
    corrected_pressure = weather["ambient_surface_pressure_hpa"] * math.exp(
        -G * (float(row.ballparkelevationm) - source_elevation) / (R_D * (temperature_c + 273.15))
    )
    terms = density_terms(temperature_c, weather["ambient_relative_humidity_pct"], corrected_pressure)
    return {
        "branch": "ENCLOSED",
        "air_environment_basis": "INDOOR_MODELED_PRESSURE_ANCHORED",
        "temperature_source": "OUTDOOR_AMBIENT_PROXY_HVAC_UNMEASURED",
        "pressure_source": "LOCAL_BAROMETRY_ELEVATION_CORRECTED",
        "temperature_c": temperature_c,
        "relative_humidity_pct": weather["ambient_relative_humidity_pct"],
        "pressure_hpa": corrected_pressure,
        "air_density_kg_m3": terms["air_density_kg_m3"],
        "density_ratio_to_ref": terms["density_ratio_to_ref"],
        "density_uncertainty_kg_m3": 0.02,
        "external_wind_speed_kmh": 0.0,
        "external_wind_direction_deg_from": np.nan,
        "wind_direction_deg_to": np.nan,
        "wind_to_cf_angle_deg": np.nan,
        "wind_out_to_cf_kmh": 0.0,
        "wind_cross_kmh": 0.0,
        "wind_model_status": "ZERO_EXTERNAL_WIND_ENCLOSED",
        "forecast_valid_time_utc": weather["forecast_valid_time_utc"],
        "forecast_time_delta_minutes": weather["forecast_time_delta_minutes"],
    }


def clean_page(source: str) -> str:
    without_script = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", " ", source, flags=re.I | re.S)
    without_tags = re.sub(r"<[^>]+>", " ", html.unescape(without_script))
    return re.sub(r"\s+", " ", without_tags).strip().lower()


def team_tokens(team_name: Any) -> list[str]:
    normalized = norm(team_name)
    tokens = {normalized}
    if normalized:
        tokens.add(normalized.split()[-1])
    for canonical, aliases in MASCOT_ALIASES.items():
        if canonical in normalized:
            tokens.update(aliases)
    return [token for token in tokens if len(token) > 1]


def matched_game_contexts(page: str, row: pd.Series, max_gap: int = 1600) -> list[str]:
    text = clean_page(page)
    home_positions = [match.start() for token in team_tokens(row.home_team) for match in re.finditer(r"\b" + re.escape(token) + r"\b", text)]
    away_positions = [match.start() for token in team_tokens(row.away_team) for match in re.finditer(r"\b" + re.escape(token) + r"\b", text)]
    contexts: list[str] = []
    for home_position in home_positions:
        close_away = min(away_positions, key=lambda point: abs(point - home_position), default=None)
        if close_away is not None and abs(close_away - home_position) <= max_gap:
            low, high = sorted((home_position, close_away))
            contexts.append(text[max(0, low - 500):min(len(text), high + 1200)])
    if contexts:
        return contexts
    venue_key = norm(row.venuename)
    venue_positions = [match.start() for match in re.finditer(re.escape(venue_key), text)] if venue_key else []
    team_positions = home_positions + away_positions
    for venue_position in venue_positions:
        close_team = min(team_positions, key=lambda point: abs(point - venue_position), default=None)
        if close_team is not None and abs(close_team - venue_position) <= max_gap:
            low, high = sorted((venue_position, close_team))
            contexts.append(text[max(0, low - 500):min(len(text), high + 1200)])
    return contexts


def parse_generic_roof(page: str, row: pd.Series, source_mode: str) -> tuple[str | None, str, float]:
    contexts = matched_game_contexts(page, row)
    if not contexts:
        return None, f"{source_mode}_NO_VERIFIED_GAME_CONTEXT", 0.0
    votes: list[str] = []
    for context in contexts:
        is_closed = bool(CLOSED_ROOF_RX.search(context))
        is_open = bool(OPEN_ROOF_RX.search(context))
        if is_closed and not is_open:
            votes.append("CLOSED")
        elif is_open and not is_closed:
            votes.append("OPEN")
        elif source_mode == "SPORTSPREDICT" and WEATHER_ANCHOR_RX.search(context) and not is_closed:
            votes.append("OPEN")
        elif source_mode == "WEATHERMLB" and WEATHER_ANCHOR_RX.search(context) and not is_closed and not is_open:
            votes.append("OPEN")
    if not votes:
        return None, f"{source_mode}_VERIFIED_CONTEXT_NO_VOTE", 0.0
    if len(set(votes)) != 1:
        return None, f"{source_mode}_CONTRADICTORY_CONTEXTS", 0.0
    vote = votes[0]
    if source_mode == "ROTOWIRE":
        return vote, f"ROTOWIRE_EXPLICIT_{vote}_PROSE", 0.90
    if source_mode == "SPORTSPREDICT":
        return vote, "SPORTSPREDICT_EXPLICIT_OR_VERIFIED_WEATHER_CARD", 0.78 if vote == "OPEN" else 0.85
    return vote, "WEATHERMLB_EXPLICIT_CLOSED_OR_VERIFIED_NO_CLOSED_CARD", 0.78 if vote == "OPEN" else 0.85


def parse_dedicated_roof(page: str, row: pd.Series, requested_date: str) -> tuple[str | None, str, float]:
    text = clean_page(page)
    parsed_date = pd.to_datetime(requested_date).date()
    date_tokens = {
        requested_date.lower(),
        f"{parsed_date.strftime('%b').lower()} {parsed_date.day}",
        f"{parsed_date.strftime('%B').lower()} {parsed_date.day}",
    }
    venue_key = norm(row.venuename)
    context = text
    if venue_key and venue_key in text:
        location = text.index(venue_key)
        context = text[max(0, location - 1000):location + 2200]
    if not any(token in context for token in date_tokens):
        return None, "DEDICATED_SOURCE_NO_TODAY_HOME_GAME_CONTEXT", 0.0
    is_open = bool(OPEN_ROOF_RX.search(context))
    is_closed = bool(CLOSED_ROOF_RX.search(context))
    if is_open ^ is_closed:
        return ("OPEN" if is_open else "CLOSED"), "DEDICATED_SOURCE_TODAY_EXPLICIT", 0.95
    return None, "DEDICATED_SOURCE_NO_UNAMBIGUOUS_TODAY_STATUS", 0.0


def resolve_roof_state(client: requests.Session, row: pd.Series, requested_date: str, raw_dir: Path,
                       health: Health, page_cache: dict[str, tuple[str, Path | None, int | None]],
                       evidence_rows: list[dict[str, Any]], conflict_rows: list[dict[str, Any]]) -> tuple[str, str, dict[str, str | None]]:
    if row.roofclass in OUTDOOR_CLASSES:
        return "NOT_APPLICABLE", "STRUCTURAL_OUTDOOR", {}
    if row.roofclass in FIXED_CLASSES:
        return "FIXED_CLOSED", "STRUCTURAL_FIXED_ENCLOSED", {}
    sources = dict(GENERIC_ROOF_SOURCES)
    dedicated_url = DEDICATED_ROOF_SOURCES.get(row.venuename)
    if dedicated_url:
        sources["dedicated"] = (dedicated_url, "DEDICATED")
    votes: dict[str, str | None] = {}
    for source_name, (url, source_mode) in sources.items():
        if url not in page_cache:
            page_cache[url] = get_text(client, url, raw_dir, f"roof_{norm(source_name)}", health)
        page, raw_path, http_status = page_cache[url]
        if source_mode == "DEDICATED":
            vote, parser_status, confidence = parse_dedicated_roof(page, row, requested_date)
        else:
            vote, parser_status, confidence = parse_generic_roof(page, row, source_mode)
        votes[source_name] = vote
        evidence_rows.append({
            "game_pk": row.game_pk,
            "venue_name": row.venuename,
            "source": source_name,
            "scope": "SINGLE_VENUE" if source_mode == "DEDICATED" else "MULTI_GAME",
            "url": url,
            "vote": vote,
            "parser_status": parser_status,
            "confidence": confidence,
            "http_status": http_status,
            "raw_file": str(raw_path) if raw_path else None,
        })
    dedicated_vote = votes.get("dedicated")
    generic_votes = {source: vote for source, vote in votes.items() if source != "dedicated" and vote}
    all_votes = set(generic_votes.values()) | ({dedicated_vote} if dedicated_vote else set())
    has_conflict = len(all_votes) > 1
    conflict_record = {
        "game_pk": row.game_pk,
        "venue_name": row.venuename,
        "votes_json": json.dumps(votes, sort_keys=True),
        "has_conflict": has_conflict,
        "resolution": "",
    }
    conflict_rows.append(conflict_record)
    if has_conflict:
        health.add("WARN", "ROOF_SOURCE_CONFLICT", row.game_pk, f"votes={votes}", "Model both OUTDOOR and ENCLOSED branches.")
        conflict_record["resolution"] = "CONFLICTED_DUAL_BRANCH"
        return "UNRESOLVED_DUAL_BRANCH", "SOURCE_CONFLICT_DUAL_BRANCH", votes
    if dedicated_vote:
        conflict_record["resolution"] = "DEDICATED_TODAY_SOURCE_DECISIVE"
        return dedicated_vote, "DEDICATED_TODAY_SOURCE_DECISIVE", votes
    generic_values = list(generic_votes.values())
    if generic_values.count("OPEN") >= 2 and generic_values.count("CLOSED") == 0:
        conflict_record["resolution"] = "TWO_GENERIC_SOURCE_CONSENSUS"
        return "OPEN", "TWO_GENERIC_SOURCE_CONSENSUS", votes
    if generic_values.count("CLOSED") >= 2 and generic_values.count("OPEN") == 0:
        conflict_record["resolution"] = "TWO_GENERIC_SOURCE_CONSENSUS"
        return "CLOSED", "TWO_GENERIC_SOURCE_CONSENSUS", votes
    conflict_record["resolution"] = "INSUFFICIENT_QUORUM_DUAL_BRANCH"
    return "UNRESOLVED_DUAL_BRANCH", "INSUFFICIENT_QUORUM_DUAL_BRANCH", votes


def savant_pitch_query(client: requests.Session, pitcher_id: int, start_date: str, end_date: str,
                       raw_dir: Path, health: Health, audit_rows: list[dict[str, Any]], label: str) -> pd.DataFrame:
    params = {
        "type": "details", "player_type": "pitcher", "player_id": pitcher_id,
        "game_date_gt": start_date, "game_date_lt": end_date, "min_pitches": 0, "min_results": 0,
    }
    audit = {
        "pitcher_id": pitcher_id, "query_start": start_date, "query_end": end_date,
        "window_label": label, "url": SAVANT_CSV, "request_params_json": json.dumps(params, sort_keys=True),
    }
    try:
        response = client.get(SAVANT_CSV, params=params, timeout=90)
        response.raise_for_status()
        raw_path = raw_dir / f"savant_{pitcher_id}_{label}.csv"
        raw_path.write_bytes(response.content)
        frame = pd.read_csv(raw_path)
        frame.columns = [norm(column).replace(" ", "_") for column in frame.columns]
        id_column = first_column(frame, "pitcher", "pitcher_id", "player_id")
        if id_column is None:
            raise ValueError("No pitcher identifier column in Savant CSV.")
        scoped = frame[pd.to_numeric(frame[id_column], errors="coerce").eq(pitcher_id)].copy()
        audit.update({
            "http_status": response.status_code, "raw_file": str(raw_path), "rows_returned": len(frame),
            "id_column": id_column, "rows_scoped": len(scoped), "scope_pass": bool(len(scoped)),
        })
        audit_rows.append(audit)
        return scoped
    except Exception as exc:
        audit.update({"scope_pass": False, "error": f"{type(exc).__name__}: {exc}"})
        audit_rows.append(audit)
        health.add("WARN", "SAVANT_PITCH_QUERY_FAILED", pitcher_id, audit["error"], "Skip pitcher pitch physics for this run.")
        return pd.DataFrame()


def summarize_window(frame: pd.DataFrame) -> pd.DataFrame:
    pitch_column = first_column(frame, "pitch_name", "pitch_type", "pitch") if not frame.empty else None
    if pitch_column is None:
        return pd.DataFrame()
    eligible = frame[frame[pitch_column].notna() & frame[pitch_column].astype(str).str.strip().ne("")].copy()
    if eligible.empty:
        return pd.DataFrame()
    metrics = [
        column for column in ("release_speed", "effective_speed", "release_spin_rate", "spin_axis", "pfx_x", "pfx_z", "release_extension")
        if column in eligible.columns
    ]
    for metric in metrics:
        eligible[metric] = pd.to_numeric(eligible[metric], errors="coerce")
    grouped = eligible.groupby(pitch_column, dropna=True)
    summary = grouped.size().rename("pitches").to_frame().reset_index().rename(columns={pitch_column: "pitch_type"})
    for metric in metrics:
        summary[metric] = grouped[metric].mean().values
    summary["usage_pct"] = 100.0 * summary.pitches / summary.pitches.sum()
    return summary.sort_values("pitches", ascending=False).reset_index(drop=True)


def weighted_arsenal(season: pd.DataFrame, recent: pd.DataFrame, recent_weight: float = 0.65) -> pd.DataFrame:
    if season.empty and recent.empty:
        return pd.DataFrame()
    if season.empty:
        output = recent.copy()
        output["window_basis"] = "RECENT_ONLY"
        return output
    if recent.empty:
        output = season.copy()
        output["window_basis"] = "SEASON_ONLY"
        return output
    merged = season.merge(recent, on="pitch_type", how="outer", suffixes=("_season", "_recent")).fillna(0)
    output = pd.DataFrame({"pitch_type": merged.pitch_type})
    for metric in ("pitches", "release_speed", "effective_speed", "release_spin_rate", "spin_axis", "pfx_x", "pfx_z", "release_extension", "usage_pct"):
        season_values = merged[f"{metric}_season"] if f"{metric}_season" in merged else 0
        recent_values = merged[f"{metric}_recent"] if f"{metric}_recent" in merged else 0
        output[metric] = season_values + recent_values if metric == "pitches" else (1.0 - recent_weight) * season_values + recent_weight * recent_values
    output["window_basis"] = "RECENT_65_SEASON_35"
    return output[output.pitch_type.astype(str).str.strip().ne("")].sort_values("pitches", ascending=False).reset_index(drop=True)


def extract_json_array_after_key(source: str, key: str) -> list[dict[str, Any]]:
    """Return the balanced JSON array immediately assigned to a JavaScript key."""
    assignment = re.search(r"(?:[\"']?" + re.escape(key) + r"[\"']?)\s*[:=]\s*\[", source)
    if assignment is None:
        return []
    start = source.find("[", assignment.start())
    depth = 0
    quote: str | None = None
    escaped = False
    for position in range(start, len(source)):
        char = source[position]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                candidate = source[start:position + 1]
                try:
                    decoded = json.loads(candidate)
                    return decoded if isinstance(decoded, list) else []
                except json.JSONDecodeError:
                    return []
    return []


def fetch_player_active_spin(client: requests.Session, pitcher_id: int, season: int, raw_dir: Path,
                             health: Health, audit_rows: list[dict[str, Any]]) -> pd.DataFrame:
    url = f"{SAVANT_PLAYER_URL}/{pitcher_id}"
    raw_path = raw_dir / f"savant_player_{pitcher_id}.html"
    try:
        response = client.get(url, timeout=60)
        response.raise_for_status()
        raw_path.write_text(response.text, encoding="utf-8")
        source_rows = extract_json_array_after_key(response.text, "spinAxis")
        if not source_rows:
            health.add(
                "WARN", "ACTIVE_SPIN_PLAYER_PAYLOAD_MISSING", pitcher_id,
                "serverVals.spinAxis was absent or could not be parsed as a JSON array.",
                "Leave Active Spin blank for this pitcher and inspect saved raw page.",
            )
            audit_rows.append({
                "pitcher_id": pitcher_id, "pitch_type": None, "pitch_name": None, "active_spin_pct": None,
                "active_spin_raw_decimal": None, "active_spin_formatted": None,
                "source_method": "SAVANT_PLAYER_PAGE_SPINAXIS", "source_url": url,
                "raw_evidence_path": str(raw_path), "player_id_verified": False, "season": season,
                "hawkeye_measured": None, "movement_inferred": None, "status": "PLAYER_PAYLOAD_MISSING",
                "error": "spinAxis array missing or invalid", "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
            })
            return pd.DataFrame()
        accepted: list[dict[str, Any]] = []
        for source in source_rows:
            source_player_id = pd.to_numeric(source.get("player_id"), errors="coerce")
            source_season = pd.to_numeric(source.get("season"), errors="coerce")
            pitch_code = str(source.get("api_pitch_type") or "").upper().strip()
            source_decimal = pd.to_numeric(source.get("active_spin"), errors="coerce")
            source_formatted = pd.to_numeric(source.get("active_spin_formatted"), errors="coerce")
            status = "PLAYER_PAGE_MEASURED"
            error = ""
            accepted_value: float | None = None
            if pd.isna(source_player_id) or int(source_player_id) != pitcher_id:
                status, error = "PLAYER_ID_MISMATCH_REJECTED", f"payload_player_id={source.get('player_id')}"
            elif pd.isna(source_season) or int(source_season) != season:
                status, error = "SEASON_NOT_MATCHED", f"payload_season={source.get('season')}"
            elif not pitch_code:
                status, error = "PITCH_TYPE_UNMAPPED_REJECTED", "api_pitch_type missing"
            elif pd.isna(source_decimal):
                status, error = "ACTIVE_SPIN_NOT_PUBLISHED_FOR_PITCH_TYPE", "active_spin missing"
            else:
                decimal_pct = float(source_decimal) * 100.0
                if not 0.0 <= decimal_pct <= 100.0:
                    status, error = "ACTIVE_SPIN_OUT_OF_RANGE_REJECTED", f"active_spin={source_decimal}"
                elif pd.notna(source_formatted) and abs(decimal_pct - float(source_formatted)) > 1.1:
                    status, error = "ACTIVE_SPIN_FORMAT_MISMATCH_REJECTED", f"decimal_pct={decimal_pct}; formatted={source_formatted}"
                else:
                    accepted_value = float(source_formatted) if pd.notna(source_formatted) else decimal_pct
            audit_rows.append({
                "pitcher_id": pitcher_id, "pitch_type": pitch_code or None, "pitch_name": source.get("api_pitch_name"),
                "active_spin_pct": accepted_value,
                "active_spin_raw_decimal": None if pd.isna(source_decimal) else float(source_decimal),
                "active_spin_formatted": None if pd.isna(source_formatted) else float(source_formatted),
                "source_method": "SAVANT_PLAYER_PAGE_SPINAXIS", "source_url": url,
                "raw_evidence_path": str(raw_path), "player_id_verified": status != "PLAYER_ID_MISMATCH_REJECTED",
                "season": season, "hawkeye_measured": source.get("hawkeye_measured"),
                "movement_inferred": source.get("movement_inferred"), "status": status, "error": error,
                "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
            })
            if accepted_value is not None:
                accepted.append({
                    "pitch_code": pitch_code,
                    "active_spin_pct_source": accepted_value,
                    "active_spin_source_source": "SAVANT_PLAYER_PAGE_SPINAXIS",
                    "active_spin_method_source": "HAWKEYE_MEASURED" if bool(source.get("hawkeye_measured")) else "SAVANT_PROVIDED",
                    "active_spin_movement_inferred_source": bool(source.get("movement_inferred")),
                })
        return pd.DataFrame(accepted).drop_duplicates("pitch_code", keep="first") if accepted else pd.DataFrame()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        health.add("WARN", "ACTIVE_SPIN_PLAYER_PAGE_FETCH_FAILED", pitcher_id, error, "Leave Active Spin blank for this pitcher.")
        audit_rows.append({
            "pitcher_id": pitcher_id, "pitch_type": None, "pitch_name": None, "active_spin_pct": None,
            "active_spin_raw_decimal": None, "active_spin_formatted": None,
            "source_method": "SAVANT_PLAYER_PAGE_SPINAXIS", "source_url": url,
            "raw_evidence_path": str(raw_path), "player_id_verified": False, "season": season,
            "hawkeye_measured": None, "movement_inferred": None, "status": "PLAYER_PAGE_FETCH_FAILED",
            "error": error, "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        })
        return pd.DataFrame()


def canonical_pitch_code(pitch_type: Any) -> str:
    normalized = norm(pitch_type)
    return next((code for code, name in PITCH_CODE_TO_NAME.items() if norm(name) == normalized), str(pitch_type or "").upper().strip())


def merge_active_spin(arsenal: pd.DataFrame, player_spin: pd.DataFrame, pitcher_id: int,
                      merge_audit_rows: list[dict[str, Any]]) -> pd.DataFrame:
    output = arsenal.copy()
    output["pitch_code"] = output.pitch_type.map(canonical_pitch_code)
    output["active_spin_pct"] = np.nan
    output["active_spin_source"] = "UNAVAILABLE_NO_PLAYER_VALUE"
    output["active_spin_method"] = "NOT_PUBLISHED"
    output["active_spin_movement_inferred"] = pd.NA
    if not player_spin.empty:
        output = output.merge(player_spin, on="pitch_code", how="left")
        matched = output["active_spin_pct_source"].notna()
        output.loc[matched, "active_spin_pct"] = output.loc[matched, "active_spin_pct_source"]
        output.loc[matched, "active_spin_source"] = output.loc[matched, "active_spin_source_source"]
        output.loc[matched, "active_spin_method"] = output.loc[matched, "active_spin_method_source"]
        output.loc[matched, "active_spin_movement_inferred"] = output.loc[matched, "active_spin_movement_inferred_source"]
        output = output.drop(columns=[
            "active_spin_pct_source", "active_spin_source_source", "active_spin_method_source",
            "active_spin_movement_inferred_source",
        ])
    for _, row in output.iterrows():
        merge_audit_rows.append({
            "pitcher_id": pitcher_id, "pitch_type": row.pitch_type, "pitch_code": row.pitch_code,
            "active_spin_pct": None if pd.isna(row.active_spin_pct) else float(row.active_spin_pct),
            "match_status": "MATCHED_PLAYER_PAGE" if pd.notna(row.active_spin_pct) else "NO_PLAYER_PAGE_VALUE_FOR_PITCH_TYPE",
        })
    return output.drop(columns=["pitch_code"])


def apply_environment_physics(arsenal: pd.DataFrame, branch: dict[str, Any]) -> pd.DataFrame:
    output = arsenal.copy()
    density_ratio = float(branch["density_ratio_to_ref"])
    wind_out = float(branch.get("wind_out_to_cf_kmh") or 0.0)
    output["density_ratio_to_ref"] = density_ratio
    output["density_only_movement_change_pct"] = (density_ratio - 1.0) * 100.0
    for source, projected, delta in (
        ("pfx_x", "projected_pfx_x", "delta_pfx_x_inches"),
        ("pfx_z", "projected_pfx_z", "delta_pfx_z_inches"),
    ):
        if source in output.columns:
            output[projected] = output[source] * density_ratio
            output[delta] = (output[projected] - output[source]) * 12.0
    output["carry_wind_adjustment_ft_400ft"] = wind_out / 10.0 * 3.0
    output["carry_density_adjustment_ft_400ft"] = (1.0 - density_ratio) * 5.2
    if "release_speed" in output.columns:
        output["effective_plate_speed_delta_mph"] = (1.0 - density_ratio) * output.release_speed * 0.18
    output["active_spin_environment_index"] = output.active_spin_pct * density_ratio
    output["model_label"] = "FIRST_ORDER_ENVIRONMENTAL_SENSITIVITY"
    return output


def validate_run(game_environment: pd.DataFrame, pitch_physics: pd.DataFrame,
                 active_spin_audit: pd.DataFrame, health: Health) -> None:
    if game_environment.empty:
        health.add("FATAL", "NO_ENVIRONMENT_ROWS", "run", "No game environment rows were generated.", "Stop pipeline.")
        return
    if ((game_environment.roofclass == "OUTDOOR") & (game_environment.roof_state != "NOT_APPLICABLE")).any():
        health.add("FATAL", "OUTDOOR_ROOF_STATE_VIOLATION", "run", "Outdoor venue received a non-structural roof state.", "Fix roof branching.")
    if pitch_physics.empty:
        return
    if active_spin_audit.empty:
        health.add("FATAL", "ACTIVE_SPIN_AUDIT_MISSING", "run", "Pitch physics exists but no player Active Spin audit was written.", "Inspect player-page acquisition.")
        return
    statuses = set(active_spin_audit.status.dropna().astype(str))
    terminal_source_failures = {"PLAYER_PAGE_FETCH_FAILED", "PLAYER_PAYLOAD_MISSING"}
    if statuses and statuses.issubset(terminal_source_failures):
        health.add("FATAL", "ACTIVE_SPIN_SOURCE_FAILURE", "run", "All scheduled player Active Spin page acquisitions failed or lacked payloads.", "Inspect raw evidence and Savant response schema.")
    elif pitch_physics.active_spin_pct.notna().sum() == 0:
        health.add("FATAL", "FAIL_ACTIVE_SPIN_ZERO_MATCH_RATE", "run", "Player pages produced no Active Spin matches for modeled pitch rows.", "Inspect pitch-code mapping and Active Spin audits.")


def copy_dashboard(run_dir: Path, script_dir: Path) -> None:
    dashboard = script_dir / "mlb-pitch-environment-live-dashboard-V54.html"
    if dashboard.exists():
        shutil.copy2(dashboard, run_dir / dashboard.name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=str(date.today()))
    parser.add_argument("--park-reference", default=None)
    parser.add_argument("--output-root", default="mlb_daily_outputs")
    parser.add_argument("--open-dashboard", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    health = Health()
    script_dir = Path(__file__).resolve().parent
    run_dir = ensure_dir(Path(args.output_root) / f"{args.date}_{utc_stamp()}")
    raw_dir = ensure_dir(run_dir / "raw")
    client = make_session()

    roof_page_cache: dict[str, tuple[str, Path | None, int | None]] = {}
    roof_evidence_rows: list[dict[str, Any]] = []
    roof_conflict_rows: list[dict[str, Any]] = []
    pitch_query_audit_rows: list[dict[str, Any]] = []
    active_spin_player_audit_rows: list[dict[str, Any]] = []
    active_spin_merge_audit_rows: list[dict[str, Any]] = []
    game_rows: list[dict[str, Any]] = []
    pitch_rows: list[dict[str, Any]] = []
    decomposition_rows: list[dict[str, Any]] = []
    branch_map: dict[tuple[int, str], dict[str, Any]] = {}
    joined = pd.DataFrame()

    try:
        park_path = Path(args.park_reference) if args.park_reference else autodetect_park_reference()
        parks = load_parks(park_path)
        schedule = fetch_schedule(client, args.date, health)
        if schedule.empty:
            health.add("FATAL", "NO_SCHEDULED_GAMES", args.date, "No regular-season games returned by MLB schedule.", "Stop pipeline.")
        else:
            joined = refresh_unresolved_starters(client, join_parks(schedule, parks, health), health)
    except Exception as exc:
        health.add("FATAL", "INITIALIZATION_FAILED", "run", f"{type(exc).__name__}: {exc}", "Correct required local inputs or upstream availability.")

    if not joined.empty:
        unique_games = joined.sort_values(["game_pk", "side"]).groupby("game_pk", as_index=False).first()
        for _, game in unique_games.iterrows():
            weather = fetch_ambient(client, game, raw_dir, health)
            if not weather:
                continue
            roof_state, roof_reason, roof_votes = resolve_roof_state(
                client, game, args.date, raw_dir, health, roof_page_cache, roof_evidence_rows, roof_conflict_rows
            )
            branches = (
                [outdoor_branch(game, weather)] if roof_state in {"NOT_APPLICABLE", "OPEN"}
                else [enclosed_branch(game, weather)] if roof_state in {"FIXED_CLOSED", "CLOSED"}
                else [outdoor_branch(game, weather), enclosed_branch(game, weather)]
            )
            for branch in branches:
                record = {
                    "game_pk": game.game_pk,
                    "game_datetime_utc": game.game_datetime_utc,
                    "game_datetime_et": et_display(game.game_datetime_utc),
                    "venue_name_api": game.venue_name_api,
                    "venue_name": game.venuename,
                    "roofclass": game.roofclass,
                    "ballparkelevationm": game.ballparkelevationm,
                    "roof_state": roof_state,
                    "roof_decision_reason": roof_reason,
                    "roof_votes_json": json.dumps(roof_votes, sort_keys=True),
                    "park_join_method": game.park_join_method,
                    "home_plate_bearing_deg": game.homeplatebearingdeg,
                    **weather,
                    **branch,
                    "temperature_f": branch["temperature_c"] * 9.0 / 5.0 + 32.0,
                    "forecast_valid_time_et": et_display(branch.get("forecast_valid_time_utc")),
                }
                game_rows.append(record)
                branch_map[(int(game.game_pk), branch["branch"])] = record

        season_year = pd.to_datetime(args.date).year
        season_start = date(season_year, 3, 1).isoformat()
        recent_start = (pd.to_datetime(args.date).date() - timedelta(days=30)).isoformat()
        end_exclusive = (pd.to_datetime(args.date).date() + timedelta(days=1)).isoformat()
        for _, pitcher in joined.iterrows():
            if pd.isna(pitcher.pitcher_id):
                continue
            pitcher_id = int(pitcher.pitcher_id)
            season_arsenal = summarize_window(
                savant_pitch_query(client, pitcher_id, season_start, end_exclusive, raw_dir, health, pitch_query_audit_rows, "season")
            )
            recent_arsenal = summarize_window(
                savant_pitch_query(client, pitcher_id, recent_start, end_exclusive, raw_dir, health, pitch_query_audit_rows, "recent30")
            )
            arsenal = weighted_arsenal(season_arsenal, recent_arsenal)
            if arsenal.empty:
                continue
            player_spin = fetch_player_active_spin(client, pitcher_id, season_year, raw_dir, health, active_spin_player_audit_rows)
            arsenal = merge_active_spin(arsenal, player_spin, pitcher_id, active_spin_merge_audit_rows)
            for branch_name in ("OUTDOOR", "ENCLOSED"):
                environment = branch_map.get((int(pitcher.game_pk), branch_name))
                if environment is None:
                    continue
                physics = apply_environment_physics(arsenal, environment)
                physics["game_pk"] = pitcher.game_pk
                physics["side"] = pitcher.side
                physics["team"] = pitcher.team
                physics["pitcher_id"] = pitcher_id
                physics["pitcher_name"] = pitcher.pitcher_name
                physics["pitcher_resolution_method"] = pitcher.pitcher_resolution_method
                physics["venue_name"] = pitcher.venuename
                physics["branch"] = branch_name
                physics["roof_state"] = environment["roof_state"]
                physics["air_density_kg_m3"] = environment["air_density_kg_m3"]
                records = physics.sort_values("pitches", ascending=False).to_dict(orient="records")
                pitch_rows.extend(records)
                for record in records:
                    density_effect = (1.0 - float(record["density_ratio_to_ref"])) * 5.2
                    wind_effect = float(environment.get("wind_out_to_cf_kmh") or 0.0) / 10.0 * 3.0
                    decomposition_rows.append({
                        "game_pk": record["game_pk"], "pitcher_id": record["pitcher_id"],
                        "pitch_type": record["pitch_type"], "branch": branch_name,
                        "density_ratio_to_ref": record["density_ratio_to_ref"],
                        "wind_out_to_cf_kmh": environment.get("wind_out_to_cf_kmh"),
                        "density_effect": density_effect, "wind_effect": wind_effect,
                        "roof_branch_effect": 0.0,
                        "total_carry_effect_ft_400ft": density_effect + wind_effect,
                    })

    game_environment = pd.DataFrame(game_rows)
    pitch_physics = pd.DataFrame(pitch_rows)
    active_spin_audit = pd.DataFrame(active_spin_player_audit_rows)
    validate_run(game_environment, pitch_physics, active_spin_audit, health)
    if len(pitch_rows) != len(decomposition_rows):
        health.add(
            "FATAL", "PITCH_EFFECT_ROW_PARITY_FAILURE", "run",
            f"pitch_rows={len(pitch_rows)} decomposition_rows={len(decomposition_rows)}",
            "Fix pitch effect decomposition loop.",
        )

    write_csv_contract(game_environment, run_dir / "game_environment_audit.csv", [
        "game_pk", "game_datetime_utc", "game_datetime_et", "venue_name_api", "venue_name", "roofclass",
        "ballparkelevationm", "roof_state", "roof_decision_reason", "roof_votes_json", "park_join_method",
        "home_plate_bearing_deg", "branch", "temperature_c", "temperature_f", "relative_humidity_pct",
        "pressure_hpa", "air_density_kg_m3", "density_ratio_to_ref", "external_wind_speed_kmh",
        "external_wind_direction_deg_from", "wind_direction_deg_to", "wind_out_to_cf_kmh", "wind_cross_kmh",
        "forecast_valid_time_utc", "forecast_valid_time_et", "forecast_time_delta_minutes",
    ])
    write_csv_contract(pd.DataFrame(roof_evidence_rows), run_dir / "roof_evidence_audit.csv", [
        "game_pk", "venue_name", "source", "scope", "url", "vote", "parser_status", "confidence",
        "http_status", "raw_file",
    ])
    write_csv_contract(pd.DataFrame(roof_conflict_rows), run_dir / "roof_conflict_audit.csv", [
        "game_pk", "venue_name", "votes_json", "has_conflict", "resolution",
    ])
    write_csv_contract(pd.DataFrame(pitch_query_audit_rows), run_dir / "pitcher_query_audit.csv", [
        "pitcher_id", "query_start", "query_end", "window_label", "url", "request_params_json",
        "http_status", "raw_file", "rows_returned", "id_column", "rows_scoped", "scope_pass", "error",
    ])
    write_csv_contract(active_spin_audit, run_dir / "active_spin_player_source_audit.csv", [
        "pitcher_id", "pitch_type", "pitch_name", "active_spin_pct", "active_spin_raw_decimal",
        "active_spin_formatted", "source_method", "source_url", "raw_evidence_path", "player_id_verified",
        "season", "hawkeye_measured", "movement_inferred", "status", "error", "retrieved_at_utc",
    ])
    write_csv_contract(pd.DataFrame(active_spin_merge_audit_rows), run_dir / "active_spin_merge_audit.csv", [
        "pitcher_id", "pitch_type", "pitch_code", "active_spin_pct", "match_status",
    ])
    write_csv_contract(pitch_physics, run_dir / "pitch_physics_environment.csv", [
        "game_pk", "side", "team", "pitcher_id", "pitcher_name", "pitcher_resolution_method", "venue_name",
        "branch", "roof_state", "air_density_kg_m3", "density_ratio_to_ref", "pitch_type", "pitches",
        "usage_pct", "release_speed", "effective_speed", "release_spin_rate", "spin_axis", "active_spin_pct",
        "active_spin_source", "active_spin_method", "active_spin_movement_inferred", "release_extension", "pfx_x",
        "pfx_z", "projected_pfx_x", "delta_pfx_x_inches", "projected_pfx_z", "delta_pfx_z_inches",
        "carry_wind_adjustment_ft_400ft", "carry_density_adjustment_ft_400ft",
        "effective_plate_speed_delta_mph", "active_spin_environment_index", "window_basis", "model_label",
    ])
    write_csv_contract(pd.DataFrame(decomposition_rows), run_dir / "pitch_effect_decomposition.csv", [
        "game_pk", "pitcher_id", "pitch_type", "branch", "density_ratio_to_ref", "wind_out_to_cf_kmh",
        "density_effect", "wind_effect", "roof_branch_effect", "total_carry_effect_ft_400ft",
    ])

    active_match_rate = None if pitch_physics.empty else round(100.0 * pitch_physics.active_spin_pct.notna().sum() / len(pitch_physics), 2)
    diagnostics = pd.DataFrame([
        {"check": "scheduled_games", "value": 0 if joined.empty else int(joined.game_pk.nunique()), "status": "PASS" if not joined.empty else "FAIL"},
        {"check": "pitch_physics_rows", "value": len(pitch_physics), "status": "PASS" if len(pitch_physics) else "WARN_NO_PHYSICS_ROWS"},
        {"check": "active_spin_player_page_audit_rows", "value": len(active_spin_audit), "status": "PASS" if len(active_spin_audit) else "FAIL_NO_AUDIT"},
        {"check": "active_spin_match_rate_pct", "value": active_match_rate, "status": "FAIL_ACTIVE_SPIN_ZERO_MATCH_RATE" if active_match_rate == 0 else "PASS" if active_match_rate is not None else "WARN_NO_PHYSICS_ROWS"},
        {"check": "pitch_effect_decomposition_rows", "value": len(decomposition_rows), "status": "PASS" if len(decomposition_rows) == len(pitch_physics) else "FAIL_ROW_PARITY"},
    ])
    write_csv_contract(diagnostics, run_dir / "run_health_and_model_diagnostics.csv", ["check", "value", "status"])
    write_csv_contract(health.frame(), run_dir / "findings.csv", Health.COLUMNS)
    copy_dashboard(run_dir, script_dir)

    run_status = {
        "contract_version": VERSION,
        "status": "FAIL" if health.fatal() else "COMPLETE",
        "fatal": health.fatal(),
        "version": VERSION,
        "requested_date": args.date,
        "run_dir": str(run_dir.resolve()),
        "games": 0 if joined.empty else int(joined.game_pk.nunique()),
        "environment_rows": len(game_environment),
        "pitch_rows": len(pitch_physics),
        "active_spin_match_rate_pct": active_match_rate,
        "findings": len(health.rows),
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json(run_dir / "run_status.json", run_status)
    manifest = {
        "version": VERSION,
        "requested_date": args.date,
        "run_dir": str(run_dir.resolve()),
        "fatal": health.fatal(),
        "status": run_status["status"],
        "files": {path.name: sha256_file(path) for path in run_dir.iterdir() if path.is_file()},
    }
    write_json(run_dir / "run_manifest.json", manifest)
    print(json.dumps(run_status, separators=(",", ":")))

    if args.open_dashboard:
        dashboard = run_dir / "mlb-pitch-environment-live-dashboard-V54.html"
        if dashboard.exists():
            webbrowser.open(dashboard.resolve().as_uri())
    return 1 if health.fatal() else 0


if __name__ == "__main__":
    raise SystemExit(main())
