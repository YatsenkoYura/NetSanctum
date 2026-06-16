"""
Pydantic schemas for Lib Network downloader.
"""

from pydantic import BaseModel


class DownloadRequest(BaseModel):
    url: str
    token: str | None = None
    seasons: list[str] | None = None
    episodes_range: str | None = None
    translation_team: str | None = None

