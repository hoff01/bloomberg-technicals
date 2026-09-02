"""Human-readable trade codes and unit-safe display surfaces."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

from app.technical_config import SpreadSpec


GASOIL_BBL_PER_MT = 7.45
USD_BBL_TO_CPG_DIVISOR = 0.42
ROOT_LABELS = {
    "HO": "HO",
    "CL": "WTI",
    "CO": "Brent",
    "QS": "Gasoil",
}


def _date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value not in (None, ""):
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    return None


def _months(spread: SpreadSpec, row: Mapping[str, Any]) -> list[date]:
    found: list[date] = []
    for index, _leg in enumerate(spread.legs, start=1):
        month = _date(row.get(f"leg{index}_delivery_month"))
        if month is not None and month not in found:
            found.append(month)
    return sorted(found)


def _month_path(months: list[date], *, include_year: bool) -> str:
    if not months:
        return "Unresolved"
    return "/".join(
        item.strftime("%b%y") if include_year else item.strftime("%b")
        for item in months
    )


def _root_pair(spread: SpreadSpec) -> str:
    roots: list[str] = []
    for leg in spread.legs:
        if leg.root not in roots:
            roots.append(leg.root)
    return "/".join(ROOT_LABELS.get(root, root) for root in roots)


def trade_code_fields(
    spread: SpreadSpec, row: Mapping[str, Any]
) -> dict[str, object]:
    """Return full/short contract labels without parsing one-digit tickers."""

    months = _months(spread, row)
    root_set = {leg.root for leg in spread.legs}
    month_full = _month_path(months, include_year=True)
    same_year = bool(months) and len({item.year for item in months}) == 1
    month_short = _month_path(months, include_year=not same_year)
    pair = _root_pair(spread)
    leg_count = len(spread.legs)
    is_hogo = root_set == {"HO", "QS"}
    is_ho_only = root_set == {"HO"}
    is_crack = "Crack" in spread.family
    if is_hogo:
        suffix = (
            "HOGO (HO-Gasoil)"
            if leg_count == 2
            else "HOGO Box"
            if leg_count == 4
            else "HOGO Fly"
            if leg_count == 6
            else "HOGO Structure"
        )
    elif is_crack:
        suffix = f"{pair} Crack"
        if "Fly" in spread.family:
            suffix += " Fly"
        elif "Condor" in spread.family:
            suffix += " Condor"
        elif "Curve" in spread.family or leg_count == 4:
            suffix += " Box"
    elif spread.family in {"Calendar", "Fly", "Condor"}:
        suffix = f"{pair} {spread.family}"
    elif "Box" in spread.family or leg_count >= 4:
        suffix = f"{pair} Box"
    else:
        suffix = pair
    securities = [
        str(row.get(f"leg{index}_security") or "")
        for index in range(1, leg_count + 1)
    ]
    securities = [item for item in securities if item]
    display_unit = (
        "cpg"
        if is_hogo or is_ho_only
        else "USD/bbl"
        if is_crack
        else spread.unit
    )
    display_level_factor = (
        1.0 / USD_BBL_TO_CPG_DIVISOR
        if is_hogo or is_ho_only
        else 1.0
    )
    quote_convention = (
        "HOGO_CPG"
        if is_hogo
        else "HO_CPG"
        if is_ho_only
        else "CRACK_USD_BBL"
        if is_crack
        else "NORMALIZED_USD_BBL"
    )
    conversion_method = (
        "HO cpg minus gasoil cpg; gasoil cpg = USD/MT / 7.45 / 0.42"
        if is_hogo
        else "HO cpg = normalized USD/bbl / 0.42 = native USD/gal x 100"
        if is_ho_only
        else (
            "Crack USD/bbl; HO USD/gal x 42 and gasoil USD/MT / 7.45 "
            "before leg weighting"
        )
        if is_crack
        else "Normalized USD/bbl package quote"
    )
    return {
        "trade_code": f"{month_full} {suffix}",
        "trade_code_short": f"{month_short} {suffix}",
        "contract_codes": " | ".join(securities),
        "contract_months": " | ".join(item.isoformat() for item in months),
        "structure_roots": "|".join(dict.fromkeys(leg.root for leg in spread.legs)),
        "calculation_unit": spread.unit,
        "display_unit": display_unit,
        "display_level_factor": display_level_factor,
        "quote_convention": quote_convention,
        "conversion_method": conversion_method,
    }


__all__ = [
    "GASOIL_BBL_PER_MT",
    "ROOT_LABELS",
    "USD_BBL_TO_CPG_DIVISOR",
    "trade_code_fields",
]
