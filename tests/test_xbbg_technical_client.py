from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import polars as pl

from app.technical_config import load_technical_config
from app.technical_data import XbbgTechnicalClient
from app.technical_data import TechnicalDataError


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _FakeXbbg:
    def __init__(self) -> None:
        self.configure_kwargs: dict[str, object] = {}
        self.backend: str | None = None
        self.reference_batches: list[list[str]] = []

    def configure(self, **kwargs: object) -> None:
        self.configure_kwargs = kwargs

    def set_backend(self, backend: str) -> None:
        self.backend = backend

    async def abdp(self, tickers: list[str], fields: list[str], **_: object) -> pl.DataFrame:
        self.reference_batches.append(tickers)
        return pl.DataFrame(
            {
                "ticker": tickers,
                "field": [fields[0]] * len(tickers),
                "value": ["2026-10-30"] * len(tickers),
            }
        )


class XbbgTechnicalClientTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.config = load_technical_config(
            PROJECT_ROOT / "config" / "technical_system.toml"
        )

    async def test_engine_configuration_has_hard_timeout_and_bounded_retry(self) -> None:
        fake = _FakeXbbg()
        client = XbbgTechnicalClient(self.config)
        with patch("app.technical_data.importlib.import_module", return_value=fake):
            self.assertIs(client._load(), fake)
        self.assertEqual(
            fake.configure_kwargs["request_timeout_ms"],
            self.config.bloomberg.request_timeout_seconds * 1000,
        )
        self.assertEqual(
            fake.configure_kwargs["retry_max_retries"],
            self.config.bloomberg.retry_max_retries,
        )
        self.assertEqual(fake.backend, "polars")

    async def test_reference_batches_preserve_all_configured_tickers(self) -> None:
        fake = _FakeXbbg()
        client = XbbgTechnicalClient(self.config)
        client._xbbg = fake
        definitions = self.config.build_contract_universe(
            date(2026, 9, 1),
            date(2026, 9, 1),
            history_buffer_months=0,
            forward_months=1,
        )
        result = await client.fetch_reference(definitions, batch_size=3)
        self.assertEqual(result.height, len(definitions))
        requested = [ticker for batch in fake.reference_batches for ticker in batch]
        self.assertEqual(requested, [item.ticker for item in definitions])

    async def test_request_wrapper_returns_completed_coroutine(self) -> None:
        client = XbbgTechnicalClient(self.config)
        result = await client._await_request(
            asyncio.sleep(0, result="ok"), "TEST REQUEST"
        )
        self.assertEqual(result, "ok")

    async def test_request_wrapper_timeout_does_not_wait_on_stubborn_cancel(self) -> None:
        release = asyncio.Event()

        async def stubborn() -> None:
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

        config = replace(
            self.config,
            bloomberg=replace(
                self.config.bloomberg,
                request_timeout_seconds=0.05,
                heartbeat_seconds=0.01,
            ),
        )
        client = XbbgTechnicalClient(config)
        started = asyncio.get_running_loop().time()
        with self.assertRaises(TechnicalDataError):
            await client._await_request(stubborn(), "STUBBORN REQUEST")
        self.assertLess(asyncio.get_running_loop().time() - started, 0.5)
        release.set()
        await asyncio.sleep(0)

    async def test_outer_cancellation_cancels_child_request(self) -> None:
        release = asyncio.Event()

        async def pending() -> None:
            await release.wait()

        client = XbbgTechnicalClient(self.config)
        wrapper = asyncio.create_task(
            client._await_request(pending(), "CANCELLED REQUEST")
        )
        await asyncio.sleep(0)
        wrapper.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await wrapper
        release.set()


if __name__ == "__main__":
    import unittest

    unittest.main()
