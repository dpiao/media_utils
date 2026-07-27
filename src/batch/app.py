"""CLI for batch file operations."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from batch.rename import apply_renames, format_plan_line, plan_renames


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="batch_files",
        description="Batch file management (rename with regex captures).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    rename = sub.add_parser("rename", help="Rename files matching a regex")
    rename.add_argument(
        "--root",
        required=True,
        type=Path,
        help="Root folder to walk (recursive)",
    )
    rename.add_argument(
        "--match",
        required=True,
        help="Regex matched with fullmatch against each filename",
    )
    rename.add_argument(
        "--replace",
        required=True,
        help=r"Replacement template (\1, \2, \g<name>)",
    )
    rename.add_argument(
        "--apply",
        action="store_true",
        help="Perform renames (default is dry-run)",
    )
    rename.add_argument(
        "--include-hidden",
        action="store_true",
        help="Include dotfiles / hidden path segments",
    )
    return parser


def cmd_rename(args: argparse.Namespace) -> int:
    try:
        pattern = re.compile(args.match)
    except re.error as exc:
        print(f"Invalid --match regex: {exc}", file=sys.stderr)
        return 2

    try:
        plans = plan_renames(
            args.root,
            pattern,
            args.replace,
            include_hidden=args.include_hidden,
        )
    except (NotADirectoryError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if not plans:
        print("No matching files.")
        return 0

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"{mode}: {len(plans)} match(es) under {args.root.expanduser().resolve()}")
    for plan in plans:
        print(format_plan_line(plan))

    apply_renames(plans, dry_run=not args.apply)
    if args.apply:
        renamed = sum(1 for p in plans if p.status == "rename")
        print(f"Renamed {renamed} file(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "rename":
        return cmd_rename(args)
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
