# ADR-0007: Store session lifecycle separately from facts

## Status

Accepted

## Decision

Store exclusion and restore state in `session_lifecycle`, keyed by `(athlete_id, session_id)`, and append each state transition to `session_lifecycle_audit`.

## Context

Desktop ingest replaces `session_dim` and `shot_fact` when the same session key is retried. Lifecycle state must survive that replacement, and an exclusion must not be undone by a network retry.

## Consequences

Fact readers filter excluded sessions with a shared `NOT EXISTS` predicate. Restore is explicit and audited. The state is decoupled from ingest fact replacement while preserving the existing session dimension schema.
