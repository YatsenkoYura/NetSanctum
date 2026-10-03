# NetSanctum Capture

A Manifest V2 browser extension that sends what is on screen to the NetSanctum
`vault` module. It runs on Firefox, on Chromium forks that still ship MV2 (Kiwi,
ungoogled-chromium, older desktop Chrome), and on any other browser with a
classic `browser_action` extension host.

## The flow

Press <kbd>Alt</kbd>+<kbd>Shift</kbd>+<kbd>S</kbd> (or the toolbar icon). The page dims, every media element gets a
teal outline, and nothing is saved yet — the extension is waiting for you:

- **Click a teal outline** — that media is saved.
- **Drag anywhere** — the dragged region is screenshotted.
- **Click empty page** — the whole visible tab is screenshotted.
- **<kbd>Alt</kbd>+drag** — forces a region selection even over a media outline.
- **<kbd>Esc</kbd> or right-click** — cancel.

Hovering an outline shows what it actually is: the kind and tag, the intrinsic
size next to the size on the page, where the bytes come from, and the alt text.
An inline `blob:`/`data:` source reads `inline` rather than a host, because
there is no host to name. The tooltip flips below the outline when there is no
room above it and clamps to the viewport at either edge.

Clicking a `<video>` outline is different: it asks the server to **archive the
video**, not to store a frame. See below.

The veil comes off the tab for the duration of the screenshot, because
`captureVisibleTab` photographs whatever is rendered. If the save fails, the
overlay comes back so you can retry without pressing the hotkey again.

The default shortcut is remappable at `chrome://extensions/shortcuts` (or
`about:addons` → gear → *Manage Extension Shortcuts*). Note that Chromium's
`commands` API rejects `` Ctrl+` `` outright — the backtick is not an accepted
key — and an invalid shortcut makes the browser refuse to load the whole
extension, so the manifest ships <kbd>Alt</kbd>+<kbd>Shift</kbd>+<kbd>S</kbd>.

The right-click menu entry **Save to NetSanctum Vault** keeps the old one-shot
behaviour: right-clicking an image saves that image, right-clicking a page
screenshots it.

## Two cross-browser traps this extension avoids

**`chrome.*` does not return promises in Firefox under Manifest V2.** Firefox's
`chrome` namespace is the callback-based one, and the promise support it has is
MV3-only (Bugzilla 1711570). `await chrome.tabs.sendMessage(...)` there yields
`undefined` and the caller carries on with nothing. Every asynchronous call goes
through `netsanctumCall` in `api.js` instead, because callbacks work everywhere.

**A server error is not always a string.** FastAPI answers a request-validation
failure with `detail` as a *list* of error objects, so `String(payload.detail)`
renders `[object Object]` and tells the user nothing. Every message is formatted
through `netsanctumErrorText` in `errors.js`, which unwraps the list into
`field: message` pairs.

**Reload the extension, then reload your tabs.** A content script is injected
only into documents loaded *after* the extension is installed or reloaded, so a
tab that was already open keeps running the previous copy — one that does not
see `errors.js` and `api.js`. The overlay detects this and says
*"This tab is running an older copy of the extension — reload the page"* rather
than throwing a bare `ReferenceError`.

## What counts as media

While the overlay is armed the page is scanned for anything worth an outline.
Two passes: a selector pass over the media tags, then a bounded walk of the
whole DOM for CSS backgrounds, because no selector names those and on a modern
landing page the background *is* the picture. Elements smaller than 24px,
off-screen, `visibility: hidden` or fully transparent are skipped, and the scan
stops at 4000 nodes / 300 results.

When you click an outline the element under the cursor and its ancestors are
resolved, because on a real page the picture usually lives in a wrapper rather
than in the hit target:

- `<img>`, including `srcset` (largest candidate wins), `currentSrc`, and the
  common lazy attributes (`data-src`, `data-original`, `data-lazy-src`, …)
- `<picture>` and `<source>`, `<video>` (poster preferred over the stream),
  `<audio>`, `<object>`, `<embed>`
- `<canvas>` via `toDataURL`; a cross-origin tainted canvas falls through to a
  screenshot instead of failing
- CSS `background-image`, `border-image-source` and `list-style-image`, both as
  an outline source and as an ancestor fallback
- `<a href>` pointing at a media file, by extension
- inline `<svg>` is deliberately *not* media: it has no addressable bytes, so
  the page screenshot is the faithful representation
- `blob:` and `data:` sources are read in the page context, because the
  background page is not allowed to open them

A click on empty page is treated as "the page, as seen" — a full viewport
screenshot. Dragging is the explicit way to ask for a region.

## Videos

Clicking a `<video>` outline sends the **URL**, not the pixels. Vault passes it
to the video module that owns archiving through the `media.video.ingest.v1`
contract, which decides for itself which platforms it handles — the extension
and Vault name no sites. The download is a queued background job, so the capture
returns immediately with a Vault record naming the archive job, and the file
turns up in Video Archive.

Vault keeps the record and the poster; it does not store the video bytes. That
is deliberate: Vault is expected to grow per-collection encryption, and a few
hundred megabytes of video is the wrong thing to hand to that. The record points
at the archive instead.

If the source cannot be archived the capture **fails and nothing is saved** —
an unsupported site answers `422`, a missing archive module answers `503`. That
is deliberate too: a Vault entry pointing at a download that will never happen
is worse than no entry. When that happens, <kbd>Alt</kbd>+drag over the player
gives you a screenshot of the frame instead.

The still sent alongside is the `poster` attribute, if there is one. Drawing the
live frame would need a canvas read that a cross-origin video forbids.

## How it authenticates

The options page asks for the NetSanctum address and your bootstrap access
token. The extension exchanges that token for a bearer session through
`POST /auth/login` and keeps only the short-lived bearer, refreshing it when it
is close to expiry or when the server answers `401`.

Captures go to `POST /api/vault/capture`, which accepts a bearer token only —
never the browser session cookie. Without that rule, any page able to reach the
endpoint would inherit your logged-in NetSanctum session.

The extension origin (`chrome-extension://…`, `moz-extension://…`) is accepted
on exactly two routes: `POST /auth/login` and `POST /api/vault/capture`. Both
authenticate by bearer token and neither reads a cookie, so an ordinary web page
cannot reach them; every other route keeps the normal cross-site rules.

## Install

The options page wants the full address **including the scheme** —
`http://localhost:3000`, not `localhost:3000`. A bare host is only tolerated as
a convenience for `localhost` and `127.0.0.1`, and anything else defaults to
`https://`.

Firefox — `about:debugging` → *This Firefox* → *Load Temporary Add-on* → pick
`manifest.json`.

Chromium forks — `chrome://extensions` → enable *Developer mode* → *Load
unpacked* → pick `clients/browser-extension`.

Then open the extension options from the extensions menu and fill in the address
and token.

## Where captures land

A capture is stored exactly like a picture pasted into the Vault dashboard: the
bytes become a `data:image/…;base64,` URL in the item's image field. The list
endpoint omits those bytes, the dashboard loads them lazily from
`/api/vault/items/{id}/image`, and the offline `vault_all` package carries them.
Pictures larger than the limit are re-encoded to JPEG and downscaled in the
extension before upload, so a 12 MP photo cannot exceed the server's 10 MiB
ceiling.

Screenshots use JPEG at quality 88; media is uploaded as-is. Non-image media
behind a link (audio, a bare `.mp4`) is not an image the endpoint can store, so
the extension asks for it as a video archive and falls back to screenshotting the
outline if the server declines.

## Limits

- `captureVisibleTab` needs the tab to be active and the window not minimised.
- A region screenshot is the visible viewport only; there is no full-page capture.
- The region is cropped in device pixels, using the page's own CSS viewport as
  the scale reference, so it lands correctly on a HiDPI display.
- Video frames are not extracted locally; the archive brings its own thumbnail.
- A video capture needs the archive module installed. Without it the capture
  returns `503` and saves nothing.
- Only sources the video module recognises can be archived. A `<video>` pointing
  at a plain file on an unknown host is not one of them.
- The overlay cannot be armed on pages with no content script — a PDF viewer,
  `chrome://`, the extension gallery. The toolbar press falls back to a full
  screenshot there.
- Cross-origin iframes cannot be inspected, so media inside one gets no outline.
- `vault` must be installed and enabled; the endpoint returns `503` otherwise.
- The manifest is Manifest V2. Recent desktop Chrome has removed MV2 support
  outright, so the extension targets Firefox and Chromium forks that still carry
  it (Kiwi and similar). `web-ext lint` is clean against Firefox.
