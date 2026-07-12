import uuid
from pydantic import BaseModel, Field, field_validator


class SelectionIn(BaseModel):
    source_id: uuid.UUID
    page_no: int = Field(ge=1)
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("selection text must not be blank")
        return v.strip()


class SelectionOut(BaseModel):
    selection_id: uuid.UUID
    source_id: uuid.UUID
    source_title: str
    page_no: int
    text: str
