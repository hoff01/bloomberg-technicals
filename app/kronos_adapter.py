"""Pure-Polars adapter for optional leg-level Kronos forecast diagnostics."""

from __future__ import annotations

from pathlib import Path
import os
import subprocess
import tomllib

import polars as pl

from app.technical_config import TechnicalConfig


KRONOS_COLUMNS = (
    "kronos_expected_move_1b",
    "kronos_vote",
    "kronos_contract_coverage",
    "kronos_status",
    "kronos_action_eligible",
)


def refresh_kronos_sidecar(
    project_root: str | Path,
    bars_path: str | Path,
) -> tuple[str, ...]:
    """Run the isolated optional sidecar when explicitly enabled."""

    root = Path(project_root).resolve()
    config_path = root / "config" / "kronos.toml"
    if not config_path.is_file():
        return ()
    settings = tomllib.loads(config_path.read_text(encoding="utf-8")).get(
        "kronos", {}
    )
    if not bool(settings.get("enabled")):
        return ()
    user_profile = os.environ.get("USERPROFILE")
    explicit_python = os.environ.get("BBG_TECHNICAL_KRONOS_PYTHON")
    if explicit_python:
        python = Path(explicit_python).expanduser()
    elif user_profile:
        python = (
            Path(user_profile)
            / "Pyenvs"
            / "bbg_technical_kronos"
            / "Scripts"
            / "python.exe"
        )
    else:
        return (
            "Kronos is enabled but USERPROFILE/BBG_TECHNICAL_KRONOS_PYTHON is unavailable; diagnostic skipped.",
        )
    if not python.is_file():
        return (
            f"Kronos is enabled but its isolated runtime is missing at {python}; run INSTALL_KRONOS_OPTIONAL.bat.",
        )
    local_app_data = os.environ.get("LOCALAPPDATA")
    cache_root = (
        Path(local_app_data) / "BloombergTechnicals" / "huggingface"
        if local_app_data
        else Path(user_profile or root) / ".cache" / "BloombergTechnicals" / "huggingface"
    )
    output = Path(bars_path).resolve().with_name("kronos_forecasts.parquet")
    command = [
        str(python),
        str(root / "scripts" / "kronos_sidecar.py"),
        "--bars",
        str(Path(bars_path).resolve()),
        "--output",
        str(output),
        "--cache-root",
        str(cache_root),
        "--config",
        str(config_path),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return (f"Optional Kronos diagnostic failed closed: {type(exc).__name__}: {exc}",)
    receipt = completed.stdout.strip().splitlines()
    return (
        f"Optional Kronos diagnostic refreshed: {receipt[-1] if receipt else output}",
    )


def _without_existing_kronos(features: pl.DataFrame) -> pl.DataFrame:
    existing = [column for column in KRONOS_COLUMNS if column in features.columns]
    return features.drop(existing) if existing else features


def attach_kronos_diagnostics(
    features: pl.DataFrame,
    forecast_path: str | Path,
    config: TechnicalConfig,
) -> pl.DataFrame:
    """Recombine real contract forecasts into current registered spread moves.

    The forecast remains diagnostic and non-actionable. Promotion requires a
    separate strictly forward evaluation history of at least 30 sessions.
    """

    base = _without_existing_kronos(features)
    if base.is_empty():
        return base
    source = Path(forecast_path)
    if not source.is_file():
        return base.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("kronos_expected_move_1b"),
            pl.lit(0, dtype=pl.Int8).alias("kronos_vote"),
            pl.lit(0.0).alias("kronos_contract_coverage"),
            pl.lit("DISABLED_OR_NOT_RUN").alias("kronos_status"),
            pl.lit(False).alias("kronos_action_eligible"),
        )
    forecasts = pl.read_parquet(source)
    required = {
        "security",
        "forecast_step",
        "forecast_origin_utc",
        "predicted_close_move_native",
        "action_enabled",
    }
    if forecasts.is_empty() or not required.issubset(forecasts.columns):
        return base.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("kronos_expected_move_1b"),
            pl.lit(0, dtype=pl.Int8).alias("kronos_vote"),
            pl.lit(0.0).alias("kronos_contract_coverage"),
            pl.lit("INVALID_FORECAST_ARTIFACT").alias("kronos_status"),
            pl.lit(False).alias("kronos_action_eligible"),
        )
    latest_forecasts = (
        forecasts.filter(pl.col("forecast_step") == 1)
        .sort("forecast_origin_utc")
        .group_by("security", maintain_order=True)
        .tail(1)
    )
    by_security = {
        str(row["security"]): row for row in latest_forecasts.to_dicts()
    }
    by_spread = {item.spread_id: item for item in config.spreads}
    latest_features = (
        base.sort("timestamp_utc").group_by("spread_id", maintain_order=True).tail(1)
    )
    rows: list[dict[str, object]] = []
    for row in latest_features.to_dicts():
        spread_id = str(row["spread_id"])
        spread = by_spread.get(spread_id)
        moves: list[float] = []
        expected = 0.0
        matched = 0
        if spread is not None:
            for index, leg in enumerate(spread.legs, start=1):
                security = str(row.get(f"leg{index}_security") or "")
                forecast = by_security.get(security)
                if not forecast:
                    continue
                if forecast.get("forecast_origin_utc") != row.get("timestamp_utc"):
                    continue
                native_move = float(forecast["predicted_close_move_native"])
                expected += (
                    leg.sign
                    * leg.price_weight
                    * config.roots[leg.root].price_to_usd_bbl
                    * native_move
                )
                moves.append(native_move)
                matched += 1
        required_legs = len(spread.legs) if spread is not None else 0
        coverage = matched / required_legs if required_legs else 0.0
        complete = bool(required_legs and matched == required_legs)
        volatility = float(row.get("ewma_abs_change") or 0.0)
        threshold = max(1e-9, 0.5 * volatility)
        vote = 1 if complete and expected >= threshold else -1 if complete and expected <= -threshold else 0
        rows.append(
            {
                "spread_id": spread_id,
                "timestamp_utc": row["timestamp_utc"],
                "kronos_expected_move_1b": expected if complete else None,
                "kronos_vote": vote,
                "kronos_contract_coverage": coverage,
                "kronos_status": (
                    "EXPERIMENTAL_30_SESSION_EVALUATION_REQUIRED"
                    if complete
                    else "INCOMPLETE_LEG_FORECASTS"
                ),
                # Fail closed even if an upstream artifact is accidentally
                # labelled actionable; promotion is owned by this system.
                "kronos_action_eligible": False,
            }
        )
    diagnostics = pl.DataFrame(rows, strict=False)
    return (
        base.join(diagnostics, on=["spread_id", "timestamp_utc"], how="left")
        .with_columns(
            pl.col("kronos_vote").fill_null(0).cast(pl.Int8),
            pl.col("kronos_contract_coverage").fill_null(0.0),
            pl.col("kronos_status").fill_null("HISTORICAL_NOT_SCORED"),
            pl.col("kronos_action_eligible").fill_null(False),
        )
        .sort(["spread_id", "timestamp_utc"])
    )


__all__ = [
    "KRONOS_COLUMNS",
    "attach_kronos_diagnostics",
    "refresh_kronos_sidecar",
]
