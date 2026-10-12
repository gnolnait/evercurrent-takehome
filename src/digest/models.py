"""Shared types: roles, phases, the attribute vocabulary and the normalized Slack event."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = Literal["ME", "EE", "FW", "supply_chain", "EM", "PM"]
Phase = Literal["concept", "EVT", "DVT", "PVT", "ramp"]
Attribute = Literal[
    "rev", "lead_time", "gate_date", "owner", "status", "qty", "spec_param", "priority"
]
Priority = Literal["critical", "high", "normal", "low"]
Authority = Literal["owner_stated", "plm_bot", "cm_stated", "hearsay"]
ItemType = Literal[
    "decision",
    "blocker",
    "risk",
    "schedule_change",
    "spec_change",
    "bom_change",
    "ask",
    "fyi",
    "absence",
]
ItemStatus = Literal["open", "resolved", "superseded"]
Severity = Literal["low", "medium", "high"]
Confidence = Literal["low", "medium", "high"]
TaskEffect = Literal["evidence", "done"]

# Slack `ts`: ten integer digits, a dot, six fractional digits. It is an identity, never a float.
TS_PATTERN = r"^\d{10}\.\d{6}$"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class Frozen(BaseModel):
    """Base for every model in the package: immutable, unknown keys are errors."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Reaction(Frozen):
    name: str
    user_id: str
    ts: str = Field(pattern=TS_PATTERN)


class Event(Frozen):
    source: Literal["replay", "slack"]
    channel_id: str
    channel_name: str
    thread_ts: str = Field(pattern=TS_PATTERN)
    ts: str = Field(pattern=TS_PATTERN)
    event_ts: str = Field(pattern=TS_PATTERN)
    user_id: str
    text: str
    mentions: tuple[str, ...] = ()
    reactions: tuple[Reaction, ...] = ()
    files: tuple[str, ...] = ()
    permalink: str
    edited: bool = False
    deleted: bool = False

    @model_validator(mode="after")
    def _event_ts_not_before_ts(self) -> Self:
        if self.event_ts < self.ts:
            raise ValueError(f"event_ts {self.event_ts} is before ts {self.ts}")
        return self

    @property
    def thread_id(self) -> str:
        return f"{self.channel_id}:{self.thread_ts}"

    @property
    def ts_us(self) -> int:
        return ts_micros(self.ts)

    @property
    def at(self) -> datetime:
        return _EPOCH + timedelta(microseconds=ts_micros(self.ts))

    @property
    def observed_at(self) -> datetime:
        return _EPOCH + timedelta(microseconds=ts_micros(self.event_ts))


def ts_micros(ts: str) -> int:
    """Microseconds since the Unix epoch for a Slack `ts`."""
    seconds, fraction = ts.split(".")
    return int(seconds) * 1_000_000 + int(fraction)


def micros_since_epoch(moment: datetime) -> int:
    """Microseconds from the Unix epoch to an aware datetime, the integer form of a Slack `ts`."""
    return (moment - _EPOCH) // timedelta(microseconds=1)


def permalink_for(workspace_url: str, channel_id: str, ts: str, thread_ts: str) -> str:
    """Build the Slack permalink from its parts; a reply carries the thread query string."""
    link = f"{workspace_url}/archives/{channel_id}/p{ts.replace('.', '')}"
    if thread_ts != ts:
        link = f"{link}?thread_ts={thread_ts}&cid={channel_id}"
    return link


class Evidence(Frozen):
    """A verbatim span of the cited message; the permalink is constructed, never model-written."""

    message_ts: str = Field(pattern=TS_PATTERN)
    quote: str
    permalink: str


class Claim(Frozen):
    """A value stated by one message; its ts is the asserted time and its author the authority."""

    entity: str
    attribute: Attribute
    value: str
    message_ts: str = Field(pattern=TS_PATTERN)
    authority: Authority


class TaskLink(Frozen):
    """Progress on a checklist task, shown by one message; its ts is the evidence time."""

    task: str
    effect: TaskEffect
    message_ts: str = Field(pattern=TS_PATTERN)


class AbsenceDetail(Frozen):
    """Gate, task and buffer for a checklist absence; the asker for an ask; days in working days."""

    source: Literal["checklist", "ask"]
    gate: str | None
    task: str | None
    asker: str | None
    days_silent: int
    buffer_consumed: int | None
    buffer_days: int | None


class Item(Frozen):
    """One extracted unit of information (SPEC section 4), without `phase_tags` and `links`."""

    item_id: str
    thread_id: str
    type: ItemType
    title: str
    summary: str
    entities: tuple[str, ...]
    owner: str | None  # a user id; for an ask, the person asked
    due: date | None
    affected_roles: tuple[Role, ...]
    severity: Severity
    confidence: Confidence
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    first_seen: str = Field(pattern=TS_PATTERN)
    last_updated: str = Field(pattern=TS_PATTERN)
    claims: tuple[Claim, ...] = ()
    tasks: tuple[TaskLink, ...] = ()
    status: ItemStatus = "open"
    resolved_by: str | None = None
    absence: AbsenceDetail | None = None

    @model_validator(mode="after")
    def _absence_matches_type(self) -> Self:
        if (self.type == "absence") != (self.absence is not None):
            raise ValueError("absence detail is set exactly when type is absence")
        return self


class TriageDecision(Frozen):
    thread_id: str
    last_ts: str = Field(pattern=TS_PATTERN)
    prompt_version: str
    model: str  # the logical model name
    probability: float
    threshold: float
    kept: bool
    rationale: str
    decided_at: datetime


class GlossaryEntity(Frozen):
    id: str
    type: str
    description: str
    aliases: tuple[str, ...]


class GlossaryPerson(Frozen):
    user_id: str
    name: str
    role: Role | None
    aliases: tuple[str, ...]
    authority: Authority


class Glossary(Frozen):
    """What the extractor may name: entities with aliases, people with authority."""

    entities: tuple[GlossaryEntity, ...]
    people: tuple[GlossaryPerson, ...]
