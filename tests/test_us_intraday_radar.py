# -*- coding: utf-8 -*-
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from src.core.us_intraday_radar import (
    QuoteSnapshot,
    build_quote_snapshots,
    build_us_intraday_radar_report,
    resolve_us_intraday_window,
)


class FakeFetcher:
    def __init__(self, quotes):
        self.quotes = quotes

    def get_realtime_quote(self, code, log_final_failure=True):
        return self.quotes.get(code)

    def get_daily_data(self, code, days=30):
        return pd.DataFrame({"close": list(range(80, 110))}), "fake"


class USIntradayRadarTestCase(unittest.TestCase):
    def test_resolves_dst_open_15_window_after_target_time(self):
        now = datetime(2026, 6, 1, 9, 47, tzinfo=ZoneInfo("America/New_York"))

        with patch("src.core.us_intraday_radar.is_market_open", return_value=True):
            match = resolve_us_intraday_window(
                enabled=True,
                configured_windows="pre_open,open_15,open_60",
                tolerance_minutes=12,
                now=now,
            )

        self.assertEqual(match.window.key, "open_15")
        self.assertFalse(match.skip_reason)

    def test_does_not_trigger_next_window_early(self):
        now = datetime(2026, 6, 1, 10, 25, tzinfo=ZoneInfo("America/New_York"))

        with patch("src.core.us_intraday_radar.is_market_open", return_value=True):
            match = resolve_us_intraday_window(
                enabled=True,
                configured_windows="open_60",
                tolerance_minutes=12,
                now=now,
            )

        self.assertEqual(match.skip_reason, "当前不在已配置的盘中提醒窗口")

    def test_non_trading_day_skips_without_force(self):
        now = datetime(2026, 5, 2, 9, 47, tzinfo=ZoneInfo("America/New_York"))

        with patch("src.core.us_intraday_radar.is_market_open", return_value=False):
            match = resolve_us_intraday_window(
                enabled=True,
                configured_windows="open_15",
                tolerance_minutes=12,
                now=now,
            )

        self.assertEqual(match.skip_reason, "今天不是美股交易日")

    def test_force_run_uses_requested_window(self):
        now = datetime(2026, 5, 2, 3, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("src.core.us_intraday_radar.is_market_open", return_value=False):
            match = resolve_us_intraday_window(
                enabled=False,
                configured_windows="open_15",
                tolerance_minutes=12,
                force_run=True,
                requested_window="power_hour",
                now=now,
            )

        self.assertEqual(match.window.key, "power_hour")
        self.assertTrue(match.forced)

    def test_quote_snapshots_include_realtime_ma_and_bias(self):
        fetcher = FakeFetcher({
            "QQQ": SimpleNamespace(code="QQQ", name="QQQ", price=112, change_pct=2.6),
        })

        snapshots = build_quote_snapshots(["QQQ"], fetcher)

        self.assertIn("QQQ", snapshots)
        self.assertEqual(snapshots["QQQ"].price, 112)
        self.assertIsNotNone(snapshots["QQQ"].ma20)
        self.assertIsNotNone(snapshots["QQQ"].bias_pct)

    def test_report_contains_action_sections_and_threshold_alert(self):
        config = SimpleNamespace(
            portfolio_stock_list=["QQQ"],
            us_intraday_alert_holding_change_pct=2.5,
            us_intraday_alert_index_change_pct=1.0,
            us_intraday_alert_vix_change_pct=5.0,
            us_intraday_opportunity_max=3,
            bias_threshold=5.0,
        )
        match = resolve_us_intraday_window(
            enabled=True,
            configured_windows="open_15",
            tolerance_minutes=12,
            force_run=True,
            requested_window="open_15",
            now=datetime(2026, 6, 1, 9, 50, tzinfo=ZoneInfo("America/New_York")),
        )
        snapshots = {
            "QQQ": QuoteSnapshot(code="QQQ", price=100, change_pct=-3.0, ma20=105),
            "VIX": QuoteSnapshot(code="VIX", price=22, change_pct=6.0),
            "AAPL": QuoteSnapshot(code="AAPL", price=200, change_pct=1.2, ma5=198, ma10=195, ma20=190, bias_pct=1.0),
        }

        report = build_us_intraday_radar_report(config=config, match=match, snapshots=snapshots)

        self.assertIn("需要你看", report)
        self.assertIn("真实持仓", report)
        self.assertIn("机会池", report)
        self.assertIn("VIX 异动", report)
        self.assertIn("跌破MA20", report)


if __name__ == "__main__":
    unittest.main()
