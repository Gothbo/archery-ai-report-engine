# Session exclusion and restore API

Lifecycle is stored separately from `session_dim` and `shot_fact`, so replacing a desktop snapshot never clears an exclusion.

## Exclude

`POST /api/v1/athletes/{athlete_id}/sessions/{session_id}/exclude`

Desktop clients without a confirmed engine session ID may use `POST /api/v1/athletes/{athlete_id}/sessions/by-key/{session_key}/exclude`; the engine resolves the stable ID using the desktop ingest key algorithm.

Body fields `actor_id` and `reason` are optional (defaults: `unknown`, `unspecified`). Success returns `status: excluded`, `excluded_at_utc`, and `already_excluded`. Repeated requests do not add audit rows.

## Restore

`POST /api/v1/athletes/{athlete_id}/sessions/{session_id}/restore`

Uses the same optional body. Success returns `status: active`, `restored_at_utc`, and `already_active`. Repeated restores are idempotent.

Unknown sessions return 404 `session_not_found`; sessions belonging to another athlete return 404 `session_not_owned` to avoid disclosing cross-athlete existence.

## Reads and effects

`GET /api/v1/athletes/{athlete_id}/sessions` hides excluded sessions by default; `include_excluded=true` includes them. Each row has `excluded` and `excluded_at_utc`. `GET /api/v1/sessions/{session_id}` retains the dimension and returns an empty `shots` array plus the same lifecycle fields while excluded.

Excluded sessions are omitted by shared fact queries and therefore do not contribute to reports, ask retrieval, rolling baselines, or anchor candidates. Explicit daily generation for an excluded session returns 409 `session_excluded`. Exclude and restore invalidate that session's daily cache and the weekly/monthly/quarterly/yearly caches containing its timestamp.
