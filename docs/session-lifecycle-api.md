# Session exclusion and restore API

Lifecycle is stored separately from `session_dim` and `shot_fact`, so replacing a desktop snapshot never clears an exclusion.

## Exclude

`POST /api/v1/athletes/{athlete_id}/sessions/{session_id}/exclude`

Desktop clients without a confirmed engine session ID may use `POST /api/v1/athletes/{athlete_id}/sessions/by-key/{session_key}/exclude`; the engine resolves the stable ID using the desktop ingest key algorithm.

With `reserve_if_missing: true`, this by-key route persists an exclusion even if the first ingest has not committed. Later ingest keeps the exclusion, including after an engine restart. The ordinary exclusion route and by-key calls without this option still return 404 for unknown sessions. Restoring an existing reservation works before or after ingest.

Body fields `actor_id` and `reason` are optional (defaults: `unknown`, `unspecified`). Success returns `status: excluded`, `excluded_at_utc`, and `already_excluded`. Repeated requests do not add audit rows.

## Restore

`POST /api/v1/athletes/{athlete_id}/sessions/{session_id}/restore`

Desktop clients may also restore by stable key with `POST /api/v1/athletes/{athlete_id}/sessions/by-key/{session_key}/restore`.

Uses the same optional body. Success returns `status: active`, `restored_at_utc`, and `already_active`. Repeated restores are idempotent.

Unknown sessions return 404 `session_not_found`; sessions belonging to another athlete return 404 `session_not_owned` to avoid disclosing cross-athlete existence.

## Reads and effects

`GET /api/v1/athletes/{athlete_id}/sessions` hides excluded sessions by default; `include_excluded=true` includes them. Each row has `excluded` and `excluded_at_utc`. `GET /api/v1/sessions/{session_id}` retains the dimension and returns an empty `shots` array plus the same lifecycle fields while excluded.

Excluded sessions are omitted by shared fact queries. Explicit daily generation for an excluded session returns 409 `session_excluded`, including when it is one of the sessions supplied for a desktop calendar day.

A state change invalidates the single-session daily cache and the desktop calendar cache keyed as `daily:day:YYYY-MM-DD` by the first scoring arrow in Asia/Shanghai. It also invalidates weekly, monthly, quarterly and yearly caches whose windows contain any arrow from the session. A new active desktop upload invalidates its affected caches. Re-uploading an active session also invalidates its original windows, including when its date changes. Cached report bodies and their derived memories are removed together. Desktop calendar report bodies include the sorted `session_ids` used to generate them. An ask request that supplies a report ID can only use that saved report while it remains valid.

Later reports that use the session for a rolling baseline, prior-period comparison, or anchor setup are also invalidated. A report that cites an invalidated snapshot in its plateau history expires with it. Ask checks report and cited-note freshness before returning a model answer.

If the affected session is a source for an anchor snapshot, the engine clears that athlete's report cache and derived report memories across all windows. Excluded anchor sources are skipped when selecting the current anchor and become eligible after restoration. User-authored notes are preserved.

The desktop checks `desktop_report_contract: 2` in `GET /api/v1/health` before sending report operations. Pair the desktop with an engine release that can persist an exclusion before ingest.
