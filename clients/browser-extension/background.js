/*
 * NetSanctum Capture — background page.
 *
 * The hotkey arms an overlay in the page; the user then clicks a media outline
 * or drags a region. This file does everything the page cannot: talk to
 * NetSanctum, read cross-origin media bytes, and crop the visible tab.
 */
(function () {
  "use strict";

  const DEFAULT_SETTINGS = {
    serverUrl: "",
    accessToken: "",
    collectionId: null,
    tags: [],
    screenshotFormat: "jpeg",
    screenshotQuality: 88,
    mediaMaxEdge: 2048,
    mediaMaxBytes: 8 * 1024 * 1024,
    openDashboard: true,
    videoQuality: "720",
  };

  const LOGIN_PATH = "/auth/login";
  const CAPTURE_PATH = "/api/vault/capture";
  const MENU_ID = "netsanctum-capture-page";

  let settings = Object.assign({}, DEFAULT_SETTINGS);
  let session = { token: null, expiresAt: 0 };

  function storageGet(keys) {
    return netsanctumStorageGet(keys);
  }

  /* A bare host is a common paste. Default it to the scheme the instance
   * actually serves, so `localhost:3000` does not silently become https. */
  function normalizeServerUrl(value) {
    const trimmed = String(value || "").trim().replace(/\/+$/, "");
    if (!trimmed) return "";
    if (/^https?:\/\//i.test(trimmed)) return trimmed;
    if (/^localhost(:\d+)?$|^127\.0\.0\.1(:\d+)?$/i.test(trimmed)) return `http://${trimmed}`;
    return `https://${trimmed}`;
  }

  async function loadSettings() {
    const stored = await storageGet(Object.keys(DEFAULT_SETTINGS));
    settings = Object.assign({}, DEFAULT_SETTINGS, stored);
    settings.serverUrl = normalizeServerUrl(settings.serverUrl);
    return settings;
  }

  function notify(title, message) {
    try {
      chrome.notifications.create({
        type: "basic",
        iconUrl: chrome.runtime.getURL("icons/icon128.png"),
        title,
        message,
      });
    } catch (error) {
      /* Notifications are a nicety; a denied permission must not fail a save. */
    }
  }

  async function ensureSession(force) {
    if (!settings.serverUrl || !settings.accessToken) {
      throw new Error("Set the NetSanctum address and access token in the extension options");
    }
    if (!force && session.token && Date.now() < session.expiresAt - 30000) return session.token;

    const response = await fetch(`${settings.serverUrl}${LOGIN_PATH}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: settings.accessToken }),
    });
    if (!response.ok) {
      throw new Error("NetSanctum rejected the access token");
    }
    const payload = await response.json();
    session = {
      token: payload.access_token,
      // The server advertises a 24h session; refresh early rather than on failure.
      expiresAt: Date.now() + Math.max(60, (payload.expires_in || 86400) - 60) * 1000,
    };
    return session.token;
  }

  async function apiFetch(path, options, retried) {
    const token = await ensureSession(false);
    const response = await fetch(`${settings.serverUrl}${path}`, {
      method: options.method || "POST",
      headers: Object.assign(
        { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        options.headers || {}
      ),
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    if (response.status === 401 && !retried) {
      await ensureSession(true);
      return apiFetch(path, options, true);
    }
    if (!response.ok) {
      let payload = null;
      try {
        payload = await response.json();
      } catch (error) {
        /* A non-JSON error body keeps the status-code message. */
      }
      throw new Error(netsanctumErrorText(response.status, payload, `NetSanctum returned ${response.status}`));
    }
    return response.json();
  }

  /* ── Image helpers ────────────────────────────────────── */

  function blobToDataUrl(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(new Error("Could not read the media bytes"));
      reader.readAsDataURL(blob);
    });
  }

  function loadImage(src) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = () => reject(new Error("Could not decode the image"));
      image.src = src;
    });
  }

  function approxBytes(dataUrl) {
    return Math.floor((dataUrl.length - dataUrl.indexOf(",") - 1) * 0.75);
  }

  /* Re-encode oversized pictures so one 12 MP photo cannot blow past the
   * server's 10 MiB ceiling on the embedded data-URL path. */
  async function normalizeImage(dataUrl) {
    if (!dataUrl.startsWith("data:image/")) return dataUrl;
    if (approxBytes(dataUrl) <= settings.mediaMaxBytes) return dataUrl;

    try {
      const blob = await (await fetch(dataUrl)).blob();
      const bitmap = await createImageBitmap(blob);
      const scale = Math.min(1, settings.mediaMaxEdge / Math.max(bitmap.width, bitmap.height));
      const width = Math.max(1, Math.round(bitmap.width * scale));
      const height = Math.max(1, Math.round(bitmap.height * scale));
      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      canvas.getContext("2d").drawImage(bitmap, 0, 0, width, height);
      bitmap.close();
      return canvas.toDataURL("image/jpeg", 0.85);
    } catch (error) {
      return dataUrl;
    }
  }

  /* captureVisibleTab returns device pixels; a CSS-pixel rect has to be scaled
   * by the same factor or the crop lands in the wrong place. The scale comes
   * from the page's own CSS viewport, which the content script reports — the
   * background page's window says nothing about the tab. */
  function scaleFor(image, viewport) {
    if (!viewport || !viewport.width) return 1;
    const scale = image.width / viewport.width;
    return scale > 0.25 && scale < 4 ? scale : 1;
  }

  async function cropToRect(dataUrl, rect, viewport, format, quality) {
    const image = await loadImage(dataUrl);
    const scale = scaleFor(image, viewport);
    const left = Math.max(0, Math.min(image.width - 1, Math.round(rect.left * scale)));
    const top = Math.max(0, Math.min(image.height - 1, Math.round(rect.top * scale)));
    const width = Math.min(image.width - left, Math.round(rect.width * scale));
    const height = Math.min(image.height - top, Math.round(rect.height * scale));
    if (width < 1 || height < 1) return dataUrl;

    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    canvas.getContext("2d").drawImage(image, left, top, width, height, 0, 0, width, height);
    return canvas.toDataURL(`image/${format}`, quality);
  }

  /* ── Payloads ─────────────────────────────────────────── */

  async function mediaImage(target) {
    if (target.inline && target.url && target.url.startsWith("data:")) return target.url;
    if (!target.url) throw new Error("That media has no downloadable source");

    const response = await fetch(target.url, { credentials: "include" });
    if (!response.ok) throw new Error(`The media returned ${response.status}`);
    const blob = await response.blob();
    if (!blob.type.startsWith("image/")) {
      throw new Error("Only image media can be stored as a Vault capture");
    }
    if (blob.size > settings.mediaMaxBytes) throw new Error("The media is larger than the capture limit");
    return blobToDataUrl(blob);
  }

  /* Only an addressable http(s) URL may travel to the server. A media element
     can easily be `blob:` (every YouTube player is) or `data:`, and those are
     meaningless once the page is gone — the server rejects them outright, which
     would fail the whole capture over an informational field. */
  function storableUrl(value) {
    if (!value) return null;
    const text = String(value);
    return /^https?:\/\//i.test(text) ? text : null;
  }

  function basePayload(kind, page, extra) {
    return Object.assign(
      {
        kind,
        title: (page.title || "NetSanctum capture").slice(0, 500),
        page_url: page.url,
        source_url: null,
        alt_text: null,
        tags: settings.tags,
        collection_id: settings.collectionId,
        auto_fetch_og: false,
      },
      extra
    );
  }

  function activePage(tab) {
    return {
      url: tab && tab.url ? tab.url.split("#")[0] : null,
      title: (tab && tab.title ? tab.title : "") || "NetSanctum capture",
    };
  }

  async function store(payload) {
    return apiFetch(CAPTURE_PATH, { body: payload });
  }

  /* ── Capture flow ─────────────────────────────────────── */

async function sendToTab(tabId, message) {
  try {
    return await netsanctumCall(chrome.tabs, "sendMessage", tabId, message);
  } catch (error) {
    // No content script: a browser page, a PDF viewer, the store.
    return null;
  }
}

/* Pages the browser refuses to script at all. Injecting there is not just
   pointless, it fails with an opaque error, so the user gets told why. */
function isUnscriptable(url) {
  if (!url) return true;
  return /^(?:chrome|chrome-extension|about|edge|moz-extension|devtools|view-source|chrome-untrusted):/i.test(url);
}

/* A tab that was already open when the extension was loaded or reloaded has no
 * content script, and declaring one in the manifest does not retroactively
 * inject it. Pulling the file in by hand is the fix, and the content script
 * refuses to install twice. */
async function ensureContentScript(tabId) {
  try {
    await netsanctumCall(chrome.tabs, "executeScript", tabId, { file: "content.js" });
    return true;
  } catch (error) {
    return false;
  }
}

  async function takeTabImage(tab) {
    return netsanctumCall(chrome.tabs, "captureVisibleTab", tab.windowId, {
      format: settings.screenshotFormat,
      quality: settings.screenshotQuality,
    });
  }

  /* A region or a bare click on the page: one screenshot, optionally cropped. */
  async function capturePageArea(tab, page, rect, viewport) {
    const full = await takeTabImage(tab);
    const dataUrl = rect
      ? await cropToRect(full, rect, viewport, settings.screenshotFormat, settings.screenshotQuality)
      : full;
    return basePayload("screenshot", page, { image: await normalizeImage(dataUrl) });
  }

  /* Media with no addressable bytes — a bare <video>, audio, a blob the page
   * would not hand over — still deserves a picture, so its outline becomes
   * the screenshot. */
  async function handleCapture(tab, request) {
    if (!tab || !tab.id) {
      return { ok: false, error: "The capture lost track of its tab", stage: "tab" };
    }
    let stage = "prepare";
    try {
      const page = activePage(tab);
      const viewport = request.viewport || null;
      stage = "unveil";
      const suspended = await sendToTab(tab.id, { type: "netsanctum:suspend" });
      stage = "capture";
      let payload;
      if (request.action === "video") {
        payload = await videoPayload(page, request);
      } else if (request.action === "media") {
        try {
          const image = await mediaImage(request.info);
          payload = basePayload("media", page, {
            title: (request.info.alt || request.info.title || page.title || "Media").slice(0, 500),
            source_url: storableUrl(request.info.url),
            alt_text: request.info.alt || null,
            image: await normalizeImage(image),
          });
        } catch (error) {
          // A video, or any media whose bytes will not come, still deserves a
          // picture: shoot the outline the user clicked.
          if (request.asVideo || !request.rect) throw error;
          payload = await capturePageArea(tab, page, request.rect, viewport);
        }
      } else if (request.action === "region") {
        payload = await capturePageArea(tab, page, request.rect, viewport);
      } else {
        payload = await capturePageArea(tab, page, null, viewport);
      }
      stage = "upload";
      const result = await store(payload);
      return { ok: true, message: result.message, title: result.title };
    } catch (error) {
      // Put the overlay back so the user can retry without re-arming.
      try {
        const suspended = await sendToTab(tab.id, { type: "netsanctum:resume" });
        if (suspended && suspended.ok) refreshHint(tab, stage);
      } catch (resumeError) {
        /* The overlay is gone either way; the failure below is what matters. */
      }
      return { ok: false, error: netsanctumSafeText(error, "The capture failed"), stage };
    }
  }

  /* Name the step that failed. "The capture failed" tells the user nothing when
     the report is the only thing anyone can see. */
  function refreshHint(tab, stage) {
    const labels = {
      unveil: "could not lift the overlay",
      capture: "could not photograph the page",
      upload: "could not reach NetSanctum",
      prepare: "could not read the page",
    };
    sendToTab(tab.id, { type: "netsanctum:note", text: labels[stage] || "failed" });
  }

  /* A clicked video is queued for archiving on the server. The still is only a
     preview: the server decides whether the source is archivable at all, and
     reports back if it is not. */
  async function videoPayload(page, request) {
    const still = request.still || null;
    const info = request.info || {};
    return basePayload("video", page, {
      title: (page.title || "Video").slice(0, 500),
      source_url: storableUrl(info.url),
      video_url: request.videoUrl || info.url || page.url,
      quality: settings.videoQuality,
      image: still ? await normalizeImage(still) : null,
      tags: settings.tags,
      collection_id: settings.collectionId,
      auto_fetch_og: false,
    });
  }

async function arm(tab) {
  await loadSettings();
  let response = await sendToTab(tab.id, { type: "netsanctum:arm" });
  if (!response) {
    if (isUnscriptable(tab.url)) {
      throw new Error("This page cannot be scripted — reload it, or use the right-click menu");
    }
    // A tab opened before the extension was loaded has no content script.
    if (await ensureContentScript(tab.id)) {
      response = await sendToTab(tab.id, { type: "netsanctum:arm" });
    }
  }
  if (response && response.ok) return;
  notify("NetSanctum capture", "This page cannot be dimmed; saving the visible tab instead");
  await captureTab(tab, false);
}

  async function captureTab(tab, forceScreenshot) {
    await loadSettings();
    if (!tab || !tab.id) throw new Error("There is no active tab to capture");

    const page = activePage(tab);
    const viewport = await sendToTab(tab.id, { type: "netsanctum:viewport" });
    if (forceScreenshot) {
      return store(await capturePageArea(tab, page, null, viewport));
    }

    const target = await sendToTab(tab.id, { type: "netsanctum:resolve", point: null });
    if (!target || target.kind === "page") return store(await capturePageArea(tab, page, null, viewport));
    return captureResolvedMedia(tab, target);
  }

  async function captureResolvedMedia(tab, info) {
    const page = activePage(tab);
    return store(
      basePayload("media", page, {
        title: (info.alt || info.title || page.title || "Media").slice(0, 500),
        source_url: storableUrl(info.url),
        alt_text: info.alt || null,
        image: await normalizeImage(await mediaImage(info)),
      })
    );
  }

  async function activeTab(tabId) {
    if (tabId) {
      try {
        return await netsanctumCall(chrome.tabs, "get", tabId);
      } catch (error) {
        /* fall through to the active tab */
      }
    }
    const active = await netsanctumCall(chrome.tabs, "query", { active: true, currentWindow: true });
    return active[0];
  }

  async function reportFailure(error) {
    const message = netsanctumSafeText(error && error.message ? error.message : error, "The capture failed");
    notify("NetSanctum capture failed", message);
    // Only a missing configuration is worth a tab; a failed capture should not
    // steal focus from whatever the user was reading.
    if (/address and access token/i.test(message)) {
      chrome.tabs.create({ url: chrome.runtime.getURL("options.html") });
    }
  }

  /* ── Entry points ─────────────────────────────────────── */

  chrome.commands.onCommand.addListener(async (command) => {
    const tab = await activeTab(null);
    if (!tab || !tab.id) return;
    try {
      if (command === "arm-capture") {
        await arm(tab);
        return;
      }
      const result = await captureTab(tab, command === "capture-page");
      notify("Saved to Vault", result.message || result.title);
      if (settings.openDashboard && settings.serverUrl) {
        chrome.tabs.create({ url: `${settings.serverUrl}/vault/dashboard` });
      }
    } catch (error) {
      await reportFailure(error);
    }
  });

  // The toolbar icon arms the same overlay the hotkey does.
  chrome.browserAction.onClicked.addListener(async (tab) => {
    try {
      await arm(await activeTab(tab && tab.id));
    } catch (error) {
      await reportFailure(error);
    }
  });

  chrome.contextMenus.onClicked.addListener(async (info, tab) => {
    try {
      const target = await activeTab(tab && tab.id);
      // The browser already resolved the hit target for a right-click, so
      // there is no need to guess a point in the page.
      const result = info.mediaType && info.srcUrl
        ? await captureResolvedMedia(target, { url: info.srcUrl, alt: null, title: null, inline: false })
        : await captureTab(target, false);
      notify("Saved to Vault", result.message || result.title);
    } catch (error) {
      await reportFailure(error);
    }
  });

  /* The page asks for the capture; this is where the veil comes off.

     Every exit must call sendResponse. A rejection that is not handled leaves
     the message channel open forever, the page waits forever, and the overlay
     sits on "SAVING…" with no way out. */
  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (!message || message.type !== "netsanctum:capture") return undefined;
    handleCapture(sender.tab || null, message.request || {}).then(sendResponse, (error) => {
      sendResponse({
        ok: false,
        error: netsanctumSafeText(error, "The capture failed"),
        stage: "handler",
      });
    });
    return true;
  });

  chrome.runtime.onInstalled.addListener(() => {
    chrome.contextMenus.removeAll(() => {
      chrome.contextMenus.create({
        id: MENU_ID,
        title: "Save to NetSanctum Vault",
        contexts: ["page", "image", "video", "audio", "link", "selection"],
      });
    });
  });

  loadSettings();
})();
