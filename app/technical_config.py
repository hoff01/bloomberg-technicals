"""Validated configuration and contract metadata for the technical system.

The live pipeline resolves expiry fields from Bloomberg.  The calendar
functions in this module are deliberately conservative fallbacks used for
preflight, demo data, and explicit data-quality warnings; they are never
presented as exchange-authoritative dates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import csv
from pathlib import Path
import re
import tomllib
from typing import Any, Iterable, Mapping


MONTH_CODES = {
    1: "F",
    2: "G",
    3: "H",
    4: "J",
    5: "K",
    6: "M",
    7: "N",
    8: "Q",
    9: "U",
    10: "V",
    11: "X",
    12: "Z",
}
ROOTS = ("HO", "CL", "CO", "QS")
ROOT_PATTERN = re.compile(r"^[A-Z][A-Z0-9._-]*$")


class TechnicalConfigError(ValueError):
    """Raised when a technical-system configuration is unsafe or ambiguous."""


def _parse_time(value: object, label: str) -> time:
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%H:%M").time()
    except ValueError as exc:
        raise TechnicalConfigError(f"{label} must use HH:MM, got {text!r}") from exc


def _parse_date(value: object, label: str) -> date:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise TechnicalConfigError(f"{label} must use YYYY-MM-DD, got {text!r}") from exc


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def add_months(value: date, months: int) -> date:
    index = value.year * 12 + value.month - 1 + int(months)
    year, month_zero = divmod(index, 12)
    return date(year, month_zero + 1, 1)


def month_range(start: date, end: date) -> tuple[date, ...]:
    current = date(start.year, start.month, 1)
    terminal = date(end.year, end.month, 1)
    result: list[date] = []
    while current <= terminal:
        result.append(current)
        current = add_months(current, 1)
    return tuple(result)


def business_day_offset(value: date, offset: int) -> date:
    """Return a Monday-Friday offset used only when exchange metadata is absent."""

    current = value
    remaining = abs(int(offset))
    direction = 1 if offset >= 0 else -1
    while remaining:
        current += timedelta(days=direction)
        if current.weekday() < 5:
            remaining -= 1
    return current


def _observed(value: date) -> date:
    if value.weekday() == 5:
        return value - timedelta(days=1)
    if value.weekday() == 6:
        return value + timedelta(days=1)
    return value


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    current = date(year, month, 1)
    current += timedelta(days=(weekday - current.weekday()) % 7)
    return current + timedelta(weeks=nth - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    current = add_months(date(year, month, 1), 1) - timedelta(days=1)
    return current - timedelta(days=(current.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    """Gregorian Easter (Meeus/Jones/Butcher)."""

    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = (h + ell - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def exchange_holidays(root: str, year: int) -> frozenset[date]:
    """Conservative full-session closures used to count expiry sessions.

    Bloomberg expiry metadata remains authoritative. Treating a partial
    holiday as closed can move liquidation earlier, never later.
    """

    root = root.upper()
    easter = _easter_sunday(year)
    if root in {"HO", "CL"}:
        holidays = {
            _observed(date(year, 1, 1)),
            _nth_weekday(year, 1, 0, 3),
            _nth_weekday(year, 2, 0, 3),
            easter - timedelta(days=2),
            _last_weekday(year, 5, 0),
            _observed(date(year, 6, 19)),
            _observed(date(year, 7, 4)),
            _nth_weekday(year, 9, 0, 1),
            _nth_weekday(year, 11, 3, 4),
            _observed(date(year, 12, 25)),
        }
    elif root in {"CO", "QS"}:
        christmas = _observed(date(year, 12, 25))
        boxing = _observed(date(year, 12, 26))
        if boxing == christmas:
            boxing += timedelta(days=1)
        holidays = {
            _observed(date(year, 1, 1)),
            easter - timedelta(days=2),
            easter + timedelta(days=1),
            _nth_weekday(year, 5, 0, 1),
            _last_weekday(year, 5, 0),
            _last_weekday(year, 8, 0),
            christmas,
            boxing,
        }
    else:
        holidays = set()
    return frozenset(holidays)


def exchange_session_offset(value: date, offset: int, root: str) -> date:
    current = value
    remaining = abs(int(offset))
    direction = 1 if offset >= 0 else -1
    while remaining:
        current += timedelta(days=direction)
        if current.weekday() < 5 and current not in exchange_holidays(root, current.year):
            remaining -= 1
    return current


def expected_latest_exchange_session(
    as_of: date,
    observed_at: datetime,
    root: str,
    *,
    session_start: time,
    bar_interval_minutes: int,
    grace_minutes: int,
) -> date:
    """Return the latest session that should have a completed intraday bar."""

    is_session = (
        as_of.weekday() < 5
        and as_of not in exchange_holidays(root, as_of.year)
    )
    if not is_session:
        return exchange_session_offset(as_of, -1, root)
    if as_of > observed_at.date():
        raise TechnicalConfigError("as_of cannot be later than the observation time")
    if as_of == observed_at.date():
        first_bar_ready = datetime.combine(
            as_of,
            session_start,
            tzinfo=observed_at.tzinfo,
        ) + timedelta(minutes=bar_interval_minutes + grace_minutes)
        if observed_at < first_bar_ready:
            return exchange_session_offset(as_of, -1, root)
    return as_of


def previous_business_day(value: date) -> date:
    current = value
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current


def last_business_day(year: int, month: int) -> date:
    next_month = add_months(date(year, month, 1), 1)
    return previous_business_day(next_month - timedelta(days=1))


def approximate_expiry(root: str, delivery_month: date) -> date:
    """Conservative exchange-rule approximation for demo/preflight only."""

    root = root.upper()
    delivery = date(delivery_month.year, delivery_month.month, 1)
    if root == "HO":
        prior = add_months(delivery, -1)
        return last_business_day(prior.year, prior.month)
    if root == "CO":
        expiry_month = add_months(delivery, -2)
        return last_business_day(expiry_month.year, expiry_month.month)
    if root == "CL":
        prior = add_months(delivery, -1)
        anchor = previous_business_day(date(prior.year, prior.month, 25))
        return business_day_offset(anchor, -3)
    if root == "QS":
        anchor = previous_business_day(date(delivery.year, delivery.month, 14))
        return business_day_offset(anchor, -2)
    raise TechnicalConfigError(f"No fallback expiry rule for root {root!r}")


@dataclass(frozen=True, slots=True)
class RootSpec:
    root: str
    name: str
    ticker_template: str
    generic_template: str
    native_unit: str
    price_to_usd_bbl: float
    contract_size_native: float
    contract_barrels: float
    tick_size_native: float
    commission_per_contract_side: float
    slippage_ticks_per_side: float

    @property
    def tick_usd_per_contract(self) -> float:
        return self.tick_size_native * self.contract_size_native

    @property
    def one_way_cost_usd(self) -> float:
        return (
            self.slippage_ticks_per_side * self.tick_usd_per_contract
            + self.commission_per_contract_side
        )

    def ticker(self, delivery_month: date) -> str:
        values = {
            "root": self.root,
            "month_code": MONTH_CODES[delivery_month.month],
            "y": str(delivery_month.year % 10),
            "yy": f"{delivery_month.year % 100:02d}",
            "year_1d": str(delivery_month.year % 10),
            "year_2d": f"{delivery_month.year % 100:02d}",
            "yyyy": delivery_month.year,
            "year": delivery_month.year,
        }
        return " ".join(self.ticker_template.format(**values).split())

    def generic(self, rank: int) -> str:
        return " ".join(
            self.generic_template.format(root=self.root, rank=int(rank)).split()
        )


@dataclass(frozen=True, slots=True)
class SystemSettings:
    project_name: str
    model_version: str
    timezone: str
    session_start: time
    session_end: time
    bar_interval_minutes: int
    complete_bars_per_session: int
    rolling_intraday_months: int
    daily_history_start: date
    forward_curve_months: int
    expiry_flat_sessions: int
    pull_overlap_days: int
    pull_chunk_days: int
    max_concurrent_requests: int
    minimum_preliminary_sessions: int
    minimum_production_sessions: int
    output_history_sessions: int
    maximum_model_age_sessions: int
    open_browser: bool


@dataclass(frozen=True, slots=True)
class BloombergSettings:
    host: str
    port: int
    request_pool_size: int
    request_timeout_seconds: int
    heartbeat_seconds: int
    retry_max_retries: int
    maximum_trade_failure_rate: float
    freshness_grace_minutes: int
    event_type: str
    pull_bid_ask_bars: bool
    gap_fill_initial_bar: bool
    intraday_retention_warning_days: int
    reference_fields: tuple[str, ...]
    daily_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LiquiditySettings:
    capture_seconds: int
    depth_mode: str
    top_of_book_fields: tuple[str, ...]
    depth_snapshot_max_age_minutes: int
    minimum_relative_volume: float
    maximum_bid_ask_ticks: float
    minimum_leg_alignment: float
    minimum_volume_coverage: float
    maximum_volume_participation: float
    require_true_l2_for_enter: bool
    cap_confidence_without_depth: float


@dataclass(frozen=True, slots=True)
class BacktestSettings:
    parallel_workers: int
    train_sessions: int
    validation_sessions: int
    test_sessions: int
    lockbox_sessions: int
    embargo_sessions: int
    maximum_holding_bars: int
    minimum_oos_trades: int
    minimum_profitable_fold_share: float
    minimum_profit_factor: float
    minimum_probability_sharpe: float
    minimum_confidence: float
    annual_sessions: int
    execution_lag_bars: int
    allow_overnight: bool
    risk_target_usd: float
    bootstrap_samples: int
    adaptive_horizon_bars: int
    adaptive_min_observations: int
    adaptive_learning_rate: float
    adaptive_uniform_shrinkage: float
    adaptive_max_expert_weight: float
    adaptive_entry_threshold: float
    adaptive_freeze_lockbox: bool
    maximum_new_trades_per_session: int
    one_trade_per_algebra_group: bool


@dataclass(frozen=True, slots=True)
class IndicatorSettings:
    half_session: int
    one_session: int
    three_sessions: int
    five_sessions: int
    ten_sessions: int
    twenty_sessions: int
    robust_z_window: int
    bollinger_window: int
    rsi_window: int
    macd_fast: int
    macd_slow: int
    macd_signal: int
    donchian_window: int
    volume_seasonality_sessions: int
    seasonality_min_prior_years: int
    advanced_window: int
    entropy_window: int
    variance_ratio_lag: int
    hurst_lag: int
    crossing_window: int
    cusum_window: int
    seasonality_window_days: int
    expiry_seasonality_window_sessions: int
    seasonality_shrinkage: float
    tail_event_z: float
    extreme_tod_shock_z: float
    volume_dryness_z: float
    tail_cluster_rate: float
    vol_expansion_ratio: float
    impact_stress_ratio: float
    candidate_risk_gates_enabled: bool


@dataclass(frozen=True, slots=True)
class SpreadLeg:
    spread_id: str
    leg_order: int
    root: str
    selection_mode: str
    delivery_offset: int
    rank: int
    sign: int
    contracts: int
    price_weight: float
    description: str


@dataclass(frozen=True, slots=True)
class SpreadSpec:
    spread_id: str
    enabled: bool
    display_name: str
    family: str
    anchor_root: str
    anchor_rank: int
    unit: str
    core: bool
    model_enabled: bool
    complexity_tier: int
    algebra_group: str
    notes: str
    legs: tuple[SpreadLeg, ...]


def _curve_leg(
    spread_id: str,
    order: int,
    root: str,
    offset: int,
    sign: int,
    contracts: int = 1,
    price_weight: float = 1.0,
) -> SpreadLeg:
    side = "Long" if sign > 0 else "Short"
    return SpreadLeg(
        spread_id=spread_id,
        leg_order=order,
        root=root,
        selection_mode="delivery",
        delivery_offset=offset,
        rank=1,
        sign=sign,
        contracts=contracts,
        price_weight=price_weight,
        description=f"{side} {contracts} {root} delivery offset {offset:+d}",
    )


def _generated_curve_spreads(horizon: int) -> tuple[SpreadSpec, ...]:
    """Create the canonical M1-M16 relative-value basis.

    The registry deliberately uses adjacent calendars only.  Non-adjacent
    calendars are linear combinations and are rendered on demand rather than
    counted as additional hypotheses.  Price weights define the normalized
    USD/bbl quote; contract counts define the actual executable futures package.
    """

    if not 2 <= horizon <= 24:
        raise TechnicalConfigError("forward_curve_months must be between 2 and 24")
    result: list[SpreadSpec] = []

    def add(
        spread_id: str,
        name: str,
        family: str,
        anchor_root: str,
        rank: int,
        tier: int,
        algebra_group: str,
        legs: tuple[SpreadLeg, ...],
        *,
        core: bool = False,
        notes: str = "",
    ) -> None:
        result.append(
            SpreadSpec(
                spread_id=spread_id,
                enabled=True,
                display_name=name,
                family=family,
                anchor_root=anchor_root,
                anchor_rank=rank,
                unit="USD/bbl",
                core=core,
                model_enabled=True,
                complexity_tier=tier,
                algebra_group=algebra_group,
                notes=notes,
                legs=legs,
            )
        )

    for root in ROOTS:
        for rank in range(1, horizon):
            sid = f"CAL.{root}.M{rank:02d}_M{rank + 1:02d}"
            group = f"{root}_CAL_12" if rank == 1 else sid.replace(".", "_")
            add(
                sid,
                f"{root} M{rank}-M{rank + 1} Calendar",
                "Calendar",
                root,
                rank,
                1,
                group,
                (
                    _curve_leg(sid, 1, root, 0, 1),
                    _curve_leg(sid, 2, root, 1, -1),
                ),
                core=root in {"HO", "QS"} and rank <= 2,
                notes="Adjacent calendar; positive is backwardation.",
            )
        for rank in range(1, horizon - 1):
            sid = f"FLY.{root}.M{rank:02d}_M{rank + 1:02d}_M{rank + 2:02d}"
            group = f"{root}_FLY_123" if rank == 1 else sid.replace(".", "_")
            add(
                sid,
                f"{root} M{rank}-M{rank + 1}-M{rank + 2} Fly",
                "Fly",
                root,
                rank,
                2,
                group,
                (
                    _curve_leg(sid, 1, root, 0, 1),
                    _curve_leg(sid, 2, root, 1, -1, 2, 2.0),
                    _curve_leg(sid, 3, root, 2, 1),
                ),
                core=root in {"HO", "QS"} and rank <= 2,
                notes="Adjacent curve curvature +1/-2/+1; promotion candidate.",
            )
        for rank in range(1, horizon - 2):
            sid = (
                f"CDR.{root}.M{rank:02d}_M{rank + 1:02d}_"
                f"M{rank + 2:02d}_M{rank + 3:02d}"
            )
            group = f"{root}_CONDOR_1234" if rank == 1 else sid.replace(".", "_")
            add(
                sid,
                f"{root} M{rank}-M{rank + 3} Condor",
                "Condor",
                root,
                rank,
                3,
                group,
                (
                    _curve_leg(sid, 1, root, 0, 1),
                    _curve_leg(sid, 2, root, 1, -1),
                    _curve_leg(sid, 3, root, 2, -1),
                    _curve_leg(sid, 4, root, 3, 1),
                ),
                core=root in {"HO", "QS"} and rank <= 2,
                notes="Equal-wing +1/-1/-1/+1 curve condor; promotion candidate.",
            )

    for rank in range(1, horizon + 1):
        # Natural exchange cracks.
        sid = f"CRK.HO_CL.M{rank:02d}"
        group = f"HO_WTI_M{rank}" if rank <= 2 else sid.replace(".", "_")
        add(
            sid,
            f"HO/WTI Same-Month Crack M{rank}",
            "Crack",
            "HO",
            rank,
            1,
            group,
            (_curve_leg(sid, 1, "HO", 0, 1), _curve_leg(sid, 2, "CL", 0, -1)),
            core=rank <= 2,
            notes="Natural 1 HO : 1 WTI 1,000-barrel crack package.",
        )
        sid = f"CRK.QS_CO.M{rank:02d}"
        group = f"QS_BRENT_M{rank}" if rank <= 2 else sid.replace(".", "_")
        add(
            sid,
            f"Gasoil/Brent Same-Month Crack M{rank}",
            "Crack",
            "QS",
            rank,
            1,
            group,
            (
                _curve_leg(sid, 1, "QS", 0, 1, 4),
                _curve_leg(sid, 2, "CO", 0, -1, 3),
            ),
            core=rank <= 2,
            notes="ICE natural 4 gasoil : 3 Brent package; quote normalized to USD/bbl.",
        )

        sid = f"RV.HO_QS.M{rank:02d}"
        group = "HOGO_M1" if rank == 1 else sid.replace(".", "_")
        add(
            sid,
            f"HO/Gasoil Relative Value M{rank}",
            "Relative Value",
            "HO",
            rank,
            1,
            group,
            (
                _curve_leg(sid, 1, "HO", 0, 1, 3),
                _curve_leg(sid, 2, "QS", 0, -1, 4),
            ),
            core=rank <= 2,
            notes="Near barrel-neutral 3 HO : 4 gasoil package.",
        )
        sid = f"RV.CO_CL.M{rank:02d}"
        group = "CRUDE_BASIS_M1" if rank == 1 else sid.replace(".", "_")
        add(
            sid,
            f"Brent/WTI Relative Value M{rank}",
            "Relative Value",
            "CO",
            rank,
            1,
            group,
            (_curve_leg(sid, 1, "CO", 0, 1), _curve_leg(sid, 2, "CL", 0, -1)),
            core=False,
            notes="Same-delivery Brent-WTI basis.",
        )

        sid = f"BOX.CRACK.TRANSATL.M{rank:02d}"
        group = "REL_MARGIN_M1" if rank == 1 else sid.replace(".", "_")
        add(
            sid,
            f"US-Europe Crack Relative Box M{rank}",
            "Relative Box",
            "HO",
            rank,
            3,
            group,
            (
                _curve_leg(sid, 1, "HO", 0, 1, 3),
                _curve_leg(sid, 2, "CL", 0, -1, 3),
                _curve_leg(sid, 3, "QS", 0, -1, 4),
                _curve_leg(sid, 4, "CO", 0, 1, 3),
            ),
            core=rank <= 2,
            notes="HO/WTI margin less gasoil/Brent margin; promotion candidate.",
        )

    for rank in range(1, horizon):
        for product, crude, product_lots, crude_lots, label in (
            ("HO", "CL", 1, 1, "HO/WTI"),
            ("QS", "CO", 4, 3, "Gasoil/Brent"),
        ):
            sid = f"BOX.CRKCAL.{product}_{crude}.M{rank:02d}_M{rank + 1:02d}"
            if rank == 1:
                group = "HO_WTI_CRACK_CAL" if product == "HO" else "QS_BRENT_CRACK_CAL"
            else:
                group = sid.replace(".", "_")
            add(
                sid,
                f"{label} Crack Calendar Box M{rank}-M{rank + 1}",
                "Crack Curve",
                product,
                rank,
                2,
                group,
                (
                    _curve_leg(sid, 1, product, 0, 1, product_lots),
                    _curve_leg(sid, 2, crude, 0, -1, crude_lots),
                    _curve_leg(sid, 3, product, 1, -1, product_lots),
                    _curve_leg(sid, 4, crude, 1, 1, crude_lots),
                ),
                core=rank <= 2,
                notes="Adjacent crack slope; promotion candidate.",
            )

        sid = f"BOX.CURVE.HO_QS.M{rank:02d}_M{rank + 1:02d}"
        group = "HOGO_CAL_BOX" if rank == 1 else sid.replace(".", "_")
        add(
            sid,
            f"HO-Gasoil Curve Box M{rank}-M{rank + 1}",
            "Relative Box",
            "HO",
            rank,
            2,
            group,
            (
                _curve_leg(sid, 1, "HO", 0, 1, 3),
                _curve_leg(sid, 2, "QS", 0, -1, 4),
                _curve_leg(sid, 3, "HO", 1, -1, 3),
                _curve_leg(sid, 4, "QS", 1, 1, 4),
            ),
            core=rank <= 2,
            notes="Distillate calendar relative-value box; promotion candidate.",
        )
        sid = f"BOX.CURVE.CO_CL.M{rank:02d}_M{rank + 1:02d}"
        group = "CRUDE_BASIS_BOX" if rank == 1 else sid.replace(".", "_")
        add(
            sid,
            f"Brent-WTI Curve Box M{rank}-M{rank + 1}",
            "Relative Box",
            "CO",
            rank,
            2,
            group,
            (
                _curve_leg(sid, 1, "CO", 0, 1),
                _curve_leg(sid, 2, "CL", 0, -1),
                _curve_leg(sid, 3, "CO", 1, -1),
                _curve_leg(sid, 4, "CL", 1, 1),
            ),
            core=False,
            notes="Crude basis calendar box; promotion candidate.",
        )

    for rank in range(1, horizon - 1):
        sid = f"FLY.HOGO.M{rank:02d}_M{rank + 1:02d}_M{rank + 2:02d}"
        group = "HOGO_FLY_BOX" if rank == 1 else sid.replace(".", "_")
        add(
            sid,
            f"HOGO Relative Fly M{rank}-M{rank + 2}",
            "HOGO Fly",
            "HO",
            rank,
            3,
            group,
            (
                _curve_leg(sid, 1, "HO", 0, 1, 3),
                _curve_leg(sid, 2, "HO", 1, -1, 6),
                _curve_leg(sid, 3, "HO", 2, 1, 3),
                _curve_leg(sid, 4, "QS", 0, -1, 4),
                _curve_leg(sid, 5, "QS", 1, 1, 8),
                _curve_leg(sid, 6, "QS", 2, -1, 4),
            ),
            core=rank <= 2,
            notes=(
                "HO fly less gasoil fly on near barrel-neutral 3:4 package; "
                "reported in USD/bbl and cpg."
            ),
        )
    return tuple(result)


@dataclass(frozen=True, slots=True)
class ContractDefinition:
    root: str
    ticker: str
    delivery_month: date
    fallback_expiry: date


@dataclass(frozen=True, slots=True)
class TechnicalConfig:
    source_path: Path
    system: SystemSettings
    bloomberg: BloombergSettings
    liquidity: LiquiditySettings
    backtest: BacktestSettings
    indicators: IndicatorSettings
    roots: Mapping[str, RootSpec]
    spreads: tuple[SpreadSpec, ...]

    def build_contract_universe(
        self,
        start: date,
        end: date,
        *,
        history_buffer_months: int = 3,
        forward_months: int | None = None,
    ) -> tuple[ContractDefinition, ...]:
        if forward_months is None:
            forward_months = self.system.forward_curve_months + 2
        first = add_months(date(start.year, start.month, 1), -history_buffer_months)
        last = add_months(date(end.year, end.month, 1), forward_months)
        result: list[ContractDefinition] = []
        for root_code in ROOTS:
            root = self.roots[root_code]
            for delivery in month_range(first, last):
                result.append(
                    ContractDefinition(
                        root=root_code,
                        ticker=root.ticker(delivery),
                        delivery_month=delivery,
                        fallback_expiry=approximate_expiry(root_code, delivery),
                    )
                )
        return tuple(result)


def _require_section(payload: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    section = payload.get(name)
    if not isinstance(section, Mapping):
        raise TechnicalConfigError(f"Missing [{name}] section")
    return section


def _load_spreads(library_path: Path, legs_path: Path) -> tuple[SpreadSpec, ...]:
    if not library_path.is_file():
        raise TechnicalConfigError(f"Spread library not found: {library_path}")
    if not legs_path.is_file():
        raise TechnicalConfigError(f"Spread leg library not found: {legs_path}")

    legs_by_id: dict[str, list[SpreadLeg]] = {}
    with legs_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=2):
            spread_id = str(row.get("spread_id") or "").strip().upper()
            root = str(row.get("root") or "").strip().upper()
            if not spread_id or root not in ROOTS:
                raise TechnicalConfigError(
                    f"{legs_path.name} row {row_number}: invalid spread_id/root"
                )
            sign = int(row.get("sign") or 0)
            contracts = int(row.get("contracts") or 0)
            if sign not in {-1, 1} or contracts <= 0:
                raise TechnicalConfigError(
                    f"{legs_path.name} row {row_number}: sign must be +/-1 and contracts positive"
                )
            price_weight = float(row.get("price_weight") or 1.0)
            if price_weight <= 0:
                raise TechnicalConfigError(
                    f"{legs_path.name} row {row_number}: price_weight must be positive"
                )
            leg = SpreadLeg(
                spread_id=spread_id,
                leg_order=int(row.get("leg_order") or 0),
                root=root,
                selection_mode=str(row.get("selection_mode") or "delivery").strip().lower(),
                delivery_offset=int(row.get("delivery_offset") or 0),
                rank=int(row.get("rank") or 1),
                sign=sign,
                contracts=contracts,
                price_weight=price_weight,
                description=str(row.get("description") or "").strip(),
            )
            legs_by_id.setdefault(spread_id, []).append(leg)

    result: list[SpreadSpec] = []
    seen: set[str] = set()
    with library_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=2):
            spread_id = str(row.get("spread_id") or "").strip().upper()
            if not spread_id or spread_id in seen:
                raise TechnicalConfigError(
                    f"{library_path.name} row {row_number}: duplicate or blank spread_id"
                )
            seen.add(spread_id)
            legs = tuple(sorted(legs_by_id.get(spread_id, ()), key=lambda item: item.leg_order))
            if len(legs) < 2:
                raise TechnicalConfigError(f"{spread_id} requires at least two legs")
            result.append(
                SpreadSpec(
                    spread_id=spread_id,
                    enabled=_truthy(row.get("enabled")),
                    display_name=str(row.get("display_name") or spread_id).strip(),
                    family=str(row.get("family") or "Other").strip(),
                    anchor_root=str(row.get("anchor_root") or "").strip().upper(),
                    anchor_rank=int(row.get("anchor_rank") or 1),
                    unit=str(row.get("unit") or "USD/bbl").strip(),
                    core=_truthy(row.get("core")),
                    model_enabled=(
                        True
                        if str(row.get("model_enabled") or "").strip() == ""
                        else _truthy(row.get("model_enabled"))
                    ),
                    complexity_tier=int(row.get("complexity_tier") or 1),
                    algebra_group=str(
                        row.get("algebra_group") or spread_id
                    ).strip().upper(),
                    notes=str(row.get("notes") or "").strip(),
                    legs=legs,
                )
            )
    unused = sorted(set(legs_by_id) - seen)
    if unused:
        raise TechnicalConfigError(f"Spread legs have no library row: {', '.join(unused)}")
    return tuple(item for item in result if item.enabled)


def load_technical_config(path: str | Path) -> TechnicalConfig:
    source = Path(path).resolve()
    if not source.is_file():
        raise TechnicalConfigError(f"Technical configuration not found: {source}")
    with source.open("rb") as handle:
        payload = tomllib.load(handle)

    system = _require_section(payload, "system")
    bloomberg = _require_section(payload, "bloomberg")
    liquidity = _require_section(payload, "liquidity")
    backtest = _require_section(payload, "backtest")
    indicators = _require_section(payload, "indicators")
    raw_roots = _require_section(payload, "roots")

    roots: dict[str, RootSpec] = {}
    for root_code in ROOTS:
        row = raw_roots.get(root_code)
        if not isinstance(row, Mapping):
            raise TechnicalConfigError(f"Missing [roots.{root_code}] section")
        roots[root_code] = RootSpec(
            root=root_code,
            name=str(row.get("name") or root_code),
            ticker_template=str(row.get("ticker_template") or ""),
            generic_template=str(row.get("generic_template") or ""),
            native_unit=str(row.get("native_unit") or ""),
            price_to_usd_bbl=float(row.get("price_to_usd_bbl") or 0),
            contract_size_native=float(row.get("contract_size_native") or 0),
            contract_barrels=float(row.get("contract_barrels") or 0),
            tick_size_native=float(row.get("tick_size_native") or 0),
            commission_per_contract_side=float(row.get("commission_per_contract_side") or 0),
            slippage_ticks_per_side=float(row.get("slippage_ticks_per_side") or 0),
        )
        root = roots[root_code]
        if min(
            root.price_to_usd_bbl,
            root.contract_size_native,
            root.contract_barrels,
            root.tick_size_native,
        ) <= 0:
            raise TechnicalConfigError(f"Root {root_code} has non-positive conversion metadata")

    settings = SystemSettings(
        project_name=str(system.get("project_name") or "Technical System"),
        model_version=str(system.get("model_version") or "0"),
        timezone=str(system.get("timezone") or "America/New_York"),
        session_start=_parse_time(system.get("session_start"), "session_start"),
        session_end=_parse_time(system.get("session_end"), "session_end"),
        bar_interval_minutes=int(system.get("bar_interval_minutes") or 30),
        complete_bars_per_session=int(system.get("complete_bars_per_session") or 13),
        rolling_intraday_months=int(system.get("rolling_intraday_months") or 12),
        daily_history_start=_parse_date(system.get("daily_history_start"), "daily_history_start"),
        forward_curve_months=int(system.get("forward_curve_months") or 16),
        expiry_flat_sessions=int(system.get("expiry_flat_sessions") or 3),
        pull_overlap_days=int(system.get("pull_overlap_days") or 5),
        pull_chunk_days=int(system.get("pull_chunk_days") or 28),
        max_concurrent_requests=int(system.get("max_concurrent_requests") or 2),
        minimum_preliminary_sessions=int(system.get("minimum_preliminary_sessions") or 60),
        minimum_production_sessions=int(system.get("minimum_production_sessions") or 300),
        output_history_sessions=int(system.get("output_history_sessions") or 30),
        maximum_model_age_sessions=int(
            system.get("maximum_model_age_sessions") or 5
        ),
        open_browser=bool(system.get("open_browser", True)),
    )
    session_minutes = (
        datetime.combine(date.min, settings.session_end)
        - datetime.combine(date.min, settings.session_start)
    ).seconds // 60
    expected_bars = session_minutes // settings.bar_interval_minutes
    if expected_bars != settings.complete_bars_per_session:
        raise TechnicalConfigError(
            "Session window and interval imply "
            f"{expected_bars} complete bars, not {settings.complete_bars_per_session}"
        )
    if settings.expiry_flat_sessions < 3:
        raise TechnicalConfigError("expiry_flat_sessions cannot be less than 3")
    if not 2 <= settings.forward_curve_months <= 24:
        raise TechnicalConfigError("forward_curve_months must be between 2 and 24")
    if not 1 <= settings.maximum_model_age_sessions <= 20:
        raise TechnicalConfigError(
            "maximum_model_age_sessions must be between 1 and 20"
        )

    bloomberg_settings = BloombergSettings(
        host=str(bloomberg.get("host") or "localhost"),
        port=int(bloomberg.get("port") or 8194),
        request_pool_size=int(bloomberg.get("request_pool_size") or 2),
        request_timeout_seconds=int(
            bloomberg.get("request_timeout_seconds") or 120
        ),
        heartbeat_seconds=int(bloomberg.get("heartbeat_seconds") or 5),
        retry_max_retries=int(bloomberg.get("retry_max_retries", 1)),
        maximum_trade_failure_rate=float(
            bloomberg.get("maximum_trade_failure_rate", 0.05)
        ),
        freshness_grace_minutes=int(
            bloomberg.get("freshness_grace_minutes", 15)
        ),
        event_type=str(bloomberg.get("event_type") or "TRADE").upper(),
        pull_bid_ask_bars=bool(bloomberg.get("pull_bid_ask_bars", True)),
        gap_fill_initial_bar=bool(bloomberg.get("gap_fill_initial_bar", False)),
        intraday_retention_warning_days=int(
            bloomberg.get("intraday_retention_warning_days") or 160
        ),
        reference_fields=tuple(str(item) for item in bloomberg.get("reference_fields", ())),
        daily_fields=tuple(str(item) for item in bloomberg.get("daily_fields", ())),
    )
    if not 10 <= bloomberg_settings.request_timeout_seconds <= 600:
        raise TechnicalConfigError(
            "bloomberg.request_timeout_seconds must be between 10 and 600"
        )
    if not 1 <= bloomberg_settings.heartbeat_seconds <= 60:
        raise TechnicalConfigError(
            "bloomberg.heartbeat_seconds must be between 1 and 60"
        )
    if (
        bloomberg_settings.heartbeat_seconds
        >= bloomberg_settings.request_timeout_seconds
    ):
        raise TechnicalConfigError(
            "bloomberg.heartbeat_seconds must be less than request_timeout_seconds"
        )
    if not 0 <= bloomberg_settings.retry_max_retries <= 3:
        raise TechnicalConfigError(
            "bloomberg.retry_max_retries must be between 0 and 3"
        )
    if not 0 <= bloomberg_settings.maximum_trade_failure_rate <= 0.25:
        raise TechnicalConfigError(
            "bloomberg.maximum_trade_failure_rate must be between 0 and 0.25"
        )
    if not 0 <= bloomberg_settings.freshness_grace_minutes <= 60:
        raise TechnicalConfigError(
            "bloomberg.freshness_grace_minutes must be between 0 and 60"
        )
    liquidity_settings = LiquiditySettings(
        capture_seconds=int(liquidity.get("capture_seconds") or 0),
        depth_mode=str(liquidity.get("depth_mode") or "auto").lower(),
        top_of_book_fields=tuple(str(item) for item in liquidity.get("top_of_book_fields", ())),
        depth_snapshot_max_age_minutes=int(liquidity.get("depth_snapshot_max_age_minutes") or 15),
        minimum_relative_volume=float(liquidity.get("minimum_relative_volume") or 0),
        maximum_bid_ask_ticks=float(liquidity.get("maximum_bid_ask_ticks") or 0),
        minimum_leg_alignment=float(liquidity.get("minimum_leg_alignment") or 0),
        minimum_volume_coverage=float(liquidity.get("minimum_volume_coverage") or 0),
        maximum_volume_participation=float(
            liquidity.get("maximum_volume_participation") or 0.01
        ),
        require_true_l2_for_enter=bool(liquidity.get("require_true_l2_for_enter", False)),
        cap_confidence_without_depth=float(liquidity.get("cap_confidence_without_depth") or 1),
    )
    backtest_settings = BacktestSettings(**{field: backtest[field] for field in BacktestSettings.__dataclass_fields__})
    indicator_settings = IndicatorSettings(**{field: indicators[field] for field in IndicatorSettings.__dataclass_fields__})
    if backtest_settings.adaptive_horizon_bars < 1:
        raise TechnicalConfigError("adaptive_horizon_bars must be positive")
    if backtest_settings.lockbox_sessions != 30:
        raise TechnicalConfigError(
            "lockbox_sessions must remain exactly 30 completed sessions"
        )
    minimum_backtest_sessions = (
        backtest_settings.train_sessions
        + backtest_settings.validation_sessions
        + backtest_settings.embargo_sessions
        + backtest_settings.test_sessions
        + backtest_settings.lockbox_sessions
    )
    if settings.minimum_production_sessions < minimum_backtest_sessions:
        raise TechnicalConfigError(
            "minimum_production_sessions must cover train, validation, embargo, "
            "OOS test, and the final 30-session lockbox"
        )
    if not 0 <= backtest_settings.parallel_workers <= 16:
        raise TechnicalConfigError("parallel_workers must be between 0 and 16")
    if backtest_settings.adaptive_min_observations < 20:
        raise TechnicalConfigError("adaptive_min_observations must be at least 20")
    if not 0 < backtest_settings.adaptive_learning_rate <= 1:
        raise TechnicalConfigError("adaptive_learning_rate must be in (0, 1]")
    if not 0 <= backtest_settings.adaptive_uniform_shrinkage < 1:
        raise TechnicalConfigError("adaptive_uniform_shrinkage must be in [0, 1)")
    if not 0 < backtest_settings.adaptive_max_expert_weight <= 1:
        raise TechnicalConfigError("adaptive_max_expert_weight must be in (0, 1]")
    if not 1 <= backtest_settings.maximum_new_trades_per_session <= 10:
        raise TechnicalConfigError(
            "maximum_new_trades_per_session must be between 1 and 10"
        )
    if indicator_settings.seasonality_min_prior_years < 1:
        raise TechnicalConfigError("seasonality_min_prior_years must be positive")
    if not 1.0 <= indicator_settings.tail_event_z <= 6.0:
        raise TechnicalConfigError("tail_event_z must be between 1.0 and 6.0")
    if not indicator_settings.tail_event_z < indicator_settings.extreme_tod_shock_z <= 12.0:
        raise TechnicalConfigError(
            "extreme_tod_shock_z must be greater than tail_event_z and at most 12.0"
        )
    if not -8.0 <= indicator_settings.volume_dryness_z <= -0.5:
        raise TechnicalConfigError("volume_dryness_z must be between -8.0 and -0.5")
    if not 0.01 <= indicator_settings.tail_cluster_rate <= 0.50:
        raise TechnicalConfigError("tail_cluster_rate must be between 0.01 and 0.50")
    if not 1.0 <= indicator_settings.vol_expansion_ratio <= 5.0:
        raise TechnicalConfigError("vol_expansion_ratio must be between 1.0 and 5.0")
    if not 1.0 <= indicator_settings.impact_stress_ratio <= 10.0:
        raise TechnicalConfigError("impact_stress_ratio must be between 1.0 and 10.0")

    manual_spreads = _load_spreads(
        source.with_name("spread_library.csv"), source.with_name("spread_legs.csv")
    )
    # Preserve manual aliases and special research structures, then add every
    # missing canonical curve vector.  Algebra groups prevent a front-month
    # legacy alias from being counted twice as an independent hypothesis.
    seen_groups = {item.algebra_group for item in manual_spreads}
    generated = tuple(
        item
        for item in _generated_curve_spreads(settings.forward_curve_months)
        if item.algebra_group not in seen_groups
    )
    spreads = manual_spreads + generated
    for spread in spreads:
        if spread.anchor_root not in ROOTS:
            raise TechnicalConfigError(f"{spread.spread_id} has invalid anchor root")
        if not 1 <= spread.anchor_rank <= settings.forward_curve_months:
            raise TechnicalConfigError(
                f"{spread.spread_id} anchor_rank must be between 1 and "
                f"{settings.forward_curve_months}"
            )
        if spread.complexity_tier not in {1, 2, 3, 4}:
            raise TechnicalConfigError(
                f"{spread.spread_id} complexity_tier must be 1 through 4"
            )

    return TechnicalConfig(
        source_path=source,
        system=settings,
        bloomberg=bloomberg_settings,
        liquidity=liquidity_settings,
        backtest=backtest_settings,
        indicators=indicator_settings,
        roots=roots,
        spreads=spreads,
    )


def schema_summary(config: TechnicalConfig) -> dict[str, object]:
    return {
        "model_version": config.system.model_version,
        "timezone": config.system.timezone,
        "session": f"{config.system.session_start:%H:%M}-{config.system.session_end:%H:%M}",
        "bar_interval_minutes": config.system.bar_interval_minutes,
        "daily_history_start": config.system.daily_history_start.isoformat(),
        "forward_curve_months": config.system.forward_curve_months,
        "roots": list(config.roots),
        "spreads": [item.spread_id for item in config.spreads],
        "expiry_flat_sessions": config.system.expiry_flat_sessions,
    }


__all__ = [
    "BacktestSettings",
    "BloombergSettings",
    "ContractDefinition",
    "IndicatorSettings",
    "LiquiditySettings",
    "MONTH_CODES",
    "ROOTS",
    "RootSpec",
    "SpreadLeg",
    "SpreadSpec",
    "SystemSettings",
    "TechnicalConfig",
    "TechnicalConfigError",
    "add_months",
    "approximate_expiry",
    "business_day_offset",
    "exchange_holidays",
    "exchange_session_offset",
    "expected_latest_exchange_session",
    "load_technical_config",
    "month_range",
    "schema_summary",
]
