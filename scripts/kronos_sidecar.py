#!/usr/bin/env python
"""Run optional pinned Kronos forecasts on real executable contract bars.

This script is intentionally isolated from the base Bloomberg environment. It
forecasts each current futures contract from its own real OHLCV bars; spread
forecasts are recombined later from registered leg price weights. It never
manufactures high/low values for asynchronous multi-leg structures.
"""

from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl
import torch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
VENDORED_KRONOS = PROJECT_ROOT / "vendor" / "kronos"
if str(VENDORED_KRONOS) not in sys.path:
    sys.path.insert(0, str(VENDORED_KRONOS))

from app.technical_config import exchange_session_offset, load_technical_config  # noqa: E402
from model import Kronos, KronosTokenizer  # noqa: E402
from model.kronos import auto_regressive_inference  # noqa: E402


NY = ZoneInfo("America/New_York")


def _future_timestamps(
    last_utc: datetime,
    root: str,
    count: int,
    interval_minutes: int,
) -> list[datetime]:
    current = last_utc.astimezone(NY)
    values: list[datetime] = []
    for _ in range(count):
        if current.timetz().replace(tzinfo=None) < time(14, 0):
            current += timedelta(minutes=interval_minutes)
        else:
            next_session = exchange_session_offset(current.date(), 1, root)
            current = datetime.combine(next_session, time(8, 0), tzinfo=NY)
        values.append(current)
    return values


def _time_features(values: list[datetime]) -> np.ndarray:
    return np.asarray(
        [
            [
                value.minute,
                value.hour,
                value.weekday(),
                value.day,
                value.month,
            ]
            for value in values
        ],
        dtype=np.float32,
    )


def _atomic_parquet(frame: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    os.close(descriptor)
    temp = Path(temp_name)
    try:
        frame.write_parquet(temp, compression="zstd", statistics=True)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "config" / "kronos.toml",
    )
    args = parser.parse_args()
    settings = tomllib.loads(args.config.read_text(encoding="utf-8"))["kronos"]
    if not bool(settings.get("enabled")) and not args.force:
        print("Kronos is disabled in config/kronos.toml; no forecast was produced.")
        return 0
    bars_path = args.bars.expanduser().resolve()
    if not bars_path.is_file():
        raise FileNotFoundError(f"Canonical bars not found: {bars_path}")
    config = load_technical_config(PROJECT_ROOT / "config" / "technical_system.toml")
    lookback = int(settings["lookback_bars"])
    max_context = int(settings["max_context"])
    pred_len = int(settings["prediction_bars"])
    batch_size = max(1, int(settings["batch_size"]))
    torch.set_num_threads(max(1, int(settings.get("cpu_threads") or 1)))

    bars = pl.read_parquet(bars_path).filter(pl.col("event_type") == "TRADE")
    if bars.is_empty():
        raise ValueError("Canonical bar store contains no TRADE bars")
    latest_session = bars["session_date"].max()
    current = (
        bars.filter(pl.col("session_date") == latest_session)
        .select("root", "security", "delivery_month")
        .unique()
        .sort(["root", "delivery_month", "security"])
        .group_by("root", maintain_order=True)
        .head(config.system.forward_curve_months + 2)
    )
    requested = set(str(value) for value in current["security"].to_list())
    contexts: list[np.ndarray] = []
    x_times: list[np.ndarray] = []
    y_times: list[np.ndarray] = []
    future_times: list[list[datetime]] = []
    metadata: list[dict[str, object]] = []
    for group in bars.filter(pl.col("security").is_in(requested)).partition_by(
        "security", maintain_order=True
    ):
        group = group.sort("timestamp_utc").drop_nulls(
            ["open", "high", "low", "close"]
        )
        if group.height < lookback:
            continue
        group = group.tail(lookback)
        last = group.tail(1).to_dicts()[0]
        frame = group.select(
            pl.col("open"),
            pl.col("high"),
            pl.col("low"),
            pl.col("close"),
            pl.col("volume_contracts").fill_null(0.0).alias("volume"),
            pl.coalesce(
                [
                    pl.col("value"),
                    pl.col("volume_contracts")
                    * pl.mean_horizontal("open", "high", "low", "close"),
                ]
            )
            .fill_null(0.0)
            .alias("amount"),
        ).to_numpy().astype(np.float32, copy=False)
        timestamps = [
            value.astimezone(NY) for value in group["timestamp_utc"].to_list()
        ]
        future = _future_timestamps(
            last["timestamp_utc"],
            str(last["root"]),
            pred_len,
            config.system.bar_interval_minutes,
        )
        contexts.append(frame)
        x_times.append(_time_features(timestamps))
        y_times.append(_time_features(future))
        future_times.append(future)
        metadata.append(
            {
                "root": str(last["root"]),
                "security": str(last["security"]),
                "delivery_month": last["delivery_month"],
                "forecast_origin_utc": last["timestamp_utc"],
                "last_open": float(last["open"]),
                "last_high": float(last["high"]),
                "last_low": float(last["low"]),
                "last_close": float(last["close"]),
            }
        )
    if not contexts:
        raise ValueError(
            f"No current contracts had the required {lookback} complete bars"
        )

    cache_root = args.cache_root.expanduser().resolve()
    tokenizer = KronosTokenizer.from_pretrained(
        str(settings["tokenizer_id"]),
        revision=str(settings["tokenizer_revision"]),
        cache_dir=str(cache_root),
        local_files_only=True,
    )
    model = Kronos.from_pretrained(
        str(settings["model_id"]),
        revision=str(settings["model_revision"]),
        cache_dir=str(cache_root),
        local_files_only=True,
    )
    if torch.cuda.is_available():
        device = "cuda:0"
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        device = "mps"
    else:
        device = "cpu"
    tokenizer = tokenizer.to(device)
    model = model.to(device)
    tokenizer.eval()
    model.eval()
    rows: list[dict[str, object]] = []
    for start in range(0, len(contexts), batch_size):
        stop = min(len(contexts), start + batch_size)
        raw = np.stack(contexts[start:stop], axis=0).astype(
            np.float32, copy=False
        )
        means = raw.mean(axis=1, keepdims=True)
        standard_deviations = raw.std(axis=1, keepdims=True)
        normalized = np.clip(
            (raw - means) / (standard_deviations + 1e-5), -5.0, 5.0
        )
        predictions = auto_regressive_inference(
            tokenizer,
            model,
            torch.from_numpy(normalized).to(device),
            torch.from_numpy(np.stack(x_times[start:stop], axis=0)).to(device),
            torch.from_numpy(np.stack(y_times[start:stop], axis=0)).to(device),
            max_context,
            pred_len,
            clip=5,
            T=float(settings["temperature"]),
            top_k=0,
            top_p=float(settings["top_p"]),
            sample_count=int(settings["sample_count"]),
            verbose=False,
        )[:, -pred_len:, :]
        predictions = predictions * (standard_deviations + 1e-5) + means
        for local_offset, predicted in enumerate(predictions):
            offset = start + local_offset
            meta = metadata[offset]
            for step, values in enumerate(predicted, start=1):
                forecast_time = future_times[offset][step - 1]
                rows.append(
                    {
                        **meta,
                        "forecast_step": step,
                        "forecast_timestamp_utc": forecast_time.astimezone(timezone.utc),
                        "predicted_open": float(values[0]),
                        "predicted_high": float(values[1]),
                        "predicted_low": float(values[2]),
                        "predicted_close": float(values[3]),
                        "predicted_volume": float(values[4]),
                        "predicted_close_move_native": float(values[3])
                        - float(meta["last_close"]),
                        "model_id": str(settings["model_id"]),
                        "model_revision": str(settings["model_revision"]),
                        "tokenizer_revision": str(settings["tokenizer_revision"]),
                        "inference_device": device,
                        "action_enabled": False,
                        "generated_at_utc": datetime.now(timezone.utc),
                    }
                )
    output = pl.DataFrame(rows, strict=False).sort(
        ["forecast_timestamp_utc", "root", "delivery_month", "security"]
    )
    output_path = args.output.expanduser().resolve()
    if output_path.is_file():
        existing = pl.read_parquet(output_path)
        output = (
            pl.concat([existing, output], how="diagonal_relaxed")
            .unique(
                ["security", "forecast_origin_utc", "forecast_step"],
                keep="last",
            )
            .sort(["forecast_timestamp_utc", "root", "delivery_month", "security"])
        )
    _atomic_parquet(output, output_path)
    receipt = {
        "status": "EXPERIMENTAL_NOT_ACTIONABLE",
        "rows": output.height,
        "contracts": output["security"].n_unique(),
        "latest_session": str(latest_session),
        "model_id": settings["model_id"],
        "model_revision": settings["model_revision"],
        "device": device,
        "output": str(output_path),
    }
    receipt_path = output_path.with_suffix(".json")
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
