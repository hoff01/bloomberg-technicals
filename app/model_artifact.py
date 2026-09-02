"""Persist and validate the frozen strategy model used by score-only runs.

The training workflow remains responsible for all walk-forward and lockbox
evaluation.  A score-only run loads this compact artifact, applies the frozen
expert weights, and uses only the selected per-structure strategy evidence.  It
never reruns the backtest or updates weights from the current session.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

import polars as pl

from app.technical_backtest import BASE_EXPERT_IDS, strategy_library_frame
from app.technical_config import TechnicalConfig


MODEL_ARTIFACT_SCHEMA_VERSION = 1
WEIGHT_COLUMNS = tuple(
    f"adaptive_weight_{expert.lower()}" for expert in BASE_EXPERT_IDS
)


class ModelArtifactError(RuntimeError):
    """Raised when a frozen model artifact is missing or incompatible."""


def model_artifact_path(project_root: str | Path, mode: str) -> Path:
    normalized = str(mode).strip().lower()
    if normalized not in {"live", "demo"}:
        raise ValueError("mode must be 'live' or 'demo'")
    return Path(project_root).resolve() / "models" / normalized / "latest_model.json"


def model_contract_sha256(config: TechnicalConfig) -> str:
    """Hash every setting and preregistered strategy that controls scoring."""

    payload = asdict(config)
    payload["source_path"] = str(config.source_path.name)
    payload["strategy_library"] = strategy_library_frame().to_dicts()
    source_root = Path(__file__).resolve().parent
    payload["engine_sources"] = {
        name: hashlib.sha256((source_root / name).read_bytes()).hexdigest()
        for name in (
            "model_artifact.py",
            "technical_analytics.py",
            "technical_backtest.py",
            "technical_config.py",
        )
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _selected_strategy_rows(scorecard: pl.DataFrame) -> list[dict[str, Any]]:
    if scorecard.is_empty():
        return []
    required = {"spread_id", "strategy_id", "status"}
    missing = sorted(required - set(scorecard.columns))
    if missing:
        raise ModelArtifactError(
            "Scorecard cannot be frozen; missing columns: " + ", ".join(missing)
        )
    ranked = scorecard.with_columns(
        (pl.col("status") == "VALIDATED").cast(pl.Int8).alias("_validated_rank")
    ).sort(
        [
            "spread_id",
            "_validated_rank",
            "deflated_sharpe_probability",
            "profitable_fold_share",
            "daily_sharpe",
            "net_pnl_usd",
            "oos_trades",
            "strategy_id",
        ],
        descending=[False, True, True, True, True, True, True, False],
        nulls_last=True,
    )
    selected = ranked.group_by("spread_id", maintain_order=True).head(1).drop(
        "_validated_rank"
    )
    return selected.to_dicts()


def _frozen_expert_groups(
    features: pl.DataFrame,
    config: TechnicalConfig,
) -> tuple[list[dict[str, Any]], dict[str, str | int]]:
    required = {
        "session_date",
        "timestamp_utc",
        "learner_group",
        "adaptive_observations",
        "adaptive_top_expert",
        "adaptive_top_weight",
        *WEIGHT_COLUMNS,
    }
    missing = sorted(required - set(features.columns))
    if missing:
        raise ModelArtifactError(
            "Adaptive model cannot be frozen; missing feature columns: "
            + ", ".join(missing)
        )
    sessions = sorted(features["session_date"].unique().to_list())
    lockbox_sessions = int(config.backtest.lockbox_sessions)
    if len(sessions) <= lockbox_sessions:
        raise ModelArtifactError(
            f"At least {lockbox_sessions + 1} sessions are required to freeze a model"
        )
    lockbox_start = sessions[-lockbox_sessions]
    development_sessions = [item for item in sessions if item < lockbox_start]
    if not development_sessions:
        raise ModelArtifactError("No development sessions precede the lockbox")
    development_end = development_sessions[-1]
    development = features.filter(pl.col("session_date") <= development_end)
    latest = (
        development.sort(["timestamp_utc", "spread_id"])
        .group_by("learner_group", maintain_order=True)
        .tail(1)
        .select(
            "learner_group",
            "adaptive_observations",
            "adaptive_top_expert",
            "adaptive_top_weight",
            *WEIGHT_COLUMNS,
        )
        .sort("learner_group")
    )
    groups = latest.to_dicts()
    if not groups:
        raise ModelArtifactError("No adaptive learner groups were available to freeze")
    for row in groups:
        total = sum(float(row.get(column) or 0.0) for column in WEIGHT_COLUMNS)
        if abs(total - 1.0) > 1e-5:
            raise ModelArtifactError(
                f"Frozen weights for {row['learner_group']} sum to {total:.8f}"
            )
        if max(float(row.get(column) or 0.0) for column in WEIGHT_COLUMNS) > (
            config.backtest.adaptive_max_expert_weight + 1e-6
        ):
            raise ModelArtifactError(
                f"Frozen weights for {row['learner_group']} exceed the configured cap"
            )
    window = {
        "development_end": development_end.isoformat(),
        "lockbox_start": lockbox_start.isoformat(),
        "lockbox_end": sessions[-1].isoformat(),
        "lockbox_sessions": lockbox_sessions,
    }
    return groups, window


def build_model_artifact(
    *,
    config: TechnicalConfig,
    features: pl.DataFrame,
    scorecard: pl.DataFrame,
    mode: str,
    as_of: date,
) -> dict[str, Any]:
    normalized_mode = str(mode).strip().upper()
    if normalized_mode not in {"LIVE", "DEMO"}:
        raise ValueError("mode must be 'LIVE' or 'DEMO'")
    groups, training_window = _frozen_expert_groups(features, config)
    selected = _selected_strategy_rows(scorecard)
    contract_hash = model_contract_sha256(config)
    created = datetime.now(timezone.utc)
    model_id = (
        f"{normalized_mode.lower()}-{as_of:%Y%m%d}-"
        f"{created:%H%M%S}-{contract_hash[:10]}"
    )
    validated_count = sum(row.get("status") == "VALIDATED" for row in selected)
    return {
        "schema_version": MODEL_ARTIFACT_SCHEMA_VERSION,
        "model_id": model_id,
        "mode": normalized_mode,
        "created_at_utc": created.isoformat(),
        "as_of": as_of.isoformat(),
        "model_contract_sha256": contract_hash,
        "training_window": training_window,
        "total_trial_count": scorecard.height,
        "strategy_count": scorecard["strategy_id"].n_unique()
        if not scorecard.is_empty()
        else 0,
        "selected_strategy_count": len(selected),
        "validated_strategy_count": validated_count,
        "approved_for_live_signals": bool(
            normalized_mode == "LIVE" and validated_count > 0
        ),
        "selection_policy": (
            "One preregistered strategy per structure, ranked by validated status, "
            "deflated-Sharpe probability, fold consistency, OOS Sharpe, OOS net P&L, "
            "and OOS trade count. The final lockbox is evaluation-only."
        ),
        "selected_strategies": selected,
        "expert_groups": groups,
    }


def write_model_artifact(path: str | Path, artifact: Mapping[str, Any]) -> Path:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        temp_path.write_text(
            json.dumps(dict(artifact), indent=2, default=str), encoding="utf-8"
        )
        os.replace(temp_path, target)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return target


def load_model_artifact(
    path: str | Path,
    *,
    config: TechnicalConfig,
    expected_mode: str,
) -> dict[str, Any]:
    source = Path(path).resolve()
    if not source.is_file():
        raise ModelArtifactError(
            f"Frozen model artifact not found: {source}. Run TRAIN_AND_SCORE.bat first."
        )
    try:
        artifact = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelArtifactError(f"Could not read frozen model artifact {source}: {exc}") from exc
    if int(artifact.get("schema_version") or 0) != MODEL_ARTIFACT_SCHEMA_VERSION:
        raise ModelArtifactError(
            f"Unsupported model artifact schema in {source}; retrain the model"
        )
    normalized_mode = str(expected_mode).strip().upper()
    if artifact.get("mode") != normalized_mode:
        raise ModelArtifactError(
            f"Model artifact mode {artifact.get('mode')} cannot score {normalized_mode} data"
        )
    actual_hash = model_contract_sha256(config)
    if artifact.get("model_contract_sha256") != actual_hash:
        raise ModelArtifactError(
            "Model artifact does not match the current configuration or strategy "
            "library. Run TRAIN_AND_SCORE.bat to create a new frozen artifact."
        )
    if not artifact.get("expert_groups"):
        raise ModelArtifactError("Model artifact contains no frozen expert groups")
    return artifact


def scorecard_from_artifact(artifact: Mapping[str, Any]) -> pl.DataFrame:
    rows = artifact.get("selected_strategies") or []
    return pl.DataFrame(rows, strict=False) if rows else pl.DataFrame()


__all__ = [
    "MODEL_ARTIFACT_SCHEMA_VERSION",
    "ModelArtifactError",
    "WEIGHT_COLUMNS",
    "build_model_artifact",
    "load_model_artifact",
    "model_artifact_path",
    "model_contract_sha256",
    "scorecard_from_artifact",
    "write_model_artifact",
]
