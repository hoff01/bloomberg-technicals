"""Point-in-time spread construction and close-based Polars indicators.

Synthetic spread highs and lows are intentionally absent.  Fifteen-minute leg
extrema are asynchronous; subtracting them would invent prices that may never
have traded.  Range indicators are therefore replaced with close-to-close
volatility measures, while outright OHLC remains available in the raw store.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import math
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

from app.technical_config import RootSpec, SpreadLeg, SpreadSpec, TechnicalConfig
from app.technical_labels import (
    GASOIL_BBL_PER_MT,
    USD_BBL_TO_CPG_DIVISOR,
)


class TechnicalAnalyticsError(RuntimeError):
    """Raised when contracts cannot be aligned into a valid tradable structure."""


def _delivery_index_expr(column: str = "delivery_month") -> pl.Expr:
    return (
        pl.col(column).dt.year() * 12 + pl.col(column).dt.month() - 1
    ).cast(pl.Int32)


def _business_days_between(start: date, end: date) -> int:
    if end <= start:
        return 0
    return int(np.busday_count(start.isoformat(), end.isoformat()))


def _quote_table(bars: pl.DataFrame) -> pl.DataFrame:
    quotes = bars.filter(pl.col("event_type").is_in(["BID", "ASK"]))
    if quotes.is_empty():
        return pl.DataFrame()
    quote = (
        quotes.select("timestamp_utc", "security", "event_type", pl.col("close"))
        .pivot(on="event_type", index=["timestamp_utc", "security"], values="close", aggregate_function="last")
    )
    rename: dict[str, str] = {}
    if "BID" in quote.columns:
        rename["BID"] = "bid_close"
    if "ASK" in quote.columns:
        rename["ASK"] = "ask_close"
    quote = quote.rename(rename)
    for column in ("bid_close", "ask_close"):
        if column not in quote.columns:
            quote = quote.with_columns(pl.lit(None, dtype=pl.Float64).alias(column))
    return quote


def prepare_trade_bars(
    bars: pl.DataFrame,
    contracts: pl.DataFrame,
    daily: pl.DataFrame,
    config: TechnicalConfig,
) -> pl.DataFrame:
    """Attach quotes, lagged OI, expiry state, unit conversions, and eligible rank."""

    trade = bars.filter(pl.col("event_type") == "TRADE")
    if trade.is_empty():
        return pl.DataFrame()
    quote = _quote_table(bars)
    if not quote.is_empty():
        trade = trade.join(quote, on=["timestamp_utc", "security"], how="left")
    else:
        trade = trade.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("bid_close"),
            pl.lit(None, dtype=pl.Float64).alias("ask_close"),
        )

    contract_columns = [
        "root",
        "security",
        "delivery_month",
        "last_trade_date",
        "risk_date",
        "blackout_start",
        "forced_exit_session",
        "expiry_verified",
        "expiry_source",
    ]
    missing_contract_columns = sorted(set(contract_columns) - set(contracts.columns))
    if missing_contract_columns:
        raise TechnicalAnalyticsError(
            "Contract registry is missing: " + ", ".join(missing_contract_columns)
        )
    trade = trade.join(
        contracts.select(contract_columns),
        on=["root", "security", "delivery_month"],
        how="left",
    )

    if daily.is_empty():
        trade = trade.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("open_interest_lag1"),
            pl.lit(None, dtype=pl.Float64).alias("oi_change_5d"),
        )
    else:
        oi = (
            daily.sort(["security", "session_date"])
            .with_columns(
                pl.col("open_interest").shift(1).over("security").alias("open_interest_lag1"),
                (
                    pl.col("open_interest").shift(1)
                    - pl.col("open_interest").shift(6)
                )
                .over("security")
                .alias("oi_change_5d"),
            )
            .select("security", "session_date", "open_interest_lag1", "oi_change_5d")
        )
        trade = trade.join(oi, on=["security", "session_date"], how="left")

    root_meta = pl.DataFrame(
        [
            {
                "root": root.root,
                "price_to_usd_bbl": root.price_to_usd_bbl,
                "contract_barrels": root.contract_barrels,
                "contract_size_native": root.contract_size_native,
                "tick_size_native": root.tick_size_native,
                "one_way_cost_usd": root.one_way_cost_usd,
            }
            for root in config.roots.values()
        ]
    )
    trade = trade.join(root_meta, on="root", how="left").with_columns(
        _delivery_index_expr().alias("delivery_index"),
        (pl.col("close") * pl.col("price_to_usd_bbl")).alias("close_usd_bbl"),
        (pl.col("open") * pl.col("price_to_usd_bbl")).alias("open_usd_bbl"),
        (pl.col("volume_contracts") * pl.col("contract_barrels")).alias("volume_bbl"),
        (
            (pl.col("session_date") < pl.col("forced_exit_session"))
            & pl.col("expiry_verified").fill_null(False)
        ).alias("entry_contract_eligible"),
        (
            (pl.col("session_date") < pl.col("blackout_start"))
            & pl.col("expiry_verified").fill_null(False)
        ).alias("position_contract_eligible"),
    )

    # Retain the last common session before the T-3 blackout so an existing
    # position can be liquidated at the final eligible bar open. Ranking on entry-eligible
    # rows would remove that forced-exit bar and create a subtle expiry leak.
    eligible = trade.filter(pl.col("position_contract_eligible")).with_columns(
        pl.col("delivery_month")
        .rank(method="ordinal")
        .over(["root", "timestamp_utc"])
        .cast(pl.Int16)
        .alias("generic_rank")
    )
    return eligible.sort(["timestamp_utc", "root", "delivery_month"])


def _leg_columns(index: int) -> list[pl.Expr]:
    prefix = f"leg{index}_"
    source_columns = [
        "root",
        "security",
        "delivery_month",
        "delivery_index",
        "open",
        "close",
        "bid_close",
        "ask_close",
        "volume_contracts",
        "volume_bbl",
        "num_events",
        "value",
        "open_interest_lag1",
        "oi_change_5d",
        "risk_date",
        "blackout_start",
        "forced_exit_session",
        "expiry_verified",
        "expiry_source",
        "price_to_usd_bbl",
        "contract_barrels",
        "contract_size_native",
        "tick_size_native",
        "one_way_cost_usd",
    ]
    return [pl.col(column).alias(prefix + column) for column in source_columns]


def _back_adjust_group(frame: pl.DataFrame) -> pl.DataFrame:
    if frame.is_empty():
        return frame
    if frame["roll_id"].null_count():
        raise ValueError("roll_id cannot be null during continuous back-adjustment")
    group = "spread_id" if "spread_id" in frame.columns else None
    sort_columns = [column for column in (group, "timestamp_utc") if column]
    ordered = frame.sort(sort_columns)
    previous_roll = pl.col("roll_id").shift(1)
    previous_close = pl.col("spread_close").shift(1)
    if group:
        previous_roll = previous_roll.over(group)
        previous_close = previous_close.over(group)
    ordered = ordered.with_columns(
        (pl.col("roll_id") != previous_roll)
        .fill_null(False)
        .alias("roll_event")
    ).with_columns(
        pl.when(pl.col("roll_event"))
        .then(previous_close - pl.col("spread_close"))
        .otherwise(0.0)
        .alias("_roll_gap")
    )
    cumulative_gap = pl.col("_roll_gap").cum_sum()
    if group:
        cumulative_gap = cumulative_gap.over(group)
    ordered = ordered.with_columns(cumulative_gap.alias("_roll_adjustment"))
    final_adjustment = pl.col("_roll_adjustment").last()
    if group:
        final_adjustment = final_adjustment.over(group)
    # Anchor the entire continuous research history to the latest executable
    # contract scale. Indicators keep roll continuity, while current fair value
    # and entry targets remain directly comparable with the raw tradable quote.
    return ordered.with_columns(
        (
            pl.col("spread_close")
            + pl.col("_roll_adjustment")
            - final_adjustment
        ).alias("research_close")
    ).drop("_roll_gap", "_roll_adjustment")


def build_spread_bars(
    prepared: pl.DataFrame,
    config: TechnicalConfig,
    spreads: Sequence[SpreadSpec] | None = None,
) -> pl.DataFrame:
    """Build exact-timestamp, explicit-contract cracks and calendar structures."""

    if prepared.is_empty():
        return pl.DataFrame()
    pieces: list[pl.DataFrame] = []
    alignment_cache: dict[tuple[object, ...], pl.DataFrame] = {}
    prepared_by_root = {
        root: prepared.filter(pl.col("root") == root) for root in config.roots
    }
    def alignment_signature(spread: SpreadSpec) -> tuple[object, ...]:
        return (
            spread.anchor_root,
            tuple(
                (
                    leg.root,
                    leg.selection_mode,
                    leg.delivery_offset,
                    leg.rank if leg.selection_mode == "rank" else 0,
                )
                for leg in spread.legs
            ),
        )

    spread_sequence = list(spreads or config.spreads)
    spread_sequence.sort(key=lambda item: repr(alignment_signature(item)))
    for spread in spread_sequence:
        # Start from every still-position-eligible anchor month.  After all
        # required legs have been matched by delivery month, rank the complete
        # executable structures.  This makes HO1/HO2 and QS1/QS2 package ranks,
        # rather than unrelated generic ranks which may roll on different days.
        signature = alignment_signature(spread)
        aligned = alignment_cache.get(signature)
        if aligned is None:
            # Structures are sorted by signature, so retaining only the active
            # alignment keeps the M1-M16 build bounded in memory.
            alignment_cache.clear()
            anchor = prepared_by_root[spread.anchor_root].select(
                "timestamp_utc",
                "bar_start_et",
                "bar_end_et",
                "session_date",
                "bar_slot",
                pl.col("delivery_month").alias("anchor_delivery_month"),
                pl.col("delivery_index").alias("anchor_delivery_index"),
            )
            if anchor.is_empty():
                alignment_cache[signature] = pl.DataFrame()
                continue
            aligned = anchor
            for leg_index, leg in enumerate(spread.legs, start=1):
                source = prepared_by_root[leg.root]
                if leg.selection_mode == "rank":
                    source = source.filter(pl.col("generic_rank") == leg.rank).select(
                        "timestamp_utc", *_leg_columns(leg_index)
                    )
                    aligned = aligned.join(source, on="timestamp_utc", how="inner")
                else:
                    target_column = f"_leg{leg_index}_target_delivery_index"
                    aligned = aligned.with_columns(
                        (pl.col("anchor_delivery_index") + leg.delivery_offset).alias(
                            target_column
                        )
                    )
                    source = source.select("timestamp_utc", *_leg_columns(leg_index))
                    aligned = aligned.join(
                        source,
                        left_on=["timestamp_utc", target_column],
                        right_on=["timestamp_utc", f"leg{leg_index}_delivery_index"],
                        how="inner",
                    ).drop(target_column)
            if not aligned.is_empty():
                aligned = aligned.with_columns(
                    pl.col("anchor_delivery_month")
                    .rank(method="ordinal")
                    .over("timestamp_utc")
                    .cast(pl.Int16)
                    .alias("structure_rank")
                )
            alignment_cache[signature] = aligned
        if aligned.is_empty():
            continue
        combined = aligned.filter(pl.col("structure_rank") == spread.anchor_rank)
        if combined.is_empty():
            continue

        open_terms: list[pl.Expr] = []
        close_terms: list[pl.Expr] = []
        buy_quote_terms: list[pl.Expr] = []
        sell_quote_terms: list[pl.Expr] = []
        capacity_terms: list[pl.Expr] = []
        oi_capacity_terms: list[pl.Expr] = []
        cost_terms: list[pl.Expr] = []
        event_terms: list[pl.Expr] = []
        package_open_value_terms: list[pl.Expr] = []
        package_close_value_terms: list[pl.Expr] = []
        bar_vwap_terms: list[pl.Expr] = []
        bid_ask_tick_terms: list[pl.Expr] = []
        entry_eligibility_terms: list[pl.Expr] = []
        long_barrels = 0.0
        short_barrels = 0.0
        for leg_index, leg in enumerate(spread.legs, start=1):
            prefix = f"leg{leg_index}_"
            root = config.roots[leg.root]
            open_terms.append(
                leg.sign
                * leg.price_weight
                * pl.col(prefix + "open")
                * pl.col(prefix + "price_to_usd_bbl")
            )
            close_terms.append(
                leg.sign
                * leg.price_weight
                * pl.col(prefix + "close")
                * pl.col(prefix + "price_to_usd_bbl")
            )
            long_quote = pl.coalesce([pl.col(prefix + "ask_close"), pl.col(prefix + "close")])
            short_quote = pl.coalesce([pl.col(prefix + "bid_close"), pl.col(prefix + "close")])
            buy_leg_quote = long_quote if leg.sign > 0 else short_quote
            sell_leg_quote = short_quote if leg.sign > 0 else long_quote
            buy_quote_terms.append(
                leg.sign
                * leg.price_weight
                * buy_leg_quote
                * pl.col(prefix + "price_to_usd_bbl")
            )
            sell_quote_terms.append(
                leg.sign
                * leg.price_weight
                * sell_leg_quote
                * pl.col(prefix + "price_to_usd_bbl")
            )
            capacity_terms.append(pl.col(prefix + "volume_contracts") / leg.contracts)
            oi_capacity_terms.append(pl.col(prefix + "open_interest_lag1") / leg.contracts)
            cost_terms.append(pl.col(prefix + "one_way_cost_usd") * leg.contracts)
            event_terms.append(pl.col(prefix + "num_events"))
            package_open_value_terms.append(
                leg.sign
                * leg.contracts
                * pl.col(prefix + "open")
                * pl.col(prefix + "contract_size_native")
            )
            package_close_value_terms.append(
                leg.sign
                * leg.contracts
                * pl.col(prefix + "close")
                * pl.col(prefix + "contract_size_native")
            )
            native_bar_vwap = pl.when(
                (pl.col(prefix + "volume_contracts") > 0)
                & pl.col(prefix + "value").is_not_null()
            ).then(
                pl.col(prefix + "value") / pl.col(prefix + "volume_contracts")
            ).otherwise(pl.col(prefix + "close"))
            bar_vwap_terms.append(
                leg.sign
                * leg.price_weight
                * native_bar_vwap
                * pl.col(prefix + "price_to_usd_bbl")
            )
            bid_ask_tick_terms.append(
                pl.when(
                    pl.col(prefix + "bid_close").is_not_null()
                    & pl.col(prefix + "ask_close").is_not_null()
                )
                .then(
                    (pl.col(prefix + "ask_close") - pl.col(prefix + "bid_close"))
                    / pl.col(prefix + "tick_size_native")
                )
                .otherwise(None)
            )
            entry_eligibility_terms.append(
                pl.col(prefix + "expiry_verified").fill_null(False)
                & (pl.col("session_date") < pl.col(prefix + "forced_exit_session"))
            )
            barrels = leg.contracts * root.contract_barrels
            if leg.sign > 0:
                long_barrels += barrels
            else:
                short_barrels += barrels

        package_barrels = min(long_barrels, short_barrels) or max(long_barrels, short_barrels)
        first_ho_leg = next(
            (
                index
                for index, leg in enumerate(spread.legs, start=1)
                if leg.root == "HO"
            ),
            None,
        )
        first_qs_leg = next(
            (
                index
                for index, leg in enumerate(spread.legs, start=1)
                if leg.root == "QS"
            ),
            None,
        )
        economic_roots = {leg.root for leg in spread.legs}
        heating_oil_cpg = (
            100.0 * pl.col(f"leg{first_ho_leg}_close")
            if first_ho_leg is not None
            else pl.lit(None, dtype=pl.Float64)
        )
        gasoil_usd_bbl = (
            pl.col(f"leg{first_qs_leg}_close") / GASOIL_BBL_PER_MT
            if first_qs_leg is not None
            else pl.lit(None, dtype=pl.Float64)
        )
        earliest_risk = pl.min_horizontal(
            *[pl.col(f"leg{index}_risk_date") for index in range(1, len(spread.legs) + 1)]
        )
        earliest_blackout = pl.min_horizontal(
            *[pl.col(f"leg{index}_blackout_start") for index in range(1, len(spread.legs) + 1)]
        )
        earliest_forced_exit = pl.min_horizontal(
            *[pl.col(f"leg{index}_forced_exit_session") for index in range(1, len(spread.legs) + 1)]
        )
        all_verified = pl.all_horizontal(
            *[
                pl.col(f"leg{index}_expiry_verified").fill_null(False)
                for index in range(1, len(spread.legs) + 1)
            ]
        )
        roll_id = pl.concat_str(
            [pl.col(f"leg{index}_security") for index in range(1, len(spread.legs) + 1)],
            separator=" | ",
        )
        combined = combined.with_columns(
            pl.lit(spread.spread_id).alias("spread_id"),
            pl.lit(spread.display_name).alias("spread_name"),
            pl.lit(spread.family).alias("spread_family"),
            pl.lit("|".join(sorted(economic_roots))).alias(
                "economic_roots"
            ),
            pl.lit(spread.anchor_rank).cast(pl.Int8).alias("tenor_start"),
            pl.lit(
                spread.anchor_rank
                + max(leg.delivery_offset for leg in spread.legs)
            )
            .cast(pl.Int8)
            .alias("tenor_end"),
            pl.lit(
                "FRONT"
                if spread.anchor_rank <= 4
                else "MID"
                if spread.anchor_rank <= 8
                else "DEFERRED"
                if spread.anchor_rank <= 12
                else "FAR"
            ).alias("tenor_bucket"),
            pl.lit(spread.unit).alias("spread_unit"),
            pl.lit(spread.core).alias("core_spread"),
            pl.lit(spread.model_enabled).alias("model_enabled"),
            pl.lit(spread.complexity_tier).cast(pl.Int8).alias("complexity_tier"),
            pl.lit(spread.algebra_group).alias("algebra_group"),
            pl.lit(config.system.complete_bars_per_session - 1)
            .cast(pl.Int16)
            .alias("session_last_slot"),
            pl.sum_horizontal(*open_terms).alias("spread_open"),
            pl.sum_horizontal(*close_terms).alias("spread_close"),
            pl.sum_horizontal(*buy_quote_terms).alias("spread_buy_quote"),
            pl.sum_horizontal(*sell_quote_terms).alias("spread_sell_quote"),
            pl.min_horizontal(*capacity_terms).alias("package_volume_capacity"),
            pl.min_horizontal(*oi_capacity_terms).alias("package_oi_capacity"),
            pl.sum_horizontal(*cost_terms).alias("one_way_cost_usd"),
            pl.min_horizontal(*event_terms).alias("min_leg_events"),
            pl.sum_horizontal(*event_terms).alias("total_leg_events"),
            pl.sum_horizontal(*package_open_value_terms).alias("package_open_value_usd"),
            pl.sum_horizontal(*package_close_value_terms).alias("package_close_value_usd"),
            pl.sum_horizontal(*bar_vwap_terms).alias("spread_bar_vwap"),
            pl.max_horizontal(*bid_ask_tick_terms).alias("max_leg_bid_ask_ticks"),
            pl.lit(package_barrels).alias("package_barrels"),
            earliest_risk.alias("earliest_risk_date"),
            earliest_blackout.alias("blackout_start"),
            earliest_forced_exit.alias("forced_exit_session"),
            all_verified.alias("expiry_verified"),
            roll_id.alias("roll_id"),
        ).with_columns(
            (pl.col("spread_buy_quote") - pl.col("spread_sell_quote")).alias("quoted_width_usd_bbl"),
            heating_oil_cpg.alias("heating_oil_cpg"),
            (
                pl.col(f"leg{first_qs_leg}_close")
                if first_qs_leg is not None
                else pl.lit(None, dtype=pl.Float64)
            ).alias("gasoil_usd_mt"),
            gasoil_usd_bbl.alias("gasoil_usd_bbl"),
            (gasoil_usd_bbl / USD_BBL_TO_CPG_DIVISOR).alias("gasoil_cpg"),
            pl.when(pl.lit(economic_roots == {"HO", "QS"}))
            .then(pl.col("spread_close") / USD_BBL_TO_CPG_DIVISOR)
            .otherwise(None)
            .alias("hogo_cpg"),
            (pl.col("package_volume_capacity") * pl.col("package_barrels")).alias("paired_volume_bbl"),
            (pl.col("package_oi_capacity") * pl.col("package_barrels")).alias("paired_open_interest_bbl"),
            pl.all_horizontal(*entry_eligibility_terms).alias("entry_allowed"),
            pl.when(
                pl.lit(any(leg.root == "QS" for leg in spread.legs))
                & pl.lit(any(leg.root == "CO" for leg in spread.legs))
            )
            .then(pl.lit("CROSS_MARKET_ASYNC_SETTLE"))
            .when(pl.lit(spread.anchor_rank > 6))
            .then(pl.lit("DEFERRED_SETTLE_RELATIONSHIP"))
            .otherwise(pl.lit("EXCHANGE_SETTLE"))
            .alias("settlement_quality"),
        )
        def retain_leg_column(column: str) -> bool:
            if not column.startswith("leg") or "_" not in column:
                return True
            prefix, suffix = column.split("_", 1)
            if suffix in {"security", "delivery_month"}:
                return True
            try:
                leg_number = int(prefix[3:])
            except ValueError:
                return False
            return leg_number <= 2 and suffix in {
                "close",
                "volume_contracts",
                "price_to_usd_bbl",
            }

        drop_leg_columns = [
            column for column in combined.columns if not retain_leg_column(column)
        ]
        if drop_leg_columns:
            combined = combined.drop(drop_leg_columns)
        pieces.append(combined)

    if not pieces:
        return pl.DataFrame()
    result = pl.concat(pieces, how="diagonal_relaxed").sort(["spread_id", "timestamp_utc"])
    result = _back_adjust_group(result).sort(["spread_id", "timestamp_utc"])
    result = result.with_columns(
        pl.col("spread_close").shift(1).over("spread_id").alias("previous_close"),
        pl.col("session_date").shift(1).over("spread_id").alias("previous_session_date"),
        pl.col("roll_id").shift(1).over("spread_id").alias("previous_roll_id"),
    ).with_columns(
        pl.when(~pl.col("roll_event"))
        .then(pl.col("spread_close") - pl.col("previous_close"))
        .otherwise(None)
        .alias("price_change"),
        pl.when(
            (~pl.col("roll_event"))
            & (pl.col("session_date") == pl.col("previous_session_date"))
        )
        .then(pl.col("spread_close") - pl.col("previous_close"))
        .otherwise(None)
        .alias("intraday_change"),
        pl.when(
            (~pl.col("roll_event"))
            & (pl.col("session_date") != pl.col("previous_session_date"))
        )
        .then(pl.col("spread_close") - pl.col("previous_close"))
        .otherwise(None)
        .alias("overnight_change"),
        pl.when(pl.col("earliest_risk_date") <= pl.col("session_date"))
        .then(pl.lit(0, dtype=pl.Int32))
        .otherwise(
            pl.business_day_count("session_date", "earliest_risk_date").cast(
                pl.Int32
            )
        )
        .alias("sessions_to_risk_date"),
    )
    result = result.drop(
        "previous_close",
        "previous_session_date",
        "previous_roll_id",
        "spread_buy_quote",
        "spread_sell_quote",
        "total_leg_events",
    )
    categorical_columns = [
        column
        for column in (
            "spread_name",
            "spread_family",
            "economic_roots",
            "tenor_bucket",
            "spread_unit",
            "algebra_group",
            "roll_id",
            "settlement_quality",
            *[
                name
                for index in range(1, 9)
                for name in (f"leg{index}_security",)
            ],
        )
        if column in result.columns
    ]
    if categorical_columns:
        result = result.with_columns(
            [pl.col(column).cast(pl.Categorical) for column in categorical_columns]
        )
    return result


def daily_to_prepared_bars(
    daily: pl.DataFrame, contracts: pl.DataFrame, config: TechnicalConfig
) -> pl.DataFrame:
    if daily.is_empty():
        return pl.DataFrame()
    tz = ZoneInfo(config.system.timezone)
    rows = daily.to_dicts()
    interval = timedelta(minutes=config.system.bar_interval_minutes)
    timestamps = [
        datetime.combine(
            row["session_date"], config.system.session_end, tzinfo=tz
        )
        - interval
        for row in rows
    ]
    pseudo = pl.DataFrame(
        {
            "timestamp_utc": [item.astimezone(ZoneInfo("UTC")) for item in timestamps],
            "bar_start_et": timestamps,
            "bar_end_et": [item + interval for item in timestamps],
            "session_date": [row["session_date"] for row in rows],
            "bar_slot": [config.system.complete_bars_per_session - 1] * len(rows),
            "event_type": ["TRADE"] * len(rows),
            "root": [row["root"] for row in rows],
            "security": [row["security"] for row in rows],
            "delivery_month": [row["delivery_month"] for row in rows],
            "open": [row["close"] for row in rows],
            "high": [row["close"] for row in rows],
            "low": [row["close"] for row in rows],
            "close": [row["close"] for row in rows],
            "volume_contracts": [row.get("volume_contracts") for row in rows],
            "num_events": [None] * len(rows),
            "value": [None] * len(rows),
            "source": [row.get("source", "DAILY") for row in rows],
            "pulled_at_utc": [datetime.now(ZoneInfo("UTC"))] * len(rows),
        }
    )
    return prepare_trade_bars(pseudo, contracts, daily, config)


def build_seasonality_table(
    daily_spreads: pl.DataFrame,
    config: TechnicalConfig | None = None,
    window_days: int | None = None,
    target_years: Iterable[int] | None = None,
) -> pl.DataFrame:
    """Build a past-years-only, shrunk seasonal prior.

    Raw historical levels remain available as a diagnostic, but the actionable
    feature is the expected *daily package move*.  That avoids treating a 2022
    crack level as fair value in a different outright-price regime.  Expiry-
    relative samples are estimated separately and all thin cells shrink toward
    a zero-move prior.
    """

    if daily_spreads.is_empty():
        return pl.DataFrame()
    ind = config.indicators if config is not None else None
    calendar_window = int(
        window_days
        if window_days is not None
        else ind.seasonality_window_days
        if ind is not None
        else 14
    )
    expiry_window = int(ind.expiry_seasonality_window_sessions if ind else 5)
    shrinkage = float(ind.seasonality_shrinkage if ind else 16.0)
    minimum_years = int(ind.seasonality_min_prior_years if ind else 1)
    columns = ["spread_id", "session_date", "spread_close"]
    for optional in ("price_change", "sessions_to_risk_date", "settlement_quality"):
        if optional in daily_spreads.columns:
            columns.append(optional)
    compact = daily_spreads.select(columns).sort(["spread_id", "session_date"])
    if "price_change" not in compact.columns:
        compact = compact.with_columns(
            pl.col("spread_close").diff().over("spread_id").alias("price_change")
        )
    if "sessions_to_risk_date" not in compact.columns:
        compact = compact.with_columns(
            pl.lit(None, dtype=pl.Int32).alias("sessions_to_risk_date")
        )
    if "settlement_quality" not in compact.columns:
        compact = compact.with_columns(
            pl.lit("UNKNOWN").alias("settlement_quality")
        )
    compact = compact.with_columns(
        pl.col("session_date").dt.year().alias("asof_year"),
        pl.col("session_date").dt.ordinal_day().alias("day_of_year"),
    )

    def collect(
        buckets: Mapping[int, list[float]], center: int, radius: int, circular: bool
    ) -> list[float]:
        result: list[float] = []
        for offset in range(-radius, radius + 1):
            key = center + offset
            if circular:
                key = ((key - 1) % 366) + 1
            result.extend(buckets.get(key, ()))
        return result

    rows: list[dict[str, object]] = []
    requested_years = (
        {int(value) for value in target_years} if target_years is not None else None
    )
    for spread_frame in compact.partition_by("spread_id", maintain_order=True):
        spread_id = str(spread_frame["spread_id"][0])
        values = spread_frame.to_dicts()
        years = sorted({int(item["asof_year"]) for item in values})
        for asof_year in years:
            if requested_years is not None and asof_year not in requested_years:
                continue
            history = [item for item in values if int(item["asof_year"]) < asof_year]
            targets = [item for item in values if int(item["asof_year"]) == asof_year]
            if not history or not targets:
                continue
            prior_years = len({int(item["asof_year"]) for item in history})
            level_by_day: dict[int, list[float]] = {}
            move_by_day: dict[int, list[float]] = {}
            move_by_expiry: dict[int, list[float]] = {}
            for item in history:
                day_key = int(item["day_of_year"])
                if item.get("spread_close") is not None:
                    level_by_day.setdefault(day_key, []).append(float(item["spread_close"]))
                if item.get("price_change") is not None:
                    move = float(item["price_change"])
                    if math.isfinite(move):
                        move_by_day.setdefault(day_key, []).append(move)
                        if item.get("sessions_to_risk_date") is not None:
                            move_by_expiry.setdefault(
                                int(item["sessions_to_risk_date"]), []
                            ).append(move)
            expiry_median_cache: dict[int, tuple[float | None, int]] = {}
            for target in targets:
                day_of_year = int(target["day_of_year"])
                level_sample = collect(
                    level_by_day, day_of_year, calendar_window, circular=True
                )
                move_sample = collect(
                    move_by_day, day_of_year, calendar_window, circular=True
                )
                dte = target.get("sessions_to_risk_date")
                expiry_median: float | None = None
                expiry_sample_size = 0
                if dte is not None:
                    dte_key = int(dte)
                    cached_expiry = expiry_median_cache.get(dte_key)
                    if cached_expiry is None:
                        expiry_sample = collect(
                            move_by_expiry,
                            dte_key,
                            expiry_window,
                            circular=False,
                        )
                        expiry_array = np.asarray(expiry_sample, dtype=float)
                        cached_expiry = (
                            float(np.median(expiry_array))
                            if expiry_array.size
                            else None,
                            int(expiry_array.size),
                        )
                        expiry_median_cache[dte_key] = cached_expiry
                    expiry_median, expiry_sample_size = cached_expiry
                if len(level_sample) < 8 and len(move_sample) < 8:
                    continue
                level_array = np.asarray(level_sample, dtype=float)
                move_array = np.asarray(move_sample, dtype=float)
                level_quantiles = (
                    np.quantile(level_array, (0.25, 0.50, 0.75))
                    if level_array.size
                    else None
                )
                move_quantiles = (
                    np.quantile(move_array, (0.25, 0.50, 0.75))
                    if move_array.size
                    else None
                )
                effective_n = len(move_array)
                support = min(prior_years / 8.0, 1.0) * (
                    effective_n / (effective_n + shrinkage) if effective_n else 0.0
                )
                level_median = (
                    float(level_quantiles[1])
                    if level_quantiles is not None
                    else None
                )
                rows.append(
                    {
                        "spread_id": spread_id,
                        "asof_year": asof_year,
                        "day_of_year": day_of_year,
                        "seasonal_median": level_median,
                        "seasonal_q25": (
                            float(level_quantiles[0])
                            if level_quantiles is not None
                            else None
                        ),
                        "seasonal_q75": (
                            float(level_quantiles[2])
                            if level_quantiles is not None
                            else None
                        ),
                        "seasonal_n": len(level_array),
                        "seasonal_prior_years": prior_years,
                        "seasonal_move_1d": (
                            support * float(move_quantiles[1])
                            if move_quantiles is not None
                            else None
                        ),
                        "seasonal_move_q25": (
                            float(move_quantiles[0])
                            if move_quantiles is not None
                            else None
                        ),
                        "seasonal_move_q75": (
                            float(move_quantiles[2])
                            if move_quantiles is not None
                            else None
                        ),
                        "expiry_seasonal_move_1d": (
                            support * expiry_median
                            if expiry_median is not None
                            else None
                        ),
                        "expiry_seasonal_n": expiry_sample_size,
                        "seasonal_support": support,
                        "seasonal_status": (
                            "PRODUCTION_PRIOR"
                            if prior_years >= minimum_years
                            else "PRELIMINARY_PRIOR"
                        ),
                        "daily_settlement_quality": target.get("settlement_quality"),
                    }
                )
    return pl.DataFrame(rows) if rows else pl.DataFrame()


def _rsi_expr(column: str, window: int) -> pl.Expr:
    change = pl.col(column).diff().over("spread_id")
    gain = pl.when(change > 0).then(change).otherwise(0.0)
    loss = pl.when(change < 0).then(-change).otherwise(0.0)
    average_gain = gain.ewm_mean(alpha=1.0 / window, adjust=False, min_samples=window).over(
        "spread_id"
    )
    average_loss = loss.ewm_mean(alpha=1.0 / window, adjust=False, min_samples=window).over(
        "spread_id"
    )
    return (
        pl.when(average_loss == 0)
        .then(100.0)
        .otherwise(100.0 - 100.0 / (1.0 + average_gain / average_loss))
    )


def _add_causal_risk_indicators(
    result: pl.DataFrame, ind: object
) -> pl.DataFrame:
    """Add compact causal tail, liquidity, shape, and path diagnostics.

    Every normalization baseline is backward-looking.  Time-of-day baselines
    are shifted before their same-slot rolling window, and no expanding Python
    UDFs are used.  Undefined zero-variance ratios remain null unless both the
    numerator and denominator are zero, in which case the neutral diagnostic is
    explicit.  This keeps mature flat-price fixtures finite without inventing a
    shock estimate after a zero-volatility history.
    """

    epsilon = 1e-12
    one_session = int(ind.one_session)
    five_sessions = int(ind.five_sessions)
    twenty_sessions = int(ind.twenty_sessions)
    same_slot_sessions = 20
    same_slot_minimum = 8
    shape_minimum = int(ind.three_sessions)
    hac_window = int(ind.three_sessions)
    # Preserve the prior 90-minute Newey-West lag horizon when bar frequency
    # changes (3 lags at 30 minutes, 6 at 15 minutes).
    hac_lags = max(1, int(ind.one_session) // 4)
    path_changes = five_sessions - 1

    result = result.with_columns(
        pl.col("price_change").abs().alias("_risk_abs_change"),
        pl.col("price_change").pow(2).alias("_risk_change_sq"),
        pl.col("package_volume_capacity")
        .clip(lower_bound=0.0)
        .log1p()
        .alias("_risk_log_capacity"),
        *[
            pl.col("price_change")
            .shift(lag)
            .over("spread_id")
            .alias(f"_hac_change_lag{lag}")
            for lag in range(1, hac_lags + 1)
        ],
        *[
            (
                pl.col("price_change")
                * pl.col("price_change").shift(lag).over("spread_id")
            ).alias(f"_hac_product_lag{lag}")
            for lag in range(1, hac_lags + 1)
        ],
    ).with_columns(
        pl.col("_risk_abs_change")
        .shift(1)
        .rolling_median(same_slot_sessions, min_samples=same_slot_minimum)
        .over(["spread_id", "bar_slot"])
        .alias("_risk_tod_abs_change_median"),
        pl.col("_risk_log_capacity")
        .shift(1)
        .rolling_median(same_slot_sessions, min_samples=same_slot_minimum)
        .over(["spread_id", "bar_slot"])
        .alias("_risk_tod_volume_median"),
        pl.col("amihud_impact_proxy")
        .shift(1)
        .rolling_median(same_slot_sessions, min_samples=same_slot_minimum)
        .over(["spread_id", "bar_slot"])
        .alias("_risk_tod_impact_median"),
        pl.col("price_change")
        .rolling_std(one_session, min_samples=8)
        .over("spread_id")
        .alias("_risk_vol_1d"),
        pl.col("price_change")
        .rolling_std(twenty_sessions, min_samples=int(ind.ten_sessions))
        .over("spread_id")
        .alias("_risk_vol_20d"),
        pl.col("_risk_change_sq")
        .rolling_mean(one_session, min_samples=8)
        .over("spread_id")
        .sqrt()
        .alias("_risk_rms_1d"),
        pl.col("price_change")
        .rolling_mean(hac_window, min_samples=hac_window)
        .over("spread_id")
        .alias("_hac_mean"),
        pl.col("_risk_change_sq")
        .rolling_mean(hac_window, min_samples=hac_window)
        .over("spread_id")
        .alias("_hac_second_moment"),
        *[
            pl.col(f"_hac_product_lag{lag}")
            .rolling_sum(
                hac_window - lag,
                min_samples=hac_window - lag,
            )
            .over("spread_id")
            .alias(f"_hac_product_sum{lag}")
            for lag in range(1, hac_lags + 1)
        ],
        *[
            pl.col(f"_hac_change_lag{lag}")
            .rolling_sum(
                hac_window - lag,
                min_samples=hac_window - lag,
            )
            .over("spread_id")
            .alias(f"_hac_lag_sum{lag}")
            for lag in range(1, hac_lags + 1)
        ],
        *[
            pl.col("price_change")
            .rolling_sum(
                hac_window - lag,
                min_samples=hac_window - lag,
            )
            .over("spread_id")
            .alias(f"_hac_current_sum{lag}")
            for lag in range(1, hac_lags + 1)
        ],
        pl.when(pl.col("price_change").is_not_null())
        .then(1.0)
        .otherwise(None)
        .rolling_sum(hac_window, min_samples=hac_window)
        .over("spread_id")
        .alias("_hac_observations"),
        pl.col("price_change")
        .abs()
        .rolling_sum(path_changes, min_samples=path_changes)
        .over("spread_id")
        .alias("_risk_path_length"),
        (
            pl.col("research_close")
            .rolling_max(five_sessions, min_samples=five_sessions)
            .over("spread_id")
            - pl.col("research_close")
            .rolling_min(five_sessions, min_samples=five_sessions)
            .over("spread_id")
        ).alias("_risk_path_range"),
        pl.col("price_change")
        .rolling_skew(
            five_sessions,
            bias=False,
            min_samples=shape_minimum,
        )
        .over("spread_id")
        .fill_nan(None)
        .alias("return_skew_5d"),
        pl.col("price_change")
        .rolling_kurtosis(
            five_sessions,
            fisher=True,
            bias=False,
            min_samples=shape_minimum,
        )
        .over("spread_id")
        .fill_nan(None)
        .alias("return_excess_kurtosis_5d"),
    ).with_columns(
        (
            pl.col("_risk_log_capacity") - pl.col("_risk_tod_volume_median")
        )
        .abs()
        .alias("_risk_volume_abs_deviation"),
        (1.4826 * pl.col("_risk_tod_abs_change_median")).alias(
            "_risk_tod_change_scale"
        ),
        pl.col("_risk_rms_1d")
        .rolling_mean(five_sessions, min_samples=shape_minimum)
        .over("spread_id")
        .alias("_risk_rms_mean_5d"),
        pl.col("_risk_rms_1d")
        .rolling_std(five_sessions, min_samples=shape_minimum)
        .over("spread_id")
        .alias("_risk_rms_std_5d"),
        (pl.col("_hac_second_moment") - pl.col("_hac_mean").pow(2))
        .clip(lower_bound=0.0)
        .alias("_hac_gamma0"),
        *[
            (
                (
                    pl.col(f"_hac_product_sum{lag}")
                    - pl.col("_hac_mean")
                    * (
                        pl.col(f"_hac_current_sum{lag}")
                        + pl.col(f"_hac_lag_sum{lag}")
                    )
                    + (hac_window - lag) * pl.col("_hac_mean").pow(2)
                )
                / hac_window
            ).alias(f"_hac_gamma{lag}")
            for lag in range(1, hac_lags + 1)
        ],
    ).with_columns(
        pl.col("_risk_volume_abs_deviation")
        .shift(1)
        .rolling_median(same_slot_sessions, min_samples=same_slot_minimum)
        .over(["spread_id", "bar_slot"])
        .alias("_risk_tod_volume_forecast_error_median"),
        (
            pl.col("_hac_gamma0")
            + 2.0
            * sum(
                (1.0 - lag / (hac_lags + 1.0)) * pl.col(f"_hac_gamma{lag}")
                for lag in range(1, hac_lags + 1)
            )
        ).alias("_hac_long_run_variance"),
    ).with_columns(
        pl.when(pl.col("_risk_tod_change_scale") > epsilon)
        .then(pl.col("price_change") / pl.col("_risk_tod_change_scale"))
        .when(pl.col("price_change").abs() <= epsilon)
        .then(0.0)
        .otherwise(None)
        .alias("tod_normalized_change"),
        pl.when(pl.col("_risk_vol_20d") > epsilon)
        .then(pl.col("_risk_vol_1d") / pl.col("_risk_vol_20d"))
        .when(pl.col("_risk_vol_1d").abs() <= epsilon)
        .then(1.0)
        .otherwise(None)
        .alias("vol_regime_ratio_1d_20d"),
        pl.when(pl.col("_risk_tod_impact_median") > epsilon)
        .then(pl.col("amihud_impact_proxy") / pl.col("_risk_tod_impact_median"))
        .when(pl.col("amihud_impact_proxy").abs() <= epsilon)
        .then(1.0)
        .otherwise(None)
        .alias("liquidity_stress_ratio"),
        pl.when(
            (1.4826 * pl.col("_risk_tod_volume_forecast_error_median"))
            > epsilon
        )
        .then(
            (pl.col("_risk_log_capacity") - pl.col("_risk_tod_volume_median"))
            / (
                1.4826
                * pl.col("_risk_tod_volume_forecast_error_median")
            )
        )
        .when(
            (
                pl.col("_risk_log_capacity") - pl.col("_risk_tod_volume_median")
            ).abs()
            <= epsilon
        )
        .then(0.0)
        .otherwise(None)
        .alias("robust_volume_surprise"),
        pl.when(pl.col("_risk_rms_mean_5d") > epsilon)
        .then(pl.col("_risk_rms_std_5d") / pl.col("_risk_rms_mean_5d"))
        .when(pl.col("_risk_rms_std_5d").abs() <= epsilon)
        .then(0.0)
        .otherwise(None)
        .alias("realized_vol_of_vol_5d"),
        pl.when(
            (pl.col("_hac_observations") >= hac_window)
            & (pl.col("_hac_long_run_variance") > epsilon)
        )
        .then(
            pl.col("_hac_observations").sqrt()
            * pl.col("_hac_mean")
            / pl.col("_hac_long_run_variance").sqrt()
        )
        .when(
            (pl.col("_hac_observations") >= hac_window)
            & (pl.col("_hac_mean").abs() <= epsilon)
        )
        .then(0.0)
        .otherwise(None)
        .clip(-12.0, 12.0)
        .alias("trend_hac_t_stat_3d"),
        pl.when(
            (pl.col("_risk_path_length") > epsilon)
            & (pl.col("_risk_path_range") > epsilon)
        )
        .then(
            (
                100.0
                * (pl.col("_risk_path_length") / pl.col("_risk_path_range")).log()
                / math.log(path_changes)
            ).clip(0.0, 100.0)
        )
        .when(pl.col("_risk_path_length").abs() <= epsilon)
        .then(0.0)
        .otherwise(None)
        .alias("close_path_choppiness_5d"),
    ).with_columns(
        (pl.col("tod_normalized_change").abs() >= float(ind.tail_event_z))
        .cast(pl.Float64)
        .rolling_mean(
            twenty_sessions,
            min_samples=int(ind.ten_sessions),
        )
        .over("spread_id")
        .alias("tail_event_rate_20d")
    )

    numeric_outputs = (
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
    )
    result = result.with_columns(
        [
            pl.when(pl.col(column).is_finite())
            .then(pl.col(column))
            .otherwise(None)
            .cast(pl.Float32)
            .alias(column)
            for column in numeric_outputs
        ]
    )
    result = result.with_columns(
        pl.when(
            pl.any_horizontal(
                pl.col("tod_normalized_change").is_null(),
                pl.col("tail_event_rate_20d").is_null(),
                pl.col("vol_regime_ratio_1d_20d").is_null(),
                pl.col("liquidity_stress_ratio").is_null(),
                pl.col("robust_volume_surprise").is_null(),
            )
        )
        .then(pl.lit("WARMUP"))
        .when(
            pl.sum_horizontal(
                (pl.col("tod_normalized_change").abs() >= float(ind.extreme_tod_shock_z)).cast(pl.Int8),
                (pl.col("tail_event_rate_20d") >= float(ind.tail_cluster_rate)).cast(pl.Int8),
                (pl.col("vol_regime_ratio_1d_20d") >= float(ind.vol_expansion_ratio)).cast(pl.Int8),
                (pl.col("liquidity_stress_ratio") >= float(ind.impact_stress_ratio)).cast(pl.Int8),
                (pl.col("robust_volume_surprise") <= float(ind.volume_dryness_z)).cast(pl.Int8),
            )
            >= 2
        )
        .then(pl.lit("COMPOUND_STRESS"))
        .when(pl.col("tod_normalized_change").abs() >= float(ind.extreme_tod_shock_z))
        .then(pl.lit("EXTREME_TOD_SHOCK"))
        .when(pl.col("tail_event_rate_20d") >= float(ind.tail_cluster_rate))
        .then(pl.lit("TAIL_CLUSTER"))
        .when(pl.col("liquidity_stress_ratio") >= float(ind.impact_stress_ratio))
        .then(pl.lit("IMPACT_STRESS"))
        .when(pl.col("robust_volume_surprise") <= float(ind.volume_dryness_z))
        .then(pl.lit("VOLUME_DRYNESS"))
        .when(pl.col("vol_regime_ratio_1d_20d") >= float(ind.vol_expansion_ratio))
        .then(pl.lit("VOL_EXPANSION"))
        .otherwise(pl.lit("NORMAL"))
        .alias("advanced_risk_regime")
    )

    temporary_columns = [
        column
        for column in result.columns
        if column.startswith("_risk_") or column.startswith("_hac_")
    ]
    return result.drop(temporary_columns)


def _add_advanced_indicators(result: pl.DataFrame, ind: object) -> pl.DataFrame:
    """Add causal relationship, path-complexity, shock, and flow diagnostics."""

    advanced_window = int(ind.advanced_window)
    minimum_advanced = max(int(ind.three_sessions), advanced_window // 2)
    vr_lag = int(ind.variance_ratio_lag)
    hurst_lag = int(ind.hurst_lag)
    multi_leg = (
        pl.col("leg3_security").is_not_null()
        if "leg3_security" in result.columns
        else pl.lit(False)
    )
    result = result.with_columns(
        (
            pl.col("research_close")
            - pl.col("research_close").shift(vr_lag).over("spread_id")
        ).alias("_delta_vr"),
        (
            pl.col("research_close")
            - pl.col("research_close").shift(hurst_lag).over("spread_id")
        ).alias("_delta_hurst"),
        pl.col("price_change").shift(1).over("spread_id").alias("_change_lag1"),
        pl.col("price_change").shift(2).over("spread_id").alias("_change_lag2"),
        (pl.col("leg1_close") * pl.col("leg1_price_to_usd_bbl")).alias(
            "_leg1_usd_bbl"
        ),
        (pl.col("leg2_close") * pl.col("leg2_price_to_usd_bbl")).alias(
            "_leg2_usd_bbl"
        ),
    ).with_columns(
        pl.col("_leg1_usd_bbl").diff().over("spread_id").alias("_leg1_change"),
        pl.col("_leg2_usd_bbl").diff().over("spread_id").alias("_leg2_change"),
        pl.when(
            (pl.col("_change_lag2") <= pl.col("_change_lag1"))
            & (pl.col("_change_lag1") <= pl.col("price_change"))
        )
        .then(0)
        .when(
            (pl.col("_change_lag2") <= pl.col("price_change"))
            & (pl.col("price_change") < pl.col("_change_lag1"))
        )
        .then(1)
        .when(
            (pl.col("_change_lag1") < pl.col("_change_lag2"))
            & (pl.col("_change_lag2") <= pl.col("price_change"))
        )
        .then(2)
        .when(
            (pl.col("_change_lag1") <= pl.col("price_change"))
            & (pl.col("price_change") < pl.col("_change_lag2"))
        )
        .then(3)
        .when(
            (pl.col("price_change") < pl.col("_change_lag2"))
            & (pl.col("_change_lag2") <= pl.col("_change_lag1"))
        )
        .then(4)
        .otherwise(5)
        .cast(pl.Int8)
        .alias("_ordinal_pattern"),
    ).with_columns(
        pl.col("_leg1_change").shift(1).over("spread_id").alias("_leg1_lag1"),
        pl.col("_leg2_change").shift(1).over("spread_id").alias("_leg2_lag1"),
        (
            pl.col("_delta_vr")
            .rolling_var(advanced_window, min_samples=minimum_advanced)
            .over("spread_id")
            / (
                vr_lag
                * pl.col("price_change")
                .rolling_var(advanced_window, min_samples=minimum_advanced)
                .over("spread_id")
            )
        ).alias("variance_ratio_5"),
        (
            pl.col("_delta_hurst")
            .rolling_var(advanced_window, min_samples=minimum_advanced)
            .over("spread_id")
            / (
                hurst_lag
                * pl.col("price_change")
                .rolling_var(advanced_window, min_samples=minimum_advanced)
                .over("spread_id")
            )
        ).alias("variance_ratio_13"),
        (
            0.5
            * (
                pl.col("_delta_hurst")
                .rolling_var(advanced_window, min_samples=minimum_advanced)
                .over("spread_id")
                / pl.col("price_change")
                .rolling_var(advanced_window, min_samples=minimum_advanced)
                .over("spread_id")
            ).log()
            / math.log(hurst_lag)
        )
        .clip(0.0, 1.0)
        .alias("hurst_exponent_proxy"),
        (
            (pl.col("robust_z") * pl.col("robust_z").shift(1).over("spread_id") < 0)
            .cast(pl.Float64)
            .rolling_mean(int(ind.crossing_window), min_samples=int(ind.three_sessions))
            .over("spread_id")
        ).alias("zero_crossing_rate"),
        (
            -pl.col("ou_half_life_bars")
            .log()
            .rolling_std(int(ind.crossing_window), min_samples=int(ind.three_sessions))
            .over("spread_id")
            / math.log(2.0)
        )
        .exp()
        .alias("half_life_stability"),
    )
    probability_columns: list[str] = []
    for pattern in range(6):
        name = f"_ordinal_p{pattern}"
        probability_columns.append(name)
        result = result.with_columns(
            (pl.col("_ordinal_pattern") == pattern)
            .cast(pl.Float64)
            .rolling_mean(
                int(ind.entropy_window),
                min_samples=max(24, int(ind.entropy_window) // 2),
            )
            .over("spread_id")
            .alias(name)
        )
    entropy_terms = [
        pl.when(pl.col(name) > 0)
        .then(-pl.col(name) * pl.col(name).log())
        .otherwise(0.0)
        for name in probability_columns
    ]
    result = result.with_columns(
        (pl.sum_horizontal(*entropy_terms) / math.log(6.0)).alias(
            "permutation_entropy_3"
        ),
        pl.col("price_change")
        .pow(2)
        .rolling_sum(advanced_window, min_samples=minimum_advanced)
        .over("spread_id")
        .alias("_realized_variation"),
        (
            math.pi
            / 2.0
            * (pl.col("price_change").abs() * pl.col("_change_lag1").abs())
            .rolling_sum(advanced_window, min_samples=minimum_advanced)
            .over("spread_id")
        ).alias("_bipower_variation"),
        (
            pl.col("normalized_change")
            .rolling_sum(int(ind.cusum_window), min_samples=int(ind.cusum_window))
            .over("spread_id")
            / math.sqrt(int(ind.cusum_window))
        ).alias("cusum_change_score"),
        (
            pl.col("price_change").abs()
            / (pl.col("paired_volume_bbl") + 1.0)
            * 1_000_000.0
        ).alias("amihud_impact_proxy"),
        (
            pl.col("signed_volume")
            .rolling_sum(int(ind.one_session), min_samples=int(ind.one_session))
            .over("spread_id")
            / pl.col("paired_volume_bbl")
            .rolling_sum(int(ind.one_session), min_samples=int(ind.one_session))
            .over("spread_id")
        ).alias("signed_volume_imbalance_proxy"),
        (
            pl.col("relative_volume") / (pl.col("normalized_change").abs() + 0.25)
        ).alias("effort_vs_result"),
        (
            pl.col("package_oi_capacity")
            / pl.col("package_oi_capacity")
            .shift(int(ind.one_session))
            .over("spread_id")
            - 1.0
        ).alias("oi_migration_1d"),
        pl.when(~multi_leg)
        .then(
            pl.rolling_corr(
                pl.col("_leg1_change"),
                pl.col("_leg2_change"),
                window_size=advanced_window,
                min_samples=minimum_advanced,
            ).over("spread_id")
        )
        .otherwise(None)
        .alias("leg_return_correlation"),
        pl.when(~multi_leg)
        .then(
            pl.rolling_corr(
                pl.col("_leg1_change"),
                pl.col("_leg2_lag1"),
                window_size=advanced_window,
                min_samples=minimum_advanced,
            ).over("spread_id")
            - pl.rolling_corr(
                pl.col("_leg2_change"),
                pl.col("_leg1_lag1"),
                window_size=advanced_window,
                min_samples=minimum_advanced,
            ).over("spread_id")
        )
        .otherwise(None)
        .alias("lead_lag_score"),
        pl.when(multi_leg)
        .then(pl.lit("MULTI_LEG_DIAGNOSTIC_UNAVAILABLE"))
        .otherwise(pl.lit("TWO_LEG"))
        .alias("relationship_health_scope"),
    )
    result = _add_causal_risk_indicators(result, ind)
    result = result.with_columns(
        (
            (pl.col("_realized_variation") - pl.col("_bipower_variation"))
            .clip(lower_bound=0.0)
            / pl.col("_realized_variation")
        ).alias("jump_share"),
        (
            (pl.col("upside_semivol").pow(2) - pl.col("downside_semivol").pow(2))
            / (
                pl.col("upside_semivol").pow(2)
                + pl.col("downside_semivol").pow(2)
            )
        ).alias("semivariance_asymmetry"),
        (
            (
                (1.0 - pl.col("variance_ratio_5")).clip(0.0, 1.0)
                + ((0.5 - pl.col("hurst_exponent_proxy")) * 2.0).clip(0.0, 1.0)
                + (pl.col("zero_crossing_rate") * 10.0).clip(0.0, 1.0)
                + pl.col("half_life_stability").clip(0.0, 1.0)
                + (1.0 - pl.col("permutation_entropy_3")).clip(0.0, 1.0)
            )
            / 5.0
        ).alias("mean_reversion_stability"),
    ).with_columns(
        (
            (pl.col("cusum_change_score").abs() >= 3.0)
            | (pl.col("jump_share") >= 0.45)
        ).alias("change_point_alarm"),
        pl.when(
            (pl.col("cusum_change_score").abs() >= 3.0)
            | (pl.col("jump_share") >= 0.45)
        )
        .then(pl.lit("BREAK_RISK"))
        .when(pl.col("mean_reversion_stability") >= 0.55)
        .then(pl.lit("MEAN_REVERTING"))
        .when(pl.col("efficiency_ratio") >= 0.45)
        .then(pl.lit("TRENDING"))
        .otherwise(pl.lit("MIXED"))
        .alias("regime"),
    )
    return result.drop(
        "_delta_vr",
        "_delta_hurst",
        "_change_lag1",
        "_change_lag2",
        "_ordinal_pattern",
        "_realized_variation",
        "_bipower_variation",
        "_leg1_usd_bbl",
        "_leg2_usd_bbl",
        "_leg1_change",
        "_leg2_change",
        "_leg1_lag1",
        "_leg2_lag1",
        *probability_columns,
    )


def add_indicators(
    spreads: pl.DataFrame,
    config: TechnicalConfig,
    seasonality: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Calculate transparent, backward-only price, volume, and regime features."""

    if spreads.is_empty():
        return spreads
    ind = config.indicators
    result = spreads.sort(["spread_id", "timestamp_utc"])
    result = result.with_columns(
        pl.col("research_close")
        .rolling_median(ind.robust_z_window, min_samples=ind.three_sessions)
        .over("spread_id")
        .alias("rolling_median"),
        pl.col("research_close")
        .rolling_mean(ind.bollinger_window, min_samples=ind.three_sessions)
        .over("spread_id")
        .alias("bollinger_mid"),
        pl.col("research_close")
        .rolling_std(ind.bollinger_window, min_samples=ind.three_sessions)
        .over("spread_id")
        .alias("bollinger_std"),
        pl.col("research_close")
        .ewm_mean(span=ind.macd_fast, adjust=False, min_samples=ind.macd_fast)
        .over("spread_id")
        .alias("ema_fast"),
        pl.col("research_close")
        .ewm_mean(span=ind.macd_slow, adjust=False, min_samples=ind.macd_slow)
        .over("spread_id")
        .alias("ema_slow"),
        pl.col("research_close")
        .shift(1)
        .rolling_max(ind.donchian_window, min_samples=ind.three_sessions)
        .over("spread_id")
        .alias("donchian_high"),
        pl.col("research_close")
        .shift(1)
        .rolling_min(ind.donchian_window, min_samples=ind.three_sessions)
        .over("spread_id")
        .alias("donchian_low"),
        _rsi_expr("research_close", ind.rsi_window).alias("rsi"),
    )
    result = result.with_columns(
        (pl.col("research_close") - pl.col("rolling_median")).abs().alias("absolute_median_deviation"),
        (pl.col("bollinger_mid") + 2 * pl.col("bollinger_std")).alias("bollinger_upper"),
        (pl.col("bollinger_mid") - 2 * pl.col("bollinger_std")).alias("bollinger_lower"),
        (pl.col("ema_fast") - pl.col("ema_slow")).alias("macd_line"),
    )
    result = result.with_columns(
        pl.col("absolute_median_deviation")
        .rolling_median(ind.robust_z_window, min_samples=ind.three_sessions)
        .over("spread_id")
        .alias("rolling_mad"),
        pl.col("macd_line")
        .ewm_mean(span=ind.macd_signal, adjust=False, min_samples=ind.macd_signal)
        .over("spread_id")
        .alias("macd_signal"),
        pl.col("price_change")
        .rolling_std(ind.five_sessions, min_samples=ind.three_sessions)
        .over("spread_id")
        .alias("realized_vol_5d"),
        pl.col("price_change")
        .abs()
        .ewm_mean(span=ind.three_sessions, adjust=False, min_samples=ind.one_session)
        .over("spread_id")
        .alias("ewma_abs_change"),
        pl.when(pl.col("price_change") > 0)
        .then(pl.col("price_change") ** 2)
        .otherwise(0.0)
        .rolling_mean(ind.five_sessions, min_samples=ind.three_sessions)
        .over("spread_id")
        .sqrt()
        .alias("upside_semivol"),
        pl.when(pl.col("price_change") < 0)
        .then(pl.col("price_change") ** 2)
        .otherwise(0.0)
        .rolling_mean(ind.five_sessions, min_samples=ind.three_sessions)
        .over("spread_id")
        .sqrt()
        .alias("downside_semivol"),
    )
    result = result.with_columns(
        ((pl.col("research_close") - pl.col("rolling_median")) / (1.4826 * pl.col("rolling_mad")))
        .alias("robust_z"),
        ((pl.col("research_close") - pl.col("bollinger_lower")) / (4 * pl.col("bollinger_std")))
        .alias("bollinger_pct_b"),
        ((pl.col("bollinger_upper") - pl.col("bollinger_lower")) / pl.col("bollinger_mid").abs())
        .alias("bollinger_width"),
        (pl.col("macd_line") - pl.col("macd_signal")).alias("macd_histogram"),
        (
            (pl.col("research_close") - pl.col("research_close").shift(ind.one_session).over("spread_id")).abs()
            / pl.col("price_change")
            .abs()
            .rolling_sum(ind.one_session, min_samples=ind.one_session)
            .over("spread_id")
        ).alias("efficiency_ratio"),
        (pl.col("price_change") / pl.col("realized_vol_5d")).alias("normalized_change"),
        (pl.col("price_change").abs() / pl.col("ewma_abs_change")).alias("jump_score"),
    )
    result = result.with_columns(
        pl.col("bollinger_width")
        .shift(1)
        .rolling_quantile(
            0.20,
            interpolation="linear",
            window_size=ind.ten_sessions,
            min_samples=ind.five_sessions,
        )
        .over("spread_id")
        .alias("bollinger_width_p20")
    ).with_columns(
        (pl.col("bollinger_width") < pl.col("bollinger_width_p20")).alias(
            "volatility_squeeze"
        ),
        pl.col("bollinger_width")
        .rolling_std(ind.five_sessions, min_samples=ind.three_sessions)
        .over("spread_id")
        .alias("volatility_of_volatility"),
    )

    # Rolling AR(1) beta and an OU-style half-life diagnostic.
    result = result.with_columns(
        pl.col("research_close").shift(1).over("spread_id").alias("lag_close")
    )
    window = ind.ten_sessions
    result = result.with_columns(
        pl.col("research_close").rolling_mean(window, min_samples=ind.five_sessions).over("spread_id").alias("_x_mean"),
        pl.col("lag_close").rolling_mean(window, min_samples=ind.five_sessions).over("spread_id").alias("_lag_mean"),
        (pl.col("research_close") * pl.col("lag_close"))
        .rolling_mean(window, min_samples=ind.five_sessions)
        .over("spread_id")
        .alias("_cross_mean"),
        (pl.col("lag_close") ** 2)
        .rolling_mean(window, min_samples=ind.five_sessions)
        .over("spread_id")
        .alias("_lag_sq_mean"),
    ).with_columns(
        ((pl.col("_cross_mean") - pl.col("_x_mean") * pl.col("_lag_mean")) /
         (pl.col("_lag_sq_mean") - pl.col("_lag_mean") ** 2)).alias("ar1_beta")
    ).with_columns(
        pl.when((pl.col("ar1_beta") > 0) & (pl.col("ar1_beta") < 0.9999))
        .then(-math.log(2.0) / pl.col("ar1_beta").log())
        .otherwise(None)
        .alias("ou_half_life_bars")
    )

    # Volume is normalized by the same bar-of-day across prior sessions.
    result = result.with_columns(
        pl.col("package_volume_capacity")
        .shift(1)
        .rolling_median(ind.volume_seasonality_sessions, min_samples=8)
        .over(["spread_id", "bar_slot"])
        .alias("expected_tod_package_volume"),
        pl.col("package_volume_capacity")
        .ewm_mean(span=ind.one_session, adjust=False, min_samples=ind.one_session)
        .over("spread_id")
        .alias("volume_ema_fast"),
        pl.col("package_volume_capacity")
        .ewm_mean(span=ind.three_sessions, adjust=False, min_samples=ind.three_sessions)
        .over("spread_id")
        .alias("volume_ema_slow"),
    ).with_columns(
        (pl.col("package_volume_capacity") / pl.col("expected_tod_package_volume")).alias("relative_volume"),
        ((pl.col("volume_ema_fast") - pl.col("volume_ema_slow")) / pl.col("volume_ema_slow")).alias("pvo"),
        (pl.col("paired_volume_bbl") / pl.col("min_leg_events")).alias("average_trade_size_bbl_proxy"),
        (
            pl.col("price_change").sign() * pl.col("paired_volume_bbl")
        ).fill_null(0.0).alias("signed_volume"),
    ).with_columns(
        (
            (pl.col("spread_bar_vwap") * pl.col("package_volume_capacity"))
            .cum_sum()
            .over(["spread_id", "roll_id", "session_date"])
            / pl.col("package_volume_capacity")
            .cum_sum()
            .over(["spread_id", "roll_id", "session_date"])
        ).alias("session_vwap_proxy"),
        pl.col("signed_volume").cum_sum().over(["spread_id", "roll_id"]).alias("obv_proxy"),
        pl.when(
            (pl.col("efficiency_ratio") < 0.35)
            & (pl.col("ou_half_life_bars") >= 2)
            & (pl.col("ou_half_life_bars") <= ind.three_sessions)
        )
        .then(pl.lit("MEAN_REVERTING"))
        .when((pl.col("efficiency_ratio") > 0.45) & (pl.col("macd_histogram").abs() > 0))
        .then(pl.lit("TRENDING"))
        .otherwise(pl.lit("MIXED"))
        .alias("regime"),
    )

    result = _add_advanced_indicators(result, ind)

    result = result.with_columns(
        pl.col("session_date").dt.year().alias("asof_year"),
        pl.col("session_date").dt.ordinal_day().alias("day_of_year"),
        pl.col("session_date").dt.month().alias("calendar_month"),
        pl.col("session_date").dt.weekday().alias("weekday"),
        (
            (pl.col("session_date").dt.weekday() == 3)
            & (pl.col("bar_start_et").dt.hour() == 10)
            & (pl.col("bar_start_et").dt.minute() == 30)
        ).alias("eia_window"),
    )
    if seasonality is not None and not seasonality.is_empty():
        result = result.join(
            seasonality,
            on=["spread_id", "asof_year", "day_of_year"],
            how="left",
        ).with_columns(
            pl.when((pl.col("seasonal_q75") - pl.col("seasonal_q25")) > 0)
            .then(
                (pl.col("spread_close") - pl.col("seasonal_median"))
                / ((pl.col("seasonal_q75") - pl.col("seasonal_q25")) / 1.349)
            )
            .otherwise(None)
            .alias("seasonal_z"),
            pl.coalesce(
                [
                    0.70 * pl.col("seasonal_move_1d")
                    + 0.30 * pl.col("expiry_seasonal_move_1d"),
                    pl.col("seasonal_move_1d"),
                    pl.col("expiry_seasonal_move_1d"),
                ]
            ).alias("seasonal_expected_move_1d"),
        ).with_columns(
            (pl.col("spread_close") + pl.col("seasonal_expected_move_1d")).alias(
                "seasonal_fair_value_1d"
            ),
            (
                pl.col("seasonal_expected_move_1d") / pl.col("realized_vol_5d")
            ).alias("seasonal_move_z"),
            (
                pl.col("seasonal_support")
                * pl.when(pl.col("daily_settlement_quality") == "EXCHANGE_SETTLE")
                .then(1.0)
                .otherwise(0.65)
            ).alias("seasonal_confidence"),
        )
    else:
        result = result.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("seasonal_median"),
            pl.lit(None, dtype=pl.Float64).alias("seasonal_q25"),
            pl.lit(None, dtype=pl.Float64).alias("seasonal_q75"),
            pl.lit(None, dtype=pl.Int64).alias("seasonal_n"),
            pl.lit(None, dtype=pl.Float64).alias("seasonal_z"),
            pl.lit(None, dtype=pl.Int64).alias("seasonal_prior_years"),
            pl.lit(None, dtype=pl.Float64).alias("seasonal_move_1d"),
            pl.lit(None, dtype=pl.Float64).alias("expiry_seasonal_move_1d"),
            pl.lit(None, dtype=pl.Float64).alias("seasonal_support"),
            pl.lit(None, dtype=pl.Float64).alias("seasonal_expected_move_1d"),
            pl.lit(None, dtype=pl.Float64).alias("seasonal_fair_value_1d"),
            pl.lit(None, dtype=pl.Float64).alias("seasonal_move_z"),
            pl.lit(None, dtype=pl.Float64).alias("seasonal_confidence"),
            pl.lit("UNAVAILABLE").alias("seasonal_status"),
        )

    result = result.drop("_x_mean", "_lag_mean", "_cross_mean", "_lag_sq_mean")
    disposable = [
        column
        for column in (
            "lag_close",
            "absolute_median_deviation",
            "volume_ema_fast",
            "volume_ema_slow",
            "expected_tod_package_volume",
            "spread_bar_vwap",
            "signed_volume",
            "leg1_close",
            "leg2_close",
            "leg1_volume_contracts",
            "leg2_volume_contracts",
            "leg1_price_to_usd_bbl",
            "leg2_price_to_usd_bbl",
        )
        if column in result.columns
    ]
    if disposable:
        result = result.drop(disposable)
    preserve_float64 = {
        "spread_open",
        "spread_close",
        "research_close",
        "package_open_value_usd",
        "package_close_value_usd",
        "one_way_cost_usd",
        "heating_oil_cpg",
        "gasoil_usd_mt",
        "gasoil_usd_bbl",
        "gasoil_cpg",
        "hogo_cpg",
    }
    compact_float_columns = [
        column
        for column, dtype in result.schema.items()
        if dtype == pl.Float64 and column not in preserve_float64
    ]
    if compact_float_columns:
        result = result.with_columns(
            [pl.col(column).cast(pl.Float32) for column in compact_float_columns]
        )
    return result


def latest_leg_tickers(frame: pl.DataFrame) -> list[str]:
    if frame.is_empty():
        return []
    latest_time = frame["timestamp_utc"].max()
    latest = frame.filter(pl.col("timestamp_utc") == latest_time)
    columns = sorted(
        column for column in latest.columns if column.startswith("leg") and column.endswith("_security")
    )
    values: set[str] = set()
    for column in columns:
        values.update(str(item) for item in latest[column].drop_nulls().to_list())
    return sorted(values)


def indicator_library_audit(
    features: pl.DataFrame, config: TechnicalConfig
) -> pl.DataFrame:
    """Numerically cross-check native indicators against both requested libraries.

    The native grouped Polars expressions remain authoritative.  The lightweight
    `polars-talis` package is always checked separately per spread so state cannot
    leak across structures.  The Pandas-dependent `polars-ta` comparison remains
    optional and is never required by the operating runtime.
    """

    if features.is_empty():
        return pl.DataFrame()
    try:
        from polars_talis.indicators.momentum import RSI as TalisRSI
        from polars_talis.indicators.trend import EMA as TalisEMA
    except ImportError as exc:
        return pl.DataFrame(
            [
                {
                    "spread_id": "PACKAGE_AUDIT",
                    "status": "MISSING_PACKAGE",
                    "notes": str(exc),
                }
            ]
        )
    try:
        from polars_ta.ta.momentum import RSI as PolarsTaRSI
        from polars_ta.ta.overlap import EMA as PolarsTaEMA

        polars_ta_available = True
    except ImportError:
        PolarsTaRSI = None  # type: ignore[assignment]
        PolarsTaEMA = None  # type: ignore[assignment]
        polars_ta_available = False

    rows: list[dict[str, object]] = []
    tail_rows = max(config.indicators.twenty_sessions, config.indicators.macd_slow * 3)
    audit_features = features.select(
        "spread_id", "timestamp_utc", "research_close", "rsi", "ema_fast"
    ).sort(["spread_id", "timestamp_utc"])
    for group in audit_features.partition_by("spread_id", maintain_order=True):
        ordered = group.tail(tail_rows)
        sample = ordered.select(pl.col("research_close").alias("close"))
        ta_last: dict[str, object] = {}
        if polars_ta_available and PolarsTaRSI is not None and PolarsTaEMA is not None:
            ta = sample.with_columns(
                (
                    100.0
                    * PolarsTaRSI(pl.col("close"), config.indicators.rsi_window)
                ).alias("polars_ta_rsi"),
                PolarsTaEMA(pl.col("close"), config.indicators.macd_fast).alias(
                    "polars_ta_ema"
                ),
            )
            ta_last = ta.tail(1).to_dicts()[0]
        talis = TalisRSI(
            period=config.indicators.rsi_window,
            column="close",
            name="polars_talis_rsi",
        )._calculate(sample)
        talis = TalisEMA(
            period=config.indicators.macd_fast,
            column="close",
            name="polars_talis_ema",
        )._calculate(talis)
        native = ordered.tail(1).to_dicts()[0]
        talis_last = talis.tail(1).to_dicts()[0]
        native_rsi = _finite_or_none(native.get("rsi"))
        native_ema = _finite_or_none(native.get("ema_fast"))
        ta_rsi = _finite_or_none(ta_last.get("polars_ta_rsi"))
        ta_ema = _finite_or_none(ta_last.get("polars_ta_ema"))
        talis_rsi = _finite_or_none(talis_last.get("polars_talis_rsi"))
        talis_ema = _finite_or_none(talis_last.get("polars_talis_ema"))
        max_delta = max(
            [
                abs(item)
                for item in (
                    ta_rsi - native_rsi if None not in (ta_rsi, native_rsi) else None,
                    talis_rsi - native_rsi
                    if None not in (talis_rsi, native_rsi)
                    else None,
                    ta_ema - native_ema if None not in (ta_ema, native_ema) else None,
                    talis_ema - native_ema
                    if None not in (talis_ema, native_ema)
                    else None,
                )
                if item is not None
            ],
            default=float("nan"),
        )
        rows.append(
            {
                "spread_id": str(native["spread_id"]),
                "as_of_utc": native["timestamp_utc"],
                "native_rsi": native_rsi,
                "polars_ta_rsi": ta_rsi,
                "polars_talis_rsi": talis_rsi,
                "native_ema_fast": native_ema,
                "polars_ta_ema_fast": ta_ema,
                "polars_talis_ema_fast": talis_ema,
                "maximum_absolute_delta": max_delta,
                "status": (
                    "AUDITED"
                    if polars_ta_available
                    else "AUDITED_NATIVE_VS_TALIS"
                ),
                "notes": (
                    "Native grouped Polars is authoritative. Talis is checked in every "
                    "run; the Pandas-dependent polars-ta comparison is optional. "
                    "Differing warm-up and smoothing definitions are expected."
                ),
            }
        )
    return pl.DataFrame(rows).sort("spread_id")


def _finite_or_none(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


__all__ = [
    "TechnicalAnalyticsError",
    "add_indicators",
    "build_seasonality_table",
    "build_spread_bars",
    "daily_to_prepared_bars",
    "indicator_library_audit",
    "latest_leg_tickers",
    "prepare_trade_bars",
]
