from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from pypdf import PdfReader

from app.technical_backtest import _pattern_diagnostic
from app.technical_config import load_technical_config
from scripts.build_technical_pdf import PRODUCT_ORDER, _safe, build_pdf


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TechnicalPdfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_technical_config(
            PROJECT_ROOT / "config" / "technical_system.toml"
        )

    def test_pattern_diagnostic_exposes_frozen_consensus_without_new_rule(self) -> None:
        row = {
            "adaptive_score": 0.44,
            "adaptive_vote": 1,
            "adaptive_observations": 120,
            "adaptive_top_expert": "TREND_BREAKOUT",
            "change_point_alarm": False,
        }
        state, strength, agreement, components = _pattern_diagnostic(
            row,
            positive_votes=3,
            negative_votes=0,
            model_enabled=True,
            model_stale=False,
            config=self.config,
        )
        self.assertEqual(state, "BULLISH_CONSENSUS")
        self.assertAlmostEqual(strength or 0.0, 0.44)
        self.assertAlmostEqual(agreement, 3 / 7)
        self.assertIn("top=TREND_BREAKOUT", components)

        break_state = _pattern_diagnostic(
            {**row, "change_point_alarm": True},
            positive_votes=3,
            negative_votes=0,
            model_enabled=True,
            model_stale=False,
            config=self.config,
        )[0]
        self.assertEqual(break_state, "STRUCTURAL_BREAK")

        stale_state = _pattern_diagnostic(
            row,
            positive_votes=3,
            negative_votes=0,
            model_enabled=True,
            model_stale=True,
            config=self.config,
        )[0]
        self.assertEqual(stale_state, "MODEL_STALE")

    def test_pattern_diagnostic_respects_warmup(self) -> None:
        config = replace(
            self.config,
            backtest=replace(self.config.backtest, adaptive_min_observations=80),
        )
        state = _pattern_diagnostic(
            {
                "adaptive_score": -0.50,
                "adaptive_vote": -1,
                "adaptive_observations": 79,
                "adaptive_top_expert": "FLOW_DIVERGENCE",
            },
            positive_votes=0,
            negative_votes=3,
            model_enabled=True,
            model_stale=False,
            config=config,
        )[0]
        self.assertEqual(state, "WARMUP")

    def test_pdf_text_sanitizer_uses_ascii_hyphens(self) -> None:
        safe = _safe("A\u2014B \u2264 C \u00b1 D")
        self.assertEqual(safe, "A-B &lt;= C +/- D")

    def test_full_product_report_contains_all_product_chapters(self) -> None:
        signals = PROJECT_ROOT / "dist" / "technical_live_signals.csv"
        if not signals.is_file() or "pattern_state" not in signals.read_text(
            encoding="utf-8", errors="ignore"
        ).splitlines()[0]:
            self.skipTest("dynamic-pattern demo outputs have not been generated")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "Technical_Product_Report.pdf"
            build_pdf(PROJECT_ROOT, output, mode="demo")
            reader = PdfReader(output)
            self.assertGreaterEqual(len(reader.pages), 8)
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
            self.assertIn("How the dynamic pattern engine works", text)
            self.assertIn("Bloomberg API readiness", text)
            for product in PRODUCT_ORDER:
                self.assertIn(product, text)
            self.assertEqual(float(reader.pages[0].mediabox.width), 792.0)
            self.assertEqual(float(reader.pages[0].mediabox.height), 612.0)


if __name__ == "__main__":
    unittest.main()
