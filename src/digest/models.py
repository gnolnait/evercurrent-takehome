"""Shared types: roles, phases, the attribute vocabulary and the normalized Slack event."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = Literal["ME", "EE", "FW", "supply_chain", "EM", "PM"]
Phase = Literal["concept", "EVT", "DVT", "PVT", "ramp"]
Attribute = Literal[
    "rev", "lead_time", "gate_date", "owner", "status", "qty", "spec_param", "priority"
]
Priority = Literal["critical", "high", "normal", "low"]
Authority = Literal["owner_stated", "plm_bot", "cm_stated", "hearsay"]

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
