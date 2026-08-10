#!/usr/bin/env python3
"""V53 strict dashboard-bundle builder.

Consumes the V53 runner's exact output contract, including the real
Active-Spin-leaderboard-sourced pitch_physics_environment columns and the new
active_spin_merge_audit.csv trail, and forecast_time_delta_minutes for weather
timing auditability. Blocks packaging if a required artifact is
missing/unexpectedly empty or if pitch-physics/decomposition row counts don't
match.
"""
from __future__ import annotations
import argparse, hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import pandas as pd
from pandas.errors import EmptyDataError

VERSION = "53.0.0"
FINDINGS_COLUMNS = ["severity", "code", "entity", "message", "action"]

REQUIRED = {
    "game_environment_audit": ("game_environment_audit.csv",
        ["game_pk", "venue_name", "roofclass", "roof_state", "branch", "air_density_kg_m3", "forecast_time_delta_minutes"]),
    "roof_evidence_audit": ("roof_evidence_audit.csv",
        ["game_pk", "source", "vote", "parser_status", "confidence"]),
    "roof_conflict_audit": ("roof_conflict_audit.csv",
        ["game_pk", "has_conflict", "resolution"]),
    "pitcher_query_audit": ("pitcher_query_audit.csv", ["pitcher_id", "scope_pass"]),
    "pitch_physics_environment": ("pitch_physics_environment.csv",
        ["game_pk", "pitcher_id", "pitch_type", "branch", "active_spin_pct", "active_spin_source"]),
    "atmosphere_provenance_audit": ("atmosphere_provenance_audit.csv",
        ["game_pk", "branch", "temperature_source", "pressure_source", "forecast_time_delta_minutes"]),
    "density_calculation_audit": ("density_calculation_audit.csv",
        ["game_pk", "branch", "temperature_k", "vapor_pressure_pa", "air_density_kg_m3"]),
    "pitch_effect_decomposition": ("pitch_effect_decomposition.csv",
        ["game_pk", "pitcher_id", "pitch_type", "branch", "density_effect", "wind_effect", "roof_branch_effect"]),
    "active_spin_merge_audit": ("active_spin_merge_audit.csv",
        ["pitcher_id", "pitcher_name", "pitch_type", "match_method", "active_spin_pct", "leaderboard_year"]),
    "run_health_and_model_diagnostics": ("run_health_and_model_diagnostics.csv", ["check", "value", "status"]),
    "findings": ("findings.csv", FINDINGS_COLUMNS),
}
OPTIONAL_EMPTY_OK = {"roof_conflict_audit", "roof_evidence_audit", "findings", "active_spin_merge_audit"}


def safe_read(path: Path, columns: list[str]) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=columns)
    try:
        df = pd.read_csv(path)
    except EmptyDataError:
        return pd.DataFrame(columns=columns)
    for c in columns:
        if c not in df.columns:
            df[c] = pd.NA
    return df


def records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    return json.loads(df.where(pd.notna(df), None).to_json(orient="records", date_format="iso"))


def fail(code: str, message: str, failures: list[dict[str, str]]) -> None:
    failures.append({"severity": "FATAL", "code": code, "entity": "bundle", "message": message,
                      "action": "Correct the source artifact and rebuild the bundle."})


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--glossary", type=Path, default=None)
    args = ap.parse_args()
    run = args.run_dir
    if not run.is_dir():
        print(json.dumps({"status": "FAIL", "error": f"Not a run directory: {run}"}))
        return 2

    sections: dict[str, pd.DataFrame] = {}
    failures: list[dict[str, str]] = []
    for key, (name, cols) in REQUIRED.items():
        path = run / name
        df = safe_read(path, cols)
        sections[key] = df
        if not path.exists():
            fail("BUNDLE_MISSING_REQUIRED_SECTION", name, failures)
        elif df.empty and key not in OPTIONAL_EMPTY_OK:
            fail("BUNDLE_REQUIRED_SECTION_EMPTY", name, failures)

    env = sections["game_environment_audit"]
    physics = sections["pitch_physics_environment"]
    decomp = sections["pitch_effect_decomposition"]
    spin_audit = sections["active_spin_merge_audit"]

    if not env.empty and not physics.empty:
        env_games = set(env["game_pk"].dropna().astype(str))
        phy_games = set(physics["game_pk"].dropna().astype(str))
        missing = sorted(env_games - phy_games)
        if missing:
            failures.append({"severity": "WARN", "code": "BUNDLE_GAME_WITHOUT_PITCH_PHYSICS",
                              "entity": "bundle", "message": ",".join(missing),
                              "action": "Starter unresolved or Savant data unavailable for these games."})

    if not physics.empty and len(decomp) != len(physics):
        fail("BUNDLE_PITCH_EFFECT_ROW_PARITY_FAILURE",
             f"physics_rows={len(physics)} decomposition_rows={len(decomp)}", failures)

    if not physics.empty and "active_spin_pct" in physics.columns:
        non_null_spin = physics["active_spin_pct"].notna().sum()
        if non_null_spin == 0 and not spin_audit.empty:
            failures.append({"severity": "WARN", "code": "BUNDLE_ZERO_ACTIVE_SPIN_MATCHES",
                              "entity": "bundle", "message": f"0 of {len(physics)} rows have active_spin_pct despite merge audit rows existing.",
                              "action": "Inspect active_spin_merge_audit.csv match_method distribution."})

    glossary = []
    gp = args.glossary or (run / "V53_IN_APP_GLOSSARY.json")
    if gp.exists():
        try:
            glossary = json.loads(gp.read_text(encoding="utf-8"))
        except Exception as e:
            fail("BUNDLE_GLOSSARY_INVALID", str(e), failures)
    else:
        fail("BUNDLE_GLOSSARY_MISSING", str(gp.name), failures)

    games = []
    if not env.empty:
        keep = [c for c in ["game_pk", "game_datetime_utc", "game_datetime_est", "venue_name", "venue_name_api",
                             "roofclass", "roof_state", "roof_decision_reason", "ballparkelevationm", "elevation_ft",
                             "forecast_time_delta_minutes"]
                if c in env.columns]
        games = records(env[keep].drop_duplicates()) if keep else []

    fatal_present = any(f["severity"] == "FATAL" for f in failures)
    match_rate = (100.0 * physics["active_spin_pct"].notna().sum() / len(physics)) if not physics.empty and "active_spin_pct" in physics.columns else None
    summary = {
        "scheduled_games": len({str(x) for x in env.get("game_pk", pd.Series(dtype=object)).dropna()}),
        "environment_rows": len(env),
        "pitch_physics_rows": len(physics),
        "pitch_effect_decomposition_rows": len(decomp),
        "conflicted_roof_games": int(sections["roof_conflict_audit"].get("has_conflict", pd.Series(dtype=bool)).astype(bool).sum())
            if not sections["roof_conflict_audit"].empty else 0,
        "active_spin_match_rate_pct": round(match_rate, 1) if match_rate is not None else None,
        "findings": len(sections["findings"]) + len(failures),
        "bundle_fatal": fatal_present,
    }

    bundle = {
        "meta": {"version": VERSION, "generated_at_utc": datetime.now(timezone.utc).isoformat(), "run_dir": str(run.resolve())},
        "summary": summary,
        "games": games,
        "glossary": glossary,
        **{k: records(v) for k, v in sections.items()},
        "bundle_findings": failures,
    }
    (run / "mlb_dashboard_data_bundle.json").write_text(json.dumps(bundle, indent=2), encoding="utf-8")

    health_json = {
        "export_type": "system_health", "version": VERSION,
        "generated_at_utc": bundle["meta"]["generated_at_utc"], "summary": summary,
        "diagnostics": bundle["run_health_and_model_diagnostics"],
        "findings": bundle["findings"] + failures,
        "artifact_hashes": {p.name: sha256_file(p) for p in run.glob("*") if p.is_file()},
    }
    (run / "mlb_dashboard_system_health.json").write_text(json.dumps(health_json, indent=2), encoding="utf-8")

    print(json.dumps({"status": "FAIL" if fatal_present else "PASS", "summary": summary, "bundle_findings": failures}, indent=2))
    return 2 if fatal_present else 0


if __name__ == "__main__":
    raise SystemExit(main())
