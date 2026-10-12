"""The renderer prompt and its request (plan B3.1)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from digest.corpus.avoid import AvoidNames
from digest.llm import LLMRequest, Message
from digest.world.schema import World, WorldEvent

RENDER_PROMPT_VERSION = "m2.1"
RENDER_SYSTEM = """\
You write Slack threads for a synthetic hardware engineering program. Each request \
describes one event. Write the thread in which the team discusses that event.

Style:
- Write short, informal engineering chat. Most messages are one or two sentences.
- Mention a person as <@USERID>, with the user id from the member list.
- Write part numbers and revisions in the forms the glossary lists: the entity id or \
one of its aliases, and a revision as "Rev B".
- Do not narrate the event or the story behind it. Do not invent people, parts or \
numbers that the request does not give.
- The poster states a claim, or a member who would know it from their role states it.
- When the noise knob indirection is on, each claim value is implied. Do not state it \
directly.
- When the noise knob delayed_crosspost is on, the thread restates a post that was made \
earlier in another place.
- Never write an event id, the scenario name or the word "scenario".

Output:
- Return one JSON object that matches the output schema.
- messages is the thread in order. The first message is the thread root. The poster of \
the event writes it.
- poster is the user id of the member who writes the message.
- day_offset is 0 for each message. The only exception is the answer to an answered \
ask: it has the day_offset that the request gives.
- minutes_after_start is the number of minutes from the start of the thread to the \
message. The root has the smallest value.
- files lists the names of the files that the message attaches.
- reactions lists the emoji reactions to the message. Each has the emoji name and the \
user id of the member who reacts.
- Write the number of messages that the request gives."""

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MESSAGE_COUNTS = {
    "fyi": "1 to 3",
    "bom_change": "1 to 3",
    "decision": "3 to 8",
    "risk": "3 to 8",
    "schedule_change": "3 to 8",
    "spec_change": "3 to 8",
    "blocker": "3 to 6",
    "distractor": "as the summary says, else 2 to 6",
}


def render_request(
    world: World,
    event: WorldEvent,
    earlier: Sequence[WorldEvent],
    avoid: AvoidNames,
    output_schema: dict[str, Any],
) -> LLMRequest:
    system = (
        f"{RENDER_SYSTEM}\n\nNever use these names, ids or phrases:\n"
        f"- Names: {', '.join(avoid.names)}\n"
        f"- Identifiers: {', '.join(avoid.identifiers)}\n"
        f"- Phrases: {', '.join(avoid.phrases)}"
    )
    return LLMRequest(
        model="render",
        system=system,
        messages=(Message(role="user", content=_user_message(world, event, earlier)),),
        output_schema=output_schema,
    )


def _user_message(world: World, event: WorldEvent, earlier: Sequence[WorldEvent]) -> str:
    personas = {persona.user_id: persona for persona in world.personas}
    entities = {entity.id: entity for entity in world.entities}
    channel = next(channel for channel in world.channels if channel.id == event.channel)
    poster = personas[event.poster]
    lines = [
        f"Program: {world.program}. {world.project.name}.",
        f"Channel: #{channel.name}. Purpose: {channel.purpose}.",
        "Members:",
    ]
    for persona in world.personas:
        if event.channel in persona.channels:
            role = persona.role or "bot"
            aliases = f"; also called {', '.join(persona.aliases)}" if persona.aliases else ""
            lines.append(f"- {persona.user_id}: {persona.name}, {role}{aliases}")
    lines += [
        f"Poster: {poster.user_id} ({poster.name}).",
        f"Day: {_day(world.date_of(event.day))}.",
        f"Kind: {event.kind}.",
        f"Summary: {event.summary}",
    ]
    if event.claims:
        lines.append("Claims:")
        for claim in event.claims:
            aliases = ", ".join(entities[claim.entity].aliases) or "none"
            lines.append(
                f"- {claim.entity} (aliases: {aliases}): {claim.attribute} is {claim.value}"
            )
    knobs = [key for key, on in event.noise.items() if on]
    if knobs:
        lines.append(f"Noise knobs that are on: {', '.join(knobs)}.")
    if event.ask is None:
        lines.append(f"Target message count: {_MESSAGE_COUNTS[event.kind]}.")
    else:
        asked = personas[event.ask.of]
        lines.append(f"The root asks {asked.user_id} ({asked.name}).")
        if event.ask.answered_day is None:
            lines.append(f"The ask is never answered. {asked.user_id} does not post in the thread.")
            lines.append("Target message count: 1 root message.")
        else:
            offset = event.ask.answered_day - event.day
            lines.append(
                f"{asked.user_id} answers in the last message, on "
                f"{_day(world.date_of(event.ask.answered_day))} (day_offset {offset})."
            )
            lines.append("Target message count: 1 root message plus 1 reply.")
    if earlier:
        lines.append("Earlier posts on the same topic:")
        lines.extend(f"- {_day(world.date_of(item.day))}: {item.summary}" for item in earlier)
    return "\n".join(lines)


def _day(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]} {day.isoformat()}"
