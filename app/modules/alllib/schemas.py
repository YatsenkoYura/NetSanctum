"""
Pydantic schemas for Lib Network downloader.
"""

from pydantic import BaseModel, Field


class DownloadRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    token: str | None = Field(default=None, max_length=8192)
    seasons: list[str] | None = None
    episodes_range: str | None = None
    translation_team: str | None = None
    content_language: str | None = Field(default=None, min_length=2, max_length=16, pattern=r"^[a-z-]+$")
    branch_id: str | None = Field(default=None, min_length=1, max_length=128)
