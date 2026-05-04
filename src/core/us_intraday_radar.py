# -*- coding: utf-8 -*-
"""US intraday radar for concise action-oriented alerts."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from src.core.trading_calendar import is_market_open

logger = logging.getLogger(__name__)


US_RISK_PROXIES = {
    "VIX", "TLT", "HYG", "UUP", "GLD", "SPY", "QQQ", "SMH",
    "IWM", "XLK", "XLF", "XLE", "SPX", "NASDAQ",
}

DEFAULT_RISK_PROXY_ORDER = ["VIX", "TLT", "HYG", "UUP", "GLD", "SPY", "QQQ", "SMH"]


@dataclass(frozen=True)
class IntradayWindow:
    key: str
    label: str
    local_time: time
    focus: str


@dataclass(frozen=True)
class IntradayWindowMatch:
    window: IntradayWindow
    now: datetime
    forced: bool = False
    skip_reason: str = ""


@dataclass
class QuoteSnapshot:
    code: str
    name: str = ""
    price: Optional[float] = None
    change_pct: Optional[float] = None
    open_price: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    pre_close: Optional[float] = None
    volume_ratio: Optional[float] = None
    source: str = ""
    ma5: Optional[float] = None
    ma10: Optional[float] = None
    ma20: Optional[float] = None
    bias_pct: Optional[float] = None


WINDOW_DEFS: Dict[str, IntradayWindow] = {
    "pre_open": IntradayWindow("pre_open", "盘前5分钟", time(9, 25), "今日重点与隔夜风险"),
    "open_15": IntradayWindow("open_15", "开盘15分钟", time(9, 45), "开盘真假强弱"),
    "open_60": IntradayWindow("open_60", "开盘60分钟", time(10, 30), "追还是等回踩"),
    "midday": IntradayWindow("midday", "午盘", time(12, 0), "趋势延续或冲高回落"),
    "power_hour": IntradayWindow("power_hour", "尾盘30分钟", time(15, 30), "仓位与风控动作"),
    "close_15": IntradayWindow("close_15", "收盘后15分钟", time(16, 15), "收盘动作摘要"),
}


def _parse_windows(raw_windows: Sequence[str] | str | None) -> List[str]:
    if not raw_windows:
        return list(WINDOW_DEFS)
    if isinstance(raw_windows, str):
        values = [item.strip() for item in raw_windows.split(",")]
    else:
        values = [str(item).strip() for item in raw_windows]
    return [item for item in values if item in WINDOW_DEFS]


def resolve_us_intraday_window(
    *,
    enabled: bool,
    configured_windows: Sequence[str] | str | None,
    tolerance_minutes: int,
    force_run: bool = False,
    requested_window: str = "auto",
    now: Optional[datetime] = None,
) -> IntradayWindowMatch:
    """Resolve the current US intraday radar window.

    Auto matching intentionally accepts only times at/after a window, so the
    duplicate DST/standard GitHub schedules cannot fire the next window early.
    """
    ny_tz = ZoneInfo("America/New_York")
    current = now or datetime.now(ny_tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=ny_tz)
    else:
        current = current.astimezone(ny_tz)

    if not enabled and not force_run:
        return IntradayWindowMatch(
            window=WINDOW_DEFS["open_15"],
            now=current,
            skip_reason="US_INTRADAY_RADAR_ENABLED 未启用",
        )

    if not force_run and not is_market_open("us", current.date()):
        return IntradayWindowMatch(
            window=WINDOW_DEFS["open_15"],
            now=current,
            skip_reason="今天不是美股交易日",
        )

    allowed = _parse_windows(configured_windows)
    if not allowed:
        return IntradayWindowMatch(
            window=WINDOW_DEFS["open_15"],
            now=current,
            skip_reason="没有可用的盘中窗口配置",
        )

    requested = (requested_window or "auto").strip()
    if requested and requested != "auto":
        if requested not in WINDOW_DEFS:
            return IntradayWindowMatch(
                window=WINDOW_DEFS["open_15"],
                now=current,
                skip_reason=f"未知盘中窗口: {requested}",
            )
        if requested not in allowed and not force_run:
            return IntradayWindowMatch(
                window=WINDOW_DEFS[requested],
                now=current,
                skip_reason=f"盘中窗口未启用: {requested}",
            )
        return IntradayWindowMatch(window=WINDOW_DEFS[requested], now=current, forced=force_run)

    tolerance = max(0, int(tolerance_minutes))
    for key in allowed:
        window = WINDOW_DEFS[key]
        target = current.replace(
            hour=window.local_time.hour,
            minute=window.local_time.minute,
            second=0,
            microsecond=0,
        )
        elapsed_minutes = (current - target).total_seconds() / 60.0
        if 0 <= elapsed_minutes <= tolerance:
            return IntradayWindowMatch(window=window, now=current)

    if force_run:
        fallback_key = allowed[0] if allowed else "open_15"
        return IntradayWindowMatch(window=WINDOW_DEFS[fallback_key], now=current, forced=True)

    return IntradayWindowMatch(
        window=WINDOW_DEFS["open_15"],
        now=current,
        skip_reason="当前不在已配置的盘中提醒窗口",
    )


def _quote_field(quote: Any, field_name: str) -> Any:
    if quote is None:
        return None
    if isinstance(quote, dict):
        return quote.get(field_name)
    value = getattr(quote, field_name, None)
    if value is None and hasattr(quote, "to_dict"):
        try:
            value = quote.to_dict().get(field_name)
        except Exception:
            value = None
    return value


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if value.endswith("%"):
            value = value[:-1]
        if not value:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_pct(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return f"{value:+.2f}%"


def _format_price(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return f"{value:.2f}"


def _format_source(value: Any) -> str:
    if value is None:
        return ""
    return getattr(value, "value", str(value))


def _dedupe_codes(codes: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for code in codes:
        normalized = (code or "").strip().upper()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _daily_closes(fetcher_manager: Any, code: str) -> List[float]:
    try:
        result = fetcher_manager.get_daily_data(code, days=30)
    except Exception as exc:
        logger.debug("[IntradayRadar] daily data failed for %s: %s", code, exc)
        return []
    if result is None:
        return []
    df = result[0] if isinstance(result, tuple) else result
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return []
    close_col = "close" if "close" in df.columns else "Close" if "Close" in df.columns else None
    if close_col is None:
        return []
    closes = []
    for value in df[close_col].tail(30).tolist():
        parsed = _as_float(value)
        if parsed is not None and parsed > 0:
            closes.append(parsed)
    return closes


def _augment_ma(snapshot: QuoteSnapshot, closes: List[float]) -> None:
    values = list(closes)
    if snapshot.price is not None and snapshot.price > 0:
        if values:
            values[-1] = snapshot.price
        else:
            values.append(snapshot.price)
    if len(values) >= 5:
        snapshot.ma5 = sum(values[-5:]) / 5
    if len(values) >= 10:
        snapshot.ma10 = sum(values[-10:]) / 10
    if len(values) >= 20:
        snapshot.ma20 = sum(values[-20:]) / 20
    if snapshot.price and snapshot.ma5:
        snapshot.bias_pct = (snapshot.price - snapshot.ma5) / snapshot.ma5 * 100


def build_quote_snapshots(codes: Sequence[str], fetcher_manager: Any) -> Dict[str, QuoteSnapshot]:
    snapshots: Dict[str, QuoteSnapshot] = {}
    for code in _dedupe_codes(codes):
        try:
            quote = fetcher_manager.get_realtime_quote(code, log_final_failure=False)
        except TypeError:
            quote = fetcher_manager.get_realtime_quote(code)
        except Exception as exc:
            logger.debug("[IntradayRadar] realtime quote failed for %s: %s", code, exc)
            quote = None

        snapshot = QuoteSnapshot(
            code=code,
            name=str(_quote_field(quote, "name") or code),
            price=_as_float(_quote_field(quote, "price")),
            change_pct=_as_float(
                _quote_field(quote, "change_pct")
                or _quote_field(quote, "change_percent")
                or _quote_field(quote, "pct_chg")
            ),
            open_price=_as_float(_quote_field(quote, "open_price")),
            high=_as_float(_quote_field(quote, "high")),
            low=_as_float(_quote_field(quote, "low")),
            pre_close=_as_float(_quote_field(quote, "pre_close")),
            volume_ratio=_as_float(_quote_field(quote, "volume_ratio")),
            source=_format_source(_quote_field(quote, "source")),
        )
        _augment_ma(snapshot, _daily_closes(fetcher_manager, code))
        snapshots[code] = snapshot
    return snapshots


def _action_for_snapshot(snapshot: QuoteSnapshot, *, holding_threshold: float, bias_threshold: float) -> str:
    change = snapshot.change_pct
    price = snapshot.price
    if price and snapshot.ma20 and price < snapshot.ma20:
        return "跌破MA20，优先防守/减仓观察"
    if change is not None and change <= -abs(holding_threshold):
        return "盘中明显转弱，检查仓位和止损线"
    if change is not None and change >= abs(holding_threshold):
        if snapshot.bias_pct is not None and snapshot.bias_pct > bias_threshold:
            return "上涨但乖离偏高，禁止追高，等回踩"
        return "强势运行，持有为主，回踩再考虑加"
    if snapshot.ma5 and snapshot.ma10 and snapshot.ma20 and snapshot.ma5 > snapshot.ma10 > snapshot.ma20:
        return "趋势仍在，持有观察"
    return "信号一般，先观望等确认"


def _risk_reason(
    snapshot: QuoteSnapshot,
    *,
    code: str,
    holding_codes: set[str],
    holding_threshold: float,
    index_threshold: float,
    vix_threshold: float,
    bias_threshold: float,
) -> Optional[str]:
    change = snapshot.change_pct
    if code == "VIX" and change is not None and abs(change) >= vix_threshold:
        return f"VIX 异动 {_format_pct(change)}"
    if code in {"SPY", "QQQ", "SMH", "SPX", "NASDAQ"} and change is not None and abs(change) >= index_threshold:
        return f"指数/主线波动 {_format_pct(change)}"
    if code in holding_codes and change is not None and abs(change) >= holding_threshold:
        return f"持仓盘中波动 {_format_pct(change)}"
    if snapshot.price and snapshot.ma20 and snapshot.price < snapshot.ma20:
        return "跌破 MA20"
    if snapshot.bias_pct is not None and snapshot.bias_pct > bias_threshold:
        return f"乖离率偏高 {snapshot.bias_pct:.2f}%"
    return None


def _opportunity_score(snapshot: QuoteSnapshot, *, bias_threshold: float) -> float:
    score = 0.0
    if snapshot.change_pct is not None:
        score += snapshot.change_pct
    if snapshot.ma5 and snapshot.ma10 and snapshot.ma20 and snapshot.ma5 > snapshot.ma10 > snapshot.ma20:
        score += 3.0
    if snapshot.bias_pct is not None:
        if 0 <= snapshot.bias_pct <= bias_threshold:
            score += 1.5
        elif snapshot.bias_pct > bias_threshold:
            score -= 3.0
    if snapshot.volume_ratio is not None and snapshot.volume_ratio >= 1.5:
        score += 1.0
    return score


def build_us_intraday_radar_report(
    *,
    config: Any,
    match: IntradayWindowMatch,
    snapshots: Dict[str, QuoteSnapshot],
) -> str:
    holding_codes = set(_dedupe_codes(getattr(config, "portfolio_stock_list", []) or []))
    holding_threshold = float(getattr(config, "us_intraday_alert_holding_change_pct", 2.5))
    index_threshold = float(getattr(config, "us_intraday_alert_index_change_pct", 1.0))
    vix_threshold = float(getattr(config, "us_intraday_alert_vix_change_pct", 5.0))
    opportunity_max = int(getattr(config, "us_intraday_opportunity_max", 5))
    bias_threshold = float(getattr(config, "bias_threshold", 5.0))

    risk_lines: List[str] = []
    for code, snapshot in snapshots.items():
        reason = _risk_reason(
            snapshot,
            code=code,
            holding_codes=holding_codes,
            holding_threshold=holding_threshold,
            index_threshold=index_threshold,
            vix_threshold=vix_threshold,
            bias_threshold=bias_threshold,
        )
        if reason:
            risk_lines.append(f"- **{code}**：{reason}，现价 {_format_price(snapshot.price)}，涨跌 {_format_pct(snapshot.change_pct)}")

    if not risk_lines:
        risk_lines.append("- 暂无必须立刻处理的异常；按计划观察，不追高。")

    market_lines = []
    for code in DEFAULT_RISK_PROXY_ORDER:
        snapshot = snapshots.get(code)
        if snapshot:
            market_lines.append(f"- **{code}**：{_format_pct(snapshot.change_pct)} | {_format_price(snapshot.price)}")

    holding_lines = []
    for code in sorted(holding_codes):
        snapshot = snapshots.get(code)
        if not snapshot:
            holding_lines.append(f"- **{code}**：暂无实时行情，先不动作。")
            continue
        action = _action_for_snapshot(
            snapshot,
            holding_threshold=holding_threshold,
            bias_threshold=bias_threshold,
        )
        holding_lines.append(
            f"- **{code}**：{action} | 涨跌 {_format_pct(snapshot.change_pct)} | "
            f"现价 {_format_price(snapshot.price)} | MA20 {_format_price(snapshot.ma20)}"
        )

    opportunity_candidates = [
        snapshot for code, snapshot in snapshots.items()
        if code not in holding_codes and code not in US_RISK_PROXIES
    ]
    opportunity_candidates.sort(
        key=lambda item: _opportunity_score(item, bias_threshold=bias_threshold),
        reverse=True,
    )
    opportunity_lines = []
    for snapshot in opportunity_candidates[:opportunity_max]:
        action = _action_for_snapshot(
            snapshot,
            holding_threshold=holding_threshold,
            bias_threshold=bias_threshold,
        )
        opportunity_lines.append(
            f"- **{snapshot.code}**：{action} | 涨跌 {_format_pct(snapshot.change_pct)} | "
            f"乖离 {snapshot.bias_pct:.2f}%" if snapshot.bias_pct is not None
            else f"- **{snapshot.code}**：{action} | 涨跌 {_format_pct(snapshot.change_pct)}"
        )
    if not opportunity_lines:
        opportunity_lines.append("- 暂无高质量新增机会，保持观察池即可。")

    forced_note = " | 手动/强制运行" if match.forced else ""
    now_text = match.now.strftime("%Y-%m-%d %H:%M ET")
    report = [
        f"# 🧭 美股盘中雷达：{match.window.label}",
        "",
        f"> {now_text}{forced_note} | 重点：{match.window.focus}",
        "> 这是条件型提醒，不是自动交易指令。",
        "",
        "## 需要你看",
        *risk_lines,
        "",
        "## 市场温度",
        *(market_lines or ["- 暂无市场代理实时行情。"]),
        "",
        "## 真实持仓",
        *(holding_lines or ["- 未配置真实持仓列表。"]),
        "",
        "## 机会池",
        *opportunity_lines,
        "",
        "## 当前指导",
        f"- **{match.window.label}**：{match.window.focus}。若信号没有触发，按原仓位计划执行；若触发风险，先处理风险再看机会。",
    ]
    return "\n".join(report).strip() + "\n"


def _write_report(report: str, *, match: IntradayWindowMatch) -> str:
    os.makedirs("reports", exist_ok=True)
    stamp = match.now.strftime("%Y%m%d")
    path = os.path.join("reports", f"us_intraday_radar_{stamp}_{match.window.key}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    return path


def run_us_intraday_radar(
    *,
    config: Any,
    force_run: bool = False,
    requested_window: str = "auto",
    send_notification: bool = True,
    fetcher_manager: Any = None,
    notifier: Any = None,
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """Run one US intraday radar cycle.

    Returns (sent_or_skipped_successfully, message).
    """
    match = resolve_us_intraday_window(
        enabled=bool(getattr(config, "us_intraday_radar_enabled", False)),
        configured_windows=getattr(config, "us_intraday_windows", []),
        tolerance_minutes=int(getattr(config, "us_intraday_window_tolerance_minutes", 12)),
        force_run=force_run,
        requested_window=requested_window,
        now=now,
    )
    if match.skip_reason:
        message = f"[IntradayRadar] 跳过：{match.skip_reason}"
        logger.info(message)
        return True, message

    if not force_run and not bool(getattr(config, "us_intraday_push_night", True)):
        beijing_now = match.now.astimezone(ZoneInfo("Asia/Shanghai"))
        if beijing_now.hour >= 23 or beijing_now.hour < 8:
            message = "[IntradayRadar] 跳过：已关闭北京时间夜间盘中推送"
            logger.info(message)
            return True, message

    if fetcher_manager is None:
        from data_provider import DataFetcherManager
        fetcher_manager = DataFetcherManager()

    holding_codes = _dedupe_codes(getattr(config, "portfolio_stock_list", []) or [])
    stock_codes = _dedupe_codes(getattr(config, "stock_list", []) or [])
    risk_codes = [code for code in DEFAULT_RISK_PROXY_ORDER if code in stock_codes or code in US_RISK_PROXIES]
    opportunity_pool = [code for code in stock_codes if code not in set(holding_codes) and code not in US_RISK_PROXIES]
    codes = _dedupe_codes(holding_codes + risk_codes + opportunity_pool)

    snapshots = build_quote_snapshots(codes, fetcher_manager)
    report = build_us_intraday_radar_report(config=config, match=match, snapshots=snapshots)
    path = _write_report(report, match=match)
    logger.info("[IntradayRadar] 盘中雷达已保存: %s", path)

    if not send_notification:
        return True, path

    if notifier is None:
        from src.notification import NotificationService
        notifier = NotificationService()
    sent = notifier.send(report)
    return bool(sent), path if sent else "盘中雷达推送失败"
