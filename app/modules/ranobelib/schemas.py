from pydantic import BaseModel
from typing import Optional

class DownloadRequest(BaseModel):
    url: str
