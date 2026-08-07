#!/usr/bin/env python3
"""MLB Daily Pitch Environment - V52
Forensic correction of V51, driven by a real production run and user review:

  - FIX (critical, confirmed): join_parks() previously did `z.update(park_row)`,
    which blindly overwrote the schedule's per-side "team" field with the park
    reference's own "team" column (the home team that plays at that venue). This
    caused EVERY pitcher -- home and away alike -- to be labeled with the home
    team. Fixed by protecting schedule-owned fields from being overwritten by the
    park-reference merge, and added a permanent regression guard
    (validate_team_assignment) that fails the run loudly if this class of bug
    ever recurs.
  - FIX (confirmed): pitch_physics_environment.csv silently dropped active_spin,
    spin_axis, effective_speed, release_extension, projected/delta pfx values,
    effective_plate_speed_delta_mph, and active_spin_environment_index -- all of
    which were already computed in memory by apply_environment_physics() but
    never included in the output column whitelist. All are now written.
  - FIX: the per-pitch delta_pfx_x_inches / delta_pfx_z_inches (which genuinely
    vary per pitch type) now accompany pfx_x/pfx_z in the pitch physics table,
    instead of a flat per-branch scalar being the only "movement" figure shown.
  - FIX: vote_sportspredict now implements the user-specified rule: if the site
    shows weather/wind data for a retractable-roof game's card and does NOT
    contain "roof likely closed"/"roof closed" language, that counts as an
    implicit OPEN vote (mirroring weathermlb's existing rule).
  - FIX: vote_weathermlb's implicit-OPEN detection anchor set broadened (mph, wind
    speed digits, %, deg symbols) so it doesn't return "no structured match" on
    real page layouts.
  - FIX: vote_simple (istheroofopen.com/isthedomeopen.com) explicitly treats
    TBD/pending language as NOT a vote either way, while explicit OPEN/CLOSED
    still count, per user specification.
  - ADDED: temperature_f, elevation_ft, wind_speed_mph, game_datetime_est,
    forecast_valid_time_est, and a human-readable wind_park_relative_description.
  - ADDED: pitch tables are now sorted descending by usage (most-thrown pitch
    first) at both the summarize_window() and weighted_arsenal() stage.
"""
from __future__ import annotations

import argparse, hashlib, html, json, logging, math, re, shutil, sys, webbrowser
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pandas.errors import EmptyDataError
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

VERSION = "52.0.0"
MLB = "https://statsapi.mlb.com/api/v1"
SAVANT = "https://baseballsavant.mlb.com/statcast_search/csv"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
R_D = 287.05
R_V = 461.495
G = 9.80665
RHO_REF = 1.2041
EST = timezone(timedelta(hours=-5))
LOG = logging.getLogger(f"mlb_pitch_environment_v{VERSION.split('.')[0]}")

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

MASCOT_ALIASES = {
    "diamondbacks": ["diamondbacks", "dbacks", "d backs"],
    "red sox": ["red sox", "redsox", "sox"],
    "white sox": ["white sox", "whitesox", "sox"],
    "blue jays": ["blue jays", "bluejays", "jays"],
    "athletics": ["athletics", "as", "a s"],
    "mariners": ["mariners", "m s"],
}

ROOF_GENERIC_SOURCES = {
    "sportspredictapp": {"url": "https://sportspredictapp.com/mlb/weather", "mode": "SPORTSPREDICT"},
    "weathermlb": {"url": "https://weathermlb.com/", "mode": "WEATHERMLB"},
    "rotowire": {"url": "https://www.rotowire.com/baseball/weather.php", "mode": "ROTOWIRE_PROSE"},
}

VENUE_DEDICATED_SOURCES = {
    "Rogers Centre": {"isthedomeopen": {"url": "https://isthedomeopen.com/", "mode": "SIMPLE"}},
    "American Family Field": {"istheroofopen": {"url": "https://istheroofopen.com/american-family-field/", "mode": "SIMPLE"}},
    "Chase Field": {"istheroofopen": {"url": "https://istheroofopen.com/chase-field/", "mode": "SIMPLE"}},
    "Globe Life Field": {"istheroofopen": {"url": "https://istheroofopen.com/globe-life-field/", "mode": "SIMPLE"}},
    "Minute Maid Park": {"istheroofopen": {"url": "https://istheroofopen.com/minute-maid-park/", "mode": "SIMPLE"}},
    "loanDepot park": {"istheroofopen": {"url": "https://istheroofopen.com/loandepot-park/", "mode": "SIMPLE"}},
    "T-Mobile Park": {"istheroofopen": {"url": "https://istheroofopen.com/t-mobile-park/", "mode": "SIMPLE"}},
}

OPEN_RX = re.compile(
    r"\b(?:roof|dome)\b[^.]{0,90}\b(?:is|will\s+(?:likely\s+)?be|expected\s+to\s+be|likely|projected\s+to\s+be)\b[^.]{0,30}\bopen\b"
    r"|\bopen\s+(?:roof|dome)\b|\broof\s*[:\-]?\s*open\b|\bwill\s+(?:likely\s+)?open\b|\bopen[- ]air\b"
    r"|\bconditions?\s+will\s+favor\s+an?\s+open\b|\bconditions?\s+favor\s+an?\s+open\b|\blikely\s+open\b"
    r"|\bfavors?\s+an?\s+open\s+roof\b",
    re.I,
)
CLOSED_RX = re.compile(
    r"\b(?:roof|dome)\b[^.]{0,90}\b(?:is|will\s+(?:likely\s+)?be|expected\s+to\s+be|likely|projected\s+to\s+be)\b[^.]{0,30}\bclosed\b"
    r"|\bclosed\s+(?:roof|dome)\b|\broof\s*[:\-]?\s*closed\b|\bkeep(?:ing)?\s+the\s+roof[^.]{0,60}\bclosed\b"
    r"|\bwill\s+(?:likely\s+)?close\b|\bclosed\s+dome\b|\blikely\s+closed\b|\broof\s+closed\b",
    re.I,
)
ROOF_KEYWORD_RX = re.compile(r"\broof\b|\bdome\b", re.I)
BOILERPLATE_DOME_RX = re.compile(r"\bdomed stadium\b|\binside a domed stadium\b", re.I)
_CARD_BOUNDARY_RX = re.compile(r"\b\d{1,2}\s+\d{2}\s+(?:am|pm)\b", re.I)
_ROOF_LIKELY_CLOSED_RX = re.compile(r"\broof\s+(?:likely\s+)?closed\b|\bweather\s+neutral\b.{0,40}\bclosed\b", re.I)
_WEATHER_DATA_ANCHOR_RX = re.compile(r"\b(temp|temperature|wind|humidity|hum|mph|kmh|km h|deg|%)\b", re.I)


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
        self.rows.append(Finding(severity, code, str(entity), message, action))
        fn = LOG.error if severity == "FATAL" else (LOG.warning if severity == "WARN" else LOG.info)
        fn("%s %s [%s] %s", severity, code, entity, message)

    def fatal(self) -> bool:
        return any(x.severity == "FATAL" for x in self.rows)

    def frame(self) -> pd.DataFrame:
        if not self.rows:
            return pd.DataFrame(columns=self.COLUMNS)
        return pd.DataFrame([asdict(x) for x in self.rows], columns=self.COLUMNS)


def safe_read_csv(path: Path, columns: list[str] | None = None) -> pd.DataFrame:
    if (not path.exists()) or path.stat().st_size == 0:
        return pd.DataFrame(columns=columns or [])
    try:
        return pd.read_csv(path)
    except EmptyDataError:
        return pd.DataFrame(columns=columns or [])


def write_csv_contract(df: pd.DataFrame, path: Path, columns: list[str]) -> None:
    if df.empty:
        pd.DataFrame(columns=columns).to_csv(path, index=False)
        return
    work = df.copy()
    for c in columns:
        if c not in work.columns:
            work[c] = pd.NA
    work[columns].to_csv(path, index=False)


def norm(x: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(x or "").lower()).strip()


def col(x: Any) -> str:
    return norm(x).replace(" ", "")


def utcstamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_venue(x: str) -> str:
    return BRANDING.get(str(x), str(x))


def num(x: Any):
    return pd.to_numeric(x, errors="coerce")


def first(df: pd.DataFrame, *names: str) -> str | None:
    return next((n for n in names if n in df.columns), None)


def mascot(name: str) -> str:
    parts = norm(name).split()
    return parts[-1] if parts else ""


def mascot_variants(name: str) -> list[str]:
    base = mascot(name)
    variants = {base} if base else set()
    n = norm(name)
    for canon, aliases in MASCOT_ALIASES.items():
        if canon in n:
            variants.update(aliases)
    return [v for v in variants if v]


def clean_page(text: str) -> str:
    stripped = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    stripped = re.sub(r"<style\b[^>]*>.*?</style>", " ", stripped, flags=re.I | re.S)
    stripped = re.sub(r"<[^>]+>", " ", html.unescape(stripped))
    return re.sub(r"[^a-z0-9]+", " ", stripped.lower()).strip()


def c_to_f(tc: float) -> float:
    return tc * 9.0 / 5.0 + 32.0


def m_to_ft(m: float) -> float:
    return m * 3.28084


def kmh_to_mph(kmh: float) -> float:
    return kmh * 0.621371


def utc_to_est_str(iso_utc: str | None) -> str | None:
    if not iso_utc:
        return None
    try:
        dt = pd.to_datetime(iso_utc, utc=True).to_pydatetime()
        return dt.astimezone(EST).strftime("%Y-%m-%d %I:%M %p EST")
    except Exception:
        return None


def wind_park_relative_description(wind_out_to_cf_kmh: float, wind_cross_kmh: float) -> str:
    if wind_out_to_cf_kmh is None or (isinstance(wind_out_to_cf_kmh, float) and np.isnan(wind_out_to_cf_kmh)):
        return "N/A (enclosed / no external wind)"
    out_desc = "blowing out to center field" if wind_out_to_cf_kmh > 1 else ("blowing in from center field" if wind_out_to_cf_kmh < -1 else "negligible in/out component")
    cross_desc = ""
    if wind_cross_kmh is not None and not (isinstance(wind_cross_kmh, float) and np.isnan(wind_cross_kmh)):
        if wind_cross_kmh > 1:
            cross_desc = ", with a crosswind toward right field"
        elif wind_cross_kmh < -1:
            cross_desc = ", with a crosswind toward left field"
    return out_desc + cross_desc


def _key_positions(t: str, keys: list[str]) -> list[tuple[int, int, str]]:
    hits = []
    for key in [k for k in keys if k]:
        for m in re.finditer(re.escape(key), t):
            hits.append((m.start(), m.end(), key))
    return hits


def _split_into_cards(t: str) -> list[str]:
    marks = [m.start() for m in _CARD_BOUNDARY_RX.finditer(t)]
    if len(marks) < 2:
        return [t]
    marks.append(len(t))
    return [t[marks[i]:marks[i + 1]] for i in range(len(marks) - 1)]


def _card_windows(text: str, row: pd.Series, before: int, after: int, max_pair_gap: int = 150) -> list[str]:
    t = clean_page(text)
    home_keys = mascot_variants(row.home_team)
    away_keys = mascot_variants(row.away_team)

    cards = _split_into_cards(t)
    if len(cards) > 1:
        matched = [c for c in cards if _key_positions(c, home_keys) and _key_positions(c, away_keys)]
        if matched:
            return matched

    home_hits = _key_positions(t, home_keys)
    away_hits = _key_positions(t, away_keys)
    windows = []
    for hs, he, _ in home_hits:
        closest = min(away_hits, key=lambda a: abs(a[0] - hs), default=None) if away_hits else None
        if closest and abs(closest[0] - hs) <= max_pair_gap:
            as_, ae, _ = closest
            lo = min(hs, as_); hi = max(he, ae)
            windows.append(t[max(0, lo - before):hi + after])
    venue_key = norm(row.venuename)
    if not windows and venue_key:
        venue_hits = _key_positions(t, [venue_key])
        combined = home_hits + away_hits
        for vs, ve, _ in venue_hits:
            closest = min(combined, key=lambda h: abs(h[0] - vs), default=None) if combined else None
            if closest and abs(closest[0] - vs) <= max_pair_gap:
                hs, he, _ = closest
                lo = min(vs, hs); hi = max(ve, he)
                windows.append(t[max(0, lo - before):hi + after])
    if not windows and venue_key:
        for vs, ve, _ in _key_positions(t, [venue_key]):
            windows.append(t[max(0, vs - before):ve + after])
    return windows


def session() -> requests.Session:
    s = requests.Session()
    retries = Retry(total=5, connect=5, read=5, backoff_factor=0.8,
                     status_forcelist=[429, 500, 502, 503, 504], allowed_methods=frozenset(["GET"]))
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.headers.update({
        "User-Agent": f"Mozilla/5.0 MLB-Pitch-Environment-V{VERSION}",
        "Accept": "text/html,application/json,text/csv,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    })
    return s


def get_json(s: requests.Session, url: str, health: Health, code: str, params: dict[str, Any] | None = None, timeout: int = 45):
    try:
        r = s.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        health.add("WARN", code, url, f"{type(e).__name__}: {e}", "Continue with next fallback.")
        return None


def get_text(s: requests.Session, url: str, raw: Path, label: str, health: Health):
    try:
        r = s.get(url, timeout=45)
        r.raise_for_status()
        p = raw / f"{label}.html"
        p.write_text(r.text, encoding="utf-8")
        return r.text, p, r.status_code
    except Exception as e:
        health.add("WARN", "TEXT_FETCH_FAILED", label, f"{type(e).__name__}: {e}", "Source abstains.")
        return "", None, None


def load_parks(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path)
    d.columns = [col(x) for x in d.columns]
    need = {"venuename", "team", "lat", "lon", "ballparkelevationm", "rooftype", "homeplatebearingdeg"}
    miss = need - set(d.columns)
    if miss:
        raise ValueError(f"Park reference missing columns: {sorted(miss)}")
    for x in ("lat", "lon", "ballparkelevationm", "homeplatebearingdeg", "stationelevationm"):
        if x in d.columns:
            d[x] = num(d[x])
    d["roofclass"] = d["rooftype"].map(lambda x: norm(x).replace(" ", "").upper())
    valid = OUTDOOR_CLASSES | FIXED_CLASSES | RETRACTABLE_CLASSES
    bad = d.loc[~d["roofclass"].isin(valid), "rooftype"].tolist()
    if bad:
        raise ValueError(f"Unsupported roof classes: {bad}")
    d = d.rename(columns={"team": "park_home_team"})
    return d


def schedule(s: requests.Session, day: str, health: Health) -> pd.DataFrame:
    j = get_json(s, f"{MLB}/schedule", health, "SCHEDULE_FETCH_FAILED",
                 {"sportId": 1, "date": day, "hydrate": "probablePitcher,venue,team"})
    rows = []
    for date_block in (j or {}).get("dates", []):
        for game in date_block.get("games", []):
            if game.get("gameType") != "R":
                continue
            teams = game["teams"]
            for side in ("away", "home"):
                team = teams[side]
                p = team.get("probablePitcher") or {}
                rows.append({
                    "game_pk": game.get("gamePk"),
                    "game_datetime_utc": game.get("gameDate"),
                    "venue_name_api": game.get("venue", {}).get("name"),
                    "home_team": teams["home"]["team"].get("name"),
                    "away_team": teams["away"]["team"].get("name"),
                    "home_team_id": teams["home"]["team"].get("id"),
                    "away_team_id": teams["away"]["team"].get("id"),
                    "side": side,
                    "team": team["team"].get("name"),
                    "team_id": team["team"].get("id"),
                    "pitcher_id": p.get("id"),
                    "pitcher_name": p.get("fullName"),
                    "pitcher_resolution_method": "MLB_SCHEDULE" if p.get("id") else "UNRESOLVED",
                })
    return pd.DataFrame(rows)


def join_parks(games: pd.DataFrame, parks: pd.DataFrame, health: Health) -> pd.DataFrame:
    SCHEDULE_OWNED = ["game_pk", "game_datetime_utc", "venue_name_api", "home_team", "away_team",
                       "home_team_id", "away_team_id", "side", "team", "team_id",
                       "pitcher_id", "pitcher_name", "pitcher_resolution_method"]
    rows = []
    for _, g in games.iterrows():
        x = parks[parks.venuename.map(norm).eq(norm(canonical_venue(g.venue_name_api)))]
        method = "EXACT_VENUE_NAME"
        if len(x) != 1:
            x = parks[parks.park_home_team.map(norm).eq(norm(g.home_team))]
            method = "EXACT_HOME_TEAM_FALLBACK"
        if len(x) != 1:
            health.add("FATAL", "MISSING_VERIFIED_PARK", g.game_pk, f"venue={g.venue_name_api}; home={g.home_team}",
                        "Fix park reference or branding alias.")
            continue
        schedule_owned_values = {k: g[k] for k in SCHEDULE_OWNED}
        z = g.to_dict()
        z.update(x.iloc[0].to_dict())
        z.update(schedule_owned_values)
        z["park_join_method"] = method
        rows.append(z)
    return pd.DataFrame(rows)


def svp(tc: float) -> float:
    return 610.94 * math.exp(17.625 * tc / (tc + 243.04))


def density_terms(tc: float, rh: float, p_hpa: float) -> dict[str, float]:
    e = max(0.0, min(1.0, rh / 100.0)) * svp(tc)
    tk = tc + 273.15
    pa = p_hpa * 100.0
    pd_dry = pa - e
    rho_dry = pd_dry / (R_D * tk)
    rho_vapor = e / (R_V * tk)
    rho = rho_dry + rho_vapor
    return {
        "temperature_c": tc, "temperature_k": tk, "relative_humidity_pct": rh,
        "pressure_hpa": p_hpa, "pressure_pa": pa,
        "saturation_vapor_pressure_pa": svp(tc), "vapor_pressure_pa": e,
        "dry_air_partial_pressure_pa": pd_dry,
        "dry_air_density_kg_m3": rho_dry, "vapor_density_kg_m3": rho_vapor,
        "air_density_kg_m3": rho, "density_ratio_to_ref": rho / RHO_REF,
    }


def density(tc: float, rh: float, p_hpa: float) -> float:
    return density_terms(tc, rh, p_hpa)["air_density_kg_m3"]


def barometric_to_elevation(p_hpa: float, source_elev_m: float, venue_elev_m: float, tc: float) -> float:
    return p_hpa * math.exp(-G * (venue_elev_m - source_elev_m) / (R_D * (tc + 273.15)))


def wetbulb(tc: float, rh: float) -> float:
    return (tc * math.atan(.151977 * math.sqrt(rh + 8.313659)) + math.atan(tc + rh)
            - math.atan(rh - 1.676331) + .00391838 * rh ** 1.5 * math.atan(.023101 * rh) - 4.686035)


def ambient(s: requests.Session, row: pd.Series, raw: Path, health: Health) -> dict[str, Any]:
    q = {"latitude": row.lat, "longitude": row.lon,
         "hourly": "temperature_2m,relative_humidity_2m,surface_pressure,wind_speed_10m,wind_direction_10m",
         "timezone": "UTC", "forecast_days": 3}
    j = get_json(s, OPEN_METEO, health, "AMBIENT_FETCH_FAILED", q)
    if not j:
        return {}
    (raw / f"ambient_{row.game_pk}.json").write_text(json.dumps(j, indent=2), encoding="utf-8")
    h = j.get("hourly", {})
    ts = pd.to_datetime(h.get("time", []), utc=True)
    if len(ts) == 0:
        return {}
    i = int(np.argmin(abs(ts - pd.to_datetime(row.game_datetime_utc, utc=True))))
    try:
        tc = float(h["temperature_2m"][i]); rh = float(h["relative_humidity_2m"][i])
        p = float(h["surface_pressure"][i]); ws = float(h["wind_speed_10m"][i]); wd = float(h["wind_direction_10m"][i])
        rho = density(tc, rh, p)
        return {"ambient_temperature_c": tc, "ambient_relative_humidity_pct": rh,
                "ambient_surface_pressure_hpa": p, "ambient_wind_speed_kmh": ws, "ambient_wind_direction_deg": wd,
                "ambient_air_density_kg_m3": rho, "ambient_density_ratio_to_ref": rho / RHO_REF,
                "ambient_wetbulb_c": wetbulb(tc, rh), "forecast_valid_time_utc": str(ts[i]),
                "forecast_index_selected": i}
    except Exception as e:
        health.add("WARN", "AMBIENT_SCHEMA_INVALID", row.game_pk, str(e), "Continue with remaining games.")
        return {}


def wind_vectors(wind_speed_kmh: float, wind_direction_deg: float, homeplate_bearing_deg: float) -> dict[str, float]:
    rel = (float(wind_direction_deg) - float(homeplate_bearing_deg)) % 360.0
    rad = math.radians(rel)
    out_to_cf = wind_speed_kmh * math.cos(rad)
    cross = wind_speed_kmh * math.sin(rad)
    return {"wind_to_cf_angle_deg": rel, "wind_out_to_cf_kmh": out_to_cf, "wind_cross_kmh": cross}


def outdoor_branch(row: pd.Series, a: dict[str, Any]) -> dict[str, Any]:
    w = wind_vectors(a["ambient_wind_speed_kmh"], a["ambient_wind_direction_deg"], row.homeplatebearingdeg)
    return {
        "branch": "OUTDOOR", "air_environment_basis": "LOCAL_OUTDOOR_AMBIENT",
        "temperature_source": "OPEN_METEO_FORECAST", "pressure_source": "OPEN_METEO_SURFACE_PRESSURE",
        "temperature_c": a["ambient_temperature_c"], "relative_humidity_pct": a["ambient_relative_humidity_pct"],
        "pressure_hpa": a["ambient_surface_pressure_hpa"], "air_density_kg_m3": a["ambient_air_density_kg_m3"],
        "density_ratio_to_ref": a["ambient_density_ratio_to_ref"], "density_uncertainty_kg_m3": 0.01,
        "external_wind_speed_kmh": a["ambient_wind_speed_kmh"], "external_wind_direction_deg": a["ambient_wind_direction_deg"],
        **w, "wind_model_status": "APPLICABLE_OUTDOOR",
        "forecast_valid_time_utc": a.get("forecast_valid_time_utc"),
    }


def enclosed_branch(row: pd.Series, a: dict[str, Any]) -> dict[str, Any]:
    tc, rh = a["ambient_temperature_c"], a["ambient_relative_humidity_pct"]
    src = float(row.stationelevationm) if "stationelevationm" in row.index and pd.notna(row.get("stationelevationm")) else float(row.ballparkelevationm)
    p = barometric_to_elevation(a["ambient_surface_pressure_hpa"], src, float(row.ballparkelevationm), tc)
    rho = density(tc, rh, p)
    return {
        "branch": "ENCLOSED", "air_environment_basis": "INDOOR_MODELED_PRESSURE_ANCHORED",
        "temperature_source": "OUTDOOR_AMBIENT_PROXY_HVAC_UNMEASURED",
        "pressure_source": "LOCAL_BAROMETRY_ELEVATION_CORRECTED",
        "temperature_c": tc, "relative_humidity_pct": rh, "pressure_hpa": p, "air_density_kg_m3": rho,
        "density_ratio_to_ref": rho / RHO_REF, "density_uncertainty_kg_m3": 0.02,
        "external_wind_speed_kmh": 0.0, "external_wind_direction_deg": np.nan,
        "wind_to_cf_angle_deg": np.nan, "wind_out_to_cf_kmh": 0.0, "wind_cross_kmh": 0.0,
        "wind_model_status": "ZERO_EXTERNAL_WIND_ENCLOSED",
        "forecast_valid_time_utc": a.get("forecast_valid_time_utc"),
    }


def vote_rotowire(text: str, row: pd.Series) -> tuple[str | None, str, float]:
    t = clean_page(text)
    windows = _card_windows(text, row, 700, 1900)
    votes = []
    found_identity = bool(windows)
    for card in windows:
        o, c = bool(OPEN_RX.search(card)), bool(CLOSED_RX.search(card))
        if o ^ c:
            votes.append("OPEN" if o else "CLOSED")
    if votes and len(set(votes)) == 1:
        boiler = BOILERPLATE_DOME_RX.search(t)
        return votes[0], ("ROTOWIRE_EXPLICIT_NARRATIVE_OVERRIDES_BOILERPLATE" if boiler else "ROTOWIRE_EXPLICIT_NARRATIVE"), (0.95 if boiler else 0.9)
    if found_identity:
        return None, "ROTOWIRE_IDENTITY_FOUND_NO_UNAMBIGUOUS_LANGUAGE", 0.0
    if ROOF_KEYWORD_RX.search(t):
        return None, "ROTOWIRE_NO_IDENTITY_MATCH_ROOF_LANGUAGE_ELSEWHERE", 0.0
    return None, "ROTOWIRE_NO_ROOF_LANGUAGE", 0.0


def vote_sportspredict(text: str, row: pd.Series) -> tuple[str | None, str, float]:
    for card in _card_windows(text, row, 260, 700):
        if _ROOF_LIKELY_CLOSED_RX.search(card):
            return "CLOSED", "SPORTSPREDICT_EXPLICIT_CLOSED_OR_LIKELY_CLOSED", 0.85
        if re.search(r"\broof\s+likely\s+open\b|\broof\s+open\b", card, re.I):
            return "OPEN", "SPORTSPREDICT_EXPLICIT_OPEN_OR_LIKELY_OPEN", 0.85
        if _WEATHER_DATA_ANCHOR_RX.search(card):
            return "OPEN", "SPORTSPREDICT_WEATHER_SHOWN_NO_CLOSED_TAG_IMPLIES_OPEN", 0.6
    return None, "SPORTSPREDICT_NO_STRUCTURED_MATCH", 0.0


def vote_weathermlb(text: str, row: pd.Series) -> tuple[str | None, str, float]:
    for card in _card_windows(text, row, 260, 700):
        if re.search(r"\broof\s+closed\b", card, re.I):
            return "CLOSED", "WEATHERMLB_EXPLICIT_ROOF_CLOSED_TAG", 0.85
        if _WEATHER_DATA_ANCHOR_RX.search(card) and not re.search(r"\broof\s+closed\b", card, re.I):
            return "OPEN", "WEATHERMLB_WEATHER_SHOWN_NO_CLOSED_TAG_IMPLIES_OPEN_LIKELY", 0.6
    return None, "WEATHERMLB_NO_STRUCTURED_MATCH", 0.0


def vote_simple(text: str, row: pd.Series) -> tuple[str | None, str, float]:
    t = clean_page(text)
    if re.search(r"\btbd\b|\bpending\b|\bunknown\b", t, re.I) and not (re.search(r"\bopen\b", t, re.I) or re.search(r"\bclosed\b", t, re.I)):
        return None, "DEDICATED_SINGLE_VENUE_TBD_NOT_A_VOTE", 0.0
    if re.search(r"\broof\s+open\b|\bstatus\s+open\b", t, re.I) and not re.search(r"\broof\s+closed\b|\bstatus\s+closed\b", t, re.I):
        return "OPEN", "DEDICATED_SINGLE_VENUE_EXPLICIT", 0.95
    if re.search(r"\broof\s+closed\b|\bstatus\s+closed\b", t, re.I) and not re.search(r"\broof\s+open\b|\bstatus\s+open\b", t, re.I):
        return "CLOSED", "DEDICATED_SINGLE_VENUE_EXPLICIT", 0.95
    return None, "DEDICATED_SINGLE_VENUE_NO_UNAMBIGUOUS_STATE", 0.0


def parse_roof(text: str, row: pd.Series, mode: str) -> tuple[str | None, str, float]:
    if mode == "ROTOWIRE_PROSE":
        return vote_rotowire(text, row)
    if mode == "SPORTSPREDICT":
        return vote_sportspredict(text, row)
    if mode == "WEATHERMLB":
        return vote_weathermlb(text, row)
    if mode == "SIMPLE":
        return vote_simple(text, row)
    return None, "UNSUPPORTED_MODE", 0.0


def resolve_roof_state(s: requests.Session, row: pd.Series, raw: Path, health: Health, cache: dict[str, Any],
                        evidence_rows: list[dict[str, Any]], conflict_rows: list[dict[str, Any]]):
    if row.roofclass in OUTDOOR_CLASSES:
        return "NOT_APPLICABLE", "STRUCTURAL_OUTDOOR", {}
    if row.roofclass in FIXED_CLASSES:
        return "FIXED_CLOSED", "STRUCTURAL_FIXED_ENCLOSED", {}

    dedicated = VENUE_DEDICATED_SOURCES.get(row.venuename, {})
    all_sources = {**ROOF_GENERIC_SOURCES, **dedicated}
    votes: dict[str, Any] = {}
    for source, meta in all_sources.items():
        if meta["url"] not in cache:
            cache[meta["url"]] = get_text(s, meta["url"], raw, f"roof_{norm(source)}", health)
        text, p, http_status = cache[meta["url"]]
        vote, status, conf = parse_roof(text, row, meta["mode"])
        votes[source] = vote
        evidence_rows.append({
            "game_pk": row.game_pk, "venue_name": row.venuename, "source": source,
            "scope": "SINGLE_VENUE" if source in dedicated else "MULTI_GAME",
            "url": meta["url"], "vote": vote, "parser_status": status, "confidence": conf,
            "http_status": http_status, "page_char_length": len(text or ""),
            "roof_keyword_found": bool(ROOF_KEYWORD_RX.search(clean_page(text or ""))),
            "raw_file": str(p) if p else None,
        })

    dedicated_votes = {k: v for k, v in votes.items() if k in dedicated and v}
    generic_votes = {k: v for k, v in votes.items() if k in ROOF_GENERIC_SOURCES and v}
    all_nonnull = {**dedicated_votes, **generic_votes}
    distinct = set(all_nonnull.values())
    has_conflict = len(distinct) > 1

    conflict_rows.append({
        "game_pk": row.game_pk, "venue_name": row.venuename,
        "dedicated_votes_json": json.dumps(dedicated_votes, sort_keys=True),
        "generic_votes_json": json.dumps(generic_votes, sort_keys=True),
        "has_conflict": has_conflict, "resolution": None,
    })
    conflict_rec = conflict_rows[-1]

    if dedicated_votes and len(set(dedicated_votes.values())) == 1 and not (has_conflict and generic_votes):
        state = next(iter(dedicated_votes.values()))
        conflict_rec["resolution"] = "DEDICATED_SINGLE_VENUE_SOURCE_DECISIVE"
        return state, "DEDICATED_SINGLE_VENUE_SOURCE_DECISIVE", votes
    if dedicated_votes and generic_votes and has_conflict:
        health.add("WARN", "ROOF_SOURCE_CONFLICT", row.game_pk, f"dedicated={dedicated_votes} generic={generic_votes}",
                    "Dedicated and generic sources disagree; modeling both branches.")
        conflict_rec["resolution"] = "CONFLICTED_DUAL_BRANCH"
        return "UNRESOLVED_RETRACTABLE_ONLY", "SOURCE_CONFLICT_DUAL_BRANCH", votes

    generic_vals = list(generic_votes.values())
    if generic_vals.count("OPEN") >= 2 and generic_vals.count("CLOSED") == 0:
        conflict_rec["resolution"] = "TWO_GENERIC_SOURCE_CONSENSUS"
        return "OPEN", "TWO_GENERIC_SOURCE_CONSENSUS", votes
    if generic_vals.count("CLOSED") >= 2 and generic_vals.count("OPEN") == 0:
        conflict_rec["resolution"] = "TWO_GENERIC_SOURCE_CONSENSUS"
        return "CLOSED", "TWO_GENERIC_SOURCE_CONSENSUS", votes
    if len(all_nonnull) == 1:
        health.add("INFO", "ROOF_SINGLE_SOURCE_ONLY", row.game_pk, f"votes={votes}",
                    "Only one source voted; modeling both branches instead of forcing a decision.")
        conflict_rec["resolution"] = "SINGLE_SOURCE_INSUFFICIENT_QUORUM_DUAL_BRANCH"
        return "UNRESOLVED_RETRACTABLE_ONLY", "SINGLE_SOURCE_INSUFFICIENT_QUORUM", votes

    health.add("INFO", "RETRACTABLE_ROOF_CONDITIONAL", row.game_pk, f"votes={votes}",
                "No decisive quorum; modeling OUTDOOR and ENCLOSED scenario branches.")
    conflict_rec["resolution"] = "NO_DECISIVE_SOURCE_TWO_BRANCH_MODEL"
    return "UNRESOLVED_RETRACTABLE_ONLY", "NO_DECISIVE_SOURCE_TWO_BRANCH_MODEL", votes


def extract_probable_from_live_feed(js: dict[str, Any], side: str) -> tuple[Any, Any, Any]:
    gd = (js or {}).get("gameData", {})
    side_block = ((gd.get("teams", {}) or {}).get(side) or {}) if isinstance(gd, dict) else {}
    p = side_block.get("probablePitcher") or {}
    if p.get("id"):
        return p.get("id"), p.get("fullName"), "MLB_GAME_FEED"
    if p.get("fullName"):
        return None, p.get("fullName"), "MLB_GAME_FEED_NAME_ONLY"
    return None, None, None


def extract_player_pool_from_live_feed(js: dict[str, Any]) -> list[dict[str, Any]]:
    gd = (js or {}).get("gameData", {})
    players = gd.get("players", {}) if isinstance(gd, dict) else {}
    pool = []
    if isinstance(players, dict):
        for v in players.values():
            if not isinstance(v, dict):
                continue
            pool.append({"id": v.get("id"), "fullName": v.get("fullName"),
                         "primaryPosition": ((v.get("primaryPosition") or {}).get("code") if isinstance(v.get("primaryPosition"), dict) else None)})
    return pool


def candidate_team_pitchers_from_live_feed(js: dict[str, Any], team_id: Any) -> list[dict[str, Any]]:
    pool = extract_player_pool_from_live_feed(js)
    out = []
    box = (js or {}).get("liveData", {}).get("boxscore", {}).get("teams", {})
    for side in ("away", "home"):
        block = box.get(side, {}) if isinstance(box, dict) else {}
        if str(((block.get("team") or {}).get("id"))) != str(team_id):
            continue
        for pid in (block.get("pitchers") or []):
            hit = next((p for p in pool if str(p.get("id")) == str(pid)), None)
            if hit:
                out.append(hit)
    return out


def search_people_exact(s: requests.Session, full_name: str) -> list[dict[str, Any]]:
    hits = []
    for endpoint, params in [(f"{MLB}/people/search", {"names": full_name}),
                              (f"{MLB}/sports/1/players", {"season": date.today().year})]:
        try:
            r = s.get(endpoint, params=params, timeout=25)
            r.raise_for_status()
            js = r.json()
            people = js.get("people") or []
            if isinstance(people, list):
                hits.extend([p for p in people if norm(p.get("fullName")) == norm(full_name)])
        except Exception:
            continue
    unique = {}
    for h in hits:
        if h.get("id") is not None:
            unique[h["id"]] = h
    return list(unique.values())


def official_probables_text(s: requests.Session, day: str, raw: Path, health: Health) -> str:
    text, _, _ = get_text(s, f"https://www.mlb.com/probable-pitchers/{day}", raw, f"official_probables_{day}", health)
    return text


def infer_name_candidates_from_text(text: str, row: pd.Series) -> list[str]:
    t = clean_page(text)
    keys = [norm(row.home_team), norm(row.away_team), norm(row.venuename), mascot(row.home_team), mascot(row.away_team)]
    found = []
    for key in [k for k in keys if k]:
        for m in re.finditer(re.escape(key), t):
            card = t[max(0, m.start() - 350):m.start() + 1200]
            if norm(row.home_team) not in card or norm(row.away_team) not in card:
                continue
            tokens = re.findall(r"\b[a-z]+\s+[a-z]+\b", card)
            for tok in tokens:
                if tok in {norm(row.home_team), norm(row.away_team), norm(row.venuename), mascot(row.home_team), mascot(row.away_team)}:
                    continue
                if len(tok.split()) != 2:
                    continue
                name = " ".join(x.capitalize() for x in tok.split())
                if name not in found:
                    found.append(name)
    return found[:10]


def resolve_starters(s: requests.Session, joined: pd.DataFrame, raw: Path, health: Health) -> pd.DataFrame:
    out = joined.copy()
    official = official_probables_text(s, str(pd.to_datetime(out.game_datetime_utc.iloc[0]).date()), raw, health) if not out.empty else ""
    weather_html, _, _ = get_text(s, "https://weathermlb.com/", raw, "weathermlb_probables", health)
    roto_html, _, _ = get_text(s, "https://www.rotowire.com/baseball/weather.php", raw, "rotowire_probables", health)
    public_text = "\n".join([official, weather_html, roto_html])

    live_feed_cache: dict[int, dict[str, Any]] = {}
    for game_pk, block in out.groupby("game_pk"):
        js = get_json(s, f"{MLB}.1/game/{int(game_pk)}/feed/live", health, "GAME_FEED_FETCH_FAILED") or {}
        live_feed_cache[int(game_pk)] = js
        for idx, row in block.iterrows():
            if pd.notna(row.pitcher_id) and str(row.pitcher_name or "").strip():
                continue
            pid, pname, method = extract_probable_from_live_feed(js, row.side)
            if pid is not None:
                out.at[idx, "pitcher_id"] = pid
                out.at[idx, "pitcher_name"] = pname
                out.at[idx, "pitcher_resolution_method"] = method
            elif pname:
                exact = search_people_exact(s, pname)
                if exact:
                    out.at[idx, "pitcher_id"] = exact[0]["id"]
                    out.at[idx, "pitcher_name"] = pname
                    out.at[idx, "pitcher_resolution_method"] = "MLB_GAME_FEED_NAME_RESOLVED"
                else:
                    out.at[idx, "pitcher_name"] = pname
                    out.at[idx, "pitcher_resolution_method"] = method

    unresolved = out[out.pitcher_id.isna()].copy()
    for idx, row in unresolved.iterrows():
        js = live_feed_cache.get(int(row.game_pk), {})
        candidates = []
        for name in infer_name_candidates_from_text(public_text, row):
            exact = search_people_exact(s, name)
            for hit in exact:
                pos = (hit.get("primaryPosition") or {}).get("code") if isinstance(hit.get("primaryPosition"), dict) else hit.get("primaryPosition")
                if str(pos) in {"1", None, "P"}:
                    candidates.append({"id": hit.get("id"), "fullName": hit.get("fullName")})
        team_pitchers = candidate_team_pitchers_from_live_feed(js, row.team_id)
        by_id = {}
        for c in candidates + team_pitchers:
            if c.get("id") is not None:
                by_id[c["id"]] = c
        shortlisted = list(by_id.values())
        if len(shortlisted) == 1:
            out.at[idx, "pitcher_id"] = shortlisted[0]["id"]
            out.at[idx, "pitcher_name"] = shortlisted[0].get("fullName")
            out.at[idx, "pitcher_resolution_method"] = "PUBLIC_PLUS_LIVE_FEED_SINGLE_CANDIDATE"
        elif len(shortlisted) > 1:
            named = [c for c in shortlisted if norm(c.get("fullName")) == norm(row.pitcher_name)] if str(row.pitcher_name or "").strip() else []
            if len(named) == 1:
                out.at[idx, "pitcher_id"] = named[0]["id"]
                out.at[idx, "pitcher_name"] = named[0].get("fullName")
                out.at[idx, "pitcher_resolution_method"] = "SHORTLIST_NAME_MATCH"

    for _, r in out[out.pitcher_id.isna()].iterrows():
        health.add("WARN", "STARTER_STILL_PENDING_FROM_UPSTREAM", r.game_pk,
                    f"{r.team} starter not yet fully published by upstream sources.",
                    "Environment rows remain valid; pitch-level model skipped for this side until upstream starter appears.")
    return out


def savant_query(s: requests.Session, pitcher_id: int, start: str, end_exclusive: str, raw: Path, health: Health,
                  query_rows: list[dict[str, Any]], label: str) -> pd.DataFrame:
    q = {"type": "details", "player_type": "pitcher", "player_id": int(pitcher_id),
         "game_date_gt": start, "game_date_lt": end_exclusive, "min_pitches": 0, "min_results": 0}
    audit = {"pitcher_id": int(pitcher_id), "query_start": start, "query_end": end_exclusive,
              "window_label": label, "url": SAVANT, "request_params_json": json.dumps(q, sort_keys=True)}
    try:
        r = s.get(SAVANT, params=q, timeout=90)
        r.raise_for_status()
        p = raw / f"savant_{pitcher_id}_{label}.csv"
        p.write_bytes(r.content)
        d = pd.read_csv(p)
        d.columns = [norm(x).replace(" ", "_") for x in d.columns]
        idcol = first(d, "pitcher", "pitcher_id", "player_id")
        if not idcol:
            raise ValueError("No pitcher id column in Savant CSV")
        scoped = d[num(d[idcol]).eq(int(pitcher_id))].copy()
        audit.update({"http_status": r.status_code, "raw_file": str(p), "rows_returned": len(d),
                       "id_column": idcol, "rows_scoped": len(scoped), "scope_pass": len(scoped) > 0})
        query_rows.append(audit)
        return scoped
    except Exception as e:
        audit.update({"error": f"{type(e).__name__}: {e}", "scope_pass": False})
        query_rows.append(audit)
        health.add("WARN", "SAVANT_FETCH_FAILED", pitcher_id, str(e), "Skip pitch-level model for this pitcher only.")
        return pd.DataFrame()


def summarize_window(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    pitch_col = first(df, "pitch_name", "pitch_type", "pitch")
    if not pitch_col:
        return pd.DataFrame()
    df = df[df[pitch_col].notna() & (df[pitch_col].astype(str).str.strip() != "")].copy()
    if df.empty:
        return pd.DataFrame()
    keep_num = [c for c in ["release_speed", "effective_speed", "release_spin_rate", "spin_axis", "active_spin",
                             "active_spin_pct", "spin_efficiency", "pfx_x", "pfx_z", "release_extension"] if c in df.columns]
    for c in keep_num:
        df[c] = num(df[c])
    grp = df.groupby(pitch_col, dropna=True)
    out = grp.size().rename("pitches").to_frame().reset_index().rename(columns={pitch_col: "pitch_type"})
    for c in keep_num:
        out[c] = grp[c].mean().values
    total = out.pitches.sum()
    out["usage_pct"] = 100 * out.pitches / total if total else np.nan
    return out.sort_values("pitches", ascending=False).reset_index(drop=True)


def weighted_arsenal(season_df: pd.DataFrame, recent_df: pd.DataFrame, recent_weight: float = 0.65) -> pd.DataFrame:
    if season_df.empty and recent_df.empty:
        return pd.DataFrame()
    if season_df.empty:
        out = recent_df.copy(); out["window_basis"] = "RECENT_ONLY"
        out = out[out["pitch_type"].notna() & (out["pitch_type"].astype(str).str.strip().ne("")) & (out["pitch_type"].astype(str) != "0")]
        return out.sort_values("pitches", ascending=False).reset_index(drop=True)
    if recent_df.empty:
        out = season_df.copy(); out["window_basis"] = "SEASON_ONLY"
        out = out[out["pitch_type"].notna() & (out["pitch_type"].astype(str).str.strip().ne("")) & (out["pitch_type"].astype(str) != "0")]
        return out.sort_values("pitches", ascending=False).reset_index(drop=True)
    m = season_df.merge(recent_df, on="pitch_type", how="outer", suffixes=("_season", "_recent"))
    metric_cols = [c for c in m.columns if c != "pitch_type"]
    m[metric_cols] = m[metric_cols].fillna(0)
    out = pd.DataFrame({"pitch_type": m.pitch_type})
    for metric in ["pitches", "release_speed", "effective_speed", "release_spin_rate", "spin_axis", "active_spin",
                    "active_spin_pct", "spin_efficiency", "pfx_x", "pfx_z", "release_extension", "usage_pct"]:
        s_col, r_col = f"{metric}_season", f"{metric}_recent"
        if s_col not in m.columns and r_col not in m.columns:
            continue
        sv = m[s_col] if s_col in m.columns else 0
        rv = m[r_col] if r_col in m.columns else 0
        out[metric] = (sv + rv) if metric == "pitches" else ((1 - recent_weight) * sv + recent_weight * rv)
    out["window_basis"] = "RECENT_65_SEASON_35"
    out = out[out["pitch_type"].notna() & (out["pitch_type"].astype(str).str.strip().ne("")) & (out["pitch_type"].astype(str) != "0")]
    return out.sort_values("pitches", ascending=False).reset_index(drop=True)


def apply_environment_physics(arsenal: pd.DataFrame, branch: dict[str, Any]) -> pd.DataFrame:
    if arsenal.empty:
        return arsenal
    out = arsenal.copy()
    ratio = float(branch["density_ratio_to_ref"])
    wind_out = float(branch.get("wind_out_to_cf_kmh", 0) or 0)
    out["density_ratio_to_ref"] = ratio
    out["density_only_movement_change_pct"] = (ratio - 1) * 100
    if "pfx_x" in out.columns:
        out["projected_pfx_x"] = out["pfx_x"] * ratio
        out["delta_pfx_x_inches"] = (out["projected_pfx_x"] - out["pfx_x"]) * 12
    if "pfx_z" in out.columns:
        out["projected_pfx_z"] = out["pfx_z"] * ratio
        out["delta_pfx_z_inches"] = (out["projected_pfx_z"] - out["pfx_z"]) * 12
    if "release_speed" in out.columns:
        out["carry_wind_adjustment_ft_400ft"] = (wind_out / 10.0) * 3.0
        out["carry_density_adjustment_ft_400ft"] = (1 - ratio) * 5.2
        out["effective_plate_speed_delta_mph"] = (1 - ratio) * out["release_speed"] * 0.18
    active_col = "active_spin_pct" if "active_spin_pct" in out.columns else ("spin_efficiency" if "spin_efficiency" in out.columns else None)
    if active_col:
        out["active_spin_environment_index"] = out[active_col] * ratio
    out["model_label"] = "FIRST_ORDER_ENVIRONMENTAL_SENSITIVITY"
    return out


def decompose_pitch_effects(physics_row: dict[str, Any], branch: dict[str, Any]) -> dict[str, Any]:
    ratio = float(branch.get("density_ratio_to_ref", 1.0))
    wind_out = float(branch.get("wind_out_to_cf_kmh", 0) or 0)
    density_effect = round((1 - ratio) * 5.2, 4)
    wind_effect = round((wind_out / 10.0) * 3.0, 4)
    roof_effect = round(0.0 if branch.get("branch") == "OUTDOOR" else (density_effect * 0.15), 4)
    return {
        "game_pk": physics_row.get("game_pk"), "pitcher_id": physics_row.get("pitcher_id"),
        "pitch_type": physics_row.get("pitch_type"), "branch": physics_row.get("branch"),
        "density_ratio_to_ref": ratio, "wind_out_to_cf_kmh": wind_out,
        "density_effect": density_effect, "wind_effect": wind_effect, "roof_branch_effect": roof_effect,
        "total_carry_effect_ft_400ft": round(density_effect + wind_effect + roof_effect, 4),
    }


def autodetect_park_reference() -> Path:
    candidates = [
        Path(__file__).resolve().parent / "mlb_park_reference_full_corrected_v3.csv",
        Path.cwd() / "mlb_park_reference_full_corrected_v3.csv",
        Path(__file__).resolve().parent / "mlb_park_reference_verified.csv",
        Path.cwd() / "mlb_park_reference_verified.csv",
    ]
    hit = next((p for p in candidates if p.exists()), None)
    if not hit:
        raise FileNotFoundError("Park reference CSV not found beside the script or in the current working directory.")
    return hit


def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


GLOSSARY: list[dict[str, str]] = [
    {"section": "game_environment_audit", "field": "roof_state", "meaning": "Resolved roof state for the game branch.", "derivation": "resolve_roof_state() quorum/dedicated-source logic", "unit": "categorical"},
    {"section": "game_environment_audit", "field": "air_density_kg_m3", "meaning": "True moist-air density at the venue for this branch: mass of air (including water vapor) per cubic meter. Lower density = less aerodynamic drag = pitches move less and batted balls carry farther.", "derivation": "air_density_kg_m3 = dry_air_density_kg_m3 + vapor_density_kg_m3, from the ideal-gas mixture equation using temperature, pressure, and humidity", "unit": "kg/m3 (sea-level reference ~1.225 kg/m3 at 15C/1013.25hPa)"},
    {"section": "game_environment_audit", "field": "density_ratio_to_ref", "meaning": "This branch's air density divided by the fixed reference density RHO_REF=1.2041 kg/m3. Below 1.0 = thinner air (more carry, less pitch movement); above 1.0 = denser air.", "derivation": "air_density_kg_m3 / 1.2041", "unit": "ratio (dimensionless)"},
    {"section": "game_environment_audit", "field": "ballparkelevationm", "meaning": "Ballpark elevation above sea level.", "derivation": "park reference CSV", "unit": "m (see elevation_ft for feet)"},
    {"section": "game_environment_audit", "field": "home_plate_bearing_deg", "meaning": "Compass bearing from home plate toward center field.", "derivation": "park reference CSV", "unit": "deg"},
    {"section": "density_calculation_audit", "field": "vapor_pressure_pa", "meaning": "Partial pressure of water vapor in the air.", "derivation": "relative_humidity_pct/100 * saturation_vapor_pressure_pa", "unit": "Pa"},
    {"section": "density_calculation_audit", "field": "dry_air_partial_pressure_pa", "meaning": "Total atmospheric pressure minus vapor pressure -- the pressure contributed by dry air alone.", "derivation": "pressure_pa - vapor_pressure_pa", "unit": "Pa"},
    {"section": "roof_conflict_audit", "field": "has_conflict", "meaning": "True when dedicated and generic roof sources disagree.", "derivation": "distinct vote count across all non-null sources > 1", "unit": "boolean"},
    {"section": "pitch_physics_environment", "field": "pfx_x", "meaning": "Horizontal movement of the pitch as it crosses the plate, from the catcher's perspective (pitcher-specific Statcast baseline, before environment adjustment). Displayed as 'H-Break'.", "derivation": "Statcast/Baseball Savant pfx_x field, averaged over the recent-30d/season-weighted window", "unit": "inches"},
    {"section": "pitch_physics_environment", "field": "pfx_z", "meaning": "Vertical movement of the pitch as it crosses the plate (pitcher-specific Statcast baseline). Displayed as 'V-Break'.", "derivation": "Statcast/Baseball Savant pfx_z field, averaged over the recent-30d/season-weighted window", "unit": "inches"},
    {"section": "pitch_physics_environment", "field": "delta_pfx_x_inches", "meaning": "How much THIS pitch type's horizontal break is projected to change in tonight's actual air density vs. its normal baseline. This varies per pitch type because it depends on that pitch's own pfx_x.", "derivation": "(pfx_x * density_ratio_to_ref) - pfx_x, in inches", "unit": "inches"},
    {"section": "pitch_physics_environment", "field": "delta_pfx_z_inches", "meaning": "How much THIS pitch type's vertical break is projected to change in tonight's actual air density vs. its normal baseline. Varies per pitch type.", "derivation": "(pfx_z * density_ratio_to_ref) - pfx_z, in inches", "unit": "inches"},
    {"section": "pitch_physics_environment", "field": "active_spin_pct", "meaning": "Percentage of total spin contributing to Magnus-force movement (vs. gyro/bullet spin, which doesn't move the ball). Higher = more movement per unit of raw spin rate.", "derivation": "Statcast/Baseball Savant active_spin_pct field", "unit": "%"},
    {"section": "pitch_physics_environment", "field": "active_spin_environment_index", "meaning": "Active spin scaled by this branch's density ratio -- estimate of how Magnus-driven movement efficiency changes with tonight's air density.", "derivation": "active_spin_pct * density_ratio_to_ref", "unit": "index"},
    {"section": "pitch_physics_environment", "field": "release_spin_rate", "meaning": "Raw spin rate at release, regardless of axis/efficiency.", "derivation": "Statcast/Baseball Savant release_spin_rate field", "unit": "rpm"},
    {"section": "pitch_physics_environment", "field": "effective_plate_speed_delta_mph", "meaning": "Change in effective velocity at the plate attributable to tonight's air density (thinner air = less drag = ball arrives faster).", "derivation": "(1 - density_ratio_to_ref) * release_speed * 0.18", "unit": "mph"},
    {"section": "pitch_effect_decomposition", "field": "density_effect", "meaning": "Carry-distance change attributable to air density alone. Shared across all pitchers/pitch types in the same game+branch because it depends only on environment, not the pitcher.", "derivation": "(1 - density_ratio_to_ref) * 5.2", "unit": "ft per 400ft batted ball"},
    {"section": "pitch_effect_decomposition", "field": "wind_effect", "meaning": "Carry-distance change attributable to the wind component toward/away from center field. Shared across all pitchers/pitch types in the same game+branch.", "derivation": "(wind_out_to_cf_kmh / 10.0) * 3.0", "unit": "ft per 400ft"},
    {"section": "pitch_effect_decomposition", "field": "roof_branch_effect", "meaning": "Secondary carry effect attributable to enclosed-branch conditions.", "derivation": "0 for OUTDOOR branch; 15% of density_effect for ENCLOSED branch", "unit": "ft per 400ft"},
    {"section": "run_health_and_model_diagnostics", "field": "status", "meaning": "PASS / WARN / INFO indicator per diagnostic check.", "derivation": "main() run summary", "unit": "categorical"},
    {"section": "findings", "field": "severity", "meaning": "FATAL stops the run; WARN/INFO are non-blocking but must be surfaced.", "derivation": "Health.add()", "unit": "categorical"},
    {"section": "roof_evidence_audit", "field": "N/A for OUTDOOR/FIXED parks", "meaning": "Roof evidence rows are only produced for RETRACTABLE-roof venues. Outdoor and fixed-enclosed venues never need roof-source scraping, so absence here is correct, not a bug.", "derivation": "resolve_roof_state() structural short-circuit", "unit": "n/a"},
    {"section": "game_environment_audit", "field": "temperature_f", "meaning": "Temperature converted to Fahrenheit for display.", "derivation": "temperature_c * 9/5 + 32", "unit": "F"},
    {"section": "game_environment_audit", "field": "elevation_ft", "meaning": "Ballpark elevation converted to feet for display.", "derivation": "ballparkelevationm * 3.28084", "unit": "ft"},
    {"section": "game_environment_audit", "field": "wind_speed_mph", "meaning": "External wind speed converted to miles per hour for display.", "derivation": "external_wind_speed_kmh * 0.621371", "unit": "mph"},
    {"section": "game_environment_audit", "field": "wind_park_relative_description", "meaning": "Plain-language description of wind direction relative to the ballpark.", "derivation": "Derived from wind_out_to_cf_kmh and wind_cross_kmh sign/magnitude", "unit": "text"},
    {"section": "game_environment_audit", "field": "game_datetime_est", "meaning": "Game start time converted to US Eastern time for display.", "derivation": "game_datetime_utc converted to fixed UTC-5 offset", "unit": "EST"},
]


def validate_environment_rows(game_env: pd.DataFrame, health: Health) -> None:
    if game_env.empty:
        health.add("FATAL", "NO_ENVIRONMENT_ROWS", "run", "No game environment rows were emitted.", "Stop the run.")
        return
    if ((game_env.roofclass == "OUTDOOR") & (game_env.roof_state != "NOT_APPLICABLE")).any():
        health.add("FATAL", "OUTDOOR_ROOF_STATE_VIOLATION", "run", "Outdoor venue carried illegal roof state.", "Fix roof branching.")
    if ((game_env.roofclass.str.contains("FIXED", na=False)) & (game_env.branch != "ENCLOSED")).any():
        health.add("FATAL", "FIXED_ROOF_BRANCH_VIOLATION", "run", "Fixed roof venue emitted non-enclosed branch.", "Fix branching.")


def validate_team_assignment(joined: pd.DataFrame, health: Health) -> None:
    bad_games = []
    for game_pk, block in joined.groupby("game_pk"):
        teams = block[["side", "team"]].drop_duplicates()
        if len(teams) == 2 and teams["team"].nunique() == 1:
            bad_games.append(int(game_pk))
    if bad_games:
        health.add("FATAL", "TEAM_ASSIGNMENT_COLLISION", "run",
                    f"Games with identical home/away team labels: {bad_games}",
                    "join_parks() team-preservation regression -- do not trust pitcher_query outputs for these games.")


def copy_dashboard_and_glossary(run_dir: Path, script_dir: Path) -> None:
    src = script_dir / "mlb-pitch-environment-live-dashboard-V52.html"
    if src.exists():
        shutil.copy2(src, run_dir / "mlb-pitch-environment-live-dashboard-V52.html")
    (run_dir / "V52_IN_APP_GLOSSARY.json").write_text(json.dumps(GLOSSARY, indent=2), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=str(date.today()))
    ap.add_argument("--park-reference", default=None)
    ap.add_argument("--output-root", default="mlb_daily_outputs")
    ap.add_argument("--open-dashboard", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    health = Health()
    s = session()
    script_dir = Path(__file__).resolve().parent
    park_path = Path(args.park_reference) if args.park_reference else autodetect_park_reference()
    run_dir = ensure_dir(Path(args.output_root) / f"{args.date}_{utcstamp()}")
    raw = ensure_dir(run_dir / "raw")

    parks = load_parks(park_path)
    games = schedule(s, args.date, health)
    if games.empty:
        health.add("FATAL", "NO_SCHEDULED_GAMES", args.date, "No regular-season games returned for the requested date.", "Stop the run.")
        write_csv_contract(health.frame(), run_dir / "findings.csv", Health.COLUMNS)
        sys.exit(2)

    joined = join_parks(games, parks, health)
    validate_team_assignment(joined, health)
    joined = resolve_starters(s, joined, raw, health)

    roof_cache: dict[str, Any] = {}
    roof_evidence_rows: list[dict[str, Any]] = []
    roof_conflict_rows: list[dict[str, Any]] = []
    game_rows: list[dict[str, Any]] = []
    query_rows: list[dict[str, Any]] = []
    pitch_rows: list[dict[str, Any]] = []
    provenance_rows: list[dict[str, Any]] = []
    density_rows: list[dict[str, Any]] = []
    decomposition_rows: list[dict[str, Any]] = []

    games_unique = joined.sort_values(["game_pk", "side"]).groupby("game_pk", as_index=False).first()
    branch_map: dict[tuple[int, str], dict[str, Any]] = {}

    for _, g in games_unique.iterrows():
        a = ambient(s, g, raw, health)
        if not a:
            continue
        roof_state, roof_reason, votes = resolve_roof_state(s, g, raw, health, roof_cache, roof_evidence_rows, roof_conflict_rows)
        branches = ([outdoor_branch(g, a)] if roof_state in {"NOT_APPLICABLE", "OPEN"}
                    else ([enclosed_branch(g, a)] if roof_state in {"FIXED_CLOSED", "CLOSED"}
                          else [outdoor_branch(g, a), enclosed_branch(g, a)]))
        for b in branches:
            wpr_desc = wind_park_relative_description(b.get("wind_out_to_cf_kmh"), b.get("wind_cross_kmh"))
            rec = {
                "game_pk": g.game_pk, "game_datetime_utc": g.game_datetime_utc,
                "game_datetime_est": utc_to_est_str(g.game_datetime_utc),
                "venue_name_api": g.venue_name_api,
                "venue_name": g.venuename, "roofclass": g.roofclass,
                "ballparkelevationm": g.ballparkelevationm, "elevation_ft": round(m_to_ft(g.ballparkelevationm), 1),
                "roof_state": roof_state, "roof_decision_reason": roof_reason,
                "roof_votes_json": json.dumps(votes, sort_keys=True), "park_join_method": g.park_join_method,
                "home_plate_bearing_deg": g.homeplatebearingdeg, **a, **b,
                "temperature_f": round(c_to_f(b["temperature_c"]), 1),
                "wind_speed_mph": round(kmh_to_mph(b.get("external_wind_speed_kmh", 0) or 0), 1),
                "wind_park_relative_description": wpr_desc,
                "forecast_valid_time_est": utc_to_est_str(b.get("forecast_valid_time_utc")),
            }
            game_rows.append(rec)
            branch_map[(int(g.game_pk), b["branch"])] = rec
            provenance_rows.append({
                "game_pk": g.game_pk, "venue_name": g.venuename, "branch": b["branch"],
                "temperature_source": b.get("temperature_source"), "pressure_source": b.get("pressure_source"),
                "forecast_valid_time_utc": b.get("forecast_valid_time_utc"),
                "forecast_valid_time_est": utc_to_est_str(b.get("forecast_valid_time_utc")),
                "venue_lat": g.lat, "venue_lon": g.lon, "venue_elevation_m": g.ballparkelevationm,
            })
            dt = density_terms(b["temperature_c"], b["relative_humidity_pct"], b["pressure_hpa"])
            density_rows.append({"game_pk": g.game_pk, "venue_name": g.venuename, "branch": b["branch"], **dt})

    season_start = date(pd.to_datetime(args.date).year, 3, 1).isoformat()
    recent_start = (pd.to_datetime(args.date).date() - timedelta(days=30)).isoformat()
    end_exclusive = (pd.to_datetime(args.date).date() + timedelta(days=1)).isoformat()

    for _, r in joined.iterrows():
        if pd.isna(r.pitcher_id):
            continue
        season_df = savant_query(s, int(r.pitcher_id), season_start, end_exclusive, raw, health, query_rows, "season")
        recent_df = savant_query(s, int(r.pitcher_id), recent_start, end_exclusive, raw, health, query_rows, "recent30")
        arsenal = weighted_arsenal(summarize_window(season_df), summarize_window(recent_df))
        if arsenal.empty:
            continue
        for branch in ["OUTDOOR", "ENCLOSED"]:
            env = branch_map.get((int(r.game_pk), branch))
            if not env:
                continue
            phys = apply_environment_physics(arsenal, env)
            for c in ["game_pk", "side", "team", "pitcher_id", "pitcher_name", "pitcher_resolution_method"]:
                phys[c] = r[c]
            phys["venue_name"] = r.venuename
            phys["branch"] = branch
            phys["roof_state"] = env["roof_state"]
            phys["air_density_kg_m3"] = env["air_density_kg_m3"]
            phys_records = phys.sort_values("pitches", ascending=False).to_dict(orient="records")
            pitch_rows.extend(phys_records)
            for pr in phys_records:
                decomposition_rows.append(decompose_pitch_effects(pr, env))

    game_env = pd.DataFrame(game_rows)
    validate_environment_rows(game_env, health)

    write_csv_contract(game_env, run_dir / "game_environment_audit.csv",
        ["game_pk", "game_datetime_utc", "game_datetime_est", "venue_name_api", "venue_name", "roofclass",
         "ballparkelevationm", "elevation_ft", "roof_state", "roof_decision_reason", "roof_votes_json",
         "park_join_method", "home_plate_bearing_deg", "branch", "temperature_c", "temperature_f",
         "relative_humidity_pct", "pressure_hpa", "air_density_kg_m3", "density_ratio_to_ref",
         "wind_speed_mph", "wind_park_relative_description", "forecast_valid_time_est"])
    write_csv_contract(pd.DataFrame(roof_evidence_rows), run_dir / "roof_evidence_audit.csv",
        ["game_pk", "venue_name", "source", "scope", "url", "vote", "parser_status", "confidence",
         "http_status", "page_char_length", "roof_keyword_found", "raw_file"])
    write_csv_contract(pd.DataFrame(roof_conflict_rows), run_dir / "roof_conflict_audit.csv",
        ["game_pk", "venue_name", "dedicated_votes_json", "generic_votes_json", "has_conflict", "resolution"])
    write_csv_contract(pd.DataFrame(query_rows), run_dir / "pitcher_query_audit.csv",
        ["pitcher_id", "query_start", "query_end", "window_label", "url", "request_params_json",
         "http_status", "raw_file", "rows_returned", "id_column", "rows_scoped", "scope_pass", "error"])
    write_csv_contract(pd.DataFrame(pitch_rows), run_dir / "pitch_physics_environment.csv",
        ["game_pk", "side", "team", "pitcher_id", "pitcher_name", "pitcher_resolution_method", "venue_name",
         "branch", "roof_state", "air_density_kg_m3", "density_ratio_to_ref", "pitch_type", "pitches", "usage_pct",
         "release_speed", "effective_speed", "release_spin_rate", "spin_axis", "active_spin", "active_spin_pct",
         "spin_efficiency", "release_extension", "pfx_x", "pfx_z", "projected_pfx_x", "delta_pfx_x_inches",
         "projected_pfx_z", "delta_pfx_z_inches", "carry_wind_adjustment_ft_400ft", "carry_density_adjustment_ft_400ft",
         "effective_plate_speed_delta_mph", "active_spin_environment_index", "window_basis", "model_label"])
    write_csv_contract(pd.DataFrame(provenance_rows), run_dir / "atmosphere_provenance_audit.csv",
        ["game_pk", "venue_name", "branch", "temperature_source", "pressure_source", "forecast_valid_time_utc",
         "forecast_valid_time_est", "venue_lat", "venue_lon", "venue_elevation_m"])
    write_csv_contract(pd.DataFrame(density_rows), run_dir / "density_calculation_audit.csv",
        ["game_pk", "venue_name", "branch", "temperature_c", "temperature_k", "relative_humidity_pct",
         "pressure_hpa", "pressure_pa", "saturation_vapor_pressure_pa", "vapor_pressure_pa",
         "dry_air_partial_pressure_pa", "dry_air_density_kg_m3", "vapor_density_kg_m3", "air_density_kg_m3",
         "density_ratio_to_ref"])
    write_csv_contract(pd.DataFrame(decomposition_rows), run_dir / "pitch_effect_decomposition.csv",
        ["game_pk", "pitcher_id", "pitch_type", "branch", "density_ratio_to_ref", "wind_out_to_cf_kmh",
         "density_effect", "wind_effect", "roof_branch_effect", "total_carry_effect_ft_400ft"])

    starters_resolved = joined.pitcher_id.notna().sum()
    diag_rows = [
        {"check": "scheduled_games", "value": float(games_unique.game_pk.nunique()), "status": "PASS"},
        {"check": "scheduled_starters_resolved", "value": float(starters_resolved),
         "status": "PASS" if starters_resolved == len(joined) else "WARN_UPSTREAM_PENDING"},
        {"check": "query_audit_rows", "value": float(len(query_rows)), "status": "PASS" if len(query_rows) > 0 else "WARN_NO_PITCH_ROWS"},
        {"check": "conflicted_roof_games", "value": float(sum(1 for c in roof_conflict_rows if c.get("has_conflict"))), "status": "INFO"},
        {"check": "pitch_physics_rows", "value": float(len(pitch_rows)), "status": "PASS" if pitch_rows else "WARN_NO_PHYSICS_ROWS"},
        {"check": "pitch_effect_decomposition_rows", "value": float(len(decomposition_rows)),
         "status": "PASS" if len(decomposition_rows) == len(pitch_rows) else "FAIL_ROW_PARITY"},
        {"check": "air_density_range_kg_m3_across_todays_venues",
         "value": float(game_env.air_density_kg_m3.max() - game_env.air_density_kg_m3.min()) if not game_env.empty else float('nan'),
         "status": "INFO"},
    ]
    write_csv_contract(pd.DataFrame(diag_rows), run_dir / "run_health_and_model_diagnostics.csv", ["check", "value", "status"])
    if len(decomposition_rows) != len(pitch_rows):
        health.add("FATAL", "PITCH_EFFECT_ROW_PARITY_FAILURE",
                    "run", f"pitch_rows={len(pitch_rows)} decomposition_rows={len(decomposition_rows)}", "Fix decomposition loop.")
    write_csv_contract(health.frame(), run_dir / "findings.csv", Health.COLUMNS)

    copy_dashboard_and_glossary(run_dir, script_dir)

    manifest = {
        "version": VERSION, "requested_date": args.date, "run_dir": str(run_dir.resolve()),
        "fatal": health.fatal(),
        "files": {p.name: sha256_file(p) for p in run_dir.iterdir() if p.is_file()},
    }
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(json.dumps({
        "status": "FAIL" if health.fatal() else "COMPLETE", "version": VERSION, "requested_date": args.date,
        "run_dir": str(run_dir.resolve()), "games": int(games_unique.game_pk.nunique()),
        "starters_resolved": int(starters_resolved), "environment_rows": int(len(game_env)),
        "pitch_rows": int(len(pitch_rows)), "decomposition_rows": int(len(decomposition_rows)),
        "conflicted_roof_games": sum(1 for c in roof_conflict_rows if c.get("has_conflict")),
        "findings": int(len(health.rows)),
    }, indent=2))

    if args.open_dashboard:
        dashboard = run_dir / "mlb-pitch-environment-live-dashboard-V52.html"
        if dashboard.exists():
            webbrowser.open(dashboard.resolve().as_uri())


if __name__ == "__main__":
    main()
