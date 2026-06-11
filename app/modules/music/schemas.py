"""
Music module Pydantic schemas.
"""

from pydantic import BaseModel, Field


class VideoModel(BaseModel):
    title: str
    description: str
    comments: list[str]


class MusicModel(BaseModel):
    title: str = Field(
        description="The actual title of the song without extra tags like '(Official Video)' or 'Cover by'."
    )
    author: str = Field(
        description="The artist performing the specific version of the song in the video (e.g. the cover artist)."
    )
    original_artist: str | None = Field(
        None,
        description="The original author of the song, if this is a cover. Null if it is the original track.",
    )


class DownloadRequest(BaseModel):
    url: str
    use_ai: bool = Field(default=True, description="Whether to use AI for metadata analysis")
    openai_api_key: str | None = Field(None, description="Optional override for OpenAI API key")
    openai_base_url: str | None = Field(None, description="Optional override for OpenAI Base URL")
    youtube_cookies: str | None = Field(None, description="Optional cookies text in Netscape format")
    playlist_id: int | None = Field(None, description="Optional playlist ID to add downloaded song to")
