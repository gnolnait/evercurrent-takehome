"""What the model returns. Every field is required; optional fields are nullable (plan, Output models)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from digest.models import Attribute, Confidence, Frozen, Role, Severity, TaskEffect


class ExtractedEvidence(Frozen):
    message_ts: str
    quote: str


class ExtractedClaim(Frozen):
    entity: str  # id or alias
    attribute: Attribute
    value: str
    message_ts: str  # one of the item's evidence message_ts


class ExtractedTask(Frozen):
    task: str  # ChecklistTask.id from the prompt
    effect: TaskEffect
    message_ts: str  # one of the item's evidence message_ts


class ExtractedItem(Frozen):
    rationale: str  # first, before every other field
    item_id: str | None  # an existing item of this thread to update, else null
    type: Literal[
        "decision", "blocker", "risk", "schedule_change", "spec_change", "bom_change", "ask", "fyi"
    ]
    title: str
    summary: str
    entities: list[str]  # ids or aliases
    owner: str | None  # user id, mention or alias; for an ask, the person asked
    due: str | None  # YYYY-MM-DD
    affected_roles: list[Role]
    severity: Severity
    confidence: Confidence
    status: Literal["open", "resolved"]
    evidence: list[ExtractedEvidence] = Field(min_length=1)
    claims: list[ExtractedClaim]
    tasks: list[ExtractedTask]


class Extraction(Frozen):
    rationale: str
    items: list[ExtractedItem]


class TriageOutput(Frozen):
    rationale: str
    probability: float

    # A validator, not a schema bound: Anthropic structured outputs reject minimum/maximum,
    # and a validation failure is what makes the client retry once.
    @field_validator("probability")
    @classmethod
    def _unit_interval(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError(f"probability {value} is outside [0, 1]")
        return value
