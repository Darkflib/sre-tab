from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ApiModel(BaseModel):
    """Base for response models: buildable straight from ORM objects."""

    model_config = ConfigDict(from_attributes=True)


class ErrorResponse(BaseModel):
    """Shape of FastAPI's HTTPException body; used in OpenAPI responses."""

    detail: str
