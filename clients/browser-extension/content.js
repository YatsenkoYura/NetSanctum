/*
 * NetSanctum Capture — content script.
 *
 * The user arms the extension with a hotkey; this script then dims the page,
 * outlines every media element in teal, and waits. Clicking an outline saves
 * that media; dragging anywhere else selects the region to screenshot.
 *
 * Nothing is decided here beyond "what did the user point at". Fetching the
 * bytes and talking to the server is the background page's job, because only it
 * may reach across origins and only it holds the token.
 */
(function () {
  "use strict";

  /* A tab that was already open when the extension was installed or reloaded
   * has no content script, so the background page injects this file on demand.
   * Two copies must not both install a listener. */
  if (window.__netsanctumCaptureInstalled) return;
  window.__netsanctumCaptureInstalled = true;

  /* A tab that was already open when the extension was installed or reloaded
   * keeps running the previous copy of this script, which never saw the shared
   * helpers that now load alongside it. A bare ReferenceError tells the user
   * nothing, so the failure is detected and explained instead. */
  const SHARED_HELPERS_MISSING =
    typeof netsanctumCall !== "function" || typeof netsanctumSafeText !== "function";

  const OVERLAY_ID = "netsanctum-capture-overlay";
  const MAX_BOXES = 300;
  const CLICK_SLOP = 6;

  const LAZY_SRC_ATTRIBUTES = [
    "data-src",
    "data-original",
    "data-original-src",
    "data-lazy-src",
    "data-lazy",
    "data-url",
    "data-image",
    "data-echo",
    "data-hi-res-src",
    "data-full-src",
  ];

  const MEDIA_EXTENSION =
    /\.(?:jpe?g|png|gif|webp|avif|bmp|svg|ico|apng|mp4|webm|m4v|mov|mkv|ogv|mp3|m4a|aac|ogg|oga|opus|wav|flac)(?:$|[?#])/i;

  const IMAGE_EXTENSIONS = /\.(?:jpe?g|png|gif|webp|avif|bmp|svg|ico|apng)(?:$|[?#])/i;

  const MEDIA_SELECTOR = "img, picture, video, audio, canvas, embed, object, input[type=image], a";

  /* A CSS background is media too, and on a modern landing page it is often
   * the only picture there is. Scanning every element is the only way to find
   * them, so the walk is bounded by node count as well as by result count. */
  const MAX_SCAN = 4000;
  const MIN_BACKGROUND_EDGE = 96;

  let overlay = null;
  let suspended = null;
  let armed = false;
  let returning = false;

  /* ── Media resolution ─────────────────────────────────── */

  function absolute(url) {
    if (!url) return null;
    const value = String(url).trim();
    if (!value || value === "#" || value.startsWith("javascript:")) return null;
    try {
      return new URL(value, document.baseURI).href;
    } catch (error) {
      return null;
    }
  }

  /* The biggest candidate in a srcset, because a thumbnail is not what the
   * user meant to save. */
  function bestFromSrcset(srcset) {
    if (!srcset) return null;
    let best = null;
    for (const entry of String(srcset).split(",")) {
      const parts = entry.trim().split(/\s+/);
      const url = parts[0];
      if (!url) continue;
      const descriptor = parts[1] || "";
      let weight = 1;
      if (/^\d+(\.\d+)?w$/.test(descriptor)) weight = parseFloat(descriptor);
      else if (/^\d+(\.\d+)?x$/.test(descriptor)) weight = parseFloat(descriptor) * 1000;
      if (!best || weight > best.weight) best = { url, weight };
    }
    return best ? best.url : null;
  }

  function lazySource(element) {
    for (const attribute of LAZY_SRC_ATTRIBUTES) {
      const url = element.getAttribute && element.getAttribute(attribute);
      if (url) return url;
    }
    return null;
  }

  function backgroundImageUrl(element) {
    if (!element || element.nodeType !== 1) return null;
    let style;
    try {
      style = window.getComputedStyle(element);
    } catch (error) {
      return null;
    }
    if (!style) return null;
    for (const value of [style.backgroundImage, style.borderImageSource, style.listStyleImage]) {
      if (!value || value === "none") continue;
      const match = /url\((['"]?)(.*?)\1\)/.exec(value);
      if (match && match[2] && !match[2].startsWith("data:image/svg")) return match[2];
    }
    return null;
  }

  function describe(element) {
    if (!element || element.nodeType !== 1) return null;
    const tag = element.tagName ? element.tagName.toLowerCase() : "";
    const common = {
      kind: "image",
      url: null,
      element: tag,
      alt: element.getAttribute ? element.getAttribute("alt") : null,
      title: element.getAttribute ? element.getAttribute("title") : null,
      canvas: false,
    };

    switch (tag) {
      case "img":
      case "input": {
        if (tag === "input" && element.type !== "image") return null;
        const url =
          element.currentSrc ||
          lazySource(element) ||
          absolute(element.getAttribute("src")) ||
          bestFromSrcset(element.getAttribute("srcset") || element.getAttribute("data-srcset"));
        if (!url) return null;
        return { ...common, kind: "image", url };
      }
      case "source": {
        const url = bestFromSrcset(element.getAttribute("srcset")) || absolute(element.getAttribute("src"));
        if (!url) return null;
        const type = element.getAttribute("type") || "";
        const kind = type.startsWith("video") ? "video" : type.startsWith("audio") ? "audio" : "image";
        return { ...common, kind, url };
      }
      case "video": {
        // The poster is addressable; the stream is not. Either way the user
        // gets a picture, and a bare <video> falls back to a region shot.
        const poster = absolute(element.getAttribute("poster"));
        const url = poster || element.currentSrc || absolute(element.getAttribute("src"));
        if (url) return { ...common, kind: "video", url };
        return { ...common, kind: "video", url: null };
      }
      case "audio":
        return { ...common, kind: "audio", url: element.currentSrc || absolute(element.getAttribute("src")) };
      case "canvas":
        return { ...common, kind: "image", url: null, canvas: true };
      case "embed":
      case "object": {
        const url = absolute(element.getAttribute("src") || element.getAttribute("data"));
        return url ? { ...common, kind: "file", url } : null;
      }
      case "a": {
        const url = absolute(element.getAttribute("href"));
        if (!url || !MEDIA_EXTENSION.test(url)) return null;
        return { ...common, kind: IMAGE_EXTENSIONS.test(url) ? "image" : "file", url };
      }
      default:
        return null;
    }
  }

  function mediaFromAncestors(start) {
    let element = start;
    for (let depth = 0; element && depth < 6; depth += 1) {
      const described = describe(element);
      if (described) return described;
      const background = absolute(backgroundImageUrl(element));
      if (background) return { kind: "image", url: background, element: element.tagName.toLowerCase(), alt: null, title: element.getAttribute ? element.getAttribute("title") : null, canvas: false };
      element = element.parentElement;
    }
    return null;
  }

  function canvasDataUrl(element) {
    try {
      const data = element.toDataURL("image/png");
      return data && data.startsWith("data:image/") ? data : null;
    } catch (error) {
      // A canvas painted from a cross-origin image is tainted and unreadable.
      return null;
    }
  }

  /* blob:/data: sources can only be read here: the background page is not
   * allowed to open another origin's blob store. */
  async function inlineDataUrl(url) {
    if (!url) return null;
    if (url.startsWith("data:")) return url;
    if (!url.startsWith("blob:")) return null;
    try {
      const response = await fetch(url, { credentials: "include" });
      const blob = await response.blob();
      return await new Promise((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ""));
        reader.onerror = () => resolve(null);
        reader.readAsDataURL(blob);
      });
    } catch (error) {
      return null;
    }
  }

  /* ── Overlay ──────────────────────────────────────────── */

  const OVERLAY_CSS = `
:host { all: initial; }
.root { position: fixed; inset: 0; z-index: 2147483647; font: 500 12px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; cursor: crosshair; touch-action: none; user-select: none; -webkit-user-select: none; -webkit-touch-callout: none; -webkit-tap-highlight-color: transparent; }
.backdrop { position: absolute; inset: 0; background: rgba(8, 10, 12, 0.55); }
.box { position: absolute; z-index: 1; border: 1.5px solid rgba(45, 212, 191, 0.85); background: rgba(45, 212, 191, 0.08); pointer-events: none; }
.box.hover { border-color: #5eead4; background: rgba(94, 234, 212, 0.2); box-shadow: 0 0 0 1px rgba(94, 234, 212, 0.5), 0 0 18px rgba(45, 212, 191, 0.35); }
.tag { position: absolute; z-index: 5; max-width: min(320px, calc(100vw - 8px)); padding: 4px 8px; border: 1px solid rgba(94, 234, 212, 0.6); background: rgba(6, 20, 22, 0.95); color: #ccfbf1; font: 500 11px/1.45 ui-monospace, monospace; pointer-events: none; overflow-wrap: anywhere; }
.tag .k { color: #5eead4; font-weight: 700; }
.tag .d { color: #94a3b8; }
.tag .t { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; margin-top: 2px; color: #e2e8f0; }
.tag.below { transform: none; }
.selection { position: absolute; z-index: 3; border: 1.5px dashed #5eead4; background: rgba(94, 234, 212, 0.14); box-shadow: 0 0 0 9999px rgba(8, 10, 12, 0.55); pointer-events: none; }
/* The hint sits at the bottom of the screen, so it has to survive a phone: it
   wraps instead of running off both edges, keeps clear of the home indicator,
   and grows its type on a coarse pointer where 12px monospace is not readable
   with a thumb on it. The nowrap here was the whole of "it doesn't fit". */
.hint { position: absolute; z-index: 6; left: 50%; bottom: 0; transform: translateX(-50%); display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: center; justify-content: center; max-width: calc(100vw - 16px); padding: 10px 14px calc(10px + env(safe-area-inset-bottom)); background: #0b1113; border: 1px solid rgba(45, 212, 191, 0.5); border-radius: 8px 8px 0 0; color: #ccfbf1; text-align: center; }
.hint kbd { padding: 1px 5px; background: #134e4a; border: 1px solid #2dd4bf; border-radius: 2px; color: #5eead4; font: inherit; }
.hint .err { color: #fca5a5; }
.hint .d { color: #94a3b8; }

/* A finger is not a cursor: the desktop sizes are chosen for a glance, and on a
   phone the same numbers are unreadable. Only the overlay's own chrome grows —
   the outlines stay exact, because they are measured against real pixels. */
@media (pointer: coarse) {
  .root { font-size: 13px; }
  .hint { font-size: 13px; gap: 8px 16px; }
  .tag { font-size: 12px; }
}
.busy { position: absolute; inset: 0; z-index: 10; display: flex; align-items: center; justify-content: center; background: rgba(8, 10, 12, 0.72); color: #5eead4; font: 600 13px/1 ui-monospace, monospace; letter-spacing: 0.08em; }
`;

  /* How long a tap waits for a second one before it counts as a save. A tap that
     saves has to be told apart from a double tap that only changes the layer,
     so the save cannot happen on the finger's lift — it happens here. */
  const TAP_WINDOW_MS = 260;
  /* Two taps that drift further apart than this are two intentions, not one. */
  const TAP_SLOP = 24;
  /* A finger wobbles more than a mouse hand does, and a wobbly tap must not turn
     into a screenshot of a region nobody asked for. */
  const TOUCH_CLICK_SLOP = 16;

  function isCoarsePointer() {
    try {
      return window.matchMedia("(pointer: coarse)").matches;
    } catch (error) {
      return false;
    }
  }

  /* The hint is rewritten rather than appended to: it is also where a
     confirmation and the "older copy of the extension" notice go, and those two
     have to be able to hand the original back when they are done. */
  function buildHintInto(hint, mediaCount) {
    hint.textContent = "";
    if (isCoarsePointer()) {
      hint.innerHTML =
        "<span>Tap to save</span>" +
        "<span>Double tap: layer below</span>" +
        "<span>Triple tap: layer above</span>" +
        "<span>Drag: screenshot a region</span>";
    } else {
      hint.innerHTML =
        "<span>Click a teal outline to save that media</span>" +
        "<span>Drag to screenshot a region</span>" +
        "<span><kbd>Esc</kbd> cancel</span>";
    }
    if (mediaCount > 0) {
      const counter = document.createElement("span");
      counter.textContent = `${mediaCount} media found`;
      hint.appendChild(counter);
      /* The layer keys are invisible until something is stacked under the
         pointer, so say once that they exist. */
      if (!isCoarsePointer()) {
        const layerHelp = document.createElement("span");
        layerHelp.className = "d";
        layerHelp.textContent = "· ↑↓ слои · Esc отмена";
        hint.appendChild(layerHelp);
      }
    }
  }

  function buildOverlay() {
    const host = document.createElement("div");
    host.id = OVERLAY_ID;
    host.style.cssText = "position:fixed;inset:0;z-index:2147483647;";
    const shadow = host.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = OVERLAY_CSS;

    const root = document.createElement("div");
    root.className = "root";

    const backdrop = document.createElement("div");
    backdrop.className = "backdrop";

    const selection = document.createElement("div");
    selection.className = "selection";
    selection.style.display = "none";

    const hint = document.createElement("div");
    hint.className = "hint";
    buildHintInto(hint, 0);

    const busy = document.createElement("div");
    busy.className = "busy";
    busy.style.display = "none";
    busy.textContent = "SAVING…";

    // One reusable tooltip, parked behind the selection veil in z-order.
    const tag = document.createElement("div");
    tag.className = "tag";
    tag.style.display = "none";

    root.append(backdrop, selection, tag, hint, busy);
    shadow.append(style, root);
    return { host, shadow, root, backdrop, selection, tag, hint, busy };
  }

  function visibleRect(element) {
    const rect = element.getBoundingClientRect();
    if (rect.width < 24 || rect.height < 24) return null;
    if (rect.bottom <= 0 || rect.right <= 0) return null;
    if (rect.top >= window.innerHeight || rect.left >= window.innerWidth) return null;
    const style = window.getComputedStyle(element);
    if (style.visibility === "hidden" || style.display === "none" || Number(style.opacity) < 0.05) return null;
    return rect;
  }

  /* Every media element currently on screen, in paint order, so the boxes sit
   * above the page but below the selection veil. */
  function collectTargets() {
    const targets = [];
    const seen = new Set();

    const add = (element, info) => {
      if (targets.length >= MAX_BOXES || seen.has(element)) return;
      const rect = visibleRect(element);
      if (!rect) return;
      seen.add(element);
      targets.push({ element, info, rect });
    };

    let elements;
    try {
      elements = document.querySelectorAll(MEDIA_SELECTOR);
    } catch (error) {
      elements = [];
    }
    for (const element of elements) {
      const described = describe(element);
      if (described) add(element, described);
    }

    // Backgrounds are found by walking, because no selector names them.
    let scanned = 0;
    const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_ELEMENT);
    let node = walker.currentNode;
    while (node && scanned < MAX_SCAN && targets.length < MAX_BOXES) {
      scanned += 1;
      if (!seen.has(node)) {
        const rect = visibleRect(node);
        if (rect && rect.width >= MIN_BACKGROUND_EDGE && rect.height >= MIN_BACKGROUND_EDGE) {
          const background = backgroundImageUrl(node);
          const url = absolute(background);
          if (url) {
            add(node, {
              kind: "image",
              url,
              element: node.tagName.toLowerCase(),
              alt: null,
              title: node.getAttribute ? node.getAttribute("title") : null,
              canvas: false,
            });
          }
        }
      }
      node = walker.nextNode();
    }
    return targets;
  }

  /* The page's CSS viewport. The background page needs it to convert a
     CSS-pixel selection into the device pixels captureVisibleTab returns. */
  function viewportSize() {
    return { width: window.innerWidth, height: window.innerHeight };
  }

  function clampRect(rect) {
    const left = Math.max(0, Math.min(rect.left, window.innerWidth));
    const top = Math.max(0, Math.min(rect.top, window.innerHeight));
    const right = Math.max(0, Math.min(rect.right, window.innerWidth));
    const bottom = Math.max(0, Math.min(rect.bottom, window.innerHeight));
    return { left, top, width: right - left, height: bottom - top };
  }

  /* Turn a clicked outline into a capture request.
   A video is asked for by URL so the server can archive the real file; the
   current frame is only sent along as a preview. Anything else is asked for by
   its own bytes, read here when the page is the only context allowed to. */
function mediaRequest(entry) {
    const info = entry.info;
    const base = { rect: entry.rect, viewport: viewportSize() };

    if (isVideoElement(entry.element)) {
      return {
        ...base,
        action: "video",
        asVideo: true,
        videoUrl: videoSourceUrl(entry.element) || pageUrl() || null,
        info,
        still: stillFrom(entry.element),
      };
    }

    if (info.canvas) {
      const inline = canvasDataUrl(entry.element);
      if (inline) return { ...base, action: "media", info: { ...info, url: inline, inline: true } };
    }
    if (info.kind === "video" || info.kind === "audio") {
      // Media the page will not hand over as an image: let the server try to
      // archive it, and fall back to a shot of the outline if it cannot.
      return { ...base, action: "media", asVideo: true, info };
    }
    return { ...base, action: "media", info };
  }

  function isVideoElement(element) {
    if (!element || !element.tagName) return false;
    const tag = element.tagName.toLowerCase();
    if (tag === "video" || tag === "audio") return true;
    /* An iframe is where the player lives, not the media itself. The content
       script runs inside it too and draws the real outline there, so treating
       the frame as a target as well would box the player and everything in it. */
    if (tag === "iframe") return false;
    /* `embed` is the exception: it has no document of its own to descend into. */
    if (tag === "embed") return true;
    return false;
  }

  /* The page URL, not the stream URL: a blob: or CDN segment is not something
     the server can archive, but the watch page usually is. */
  function videoSourceUrl(element) {
    const candidate = element.currentSrc || element.getAttribute("src") || "";
    if (!candidate || candidate.startsWith("blob:") || candidate.startsWith("data:")) return null;
    return absolute(candidate);
  }

  /* The page a card should point back to. Inside a blob: or data: frame — the
     shape every JavaScript video player uses — `location.href` is the blob
     itself, which is meaningless once stored. The watch page is the top document,
     which is reachable whenever the frame shares its origin and throws otherwise. */
  function pageUrl() {
    const here = location.href.split("#")[0];
    if (!/^(blob:|data:|about:)/.test(here)) return here;
    try {
      const top = window.top.location.href.split("#")[0];
      if (top && !/^(blob:|data:|about:)/.test(top)) return top;
    } catch (error) {
      /* A cross-origin top document: no page to name, so send nothing. */
    }
    return "";
  }

  function stillFrom(element) {
    // The poster, not the current frame: drawing a cross-origin video taints a
    // canvas, and the archive will bring a thumbnail of its own anyway.
    const poster = element.getAttribute && element.getAttribute("poster");
    return poster ? absolute(poster) : null;
  }

  /* What the tooltip says about a hovered outline. Everything here is already
     known to the page, so it is read on hover rather than for every element. */
  function describeBox(entry) {
    /* An outline owned by another frame arrives as the summary that frame already
       computed. Everything below reads the local DOM, which does not exist here,
       so returning the summary is the only way to keep the details. */
    if (entry.remote) return entry.info || { kind: "image", tagName: "?", size: null, shown: "", origin: "inline", label: "" };
    const info = entry.info || {};
    const element = entry.element;
    const tagName = element && element.tagName ? element.tagName.toLowerCase() : info.element || "media";

    let size = null;
    if (tagName === "img" && element.naturalWidth) size = [element.naturalWidth, element.naturalHeight];
    else if (tagName === "video" && element.videoWidth) size = [element.videoWidth, element.videoHeight];
    else if (tagName === "canvas" && element.width) size = [element.width, element.height];

    const kind = info.canvas ? "canvas" : info.kind || "image";

    let origin;
    const url = info.url || "";
    if (!url || url.startsWith("blob:")) origin = "inline";
    else if (url.startsWith("data:")) origin = "embedded";
    else {
      try {
        origin = new URL(url, document.baseURI).hostname || "page";
      } catch (error) {
        origin = "?";
      }
    }

    const label = String(info.alt || info.title || "").trim().replace(/\s+/g, " ");
    const shown = entry.rect ? `${Math.round(entry.rect.width)}×${Math.round(entry.rect.height)}` : "";
    return { kind, tagName, size, shown, origin, label };
  }

  function renderTag(entry, layerIndex = 0, layerCount = 1) {
    const tag = overlay.tag;
    const info = describeBox(entry);
    tag.textContent = "";

    const head = document.createElement("div");
    const name = document.createElement("span");
    name.className = "k";
    name.textContent = info.tagName && info.tagName !== info.kind ? `${info.kind} <${info.tagName}>` : info.kind;
    head.appendChild(name);

    const facts = document.createElement("span");
    facts.className = "d";
    const bits = [];
    if (info.size) bits.push(`${info.size[0]}×${info.size[1]}`);
    if (info.shown && !(info.size && info.size[0] === entry.rect.width && info.size[1] === entry.rect.height)) {
      bits.push(`shown ${info.shown}`);
    }
    bits.push(info.origin);
    /* Only worth saying when there is something underneath to reach. */
    if (layerCount > 1) bits.push(`слой ${layerIndex + 1} из ${layerCount}`);
    facts.textContent = ` ${bits.join(" · ")}`;
    head.appendChild(facts);
    tag.appendChild(head);

    if (info.label) {
      const text = document.createElement("span");
      text.className = "t";
      // A long alt text would cover the very thing it describes.
      text.textContent = info.label.length > 140 ? `${info.label.slice(0, 139)}…` : info.label;
      tag.appendChild(text);
    }

    tag.style.display = "block";

    // Above the outline when there is room, below it when there is not.
    const rect = entry.rect;
    const height = tag.offsetHeight;
    const above = rect.top - height - 4 >= 0;
    tag.style.top = above ? `${rect.top - 4}px` : `${Math.min(rect.top + rect.height + 4, Math.max(4, window.innerHeight - height - 4))}px`;
    tag.style.transform = above ? "translateY(-100%)" : "none";
    tag.classList.toggle("below", !above);
    const maxLeft = Math.max(4, window.innerWidth - tag.offsetWidth - 4);
    tag.style.left = `${Math.max(4, Math.min(rect.left, maxLeft))}px`;
  }

  function hideTag() {
    if (overlay) overlay.tag.style.display = "none";
  }

  function arm() {
    /* Remembered so a frame that handed its overlay away can take it back when
       the pointer returns. Without this, entering an iframe would leave the page
       permanently unarmed. */
    armed = true;
    if (overlay) {
      overlay.hint.textContent = "";
      overlay.hint.append("Already armed");
      return;
    }
    build();
  }

  function build() {
    if (overlay) return;
    overlay = buildOverlay();
    document.documentElement.appendChild(overlay.host);

    if (SHARED_HELPERS_MISSING) {
      overlay.hint.textContent = "";
      const note = document.createElement("span");
      note.className = "err";
      note.textContent = "This tab is running an older copy of the extension — reload the page";
      overlay.hint.appendChild(note);
      overlay.host.__netsanctum = { boxes: [] };
      return;
    }

    const boxes = [];
    for (const target of collectTargets()) {
      const rect = clampRect(target.rect);
      if (rect.width < 1 || rect.height < 1) continue;
      const box = document.createElement("div");
      box.className = "box";
      box.style.left = `${rect.left}px`;
      box.style.top = `${rect.top}px`;
      box.style.width = `${rect.width}px`;
      box.style.height = `${rect.height}px`;
      overlay.root.appendChild(box);
      boxes.push({ ...target, box, rect });
    }
    if (boxes.length) {
      buildHintInto(overlay.hint, boxes.length);
    }

    let drag = null;

    /* Every outline under the pointer, topmost first.

       Media stacks: a poster image under a <video>, a thumbnail under the player
       that replaced it, a lightbox over the page it opened. The old lookup
       returned one box, always the topmost, so the layers underneath could not
       be reached at all — and they are usually the better capture. Returning the
       whole stack lets the caller step through it. */
    const stackAt = (x, y) => {
      const hits = [];
      for (let index = boxes.length - 1; index >= 0; index -= 1) {
        const entry = boxes[index];
        const r = entry.rect;
        if (x >= r.left && x <= r.left + r.width && y >= r.top && y <= r.top + r.height) hits.push(entry);
      }
      return hits;
    };

    let hovered = null;
    let stack = [];
    let layerIndex = 0;

    const showHovered = () => {
      if (!stack.length) return;
      const entry = stack[Math.min(layerIndex, stack.length - 1)];
      if (entry === hovered) return;
      if (hovered) hovered.box.classList.remove("hover");
      hovered = entry;
      if (hovered) {
        hovered.box.classList.add("hover");
        renderTag(hovered, stack.length ? Math.min(layerIndex, stack.length - 1) : 0, stack.length);
      } else {
        hideTag();
      }
    };

    /* Step through the stack, wrapping so the last layer hands back to the first
       rather than dead-ending. */
    const cycleLayer = (step) => {
      if (stack.length < 2) return;
      layerIndex = (layerIndex + step + stack.length) % stack.length;
      showHovered();
    };

    /* ── Taps ────────────────────────────────────────────

       A finger has no hover, so the whole hover-driven half of this overlay —
       the stack, the layer choice, the tag — had nothing to hang on: there was
       no pointer position until the finger moved, and a tap that did not drag
       was read as "the page, as seen". On a phone that meant the only thing you
       could capture was a region or the whole viewport, and the layers could
       not be reached at all.

       So a tap is a real gesture here, and the stack is hit-tested at the point
       the finger landed rather than wherever it last hovered. Three taps in one
       place in quick succession mean three different things, which is why the
       save waits out the double-tap window instead of firing on the lift. */

    let tapTimer = null;
    let tapCount = 0;
    let tapX = 0;
    let tapY = 0;
    let tapStack = [];

    const forgetTaps = () => {
      if (tapTimer) clearTimeout(tapTimer);
      tapTimer = null;
      tapCount = 0;
      tapStack = [];
    };

    /* Save whatever layer the tag is currently showing. Shared with the mouse
       path so the two cannot drift on what "the chosen media" means. */
    const saveChosen = (chosen) => {
      if (!chosen) return false;
      if (chosen.remote) {
        /* The outline belongs to another frame; let it do its own capture. */
        if (!forwardClick(chosen)) dismiss();
        return true;
      }
      submit(mediaRequest(chosen));
      return true;
    };

    const commitTaps = () => {
      const hits = tapStack;
      const taps = tapCount;
      forgetTaps();
      if (!hits.length) return;
      // The tag has to follow the finger between taps, so the stack is set here
      // rather than in a move handler that touch never calls.
      stack = hits;
      if (taps >= 3) {
        layerIndex = (layerIndex - 1 + hits.length) % hits.length;
        showHovered();
        return;
      }
      if (taps === 2) {
        layerIndex = (layerIndex + 1) % hits.length;
        showHovered();
        return;
      }
      saveChosen(hits[Math.min(layerIndex, hits.length - 1)]);
    };

    const onTap = (x, y) => {
      const hits = stackAt(x, y);
      if (!hits.length) return false;
      const near = tapCount > 0 && Math.abs(x - tapX) <= TAP_SLOP && Math.abs(y - tapY) <= TAP_SLOP;
      tapCount = near ? tapCount + 1 : 1;
      tapX = x;
      tapY = y;
      tapStack = hits;
      if (tapTimer) clearTimeout(tapTimer);
      tapTimer = setTimeout(commitTaps, TAP_WINDOW_MS);
      // Show the layer immediately: waiting out the window to draw anything at
      // all would read as a dropped tap.
      stack = hits;
      showHovered();
      return true;
    };

    const onMove = (event) => {
      if (drag) {
        drag.x = event.clientX;
        drag.y = event.clientY;
        paint();
        return;
      }
      const hits = stackAt(event.clientX, event.clientY);
      const sameStack = hits.length === stack.length && hits.every((entry, i) => entry === stack[i]);
      stack = hits;
      if (!sameStack) layerIndex = 0;
      // Still hovering the same outline: keep the tooltip pinned to it even
      // when the page scrolls underneath.
      showHovered();
      if (hovered) renderTag(hovered, Math.min(layerIndex, stack.length - 1), stack.length);
    };

    const onKey = (event) => {
      /* Escape has to reach here even if the page thinks it owns the keyboard, so
         it is handled before anything else and stops propagation on success. */
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        dismiss();
        return;
      }
      if (drag) return;
      const isDown = event.key === "ArrowDown" || event.key === "Tab";
      const isUp = event.key === "ArrowUp" || (event.key === "Tab" && event.shiftKey);
      if (!isDown && !isUp) return;
      /* Swallow the key even with a single layer under the pointer: an arrow that
         silently scrolls the page looks like the extension ignoring the keyboard. */
      event.preventDefault();
      if (stack.length < 2) return;
      cycleLayer(isDown ? 1 : -1);
    };

    const paint = () => {
      if (!drag) return;
      const rect = clampRect({
        left: Math.min(drag.startX, drag.x),
        top: Math.min(drag.startY, drag.y),
        right: Math.max(drag.startX, drag.x),
        bottom: Math.max(drag.startY, drag.y),
      });
      overlay.selection.style.display = "block";
      overlay.selection.style.left = `${rect.left}px`;
      overlay.selection.style.top = `${rect.top}px`;
      overlay.selection.style.width = `${rect.width}px`;
      overlay.selection.style.height = `${rect.height}px`;
    };

    /* Alt overrides a media hit so a region can still be dragged over a big
     * full-bleed banner. Otherwise the click takes the layer the arrows selected,
     * which is not always the topmost one. */
    const onDown = (event) => {
      if (event.button !== 0) return;
      /* Touch never chooses here. A tap has to be told apart from a double tap
         that only steps a layer, and the lift is the only moment both are known
         — choosing on the press would have spent the save before the second tap
         could arrive. So a press on touch only ever begins a drag, and the tap
         handlers decide what the gesture was. */
      if (event.pointerType !== "mouse") {
        drag = { startX: event.clientX, startY: event.clientY, x: event.clientX, y: event.clientY };
        // Drawn straight away: a finger gives no hover to react to, so this mark
        // is the only thing saying the press landed.
        paint();
        return;
      }
      const chosen = stack.length ? stack[Math.min(layerIndex, stack.length - 1)] : null;
      const found = event.altKey ? null : chosen;
      if (found && found.remote) {
        /* The outline belongs to another frame; let it do its own capture. */
        if (!forwardClick(found)) dismiss();
        return;
      }
      if (found) {
        submit(mediaRequest(found));
        return;
      }
      drag = { startX: event.clientX, startY: event.clientY, x: event.clientX, y: event.clientY };
      paint();
    };

    const onUp = (event) => {
      if (!drag) return;
      const touch = event.pointerType !== "mouse";
      const slop = touch ? TOUCH_CLICK_SLOP : CLICK_SLOP;
      const moved = Math.abs(event.clientX - drag.startX) > slop || Math.abs(event.clientY - drag.startY) > slop;
      const rect = clampRect({
        left: Math.min(drag.startX, event.clientX),
        top: Math.min(drag.startY, event.clientY),
        right: Math.max(drag.startX, event.clientX),
        bottom: Math.max(drag.startY, event.clientY),
      });
      drag = null;
      overlay.selection.style.display = "none";
      if (!moved) {
        // A tap that landed on media is a choice about that media — the layer
        // gesture or the save — and never a screenshot. Only a tap on nothing
        // means "the page, as seen", and on a coarse pointer that stays a
        // deliberate second gesture: an empty tap is still more likely to be a
        // mis-aimed thumb than an intent to save the viewport.
        if (onTap(event.clientX, event.clientY)) return;
        if (touch && !confirmViewport()) return;
        submit({ action: "viewport", viewport: viewportSize() });
        return;
      }
      submit({ action: "region", rect, viewport: viewportSize() });
    };

    /* An empty tap on a phone saves the whole viewport, which is one keystroke
       away from a mis-aimed thumb and impossible to undo from the overlay. It
       waits to be asked for twice. */
    let viewportArmed = false;
    let viewportTimer = null;
    const confirmViewport = () => {
      if (viewportArmed) {
        if (viewportTimer) clearTimeout(viewportTimer);
        viewportArmed = false;
        return true;
      }
      viewportArmed = true;
      overlay.hint.textContent = "";
      const note = document.createElement("span");
      note.textContent = "Tap again to save the whole screen";
      overlay.hint.appendChild(note);
      viewportTimer = setTimeout(() => {
        viewportArmed = false;
        overlay.hint.textContent = "";
        buildHintInto(overlay.hint, boxes.length);
      }, 2000);
      return false;
    };

    overlay.root.addEventListener("pointermove", onMove);
    overlay.root.addEventListener("pointerdown", onDown);
    overlay.root.addEventListener("pointerup", onUp);
    /* Leaving the page drops the stack, so coming back does not resume a stale
       layer choice against whatever is now underneath. Mouse only: a finger
       produces `pointerleave` the moment it lifts, which wiped the stack
       immediately after every tap and took the layer gesture with it. */
    const onLeave = (event) => {
      if (event && event.pointerType && event.pointerType !== "mouse") return;
      stack = [];
      layerIndex = 0;
      if (hovered) hovered.box.classList.remove("hover");
      hovered = null;
      hideTag();
    };
    overlay.root.addEventListener("pointerleave", onLeave);
    overlay.root.addEventListener("contextmenu", dismiss);
    // A stray wheel event must not scroll the page behind the veil.
    overlay.root.addEventListener("wheel", (event) => event.preventDefault(), { passive: false });
    window.addEventListener("keydown", onKey, true);

    /* Every handler the teardown needs lives here. `cleanup` is a module-level
       function and cannot see the locals declared inside this closure, so the
       references are handed over rather than closed over. */
    overlay.host.__netsanctum = { onMove, onDown, onUp, onKey, onLeave, boxes };
    askChildrenForTargets();
  }

  /* Every frame would otherwise draw its own overlay, and the ancestor's would
     hide the descendant's while starving it of pointer events. So only the top
     frame draws. A child frame contributes outlines by posting its rectangles up
     to the parent, which translates them and adds them to the same list, and a
     click on one is posted back so the child can do the reading itself — the bytes
     of a blob: stream are only available in the frame that owns them. */

  function childFrames() {
    try {
      return Array.from(document.querySelectorAll("iframe, frame")).filter((frame) => {
        const rect = frame.getBoundingClientRect();
        return rect.width >= 2 && rect.height >= 2;
      });
    } catch (error) {
      return [];
    }
  }

  function askChildrenForTargets() {
    for (const frame of childFrames()) {
      try {
        frame.contentWindow.postMessage({ type: "netsanctum:scan" }, "*");
      } catch (error) {
        /* A cross-origin frame may refuse the post. The page's own media still works. */
      }
    }
  }

  window.addEventListener("message", (event) => {
    const data = event.data;
    if (!data || typeof data !== "object" || !overlay) return;
    if (data.type !== "netsanctum:targets") return;

    const owner = overlay.host.__netsanctum;
    if (!owner) return;
    /* The frame that sent these may already have been torn down. */
    for (const entry of data.targets || []) {
      if (owner.boxes.length >= MAX_BOXES) break;
      const box = document.createElement("div");
      box.className = "box";
      box.style.left = `${entry.rect.left}px`;
      box.style.top = `${entry.rect.top}px`;
      box.style.width = `${entry.rect.width}px`;
      box.style.height = `${entry.rect.height}px`;
      overlay.root.appendChild(box);
      /* `remote` marks an outline owned by another frame. The click has to go back
         to that frame, because a blob: stream can only be read where it lives. */
      owner.boxes.push({
        element: null,
        info: entry.info || {},
        rect: entry.rect,
        box,
        remote: true,
        frame: event.source,
        remoteIndex: (data.targets || []).indexOf(entry),
      });
    }
  });

  function forwardClick(entry) {
    if (!entry.frame || typeof entry.frame.postMessage !== "function") return false;
    try {
      entry.frame.postMessage({ type: "netsanctum:pick", index: entry.remoteIndex }, "*");
      return true;
    } catch (error) {
      return false;
    }
  }

  function cleanup() {
    suspended = null;
    if (!overlay) return;
    const handlers = overlay.host.__netsanctum;
    if (handlers) {
      overlay.root.removeEventListener("pointermove", handlers.onMove);
      overlay.root.removeEventListener("pointerdown", handlers.onDown);
      overlay.root.removeEventListener("pointerup", handlers.onUp);
      overlay.root.removeEventListener("pointerleave", handlers.onLeave);
      overlay.root.removeEventListener("contextmenu", dismiss);
      window.removeEventListener("keydown", handlers.onKey, true);
    }
    overlay.host.remove();
    overlay = null;
  }

  function dismiss() {
    armed = false;
    cleanup();
    if (SHARED_HELPERS_MISSING) return;
    netsanctumCall(chrome.runtime, "sendMessage", { type: "netsanctum:disarmed" }).catch(() => {});
  }

  function fail(message) {
    // The background page restores the overlay before reporting the failure.
    resume();
    if (!overlay) return;
    overlay.busy.style.display = "none";
    const note = document.createElement("span");
    note.className = "err";
    // An object reaching textContent would render as "[object Object]".
    note.textContent =
      typeof netsanctumSafeText === "function"
        ? netsanctumSafeText(message, "The capture failed")
        : String(message == null ? "The capture failed" : message);
    overlay.hint.appendChild(note);
  }

/* The background page must always answer, and if it somehow does not, the user
     must not be left staring at "SAVING…" forever. */
  const CAPTURE_TIMEOUT_MS = 45000;

  function submit(request) {
    if (!overlay) return;
    overlay.busy.style.display = "flex";

    if (SHARED_HELPERS_MISSING) {
      fail("This tab is running an older copy of the extension — reload the page");
      return;
    }

    let settled = false;
    const finish = (fn) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      fn();
    };

    const timer = setTimeout(() => {
      finish(() => fail("NetSanctum did not answer in 45 seconds"));
    }, CAPTURE_TIMEOUT_MS);

    netsanctumCall(chrome.runtime, "sendMessage", { type: "netsanctum:capture", request })
      .then((response) => {
        finish(() => {
          if (response && response.ok) {
            cleanup();
            return;
          }
          const why = (response && response.error) || "The capture failed";
          const where = response && response.stage ? ` (${response.stage})` : "";
          fail(`${netsanctumSafeText(why, "The capture failed")}${where}`);
        });
      })
      .catch((error) => finish(() => fail(netsanctumSafeText(error, "The extension did not respond"))));
  }

  /* The background page needs the tab without the veil on it, so the overlay
   * is detached for the screenshot and put back if the save fails. The state
   * stays here: it must not cross the message boundary. */
  function suspend() {
    if (!overlay || !overlay.host.parentNode) return false;
    suspended = { overlay, parent: overlay.host.parentNode };
    suspended.parent.removeChild(overlay.host);
    overlay = null;
    return true;
  }

  function resume() {
    if (!suspended) return false;
    const restored = suspended.overlay;
    const parent = suspended.parent;
    suspended = null;
    overlay = restored;
    parent.appendChild(restored.host);
    refreshBoxes();
    return true;
  }

  /* Page position may have shifted between arming and capturing, so the boxes
   * are re-measured rather than trusted. */
  function refreshBoxes() {
    if (!overlay) return;
    const boxes = overlay.host.__netsanctum.boxes;
    for (const entry of boxes) {
      const rect = clampRect(entry.element.getBoundingClientRect());
      entry.rect = rect;
      entry.box.style.left = `${rect.left}px`;
      entry.box.style.top = `${rect.top}px`;
      entry.box.style.width = `${Math.max(0, rect.width)}px`;
      entry.box.style.height = `${Math.max(0, rect.height)}px`;
    }
  }

  /* ── Site rules ──────────────────────────────────────────

     What a capture rule asked to read off this page. The rule is selectors, not
     code: it arrived from the instance as data and is matched again on the
     server before anything it names is stored. Here it is only ever a
     querySelectorAll and an attribute read.

     Every result is cut. A rule that says "the tags" and hits a page with four
     thousand of them must not turn a card into a page — and the count is capped
     per field as well as per value, because a `many` selector on a page that
     renders a thousand matches is the normal failure, not the exotic one. */

  const MAX_EXTRACT_CHARS = 500;
  const MAX_EXTRACT_VALUES = 40;
  const MAX_EXTRACT_FIELDS = 30;

  function extractValue(element, attr) {
    if (attr === "text") return element.textContent || "";
    if (typeof element.getAttribute === "function") return element.getAttribute(attr) || "";
    return "";
  }

  /* A pattern, for the shape most of the web is in: an id that lives inside a
     URL. It arrives from the instance, so it is compiled here — at the point of
     use rather than trusted because it was validated somewhere else.

     `false` means the rule asked for a pattern and this browser would not take
     it, which is not the same as there being no pattern: returning null there
     would pass the whole attribute through and store a URL under the field's
     name, quietly and invisibly. The field is dropped instead. */
  function extractPattern(spec) {
    if (!spec.regex) return null;
    try {
      return new RegExp(spec.regex);
    } catch (error) {
      return false;
    }
  }

  function extractFields(fields) {
    const out = {};
    if (!fields || typeof fields !== "object") return out;
    for (const [name, spec] of Object.entries(fields).slice(0, MAX_EXTRACT_FIELDS)) {
      if (!spec || typeof spec.selector !== "string" || !spec.selector) continue;
      const cap = Number(spec.max) > 0 ? Math.min(Number(spec.max), MAX_EXTRACT_VALUES) : 1;
      const pattern = extractPattern(spec);
      if (pattern === false) continue;
      const group = Number(spec.group) > 0 ? Number(spec.group) : 1;
      let found;
      try {
        found = document.querySelectorAll(spec.selector);
      } catch (error) {
        /* A selector the browser refuses is a rule that will never work on this
           page. Skipping the field leaves the card correct and incomplete, which
           beats failing a capture over one field. */
        continue;
      }
      const values = [];
      for (const element of found) {
        if (values.length >= cap) break;
        let value = extractValue(element, spec.attr).replace(/\s+/g, " ").trim();
        if (pattern) {
          const hit = pattern.exec(value);
          value = hit && hit[group] !== undefined ? String(hit[group]).trim() : "";
        }
        if (value) values.push(value.slice(0, MAX_EXTRACT_CHARS));
      }
      if (!values.length) continue;
      out[String(name).slice(0, 80)] = spec.many || values.length > 1 ? values : values[0];
    }
    return out;
  }

  /* ── Cross-frame reporting ────────────────────────────────
     Only the top frame draws an overlay. A frame below it posts its own media up
     so the outline appears in one place, and takes the click back when the user
     picks it. */

  function reportTargets() {
    if (window.top === window.self) return;
    const targets = collectTargets()
      .filter((target) => target.rect.width >= 1 && target.rect.height >= 1)
      .slice(0, MAX_BOXES)
      .map((target) => ({
        rect: { left: target.rect.left, top: target.rect.top, width: target.rect.width, height: target.rect.height },
        info: describeBox(target),
      }));
    if (!targets.length) return;
    try {
      window.parent.postMessage({ type: "netsanctum:targets", targets }, "*");
    } catch (error) {
      /* The parent may be cross-origin to us in a way that blocks postMessage. */
    }
  }

  /* Only the top frame draws an overlay, so only it listens for the keyboard. A
     key press with focus inside a player never reaches it, which is why Escape
     used to do nothing at all; the frame that has focus has to pass it up. */
  window.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || window.top === window.self) return;
    try {
      window.parent.postMessage({ type: "netsanctum:dismiss" }, "*");
    } catch (error) {
      /* Nothing above us to tell. */
    }
  }, true);

  window.addEventListener("message", (event) => {
    const data = event.data;
    if (!data || typeof data !== "object") return;
    if (data.type === "netsanctum:dismiss") {
      if (overlay) dismiss();
      return;
    }
    if (data.type === "netsanctum:scan") {
      reportTargets();
      return;
    }
    if (data.type === "netsanctum:pick") {
      const target = reportIndexTargets()[data.index];
      if (target) submit(mediaRequest(target));
    }
  });

  /* The same order `reportTargets` sends, kept in step so an index means the same
     rectangle on both sides. */
  function reportIndexTargets() {
    return collectTargets()
      .filter((target) => target.rect.width >= 1 && target.rect.height >= 1)
      .slice(0, MAX_BOXES);
  }

  /* ── Messaging ────────────────────────────────────────── */

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (!message) return undefined;

    if (message.type === "netsanctum:arm") {
      arm();
      sendResponse({ ok: true, boxes: overlay ? overlay.host.__netsanctum.boxes.length : 0 });
      return false;
    }

    if (message.type === "netsanctum:suspend") {
      sendResponse({ ok: suspend() });
      return false;
    }

    if (message.type === "netsanctum:resume") {
      sendResponse({ ok: resume() });
      return false;
    }

    if (message.type === "netsanctum:viewport") {
      sendResponse({ width: window.innerWidth, height: window.innerHeight });
      return false;
    }

    if (message.type === "netsanctum:extract") {
      sendResponse(extractFields(message.fields));
      return false;
    }

    if (message.type === "netsanctum:note") {
      if (overlay) overlay.host.setAttribute("data-last-stage", String(message.text || ""));
      sendResponse({ ok: true });
      return false;
    }

    if (message.type === "netsanctum:resolve") {
      // Legacy single-shot path used by the context menu.
      const found = mediaFromAncestors(document.elementFromPoint(message.point?.x ?? 0, message.point?.y ?? 0) || document.body);
      resolveMedia(found).then(sendResponse);
      return true;
    }

    return undefined;
  });

  async function resolveMedia(found) {
    if (!found) return { kind: "page" };
    if (found.canvas && found.element && found.element.tagName && found.element.tagName.toLowerCase() === "canvas") {
      const inline = canvasDataUrl(found.element);
      if (inline) return { ...found, url: inline, inline: true };
      return { kind: "page" };
    }
    if (!found.url) return { kind: "page" };
    if (found.url.startsWith("data:")) return { ...found, inline: true };
    const inline = await inlineDataUrl(found.url);
    return inline ? { ...found, url: inline, inline: true } : found;
  }

  window.addEventListener("pagehide", cleanup);
})();
