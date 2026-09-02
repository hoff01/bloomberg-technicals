"""XBBG ingestion, normalized intraday storage, and expiry metadata.

The module intentionally imports XBBG only inside the live client.  Demo,
validation, reporting, and backtests therefore run on machines without a
Bloomberg SDK while the same code can use a licensed Terminal at localhost:8194.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import tempfile
import time as clock
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import polars as pl

from app.technical_config import (
    ContractDefinition,
    TechnicalConfig,
    add_months,
    exchange_session_offset,
)


BAR_COLUMNS = (
    "timestamp_utc",
    "bar_start_et",
    "bar_end_et",
    "session_date",
    "bar_slot",
    "event_type",
    "root",
    "security",
    "delivery_month",
    "open",
    "high",
    "low",
    "close",
    "volume_contracts",
    "num_events",
    "value",
    "source",
    "pulled_at_utc",
)


class TechnicalDataError(RuntimeError):
    """Base class for data ingestion and validation failures."""


class XbbgUnavailableError(TechnicalDataError):
    """Raised when XBBG or the Bloomberg SDK runtime cannot be loaded."""


@dataclass(frozen=True, slots=True)
class DataPaths:
    project_root: Path
    bars: Path
    daily: Path
    contracts: Path
    liquidity: Path
    depth: Path
    quality: Path
    run_manifest: Path

    @classmethod
    def under(cls, root: str | Path, dataset: str = "live") -> "DataPaths":
        base = Path(root).resolve()
        if dataset not in {"live", "demo"}:
            raise ValueError("dataset must be 'live' or 'demo'")
        data_dir = base / "data" / ("technical" if dataset == "live" else "technical_demo")
        return cls(
            project_root=base,
            bars=data_dir / "intraday_bars.parquet",
            daily=data_dir / "daily_contract_history.parquet",
            contracts=data_dir / "contract_registry.parquet",
            liquidity=data_dir / "top_of_book.parquet",
            depth=data_dir / "market_depth.parquet",
            quality=base / "dist" / "technical_data_quality.csv",
            run_manifest=base / "dist" / "technical_run_manifest.json",
        )


@dataclass(frozen=True, slots=True)
class PullStats:
    requested: int = 0
    succeeded: int = 0
    empty: int = 0
    failed: int = 0
    trade_requested: int = 0
    trade_succeeded: int = 0
    trade_empty: int = 0
    trade_failed: int = 0
    rows: int = 0
    warnings: tuple[str, ...] = ()


def _empty_bars() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "timestamp_utc": pl.Datetime("us", "UTC"),
            "bar_start_et": pl.Datetime("us", "America/New_York"),
            "bar_end_et": pl.Datetime("us", "America/New_York"),
            "session_date": pl.Date,
            "bar_slot": pl.Int16,
            "event_type": pl.Utf8,
            "root": pl.Utf8,
            "security": pl.Utf8,
            "delivery_month": pl.Date,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume_contracts": pl.Float64,
            "num_events": pl.Float64,
            "value": pl.Float64,
            "source": pl.Utf8,
            "pulled_at_utc": pl.Datetime("us", "UTC"),
        }
    )


def _canonical_column(name: str) -> str:
    key = str(name).strip().lower().replace(" ", "_")
    aliases = {
        "time": "time",
        "datetime": "time",
        "timestamp": "time",
        "num_events": "num_events",
        "numevents": "num_events",
        "number_of_events": "num_events",
        "volume": "volume_contracts",
        "px_volume": "volume_contracts",
        "ticker": "security",
        "security": "security",
    }
    return aliases.get(key, key)


def _rename_columns(frame: pl.DataFrame) -> pl.DataFrame:
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for column in frame.columns:
        canonical = _canonical_column(column)
        if canonical in used:
            continue
        mapping[column] = canonical
        used.add(canonical)
    return frame.rename(mapping)


def _pivot_xbbg_long(frame: pl.DataFrame, index: Sequence[str]) -> pl.DataFrame:
    """Pivot XBBG v1's default long result while accepting already-wide data."""

    if frame.is_empty() or not {"field", "value"}.issubset(frame.columns):
        return frame
    available_index = [column for column in index if column in frame.columns]
    if not available_index:
        return frame
    return frame.pivot(
        on="field",
        index=available_index,
        values="value",
        aggregate_function="last",
    )


def _coerce_polars(value: Any) -> pl.DataFrame:
    if value is None:
        return pl.DataFrame()
    if isinstance(value, pl.LazyFrame):
        return value.collect()
    if isinstance(value, pl.DataFrame):
        return value
    try:
        return pl.from_arrow(value)
    except Exception:
        pass
    try:
        return pl.from_pandas(value, include_index=False)
    except Exception as exc:
        raise TechnicalDataError(
            f"XBBG returned unsupported frame type {type(value).__name__}: {exc}"
        ) from exc


def normalize_intraday_frame(
    raw: Any,
    contract: ContractDefinition,
    config: TechnicalConfig,
    *,
    event_type: str,
    pulled_at: datetime | None = None,
    source: str = "XBBG",
) -> pl.DataFrame:
    """Normalize one XBBG BDIB response and retain only complete US-session bars."""

    frame = _rename_columns(_coerce_polars(raw))
    if frame.is_empty():
        return _empty_bars()
    required = {"time", "open", "high", "low", "close"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise TechnicalDataError(
            f"{contract.ticker} {event_type}: missing XBBG bar columns {', '.join(missing)}"
        )
    for optional in ("volume_contracts", "num_events", "value"):
        if optional not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias(optional))

    time_dtype = frame.schema["time"]
    if not isinstance(time_dtype, pl.Datetime):
        frame = frame.with_columns(
            pl.col("time").cast(pl.Utf8).str.to_datetime(strict=False).alias("time")
        )
        time_dtype = frame.schema["time"]
    if not isinstance(time_dtype, pl.Datetime):
        raise TechnicalDataError(f"{contract.ticker}: XBBG time column is not datetime")
    if time_dtype.time_zone:
        time_expr = pl.col("time").dt.convert_time_zone(config.system.timezone)
    else:
        time_expr = pl.col("time").dt.replace_time_zone(config.system.timezone)

    interval = timedelta(minutes=config.system.bar_interval_minutes)
    pulled = pulled_at or datetime.now(timezone.utc)
    frame = frame.with_columns(time_expr.alias("bar_start_et")).with_columns(
        (pl.col("bar_start_et") + interval).alias("bar_end_et")
    )
    frame = frame.filter(
        (pl.col("bar_start_et").dt.time() >= config.system.session_start)
        & (pl.col("bar_end_et").dt.time() <= config.system.session_end)
    )
    if frame.is_empty():
        return _empty_bars()

    start_minutes = config.system.session_start.hour * 60 + config.system.session_start.minute
    frame = frame.with_columns(
        pl.col("bar_start_et").dt.convert_time_zone("UTC").alias("timestamp_utc"),
        pl.col("bar_start_et").dt.date().alias("session_date"),
        (
            (
                pl.col("bar_start_et").dt.hour() * 60
                + pl.col("bar_start_et").dt.minute()
                - start_minutes
            )
            // config.system.bar_interval_minutes
        )
        .cast(pl.Int16)
        .alias("bar_slot"),
        pl.lit(event_type.upper()).alias("event_type"),
        pl.lit(contract.root).alias("root"),
        pl.lit(contract.ticker).alias("security"),
        pl.lit(contract.delivery_month).cast(pl.Date).alias("delivery_month"),
        pl.lit(source).alias("source"),
        pl.lit(pulled).cast(pl.Datetime("us", "UTC")).alias("pulled_at_utc"),
    )
    numeric = ["open", "high", "low", "close", "volume_contracts", "num_events", "value"]
    frame = frame.with_columns([pl.col(column).cast(pl.Float64, strict=False) for column in numeric])
    frame = frame.select(BAR_COLUMNS).drop_nulls(["timestamp_utc", "close"])
    return frame.unique(["security", "event_type", "timestamp_utc"], keep="last").sort(
        ["timestamp_utc", "root", "security", "event_type"]
    )


def _field_lookup(columns: Sequence[str], *candidates: str) -> str | None:
    by_key = {_canonical_column(column): column for column in columns}
    for candidate in candidates:
        found = by_key.get(_canonical_column(candidate))
        if found:
            return found
    return None


def normalize_contract_registry(
    definitions: Sequence[ContractDefinition], raw: Any | None = None
) -> pl.DataFrame:
    """Combine generated contracts with Bloomberg expiry/reference fields."""

    metadata = _rename_columns(_coerce_polars(raw)) if raw is not None else pl.DataFrame()
    metadata = _pivot_xbbg_long(metadata, ["security"])
    rows: list[dict[str, object]] = []
    metadata_rows: dict[str, dict[str, object]] = {}
    if not metadata.is_empty():
        security_col = _field_lookup(metadata.columns, "security", "ticker")
        if security_col:
            for row in metadata.to_dicts():
                metadata_rows[str(row.get(security_col) or "").strip()] = row

    for definition in definitions:
        row = metadata_rows.get(definition.ticker, {})

        def get_date(*names: str) -> date | None:
            for name in names:
                value = row.get(_canonical_column(name))
                if value is None:
                    value = row.get(name)
                if isinstance(value, datetime):
                    return value.date()
                if isinstance(value, date):
                    return value
                if value not in (None, ""):
                    try:
                        return date.fromisoformat(str(value)[:10])
                    except ValueError:
                        continue
            return None

        last_trade = get_date("FUT_LAST_TRADE_DT", "LAST_TRADEABLE_DT")
        first_notice = get_date("FUT_NOTICE_FIRST")
        first_delivery = get_date("FUT_DLV_DT_FIRST")
        candidates = [item for item in (last_trade, first_notice, first_delivery) if item]
        risk_date = min(candidates) if candidates else definition.fallback_expiry
        verified = bool(last_trade)
        blackout_start = exchange_session_offset(risk_date, -3, definition.root)
        forced_exit_session = exchange_session_offset(
            blackout_start, -1, definition.root
        )
        rows.append(
            {
                "root": definition.root,
                "security": definition.ticker,
                "delivery_month": definition.delivery_month,
                "last_trade_date": last_trade or definition.fallback_expiry,
                "first_notice_date": first_notice,
                "first_delivery_date": first_delivery,
                "risk_date": risk_date,
                "blackout_start": blackout_start,
                "forced_exit_session": forced_exit_session,
                "expiry_verified": verified,
                "expiry_source": "BLOOMBERG" if verified else "FALLBACK_UNVERIFIED",
            }
        )
    return pl.DataFrame(rows).with_columns(
        pl.col("delivery_month").cast(pl.Date),
        pl.col("last_trade_date").cast(pl.Date),
        pl.col("first_notice_date").cast(pl.Date),
        pl.col("first_delivery_date").cast(pl.Date),
        pl.col("risk_date").cast(pl.Date),
        pl.col("blackout_start").cast(pl.Date),
        pl.col("forced_exit_session").cast(pl.Date),
    ).sort(["root", "delivery_month"])


def normalize_daily_frame(
    raw: Any,
    definitions: Mapping[str, ContractDefinition],
    *,
    source: str = "XBBG",
) -> pl.DataFrame:
    frame = _rename_columns(_coerce_polars(raw))
    if frame.is_empty():
        return pl.DataFrame()
    frame = _pivot_xbbg_long(frame, ["security", "date"])
    security_col = _field_lookup(frame.columns, "security", "ticker")
    date_col = _field_lookup(frame.columns, "date")
    if not security_col or not date_col:
        raise TechnicalDataError("Daily XBBG output requires ticker/security and date columns")
    rename: dict[str, str] = {}
    for canonical, candidates in {
        "settle": ("PX_SETTLE", "settle"),
        "last_price": ("PX_LAST", "close", "last_price"),
        "volume_contracts": ("PX_VOLUME", "volume"),
        "open_interest": ("FUT_AGGTE_OPEN_INT", "open_interest"),
    }.items():
        found = _field_lookup(frame.columns, *candidates)
        if found:
            rename[found] = canonical
    frame = frame.rename(rename)
    for column in ("settle", "last_price", "volume_contracts", "open_interest"):
        if column not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias(column))
    frame = frame.rename({security_col: "security", date_col: "session_date"})
    meta = pl.DataFrame(
        [
            {
                "security": item.ticker,
                "root": item.root,
                "delivery_month": item.delivery_month,
            }
            for item in definitions.values()
        ]
    ).with_columns(pl.col("delivery_month").cast(pl.Date))
    return (
        frame.with_columns(
            pl.col("session_date").cast(pl.Date),
            pl.col("settle").cast(pl.Float64, strict=False),
            pl.col("last_price").cast(pl.Float64, strict=False),
            pl.col("volume_contracts").cast(pl.Float64, strict=False),
            pl.col("open_interest").cast(pl.Float64, strict=False),
        )
        .join(meta, on="security", how="inner")
        .with_columns(
            pl.coalesce([pl.col("settle"), pl.col("last_price")]).alias("close"),
            pl.when(pl.col("settle").is_not_null())
            .then(pl.lit("PX_SETTLE"))
            .when(pl.col("last_price").is_not_null())
            .then(pl.lit("PX_LAST_FALLBACK"))
            .otherwise(pl.lit("MISSING"))
            .alias("settle_source_field"),
            pl.lit(source).alias("source"),
        )
        .select(
            "session_date",
            "root",
            "security",
            "delivery_month",
            "close",
            "settle",
            "last_price",
            "settle_source_field",
            "volume_contracts",
            "open_interest",
            "source",
        )
        .unique(["session_date", "security"], keep="last")
        .sort(["session_date", "root", "delivery_month"])
    )


def _chunks(start: date, end: date, days: int) -> Iterable[tuple[date, date]]:
    current = start
    while current <= end:
        terminal = min(end, current + timedelta(days=max(1, days) - 1))
        yield current, terminal
        current = terminal + timedelta(days=1)


class XbbgTechnicalClient:
    """Bounded-concurrency XBBG client with a native Polars output backend."""

    def __init__(self, config: TechnicalConfig) -> None:
        self.config = config
        self._xbbg: Any | None = None

    def _load(self) -> Any:
        if self._xbbg is not None:
            return self._xbbg
        try:
            module = importlib.import_module("xbbg")
        except (ImportError, OSError) as exc:
            raise XbbgUnavailableError(
                "XBBG or Bloomberg's SDK runtime could not be loaded. Keep Bloomberg "
                "Terminal open, then run INSTALL_BLOOMBERG.bat if needed. "
                f"Original error: {exc}"
            ) from exc
        module.configure(
            host=self.config.bloomberg.host,
            port=self.config.bloomberg.port,
            request_pool_size=self.config.bloomberg.request_pool_size,
            request_timeout_ms=(
                self.config.bloomberg.request_timeout_seconds * 1000
            ),
            retry_max_retries=self.config.bloomberg.retry_max_retries,
        )
        module.set_backend("polars")
        self._xbbg = module
        return module

    async def _await_request(
        self,
        operation: Any,
        label: str,
        *,
        announce: bool = False,
    ) -> Any:
        """Apply a hard timeout and periodic progress to one XBBG coroutine."""

        timeout_seconds = self.config.bloomberg.request_timeout_seconds
        heartbeat_seconds = self.config.bloomberg.heartbeat_seconds
        started = clock.monotonic()
        deadline = started + timeout_seconds
        task = asyncio.ensure_future(operation)

        async def cancel_bounded() -> None:
            if task.done():
                return
            task.cancel()
            await asyncio.wait({task}, timeout=0.25)

        if announce:
            print(f"{label} start", flush=True)
        try:
            while True:
                remaining = deadline - clock.monotonic()
                if remaining <= 0:
                    raise TechnicalDataError(
                        f"{label} exceeded the hard timeout of {timeout_seconds} seconds"
                    )
                try:
                    result = await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=min(float(heartbeat_seconds), remaining),
                    )
                    if announce:
                        print(
                            f"{label} complete in {clock.monotonic() - started:.1f}s",
                            flush=True,
                        )
                    return result
                except asyncio.TimeoutError:
                    if task.done():
                        return task.result()
                    print(
                        f"{label} waiting {clock.monotonic() - started:.0f}s",
                        flush=True,
                    )
        finally:
            await cancel_bounded()

    async def fetch_reference(
        self, definitions: Sequence[ContractDefinition], batch_size: int = 24
    ) -> pl.DataFrame:
        xbbg = self._load()
        tickers = [item.ticker for item in definitions]
        batches = [
            tickers[index : index + batch_size]
            for index in range(0, len(tickers), batch_size)
        ]
        total_batches = len(batches)
        semaphore = asyncio.Semaphore(
            min(
                self.config.bloomberg.request_pool_size,
                self.config.system.max_concurrent_requests,
            )
        )

        async def one(batch_number: int, batch: list[str]) -> pl.DataFrame:
            async with semaphore:
                response = await self._await_request(
                    xbbg.abdp(
                        batch,
                        list(self.config.bloomberg.reference_fields),
                        backend="polars",
                        validate_fields=True,
                    ),
                    f"BBG REF {batch_number}/{total_batches} {batch}",
                    announce=True,
                )
            return _coerce_polars(response)

        pieces = await asyncio.gather(
            *(one(index, batch) for index, batch in enumerate(batches, start=1))
        )
        return pl.concat(pieces, how="diagonal_relaxed") if pieces else pl.DataFrame()

    async def fetch_daily(
        self,
        definitions: Sequence[ContractDefinition],
        start: date,
        end: date,
        batch_size: int = 20,
    ) -> pl.DataFrame:
        xbbg = self._load()
        tickers = [item.ticker for item in definitions]
        batches = [
            tickers[index : index + batch_size]
            for index in range(0, len(tickers), batch_size)
        ]
        total_batches = len(batches)
        semaphore = asyncio.Semaphore(
            min(
                self.config.bloomberg.request_pool_size,
                self.config.system.max_concurrent_requests,
            )
        )

        async def one(batch_number: int, batch: list[str]) -> pl.DataFrame:
            async with semaphore:
                response = await self._await_request(
                    xbbg.abdh(
                        batch,
                        list(self.config.bloomberg.daily_fields),
                        start_date=start,
                        end_date=end,
                        backend="polars",
                        validate_fields=True,
                    ),
                    f"BBG HIST {batch_number}/{total_batches} {batch} {start}:{end}",
                    announce=True,
                )
            return _coerce_polars(response)

        pieces = await asyncio.gather(
            *(one(index, batch) for index, batch in enumerate(batches, start=1))
        )
        return pl.concat(pieces, how="diagonal_relaxed") if pieces else pl.DataFrame()

    async def fetch_intraday(
        self,
        definitions: Sequence[ContractDefinition],
        start: date,
        end: date,
        *,
        event_types: Sequence[str] | None = None,
    ) -> tuple[pl.DataFrame, PullStats]:
        xbbg = self._load()
        types = tuple(event_types or (self.config.bloomberg.event_type,))
        semaphore = asyncio.Semaphore(self.config.system.max_concurrent_requests)
        pieces: list[pl.DataFrame] = []
        warnings: list[str] = []
        counts = {"requested": 0, "succeeded": 0, "empty": 0, "failed": 0}
        trade_counts = {
            "requested": 0,
            "succeeded": 0,
            "empty": 0,
            "failed": 0,
        }

        async def one(
            definition: ContractDefinition,
            chunk_start: date,
            chunk_end: date,
            event_type: str,
        ) -> None:
            counts["requested"] += 1
            is_trade = event_type == self.config.bloomberg.event_type
            if is_trade:
                trade_counts["requested"] += 1
            start_dt = datetime.combine(chunk_start, self.config.system.session_start)
            end_dt = datetime.combine(chunk_end, self.config.system.session_end)
            try:
                async with semaphore:
                    response = await self._await_request(
                        xbbg.abdib(
                            definition.ticker,
                            start_datetime=start_dt,
                            end_datetime=end_dt,
                            interval=self.config.system.bar_interval_minutes,
                            typ=event_type,
                            backend="polars",
                            request_tz=self.config.system.timezone,
                            output_tz=self.config.system.timezone,
                            gapFillInitialBar=self.config.bloomberg.gap_fill_initial_bar,
                        ),
                        (
                            f"BBG INTRADAY {definition.ticker} {event_type} "
                            f"{chunk_start}:{chunk_end}"
                        ),
                    )
                normalized = normalize_intraday_frame(
                    response, definition, self.config, event_type=event_type
                )
                if normalized.is_empty():
                    counts["empty"] += 1
                    if is_trade:
                        trade_counts["empty"] += 1
                else:
                    pieces.append(normalized)
                    counts["succeeded"] += 1
                    if is_trade:
                        trade_counts["succeeded"] += 1
            except Exception as exc:
                counts["failed"] += 1
                if is_trade:
                    trade_counts["failed"] += 1
                warnings.append(
                    f"{definition.ticker} {event_type} {chunk_start}:{chunk_end}: {type(exc).__name__}: {exc}"
                )

        tasks = [
            one(definition, chunk_start, chunk_end, event_type)
            for definition in definitions
            for event_type in types
            for chunk_start, chunk_end in _chunks(
                start, end, self.config.system.pull_chunk_days
            )
        ]
        print(
            f"BBG INTRADAY queued {len(tasks):,} bounded requests "
            f"(concurrency={self.config.system.max_concurrent_requests})",
            flush=True,
        )
        await asyncio.gather(*tasks)
        result = (
            pl.concat(pieces, how="vertical_relaxed")
            .unique(["security", "event_type", "timestamp_utc"], keep="last")
            .sort(["timestamp_utc", "security", "event_type"])
            if pieces
            else _empty_bars()
        )
        return result, PullStats(
            requested=counts["requested"],
            succeeded=counts["succeeded"],
            empty=counts["empty"],
            failed=counts["failed"],
            trade_requested=trade_counts["requested"],
            trade_succeeded=trade_counts["succeeded"],
            trade_empty=trade_counts["empty"],
            trade_failed=trade_counts["failed"],
            rows=result.height,
            warnings=tuple(warnings),
        )

    async def capture_liquidity(
        self, tickers: Sequence[str], seconds: int | None = None
    ) -> tuple[pl.DataFrame, str, tuple[str, ...]]:
        """Capture L2 on B-PIPE when available, else Terminal top-of-book."""

        xbbg = self._load()
        duration = max(0, int(seconds if seconds is not None else self.config.liquidity.capture_seconds))
        if not tickers or duration <= 0:
            return pl.DataFrame(), "DISABLED", ()
        warnings: list[str] = []

        async def collect(subscription: Any) -> pl.DataFrame:
            frames: list[pl.DataFrame] = []
            deadline = clock.monotonic() + duration
            async with subscription as stream:
                iterator = stream.__aiter__()
                while clock.monotonic() < deadline:
                    timeout = max(0.05, min(1.0, deadline - clock.monotonic()))
                    try:
                        batch = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
                    except asyncio.TimeoutError:
                        continue
                    except StopAsyncIteration:
                        break
                    frames.append(_coerce_polars(batch))
            return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

        mode = self.config.liquidity.depth_mode
        if mode in {"auto", "bpipe", "l2"}:
            try:
                depth_subscription = await self._await_request(
                    xbbg.adepth(list(tickers), backend="polars", all_fields=True),
                    f"BBG DEPTH subscription {len(tickers)} tickers",
                    announce=True,
                )
                depth = await collect(depth_subscription)
                if not depth.is_empty():
                    return depth, "BPIPE_L2", tuple(warnings)
            except Exception as exc:
                warnings.append(f"L2 depth unavailable: {type(exc).__name__}: {exc}")
                if mode in {"bpipe", "l2"}:
                    return pl.DataFrame(), "UNAVAILABLE", tuple(warnings)

        try:
            subscription = await self._await_request(
                xbbg.asubscribe(
                    list(tickers),
                    list(self.config.liquidity.top_of_book_fields),
                    backend="polars",
                ),
                f"BBG TOP subscription {len(tickers)} tickers",
                announce=True,
            )
            top = await collect(subscription)
            return top, "TOP_OF_BOOK", tuple(warnings)
        except Exception as exc:
            warnings.append(f"Top-of-book unavailable: {type(exc).__name__}: {exc}")
            return pl.DataFrame(), "BAR_PROXY_ONLY", tuple(warnings)


def _atomic_write_parquet(frame: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        frame.write_parquet(temp_path, compression="zstd", statistics=True)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _read_parquet(path: Path, fallback: pl.DataFrame | None = None) -> pl.DataFrame:
    if not path.is_file():
        return fallback if fallback is not None else pl.DataFrame()
    try:
        return pl.read_parquet(path)
    except Exception as exc:
        raise TechnicalDataError(f"Could not read {path}: {exc}") from exc


class TechnicalStore:
    """Atomic, rolling Parquet store for intraday, daily, and metadata tables."""

    def __init__(self, paths: DataPaths, config: TechnicalConfig) -> None:
        self.paths = paths
        self.config = config

    def load_bars(self) -> pl.DataFrame:
        return _read_parquet(self.paths.bars, _empty_bars())

    def update_bars(self, incoming: pl.DataFrame, as_of: date) -> pl.DataFrame:
        existing = self.load_bars()
        target_year = as_of.year - self.config.system.rolling_intraday_months // 12
        target_month = as_of.month - self.config.system.rolling_intraday_months % 12
        if target_month <= 0:
            target_year -= 1
            target_month += 12
        try:
            cutoff = as_of.replace(year=target_year, month=target_month)
        except ValueError:
            # February 29 and shorter target months use their final calendar day.
            cutoff = add_months(date(target_year, target_month, 1), 1) - timedelta(days=1)
        frames = [frame for frame in (existing, incoming) if not frame.is_empty()]
        merged = pl.concat(frames, how="diagonal_relaxed") if frames else _empty_bars()
        if not merged.is_empty():
            merged = (
                merged.filter(pl.col("session_date") >= cutoff)
                .unique(["security", "event_type", "timestamp_utc"], keep="last")
                .sort(["timestamp_utc", "security", "event_type"])
            )
        _atomic_write_parquet(merged, self.paths.bars)
        return merged

    def replace_bars(self, frame: pl.DataFrame) -> None:
        """Atomically replace a synthetic/test store with an exact fixture."""

        _atomic_write_parquet(frame, self.paths.bars)

    def load_daily(self) -> pl.DataFrame:
        return _read_parquet(self.paths.daily)

    def update_daily(self, incoming: pl.DataFrame) -> pl.DataFrame:
        existing = self.load_daily()
        frames = [frame for frame in (existing, incoming) if not frame.is_empty()]
        merged = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
        if not merged.is_empty():
            merged = (
                merged.filter(pl.col("session_date") >= self.config.system.daily_history_start)
                .unique(["session_date", "security"], keep="last")
                .sort(["session_date", "root", "delivery_month"])
            )
            _atomic_write_parquet(merged, self.paths.daily)
        return merged

    def replace_daily(self, frame: pl.DataFrame) -> None:
        """Atomically replace a synthetic/test daily fixture."""

        _atomic_write_parquet(frame, self.paths.daily)

    def write_contracts(self, registry: pl.DataFrame) -> None:
        _atomic_write_parquet(registry, self.paths.contracts)

    def load_contracts(self) -> pl.DataFrame:
        return _read_parquet(self.paths.contracts)

    def write_liquidity(self, frame: pl.DataFrame, *, l2: bool = False) -> None:
        if frame.is_empty():
            return
        path = self.paths.depth if l2 else self.paths.liquidity
        existing = _read_parquet(path)
        merged = pl.concat([item for item in (existing, frame) if not item.is_empty()], how="diagonal_relaxed")
        if "time" in merged.columns:
            merged = merged.sort("time").tail(250_000)
        else:
            merged = merged.tail(250_000)
        _atomic_write_parquet(merged, path)

    def load_liquidity(self, *, l2: bool = False) -> pl.DataFrame:
        return _read_parquet(self.paths.depth if l2 else self.paths.liquidity)


def package_depth_snapshot(
    features: pl.DataFrame,
    snapshot: pl.DataFrame,
    config: TechnicalConfig,
    *,
    depth_source: str,
) -> pl.DataFrame:
    """Convert current leg sizes into side-correct complete-package capacity."""

    if features.is_empty():
        return pl.DataFrame()
    latest = (
        features.sort("timestamp_utc")
        .group_by("spread_id", maintain_order=True)
        .tail(1)
    )
    normalized = _rename_columns(snapshot) if not snapshot.is_empty() else pl.DataFrame()
    security_col = _field_lookup(normalized.columns, "security", "ticker")
    bid_size_col = _field_lookup(normalized.columns, "bid_size", "bidsize")
    ask_size_col = _field_lookup(normalized.columns, "ask_size", "asksize")
    time_col = _field_lookup(normalized.columns, "time", "timestamp")
    size_by_security: dict[str, dict[str, object]] = {}
    if security_col and bid_size_col and ask_size_col:
        working = normalized.with_columns(
            pl.col(bid_size_col).cast(pl.Float64, strict=False),
            pl.col(ask_size_col).cast(pl.Float64, strict=False),
        )
        if time_col:
            latest_times = working.group_by(security_col).agg(
                pl.col(time_col).max().alias("_latest_snapshot_time")
            )
            working = working.join(latest_times, on=security_col, how="inner").filter(
                pl.col(time_col) == pl.col("_latest_snapshot_time")
            )
        aggregations: list[pl.Expr] = [
            pl.col(bid_size_col).drop_nulls().sum().alias("bid_size"),
            pl.col(ask_size_col).drop_nulls().sum().alias("ask_size"),
        ]
        if time_col:
            aggregations.append(pl.col(time_col).max().alias("snapshot_time"))
        for row in working.group_by(security_col).agg(aggregations).to_dicts():
            size_by_security[str(row[security_col])] = row
    spec_by_id = {item.spread_id: item for item in config.spreads}
    rows: list[dict[str, object]] = []
    for row in latest.to_dicts():
        spec = spec_by_id.get(str(row["spread_id"]))
        buy_capacities: list[float] = []
        sell_capacities: list[float] = []
        ages: list[float] = []
        if spec is not None:
            for index, leg in enumerate(spec.legs, start=1):
                security = str(row.get(f"leg{index}_security") or "")
                quote = size_by_security.get(security)
                if not quote:
                    continue
                bid_size = float(quote.get("bid_size") or 0.0)
                ask_size = float(quote.get("ask_size") or 0.0)
                buy_size = ask_size if leg.sign > 0 else bid_size
                sell_size = bid_size if leg.sign > 0 else ask_size
                buy_capacities.append(buy_size / leg.contracts)
                sell_capacities.append(sell_size / leg.contracts)
                snapshot_time = quote.get("snapshot_time")
                if isinstance(snapshot_time, datetime):
                    feature_time = row.get("timestamp_utc")
                    if isinstance(feature_time, datetime):
                        try:
                            ages.append(
                                max(
                                    0.0,
                                    (feature_time - snapshot_time).total_seconds() / 60.0,
                                )
                            )
                        except TypeError:
                            pass
        complete = spec is not None and len(buy_capacities) == len(spec.legs)
        buy_depth = min(buy_capacities) if complete else None
        sell_depth = min(sell_capacities) if complete else None
        age = max(ages) if ages and complete else None
        fresh = age is not None and age <= config.liquidity.depth_snapshot_max_age_minutes
        rows.append(
            {
                "spread_id": row["spread_id"],
                "depth_source": depth_source,
                "buy_package_depth": buy_depth,
                "sell_package_depth": sell_depth,
                "package_depth_imbalance": (
                    (buy_depth - sell_depth) / (buy_depth + sell_depth)
                    if None not in (buy_depth, sell_depth)
                    and (buy_depth + sell_depth) > 0
                    else None
                ),
                "depth_snapshot_age_minutes": age,
                "depth_fresh": fresh,
                "depth_supports_one_package": bool(
                    fresh
                    and buy_depth is not None
                    and sell_depth is not None
                    and min(buy_depth, sell_depth) >= 1.0
                ),
            }
        )
    return pl.DataFrame(rows).sort("spread_id")


def import_intraday_backfill(
    path: str | Path,
    definitions: Mapping[str, ContractDefinition],
    config: TechnicalConfig,
) -> pl.DataFrame:
    """Import a user/Data-License backfill in canonical or XBBG-like format."""

    source = Path(path)
    if not source.is_file():
        raise TechnicalDataError(f"Backfill not found: {source}")
    if source.suffix.lower() in {".parquet", ".pq"}:
        frame = pl.read_parquet(source)
    elif source.suffix.lower() in {".csv", ".gz"}:
        frame = pl.read_csv(source, try_parse_dates=True, infer_schema_length=10_000)
    else:
        raise TechnicalDataError("Backfill must be CSV, CSV.GZ, or Parquet")
    if set(BAR_COLUMNS).issubset(frame.columns):
        return frame.select(BAR_COLUMNS)
    frame = _rename_columns(frame)
    security_col = _field_lookup(frame.columns, "security", "ticker")
    event_col = _field_lookup(frame.columns, "event_type", "type")
    if not security_col:
        raise TechnicalDataError("Backfill requires a security/ticker column")
    pieces: list[pl.DataFrame] = []
    for security, group in frame.partition_by(security_col, as_dict=True).items():
        key = security[0] if isinstance(security, tuple) else security
        definition = definitions.get(str(key))
        if not definition:
            continue
        event = "TRADE"
        if event_col and group[event_col].drop_nulls().len():
            event = str(group[event_col].drop_nulls()[0]).upper()
        pieces.append(
            normalize_intraday_frame(
                group, definition, config, event_type=event, source="IMPORTED_BACKFILL"
            )
        )
    return pl.concat(pieces, how="vertical_relaxed") if pieces else _empty_bars()


def data_quality_report(
    bars: pl.DataFrame,
    contracts: pl.DataFrame,
    config: TechnicalConfig,
    *,
    daily: pl.DataFrame | None = None,
    depth_source: str = "UNKNOWN",
) -> pl.DataFrame:
    if bars.is_empty():
        return pl.DataFrame(
            [
                {
                    "check": "intraday_rows",
                    "status": "FAIL",
                    "actual": "0",
                    "expected": "at least one complete bar",
                    "blocking": True,
                    "notes": "No intraday data is available.",
                }
            ]
        )
    trade = bars.filter(pl.col("event_type") == "TRADE")
    session_count = trade.select(pl.col("session_date").n_unique()).item()
    earliest = trade["session_date"].min()
    latest = trade["session_date"].max()
    volume_coverage = trade.select(pl.col("volume_contracts").is_not_null().mean()).item()
    expected_per_security = config.system.complete_bars_per_session
    completeness = (
        trade.group_by(["security", "session_date"])
        .agg(pl.len().alias("bars"))
        .select((pl.col("bars") == expected_per_security).mean())
        .item()
    )
    verified_share = (
        contracts.select(pl.col("expiry_verified").mean()).item()
        if not contracts.is_empty() and "expiry_verified" in contracts.columns
        else 0.0
    )
    rolling_cutoff = add_months(
        date(latest.year, latest.month, 1),
        -config.system.rolling_intraday_months,
    )
    # A calendar cutoff can land on a weekend.  Treat the first normal session
    # immediately after it as complete rolling-year coverage.
    first_expected_session = rolling_cutoff
    while first_expected_session.weekday() >= 5:
        first_expected_session += timedelta(days=1)
    full_rolling_history = bool(earliest <= first_expected_session)
    latest_timestamp = trade["timestamp_utc"].max()
    current_curve = trade.filter(pl.col("timestamp_utc") == latest_timestamp)
    curve_counts = (
        current_curve.group_by("root").agg(pl.col("security").n_unique().alias("contracts"))
        if not current_curve.is_empty()
        else pl.DataFrame()
    )
    curve_min = (
        int(curve_counts["contracts"].min())
        if not curve_counts.is_empty() and curve_counts.height == len(config.roots)
        else 0
    )
    settle_coverage = 0.0
    settle_start: date | None = None
    first_expected_daily_session = config.system.daily_history_start
    while first_expected_daily_session.weekday() >= 5:
        first_expected_daily_session += timedelta(days=1)
    if daily is not None and not daily.is_empty():
        settle_start = daily["session_date"].min()
        if "settle_source_field" in daily.columns:
            settle_coverage = float(
                daily.select((pl.col("settle_source_field") == "PX_SETTLE").mean()).item()
            )
        elif "settle" in daily.columns:
            settle_coverage = float(daily.select(pl.col("settle").is_not_null().mean()).item())
    history_status = (
        "PASS"
        if session_count >= config.system.minimum_production_sessions
        else "WARN"
        if session_count >= config.system.minimum_preliminary_sessions
        else "FAIL"
    )
    rows = [
        {
            "check": "history_sessions",
            "status": history_status,
            "actual": str(session_count),
            "expected": f">={config.system.minimum_production_sessions} for production",
            "blocking": session_count < config.system.minimum_preliminary_sessions,
            "notes": (
                f"Coverage {earliest} through {latest}; "
                f"<{config.system.minimum_preliminary_sessions} sessions blocks signals."
            ),
        },
        {
            "check": "rolling_intraday_coverage",
            "status": "PASS" if full_rolling_history else "WARN",
            "actual": f"{earliest} through {latest}",
            "expected": f"start on or before first session near {rolling_cutoff}",
            "blocking": False,
            "notes": (
                f"Target is {config.system.rolling_intraday_months} calendar months. "
                "Standard Bloomberg intraday retention may require an imported "
                "historical backfill; the local Parquet store accumulates forward."
            ),
        },
        {
            "check": "forward_curve_contracts",
            "status": "PASS" if curve_min >= config.system.forward_curve_months else "WARN",
            "actual": str(curve_min),
            "expected": f">={config.system.forward_curve_months} synchronized contracts per root",
            "blocking": False,
            "notes": "Thin or missing far contracts remain analytical and fail liquidity gates.",
        },
        {
            "check": "daily_settle_history",
            "status": (
                "PASS"
                if settle_start is not None
                and settle_start <= first_expected_daily_session
                and settle_coverage >= 0.90
                else "WARN"
            ),
            "actual": (
                f"start {settle_start}; PX_SETTLE {settle_coverage:.1%}"
                if settle_start is not None
                else "missing"
            ),
            "expected": (
                f"start by first session near {config.system.daily_history_start}; "
                ">=90% PX_SETTLE with labeled fallback"
            ),
            "blocking": False,
            "notes": "PX_LAST fallback is retained and labeled; it is never silently mixed with settle.",
        },
        {
            "check": "complete_session_security_groups",
            "status": "PASS" if completeness >= 0.95 else "WARN",
            "actual": f"{completeness:.1%}",
            "expected": "95%",
            "blocking": completeness < 0.80,
            "notes": (
                f"A normal session contains "
                f"{config.system.complete_bars_per_session} complete "
                f"{config.system.bar_interval_minutes}-minute bars."
            ),
        },
        {
            "check": "volume_coverage",
            "status": "PASS" if volume_coverage >= config.liquidity.minimum_volume_coverage else "FAIL",
            "actual": f"{volume_coverage:.1%}",
            "expected": f"{config.liquidity.minimum_volume_coverage:.0%}",
            "blocking": volume_coverage < config.liquidity.minimum_volume_coverage,
            "notes": "Volume is a hard liquidity input.",
        },
        {
            "check": "verified_expiry_metadata",
            "status": "PASS" if verified_share >= 0.95 else "FAIL",
            "actual": f"{verified_share:.1%}",
            "expected": "95%",
            "blocking": verified_share < 0.95,
            "notes": "Unverified expiry dates fail closed for live entries.",
        },
        {
            "check": "market_depth_source",
            "status": "PASS" if depth_source == "BPIPE_L2" else "WARN",
            "actual": depth_source,
            "expected": "BPIPE_L2 preferred; top-of-book or bar proxy accepted with confidence cap",
            "blocking": bool(
                config.liquidity.require_true_l2_for_enter and depth_source != "BPIPE_L2"
            ),
            "notes": "Historical L2 depth is not present in standard BDIB bars.",
        },
    ]
    return pl.DataFrame(rows)


def frame_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        temp_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


__all__ = [
    "BAR_COLUMNS",
    "DataPaths",
    "PullStats",
    "TechnicalDataError",
    "TechnicalStore",
    "XbbgTechnicalClient",
    "XbbgUnavailableError",
    "data_quality_report",
    "frame_sha256",
    "import_intraday_backfill",
    "normalize_contract_registry",
    "normalize_daily_frame",
    "normalize_intraday_frame",
    "package_depth_snapshot",
    "write_manifest",
]
