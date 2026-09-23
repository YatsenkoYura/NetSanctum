# MIKU companion architecture

MIKU is a companion and an orchestrator, not a privileged autonomous agent. The model decides how
to answer and may propose module API calls; NetSanctum remains the authority that validates and
executes those calls.

## Turn pipeline

1. The runtime receives the user message, the consumer-scoped API catalog, and bounded result
   references from the current session.
2. The model either answers directly or emits one native function call. Polite or vague requests to
   interact with system data are still tool requests.
3. NetSanctum validates the selected integration, input schema, effect, consumer scope, and current
   result references.
4. Read calls execute immediately. Create calls stop at a signed, single-use confirmation preview.
   Update, delete, and execute effects are not available to the companion.
5. After a read call, the runtime receives only sanitized observations and writes a short grounded
   response. It cannot invoke another API during this phase.
6. A read plan may request `open` or `play` for its first result. NetSanctum applies that action only
   after checking the returned reference and its resource capability.
7. The client renders ordered response segments and sends only segments marked `speak` to TTS.

Direct conversation is a one-model-call fast path. A tool-assisted answer uses a planning call and a
grounded response call. Provider failure falls back to a bounded server-rendered response without
changing execution policy.

## Trust boundaries

- The runtime has no database, Redis, storage, encryption, or owner credentials.
- Runtime decisions are advisory. Every integration request is validated again by NetSanctum.
- External read APIs are omitted from the model catalog unless the request explicitly names their
  provider, and the server rejects any runtime decision outside that per-turn catalog. Prompt
  preferences are never treated as an external-I/O authorization boundary.
- Tools whose required target must come from current results are omitted until that result context
  exists. The model is not allowed to fabricate entity identifiers.
- Tool results are projected into bounded companion observations before they return to the model. The
  response model receives only result references and trusted module IDs, not provider-authored titles,
  descriptions, URLs, or opaque payloads. Item details are rendered separately by NetSanctum.
- Titles and summaries are untrusted data and never become instructions or executable parameters.
- Hidden reasoning is not requested, stored, or returned. The assistant may provide concise user-facing
  explanations, but not private chain-of-thought.
- Conversation text is not persisted. Session context contains only bounded result references with a
  short TTL; audit records contain metadata only.

Natural follow-up conversation will eventually need bounded working memory. Add it as an explicit
privacy-controlled session feature: keep only a short rolling summary or a few recent turns, expire it
with the session, never write it to the audit log, and provide an immediate clear-memory action. Do not
silently turn working memory into permanent chat history.

## Response contract

`MikuReply.text` remains the final text for compatibility. `MikuReply.segments` is the presentation and
speech contract:

- `acknowledgement`: a natural pre-tool phrase;
- `response`: the grounded final answer;
- `status`: non-conversational progress information;
- `confirmation`: a mutation preview, never spoken by default.

The current transport returns segments together after the turn. A future streaming transport may emit
the acknowledgement before tool execution without changing the reply schema or the server-side trust
boundary.

## Evolution rules

- Keep direct conversation separate from tool selection; direct replies do not need a fake module API.
- Keep planning and grounded response generation as separate runtime operations.
- Put search semantics in provider APIs instead of teaching the planner provider-specific query rules.
- Add bounded multi-tool plans only when the server can validate dependencies and cap cost; do not add
  an open-ended autonomous loop.
- Rank equivalent tools by declared policy metadata: prefer local and private reads before external I/O,
  unless the user explicitly requests a remote source. A failed read may trigger at most one bounded
  re-plan with the failed provider marked unavailable.
- Preserve a deterministic fallback for provider outages, but do not use phrase matching as the primary
  intent router.
- Prefer structured observations over raw provider payloads and structured session state over transcript
  history.
- Measure plan latency, tool latency, response latency, selected integration, and result count without
  logging user or assistant text.
