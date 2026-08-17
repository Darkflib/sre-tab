from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.api.v1.schemas.common import ApiModel


class ReadStateUpdate(BaseModel):
    read: bool


class ReadStateOut(ApiModel):
    item_id: int
    read: bool
    read_at: datetime | None
