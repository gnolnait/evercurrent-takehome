"""Checks for the committed Kestrel corpus and extraction recording."""

from pathlib import Path

import pytest

from digest.eval.extraction import ExtractionReport, run_extraction_eval, write_report
from digest.eval.ranking import run_ranking_eval
from digest.llm.client import LLM, from_settings
from digest.priors import load_priors
from digest.replay import read_events
from digest.settings import Settings
from digest.world.load import load_world
from digest.world.manifest import load_manifest

ROOT = Path(__file__).parents[1]
WORLD_DIR = ROOT / "data" / "world" / "kestrel"
COMMITTED_REPORT = ROOT / "reports" / "eval-2026-10-23.md"


def _replay_llm() -> LLM:
    settings = Settings(
        _env_file=None,
        mode="replay",
        cassettes=ROOT / "data" / "cassettes",
        ANTHROPIC_API_KEY=None,
        OPENROUTER_API_KEY=None,
    )
    return from_settings(settings)


@pytest.fixture(scope="module")
def extraction_eval(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[ExtractionReport, Path]:
    output_dir = tmp_path_factory.mktemp("corpus-eval")

    report = run_extraction_eval(
        WORLD_DIR,
        _replay_llm(),
        load_priors(ROOT / "config" / "priors.yaml"),
        output_dir / "eval.sqlite",
    )

    return report, output_dir


def test_committed_eval_report_matches_replay(
    extraction_eval: tuple[ExtractionReport, Path],
) -> None:
    """The report with its Ranking section, from a fresh store for each eval, equals the file."""
    report, output_dir = extraction_eval
    # The ranking eval runs here and not in the fixture, so a failure of it fails this test only.
    ranking_report = run_ranking_eval(
        WORLD_DIR,
        _replay_llm(),
        load_priors(ROOT / "config" / "priors.yaml"),
        output_dir / "eval-ranking.sqlite",
    )
    written_report = output_dir / COMMITTED_REPORT.name
    write_report(report, written_report, ranking=ranking_report)

    assert written_report.read_bytes() == COMMITTED_REPORT.read_bytes()


def test_committed_recording_has_valid_citations_and_no_failed_threads(
    extraction_eval: tuple[ExtractionReport, Path],
) -> None:
    report, _ = extraction_eval

    assert report.citation_validity == 1.0
    assert report.extract_failed == 0


def test_manifest_covers_the_world_and_each_thread_has_an_event() -> None:
    world = load_world(WORLD_DIR / "world.yaml")
    manifest = load_manifest(WORLD_DIR / "manifest.json")
    event_threads = {
        (event.channel_id, event.thread_ts)
        for event in read_events(WORLD_DIR / "events.jsonl", world.workspace_url)
    }

    assert set(manifest.threads) >= {event.id for event in world.events}
    assert {
        (thread.channel_id, thread.thread_ts) for thread in manifest.threads.values()
    } <= event_threads


def test_eval_report_names_two_model_families(
    extraction_eval: tuple[ExtractionReport, Path],
) -> None:
    report, _ = extraction_eval
    families = {
        model.strip().partition("/")[0]
        for models in report.models.values()
        for model in models.split(",")
    }

    assert families == {"anthropic", "openai"}
