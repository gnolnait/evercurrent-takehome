"""The room reseeds against the corpus last pushed to Slack (M5 R1)."""

import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

from digest.cli import _RoomReseed
from digest.replay import read_events
from digest.seed import Post, SeedReport, SlackSeedClient, plan_seed
from digest.world.load import load_world
from digest.world.schema import World

WORLD_DIR = Path(__file__).resolve().parents[1] / "data/world/kestrel"
PUSHED = Path(".room/pushed-kestrel.jsonl")


class RecordingSeeder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[Post], list[Post]]] = []
        self.error: Exception | None = None

    def __call__(
        self,
        world: World,
        posts: Sequence[Post],
        client: SlackSeedClient,
        map_path: Path | None,
        *,
        reference: Sequence[Post],
        **options: object,
    ) -> SeedReport:
        self.calls.append((list(reference), list(posts)))
        if self.error is not None:
            raise self.error
        return SeedReport(
            posts=len(posts),
            files=0,
            channels=1,
            reactions_not_seeded=0,
            complete=True,
            map_path=Path("seed-map.json"),
        )


@pytest.fixture
def seeder(monkeypatch: pytest.MonkeyPatch) -> RecordingSeeder:
    fake = RecordingSeeder()
    monkeypatch.setattr("digest.cli.seed_corpus", fake)
    return fake


@pytest.fixture
def corpora(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    committed = tmp_path / "data/world/kestrel"
    copy = tmp_path / ".room/kestrel"
    shutil.copytree(WORLD_DIR, committed)
    shutil.copytree(committed, copy)
    events = copy / "events.jsonl"
    lines = events.read_bytes().splitlines(keepends=True)
    message = json.loads(lines[0])
    message["text"] = "Room edit: the release is on hold."
    lines[0] = (json.dumps(message) + "\n").encode("utf-8")
    events.write_bytes(b"".join(lines))
    monkeypatch.chdir(tmp_path)
    return committed, copy


def planned(committed: Path, events: Path) -> list[Post]:
    world = load_world(committed / "world.yaml")
    return plan_seed(world, list(read_events(events, world.workspace_url)), None)


def adapter(committed: Path) -> _RoomReseed:
    return _RoomReseed(cast(SlackSeedClient, object()), committed)


def test_first_real_run_uses_committed_reference_and_saves_copy(
    corpora: tuple[Path, Path], seeder: RecordingSeeder
) -> None:
    committed, copy = corpora
    expected_reference = planned(committed, committed / "events.jsonl")
    expected_posts = planned(committed, copy / "events.jsonl")
    assert expected_posts != expected_reference

    adapter(committed)(copy, dry_run=False)

    assert seeder.calls == [(expected_reference, expected_posts)]
    assert PUSHED.read_bytes() == (copy / "events.jsonl").read_bytes()


def test_second_real_run_uses_pushed_reference_after_revert(
    corpora: tuple[Path, Path], seeder: RecordingSeeder
) -> None:
    committed, copy = corpora
    pushed_posts = planned(committed, copy / "events.jsonl")
    committed_posts = planned(committed, committed / "events.jsonl")
    adapter(committed)(copy, dry_run=False)
    shutil.copyfile(committed / "events.jsonl", copy / "events.jsonl")

    adapter(committed)(copy, dry_run=False)

    assert seeder.calls == [
        (committed_posts, pushed_posts),
        (pushed_posts, committed_posts),
    ]
    reference, posts = seeder.calls[1]
    reference_text = {post.key: post.text for post in reference}
    assert sum(post.text != reference_text[post.key] for post in posts) == 1
    assert PUSHED.read_bytes() == (copy / "events.jsonl").read_bytes()


def test_dry_run_uses_committed_reference_without_saving(
    corpora: tuple[Path, Path], seeder: RecordingSeeder
) -> None:
    committed, copy = corpora

    adapter(committed)(copy, dry_run=True)

    assert seeder.calls == [
        (
            planned(committed, committed / "events.jsonl"),
            planned(committed, copy / "events.jsonl"),
        )
    ]
    assert not PUSHED.exists()


def test_seeder_error_reaches_caller_as_value_error_without_saving(
    corpora: tuple[Path, Path], seeder: RecordingSeeder
) -> None:
    committed, copy = corpora
    seeder.error = RuntimeError("seed failed")

    with pytest.raises(ValueError):
        adapter(committed)(copy, dry_run=False)

    assert seeder.calls == [
        (
            planned(committed, committed / "events.jsonl"),
            planned(committed, copy / "events.jsonl"),
        )
    ]
    assert not PUSHED.exists()
