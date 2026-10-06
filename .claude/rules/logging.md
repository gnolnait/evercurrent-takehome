---
name: logging
description: |
  Log line shape — event token plus structured fields, never values interpolated
  into the message. Loads when editing any source file under src/**.
paths:
  - "src/**/*.py"
---
# Log line shape

A log call is an **event token plus kwargs**, not prose: a snake_case event name first
(greppable, aggregatable), every variable as a structured field. Never interpolate
values into the message with an f-string or `%`/`.format()`: structured fields pass
through the serializer (where redaction and typing live); an interpolated message
smuggles raw values past it. (With stdlib `logging`, the fields go in `extra={...}`.)

```python
# good
log.warning("channel_fetch_failed", channel_id=channel_id, status=resp.status_code)

# bad
log.warning(f"Failed to fetch channel {channel_id}: {resp.status_code}")
```
