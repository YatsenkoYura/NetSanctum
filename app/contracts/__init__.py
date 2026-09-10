"""Stable cross-module contracts shared by independent product modules."""

from app.contracts.video_archive_v1 import ArchiveVideoRequest, ArchiveVideoResult
from app.contracts.video_source_catalog_v1 import (
    VideoSourceItem,
    VideoSourceRequest,
    VideoSourceResult,
)

__all__ = (
    "ArchiveVideoRequest",
    "ArchiveVideoResult",
    "VideoSourceItem",
    "VideoSourceRequest",
    "VideoSourceResult",
)
