"""Tests for the checker_eval dataset structure and ground-truth labels."""

import json
import py_compile
import re
from pathlib import Path

import pytest

DATASET = Path(__file__).resolve().parent.parent / "datasets" / "checker_eval"

CATEGORIES = {
    "correctness",
    "safety",
    "regression",
    "completeness",
    "performance_regression",
}

META_FIELDS = {
    "id", "name", "category", "polarity", "domain",
    "expected_label", "expected_status", "expected_category",
    "description", "flaw",
}

INTENT_FIELDS = {
    "connection", "queries", "transactions", "n_plus_one",
    "concurrency", "orm", "optimization_targets", "notes",
}

TAG_RE = re.compile(r"^[#-]{1,2}\s*ADCO_OPTIMIZED:")


def _samples() -> list[Path]:
    return sorted(p for p in DATASET.iterdir() if p.is_dir() and (p / "meta.json").is_file())


def _load(sample: Path, name: str) -> dict:
    with open(sample / name) as f:
        return json.load(f)


def test_dataset_has_20_samples():
    assert len(_samples()) == 20


def test_two_pos_two_neg_per_category():
    counts: dict[str, dict[str, int]] = {}
    for sample in _samples():
        meta = _load(sample, "meta.json")
        cat, pol = meta["category"], meta["polarity"]
        counts.setdefault(cat, {}).setdefault(pol, 0)
        counts[cat][pol] += 1
    for cat in CATEGORIES:
        assert counts[cat] == {"pos": 2, "neg": 2}, f"{cat}: {counts[cat]}"


def test_sample_dir_naming():
    pattern = re.compile(
        rf"^(\d{{2}})_({'|'.join(sorted(CATEGORIES))})_(pos|neg)_(.+)$"
    )
    for sample in _samples():
        m = pattern.match(sample.name)
        assert m, f"unexpected dir name: {sample.name}"
        num, cat, pol, _ = m.groups()
        assert num == sample.name[:2]
        assert cat in CATEGORIES, sample.name
        assert pol in ("pos", "neg"), sample.name


def test_ids_unique_and_sequential():
    ids = [_load(s, "meta.json")["id"] for s in _samples()]
    assert sorted(ids) == [f"{i:02d}" for i in range(1, 21)]


def test_meta_schema():
    for sample in _samples():
        meta = _load(sample, "meta.json")
        assert set(meta) == META_FIELDS, sample.name
        assert meta["category"] in CATEGORIES
        assert meta["polarity"] in ("pos", "neg")
        if meta["polarity"] == "pos":
            assert meta["expected_label"] == "Correct"
            assert meta["expected_status"] == "PASS"
            assert meta["expected_category"] is None
            assert meta["flaw"] == ""
        else:
            assert meta["expected_label"] == "Incorrect"
            assert meta["expected_status"] == "FAIL"
            assert meta["expected_category"] == meta["category"]


def test_intent_schema():
    for sample in _samples():
        intent = _load(sample, "intent.json")
        assert set(intent) == INTENT_FIELDS, sample.name
        assert isinstance(intent["optimization_targets"], list)
        for target in intent["optimization_targets"]:
            assert set(target) == {"file", "description"}, sample.name


def test_intent_targets_exist_in_optimized():
    for sample in _samples():
        intent = _load(sample, "intent.json")
        for target in intent["optimization_targets"]:
            assert (sample / "optimized" / target["file"]).is_file(), (
                f"{sample.name}: missing optimized/{target['file']}"
            )


def test_every_sample_has_tagged_file():
    for sample in _samples():
        tagged = []
        for f in (sample / "optimized").rglob("*"):
            if f.is_file():
                first = f.read_text(errors="ignore").splitlines()
                if first and TAG_RE.match(first[0]):
                    tagged.append(f.relative_to(sample / "optimized"))
                    assert sample.name in first[0], (
                        f"{sample.name}: tag value mismatch in {f}"
                    )
        assert tagged, f"{sample.name}: no ADCO_OPTIMIZED tag found in optimized/"


def test_no_tags_in_original():
    for sample in _samples():
        for f in (sample / "original").rglob("*"):
            if f.is_file():
                assert not TAG_RE.search(f.read_text(errors="ignore")), (
                    f"{sample.name}: tag in original/{f.name}"
                )


def test_tagged_files_are_first_line_only():
    for sample in _samples():
        for f in (sample / "optimized").rglob("*"):
            if not f.is_file():
                continue
            content = f.read_text(errors="ignore")
            if TAG_RE.search(content):
                assert TAG_RE.match(content.splitlines()[0]), (
                    f"{sample.name}: tag not on first line of {f}"
                )


def test_all_py_files_compile():
    for sample in _samples():
        for f in list((sample / "original").rglob("*.py")) + list(
            (sample / "optimized").rglob("*.py")
        ):
            try:
                py_compile.compile(str(f), doraise=True)
            except py_compile.PyCompileError as exc:
                pytest.fail(f"{sample.name}: {f} does not compile: {exc}")
