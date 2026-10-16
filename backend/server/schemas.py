import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class UserOut(_Out):
    id: uuid.UUID
    name: str


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class ConversationOut(_Out):
    id: uuid.UUID
    title: str | None
    created_at: datetime


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20000)


class MessageOut(_Out):
    id: uuid.UUID
    role: str
    content: str
    created_at: datetime


class RunOut(_Out):
    id: uuid.UUID
    conversation_id: uuid.UUID
    input_message_id: uuid.UUID
    output_message_id: uuid.UUID | None
    status: str
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
