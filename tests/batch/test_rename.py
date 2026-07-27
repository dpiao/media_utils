"""Tests for batch.rename planner and apply."""

from __future__ import annotations

import re
from pathlib import Path

from batch.rename import apply_renames, plan_renames


def test_plan_matches_nested_and_ignores_non_matches(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("x", encoding="utf-8")
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "a_1_b.txt").write_text("y", encoding="utf-8")
    (nested / "other.txt").write_text("z", encoding="utf-8")

    pattern = re.compile(r"^a_(\d+)_b\.txt$")
    plans = plan_renames(tmp_path, pattern, r"clip_\1.txt")
    assert len(plans) == 1
    assert plans[0].src == nested / "a_1_b.txt"
    assert plans[0].dest == nested / "clip_1.txt"
    assert plans[0].status == "rename"


def test_capture_groups_in_new_name(tmp_path: Path) -> None:
    (tmp_path / "1 (10).mp4").write_text("v", encoding="utf-8")
    pattern = re.compile(r"^(\d+) \((\d+)\)(\.[^.]+)$")
    plans = plan_renames(tmp_path, pattern, r"clip_\1_v\2\3")
    assert plans[0].dest.name == "clip_1_v10.mp4"
    assert plans[0].status == "rename"


def test_dry_run_does_not_rename(tmp_path: Path) -> None:
    src = tmp_path / "a_2_b.txt"
    src.write_text("y", encoding="utf-8")
    pattern = re.compile(r"^a_(\d+)_b\.txt$")
    plans = plan_renames(tmp_path, pattern, r"clip_\1.txt")
    apply_renames(plans, dry_run=True)
    assert src.is_file()
    assert not (tmp_path / "clip_2.txt").exists()


def test_apply_renames(tmp_path: Path) -> None:
    src = tmp_path / "a_3_b.txt"
    src.write_text("y", encoding="utf-8")
    pattern = re.compile(r"^a_(\d+)_b\.txt$")
    plans = plan_renames(tmp_path, pattern, r"clip_\1.txt")
    apply_renames(plans, dry_run=False)
    assert not src.exists()
    assert (tmp_path / "clip_3.txt").is_file()


def test_collision_skip(tmp_path: Path) -> None:
    (tmp_path / "a_1_b.txt").write_text("a", encoding="utf-8")
    (tmp_path / "clip_1.txt").write_text("exists", encoding="utf-8")
    pattern = re.compile(r"^a_(\d+)_b\.txt$")
    plans = plan_renames(tmp_path, pattern, r"clip_\1.txt")
    assert plans[0].status == "skip"
    assert "exists" in plans[0].reason
    apply_renames(plans, dry_run=False)
    assert (tmp_path / "a_1_b.txt").is_file()


def test_unchanged_when_same_name(tmp_path: Path) -> None:
    (tmp_path / "same.txt").write_text("x", encoding="utf-8")
    pattern = re.compile(r"^(same\.txt)$")
    plans = plan_renames(tmp_path, pattern, r"\1")
    assert plans[0].status == "unchanged"


def test_hidden_skipped_by_default(tmp_path: Path) -> None:
    (tmp_path / ".secret_1.txt").write_text("h", encoding="utf-8")
    (tmp_path / "a_1_b.txt").write_text("v", encoding="utf-8")
    pattern = re.compile(r"^a_(\d+)_b\.txt$|^\.secret_(\d+)\.txt$")
    plans = plan_renames(tmp_path, pattern, r"out_\1\2.txt")
    assert len(plans) == 1
    assert plans[0].src.name == "a_1_b.txt"

    plans_all = plan_renames(
        tmp_path, pattern, r"out_\1\2.txt", include_hidden=True
    )
    names = {p.src.name for p in plans_all}
    assert ".secret_1.txt" in names
