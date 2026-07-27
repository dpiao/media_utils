"""Recursive regex rename planner and apply."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class RenamePlan:
    src: Path
    dest: Path
    status: Literal["rename", "skip", "unchanged"]
    reason: str = ""


def _is_hidden(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return any(part.startswith(".") for part in rel.parts)


def plan_renames(
    root: Path,
    pattern: re.Pattern[str],
    repl: str,
    *,
    include_hidden: bool = False,
) -> list[RenamePlan]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")

    plans: list[RenamePlan] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if not include_hidden and _is_hidden(path, root):
            continue
        if pattern.fullmatch(path.name) is None:
            continue
        new_name = pattern.sub(repl, path.name)
        if not new_name or new_name in (".", "..") or "/" in new_name or "\\" in new_name:
            plans.append(
                RenamePlan(
                    src=path,
                    dest=path,
                    status="skip",
                    reason="invalid replacement",
                ),
            )
            continue
        dest = path.with_name(new_name)
        if dest == path:
            plans.append(
                RenamePlan(src=path, dest=dest, status="unchanged", reason="same name"),
            )
            continue
        if dest.exists():
            plans.append(
                RenamePlan(
                    src=path,
                    dest=dest,
                    status="skip",
                    reason="target exists",
                ),
            )
            continue
        plans.append(RenamePlan(src=path, dest=dest, status="rename"))
    return plans


def apply_renames(plans: list[RenamePlan], *, dry_run: bool) -> list[RenamePlan]:
    if dry_run:
        return plans
    for plan in plans:
        if plan.status != "rename":
            continue
        plan.src.rename(plan.dest)
    return plans


def format_plan_line(plan: RenamePlan) -> str:
    label = {"rename": "RENAME", "skip": "SKIP", "unchanged": "KEEP"}[plan.status]
    line = f"{label}  {plan.src}  →  {plan.dest.name}"
    if plan.reason:
        line = f"{line}  [{plan.reason}]"
    return line
