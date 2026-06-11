# NetOutpost: Module-Agnostic Offline Proxy Architecture

This document describes the design specification for building a module-agnostic **NetOutpost** companion client (iOS/Android/Desktop). 

The core design principle is **"Smart Hybrid Caching Proxy"**, meaning the companion app has zero hardcoded business logic or UI elements for specific modules. Instead, it serves as a generic runtime shell that intercepts HTTP traffic and serves cached assets locally when offline.

---

## 1. Architectural Diagram

```text
+-----------------------------------------------------------------+
|                    NETOUTPOST COMPANION APP                     |
|                                                                 |
|   +-----------------------+           +---------------------+   |
|   |                       |           |                     |   |
|   |      WebView UI       |           |  Background Agent   |   |
|   |  (HTML/JS/HTMX Shell) |           |  (Package Sync)     |   |
|   |                       |           |                     |   |
|   +-----------+-----------+           +----------+----------+   |
|               | (HTTP requests)                  | (Download)   |
|               v                                  v              |
|   +-----------------------+           +---------------------+   |
|   |   Local HTTP Proxy /  |           |  Native Filesystem  |   |
|   |    Service Worker     |<--------->|  (Videos, EPUBs,    |   |
|   |  (Request Interceptor)|           |   JSON Metadata)    |   |
|   +-----------------------+           +---------------------+   |
+-----------------------------------------------------------------+
```

---

## 2. The Sync Manifest Contract

To download resources, NetSanctum modules generate a standardized, module-agnostic **Sync Manifest**. The client app parses this manifest to download assets without knowing their internal structure.

### Manifest Schema
```json
{
  "package_id": "ranobe_overlord_v1",
  "root_url": "/ranobelib/reader/overlord",
  "resources": [
    {
      "url": "/api/novels",
      "type": "json"
    },
    {
      "url": "/api/novel/1",
      "type": "json"
    },
    {
      "url": "/api/novel/1/export",
      "type": "binary"
    },
    {
      "url": "/api/cover/1",
      "type": "image"
    }
  ]
}
```

---

## 3. Communication Bridge (JS Bridge)

When running inside the NetOutpost WebView, the NetSanctum frontend detects the native wrapper and injects sync controls:

```javascript
// Detect presence of Outpost Bridge
if (window.NetOutpostBridge) {
    // Show native "Download to Device" buttons in the UI
    const syncButton = document.getElementById("sync-outpost-btn");
    syncButton.style.display = "block";
    
    syncButton.addEventListener("click", () => {
        // Post the manifest to the native shell
        window.NetOutpostBridge.postMessage(JSON.stringify({
            action: "DOWNLOAD_PACKAGE",
            manifest: manifestData
        }));
    });
}
```

---

## 4. Local Interception & Mocking

When the user goes offline, NetOutpost redirects the WebView to a local webserver (running on `localhost:9000`) or intercepts requests via native API hooks (`shouldInterceptRequest` on Android, `WKURLSchemeHandler` on iOS).

### Interception Logic
1. **Outgoing request:** WebView requests `/api/novels`.
2. **Proxy check:**
   * **If Online:** Forward request directly to NetSanctum server.
   * **If Offline:** 
     * Read the local file stored at `<Storage>/api/novels`.
     * Return the saved JSON bytes with standard headers (`Content-Type: application/json`).
3. **Binary Streaming (Videos):**
   * When playing a local video via `/api/video-archiver/videos/{id}/stream`, the local HTTP server supports range-requests (`HTTP 206 Partial Content`) reading directly from the native `.mp4` file on disk.

This keeps the codebase lightweight, 100% reusable, and completely decoupled from future modular extensions.
