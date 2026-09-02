#!/usr/bin/env python3
"""Pull Bloomberg Desktop data and transactionally rebuild every shareable artifact."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.update_pipeline import UpdatePaths, run_bloomberg_update


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(PROJECT_ROOT / "config" / "security_roots.xlsx"),
        help="Security-root workbook containing the Bloomberg Update sheet.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Ignore the prior CSV and rebuild every available contract from the configured start.",
    )
    parser.add_argument(
        "--skip-repository-validator",
        action="store_true",
        help="Skip the second repository-level CSV validation pass (not recommended).",
    )
    args = parser.parse_args()

    paths = UpdatePaths(config=Path(args.config).expanduser().resolve())
    try:
        summary = run_bloomberg_update(
            paths=paths,
            full=args.full,
            run_repository_validator=not args.skip_repository_validator,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(summary["message"])
    print(f"mode={summary['mode']}")
    if summary.get("full_refresh_reason"):
        print(f"full_refresh_reason={summary['full_refresh_reason']}")
    print(f"manifest={summary['manifest']}")
    if summary.get("warnings"):
        print("Warnings:")
        for warning in summary["warnings"]:
            print(f"  - {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
