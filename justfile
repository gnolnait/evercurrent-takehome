# Task runner. `just` lists recipes. Every recipe is a thin wrapper over uv.
# Without just installed, run the uv command shown in each recipe directly.

set positional-arguments

default:
    @just --list --unsorted

# Install everything (runtime + slack + web extras + dev tools)
sync:
    uv sync --all-extras --dev

# Lint, format check, type check
lint:
    uv run ruff check .
    uv run ruff format --check .
    uv run ty check
    uv run lint-imports

# Auto-fix lint and formatting
fix:
    uv run ruff check --fix .
    uv run ruff format .

# Run the test suite; args pass through (e.g. `just test -k ranking`)
test *ARGS:
    uv run pytest "$@"

# Hook guard tests (Claude Code PreToolUse guards); run from the hooks dir so the
# standalone shims import their siblings the way Claude Code invokes them
test-hooks:
    cd .claude/hooks && uv run --project ../.. python -m pytest -q -n auto

# Everything CI runs
ci: lint test test-hooks

# Produce the demo digests from the bundled synthetic corpus (no API key needed)
demo:
    uv run digest demo

# Replay the synthetic corpus up to a date and print one user's digest
digest USER DATE:
    uv run digest run --user "$1" --as-of "$2"

# Run the evaluation suite against golden data
eval:
    uv run digest eval

# Prove a test is RED-GREEN (prove-test is installed by ~/Documents/dev-kit/install.ps1)
prove *ARGS:
    prove-test "$@"

# Regenerate the review-lens agents from .claude/review-lenses (kit script)
lenses:
    python ~/Documents/dev-kit/bin/gen_review_lens_agents.py
