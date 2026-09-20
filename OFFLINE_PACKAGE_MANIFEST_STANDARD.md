# NetSanctum Offline Package Manifest Standard

Status: implementation standard
Manifest schema: `1`
Applies to: NetSanctum nodes, native clients, and every offline-capable module

## 1. Purpose

This document defines how a module describes an offline package and how a client installs,
updates, opens, and removes it.

The manifest is a complete snapshot of one logical package. It is not an append-only list and
it is not a delta. Delta synchronization is a client optimization derived from resource hashes.

The standard has four primary goals:

1. Repeating **Save to device** downloads only new or changed large resources.
2. A failed update never destroys the last usable offline package.
3. Identical bytes shared by multiple packages occupy storage only once.
4. Additions, changes, and removals have deterministic behavior across all modules.

The terms **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are normative.

## 2. Terms

- **Node**: the NetSanctum server that generates manifests and serves resources.
- **Client**: a native or web client that stores and opens packages offline.
- **Package**: a named offline snapshot identified by `(node_origin, package_id)`.
- **Resource**: one URL and its expected content, such as audio, video, JSON, or an NSP file.
- **Object**: locally stored bytes addressed by their SHA-256 digest.
- **NSP container**: an indexed package containing non-binary resources.
- **Current package**: the last completely verified and atomically published package version.
- **Update attempt**: one request to replace the current package with a newly fetched snapshot.

## 3. Manifest Shape

```json
{
  "schema_version": 1,
  "package_id": "playlist_42",
  "package_title": "Playlist: Example",
  "module": {
    "id": "music",
    "title": "Music",
    "root_url": "/music/dashboard"
  },
  "root_url": "/music/dashboard?package_id=playlist_42",
  "resources": [
    {
      "url": "/music/audio/7",
      "type": "binary",
      "size": 7340032,
      "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    },
    {
      "url": "/api/packages/playlist_42/nsp",
      "type": "container",
      "size": 65536,
      "sha256": "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
    }
  ]
}
```

### 3.1 Top-level fields

| Field | Requirement | Meaning |
| --- | --- | --- |
| `schema_version` | MUST | Contract version. Current value is `1`. |
| `package_id` | MUST | Stable identity of the logical package on one node. |
| `package_title` | MUST | Human-readable title; it MAY change without changing identity. |
| `module.id` | MUST | Explicit module identity. Clients MUST NOT infer it from URLs. |
| `module.title` | MUST | Human-readable module title. |
| `module.root_url` | MUST | Online module root. |
| `root_url` | MUST | Entry page for this exact offline package. |
| `resources` | MUST | Complete resource set for the current snapshot. |

Compatibility aliases such as `package_name`, `title`, `name`, `title_en`, and `title_ru` MAY be
present. Clients MUST NOT use them as package identity.

### 3.2 Package identity

The canonical package identity is:

```text
(normalized node origin, package_id)
```

`package_id` MUST:

- remain stable when the package title, metadata, or contents change;
- identify one logical selection, such as one song, one playlist, or the complete Vault;
- match `^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`;
- be validated strictly by the module package resolver;
- never be accepted as a loose prefix alias for another package.

Examples:

```text
song_7
playlist_42
video_dQw4w9WgXcQ
video_playlist_9
novel_11
vault_all
```

Deleting and recreating a different logical entity SHOULD produce a different `package_id`.

## 4. Resource Contract

Each resource contains:

| Field | Requirement | Meaning |
| --- | --- | --- |
| `url` | MUST | Same-origin absolute path, including a meaningful query string. |
| `type` | MUST | One of `binary`, `container`, `css`, `html`, `image`, `js`, `json`, `text`. |
| `size` | MUST for binary; SHOULD otherwise | Exact response body length in bytes. |
| `sha256` | MUST for binary; SHOULD otherwise | SHA-256 of the exact response body, encoded as lowercase hexadecimal. |

### 4.1 Resource identity

- Within a package, `url` is the logical resource identity.
- Across packages, `sha256` is the content identity.
- A stable URL MAY return new bytes in a later package snapshot, but then `sha256` and `size`
  MUST change.
- Changing a storage path MUST NOT require changing a public resource URL.
- Query parameters that affect the response are part of the resource identity.
- A manifest MUST NOT contain the same exact URL more than once.
- Producers MUST NOT rely on duplicate normalization choosing the first entry.

### 4.2 Hash and size requirements

The node SHOULD calculate `sha256` and `size` while initially writing or transforming an object,
not by reading a large file for every manifest request.

For legacy resources where either value is absent:

- the manifest remains valid;
- the client MUST download the resource;
- the client MUST still calculate a local SHA-256 before publication;
- the client MUST NOT perform pre-download reuse based only on URL;
- storage deduplication MAY still happen after download.

If both values are present, a client MAY reuse an app-private content-addressed object only when:

- its path corresponds to `sha256`;
- it is a regular file and not a symbolic link;
- its exact length equals `size`;
- the object store and package publication are protected from concurrent deletion.

## 5. Snapshot and Update Semantics

A manifest MUST describe the complete current package state.

Repeating **Save to device** for an existing package means:

```text
replace current package snapshot with this complete snapshot
```

It MUST NOT mean:

```text
append listed resources to the existing package
```

The client computes an update as follows:

1. Fetch and validate the complete manifest.
2. Stage every required resource without changing the current package.
3. Reuse resources whose complete `(sha256, size)` identity is already available.
4. Download and verify every missing or changed resource.
5. Build the complete new package-resource mapping.
6. Atomically replace package metadata and references in one database transaction.
7. Publish the new package as `ready`.
8. Remove old objects only when no package references them.

The old package MUST remain openable until step 6 succeeds.

### 5.1 Added resource

An added resource appears in the new full manifest.

- If its `(sha256, size)` exists locally, the client reuses it without a network download.
- Otherwise, the client downloads and verifies it.
- It becomes visible only when the complete package update is published.

### 5.2 Changed resource

A changed resource retains its logical URL but has a different hash and usually a different size.

- The client downloads the new bytes.
- The old bytes remain available through the current package during staging.
- After publication, the old object is removed only if no package references it.

Compression, re-encoding, repair, replacement, cover regeneration, and export regeneration are all
ordinary resource changes. Clients MUST NOT need feature-specific flags such as `compressed=true`.

### 5.3 Removed resource

A removed resource is absent from the new full manifest.

- Its package reference MUST disappear in the publication transaction.
- Its object MUST remain if another package still references it.
- Otherwise, it SHOULD be removed after publication or by startup garbage collection.

Tombstones are not required because absence from the authoritative snapshot is the tombstone.

### 5.4 Unchanged resource without identity metadata

If `sha256` or `size` is missing, the client cannot prove the resource is unchanged before download.
It MUST download it again. This is a valid compatibility fallback, not efficient delta sync.

## 6. Hybrid and NSP Packages

The default hybrid projection is:

- resources with `type: binary` remain standalone;
- all non-binary resources are packed into one `type: container` NSP resource;
- the NSP resource URL is `/api/packages/{package_id}/nsp`.

The NSP index MUST contain, for every packed entry:

- the exact resource URL;
- byte offset;
- byte length;
- MIME type;
- SHA-256.

An incomplete NSP MUST NOT be returned as a successful response.

A dynamically generated NSP without manifest-level `sha256` and `size` is downloaded again during
every package update. This is valid legacy behavior. A node that needs NSP delta reuse SHOULD build
or cache one deterministic container and expose its exact hash and size in the manifest.

Binary resources MUST NOT also be embedded in the NSP container.

## 7. Atomicity and Failure Handling

### 7.1 Required behavior

The client MUST stage and verify the complete replacement before changing the current package.

The following failures MUST leave the previous `ready` package unchanged:

- manifest request failure;
- invalid manifest;
- authentication failure;
- resource network failure;
- redirect policy violation;
- size mismatch;
- hash mismatch;
- resource or package safety limit violation;
- disk write, fsync, or rename failure before publication;
- database transaction failure.

New unreferenced objects from a failed attempt SHOULD be deleted immediately and MUST be eligible
for startup garbage collection.

### 7.2 Crash recovery

| Crash point | Required recovery |
| --- | --- |
| Before database publication | Keep the old package; remove staging and unreferenced objects later. |
| During resource download | Keep the old package; discard partial files. |
| After object creation, before publication | Keep the old package; startup GC removes orphan objects. |
| After publication, before old-object cleanup | Use the new package; startup GC removes old orphans. |
| During package deletion after DB commit | Keep the package deleted; startup GC finishes cleanup. |

SQLite and filesystem changes cannot form one cross-system transaction. The package database is the
source of truth. A manifest file stored beside a package is diagnostic metadata and MUST NOT override
the database state.

## 8. Concurrency

Clients MUST serialize mutations of one `(node_origin, package_id)`.

- Two refreshes of the same package MUST NOT publish partially interleaved resource mappings.
- A repeated request for the same manifest URL SHOULD be coalesced.
- If two distinct refresh requests target the same package, the latest requested valid update SHOULD
  win; an older in-flight request MUST NOT overwrite a newer committed request.
- A delete requested after a refresh began MUST prevent that older refresh from resurrecting the
  package.
- Object reuse, package publication, package deletion, and orphan cleanup MUST share a synchronization
  mechanism so a reusable object cannot be deleted between lookup and publication.

A process-local mutex is sufficient only while the client guarantees one process per data directory.
A multi-process client MUST additionally use an OS file lock or equivalent cross-process lease.

## 9. Shared Objects and Overlapping Packages

Multiple packages MAY reference the same content-addressed object.

Examples:

- `song_7` and `playlist_42` both contain `/music/audio/7`;
- two playlists contain the same song;
- `video_X` and `video_playlist_9` both contain the same video stream.

Rules:

- downloading a package SHOULD reuse an existing matching object without network access;
- replacing or deleting one package MUST NOT remove an object referenced by another package;
- reference checks MUST use canonical object identity, not only a package-local URL;
- orphan cleanup MUST recheck references while holding the same object-store synchronization used by
  publication;
- opening one specific package MUST resolve its own resources before considering another package.

Opening an entire offline module MAY combine several packages. If the same logical URL exists in
several packages, the newest `ready` package wins. Falling back to an older package is allowed only
when the newer package's referenced object is physically unavailable. JSON-array merging, if used,
MUST deduplicate by stable entity identity.

## 10. Security and Limits

The client and node MUST enforce:

- same-origin absolute-path resource URLs;
- no URL fragments;
- no cross-origin redirects for authenticated downloads;
- strict package ID validation;
- a bounded manifest size;
- a bounded resource count;
- per-resource and total-package byte limits;
- hash and size verification when supplied;
- content-addressed paths derived only from validated hexadecimal hashes;
- rejection of symbolic links in the private object store;
- temporary writes followed by fsync and atomic rename;
- Range serving without loading large media into memory.

The package remains read-only offline. Offline UI code MUST block state-changing requests such as
`POST`, `PUT`, `PATCH`, and `DELETE` unless a future contract explicitly defines offline mutations.

## 11. Edge Case Matrix

| Case | Network | Published result | Object cleanup |
| --- | --- | --- | --- |
| First install | Download all missing resources | New package | None |
| Same package, unchanged resource with hash+size | No resource GET | Reference reused | None |
| Same package, unchanged resource missing hash or size | Download again | Locally deduplicated | Temporary duplicate removed |
| One item added to a playlist | Download only missing identified resources | Full new snapshot | None |
| One item removed from a playlist | No GET for removed item | Reference removed | Delete if globally unreferenced |
| Bytes changed at the same URL | Download changed resource | New object referenced | Old object deleted if unreferenced |
| Metadata changed inside NSP | Download NSP | New package metadata | Old NSP deleted if unreferenced |
| Same bytes already exist in another package | No GET when hash+size are present | Shared reference added | Shared object retained |
| Manifest returns `404` | Manifest request only | Existing package unchanged | None |
| Resource returns `401` or `403` | Failed attempt | Existing package unchanged | Staged orphans removed |
| Network fails mid-resource | Partial download only | Existing package unchanged | Partial file removed |
| Hash or size mismatch | Resource downloaded then rejected | Existing package unchanged | Invalid bytes removed |
| Device runs out of space before publication | Failed attempt | Existing package unchanged | Best-effort cleanup, then startup GC |
| Database publication fails | Resources may be staged | Existing package unchanged | New unreferenced objects removed |
| App crashes before publication | Incomplete attempt | Existing package unchanged after restart | Startup cleanup |
| App crashes after publication | None required | New package remains current | Startup cleanup of old orphans |
| Package is deleted during an older refresh | Depends on cancellation point | Package stays deleted | Older staged resources removed |
| Two refreshes target the same package | Serialize/coalesce | Latest requested valid snapshot | Superseded objects GC'd |
| Shared object loses one package reference | No GET | Other packages remain valid | Object retained |
| Source entity is deleted on the node | Manifest may return `404` | Existing offline package remains until local deletion | None |

## 12. Example: Updating a Music Playlist

Initial package `playlist_42` contains songs `1`, `2`, and `3`. Song `4` is then added on the node.

The next manifest is still `playlist_42` and contains the complete set `1`, `2`, `3`, `4`.

With complete identity metadata:

```text
song 1: reuse local object
song 2: reuse local object
song 3: reuse local object
song 4: download and verify
NSP: download only if its identity changed or is unavailable
publish references for 1, 2, 3, 4 in one transaction
```

Without `sha256` and `size`, all four audio resources are downloaded again. Their bytes are still
deduplicated after download, but network delta synchronization is not achieved.

If song `2` is later removed, the next complete manifest contains `1`, `3`, `4`. Publication removes
the package reference to song `2`; its object remains if `song_2` or another playlist references it.

## 13. Server Implementation Checklist

Every offline-capable module MUST:

- define stable and strict package ID prefixes;
- provide a strict package resolver for NSP generation;
- generate the same complete resource set from the manifest endpoint and resolver;
- include package-scoped HTML and JSON required by the offline UI;
- include every binary needed for playback or reading;
- include exact `size` and `sha256` for every binary;
- update resource identity metadata whenever bytes change;
- keep public resource URLs stable across storage-path changes;
- produce deterministic ordering where order has user-visible meaning;
- avoid external resource URLs;
- verify that package mode performs no unlisted fetches;
- document whether source synchronization is a strict mirror or additive-only.

The module SHOULD test:

- exact package ID validation;
- full manifest contents;
- resolver and endpoint equivalence;
- known binary hash and size;
- legacy missing identity behavior where migration requires it;
- add, change, and remove item snapshots;
- stable ordering;
- hybrid projection;
- package-mode network closure.

## 14. Client Implementation Checklist

Every client MUST:

- validate schema, IDs, URLs, types, limits, size, and hash;
- treat a manifest as a full replacement snapshot;
- keep the old package `ready` while staging an update;
- use content-addressed object storage;
- reuse only resources with complete identity metadata;
- publish all package references in one transaction;
- retain shared objects;
- garbage-collect only unreferenced objects;
- clean partial downloads and startup orphans;
- serialize update and delete operations;
- preserve deterministic behavior across crashes;
- serve large media with HTTP Range support.

Required client tests include:

- unchanged/new/changed/removed resource updates;
- update failure preserving the old package;
- shared-object retention;
- hash and size mismatch;
- disk failure before publication;
- crash recovery before and after publication;
- concurrent refresh and delete;
- two refreshes for the same package;
- overlap precedence when opening an entire module;
- missing physical object fallback;
- legacy manifest fallback without hash or size.

## 15. Source Synchronization Is Separate

Package synchronization copies the node's current local state to a device. It does not define how the
node synchronizes a playlist or library with YouTube, a music service, or another upstream source.

Each module MUST separately document whether upstream synchronization is:

- a strict mirror, where upstream removals remove local membership; or
- additive-only, where existing local membership is preserved.

An offline manifest MUST always reflect the node's local database at manifest generation time.

## 16. Adoption Requirements

Current schema v1 allows missing `size` and `sha256` for compatibility. New implementations MUST
provide both for binary resources.

Existing modules should migrate in this order:

1. Store hash and size when ingesting or generating binary content.
2. Backfill legacy binary metadata in a background task.
3. Add hash and size to manifest resource entries.
4. Add snapshot update tests for additions, changes, and removals.
5. Add NSP-level identity when deterministic container reuse is needed.

Until a module completes these steps, its packages remain correct but repeat updates may download
unchanged resources again.

## 17. Current Provider Conformance

| Provider | Package identity | Binary identity | Snapshot behavior | Remaining compatibility behavior |
| --- | --- | --- | --- | --- |
| Music | Strict `song_<id>` and `playlist_<id>` | Persisted for audio; legacy files are backfilled once | Full ordered song/playlist snapshots | NSP metadata and covers are downloaded as one container |
| Video Archive | Strict `video_<id>` and `video_playlist_<id>` | Persisted for new, uploaded, and optimized video | Full video/playlist snapshots | Legacy video without identity is downloaded until optimization/backfill records it |
| AllLib | Strict `<media_type>_<id>` | Persisted for anime; EPUB/CBZ use immutable hashed artifacts | Deterministic chapter order and complete media snapshot | Old export artifacts are retained for in-flight manifests and require later retention cleanup |
| Vault | Strict `vault_all` | No standalone binary resources | One complete deterministic item snapshot; remote previews are excluded | Dynamic NSP is downloaded on each refresh |
| Storage | Strict `storage_manager` | No standalone binary resources | Read-only storage snapshot with a real package resolver | Dynamic NSP is downloaded on each refresh |

The native client performs full-snapshot replacement, pre-download CAS reuse, transactional reference
publication, shared-object retention, startup orphan collection, and monotonic refresh/delete ordering.
Its object-store coordination is process-local; multiple client processes MUST NOT share one data
directory until cross-process locking is implemented.
