"""Request schemas.

Policy payloads are accepted as plain JSON objects and validated by
``app.validation.validate_policy`` so that error messages can point at the
exact node path inside condition trees.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class EvaluateRequest(BaseModel):
    facts: dict[str, Any] = Field(default_factory=dict)

    @field_validator("facts")
    @classmethod
    def _facts_must_be_object(cls, v: Any) -> dict:
        if not isinstance(v, dict):
            raise ValueError("facts must be a JSON object")
        return v
