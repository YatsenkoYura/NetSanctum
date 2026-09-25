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

All local-material discovery uses the private `search.global.v1` integration. Module-specific
`library.viewer.v1` APIs are hidden from the model catalog, so search, selection, opening, and
playback cannot bypass the shared ranking path. The contract stays declared in the manifest for
server-side resource resolution only. The per-turn tool catalog is frozen: result context never
adds or removes tools mid-turn; compatibility is validated at execution time. The Search module
builds its index from paginated `search.documents.v1` snapshots published by active modules, so
MIKU does not import module models or invent entity URLs. `required_terms` is a generic exact-token
MUST filter applied in SQL with no language knowledge. Search results carry bounded metadata and
validated local open paths; the runtime still sees only result references during grounded response
generation.

Search result handling is an explicit bounded graph: `retrieved -> evaluate -> act|clarify -> complete`.
Only a high-confidence result with a sufficient lead can transition to `act`; ambiguous results always
transition to `clarify`. The graph has a hard step limit and never performs integration calls itself.

Direct conversation is a one-model-call fast path. A tool-assisted answer uses a planning call and a
grounded response call. Provider failure falls back to a bounded server-rendered response without
changing execution policy.

Each engine (chat, speech-to-text, speech-to-speech) runs in one of three modes, chosen per engine
in provider settings: `api` (third-party OpenAI-compatible endpoint), `local` (self-hosted URL such
as Ollama or a whisper server, no key required), or `client` (browser/Android handles it natively;
chat is never client-side). The runtime advertises modes in capabilities, the side chat adapts its
paths (socket voice/speech vs browser recognition/synthesis), and client-mode engines short-circuit
server-side with `503` instead of failing obscurely.

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
- Audit records contain metadata only; they never store user text, assistant text, or provider payloads.

MIKU memory now has three explicit layers:

- `session`: bounded recent turns + result references in Redis, TTL-limited, used only for follow-up turns;
- `profile`: durable user-specific preferences/facts that are either explicit (`запомни`) or system-owned;
- `episodic`: durable compact event summaries such as what was opened, watched, or clarified.

Only `session` memory may contain raw recent conversation text, and it expires with the session. `profile`
and `episodic` memory store structured facts and short summaries only, never raw transcripts. Neither
layer is written to the audit log. Any long-term memory feature must expose a clear-memory action and a
reviewable representation instead of silently accumulating hidden history.

## Response contract

`MikuReply.text` remains the final text for compatibility. `MikuReply.segments` is the presentation and
speech contract:

- `acknowledgement`: a natural pre-tool phrase;
- `response`: the grounded final answer;
- `status`: non-conversational progress information;
- `confirmation`: a mutation preview, never spoken by default.

The socket transport streams `turn.partial` events (acknowledgement text, tool result counts) before
the final `turn.result`/`turn.completed`, so the client feels alive without waiting for the full turn.
A `cancel` message by `request_id` aborts the in-flight turn and a new `query` barges in over it; the
server answers `turn.cancelled` and releases the session lock. Partial payloads carry counts and short
texts only, never raw provider data. Push-to-talk voice arrives as a `voice` message with base64 audio
(up to 4 MiB) and an allowlisted audio content type; the server transcribes it, emits a `transcript`
partial, then runs the normal text turn so desktop/mobile clients need no separate voice API.
Speech streams back as `speak`/`speech.chunk` events: the client sends full reply text, the server
splits it into sentence chunks and synthesizes them in order, and the client plays chunks back-to-back
so the first sentence sounds while the rest is still synthesizing. The WebView wake word (`miku`/мику)
is continuous recognition with an 8s command window after a bare wake; TTS playback ducks the listener
so MIKU never answers herself, and only mic-permission errors stop wake mode.

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
