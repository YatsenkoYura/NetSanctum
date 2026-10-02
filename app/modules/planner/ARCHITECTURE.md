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
via a Redis SETNX flag, and a `worker_ready` handler kicks the same gate on
worker (re)start — a second loop can never fork, which matters because task
claiming is not atomic across processes. Each
pass pushes due reminders exactly once (`notified_at`), then rolls repeating
events whose end passed (old → done, next instance spawned with shifted
remind). Task repeats roll on explicit complete, never in the sweep.

## Range API

- `GET /api/planner/events?from=&to=` and `GET /api/planner/tasks?due_from=&due_to=`
  filter by an ISO datetime or a plain `YYYY-MM-DD` day (naive means Moscow).
- `GET /api/planner/calendar?from=&to=&space_kind=&space_id=&include_done_tasks=`
  returns one `{events, tasks}` payload for the visible range (cap 500 each).
  The dashboard and the Vault mini-calendar both read through this endpoint.

## Dashboard (`/planner`)

Server renders an empty calendar shell; the visible range loads client-side
via `/api/planner/calendar`, so the server never guesses which 42 days the
owner looks at. Views: month grid (Monday-first, pills in cells, `3 + "+ещё N"`
density cap with a day popover), week time-grid (all-day row + 24h columns,
drag-to-move, bottom-handle resize for `ends_at`), agenda grouped by day
(complete / snooze / delete inline, undated tasks in their own section).

- Month pills: solid for events, dashed with `✓` for tasks; color comes from
  the Vault collection (`/api/vault/collections` joined client-side, folders
  hash to a palette, inbox gray); `↻` marks repeats, overdue tasks tint red.
- Click an empty cell/slot → create modal prefilled with that date (event/task
  tabs, all-day, end date, space picker fed by collections + folder nodes,
  location/priority/recurrence/remind/notes). Click a pill → edit modal.
- Keyboard: `t` today, `m/w/a` views, `←/→` paging, `Esc` closes. Mobile gets
  a compact month (shorter cells) plus a horizontally scrollable week.
- Shared rendering/DnD/date helpers live in `static/netsanctum-calendar.js`
  (`window.NetSanctumCalendar`), reused by the Vault mini-calendar. Calendar
  styling is custom `ncal-*` classes in template `<style>` blocks, so no
  Tailwind rebuild is needed for calendar changes.
- The path is pinned in MIKU's deep-link allowlist.

## Vault mini-calendar

`vault_dashboard.html` shows a collapsible "Календарь пространства" panel
above the tiles whenever a concrete workspace is selected (hidden for "Все
карточки", other views, and read-only/package mode). It renders the same
month grid in compact mode (`2 + "+ещё N"`), scoped with
`space_kind=collection&space_id=<workspace>`, and supports quick event
creation (prefilled space) plus drag-to-move; a pill click jumps to `/planner`
for full editing.

## Non-goals (second iteration)

Task↔note links with a widget on the Vault card, week view, smart time
suggestions from completion history. The `space_id` columns already accept a
future item link without a migration.
