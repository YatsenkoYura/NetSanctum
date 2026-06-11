# RanobeLib Module Specification & API

The RanobeLib module downloads light novels (ranobe) from RanobeLib, stores chapter XHTML bodies, downloads and embeds illustrations, and packages books as EPUB files for offline reading.

---

## 1. Directory Layout
* `models.py` - Stores schema details for `RanobeNovel` and `RanobeChapter`.
* `tasks.py` - Connects to RanobeLib API, parses document nodes to clean XHTML, and stores contents.
* `epub_builder.py` - Bundles HTML contents and downloads remote images to package a valid standalone EPUB.
* `router.py` - Serves UI pages, reader views, and exposes REST APIs.

---

## 2. API Reference

### List Novels
* **Method & Route:** `GET /api/novels`
* **Query Parameters:** `search` (string, optional)
* **Returns:** JSON array of novel objects.

### Novel Details & Chapter list
* **Method & Route:** `GET /api/novel/{novel_id}`
* **Returns:** Novel object containing nested chapter metadata.

### Export Novel as EPUB
* **Method & Route:** `GET /api/novel/{novel_id}/export`
* **Returns:** Complete packaged `.epub` archive containing all chapters, metadata, covers, and fully offline-bundled images.

### Trigger Download
* **Method & Route:** `POST /api/download`
* **Request Body:** `{"url": "https://ranobelib.me/..."}`
* **Returns:** Scheduled task ID.
