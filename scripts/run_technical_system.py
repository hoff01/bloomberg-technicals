#!/usr/bin/env python
"""Run the complete Bloomberg distillate technical system."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys
import webbrowser

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.technical_pipeline import run_technical_pipeline  # noqa: E402
from app.model_artifact import ModelArtifactError  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--mode", choices=("live", "demo"), default="live")
    parser.add_argument(
        "--workflow",
        choices=("train", "score"),
        default="train",
        help="Train/evaluate and persist a model, or score with the frozen model.",
    )
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument(
        "--backfill",
        type=Path,
        help="Optional licensed CSV/CSV.GZ/Parquet intraday backfill.",
    )
    parser.add_argument(
        "--skip-pull",
        action="store_true",
        help="Recalculate from the stored live snapshot without contacting Bloomberg.",
    )
    parser.add_argument(
        "--reuse-demo",
        action="store_true",
        help="Use the stored demo data instead of regenerating it.",
    )
    parser.add_argument(
        "--model-artifact",
        type=Path,
        help="Optional frozen-model JSON path; defaults under models/<mode>/.",
    )
    parser.add_argument("--no-open", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = run_technical_pipeline(
            args.project_root,
            mode=args.mode,
            as_of=args.as_of,
            backfill=args.backfill,
            skip_pull=args.skip_pull,
            regenerate_demo=not args.reuse_demo,
            workflow=args.workflow,
            model_artifact=args.model_artifact,
        )
    except ModelArtifactError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    manifest = result["manifest"]
    dashboard = result["paths"]["dashboard"]
    signals = result["signals"]
    print()
    print(f"Mode: {manifest['mode']}")
    if manifest.get("workflow") == "SCORE_CURRENT":
        print(
            f"Source coverage: {manifest['source_coverage_start']} through "
            f"{manifest['source_coverage_end']} "
            f"({manifest['source_sessions']} sessions)"
        )
        print(
            f"Scored {manifest['spreads']} spreads from the frozen model "
            f"{manifest['model_id']} using {manifest['scoring_history_sessions']} "
            "recent sessions; no backtest or weight update ran."
        )
    else:
        print(
            f"Coverage: {manifest['coverage_start']} through {manifest['coverage_end']} "
            f"({manifest['sessions']} sessions)"
        )
        print(
            f"Built {manifest['spreads']} spreads and {manifest['backtest_trades']:,} "
            f"auditable backtest trades."
        )
        print(f"Frozen model: {result['model_artifact']}")
    if not signals.is_empty():
        print("Current decision board:")
        print(
            signals.select(
                "spread_id",
                "status",
                "current_spread",
                "buy_entry_ceiling",
                "sell_entry_floor",
                "confidence",
                "relative_volume",
                "depth_source",
            )
        )
    print(f"Dashboard: {dashboard}")
    if not args.no_open:
        webbrowser.open(dashboard.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
