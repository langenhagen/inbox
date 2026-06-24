#!/usr/bin/env python3
"""Prune seen.txt entries whose email files no longer exist on disk."""

import argparse
import glob as glob_module
import os
from pathlib import Path


def _file_exists_for_entry(mails_dir: Path, line: str) -> bool:
    """Return True if at least one email file matches this seen entry."""
    date, sender, _subject = line.split("|||", 2)
    prefix = f"{date}-{sender}--"
    pattern = f"{glob_module.escape(prefix)}*.html"
    return any(mails_dir.rglob(pattern))


def main() -> None:
    """Rewrite seen.txt to keep only entries whose email files still exist."""
    parser = argparse.ArgumentParser(
        description="Prune stale entries from seen.txt",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        default=os.environ.get("MAILS_DIR", "~/inbox"),
    )
    args = parser.parse_args()
    mails_dir = Path(args.output_dir).expanduser()
    seen_path = mails_dir / "seen.txt"

    entries = seen_path.read_text(encoding="utf-8").splitlines()
    surviving = [e for e in entries if _file_exists_for_entry(mails_dir, e)]
    if len(surviving) != len(entries):
        seen_path.write_text("\n".join(surviving) + "\n", encoding="utf-8")
        print(  # noqa: T201  # CLI output
            f"pruned {len(entries) - len(surviving)} entries from {seen_path}",
        )
    else:
        print(f"no stale entries in {seen_path}")  # noqa: T201  # CLI output


if __name__ == "__main__":
    main()
