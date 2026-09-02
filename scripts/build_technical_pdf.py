#!/usr/bin/env python3
"""Build one clean, product-split PDF from validated technical outputs."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import html
import json
import math
import os
from pathlib import Path
import statistics
import sys
import tempfile
from typing import Any, Iterable, Mapping, Sequence

import polars as pl
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.technical_backtest import BASE_EXPERT_IDS  # noqa: E402
from app.technical_config import load_technical_config  # noqa: E402


PAGE_SIZE = landscape(letter)
PAGE_WIDTH, PAGE_HEIGHT = PAGE_SIZE
LEFT_MARGIN = 0.48 * inch
RIGHT_MARGIN = 0.48 * inch
TOP_MARGIN = 0.62 * inch
BOTTOM_MARGIN = 0.45 * inch
CONTENT_WIDTH = PAGE_WIDTH - LEFT_MARGIN - RIGHT_MARGIN

NAVY = colors.HexColor("#153550")
BLUE = colors.HexColor("#1565C0")
ORANGE = colors.HexColor("#EF6C35")
INK = colors.HexColor("#1D2A35")
MUTED = colors.HexColor("#607383")
LINE = colors.HexColor("#D6E0E7")
PALE = colors.HexColor("#F5F8FA")
PALE_BLUE = colors.HexColor("#EAF2F8")
PALE_ORANGE = colors.HexColor("#FFF1E8")
GREEN = colors.HexColor("#287A4B")
RED = colors.HexColor("#B83A3A")
AMBER = colors.HexColor("#A96A08")
WHITE = colors.white

PRODUCT_ORDER = ("HO", "CL", "CO", "QS")
PRODUCT_LABELS = {
    "HO": "NY Harbor ULSD",
    "CL": "WTI Crude",
    "CO": "Brent Crude",
    "QS": "ICE Low Sulphur Gasoil",
}
STATUS_PRIORITY = {
    "BUY": 0,
    "SELL": 0,
    "WATCH": 1,
    "FLAT": 2,
    "NO TRADE": 3,
    "REGIME BLOCK": 4,
    "MODEL STALE": 5,
    "EXPIRY BLOCK": 6,
    "DATA BLOCK": 7,
    "DEPTH BLOCK": 8,
    "ANALYTIC ONLY": 9,
}
EXPERT_LABELS = {
    "ROBUST_MEAN_REVERSION": "Robust mean reversion",
    "TREND_BREAKOUT": "Trend breakout",
    "VOLATILITY_SQUEEZE": "Volatility squeeze",
    "SESSION_VWAP_REVERSION": "Session VWAP reversion",
    "ERROR_CORRECTION_RESIDUAL": "Seasonal / error correction",
    "STABILITY_REVERSION": "Stability-qualified reversion",
    "FLOW_DIVERGENCE": "Flow divergence",
}
EXPERT_INPUTS = {
    "ROBUST_MEAN_REVERSION": "Median/MAD z, RSI, efficiency, stability",
    "TREND_BREAKOUT": "Donchian close break, MACD, efficiency, relative volume",
    "VOLATILITY_SQUEEZE": "Bollinger compression, close break, volume and PVO",
    "SESSION_VWAP_REVERSION": "Session VWAP deviation, close volatility, time slot",
    "ERROR_CORRECTION_RESIDUAL": "Prior-year seasonal move, support and confidence",
    "STABILITY_REVERSION": "Robust z, variance ratio and mean-reversion stability",
    "FLOW_DIVERGENCE": "Robust z, signed-volume proxy and effort-versus-result",
}


def _safe(value: object) -> str:
    text = "" if value is None else str(value)
    for source, target in {
        "\u2014": "-",
        "\u2013": "-",
        "\u2212": "-",
        "\u2264": "<=",
        "\u2265": ">=",
        "\u00b1": "+/-",
        "\u2022": "-",
        "\u2026": "...",
    }.items():
        text = text.replace(source, target)
    return html.escape(text)


def _number(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _fmt(value: object, digits: int = 3) -> str:
    number = _number(value)
    return "-" if number is None else f"{number:,.{digits}f}"


def _pct(value: object, digits: int = 0) -> str:
    number = _number(value)
    return "-" if number is None else f"{100.0 * number:.{digits}f}%"


def _money(value: object) -> str:
    number = _number(value)
    if number is None:
        return "-"
    if abs(number) < 0.005:
        number = 0.0
    return f"${number:,.0f}"


def _integer(value: object) -> str:
    number = _number(value)
    return "-" if number is None else f"{int(round(number)):,}"


def _median(values: Iterable[object]) -> float | None:
    numbers = [value for item in values if (value := _number(item)) is not None]
    return statistics.median(numbers) if numbers else None


def _read_csv(path: Path) -> pl.DataFrame:
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(f"Required report input is missing: {path}")
    return pl.read_csv(path, try_parse_dates=True, infer_schema_length=10_000)


def _product_rows(root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    dist = root / "dist"
    summaries = _read_csv(dist / "technical_structure_summaries.csv")
    signals = _read_csv(dist / "technical_live_signals.csv")
    library = _read_csv(dist / "technical_spread_library.csv")
    required_signal_columns = {
        "spread_id",
        "adaptive_score",
        "adaptive_observations",
        "adaptive_top_expert",
        "adaptive_top_weight",
        "strategy_votes_long",
        "strategy_votes_short",
        "pattern_state",
        "pattern_strength",
        "pattern_agreement",
        "pattern_components",
    }
    missing = sorted(required_signal_columns - set(signals.columns))
    if missing:
        raise ValueError(
            "Current signals predate the dynamic-pattern release; rerun train or "
            "score before exporting the PDF. Missing: " + ", ".join(missing)
        )
    summary_drop = [
        column
        for column in required_signal_columns - {"spread_id"}
        if column in summaries.columns
    ]
    if summary_drop:
        summaries = summaries.drop(summary_drop)
    joined = (
        summaries.join(
            signals.select(sorted(required_signal_columns)),
            on="spread_id",
            how="left",
            validate="1:1",
        )
        .join(
            library.select("spread_id", "anchor_root", "normalized_price_formula"),
            on="spread_id",
            how="left",
            validate="1:1",
        )
    )
    if joined["anchor_root"].null_count():
        raise ValueError("Every PDF row must resolve to one configured product root")
    model_summary = json.loads(
        (dist / "technical_model_summary.json").read_text(encoding="utf-8")
    )
    return joined.to_dicts(), model_summary


def _styles() -> dict[str, ParagraphStyle]:
    sample = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "ReportTitle",
            parent=sample["Title"],
            fontName="Helvetica-Bold",
            fontSize=27,
            leading=31,
            textColor=NAVY,
            spaceAfter=10,
        ),
        "subtitle": ParagraphStyle(
            "ReportSubtitle",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=12,
            leading=17,
            textColor=MUTED,
            spaceAfter=12,
        ),
        "chapter": ParagraphStyle(
            "ChapterHeading",
            parent=sample["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=23,
            textColor=NAVY,
            spaceBefore=2,
            spaceAfter=8,
            keepWithNext=True,
        ),
        "section": ParagraphStyle(
            "SectionHeading",
            parent=sample["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=15,
            textColor=NAVY,
            spaceBefore=8,
            spaceAfter=5,
            keepWithNext=True,
        ),
        "body": ParagraphStyle(
            "BodyTextClean",
            parent=sample["BodyText"],
            fontName="Helvetica",
            fontSize=8.3,
            leading=11.5,
            textColor=INK,
            spaceAfter=5,
        ),
        "small": ParagraphStyle(
            "SmallText",
            parent=sample["BodyText"],
            fontName="Helvetica",
            fontSize=7,
            leading=9.2,
            textColor=MUTED,
        ),
        "table": ParagraphStyle(
            "TableBody",
            parent=sample["BodyText"],
            fontName="Helvetica",
            fontSize=6.2,
            leading=7.5,
            textColor=INK,
        ),
        "table_bold": ParagraphStyle(
            "TableBodyBold",
            parent=sample["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=6.2,
            leading=7.5,
            textColor=INK,
        ),
        "table_header": ParagraphStyle(
            "TableHeader",
            parent=sample["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=6.1,
            leading=7.2,
            textColor=WHITE,
            alignment=TA_CENTER,
        ),
        "card_label": ParagraphStyle(
            "CardLabel",
            parent=sample["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=6.7,
            leading=8,
            textColor=MUTED,
            alignment=TA_CENTER,
        ),
        "card_value": ParagraphStyle(
            "CardValue",
            parent=sample["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=15,
            leading=17,
            textColor=NAVY,
            alignment=TA_CENTER,
        ),
        "right": ParagraphStyle(
            "RightSmall",
            parent=sample["BodyText"],
            fontName="Helvetica",
            fontSize=6.2,
            leading=7.5,
            textColor=INK,
            alignment=TA_RIGHT,
        ),
    }


class ProductReportTemplate(BaseDocTemplate):
    def __init__(self, filename: str, *, report_mode: str, **kwargs: Any) -> None:
        super().__init__(
            filename,
            pagesize=PAGE_SIZE,
            leftMargin=LEFT_MARGIN,
            rightMargin=RIGHT_MARGIN,
            topMargin=TOP_MARGIN,
            bottomMargin=BOTTOM_MARGIN,
            title="Bloomberg Technical Product Report",
            author="Bloomberg Technicals",
            subject="Product-split technical patterns, targets, and backtest evidence",
            **kwargs,
        )
        self.report_mode = report_mode
        frame = Frame(
            LEFT_MARGIN,
            BOTTOM_MARGIN,
            CONTENT_WIDTH,
            PAGE_HEIGHT - TOP_MARGIN - BOTTOM_MARGIN,
            id="report-body",
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        )
        self.addPageTemplates(PageTemplate("report", [frame], onPage=self._page))

    def _page(self, canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFillColor(NAVY)
        canvas.rect(0, PAGE_HEIGHT - 24, PAGE_WIDTH, 24, stroke=0, fill=1)
        canvas.setFillColor(WHITE)
        canvas.setFont("Helvetica-Bold", 7.2)
        canvas.drawString(LEFT_MARGIN, PAGE_HEIGHT - 16, "BLOOMBERG TECHNICALS")
        canvas.setFont("Helvetica", 7)
        canvas.drawRightString(
            PAGE_WIDTH - RIGHT_MARGIN,
            PAGE_HEIGHT - 16,
            f"{self.report_mode} - PRODUCT REPORT",
        )
        canvas.setStrokeColor(LINE)
        canvas.line(LEFT_MARGIN, 20, PAGE_WIDTH - RIGHT_MARGIN, 20)
        canvas.setFillColor(MUTED)
        canvas.setFont("Helvetica", 6.7)
        canvas.drawString(LEFT_MARGIN, 9, "Decision support only - human review required")
        canvas.drawRightString(
            PAGE_WIDTH - RIGHT_MARGIN,
            9,
            f"Page {doc.page}",
        )
        canvas.restoreState()

    def afterFlowable(self, flowable: Any) -> None:
        if not isinstance(flowable, Paragraph):
            return
        if flowable.style.name not in {"ChapterHeading", "SectionHeading"}:
            return
        level = 0 if flowable.style.name == "ChapterHeading" else 1
        text = flowable.getPlainText()
        key = getattr(flowable, "_bookmark_name", f"section-{self.page}-{level}")
        self.canv.bookmarkPage(key)
        self.canv.addOutlineEntry(text, key, level=level, closed=False)
        self.notify("TOCEntry", (level, text, self.page, key))


def _paragraph(value: object, style: ParagraphStyle) -> Paragraph:
    return Paragraph(_safe(value), style)


def _heading(
    value: str,
    style: ParagraphStyle,
    bookmark: str,
) -> Paragraph:
    paragraph = _paragraph(value, style)
    paragraph._bookmark_name = bookmark  # type: ignore[attr-defined]
    return paragraph


def _cards(items: Sequence[tuple[str, str]], styles: Mapping[str, ParagraphStyle]) -> Table:
    data = [
        [_paragraph(label, styles["card_label"]) for label, _ in items],
        [_paragraph(value, styles["card_value"]) for _, value in items],
    ]
    table = Table(data, colWidths=[CONTENT_WIDTH / len(items)] * len(items))
    commands: list[tuple[Any, ...]] = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOX", (0, 0), (-1, -1), 0.6, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, LINE),
        ("BACKGROUND", (0, 0), (-1, -1), PALE),
        ("TOPPADDING", (0, 0), (-1, 0), 7),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
        ("TOPPADDING", (0, 1), (-1, 1), 1),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 8),
    ]
    table.setStyle(TableStyle(commands))
    return table


def _styled_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
    widths: Sequence[float],
    styles: Mapping[str, ParagraphStyle],
    *,
    right_columns: Sequence[int] = (),
    long: bool = False,
) -> Table:
    data: list[list[Any]] = [
        [_paragraph(header, styles["table_header"]) for header in headers]
    ]
    for row in rows:
        rendered: list[Any] = []
        for index, value in enumerate(row):
            style = styles["right"] if index in right_columns else styles["table"]
            rendered.append(_paragraph(value, style))
        data.append(rendered)
    # The ordinary Table splitter is fast enough for this report and reliably
    # respects the frame top on short final continuation pages. LongTable's
    # optimized final fragment can intrude into the page banner.
    table = Table(
        data,
        colWidths=list(widths),
        repeatRows=1,
        hAlign="LEFT",
        splitByRow=1,
    )
    commands: list[tuple[Any, ...]] = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, LINE),
        ("TOPPADDING", (0, 0), (-1, 0), 4),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
        ("TOPPADDING", (0, 1), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
    ]
    for row_index in range(1, len(data)):
        if row_index % 2 == 0:
            commands.append(("BACKGROUND", (0, row_index), (-1, row_index), PALE))
    table.setStyle(TableStyle(commands))
    return table


def _top_rows(rows: Sequence[Mapping[str, Any]], limit: int) -> list[Mapping[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            STATUS_PRIORITY.get(str(row.get("current_status")), 99),
            -abs(_number(row.get("pattern_strength")) or 0.0),
            -(_number(row.get("confidence")) or 0.0),
            str(row.get("spread_id")),
        ),
    )[:limit]


def _pattern_rows(rows: Sequence[Mapping[str, Any]], limit: int) -> list[Mapping[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            -abs(_number(row.get("pattern_strength")) or 0.0),
            -(_number(row.get("pattern_agreement")) or 0.0),
            str(row.get("spread_id")),
        ),
    )[:limit]


def _chapter_story(
    product: str,
    rows: Sequence[Mapping[str, Any]],
    styles: Mapping[str, ParagraphStyle],
) -> list[Any]:
    label = PRODUCT_LABELS[product]
    status_counts = Counter(str(row.get("current_status") or "UNKNOWN") for row in rows)
    pattern_counts = Counter(str(row.get("pattern_state") or "UNKNOWN") for row in rows)
    risk_counts = Counter(str(row.get("advanced_risk_regime") or "UNKNOWN") for row in rows)
    top_risk = ", ".join(
        f"{name.replace('_', ' ').title()} {count}"
        for name, count in risk_counts.most_common(3)
    )
    consensus = sum(
        count for name, count in pattern_counts.items() if name.endswith("CONSENSUS")
    )
    median_confidence = _median(row.get("confidence") for row in rows)
    recent_net = sum(_number(row.get("recent_30_net_pnl_usd")) or 0.0 for row in rows)
    story: list[Any] = [
        PageBreak(),
        _heading(f"{product} - {label}", styles["chapter"], f"product-{product}"),
        _paragraph(
            f"Product chapter for {len(rows):,} configured structures anchored to {product}. "
            "Patterns are a transparent view of the frozen adaptive combination and are not a separate signal.",
            styles["body"],
        ),
        _cards(
            [
                ("Structures", f"{len(rows):,}"),
                ("Actionable", f"{status_counts['BUY'] + status_counts['SELL']:,}"),
                ("Watch", f"{status_counts['WATCH']:,}"),
                ("Consensus patterns", f"{consensus:,}"),
                ("Median confidence", _pct(median_confidence)),
                ("Latest 30-session net", _money(recent_net)),
            ],
            styles,
        ),
        Spacer(1, 7),
        _paragraph(f"Most common current risk states: {top_risk or 'unavailable'}.", styles["small"]),
        _heading("Current decision levels", styles["section"], f"product-{product}-levels"),
    ]
    opportunity_rows = []
    for row in _top_rows(rows, 14):
        opportunity_rows.append(
            [
                row.get("trade_code"),
                row.get("current_status"),
                row.get("pattern_state"),
                _fmt(row.get("display_current")),
                _fmt(row.get("display_buy_entry")),
                _fmt(row.get("display_sell_entry")),
                _fmt(row.get("display_fair_value")),
                row.get("display_unit"),
                _pct(row.get("confidence")),
                row.get("advanced_risk_regime"),
                row.get("selected_strategy_name") or "Analytic only",
                _money(row.get("recent_30_net_pnl_usd")),
            ]
        )
    story.append(
        _styled_table(
            [
                "Trade code",
                "Signal",
                "Pattern",
                "Current",
                "Buy <=",
                "Sell >=",
                "Fair",
                "Unit",
                "Conf.",
                "Risk",
                "Selected evidence",
                "30d net",
            ],
            opportunity_rows,
            [105, 40, 70, 39, 39, 39, 39, 28, 34, 58, 105, 50],
            styles,
            right_columns=(3, 4, 5, 6, 8, 11),
        )
    )
    pattern_rows = []
    for row in _pattern_rows(rows, 16):
        pattern_rows.append(
            [
                row.get("trade_code"),
                row.get("pattern_state"),
                _fmt(row.get("pattern_strength"), 2),
                _pct(row.get("pattern_agreement")),
                f"{_integer(row.get('strategy_votes_long'))}L / {_integer(row.get('strategy_votes_short'))}S",
                EXPERT_LABELS.get(
                    str(row.get("adaptive_top_expert")),
                    str(row.get("adaptive_top_expert") or "-"),
                ),
                row.get("regime"),
                row.get("advanced_risk_regime"),
                _pct(row.get("confidence")),
            ]
        )
    pattern_table = _styled_table(
        [
            "Trade code",
            "Pattern state",
            "Strength",
            "Agreement",
            "Votes",
            "Top adaptive expert",
            "Base regime",
            "Risk state",
            "Conf.",
        ],
        pattern_rows,
        [125, 80, 42, 48, 42, 112, 62, 70, 40],
        styles,
        right_columns=(2, 3, 8),
    )
    story.append(
        KeepTogether(
            [
                _heading(
                    "Dynamic pattern synthesis",
                    styles["section"],
                    f"product-{product}-patterns",
                ),
                _paragraph(
                    "The adaptive score is the frozen weighted sum of seven expert votes. "
                    "Consensus requires at least two experts, sufficient delayed outcomes, "
                    "and no change-point alarm. Mixed and fragment states remain descriptive only.",
                    styles["body"],
                ),
                pattern_table,
            ]
        )
    )
    story.extend(
        [
            _heading(
                "Complete product structure sheet",
                styles["section"],
                f"product-{product}-all",
            ),
            _paragraph(
                "Every configured structure is shown. BUY and SELL require validated "
                "directional evidence and all production gates; WATCH is not an order.",
                styles["small"],
            ),
        ]
    )
    complete_rows = []
    for row in sorted(rows, key=lambda item: (str(item.get("family")), str(item.get("spread_id")))):
        complete_rows.append(
            [
                row.get("trade_code"),
                row.get("family"),
                row.get("current_status"),
                _fmt(row.get("display_current")),
                _fmt(row.get("display_buy_entry")),
                _fmt(row.get("display_sell_entry")),
                _fmt(row.get("display_fair_value")),
                row.get("display_unit"),
                row.get("pattern_state"),
                _fmt(row.get("pattern_strength"), 2),
                row.get("advanced_risk_regime"),
                _pct(row.get("confidence")),
                _money(row.get("recent_30_net_pnl_usd")),
                _money(row.get("model_net_pnl_usd")),
            ]
        )
    story.append(
        _styled_table(
            [
                "Trade code",
                "Family",
                "Signal",
                "Current",
                "Buy <=",
                "Sell >=",
                "Fair",
                "Unit",
                "Pattern",
                "Str.",
                "Risk",
                "Conf.",
                "30d net",
                "OOS net",
            ],
            complete_rows,
            [115, 47, 40, 36, 36, 36, 36, 25, 67, 28, 58, 33, 48, 48],
            styles,
            right_columns=(3, 4, 5, 6, 9, 11, 12, 13),
            long=True,
        )
    )
    return story


def _overview_story(
    rows: Sequence[Mapping[str, Any]],
    model_summary: Mapping[str, Any],
    styles: Mapping[str, ParagraphStyle],
    *,
    demo_mode: bool,
) -> list[Any]:
    status_counts = Counter(str(row.get("current_status") or "UNKNOWN") for row in rows)
    report_mode = "DEMO / PAPER DATA" if demo_mode else "LIVE BLOOMBERG"
    selected_oos = model_summary.get("selected_oos") or {}
    portfolio_30 = model_summary.get("portfolio_latest_30_sessions") or {}
    story: list[Any] = [
        Spacer(1, 24),
        _paragraph("BLOOMBERG - XBBG - POLARS - ADAPTIVE RESEARCH", styles["small"]),
        _paragraph("Technical Product Report", styles["title"]),
        _paragraph(
            "15-minute product-split targets, dynamic multi-indicator patterns, "
            "a three-entry daily budget, risk regimes, and frozen out-of-sample evidence.",
            styles["subtitle"],
        ),
    ]
    banner = Table(
        [[_paragraph(report_mode, styles["table_bold"])]],
        colWidths=[2.2 * inch],
        hAlign="LEFT",
    )
    banner.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PALE_ORANGE if demo_mode else PALE_BLUE),
                ("BOX", (0, 0), (-1, -1), 0.6, ORANGE if demo_mode else BLUE),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.extend(
        [
            banner,
            Spacer(1, 13),
            _cards(
                [
                    ("As of", str(model_summary.get("as_of") or "-")),
                    ("Products", "4"),
                    ("Structures", f"{len(rows):,}"),
                    ("Model trials", _integer(model_summary.get("total_trial_count"))),
                    ("Actionable", f"{status_counts['BUY'] + status_counts['SELL']:,}"),
                    ("Watch", f"{status_counts['WATCH']:,}"),
                ],
                styles,
            ),
            Spacer(1, 13),
            _paragraph(
                str(model_summary.get("description") or "No model summary is available."),
                styles["body"],
            ),
            _paragraph(
                "This report is decision support, not an order ticket. Pattern state is "
                "a transparent summary of the frozen adaptive combination; it does not "
                "override validation, data, liquidity, expiry, or model-freshness gates.",
                styles["body"],
            ),
            _cards(
                [
                    ("Selected OOS trades", _integer(selected_oos.get("trades"))),
                    ("Selected OOS net", _money(selected_oos.get("net_pnl_usd"))),
                    ("30d candidates", _integer(portfolio_30.get("candidate_trades"))),
                    ("30d selected", _integer(portfolio_30.get("selected_trades"))),
                    ("30d capped net", _money(portfolio_30.get("net_pnl_usd"))),
                    ("Max new trades/day", _integer(portfolio_30.get("maximum_new_trades_in_session"))),
                ],
                styles,
            ),
            PageBreak(),
            _heading("Contents", styles["chapter"], "contents"),
        ]
    )
    toc = TableOfContents()
    toc.levelStyles = [
        ParagraphStyle(
            "TOC0",
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=15,
            leftIndent=0,
            firstLineIndent=0,
            textColor=NAVY,
        ),
        ParagraphStyle(
            "TOC1",
            fontName="Helvetica",
            fontSize=8,
            leading=12,
            leftIndent=18,
            firstLineIndent=0,
            textColor=MUTED,
        ),
    ]
    story.extend(
        [
            toc,
            PageBreak(),
            _heading(
                "How the dynamic pattern engine works",
                styles["chapter"],
                "pattern-engine",
            ),
            _paragraph(
                "Seven fixed experts combine different indicator families. Each expert "
                "emits long, short, or neutral. After a delayed 26-bar cost-aware outcome, "
                "weights update once per completed session within family and tenor groups. "
                "Weights are shrunk toward uniform, capped, frozen before the final 30-session "
                "lockbox, and reused unchanged by score-only runs.",
                styles["body"],
            ),
        ]
    )
    expert_rows = [
        [EXPERT_LABELS[expert], EXPERT_INPUTS[expert], "Delayed, cost-aware adaptive weight"]
        for expert in BASE_EXPERT_IDS
    ]
    story.extend(
        [
            _styled_table(
                ["Expert", "Combined indicator inputs", "Dynamic treatment"],
                expert_rows,
                [150, 365, 175],
                styles,
            ),
            Spacer(1, 8),
            _paragraph(
                "BULLISH_CONSENSUS and BEARISH_CONSENSUS require the frozen adaptive vote. "
                "CLUSTER, FRAGMENT, and MIXED states disclose partial evidence but cannot "
                "promote a trade by themselves. STRUCTURAL_BREAK fails closed on the CUSUM "
                "change alarm.",
                styles["body"],
            ),
            _paragraph(
                "Core research controls: next-bar-open execution, full package costs, walk-forward "
                "folds with embargoes, 30-session untouched lockbox, D-4 liquidation, deflated "
                "Sharpe trial penalty, and separately frozen current-price scoring.",
                styles["body"],
            ),
        ]
    )
    return story


def _appendix_story(styles: Mapping[str, ParagraphStyle], *, demo_mode: bool) -> list[Any]:
    mode_note = (
        "This generated file uses deterministic paper data and is not tradeable."
        if demo_mode
        else "This file reports live Bloomberg-derived outputs; human review remains mandatory."
    )
    return [
        PageBreak(),
        _heading("Methodology and operating safeguards", styles["chapter"], "methodology"),
        _paragraph(mode_note, styles["body"]),
        _styled_table(
            ["Control", "Production contract"],
            [
                ["Causality", "Completed 15-minute close only; any entry is next-bar open."],
                ["Dynamic learning", "Delayed cost-aware outcomes; family/tenor weights; lockbox freeze."],
                ["Overfitting", "Complete preregistered trial ledger; fold consistency and deflated-Sharpe penalty."],
                ["Liquidity", "All-leg capacity, width, relative volume and labelled depth source."],
                ["Expiry", "Earliest leg risk controls; mandatory D-4 exit; flat before D-3."],
                ["Missing data", "Blocking source gaps fail closed; undefined diagnostics remain null."],
                ["High/low data", "No synthetic spread highs or lows are fabricated from asynchronous legs."],
                ["Pattern state", "Reporting only; it never bypasses validation or execution gates."],
                ["Trade budget", "Maximum three independent new entries per session; one per algebra group."],
                ["Lockbox", "Final 30 completed sessions are evaluation-only and never select the model."],
                ["Crack units", "Every crack level and target is quoted in USD/bbl."],
                ["HO units", "HO-only calendars, flies, and condors are quoted in cpg."],
                ["Gasoil units", "USD/bbl = USD/MT / 7.45; cpg = USD/MT / 7.45 / 0.42."],
            ],
            [145, 545],
            styles,
        ),
        _heading("Bloomberg API readiness", styles["section"], "bloomberg-readiness"),
        _paragraph(
            "Windows setup uses 64-bit Python 3.12 or 3.13 under "
            "%USERPROFILE%\\Pyenvs\\bbg_technical_builder. INSTALL_BLOOMBERG.bat "
            "installs pinned analytics packages and Bloomberg blpapi from Bloomberg's "
            "official package index. CHECK_BLOOMBERG_READY.bat then exercises dated futures "
            "reference data, daily history, 15-minute bars, and current subscription/depth.",
            styles["body"],
        ),
        _paragraph(
            "Bloomberg Terminal must be open and logged in. BDP, BDH, and BDIB are required. "
            "Subscription or true L2 depth depends on entitlement and may be reported as a "
            "warning. Only the receipt produced on the licensed Windows workstation certifies "
            "that workstation's connectivity.",
            styles["body"],
        ),
        _heading("Method references", styles["section"], "references"),
        _paragraph(
            "Newey and West HAC: https://www.nber.org/papers/t0055",
            styles["small"],
        ),
        _paragraph(
            "Andersen and Bollerslev intraday periodicity: https://doi.org/10.1016/S0927-5398(97)00004-2",
            styles["small"],
        ),
        _paragraph(
            "Amihud illiquidity: https://doi.org/10.1016/S1386-4181(01)00024-6",
            styles["small"],
        ),
        _paragraph(
            "Harvey, Liu and Zhu multiple testing: https://doi.org/10.1093/rfs/hhv059",
            styles["small"],
        ),
    ]


def build_pdf(project_root: Path, output: Path, *, mode: str) -> Path:
    rows, model_summary = _product_rows(project_root)
    demo_mode = mode == "demo" or any(bool(row.get("demo_mode")) for row in rows)
    report_mode = "DEMO" if demo_mode else "LIVE BLOOMBERG"
    styles = _styles()
    story = _overview_story(rows, model_summary, styles, demo_mode=demo_mode)
    by_product = {
        product: [row for row in rows if str(row.get("anchor_root")) == product]
        for product in PRODUCT_ORDER
    }
    if any(not group for group in by_product.values()):
        empty = [product for product, group in by_product.items() if not group]
        raise ValueError("PDF product chapters are empty: " + ", ".join(empty))
    for product in PRODUCT_ORDER:
        story.extend(_chapter_story(product, by_product[product], styles))
    story.extend(_appendix_story(styles, demo_mode=demo_mode))

    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.stem}.", suffix=".pdf", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        document = ProductReportTemplate(str(temporary), report_mode=report_mode)
        document.multiBuild(story)
        reader = PdfReader(temporary)
        if len(reader.pages) < 8:
            raise ValueError(f"Product PDF is unexpectedly short: {len(reader.pages)} pages")
        extracted = "\n".join((page.extract_text() or "") for page in reader.pages)
        required_text = {
            "Technical Product Report",
            "How the dynamic pattern engine works",
            "NY Harbor ULSD",
            "WTI Crude",
            "Brent Crude",
            "ICE Low Sulphur Gasoil",
            "Bloomberg API readiness",
        }
        missing_text = sorted(item for item in required_text if item not in extracted)
        if missing_text:
            raise ValueError("PDF validation is missing sections: " + ", ".join(missing_text))
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"{output} ({len(PdfReader(output).pages)} pages)")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--mode", choices=("live", "demo"), required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.project_root.resolve()
    output = (
        args.output.resolve()
        if args.output
        else root / "output" / "pdf" / "Technical_Product_Report.pdf"
    )
    build_pdf(root, output, mode=args.mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
