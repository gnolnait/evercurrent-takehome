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


@pytest.mark.parametrize("source", ["model", "daily", "bundled"])
def test_ra10_recent_activity_defaults(source: str) -> None:
    if source == "model":
        model = getattr(priors_module, "RecentActivityPriors", None)
        assert callable(model), "priors must expose RecentActivityPriors"
        recent = model()
    else:
        daily = DailyPriors() if source == "daily" else load_priors().daily
        recent = getattr(daily, "recent_activity", None)
        assert recent is not None, "DailyPriors must expose recent_activity"
    assert recent.working_days == 3
    assert recent.relevance == 0.8


@pytest.mark.parametrize(("working_days", "relevance"), [(1, 0.400001), (3, 0.6), (7, 0.8)])
def test_ra10_recent_activity_loads_configured_values(
    tmp_path: Path, working_days: int, relevance: float
) -> None:
    path = tmp_path / "priors.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "triage": {"miss_cost_ratio": 10},
                "absence": {"ask_reply_cadence_days": 2},
                "daily": {
                    "recent_activity": {"working_days": working_days, "relevance": relevance}
                },
            }
        ),
        encoding="utf-8",
    )
    recent = getattr(load_priors(path).daily, "recent_activity", None)
    assert recent is not None
    assert recent.working_days == working_days
    assert recent.relevance == relevance


@pytest.mark.parametrize(
    ("field", "value", "error_type"),
    [
        ("working_days", 0, "greater_than_equal"),
        ("working_days", -1, "greater_than_equal"),
        ("working_days", 1.5, "int_from_float"),
        ("relevance", 0.4, "greater_than"),
        ("relevance", 0.3, "greater_than"),
        ("relevance", 0.800001, "less_than_equal"),
    ],
)
def test_ra10_recent_activity_rejects_invalid_values(
    field: str, value: float, error_type: str
) -> None:
    # Accept the valid pair first so rejecting an unknown field cannot pass this test.
    valid = DailyPriors.model_validate({"recent_activity": {"working_days": 1, "relevance": 0.6}})
    recent = getattr(valid, "recent_activity", None)
    assert recent is not None
    assert recent.relevance == 0.6
    with pytest.raises(ValidationError) as raised:
        DailyPriors.model_validate({"recent_activity": {field: value}})
    assert any(
        error["loc"] == ("recent_activity", field) and error["type"] == error_type
        for error in raised.value.errors()
    )


def test_ab5_terms_off_validation() -> None:
    data = load_priors().model_dump()
    data["daily"]["terms_off"] = {"importance", "recent_activity"}
    valid = priors_module.Priors.model_validate(data)
    assert isinstance(valid.daily.terms_off, frozenset)
    assert valid.daily.terms_off == frozenset({"importance", "recent_activity"})

    data["daily"]["terms_off"] = frozenset({"role_prior", "personal_relevance"})
    assert priors_module.Priors.model_validate(data).daily.terms_off == frozenset(
        {"role_prior", "personal_relevance"}
    )
    data["daily"]["terms_off"] = yaml.safe_load("[importance, importance, recent_activity]")
    listed = priors_module.Priors.model_validate(data).daily.terms_off
    assert isinstance(listed, frozenset)
    assert listed == frozenset({"importance", "recent_activity"})

    data["daily"]["terms_off"] = ["importance", "unknown"]
    with pytest.raises(ValidationError) as raised:
        priors_module.Priors.model_validate(data)
    assert any(
        error["loc"] == ("daily", "terms_off", 1)
        and error["type"] == "literal_error"
        and error["input"] == "unknown"
        for error in raised.value.errors()
    )
    for invalid in ("importance", None):
        data["daily"]["terms_off"] = invalid
        with pytest.raises(ValidationError) as raised:
            priors_module.Priors.model_validate(data)
        assert any(
            error["loc"] == ("daily", "terms_off") and error["type"] == "frozen_set_type"
            for error in raised.value.errors()
        )
    data["daily"].pop("terms_off")
    assert priors_module.Priors.model_validate(data).daily.terms_off == frozenset()


@pytest.mark.parametrize("source", ["model", "omitted", "bundled"])
def test_ab7_ablation_defaults(source: str) -> None:
    if source == "model":
        model = getattr(priors_module, "AblationPriors", None)
        assert callable(model), "priors must expose AblationPriors"
        ablation = model()
    else:
        data = load_priors().model_dump()
        if source == "omitted":
            data.pop("ablation", None)
        table = priors_module.Priors.model_validate(data)
        ablation = getattr(table, "ablation", None)
        assert ablation is not None, "Priors must expose ablation"
        if source == "bundled":
            path = Path(__file__).resolve().parents[1] / "config/priors.yaml"
            bundled = yaml.safe_load(path.read_text(encoding="utf-8"))
            assert bundled["ablation"]["min_effect"] == 0.01
    assert ablation.min_effect == 0.01


@pytest.mark.parametrize(
    "minimum", [None, 0.000001, 0.2, 1.0], ids=["omitted", "small", "fraction", "one"]
)
def test_ab7_ablation_loads_yaml(tmp_path: Path, minimum: float | None) -> None:
    data = load_priors().model_dump(mode="json")
    data.pop("ablation", None)
    if minimum is not None:
        data["ablation"] = {"min_effect": minimum}
    path = tmp_path / "ablation.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    ablation = getattr(load_priors(path), "ablation", None)
    assert ablation is not None, "Priors must expose ablation"
    assert ablation.min_effect == (0.01 if minimum is None else minimum)


@pytest.mark.parametrize(
    ("minimum", "error_type"),
    [(0.0, "greater_than"), (-0.01, "greater_than"), (1.000001, "less_than_equal")],
    ids=["zero", "negative", "above-one"],
)
def test_ab7_ablation_rejects_invalid_minimum(minimum: float, error_type: str) -> None:
    data = load_priors().model_dump()
    data["ablation"] = {"min_effect": 1.0}
    assert priors_module.Priors.model_validate(data).ablation.min_effect == 1.0
    data["ablation"] = {"min_effect": minimum}
    with pytest.raises(ValidationError) as raised:
        priors_module.Priors.model_validate(data)
    assert any(
        error["loc"] == ("ablation", "min_effect") and error["type"] == error_type
        for error in raised.value.errors()
    )
