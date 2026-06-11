# Video Archiver Module Specification & API

The Video Archiver module manages YouTube downloads, metadata scraping, subtitles, and local video/audio streaming.

---

## 1. Directory Layout
* `models.py` - Stores schema details for `ArchivedVideo` and `Playlist`.
* `tasks.py` - Runs `yt-dlp` to download video streams, extract comments, parse metadata, and transcode media.
* `router.py` - Serves UI pages and provides JSON/media streaming endpoints.
* `templates/` - Contains dashboard UI.

---

## 2. API Reference

### Get Archived Videos
* **Method & Route:** `GET /api/video-archiver/videos`
* **Query Parameters:**
  - `search` (string, optional) - filter by title/channel.
  - `status` (string, optional) - filter by download status.
  - `is_deleted` (bool, optional) - filter by YouTube deletion state.
* **Returns:** JSON array of archived videos.

### Stream Video Binary
* **Method & Route:** `GET /api/video-archiver/videos/{video_id}/stream`
* **Returns:** Binary media stream (supporting HTTP range requests for seekable playback).

### Download Subtitles
* **Method & Route:** `GET /api/video-archiver/videos/{video_id}/subtitles/{lang}`
* **Returns:** Raw `.vtt` WebVTT subtitle track.

### Fetch Thumbnail
* **Method & Route:** `GET /api/video-archiver/videos/{video_id}/thumbnail`
* **Returns:** Image stream of the video cover.

### Delete Video
* **Method & Route:** `DELETE /api/video-archiver/videos/{video_id}`
* **Returns:** Confirmation message.
