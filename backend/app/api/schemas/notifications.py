from pydantic import BaseModel, Field


class InboundEmailRequest(BaseModel):
    sender: str = Field(min_length=1, max_length=255)
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=10000)