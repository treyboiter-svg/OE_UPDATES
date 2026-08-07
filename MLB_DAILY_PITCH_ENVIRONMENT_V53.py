#!/usr/bin/env python3
"""MLB Daily Pitch Environment - V53
Forensic correction of V52, driven by direct verification against Baseball
Savant's own CSV documentation and a real production code review:

  - FIX (critical, confirmed root cause): V52 added active_spin / active_spin_pct
    / spin_efficiency to the pitch_physics_environment.csv column whitelist and
    believed this "fixed" missing active-spin data. It did not. Baseball Savant's
    statcast_search/csv per-pitch export (see https://baseballsavant.mlb.com/csv-docs)
    NEVER contains an active-spin field of any kind -- active spin is a separate
    season-level leaderboard product (baseballsavant.mlb.com/leaderboard/active-spin),
    not a per-pitch Statcast field. V52's keep_num filter silently found nothing to
    average, so the column was always blank. V53 fetches the real Active Spin
    leaderboard (the same endpoint the pybaseball library uses in production:
    statcast_pitcher_active_spin) once per run, and merges it onto each pitcher's
    arsenal by MLBAM player_id first, normalized name as fallback, and pitch-type
    key (code or descriptive name, both handled). A full audit trail
    (active_spin_merge_audit.csv) records every match/no-match decision, and a
    permanent regression guard (validate_active_spin_coverage) fails the run
    loudly if the leaderboard fetch succeeds but the match rate is ever zero.
  - FIX: ambient() previously trusted the nearest available Open-Meteo hourly
    forecast bucket with no sanity check. If the requested date's game time fell
    near or outside the fixed 3-day forecast window, the nearest-hour lookup could
    silently return a forecast far from the actual first-pitch time with zero
    indication anything was wrong. V53 (a) sizes forecast_days dynamically to the
    requested date so the correct hour is actually in the returned series, and
    (b) computes forecast_time_delta_minutes and raises a WARN finding whenever
    the closest available hour is more than 90 minutes from first pitch, so a
    genuine data gap is visible in findings.csv/the dashboard instead of hidden.
  - FIX: pitch_physics_environment rows were already correctly usage-sorted and
    already grouped per pitcher/branch in the CSV row order (V52 got this right
    at the data layer), but the live dashboard rendered every pitcher's pitches
    into one continuous flat table with no visual separation, which is what
    actually produced the "pitcher's own pitches aren't grouped with just them"
    complaint. V53's dashboard groups the Pitch Physics table by
    game+pitcher+branch with a subheading per group (see the V53 HTML file).
  - CARRIED FORWARD from V52 (re-verified, still correct): join_parks() team-field
    protection + validate_team_assignment() regression guard; roof quorum/vote
    logic; EST display fields; usage-descending sort at summarize_window() and
    weighted_arsenal().
  - ADDED: forecast_time_delta_minutes surfaced in game_environment_audit.csv and
    atmosphere_provenance_audit.csv for weather-timing auditability.
"""
SEE_FULL_SCRIPT_IN_SANDBOX_VARIABLE_main_script