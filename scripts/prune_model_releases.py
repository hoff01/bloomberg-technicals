#!/usr/bin/env python
"""Retain a bounded set of validated, versioned model dependency bundles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _referenced_release(model_file: Path) -> str | None:
    if not model_file.is_file():
        return None
    try:
        artifact = json.loads(model_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    references = dict(artifact.get("training_release_files") or {})
    for value in references.values():
        parts = Path(str(value)).parts
        if len(parts) >= 2 and parts[0] == "releases":
            return parts[1]
    return None


def prune(project_root: Path, mode: str, keep: int) -> list[Path]:
    root = project_root.resolve()
    mode_root = (root / "models" / mode).resolve()
    release_root = (mode_root / "releases").resolve()
    if not release_root.is_relative_to(root / "models"):
        raise ValueError("release root must remain under the project models directory")
    if not release_root.is_dir():
        return []
    referenced = {
        item
        for item in (
            _referenced_release(mode_root / "latest_model.json"),
            _referenced_release(mode_root / "last_known_good_model.json"),
        )
        if item
    }
    releases = sorted(
        [path for path in release_root.iterdir() if path.is_dir()],
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    retained = referenced | {path.name for path in releases[: max(1, keep)]}
    removed: list[Path] = []
    for path in releases:
        if path.name in retained:
            continue
        resolved = path.resolve()
        if resolved.parent != release_root:
            raise ValueError(f"refusing to prune unexpected path: {resolved}")
        shutil.rmtree(resolved)
        removed.append(resolved)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--mode", choices=("live", "demo"), required=True)
    parser.add_argument("--keep", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.keep <= 50:
        parser.error("--keep must be between 1 and 50")
    removed = prune(args.project_root, args.mode, args.keep)
    print(f"Model release retention: kept at least {args.keep}; removed {len(removed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
