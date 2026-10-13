from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import digest.priors as priors_module
from digest.priors import DailyPriors, load_priors


@pytest.mark.parametrize("ratio", [0.5, 3, 10])
def test_load_priors_reads_file_and_derives_threshold(tmp_path: Path, ratio: float) -> None:
    path = tmp_path / "priors.yaml"
    path.write_text(
        yaml.safe_dump(
            {"triage": {"miss_cost_ratio": ratio}, "absence": {"ask_reply_cadence_days": 1}}
        ),
        encoding="utf-8",
    )
    priors = load_priors(path)
    assert priors.triage.miss_cost_ratio == ratio
    assert priors.triage.keep_threshold == 1 / (1 + ratio)
    assert priors.absence.ask_reply_cadence_days == 1


def test_load_bundled_priors() -> None:
    path = Path(__file__).resolve().parents[1] / "config/priors.yaml"
    priors = load_priors(path)
    assert priors.triage.miss_cost_ratio == 10
    assert priors.triage.keep_threshold == 1 / 11
    assert priors.absence.ask_reply_cadence_days == 2


@pytest.mark.parametrize(
    ("contents", "field"),
    [
        ("triage: {miss_cost_ratio: 0}\nabsence: {ask_reply_cadence_days: 2}", "miss_cost_ratio"),
        ("triage: {miss_cost_ratio: -1}\nabsence: {ask_reply_cadence_days: 2}", "miss_cost_ratio"),
        (
            "triage: {miss_cost_ratio: 10}\nabsence: {ask_reply_cadence_days: 0}",
            "ask_reply_cadence_days",
        ),
        (
            "triage: {miss_cost_ratio: 10}\nabsence: {ask_reply_cadence_days: -1}",
            "ask_reply_cadence_days",
        ),
        (
            "triage: {miss_cost_ratio: 10}\nabsence: {ask_reply_cadence_days: 2}\nunexpected: true",
            "unexpected",
        ),
        (
            "triage: {miss_cost_ratio: 10, unexpected: true}\nabsence: {ask_reply_cadence_days: 2}",
            "unexpected",
        ),
        (
            "triage: {miss_cost_ratio: 10}\nabsence: {ask_reply_cadence_days: 2, unexpected: true}",
            "unexpected",
        ),
        ("triage: {miss_cost_ratio: 10}", "absence"),
    ],
    ids=[
        "zero-ratio",
        "negative-ratio",
        "zero-cadence",
        "negative-cadence",
        "extra-root",
        "extra-triage",
        "extra-absence",
        "missing-absence",
    ],
)
def test_load_priors_rejects_invalid_file(tmp_path: Path, contents: str, field: str) -> None:
    path = tmp_path / "priors.yaml"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValidationError, match=field):
        load_priors(path)


def test_load_priors_yaml_error_names_file(tmp_path: Path) -> None:
    path = tmp_path / "priors.yaml"
    path.write_text("triage:\n  - [\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError) as excinfo:
        load_priors(path)
    assert str(path) in str(excinfo.value)
    assert "line 3" in str(excinfo.value)


def test_load_priors_validation_error_notes_file(tmp_path: Path) -> None:
    path = tmp_path / "priors.yaml"
    path.write_text(
        "triage: {miss_cost_ratio: 0}\nabsence: {ask_reply_cadence_days: 2}",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError) as excinfo:
        load_priors(path)
    assert str(path) in "".join(excinfo.value.__notes__)


@pytest.mark.parametrize("weight", [0, -0.1, 1.1], ids=["exactly-zero", "below-zero", "above-one"])
def test_load_priors_rejects_role_weight_outside_bounds(tmp_path: Path, weight: float) -> None:
    path = tmp_path / "priors.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "triage": {"miss_cost_ratio": 10},
                "absence": {"ask_reply_cadence_days": 2},
                "daily": {"role_priors": {"ME": {"decision": weight}}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as excinfo:
        load_priors(path)
    assert str(path) in "".join(excinfo.value.__notes__)


def test_load_priors_accepts_role_weight_of_one(tmp_path: Path) -> None:
    path = tmp_path / "priors.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "triage": {"miss_cost_ratio": 10},
                "absence": {"ask_reply_cadence_days": 2},
                "daily": {"role_priors": {"ME": {"decision": 1}}},
            }
        ),
        encoding="utf-8",
    )

    priors = load_priors(path)

    assert priors.daily.role_priors["ME"]["decision"] == 1


@pytest.mark.parametrize("source", ["model", "daily", "bundled"])
def test_window_priors_defaults(source: str) -> None:
    if source == "model":
        window = priors_module.WindowPriors()
    elif source == "daily":
        window = DailyPriors().window
    else:
        window = load_priors(
            Path(__file__).resolve().parents[1] / "config/priors.yaml"
        ).daily.window

    assert window.first_working_days == 5
    assert window.max_working_days == 10


@pytest.mark.parametrize(("first", "maximum"), [(1, 1), (3, 7), (10, 10)])
def test_window_priors_load_configured_values(tmp_path: Path, first: int, maximum: int) -> None:
    path = tmp_path / "priors.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "triage": {"miss_cost_ratio": 10},
                "absence": {"ask_reply_cadence_days": 2},
                "daily": {"window": {"first_working_days": first, "max_working_days": maximum}},
            }
        ),
        encoding="utf-8",
    )

    window = load_priors(path).daily.window

    assert window.first_working_days == first
    assert window.max_working_days == maximum


@pytest.mark.parametrize(
    ("first", "maximum", "field"),
    [
        (0, 10, "first_working_days"),
        (-1, 10, "first_working_days"),
        (1, 0, "max_working_days"),
        (1, -1, "max_working_days"),
    ],
)
def test_window_priors_reject_values_below_one(first: int, maximum: int, field: str) -> None:
    with pytest.raises(ValidationError) as raised:
        priors_module.WindowPriors(first_working_days=first, max_working_days=maximum)

    assert any(
        error["loc"] == (field,) and error["type"] == "greater_than_equal"
        for error in raised.value.errors()
    )


def test_window_priors_reject_first_above_maximum() -> None:
    with pytest.raises(ValidationError):
        priors_module.WindowPriors(first_working_days=11, max_working_days=10)
