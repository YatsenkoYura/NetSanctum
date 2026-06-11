# NetSanctum: API-First Architecture & Specification

This document details the API-first specification of NetSanctum, designed to decouple front-end presentation from back-end logic. This ensures that the platform is 100% prepared to act as the source registry for offline-first replicas like **NetOutpost**.

---

## 1. Architectural Core Principles

1. **Decoupled Interfaces:** All business logic (scraping, downloading, transcoding, book packaging, cryptography) is handled strictly by FastAPI route functions and Celery tasks.
2. **Standard REST Endpoints:** The frontend communicates with the server via REST APIs (`/api/...`). HTMX endpoints (`/ui/...`) simply query these databases and render HTML partials, guaranteeing that the underlying API layer remains completely independent.
3. **JSON-First Data Exchange:** Metadata is represented as standard serialized JSON dictionaries.

---

## 2. Authentication Protocol

All API calls from an external companion (like NetOutpost) must include the master credentials:
* **Header-based:** `X-API-Key: <MASTER_API_KEY>` or standard bearer auth.
* **Query-based (fallback):** `?token=<MASTER_API_KEY>`

---

## 3. Video Archiver API Surface

The `/api/video-archiver/` scope provides programmatic management of YouTube/video content:

### Get Archived Videos
* **Method & Route:** `GET /api/video-archiver/videos`
* **Query Parameters:**
  - `search` (string, optional) - filter by title or channel.
  - `status` (string, optional) - filter by download status.
  - `is_deleted` (bool, optional) - filter by YouTube deletion state.
* **Returns:** JSON Array of archived video objects.
* **Response Example:**
```json
[
  {
    "id": 1,
    "youtube_id": "dQw4w9WgXcQ",
    "title": "Never Gonna Give You Up",
    "channel_name": "Rick Astley",
    "video_path": "videos/dQw4w9WgXcQ.mp4",
    "thumbnail_path": "thumbnails/dQw4w9WgXcQ.jpg",
    "duration": 212,
    "archived_at": "2026-06-11T12:00:00"
  }
]
```

### Stream Video Binary
* **Method & Route:** `GET /api/video-archiver/videos/{video_id}/stream`
* **Returns:** Binary media stream (supporting HTTP range requests for seekable playback).

### Download Subtitles
* **Method & Route:** `GET /api/video-archiver/videos/{video_id}/subtitles/{lang}`
* **Returns:** Raw `.vtt` WebVTT subtitle tracks.

### Fetch Thumbnail
* **Method & Route:** `GET /api/video-archiver/videos/{video_id}/thumbnail`
* **Returns:** Image stream of the video cover.

---

## 4. RanobeLib API Surface

The `/api/` scope of the `ranobelib` module provides novel indexing and export capabilities:

### List Novels
* **Method & Route:** `GET /api/novels`
* **Query Parameters:** `search` (string, optional)
* **Returns:** JSON array of novel objects.
* **Response Example:**
```json
[
  {
    "id": 1,
    "title": "Overlord",
    "eng_name": "Overlord",
    "rus_name": "Повелитель",
    "slug": "overlord",
    "cover_path": "ranobe/covers/overlord.jpg"
  }
]
```

### Novel Details & Chapter list
* **Method & Route:** `GET /api/novel/{novel_id}`
* **Returns:** Novel object containing nested chapter metadata.
* **Response Example:**
```json
{
  "id": 1,
  "title": "Overlord",
  "chapters": [
    {
      "id": 412,
      "volume": "1",
      "number": "1",
      "name": "The End and the Beginning"
    }
  ]
]
```

### Export Novel as EPUB
* **Method & Route:** `GET /api/novel/{novel_id}/export`
* **Returns:** Complete packaged `.epub` archive containing all chapters, metadata, covers, and fully offline-bundled images.

---

## 5. Front-Back Separation Audit

An audit of the front-end scripts (`video_dashboard.html`, `ranobe_dashboard.html`) confirms:
* **No local processing:** Browser scripts only perform display updates, modal triggering, and local video playback management.
* **No Database Operations in Templates:** HTML templates render variables directly passed by route models, with zero side-effects.
* **Readiness for NetOutpost:** A sync client only needs to call `GET /api/video-archiver/videos` and `GET /api/novels` to index content, then pull files via `/api/video-archiver/videos/{id}/stream` and `/api/novel/{id}/export` respectively.
