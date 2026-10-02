# Planner — module architecture

Dated to-dos and calendar events bound to Vault spaces. MIKU operates plans
through typed tools; the dashboard is a server-rendered page for the owner.

## Data

- `planner_task`: title, notes, status (todo/doing/done/cancelled), priority 0–3,
  `due_at`, `remind_at`, `notified_at`, recurrence enum
  (none/daily/weekdays/weekly/monthly), space binding (`space_kind`
  none/collection/folder + `space_id` + cached `space_name`), `raw_text`,
  timestamps, `completed_at`.
- `planner_event`: title, notes, location, status (active/done/cancelled),
  `starts_at`, `ends_at`, same reminder/recurrence/space columns.
- Everything is stored as aware UTC. A naive wall time means Europe/Moscow
  (single-owner box); services coerce on the way out so SQLite and Postgres
  behave identically.

Space binding is a loose reference, not a foreign key: Vault owns its tables,
planner stores kind + id + cached name for badges. A deleted collection
orphans gracefully; the name on the badge stays readable.

## Spaces

The picker and the model resolve names through `vault.spaces.v1`
(collections + folder nodes with breadcrumb paths). Resolution is
exact → path-suffix → substring; unknown names fail with the known-spaces
list so the model retries instead of guessing. No space means the inbox.
When Vault is off, space features degrade to inbox with a hint.

## MIKU tools (`planner.*.v1`)

create/list/complete/snooze for tasks, create/list for events, `today` for the
agenda digest, `resolve_space` for name lookup. Creates and completes are
reversible through `planner.undo.v1` (contract `undo.v1`): a create is found
by title, a complete by task id with its spawned twin removed.

The model parses Russian dates into ISO datetimes with offset; the server only
validates and normalizes. Bad input returns `invalid` with a retry hint, never
a silent wrong date.

## Orientation (how MIKU sees plans without being asked)

- Pull: the `today` tool returns overdue first, then due-today, then events,
  capped at 12 lines. Its description carries the anti-nag rule: mention at
  most one line at the end, only for overdue or due-within-2-hours items.
- Push: the sweep delivers `remind_at` moments as `miku:notify` lines with a
  space badge. Reading the model output is never required for a reminder.
- Digest: `GET /api/miku/briefing` appends the agenda section through the
  `planner.today.v1` contract; the section is skipped when planner is off.

## Reminders (`tasks.py`, `sweep.py`)

No Celery beat exists, so `planner.sweep_reminders` re-arms itself every 60s.
Every planner write calls `ensure_sweep_armed`, which schedules the loop once
via a Redis SETNX flag — a worker restart heals on the next API call. Each
pass pushes due reminders exactly once (`notified_at`), then rolls repeating
events whose end passed (old → done, next instance spawned with shifted
remind). Task repeats roll on explicit complete, never in the sweep.

## Dashboard (`/planner`)

Server-rendered: overdue / today / upcoming / done tasks plus upcoming events,
quick-add form with a space picker fed by `/api/vault/collections` and
`items?node_type=folder`. Actions (complete, snooze +1h / tomorrow 9:00,
delete) are fetch calls followed by reload — no client state to drift.
The path is pinned in MIKU's deep-link allowlist.

## Non-goals (second iteration)

Task↔note links with a widget on the Vault card, week view, smart time
suggestions from completion history. The `space_id` columns already accept a
future item link without a migration.
