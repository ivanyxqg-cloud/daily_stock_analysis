# -*- coding: utf-8 -*-
import os
import tempfile
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
    build_us_intraday_technical_report,
    resolve_us_intraday_window,
    run_us_intraday_radar,
)


class FakeFetcher:
    def __init__(self, quotes):
        self.quotes = quotes

    def get_realtime_quote(self, code, log_final_failure=True):
        return self.quotes.get(code)

    def get_daily_data(self, code, days=30):
        return pd.DataFrame({"close": list(range(80, 110))}), "fake"


class FakeNotifier:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        return True


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

        self.assertIn("尚未到第一个盘中提醒窗口", match.skip_reason)

    def test_resolves_open_30_window(self):
        now = datetime(2026, 6, 1, 10, 4, tzinfo=ZoneInfo("America/New_York"))

        with patch("src.core.us_intraday_radar.is_market_open", return_value=True):
            match = resolve_us_intraday_window(
                enabled=True,
                configured_windows="open_15,open_30,open_60",
                tolerance_minutes=18,
                now=now,
            )

        self.assertEqual(match.window.key, "open_30")
        self.assertFalse(match.skip_reason)

    def test_auto_catches_up_late_regular_window(self):
        now = datetime(2026, 6, 1, 10, 22, tzinfo=ZoneInfo("America/New_York"))

        with patch("src.core.us_intraday_radar.is_market_open", return_value=True):
            match = resolve_us_intraday_window(
                enabled=True,
                configured_windows="open_15,open_30,open_60",
                tolerance_minutes=18,
                catchup_minutes=45,
                now=now,
            )

        self.assertEqual(match.window.key, "open_30")
        self.assertFalse(match.skip_reason)

    def test_auto_uses_next_window_at_boundary(self):
        now = datetime(2026, 6, 1, 10, 30, tzinfo=ZoneInfo("America/New_York"))

        with patch("src.core.us_intraday_radar.is_market_open", return_value=True):
            match = resolve_us_intraday_window(
                enabled=True,
                configured_windows="open_30,open_60",
                tolerance_minutes=18,
                catchup_minutes=45,
                now=now,
            )

        self.assertEqual(match.window.key, "open_60")
        self.assertFalse(match.skip_reason)

    def test_auto_skips_after_regular_catchup_expired(self):
        now = datetime(2026, 6, 1, 10, 46, tzinfo=ZoneInfo("America/New_York"))

        with patch("src.core.us_intraday_radar.is_market_open", return_value=True):
            match = resolve_us_intraday_window(
                enabled=True,
                configured_windows="open_30",
                tolerance_minutes=18,
                catchup_minutes=45,
                now=now,
            )

        self.assertIn("已超过 开盘30分钟 的补发时间", match.skip_reason)

    def test_auto_allows_extended_close_catchup(self):
        now = datetime(2026, 6, 1, 17, 30, tzinfo=ZoneInfo("America/New_York"))

        with patch("src.core.us_intraday_radar.is_market_open", return_value=True):
            match = resolve_us_intraday_window(
                enabled=True,
                configured_windows="close_15",
                tolerance_minutes=18,
                catchup_minutes=45,
                close_catchup_minutes=120,
                now=now,
            )

        self.assertEqual(match.window.key, "close_15")
        self.assertFalse(match.skip_reason)

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
            us_intraday_max_action_items=5,
            us_intraday_readable_report=True,
            us_intraday_jargon_level="explained",
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

        self.assertIn("一句话结论", report)
        self.assertIn("需要你处理", report)
        self.assertIn("你的持仓", report)
        self.assertIn("市场环境", report)
        self.assertIn("可以关注", report)
        self.assertIn("VIX（恐慌指数）", report)
        self.assertIn("MA20（20日均线", report)
        self.assertIn("跌破 MA20", report)

    def test_readable_report_limits_action_items(self):
        config = SimpleNamespace(
            portfolio_stock_list=["QQQ", "BABA", "PLTR"],
            us_intraday_alert_holding_change_pct=2.5,
            us_intraday_alert_index_change_pct=1.0,
            us_intraday_alert_vix_change_pct=5.0,
            us_intraday_opportunity_max=3,
            us_intraday_max_action_items=2,
            us_intraday_readable_report=True,
            us_intraday_jargon_level="explained",
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
            "BABA": QuoteSnapshot(code="BABA", price=90, change_pct=-4.0, ma20=100),
            "PLTR": QuoteSnapshot(code="PLTR", price=200, change_pct=5.0, ma5=180, bias_pct=11.0),
            "VIX": QuoteSnapshot(code="VIX", price=22, change_pct=6.0),
        }

        report = build_us_intraday_radar_report(config=config, match=match, snapshots=snapshots)
        action_section = report.split("## 需要你处理", 1)[1].split("## 你的持仓", 1)[0]
        action_lines = [line for line in action_section.splitlines() if line.startswith("- ")]

        self.assertLessEqual(len(action_lines), 2)

    def test_technical_report_keeps_raw_indicators_for_artifact(self):
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

        report = build_us_intraday_technical_report(config=config, match=match, snapshots=snapshots)

        self.assertIn("MA20", report)
        self.assertIn("乖离", report)
        self.assertIn("VIX 异动", report)

    def test_dedupe_skips_existing_window_marker(self):
        config = SimpleNamespace(
            portfolio_stock_list=["QQQ"],
            stock_list=["QQQ", "VIX"],
            us_intraday_radar_enabled=True,
            us_intraday_windows=["open_30"],
            us_intraday_window_tolerance_minutes=18,
            us_intraday_push_night=True,
            us_intraday_dedupe_enabled=True,
            us_intraday_dedupe_lookback_hours=24,
        )
        notifier = FakeNotifier()

        with patch("src.core.us_intraday_radar.is_market_open", return_value=True):
            ok, message = run_us_intraday_radar(
                config=config,
                requested_window="open_30",
                send_notification=True,
                fetcher_manager=FakeFetcher({}),
                notifier=notifier,
                now=datetime(2026, 6, 1, 10, 2, tzinfo=ZoneInfo("America/New_York")),
                dedupe_checker=lambda marker_name, lookback_hours: True,
            )

        self.assertTrue(ok)
        self.assertIn("已发送过", message)
        self.assertEqual(notifier.messages, [])

    def test_force_run_bypasses_dedupe(self):
        config = SimpleNamespace(
            portfolio_stock_list=["QQQ"],
            stock_list=["QQQ", "VIX"],
            us_intraday_radar_enabled=True,
            us_intraday_windows=["open_30"],
            us_intraday_window_tolerance_minutes=18,
            us_intraday_push_night=True,
            us_intraday_dedupe_enabled=True,
            us_intraday_dedupe_lookback_hours=24,
            us_intraday_alert_holding_change_pct=2.5,
            us_intraday_alert_index_change_pct=1.0,
            us_intraday_alert_vix_change_pct=5.0,
            us_intraday_opportunity_max=3,
            us_intraday_max_action_items=5,
            us_intraday_readable_report=True,
            us_intraday_jargon_level="explained",
            us_intraday_show_technical_details=False,
            bias_threshold=5.0,
        )
        notifier = FakeNotifier()
        fetcher = FakeFetcher({
            "QQQ": {"name": "QQQ", "price": 100, "change_pct": 0.5},
            "VIX": {"name": "VIX", "price": 20, "change_pct": 1.0},
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                with patch("src.core.us_intraday_radar.is_market_open", return_value=True):
                    ok, message = run_us_intraday_radar(
                        config=config,
                        force_run=True,
                        requested_window="open_30",
                        send_notification=True,
                        fetcher_manager=fetcher,
                        notifier=notifier,
                        now=datetime(2026, 6, 1, 10, 2, tzinfo=ZoneInfo("America/New_York")),
                        dedupe_checker=lambda marker_name, lookback_hours: True,
                    )
            finally:
                os.chdir(old_cwd)

        self.assertTrue(ok)
        self.assertIn("us_intraday_radar_20260601_open_30.md", message)
        self.assertEqual(len(notifier.messages), 1)

    def test_successful_non_force_run_writes_dedupe_marker(self):
        config = SimpleNamespace(
            portfolio_stock_list=["QQQ"],
            stock_list=["QQQ", "VIX"],
            us_intraday_radar_enabled=True,
            us_intraday_windows=["open_30"],
            us_intraday_window_tolerance_minutes=18,
            us_intraday_push_night=True,
            us_intraday_dedupe_enabled=True,
            us_intraday_dedupe_lookback_hours=24,
            us_intraday_alert_holding_change_pct=2.5,
            us_intraday_alert_index_change_pct=1.0,
            us_intraday_alert_vix_change_pct=5.0,
            us_intraday_opportunity_max=3,
            us_intraday_max_action_items=5,
            us_intraday_readable_report=True,
            us_intraday_jargon_level="explained",
            us_intraday_show_technical_details=False,
            bias_threshold=5.0,
        )
        notifier = FakeNotifier()
        fetcher = FakeFetcher({
            "QQQ": {"name": "QQQ", "price": 100, "change_pct": 0.5},
            "VIX": {"name": "VIX", "price": 20, "change_pct": 1.0},
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                with patch("src.core.us_intraday_radar.is_market_open", return_value=True):
                    ok, message = run_us_intraday_radar(
                        config=config,
                        requested_window="open_30",
                        send_notification=True,
                        fetcher_manager=fetcher,
                        notifier=notifier,
                        now=datetime(2026, 6, 1, 10, 2, tzinfo=ZoneInfo("America/New_York")),
                        dedupe_checker=lambda marker_name, lookback_hours: False,
                    )
                marker_path = os.path.join("reports", "us-intraday-sent-20260601-open_30")
                marker_exists = os.path.exists(marker_path)
            finally:
                os.chdir(old_cwd)

        self.assertTrue(ok)
        self.assertIn("us_intraday_radar_20260601_open_30.md", message)
        self.assertEqual(len(notifier.messages), 1)
        self.assertTrue(marker_exists)

    def test_local_mode_uses_local_marker_for_dedupe(self):
        config = SimpleNamespace(
            portfolio_stock_list=["QQQ"],
            stock_list=["QQQ", "VIX"],
            us_intraday_radar_enabled=True,
            us_intraday_windows=["open_30"],
            us_intraday_window_tolerance_minutes=18,
            us_intraday_push_night=True,
            us_intraday_dedupe_enabled=True,
            us_intraday_dedupe_lookback_hours=24,
            us_intraday_alert_holding_change_pct=2.5,
            us_intraday_alert_index_change_pct=1.0,
            us_intraday_alert_vix_change_pct=5.0,
            us_intraday_opportunity_max=3,
            us_intraday_max_action_items=5,
            us_intraday_readable_report=True,
            us_intraday_jargon_level="explained",
            us_intraday_show_technical_details=False,
            bias_threshold=5.0,
        )
        fetcher = FakeFetcher({
            "QQQ": {"name": "QQQ", "price": 100, "change_pct": 0.5},
            "VIX": {"name": "VIX", "price": 20, "change_pct": 1.0},
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            marker_dir = os.path.join(tmpdir, "markers")
            try:
                with patch.dict(
                    os.environ,
                    {
                        "US_INTRADAY_LOCAL_MODE": "true",
                        "US_INTRADAY_LOCAL_MARKER_DIR": marker_dir,
                    },
                    clear=False,
                ):
                    with patch("src.core.us_intraday_radar.is_market_open", return_value=True):
                        notifier = FakeNotifier()
                        ok, _ = run_us_intraday_radar(
                            config=config,
                            requested_window="open_30",
                            send_notification=True,
                            fetcher_manager=fetcher,
                            notifier=notifier,
                            now=datetime(2026, 6, 1, 10, 2, tzinfo=ZoneInfo("America/New_York")),
                        )
                        second_notifier = FakeNotifier()
                        second_ok, second_message = run_us_intraday_radar(
                            config=config,
                            requested_window="open_30",
                            send_notification=True,
                            fetcher_manager=fetcher,
                            notifier=second_notifier,
                            now=datetime(2026, 6, 1, 10, 3, tzinfo=ZoneInfo("America/New_York")),
                        )
                local_marker = os.path.join(marker_dir, "us-intraday-sent-20260601-open_30")
                local_marker_exists = os.path.exists(local_marker)
            finally:
                os.chdir(old_cwd)

        self.assertTrue(ok)
        self.assertEqual(len(notifier.messages), 1)
        self.assertTrue(local_marker_exists)
        self.assertTrue(second_ok)
        self.assertIn("已发送过", second_message)
        self.assertEqual(second_notifier.messages, [])


if __name__ == "__main__":
    unittest.main()
