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
.root { position: fixed; inset: 0; z-index: 2147483647; font: 500 12px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; cursor: crosshair; }
.backdrop { position: absolute; inset: 0; background: rgba(8, 10, 12, 0.55); }
.box { position: absolute; z-index: 1; border: 1.5px solid rgba(45, 212, 191, 0.85); background: rgba(45, 212, 191, 0.08); pointer-events: none; }
.box.hover { border-color: #5eead4; background: rgba(94, 234, 212, 0.2); box-shadow: 0 0 0 1px rgba(94, 234, 212, 0.5), 0 0 18px rgba(45, 212, 191, 0.35); }
.tag { position: absolute; z-index: 5; max-width: 320px; padding: 4px 8px; border: 1px solid rgba(94, 234, 212, 0.6); background: rgba(6, 20, 22, 0.95); color: #ccfbf1; font: 500 11px/1.45 ui-monospace, monospace; pointer-events: none; overflow-wrap: anywhere; }
.tag .k { color: #5eead4; font-weight: 700; }
.tag .d { color: #94a3b8; }
.tag .t { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; margin-top: 2px; color: #e2e8f0; }
.tag.below { transform: none; }
.selection { position: absolute; z-index: 3; border: 1.5px dashed #5eead4; background: rgba(94, 234, 212, 0.14); box-shadow: 0 0 0 9999px rgba(8, 10, 12, 0.55); pointer-events: none; }
.hint { position: absolute; z-index: 6; left: 50%; bottom: 28px; transform: translateX(-50%); display: flex; gap: 18px; align-items: center; padding: 10px 18px; background: #0b1113; border: 1px solid rgba(45, 212, 191, 0.5); color: #ccfbf1; white-space: nowrap; }
.hint kbd { padding: 1px 5px; background: #134e4a; border: 1px solid #2dd4bf; border-radius: 2px; color: #5eead4; font: inherit; }
.hint .err { color: #fca5a5; }
.busy { position: absolute; inset: 0; z-index: 10; display: flex; align-items: center; justify-content: center; background: rgba(8, 10, 12, 0.72); color: #5eead4; font: 600 13px/1 ui-monospace, monospace; letter-spacing: 0.08em; }
`;

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
    hint.innerHTML =
      "<span>Click a teal outline to save that media</span>" +
      "<span>Drag to screenshot a region</span>" +
      "<span><kbd>Esc</kbd> cancel</span>";

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
        videoUrl: videoSourceUrl(entry.element) || pageUrl(),
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
    if (tag === "iframe" || tag === "embed") return true;
    return false;
  }

  /* The page URL, not the stream URL: a blob: or CDN segment is not something
     the server can archive, but the watch page usually is. */
  function videoSourceUrl(element) {
    const candidate = element.currentSrc || element.getAttribute("src") || "";
    if (!candidate || candidate.startsWith("blob:") || candidate.startsWith("data:")) return null;
    return absolute(candidate);
  }

  function pageUrl() {
    return location.href.split("#")[0];
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

  function renderTag(entry) {
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
    if (overlay) {
      overlay.hint.textContent = "";
      overlay.hint.append("Already armed");
      return;
    }
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
      const counter = document.createElement("span");
      counter.textContent = `${boxes.length} media found`;
      overlay.hint.appendChild(counter);
    }

    let hovered = null;
    let drag = null;

    const targetAt = (x, y) => {
      for (let index = boxes.length - 1; index >= 0; index -= 1) {
        const entry = boxes[index];
        const r = entry.rect;
        if (x >= r.left && x <= r.left + r.width && y >= r.top && y <= r.top + r.height) return entry;
      }
      return null;
    };

    const onMove = (event) => {
      if (drag) {
        drag.x = event.clientX;
        drag.y = event.clientY;
        paint();
        return;
      }
      const found = targetAt(event.clientX, event.clientY);
      if (found === hovered) {
        // Still hovering the same outline: keep the tooltip pinned to it even
        // when the page scrolls underneath.
        if (hovered) renderTag(hovered);
        return;
      }
      if (hovered) hovered.box.classList.remove("hover");
      hovered = found;
      if (hovered) {
        hovered.box.classList.add("hover");
        renderTag(hovered);
      } else {
        hideTag();
      }
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
     * full-bleed banner. */
    const onDown = (event) => {
      if (event.button !== 0) return;
      const found = event.altKey ? null : targetAt(event.clientX, event.clientY);
      if (found) {
        submit(mediaRequest(found));
        return;
      }
      drag = { startX: event.clientX, startY: event.clientY, x: event.clientX, y: event.clientY };
      paint();
    };

    const onUp = (event) => {
      if (!drag) return;
      const moved = Math.abs(event.clientX - drag.startX) > CLICK_SLOP || Math.abs(event.clientY - drag.startY) > CLICK_SLOP;
      const rect = clampRect({
        left: Math.min(drag.startX, event.clientX),
        top: Math.min(drag.startY, event.clientY),
        right: Math.max(drag.startX, event.clientX),
        bottom: Math.max(drag.startY, event.clientY),
      });
      drag = null;
      overlay.selection.style.display = "none";
      // A click on empty page with no drag means "the page, as seen".
      if (!moved || rect.width < 1 || rect.height < 1) {
        submit({ action: "viewport", viewport: viewportSize() });
        return;
      }
      submit({ action: "region", rect, viewport: viewportSize() });
    };

    const onKey = (event) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      dismiss();
    };

    overlay.root.addEventListener("pointermove", onMove);
    overlay.root.addEventListener("pointerdown", onDown);
    overlay.root.addEventListener("pointerup", onUp);
    overlay.root.addEventListener("pointerleave", hideTag);
    overlay.root.addEventListener("contextmenu", dismiss);
    // A stray wheel event must not scroll the page behind the veil.
    overlay.root.addEventListener("wheel", (event) => event.preventDefault(), { passive: false });
    window.addEventListener("keydown", onKey, true);

    overlay.host.__netsanctum = { onMove, onDown, onUp, onKey, boxes };
  }

  function cleanup() {
    suspended = null;
    if (!overlay) return;
    const handlers = overlay.host.__netsanctum;
    if (handlers) {
      overlay.root.removeEventListener("pointermove", handlers.onMove);
      overlay.root.removeEventListener("pointerdown", handlers.onDown);
      overlay.root.removeEventListener("pointerup", handlers.onUp);
      overlay.root.removeEventListener("pointerleave", hideTag);
      overlay.root.removeEventListener("contextmenu", dismiss);
      window.removeEventListener("keydown", handlers.onKey, true);
    }
    overlay.host.remove();
    overlay = null;
  }

  function dismiss() {
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
