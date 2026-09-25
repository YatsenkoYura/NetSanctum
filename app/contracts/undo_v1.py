"""Contract for undoing a step the assistant already took.

Every undo integration speaks the same shape: it receives the arguments of the call it
has to reverse, and the application decides what that means. Reversibility is declared
by the provider, never guessed by the agent.
"""

from typing import Any

from pydantic import BaseModel, Field, field_validator

CONTRACT_ID = "undo.v1"
MAX_ARGUMENT_BYTES = 2_048


class UndoRequest(BaseModel):
    """The arguments of the original call, plus the flag that means "reverse it"."""

    undo: bool = Field(default=True, description="Always true: this integration only undoes.")
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        max_length=20,
        description="Arguments the assistant used for the original call.",
    )

    @field_validator("arguments")
    @classmethod
    def bound_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(str(value).encode()) > MAX_ARGUMENT_BYTES:
            raise ValueError("Undo arguments are too large")
        return value


class UndoResult(BaseModel):
    status: str = Field(max_length=32)
    detail: str = Field(default="", max_length=300)
