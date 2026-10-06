# Downstream: a role- and phase-aware daily digest for hardware teams on Slack

Status: scaffold. The design doc replaces this file as milestones land (see `SPEC.md`,
`docs/decisions.md`, `docs/process.md`).

Thesis: a digest is the diff between what the project knows and what you were last told,
plus what should have happened but didn't.

## Run

```bash
uv sync --all-extras --dev
uv run digest --help
uv run pytest
```

Everything runs offline by default (`DIGEST_MODE=replay`); no API keys are needed to
run the demo, the tests or CI.
