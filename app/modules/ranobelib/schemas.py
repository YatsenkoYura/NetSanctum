from pydantic import BaseModel


class DownloadRequest(BaseModel):
    url: str
