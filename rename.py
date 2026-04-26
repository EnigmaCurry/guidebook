#!/usr/bin/env python3
"""Rename this project from 'guidebook' to a new name.

Usage:
    python rename.py <new_name>

This script replaces all references to 'guidebook' (and its capitalized
variants) with the new name, renames files and directories, rebuilds the
lock file and frontend, commits the result, then deletes itself.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Files and directories to perform text replacement in (relative to ROOT).
# Directories are walked recursively. Order doesn't matter.
REPLACE_PATHS = [
    "pyproject.toml",
    "build_entry.py",
    "Dockerfile",
    "Justfile",
    "guidebook.spec",
    ".github/workflows/release.yaml",
    ".gitignore",
    ".dockerignore",
    "README.md",
    "CLAUDE.md",
    "CHANGELOG.md",
    "TERMUX.md",
    "LICENSE.txt",
    "export-session.py",
    "frontend/index.html",
    "frontend/package.json",
    "frontend/vite.config.js",
    "frontend/src/",
    "src/guidebook/",
    ".claude/skills/",
]

# Skip these when walking directories
SKIP_DIRS = {".git", "node_modules", "__pycache__", "static", ".venv", "dist", "build"}
SKIP_EXTENSIONS = {".lock", ".pyc", ".ico", ".png", ".jpg", ".jpeg", ".gif", ".woff", ".woff2", ".ttf", ".eot"}


def validate_name(name: str) -> str | None:
    if not re.match(r"^[a-z][a-z0-9_]*$", name):
        return "Name must start with a letter and contain only lowercase letters, digits, and underscores (valid Python package name)"
    if name.startswith("__"):
        return "Name must not start with '__'"
    if name == "guidebook":
        return "That's already the current name"
    return None


def replace_in_file(path: Path, old_lower: str, new_lower: str) -> bool:
    """Replace all case variants of old name with corresponding new name."""
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False

    old_upper = old_lower.upper()
    old_title = old_lower.capitalize()
    new_upper = new_lower.upper()
    new_title = new_lower.capitalize()

    # Build short title (first two letters uppercase) for the nav bar abbreviation
    old_short = "GB"
    new_short = new_lower[:2].upper() if len(new_lower) >= 2 else new_lower.upper()

    original = text
    # Order matters: do UPPER first, then Title, then lower to avoid partial matches
    text = text.replace(old_upper, new_upper)
    text = text.replace(old_title, new_title)
    text = text.replace(old_lower, new_lower)
    # Replace the short title abbreviation in App.svelte
    if path.name == "App.svelte":
        text = text.replace(
            f'<span class="title-short">{old_short}</span>',
            f'<span class="title-short">{new_short}</span>',
        )

    if text != original:
        path.write_text(text, encoding="utf-8")
        return True
    return False


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    kwargs.setdefault("cwd", str(ROOT))
    return subprocess.run(cmd, check=True, **kwargs)


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <new_name>")
        print("  new_name: lowercase Python package name (e.g. myapp, field_notes)")
        sys.exit(1)

    new_name = sys.argv[1].strip().lower()
    error = validate_name(new_name)
    if error:
        print(f"Error: {error}")
        sys.exit(1)

    old_name = "guidebook"
    old_title = "Guidebook"
    new_title = new_name.capitalize()

    print(f"Renaming {old_title} → {new_title}")
    print()

    # 1. Collect files (expand directories recursively)
    files: list[Path] = []
    for rel in REPLACE_PATHS:
        path = ROOT / rel
        if path.is_dir():
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for fname in filenames:
                    fpath = Path(dirpath) / fname
                    if fpath.suffix in SKIP_EXTENSIONS:
                        continue
                    files.append(fpath)
        elif path.is_file():
            files.append(path)

    # 2. Text replacements
    print("Replacing text references...")
    changed = 0
    for path in files:
        rel_display = str(path.relative_to(ROOT))
        if replace_in_file(path, old_name, new_name):
            changed += 1
            print(f"  {rel_display}")
    print(f"  {changed} files updated")
    print()

    # 3. Rename files
    print("Renaming files...")
    renames = [
        (f"{old_name}.spec", f"{new_name}.spec"),
        (f"{old_name}.png", f"{new_name}.png"),
    ]
    for old_rel, new_rel in renames:
        old_path = ROOT / old_rel
        new_path = ROOT / new_rel
        if old_path.exists():
            old_path.rename(new_path)
            print(f"  {old_rel} → {new_rel}")

    # 4. Rename source directory
    old_src = ROOT / "src" / old_name
    new_src = ROOT / "src" / new_name
    if old_src.exists():
        old_src.rename(new_src)
        print(f"  src/{old_name}/ → src/{new_name}/")
    print()

    # 5. Rebuild uv.lock
    print("Rebuilding uv.lock...")
    run(["uv", "lock"])
    print()

    # 6. Rebuild frontend
    print("Rebuilding frontend...")
    run(["npm", "install"], cwd=str(ROOT / "frontend"))
    run(["npm", "run", "build"], cwd=str(ROOT / "frontend"))
    print()

    # 7. Commit
    print("Committing...")
    run(["git", "add", "-A"])
    run(["git", "commit", "-m", f"Rename guidebook to {new_name}"])
    print()

    # 8. Self-destruct
    script_path = Path(__file__).resolve()
    print(f"Removing {script_path.name}...")
    run(["git", "rm", str(script_path.relative_to(ROOT))])
    run(["git", "commit", "-m", f"Remove rename script"])
    print()

    print(f"Done! Project is now '{new_title}'.")
    print(f"  Package: {new_name}")
    print(f"  Command: uv run {new_name}")
    print(f"  Data dir: ~/.local/{new_name}/")
    print(f"  Env vars: {new_name.upper()}_HOST, {new_name.upper()}_PORT, etc.")


if __name__ == "__main__":
    main()
