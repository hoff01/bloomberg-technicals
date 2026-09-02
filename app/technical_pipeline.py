"""End-to-end technical data, spread, backtest, signal, and reporting pipeline."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
import os
from pathlib import Path
import shutil
import tempfile
from time import perf_counter
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import polars as pl

from app.kronos_adapter import attach_kronos_diagnostics, refresh_kronos_sidecar
from app.technical_analytics import (
    add_indicators,
    build_seasonality_table,
    build_spread_bars,
    daily_to_prepared_bars,
    indicator_library_audit,
    latest_leg_tickers,
    prepare_trade_bars,
)
from app.technical_backtest import (
    add_adaptive_ensemble,
    apply_frozen_expert_model,
    build_live_signal_board,
    run_backtests,
)
from app.technical_config import (
    ContractDefinition,
    TechnicalConfig,
    add_months,
    expected_latest_exchange_session,
    load_technical_config,
    schema_summary,
)
from app.technical_data import (
    DataPaths,
    PullStats,
    TechnicalDataError,
    TechnicalStore,
    XbbgTechnicalClient,
    data_quality_report,
    frame_sha256,
    import_intraday_backfill,
    normalize_contract_registry,
    normalize_daily_frame,
    package_depth_snapshot,
    write_manifest,
)
from app.model_artifact import (
    build_model_artifact,
    load_model_artifact,
    model_artifact_path,
    scorecard_from_artifact,
    write_model_artifact,
)
from app.technical_reporting import (
    portfolio_lockbox_trade_budget,
    write_technical_outputs,
    write_technical_score_outputs,
)
from app.technical_summary import build_model_summary, build_structure_summaries


TRAIN_SCORE_PARITY_FIELDS = (
    "current_spread",
    "buy_entry_ceiling",
    "sell_entry_floor",
    "fair_value_target",
    "long_stop",
    "short_stop",
    "confidence",
    "pattern_strength",
    "pattern_agreement",
    "relative_volume",
    "pvo",
    "tod_normalized_change",
    "vol_regime_ratio_1d_20d",
    "liquidity_stress_ratio",
    "tail_event_rate_20d",
    "robust_volume_surprise",
    "return_skew_5d",
    "return_excess_kurtosis_5d",
    "realized_vol_of_vol_5d",
    "trend_hac_t_stat_3d",
    "close_path_choppiness_5d",
    "robust_z",
    "rsi",
    "macd_histogram",
    "seasonal_expected_move_1d",
)


def _atomic_copy(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        shutil.copyfile(source, temp_path)
        os.replace(temp_path, target)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return target


def _live_training_session_issue(
    bars: pl.DataFrame, config: TechnicalConfig
) -> str | None:
    """Reject a live model candidate whose final session is still incomplete."""

    trades = bars.filter(pl.col("event_type") == "TRADE")
    if trades.is_empty():
        return "no TRADE bars are available"
    latest_session = trades["session_date"].max()
    latest_pull = trades["pulled_at_utc"].max()
    if latest_pull is None:
        return "TRADE bars have no pull timestamp"
    pull_local = latest_pull.astimezone(ZoneInfo(config.system.timezone))
    close_ready = datetime.combine(
        pull_local.date(),
        config.system.session_end,
        tzinfo=pull_local.tzinfo,
    ) + timedelta(minutes=config.bloomberg.freshness_grace_minutes)
    if latest_session == pull_local.date() and pull_local < close_ready:
        return (
            f"session {latest_session} is still open at {pull_local:%H:%M:%S}; "
            f"use SCORE_CURRENT.bat intraday and train after {close_ready:%H:%M} New York"
        )
    return None


def _merge_contract_registries(old: pl.DataFrame, new: pl.DataFrame) -> pl.DataFrame:
    frames = [item for item in (old, new) if not item.is_empty()]
    if not frames:
        return pl.DataFrame()
    merged = pl.concat(frames, how="diagonal_relaxed").with_columns(
        pl.col("expiry_verified").fill_null(False).cast(pl.Int8).alias("_verified_priority")
    )
    return (
        merged.sort(
            ["root", "security", "delivery_month", "_verified_priority"],
        )
        .unique(["root", "security", "delivery_month"], keep="last")
        .drop("_verified_priority")
        .sort(["root", "delivery_month"])
    )


def _definitions_for_intraday(
    definitions: tuple[ContractDefinition, ...],
    start: date,
    end: date,
    forward_curve_months: int,
) -> tuple[ContractDefinition, ...]:
    terminal_delivery = add_months(
        date(end.year, end.month, 1), forward_curve_months + 2
    )
    return tuple(
        item
        for item in definitions
        if item.fallback_expiry >= start - timedelta(days=10)
        and item.delivery_month <= terminal_delivery
    )


async def _live_update(
    project_root: Path,
    as_of: date,
    *,
    backfill: Path | None,
    skip_pull: bool,
) -> tuple[TechnicalStore, str, PullStats, tuple[str, ...]]:
    config = load_technical_config(project_root / "config" / "technical_system.toml")
    paths = DataPaths.under(project_root, dataset="live")
    store = TechnicalStore(paths, config)
    definitions = config.build_contract_universe(
        config.system.daily_history_start,
        as_of,
        history_buffer_months=7,
        forward_months=config.system.forward_curve_months + 2,
    )
    by_ticker = {item.ticker: item for item in definitions}
    warnings: list[str] = []
    pull_stats = PullStats()

    if backfill is not None:
        imported = import_intraday_backfill(backfill, by_ticker, config)
        store.update_bars(imported, as_of)
        warnings.append(f"Imported {imported.height:,} canonical backfill bars from {backfill.name}.")

    if skip_pull:
        if store.load_bars().is_empty():
            raise TechnicalDataError("--skip-pull requested but the live store has no intraday bars")
        return store, "STORED_SNAPSHOT", pull_stats, tuple(warnings)

    client = XbbgTechnicalClient(config)
    existing_registry = store.load_contracts()
    verified_tickers = (
        set(
            str(item)
            for item in existing_registry.filter(pl.col("expiry_verified"))["security"].to_list()
        )
        if not existing_registry.is_empty()
        else set()
    )
    reference_definitions = tuple(
        item for item in definitions if item.ticker not in verified_tickers
    )
    if reference_definitions:
        raw_reference = await client.fetch_reference(reference_definitions)
        new_registry = normalize_contract_registry(
            reference_definitions, raw_reference
        )
    else:
        new_registry = pl.DataFrame()
    registry = _merge_contract_registries(existing_registry, new_registry)
    store.write_contracts(registry)

    existing_daily = store.load_daily()
    if existing_daily.is_empty():
        daily_start = config.system.daily_history_start
    else:
        daily_start = max(
            config.system.daily_history_start,
            existing_daily["session_date"].max() - timedelta(days=config.system.pull_overlap_days),
        )
    daily_definitions = tuple(
        item
        for item in definitions
        if item.fallback_expiry >= daily_start - timedelta(days=45)
    )
    raw_daily = await client.fetch_daily(daily_definitions, daily_start, as_of)
    incoming_daily = normalize_daily_frame(raw_daily, by_ticker)
    daily_merged = store.update_daily(incoming_daily)

    existing_bars = store.load_bars()
    retention_start = as_of - timedelta(
        days=min(139, config.bloomberg.intraday_retention_warning_days)
    )
    if existing_bars.is_empty():
        intraday_start = retention_start
    else:
        intraday_start = max(
            retention_start,
            existing_bars["session_date"].max() - timedelta(days=config.system.pull_overlap_days),
        )
    active_definitions = _definitions_for_intraday(
        definitions,
        intraday_start,
        as_of,
        config.system.forward_curve_months,
    )
    event_types = [config.bloomberg.event_type]
    if config.bloomberg.pull_bid_ask_bars:
        event_types.extend(["BID", "ASK"])
    incoming_bars, pull_stats = await client.fetch_intraday(
        active_definitions,
        intraday_start,
        as_of,
        event_types=event_types,
    )
    warnings.extend(pull_stats.warnings)
    trade_failure_rate = (
        pull_stats.trade_failed / pull_stats.trade_requested
        if pull_stats.trade_requested
        else 1.0
    )
    if trade_failure_rate > config.bloomberg.maximum_trade_failure_rate:
        raise TechnicalDataError(
            "Bloomberg TRADE pull failure rate exceeded the configured limit: "
            f"{pull_stats.trade_failed}/{pull_stats.trade_requested} "
            f"({100.0 * trade_failure_rate:.1f}%). Prior stored bars were preserved."
        )
    incoming_trades = incoming_bars.filter(
        pl.col("event_type") == config.bloomberg.event_type
    )
    if incoming_trades.is_empty():
        raise TechnicalDataError(
            "Bloomberg returned no usable TRADE bars. Prior stored bars were preserved."
        )
    observed_at = datetime.now(ZoneInfo(config.system.timezone))
    latest_by_root = {
        str(row["root"]): row
        for row in incoming_trades.group_by("root")
        .agg(
            pl.col("session_date").max().alias("latest_session"),
            pl.col("bar_end_et").max().alias("latest_bar_end"),
        )
        .to_dicts()
    }
    stale_roots: list[str] = []
    for root in config.roots:
        expected_session = expected_latest_exchange_session(
            as_of,
            observed_at,
            root,
            session_start=config.system.session_start,
            bar_interval_minutes=config.system.bar_interval_minutes,
            grace_minutes=config.bloomberg.freshness_grace_minutes,
        )
        latest_row = latest_by_root.get(root)
        actual_session = latest_row.get("latest_session") if latest_row else None
        if actual_session is None or actual_session < expected_session:
            stale_roots.append(
                f"{root} actual={actual_session or 'missing'} expected>={expected_session}"
            )
            continue
        latest_bar_end = latest_row.get("latest_bar_end") if latest_row else None
        if latest_bar_end is None:
            stale_roots.append(f"{root} has no completed bar timestamp")
        elif expected_session < observed_at.date():
            latest_end_local = latest_bar_end.astimezone(observed_at.tzinfo)
            if (
                latest_end_local.date() != expected_session
                or latest_end_local.time() < config.system.session_end
            ):
                stale_roots.append(
                    f"{root} prior session close bar is missing"
                )
        elif expected_session == observed_at.date():
            close_ready = datetime.combine(
                observed_at.date(),
                config.system.session_end,
                tzinfo=observed_at.tzinfo,
            ) + timedelta(minutes=config.bloomberg.freshness_grace_minutes)
            if observed_at < close_ready:
                lag_minutes = (
                    observed_at - latest_bar_end.astimezone(observed_at.tzinfo)
                ).total_seconds() / 60.0
                maximum_lag = (
                    config.system.bar_interval_minutes
                    + config.bloomberg.freshness_grace_minutes
                )
                if lag_minutes < 0 or lag_minutes > maximum_lag:
                    stale_roots.append(
                        f"{root} latest bar lag={lag_minutes:.1f}m max={maximum_lag}m"
                    )
            elif latest_bar_end.astimezone(observed_at.tzinfo).time() < config.system.session_end:
                stale_roots.append(
                    f"{root} latest bar ended before {config.system.session_end:%H:%M}"
                )
    if stale_roots:
        raise TechnicalDataError(
            "Bloomberg TRADE freshness failed by root: "
            + "; ".join(stale_roots)
            + ". Prior stored bars were preserved."
        )
    bars = store.update_bars(incoming_bars, as_of)

    # Resolve the currently executable legs before starting a short real-time
    # depth capture.  Historical BPIPE L2 is not implied by this snapshot.
    latest_trade_bars = bars.filter(
        (pl.col("event_type") == config.bloomberg.event_type)
        & (pl.col("session_date") == bars.filter(
            pl.col("event_type") == config.bloomberg.event_type
        )["session_date"].max())
    )
    prepared = prepare_trade_bars(
        latest_trade_bars, registry, daily_merged, config
    )
    preliminary_spreads = build_spread_bars(prepared, config)
    tickers = latest_leg_tickers(preliminary_spreads)
    liquidity, depth_source, depth_warnings = await client.capture_liquidity(tickers)
    warnings.extend(depth_warnings)
    if not liquidity.is_empty():
        store.write_liquidity(liquidity, l2=depth_source == "BPIPE_L2")
    return store, depth_source, pull_stats, tuple(warnings)


def _analyze_and_report(
    project_root: Path,
    *,
    store: TechnicalStore,
    as_of: date,
    depth_source: str,
    pull_stats: PullStats,
    warnings: tuple[str, ...],
    demo_mode: bool,
    artifact_target: Path,
) -> dict[str, Any]:
    pipeline_started = perf_counter()
    stage_started = pipeline_started
    stage_seconds: dict[str, float] = {}
    config = store.config
    bars = store.load_bars()
    daily = store.load_daily()
    contracts = store.load_contracts()
    if not demo_mode:
        live_training_issue = _live_training_session_issue(bars, config)
        if live_training_issue:
            raise TechnicalDataError(
                "Live training requires a fully closed final session: "
                + live_training_issue
                + ". The previous frozen model was not replaced."
            )
    stage_seconds["load_data"] = perf_counter() - stage_started
    stage_started = perf_counter()
    quality = data_quality_report(
        bars, contracts, config, daily=daily, depth_source=depth_source
    )
    prepared = prepare_trade_bars(bars, contracts, daily, config)
    spreads = build_spread_bars(prepared, config)
    stage_seconds["intraday_spread_build"] = perf_counter() - stage_started
    stage_started = perf_counter()
    if spreads.is_empty():
        raise TechnicalDataError(
            "No executable spread bars were built. Verify exact delivery-month "
            "coverage and Bloomberg expiry metadata."
        )
    if not demo_mode:
        latest_spread_session = spreads["session_date"].max()
        core_ids = {
            item.spread_id
            for item in config.spreads
            if item.core and item.model_enabled
        }
        latest_core_counts = {
            str(row["spread_id"]): int(row["bars"])
            for row in spreads.filter(
                (pl.col("session_date") == latest_spread_session)
                & pl.col("spread_id").is_in(core_ids)
            )
            .group_by("spread_id")
            .agg(pl.len().alias("bars"))
            .to_dicts()
        }
        bad_core = sorted(
            spread_id
            for spread_id in core_ids
            if latest_core_counts.get(spread_id)
            != config.system.complete_bars_per_session
        )
        if bad_core:
            raise TechnicalDataError(
                "Live training requires one common closed session across NYMEX "
                "and ICE core packages. The latest date is incomplete for: "
                + ", ".join(bad_core)
                + ". Use SCORE_CURRENT.bat on root-specific holidays and retrain "
                "after the next common market session. The previous frozen model "
                "was not replaced."
            )
    minimum_training_sessions = (
        config.backtest.train_sessions
        + config.backtest.validation_sessions
        + config.backtest.embargo_sessions
        + config.backtest.test_sessions
        + config.backtest.lockbox_sessions
    )
    observed_sessions = spreads["session_date"].n_unique()
    if not demo_mode and observed_sessions < minimum_training_sessions:
        raise TechnicalDataError(
            "Live model training requires at least "
            f"{minimum_training_sessions} complete sessions for the first "
            "train/validation/OOS/lockbox sequence, but only "
            f"{observed_sessions} are stored. Standard Desktop intraday retention "
            "is insufficient for a fresh model; rerun TRAIN_AND_SCORE.bat with a "
            f"licensed {config.system.rolling_intraday_months}-month --Backfill "
            "CSV or Parquet archive. The previous "
            "frozen model was not replaced."
        )
    daily_prepared = daily_to_prepared_bars(daily, contracts, config)
    daily_spreads = build_spread_bars(daily_prepared, config)
    intraday_years = set(
        int(item)
        for item in spreads.select(pl.col("session_date").dt.year().unique())
        .to_series()
        .to_list()
    )
    seasonality = build_seasonality_table(
        daily_spreads, config, target_years=intraday_years
    )
    stage_seconds["daily_seasonality"] = perf_counter() - stage_started
    stage_started = perf_counter()
    features = add_indicators(spreads, config, seasonality)
    stage_seconds["indicators"] = perf_counter() - stage_started
    stage_started = perf_counter()
    features = add_adaptive_ensemble(features, config)
    features = attach_kronos_diagnostics(
        features, store.paths.bars.with_name("kronos_forecasts.parquet"), config
    )
    stage_seconds["adaptive_training"] = perf_counter() - stage_started
    stage_started = perf_counter()
    indicator_audit = indicator_library_audit(features, config)
    stage_seconds["indicator_audit"] = perf_counter() - stage_started
    stage_started = perf_counter()
    backtests = run_backtests(features, config)
    stage_seconds["backtests"] = perf_counter() - stage_started
    stage_started = perf_counter()
    if demo_mode and not backtests.scorecard.is_empty():
        backtests = replace(
            backtests,
            scorecard=backtests.scorecard.with_columns(
                pl.lit("DEMO_ONLY").alias("status")
            ),
        )
    artifact = build_model_artifact(
        config=config,
        features=features,
        scorecard=backtests.scorecard,
        mode="DEMO" if demo_mode else "LIVE",
        as_of=as_of,
    )
    quality_blocking = (
        quality.filter(pl.col("blocking") & (pl.col("status") == "FAIL")).height > 0
    )
    liquidity_snapshot = store.load_liquidity(
        l2=depth_source in {"BPIPE_L2", "DEMO_L2"}
    )
    depth_metrics = package_depth_snapshot(
        features,
        liquidity_snapshot,
        config,
        depth_source=depth_source,
    )
    live_signals = build_live_signal_board(
        features,
        backtests.scorecard,
        config,
        depth_source=depth_source,
        depth_metrics=depth_metrics,
        quality_blocking=quality_blocking,
        demo_mode=demo_mode,
    )
    selected_scorecard = scorecard_from_artifact(artifact)
    lockbox_start = date.fromisoformat(artifact["training_window"]["lockbox_start"])
    lockbox_end = date.fromisoformat(artifact["training_window"]["lockbox_end"])
    structure_summaries, recent_selected_trades = build_structure_summaries(
        live_signals,
        selected_scorecard,
        backtests.trades,
        lockbox_start=lockbox_start,
        lockbox_end=lockbox_end,
        demo_mode=demo_mode,
    )
    portfolio_lockbox_trades = portfolio_lockbox_trade_budget(
        backtests.trades, structure_summaries, config
    )
    artifact_summary_context = dict(artifact)
    artifact_summary_context["model_age_sessions"] = 0
    artifact_summary_context["maximum_model_age_sessions"] = (
        config.system.maximum_model_age_sessions
    )
    artifact_summary_context["model_stale"] = False
    model_summary = build_model_summary(
        structure_summaries,
        recent_selected_trades,
        selected_scorecard,
        artifact_summary_context,
        demo_mode=demo_mode,
        portfolio_lockbox_trades=portfolio_lockbox_trades,
    )
    artifact["model_summary"] = model_summary
    stage_seconds["model_freeze_and_signals"] = perf_counter() - stage_started
    stage_started = perf_counter()
    source_hashes = {
        "bars_sha256": frame_sha256(store.paths.bars),
        "daily_sha256": frame_sha256(store.paths.daily),
        "contracts_sha256": frame_sha256(store.paths.contracts),
    }
    artifact["training_source_hashes"] = source_hashes
    artifact["training_signal_snapshot"] = live_signals.select(
        "spread_id",
        "status",
        "signal_strategy_id",
        *TRAIN_SCORE_PARITY_FIELDS,
    ).to_dicts()
    artifact_path = artifact_target.resolve()
    trade = bars.filter(pl.col("event_type") == "TRADE")
    source_values = sorted(str(item) for item in trade["source"].unique().to_list())
    manifest: dict[str, Any] = {
        "mode": "DEMO" if demo_mode else "LIVE",
        "workflow": "TRAIN_AND_SCORE",
        "as_of": as_of.isoformat(),
        "model_artifact": str(artifact_path),
        "model_id": artifact["model_id"],
        "model_training_window": artifact["training_window"],
        "selected_strategy_count": artifact["selected_strategy_count"],
        "validated_strategy_count": artifact["validated_strategy_count"],
        "latest_30_sessions": model_summary["latest_30_sessions"],
        "stage_seconds": stage_seconds,
        "schema": schema_summary(config),
        "data_sources": source_values,
        "depth_source": depth_source,
        "historical_depth_note": (
            "Only the current capture may be true BPIPE L2. Historical strategy tests "
            "use aligned leg volume, event count, open interest, and bid/ask proxies."
        ),
        "bars": bars.height,
        "trade_bars": trade.height,
        "sessions": trade["session_date"].n_unique(),
        "coverage_start": str(trade["session_date"].min()),
        "coverage_end": str(trade["session_date"].max()),
        "daily_rows": daily.height,
        "daily_start": str(daily["session_date"].min()) if not daily.is_empty() else None,
        "feature_rows": features.height,
        "spreads": features["spread_id"].n_unique(),
        "backtest_trades": backtests.trades.height,
        "validated_strategy_spreads": (
            backtests.scorecard.filter(pl.col("status") == "VALIDATED").height
            if not backtests.scorecard.is_empty()
            else 0
        ),
        "expiry_rule": (
            "Earliest leg controls; mandatory liquidation at the final eligible "
            "14:15 open on D-4; "
            "flat before D-3; no entries on the forced-exit session."
        ),
        "pull_stats": asdict(pull_stats),
        "warnings": list(warnings),
        "xbbg_retention_note": (
            "Standard Bloomberg DAPI intraday history may be limited to about 140 days. "
            f"The rolling Parquet store accumulates to {config.system.rolling_intraday_months} "
            "months; import a licensed "
            "CSV/Parquet backfill when required."
        ),
        "files": source_hashes,
    }
    output_paths = write_technical_outputs(
        project_root,
        config=config,
        features=features,
        live_signals=live_signals,
        backtests=backtests,
        quality=quality,
        contracts=contracts,
        indicator_audit=indicator_audit,
        seasonality_profiles=seasonality,
        daily_settle_spreads=daily_spreads,
        structure_summaries=structure_summaries,
        model_summary=model_summary,
        manifest=manifest,
        demo_mode=demo_mode,
        as_of=as_of,
    )
    release_relative = Path("releases") / str(artifact["model_id"])
    release_targets = {
        "seasonality": artifact_path.parent
        / release_relative
        / "technical_seasonality_profiles.csv",
        "backtest_trades": artifact_path.parent
        / release_relative
        / "technical_backtest_trades.csv",
    }
    _atomic_copy(output_paths["seasonality"], release_targets["seasonality"])
    _atomic_copy(output_paths["trades"], release_targets["backtest_trades"])
    artifact["training_release_files"] = {
        key: str(path.relative_to(artifact_path.parent))
        for key, path in release_targets.items()
    }
    artifact["training_release_hashes"] = {
        "seasonality_sha256": frame_sha256(release_targets["seasonality"]),
        "backtest_trades_sha256": frame_sha256(
            release_targets["backtest_trades"]
        ),
    }
    manifest["training_release_files"] = artifact["training_release_files"]
    manifest["training_release_hashes"] = artifact["training_release_hashes"]
    manifest["stage_seconds"]["reporting"] = perf_counter() - stage_started
    manifest["stage_seconds"]["total_analysis"] = perf_counter() - pipeline_started
    write_manifest(store.paths.run_manifest, manifest)
    # Promote the candidate only after calculations, reports, and the manifest
    # have all completed. Windows additionally restores the prior artifact if
    # the workbook or independent release validator fails afterward.
    artifact_path = write_model_artifact(artifact_path, artifact)
    return {
        "manifest": manifest,
        "paths": output_paths,
        "signals": live_signals,
        "quality": quality,
        "backtests": backtests,
        "model_artifact": artifact_path,
    }


def _score_and_report(
    project_root: Path,
    *,
    store: TechnicalStore,
    as_of: date,
    depth_source: str,
    pull_stats: PullStats,
    warnings: tuple[str, ...],
    demo_mode: bool,
    artifact_source: Path,
) -> dict[str, Any]:
    """Score current bars with a frozen artifact and preserve training outputs."""

    pipeline_started = perf_counter()
    stage_started = pipeline_started
    stage_seconds: dict[str, float] = {}
    config = store.config
    artifact = load_model_artifact(
        artifact_source,
        config=config,
        expected_mode="DEMO" if demo_mode else "LIVE",
    )
    release_hashes = dict(artifact.get("training_release_hashes") or {})
    release_file_refs = dict(artifact.get("training_release_files") or {})
    required_release_files = {
        "seasonality": "seasonality_sha256",
        "backtest_trades": "backtest_trades_sha256",
    }
    missing_release_entries = sorted(
        [
            key
            for key, hash_name in required_release_files.items()
            if key not in release_file_refs or hash_name not in release_hashes
        ]
    )
    if missing_release_entries:
        raise ModelArtifactError(
            "Frozen model does not contain a complete versioned release bundle; "
            "retrain before score-only use. Missing: "
            + ", ".join(missing_release_entries)
        )
    release_root = artifact_source.resolve().parent
    release_paths: dict[str, Path] = {}
    for key, hash_name in required_release_files.items():
        release_path = (release_root / str(release_file_refs[key])).resolve()
        if not release_path.is_relative_to(release_root):
            raise ModelArtifactError(
                f"Frozen model release path escapes its model directory: {release_path}"
            )
        if not release_path.is_file() or (
            frame_sha256(release_path) != release_hashes[hash_name]
        ):
            raise ModelArtifactError(
                "Frozen model release bundle mismatch for "
                f"{release_path.name}; run TRAIN_AND_SCORE.bat to restore a "
                "consistent model, seasonality profile, and trade ledger."
            )
        release_paths[key] = release_path
    all_bars = store.load_bars()
    daily = store.load_daily()
    contracts = store.load_contracts()
    stage_seconds["load_data_and_model"] = perf_counter() - stage_started
    stage_started = perf_counter()
    quality = data_quality_report(
        all_bars, contracts, config, daily=daily, depth_source=depth_source
    )
    trade_sessions = sorted(
        all_bars.filter(pl.col("event_type") == "TRADE")["session_date"]
        .unique()
        .to_list()
    )
    score_history_sessions = min(80, len(trade_sessions))
    if not score_history_sessions:
        raise TechnicalDataError("No intraday sessions are available for scoring")
    artifact_as_of = date.fromisoformat(str(artifact["as_of"]))
    model_age_sessions = sum(session > artifact_as_of for session in trade_sessions)
    model_stale = (
        model_age_sessions > config.system.maximum_model_age_sessions
    )
    quality = pl.concat(
        [
            quality,
            pl.DataFrame(
                [
                    {
                        "check": "frozen_model_freshness",
                        "status": "FAIL" if model_stale else "PASS",
                        "actual": f"{model_age_sessions} sessions old",
                        "expected": (
                            f"<={config.system.maximum_model_age_sessions} sessions"
                        ),
                        "blocking": False,
                        "notes": (
                            "MODEL STALE blocks directional promotion until "
                            "TRAIN_AND_SCORE.bat refreshes the frozen evidence."
                        ),
                    }
                ]
            ),
        ],
        how="diagonal_relaxed",
    )
    score_cutoff = trade_sessions[-score_history_sessions]
    scoring_bars = all_bars.filter(pl.col("session_date") >= score_cutoff)
    prepared = prepare_trade_bars(scoring_bars, contracts, daily, config)
    spreads = build_spread_bars(prepared, config)
    stage_seconds["recent_spread_build"] = perf_counter() - stage_started
    stage_started = perf_counter()
    if spreads.is_empty():
        raise TechnicalDataError(
            "No executable spread bars were built for frozen-model scoring"
        )
    seasonality_path = release_paths["seasonality"]
    if not seasonality_path.is_file() or seasonality_path.stat().st_size == 0:
        raise TechnicalDataError(
            "Frozen seasonality profile is missing. Run TRAIN_AND_SCORE.bat first."
        )
    seasonality = pl.read_csv(
        seasonality_path, try_parse_dates=True, infer_schema_length=10_000
    )
    if (
        "asof_year" not in seasonality.columns
        or as_of.year not in seasonality["asof_year"].unique().to_list()
    ):
        raise TechnicalDataError(
            "Frozen seasonality profile does not cover the current year. "
            "Run TRAIN_AND_SCORE.bat to refresh the model."
        )
    stage_seconds["load_frozen_seasonality"] = perf_counter() - stage_started
    stage_started = perf_counter()
    features = add_indicators(spreads, config, seasonality)
    features = apply_frozen_expert_model(features, artifact, config)
    features = attach_kronos_diagnostics(
        features, store.paths.bars.with_name("kronos_forecasts.parquet"), config
    )
    stage_seconds["indicators_and_frozen_model"] = perf_counter() - stage_started
    stage_started = perf_counter()
    selected_scorecard = scorecard_from_artifact(artifact)
    quality_blocking = (
        quality.filter(pl.col("blocking") & (pl.col("status") == "FAIL")).height > 0
    )
    liquidity_snapshot = store.load_liquidity(
        l2=depth_source in {"BPIPE_L2", "DEMO_L2"}
    )
    depth_metrics = package_depth_snapshot(
        features,
        liquidity_snapshot,
        config,
        depth_source=depth_source,
    )
    live_signals = build_live_signal_board(
        features,
        selected_scorecard,
        config,
        depth_source=depth_source,
        depth_metrics=depth_metrics,
        quality_blocking=quality_blocking,
        demo_mode=demo_mode,
        model_stale=model_stale,
        model_age_sessions=model_age_sessions,
    )
    trades_path = release_paths["backtest_trades"]
    trained_trades = (
        pl.read_csv(trades_path, try_parse_dates=True, infer_schema_length=10_000)
        if trades_path.is_file() and trades_path.stat().st_size > 0
        else pl.DataFrame()
    )
    lockbox_start = date.fromisoformat(artifact["training_window"]["lockbox_start"])
    lockbox_end = date.fromisoformat(artifact["training_window"]["lockbox_end"])
    structure_summaries, recent_selected_trades = build_structure_summaries(
        live_signals,
        selected_scorecard,
        trained_trades,
        lockbox_start=lockbox_start,
        lockbox_end=lockbox_end,
        demo_mode=demo_mode,
    )
    portfolio_lockbox_trades = portfolio_lockbox_trade_budget(
        trained_trades, structure_summaries, config
    )
    artifact_summary_context = dict(artifact)
    artifact_summary_context["model_age_sessions"] = model_age_sessions
    artifact_summary_context["maximum_model_age_sessions"] = (
        config.system.maximum_model_age_sessions
    )
    artifact_summary_context["model_stale"] = model_stale
    model_summary = build_model_summary(
        structure_summaries,
        recent_selected_trades,
        selected_scorecard,
        artifact_summary_context,
        demo_mode=demo_mode,
        portfolio_lockbox_trades=portfolio_lockbox_trades,
    )
    score_indicator_audit = indicator_library_audit(features, config)
    stage_seconds["signals"] = perf_counter() - stage_started
    stage_started = perf_counter()
    trade = all_bars.filter(pl.col("event_type") == "TRADE")
    source_hashes = {
        "bars_sha256": frame_sha256(store.paths.bars),
        "daily_sha256": frame_sha256(store.paths.daily),
        "contracts_sha256": frame_sha256(store.paths.contracts),
    }
    parity: dict[str, Any] = {"status": "NOT_APPLICABLE_SOURCE_CHANGED"}
    if artifact.get("training_source_hashes") == source_hashes:
        training_snapshot = pl.DataFrame(
            artifact.get("training_signal_snapshot") or [], strict=False
        )
        if training_snapshot.is_empty():
            raise TechnicalDataError(
                "Frozen model lacks its train/score parity snapshot; retrain it."
            )
        comparison = training_snapshot.join(
            live_signals.select(
                "spread_id",
                "status",
                "signal_strategy_id",
                *TRAIN_SCORE_PARITY_FIELDS,
            ),
            on="spread_id",
            how="inner",
            suffix="_score",
        )
        deltas = {
            field: float(
                comparison.select(
                    (pl.col(field) - pl.col(f"{field}_score")).abs().max()
                ).item()
                or 0.0
            )
            for field in TRAIN_SCORE_PARITY_FIELDS
        }
        status_mismatches = comparison.filter(
            (pl.col("status") != pl.col("status_score"))
            | (
                pl.col("signal_strategy_id")
                != pl.col("signal_strategy_id_score")
            )
        ).height
        null_mismatches = sum(
            comparison.filter(
                pl.col(field).is_null() != pl.col(f"{field}_score").is_null()
            ).height
            for field in TRAIN_SCORE_PARITY_FIELDS
        )
        maximum_delta = max(deltas.values(), default=0.0)
        if (
            comparison.height != live_signals.height
            or status_mismatches
            or null_mismatches
            or maximum_delta > 1e-9
        ):
            raise TechnicalDataError(
                "Frozen score-only output diverged from training on identical data: "
                f"rows={comparison.height}/{live_signals.height}, "
                f"status_mismatches={status_mismatches}, "
                f"null_mismatches={null_mismatches}, max_delta={maximum_delta}"
            )
        parity = {
            "status": "PASS",
            "rows": comparison.height,
            "status_mismatches": status_mismatches,
            "null_mismatches": null_mismatches,
            "maximum_absolute_delta": maximum_delta,
        }
    manifest: dict[str, Any] = {
        "mode": "DEMO" if demo_mode else "LIVE",
        "workflow": "SCORE_CURRENT",
        "as_of": as_of.isoformat(),
        "model_artifact": str(artifact_source),
        "model_id": artifact["model_id"],
        "model_training_window": artifact["training_window"],
        "model_contract_sha256": artifact["model_contract_sha256"],
        "approved_for_live_signals": artifact["approved_for_live_signals"],
        "training_release_files": release_file_refs,
        "training_release_hashes": release_hashes,
        "model_age_sessions": model_age_sessions,
        "maximum_model_age_sessions": config.system.maximum_model_age_sessions,
        "model_stale": model_stale,
        "train_score_parity": parity,
        "latest_30_sessions": model_summary["latest_30_sessions"],
        "stage_seconds": stage_seconds,
        "data_sources": sorted(
            str(item) for item in trade["source"].unique().to_list()
        ),
        "depth_source": depth_source,
        "source_bars": all_bars.height,
        "source_trade_bars": trade.height,
        "source_sessions": trade["session_date"].n_unique(),
        "source_coverage_start": str(trade["session_date"].min()),
        "source_coverage_end": str(trade["session_date"].max()),
        "scoring_history_sessions": score_history_sessions,
        "scoring_coverage_start": str(score_cutoff),
        "feature_rows": features.height,
        "spreads": features["spread_id"].n_unique(),
        "backtest_trades": 0,
        "validated_strategy_spreads": artifact["validated_strategy_count"],
        "pull_stats": asdict(pull_stats),
        "warnings": list(warnings),
        "files": source_hashes,
    }
    output_paths = write_technical_score_outputs(
        project_root,
        config=config,
        features=features,
        live_signals=live_signals,
        selected_scorecard=selected_scorecard,
        backtest_trades=trained_trades,
        indicator_audit=score_indicator_audit,
        structure_summaries=structure_summaries,
        model_summary=model_summary,
        quality=quality,
        contracts=contracts,
        manifest=manifest,
        demo_mode=demo_mode,
        as_of=as_of,
    )
    manifest["stage_seconds"]["reporting"] = perf_counter() - stage_started
    manifest["stage_seconds"]["total_analysis"] = perf_counter() - pipeline_started
    write_manifest(output_paths["scoring_manifest"], manifest)
    return {
        "manifest": manifest,
        "paths": output_paths,
        "signals": live_signals,
        "quality": quality,
        "model_artifact": artifact_source,
    }


def run_technical_pipeline(
    project_root: str | Path,
    *,
    mode: str,
    as_of: date,
    backfill: str | Path | None = None,
    skip_pull: bool = False,
    regenerate_demo: bool = True,
    workflow: str = "train",
    model_artifact: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    normalized_mode = str(mode).strip().lower()
    if normalized_mode not in {"live", "demo"}:
        raise ValueError("mode must be 'live' or 'demo'")
    normalized_workflow = str(workflow).strip().lower()
    if normalized_workflow not in {"train", "score"}:
        raise ValueError("workflow must be 'train' or 'score'")
    config = load_technical_config(root / "config" / "technical_system.toml")
    artifact_file = (
        Path(model_artifact).expanduser().resolve()
        if model_artifact is not None
        else model_artifact_path(root, normalized_mode)
    )
    if normalized_mode == "demo":
        if regenerate_demo:
            from scripts.generate_demo_technical_data import write_demo

            write_demo(root, as_of)
        store = TechnicalStore(DataPaths.under(root, dataset="demo"), config)
        common = {
            "project_root": root,
            "store": store,
            "as_of": as_of,
            "depth_source": "DEMO_L2",
            "pull_stats": PullStats(),
            "warnings": ("Synthetic demo data is never a live recommendation.",),
            "demo_mode": True,
        }
        if normalized_workflow == "score":
            return _score_and_report(
                **common,
                artifact_source=artifact_file,
            )
        return _analyze_and_report(
            **common,
            artifact_target=artifact_file,
        )
    backfill_path = Path(backfill).resolve() if backfill else None
    store, depth_source, stats, warnings = asyncio.run(
        _live_update(
            root,
            as_of,
            backfill=backfill_path,
            skip_pull=skip_pull,
        )
    )
    warnings = tuple(warnings) + refresh_kronos_sidecar(root, store.paths.bars)
    common = {
        "project_root": root,
        "store": store,
        "as_of": as_of,
        "depth_source": depth_source,
        "pull_stats": stats,
        "warnings": warnings,
        "demo_mode": False,
    }
    if normalized_workflow == "score":
        return _score_and_report(
            **common,
            artifact_source=artifact_file,
        )
    return _analyze_and_report(
        **common,
        artifact_target=artifact_file,
    )


__all__ = ["run_technical_pipeline"]
