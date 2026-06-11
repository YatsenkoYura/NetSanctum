# SYSTEM PROMPT: Building the NetOutpost Offline Companion App

Use the following detailed instruction manual to build the **NetOutpost** offline companion application from scratch in a new, separate repository.

---

## 1. Project Overview & Objective

**NetOutpost** is a lightweight, cross-platform mobile companion app (Android & iOS) designed to complement a self-hosted media registry and downloader named **NetSanctum** (FastAPI-based).

### Core Principle: Module-Agnostic Caching Proxy WebView
The app must have **zero hardcoded UI** or database logic for specific content types (such as books, videos, or music). Instead, it acts as a generic "smart offline browser".
* **When Online:** The app opens NetSanctum's web interface in a fullscreen WebView.
* **When Syncing:** The webpage posts a standardized JSON **Sync Manifest** to the WebView's JS Bridge containing files to download. The app's native code downloads these files to local storage.
* **When Offline:** The WebView is redirected to a local HTTP server (running on `localhost:9000` or a custom scheme handler). A local Service Worker or interceptor intercepts all API fetches, serving the cached responses and files directly from the phone's disk.

---

## 2. Technical Stack Recommendation
You can implement this using **Flutter** (highly recommended for filesystem performance and background downloading) or **Capacitor (Ionic)** (to reuse web components). 

*If building with Flutter:*
* **WebView:** `flutter_inappwebview` (provides advanced request interception, custom schemes, and a robust Javascript bridge).
* **Storage:** `path_provider` + `dio` or `flutter_uploader` (for background downloads).
* **Local DB:** `sqflite` or `hive` (to index downloaded resources and track packages).
* **Local HTTP Server (Optional but recommended for video range requests):** `jaguar_http_server` or similar lightweight package.

---

## 3. Communication Contract (JS Bridge)

The web dashboard of NetSanctum will expose a Javascript API to the mobile client via:
`window.flutter_inappwebview.callHandler('NetOutpostBridge', data)` or `window.NetOutpostBridge.postMessage(data)`.

The app must register an event listener that captures messages:

```json
{
  "action": "DOWNLOAD_PACKAGE",
  "manifest": {
    "package_id": "video_123",
    "root_url": "/video-archiver/dashboard",
    "resources": [
      { "url": "/api/video-archiver/videos", "type": "json" },
      { "url": "/api/video-archiver/videos/123", "type": "json" },
      { "url": "/api/video-archiver/videos/123/stream", "type": "binary" },
      { "url": "/api/video-archiver/videos/123/thumbnail", "type": "image" }
    ]
  }
}
```

---

## 4. Local Interception Architecture (The Key Feature)

When the app detects that the device is **offline** or is requesting a path registered in the cached packages:
1. The WebView requests `/api/video-archiver/videos`.
2. The custom scheme handler or local proxy interceptor intercepts the call.
3. It maps the URL path to the downloaded file on disk (e.g. `<appData>/packages/video_123/api/video-archiver/videos`).
4. It reads the file and returns it as the HTTP response with the correct headers (e.g. `Content-Type: application/json` or `image/jpeg`).
5. **Partial Content (HTTP 206):** For binary files of type `"binary"` (e.g., video files `.mp4`), the local HTTP server must handle range requests so that the WebView's video player can seek and scrub smoothly.

---

## 5. Step-by-Step Implementation Guide for the Chatbot

You need to generate the following core blocks of code:

### Step 1: WebView Shell & Settings UI
* Create a simple entry screen where the user enters:
  - **NetSanctum Server URL** (e.g., `https://sanctum.myhouse.ru`)
  - **Master API Key** (used to authorize all background downloads).
* Load the WebView pointing to the Server URL. Inject the `MASTER_API_KEY` into headers or local storage.

### Step 2: JS Bridge Handler
* Listen to the `NetOutpostBridge` channel.
* Parse the `DOWNLOAD_PACKAGE` payloads.
* Store package metadata and sync status in a local SQLite database:
  - Table `packages` (id, root_url, status, date)
  - Table `resources` (id, package_id, relative_url, local_path, type)

### Step 3: Background Downloader Agent
* Implement a queue-based download worker.
* Download files sequentially using `Dio` or native platform downloaders.
* Append the `X-API-Key: <MASTER_API_KEY>` header to every request.
* Save files to standard local folders (e.g., `DocumentsDirectory/packages/<package_id>/...`).

### Step 4: Local HTTP Server & Scheme Interceptor
* Implement the custom scheme or local server interceptor.
* Handle requests when offline:
  - Match requested path against database of cached resource paths.
  - Return the file if found.
  - Return HTTP 404/503 if not found.

---

## 6. Prompt Output Goal
Generate the complete starting codebase for this application. Ensure that it includes a README explaining setup, dependency requirements, and handles permission requests (Storage/Network) on Android and iOS.
