"""Fail on a dead relative link or anchor in tracked Markdown files.

Usage: python scripts/check_doc_links.py [file.md ...]

With no arguments it checks every Markdown file that git tracks. External
links (http, https, mailto) are not fetched. Links inside fenced code blocks
are ignored. An anchor must match a heading slug in the target file, using
GitHub's slug rule.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HTML_REF = re.compile(r"<(?:img|a)\s[^>]*(?:src|href)=\"([^\"]+)\"")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
EXTERNAL = ("http://", "https://", "mailto:", "data:")


def slug(heading: str) -> str:
    text = re.sub(r"`|\*\*|__", "", heading).strip().lower()
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def strip_fences(text: str) -> list[str]:
    lines, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            lines.append("")
            continue
        lines.append("" if fenced else line)
    return lines


def anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    out: set[str] = set()
    for line in strip_fences(path.read_text(encoding="utf-8")):
        match = HEADING.match(line)
        if not match:
            continue
        base = slug(match.group(1))
        count = seen.get(base, 0)
        seen[base] = count + 1
        out.add(base if count == 0 else f"{base}-{count}")
    return out


def check(path: Path) -> list[str]:
    errors = []
    for number, line in enumerate(strip_fences(path.read_text(encoding="utf-8")), 1):
        for target in LINK.findall(line) + HTML_REF.findall(line):
            if target.startswith(EXTERNAL):
                continue
            ref, _, fragment = target.partition("#")
            dest = (path.parent / ref).resolve() if ref else path
            where = f"{path.relative_to(ROOT)}:{number}: {target}"
            if not dest.exists():
                errors.append(f"{where} (missing file)")
            elif fragment and dest.suffix == ".md" and fragment not in anchors(dest):
                errors.append(f"{where} (missing anchor)")
    return errors


def tracked_markdown() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    return [ROOT / name for name in out if (ROOT / name).exists()]


def main(argv: list[str]) -> int:
    files = [Path(a).resolve() for a in argv] or tracked_markdown()
    errors = [e for f in files for e in check(f)]
    for error in errors:
        print(error)
    print(f"{len(files)} files checked, {len(errors)} dead links")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
