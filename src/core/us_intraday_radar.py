# -*- coding: utf-8 -*-
"""US intraday radar for concise action-oriented alerts."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pandas as pd

from src.core.trading_calendar import is_market_open

logger = logging.getLogger(__name__)


US_RISK_PROXIES = {
    "VIX", "TLT", "HYG", "UUP", "GLD", "SPY", "QQQ", "SMH",
    "IWM", "XLK", "XLF", "XLE", "SPX", "NASDAQ",
}

DEFAULT_RISK_PROXY_ORDER = ["VIX", "TLT", "HYG", "UUP", "GLD", "SPY", "QQQ", "SMH"]

COMMANDER_ACTIONS = {
    "继续持有",
    "减仓观察",
    "禁止追高",
    "等回踩",
    "突破确认再看",
    "加入关注",
    "风险优先处理",
}

COMMANDER_LLM_KEY_WINDOWS = {"pre_open", "open_30", "open_60", "power_hour", "close_15"}

PLAIN_MARKET_LABELS = {
    "VIX": "VIX 恐慌指数",
    "TLT": "TLT 长债ETF",
    "HYG": "HYG 高收益债ETF",
    "UUP": "UUP 美元ETF",
    "GLD": "GLD 黄金ETF",
    "SPY": "SPY 大盘ETF",
    "QQQ": "QQQ 科技ETF",
    "SMH": "SMH 半导体ETF",
    "IWM": "IWM 小盘股ETF",
    "XLK": "XLK 科技板块ETF",
    "XLF": "XLF 金融板块ETF",
    "XLE": "XLE 能源板块ETF",
    "SPX": "SPX 标普500",
    "NASDAQ": "NASDAQ 纳斯达克",
}


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
class QuoteQuality:
    level: str = "high"
    session: str = "regular"
    quote_time: Optional[datetime] = None
    is_fresh: bool = True
    is_actionable: bool = True
    price_field: str = ""
    change_pct_field: str = ""
    warnings: List[str] = field(default_factory=list)


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
    quality: QuoteQuality = field(default_factory=QuoteQuality)


@dataclass
class MarketTemperature:
    score: int
    stance: str
    summary: str
    lines: List[str] = field(default_factory=list)


@dataclass
class CommanderSignal:
    code: str
    category: str
    priority: int
    score: int
    action: str
    status: str
    trigger_line: str
    defense_line: str
    target_line: str
    risk_reward: str
    evidence: List[str] = field(default_factory=list)
    confidence: float = 0.5
    plain_explanation: str = ""
    learning_note: str = ""
    stock_instruction: str = ""
    option_instruction: str = "不做期权"
    option_plan: str = ""
    change_note: str = "新信号"
    quote_quality_level: str = "高"
    quote_actionable: bool = True
    quote_warning: str = ""


@dataclass(frozen=True)
class CommanderCommand:
    conclusion: str
    stock: str
    option: str
    cancel: str
    why: str


@dataclass
class CommanderDecision:
    market: MarketTemperature
    holding_signals: List[CommanderSignal]
    market_signals: List[CommanderSignal]
    opportunity_signals: List[CommanderSignal]
    action_signals: List[CommanderSignal]


WINDOW_DEFS: Dict[str, IntradayWindow] = {
    "pre_open": IntradayWindow("pre_open", "盘前5分钟", time(9, 25), "今日重点与隔夜风险"),
    "open_15": IntradayWindow("open_15", "开盘15分钟", time(9, 45), "开盘真假强弱"),
    "open_30": IntradayWindow("open_30", "开盘30分钟", time(10, 0), "早盘方向是否站稳"),
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
    catchup_minutes: Optional[int] = None,
    close_catchup_minutes: Optional[int] = None,
    force_run: bool = False,
    requested_window: str = "auto",
    now: Optional[datetime] = None,
) -> IntradayWindowMatch:
    """Resolve the current US intraday radar window.

    Auto matching is a checkpoint catch-up resolver: it never sends before a
    window time, but it can still send after GitHub Actions arrives late.
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

    fallback_tolerance = max(0, int(tolerance_minutes))
    regular_catchup = max(
        0,
        int(catchup_minutes if catchup_minutes is not None else fallback_tolerance),
    )
    final_catchup = max(
        0,
        int(close_catchup_minutes if close_catchup_minutes is not None else regular_catchup),
    )
    ordered_windows = sorted(
        [WINDOW_DEFS[key] for key in allowed],
        key=lambda item: (item.local_time.hour, item.local_time.minute),
    )
    window_targets = [
        (
            window,
            current.replace(
                hour=window.local_time.hour,
                minute=window.local_time.minute,
                second=0,
                microsecond=0,
            ),
        )
        for window in ordered_windows
    ]

    first_window, first_target = window_targets[0]
    if current < first_target:
        if force_run:
            fallback_key = allowed[0] if allowed else "open_15"
            return IntradayWindowMatch(window=WINDOW_DEFS[fallback_key], now=current, forced=True)
        return IntradayWindowMatch(
            window=first_window,
            now=current,
            skip_reason=(
                f"尚未到第一个盘中提醒窗口：{first_window.label} "
                f"{first_target.strftime('%H:%M ET')}"
            ),
        )

    latest_expired: Optional[Tuple[IntradayWindow, datetime, datetime]] = None
    for index in range(len(window_targets) - 1, -1, -1):
        window, target = window_targets[index]
        if current < target:
            continue
        grace_minutes = final_catchup if window.key == "close_15" else regular_catchup
        expiry = target + timedelta(minutes=grace_minutes)
        expires_at_next_window = False
        if window.key != "close_15" and index + 1 < len(window_targets):
            _, next_target = window_targets[index + 1]
            if next_target <= expiry:
                expiry = next_target
                expires_at_next_window = True
        if current < expiry or (current == expiry and not expires_at_next_window):
            return IntradayWindowMatch(window=window, now=current)
        if latest_expired is None or target > latest_expired[1]:
            latest_expired = (window, target, expiry)

    if latest_expired is not None:
        window, target, expiry = latest_expired
        return IntradayWindowMatch(
            window=window,
            now=current,
            skip_reason=(
                f"已超过 {window.label} 的补发时间：目标 "
                f"{target.strftime('%H:%M ET')}，补发截止 {expiry.strftime('%H:%M ET')}"
            ),
        )

    for key in allowed:
        window = WINDOW_DEFS[key]
        target = current.replace(
            hour=window.local_time.hour,
            minute=window.local_time.minute,
            second=0,
            microsecond=0,
        )
        elapsed_minutes = (current - target).total_seconds() / 60.0
        if 0 <= elapsed_minutes <= fallback_tolerance:
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


def _parse_quote_time(value: Any) -> Optional[datetime]:
    if value in (None, "", 0):
        return None
    ny_tz = ZoneInfo("America/New_York")
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=ny_tz)
        except (OSError, OverflowError, ValueError):
            return None
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            try:
                parsed = datetime.fromtimestamp(float(text), tz=ny_tz)
            except (OSError, OverflowError, ValueError):
                return None
        else:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ny_tz)
    return parsed.astimezone(ny_tz)


def _expected_quote_session(now: datetime) -> str:
    ny_now = now.astimezone(ZoneInfo("America/New_York"))
    current = ny_now.time()
    if time(4, 0) <= current < time(9, 30):
        return "premarket"
    if time(9, 30) <= current <= time(16, 0):
        return "regular"
    if time(16, 0) < current <= time(20, 0):
        return "postmarket"
    return "closed"


def _quality_label(level: str) -> str:
    mapping = {"high": "高", "medium": "中", "low": "低"}
    return mapping.get(str(level or "").lower(), "低")


def _dedupe_text(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _assess_quote_quality(
    *,
    quote: Any,
    source: str,
    now: datetime,
    freshness_minutes: int,
    require_fresh_quotes: bool,
) -> QuoteQuality:
    session = str(
        _quote_field(quote, "market_session")
        or _quote_field(quote, "session")
        or "unknown"
    ).strip().lower()
    quote_time = _parse_quote_time(
        _quote_field(quote, "quote_time")
        or _quote_field(quote, "regular_market_time")
        or _quote_field(quote, "pre_market_time")
        or _quote_field(quote, "post_market_time")
        or _quote_field(quote, "timestamp")
        or _quote_field(quote, "time")
    )
    price_field = str(_quote_field(quote, "price_field") or "").strip()
    change_pct_field = str(_quote_field(quote, "change_pct_field") or "").strip()
    raw_warnings = _quote_field(quote, "quote_warnings") or []
    warnings = [str(item) for item in raw_warnings if str(item).strip()] if isinstance(raw_warnings, list) else []

    expected = _expected_quote_session(now)
    freshness_minutes = max(1, int(freshness_minutes))
    is_fresh = False
    if quote_time is not None:
        delta_minutes = abs((now.astimezone(ZoneInfo("America/New_York")) - quote_time).total_seconds()) / 60.0
        is_fresh = delta_minutes <= freshness_minutes
        if not is_fresh:
            warnings.append(f"报价时间超过 {freshness_minutes} 分钟，可能不是最新行情")
    else:
        warnings.append("缺少报价时间，无法确认是不是实时价")

    session_actionable = False
    if expected == "premarket":
        session_actionable = session == "premarket"
        if not session_actionable:
            warnings.append("当前是盘前，但行情源没有明确盘前报价")
    elif expected == "regular":
        session_actionable = session == "regular"
        if not session_actionable:
            warnings.append("当前是常规盘中，但行情源没有明确盘中报价")
    elif expected == "postmarket":
        session_actionable = session == "postmarket"
        if not session_actionable:
            warnings.append("当前是盘后，但行情源没有明确盘后报价")
    else:
        warnings.append("当前不在美股可交易时段，行情只用于观察")

    if not price_field:
        warnings.append("缺少原始价格字段来源")

    is_actionable = bool(is_fresh and session_actionable)
    if not require_fresh_quotes:
        is_actionable = True
        is_fresh = True if quote_time is None else is_fresh

    if is_actionable:
        level = "high"
    elif quote_time is not None and is_fresh:
        level = "medium"
    else:
        level = "low"

    return QuoteQuality(
        level=level,
        session=session,
        quote_time=quote_time,
        is_fresh=is_fresh,
        is_actionable=is_actionable,
        price_field=price_field,
        change_pct_field=change_pct_field,
        warnings=_dedupe_text(warnings),
    )


def _quote_quality_text(snapshot: QuoteSnapshot) -> str:
    quality = snapshot.quality
    pieces = [f"行情可信度：{_quality_label(quality.level)}"]
    if quality.session:
        pieces.append(f"阶段 {quality.session}")
    if quality.quote_time:
        pieces.append(f"时间 {quality.quote_time.strftime('%H:%M ET')}")
    if quality.warnings:
        pieces.append("；".join(quality.warnings[:2]))
    return "，".join(pieces)


def _quote_is_actionable(snapshot: QuoteSnapshot) -> bool:
    return bool(snapshot.quality.is_actionable)


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


def build_quote_snapshots(
    codes: Sequence[str],
    fetcher_manager: Any,
    *,
    now: Optional[datetime] = None,
    freshness_minutes: int = 20,
    require_fresh_quotes: bool = True,
    include_daily_history: bool = True,
) -> Dict[str, QuoteSnapshot]:
    snapshots: Dict[str, QuoteSnapshot] = {}
    quality_now = now or datetime.now(ZoneInfo("America/New_York"))
    if quality_now.tzinfo is None:
        quality_now = quality_now.replace(tzinfo=ZoneInfo("America/New_York"))
    else:
        quality_now = quality_now.astimezone(ZoneInfo("America/New_York"))
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
            quality=_assess_quote_quality(
                quote=quote,
                source=_format_source(_quote_field(quote, "source")),
                now=quality_now,
                freshness_minutes=freshness_minutes,
                require_fresh_quotes=require_fresh_quotes,
            ),
        )
        if include_daily_history:
            _augment_ma(snapshot, _daily_closes(fetcher_manager, code))
        snapshots[code] = snapshot
    return snapshots


def _action_for_snapshot(snapshot: QuoteSnapshot, *, holding_threshold: float, bias_threshold: float) -> str:
    if not _quote_is_actionable(snapshot):
        return "行情可信度低，先观察不交易"
    change = snapshot.change_pct
    price = snapshot.price
    if price and snapshot.ma20 and price < snapshot.ma20:
        return "跌破MA20，优先防守/减仓观察"
    if change is not None and change <= -abs(holding_threshold) and _holding_weakness_confirmed(snapshot):
        return "盘中明显转弱，检查仓位和止损线"
    if change is not None and change >= abs(holding_threshold):
        if snapshot.bias_pct is not None and snapshot.bias_pct > bias_threshold:
            return "上涨但乖离偏高，禁止追高，等回踩"
        return "强势运行，持有为主，回踩再考虑加"
    if snapshot.ma5 and snapshot.ma10 and snapshot.ma20 and snapshot.ma5 > snapshot.ma10 > snapshot.ma20:
        return "趋势仍在，持有观察"
    return "信号一般，先观望等确认"


def _plain_action_for_snapshot(snapshot: QuoteSnapshot, *, holding_threshold: float, bias_threshold: float) -> str:
    if not _quote_is_actionable(snapshot):
        return "行情不够可信，先不买不卖"
    change = snapshot.change_pct
    price = snapshot.price
    if price and snapshot.ma20 and price < snapshot.ma20:
        return "先防守，别加仓"
    if change is not None and change <= -abs(holding_threshold) and _holding_weakness_confirmed(snapshot):
        return "今天走弱，检查仓位和止损线"
    if change is not None and change >= abs(holding_threshold):
        if snapshot.bias_pct is not None and snapshot.bias_pct > bias_threshold:
            return "别追高，等回落"
        return "走势偏强，先拿着，回落再看"
    if snapshot.ma5 and snapshot.ma10 and snapshot.ma20 and snapshot.ma5 > snapshot.ma10 > snapshot.ma20:
        return "走势还没坏，先拿着看"
    return "没有明确信号，先观察"


def _plain_risk_reason(
    snapshot: QuoteSnapshot,
    *,
    code: str,
    holding_codes: set[str],
    holding_threshold: float,
    index_threshold: float,
    vix_threshold: float,
    bias_threshold: float,
) -> Optional[str]:
    if not _quote_is_actionable(snapshot):
        return "行情可信度低，先等券商价格或下一次可靠报价确认"
    change = snapshot.change_pct
    price = snapshot.price
    is_holding = code in holding_codes

    if is_holding and price and snapshot.ma20 and price < snapshot.ma20:
        return "跌破 MA20（20日均线，近期重要防线）"
    if is_holding and change is not None and change <= -abs(holding_threshold) and _holding_weakness_confirmed(snapshot):
        return f"今天走弱 {_format_pct(change)}"
    if is_holding and change is not None and change >= abs(holding_threshold):
        if snapshot.bias_pct is not None and snapshot.bias_pct > bias_threshold:
            return f"乖离率偏高（短线涨太快）{_format_pct(change)}，别追高"
        return f"今天明显走强 {_format_pct(change)}"
    if is_holding and snapshot.bias_pct is not None and snapshot.bias_pct > bias_threshold:
        return "乖离率偏高（短线涨太快），注意回落"

    if code == "VIX" and change is not None and abs(change) >= vix_threshold:
        return "VIX（恐慌指数）升温，市场更紧张" if change > 0 else "VIX（恐慌指数）降温，市场更稳"
    if code in {"SPY", "SPX"} and change is not None and abs(change) >= index_threshold:
        return "大盘波动变大"
    if code in {"QQQ", "NASDAQ"} and change is not None and abs(change) >= index_threshold:
        return "科技股方向变化明显"
    if code == "SMH" and change is not None and abs(change) >= index_threshold:
        return "半导体方向变化明显"
    if code == "TLT" and change is not None and abs(change) >= index_threshold:
        return "TLT（长债ETF）走弱，利率压力变大" if change < 0 else "TLT（长债ETF）走强，利率压力缓和"
    if code == "HYG" and change is not None and abs(change) >= index_threshold:
        return "HYG（高收益债ETF）走弱，信用风险升温" if change < 0 else "HYG（高收益债ETF）走强，信用风险缓和"
    if code == "UUP" and change is not None and abs(change) >= index_threshold:
        return "UUP（美元ETF）走强，美元压力变大" if change > 0 else "UUP（美元ETF）走弱，美元压力缓和"
    if code == "GLD" and change is not None and abs(change) >= index_threshold:
        return "GLD（黄金ETF）走强，避险情绪升温" if change > 0 else "GLD（黄金ETF）走弱，避险情绪降温"
    return None


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
    if not _quote_is_actionable(snapshot):
        return "行情可信度低"
    change = snapshot.change_pct
    if code == "VIX" and change is not None and abs(change) >= vix_threshold:
        return f"VIX 异动 {_format_pct(change)}"
    if code in {"SPY", "QQQ", "SMH", "SPX", "NASDAQ"} and change is not None and abs(change) >= index_threshold:
        return f"指数/主线波动 {_format_pct(change)}"
    if code in holding_codes and change is not None and abs(change) >= holding_threshold and (
        change > 0 or _holding_weakness_confirmed(snapshot)
    ):
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


def _clamp_score(value: float) -> int:
    return max(0, min(100, int(round(value))))


def _risk_style_multiplier(config: Any) -> float:
    style = str(getattr(config, "us_commander_risk_style", "balanced") or "balanced").lower()
    if style in {"conservative", "stable", "defensive"}:
        return 0.85
    if style in {"aggressive", "offensive"}:
        return 1.2
    return 1.0


def _price_candidates(snapshot: QuoteSnapshot) -> List[float]:
    values = [
        snapshot.low,
        snapshot.ma20,
        snapshot.ma10,
        snapshot.ma5,
        snapshot.open_price,
        snapshot.pre_close,
        snapshot.high,
    ]
    return [value for value in values if value is not None and value > 0]


def _support_price(snapshot: QuoteSnapshot) -> Optional[float]:
    price = snapshot.price
    candidates = _price_candidates(snapshot)
    if not candidates:
        return None
    if price:
        below = [item for item in candidates if item <= price]
        if below:
            return max(below)
    return min(candidates)


def _resistance_price(snapshot: QuoteSnapshot) -> Optional[float]:
    price = snapshot.price
    candidates = _price_candidates(snapshot)
    if price:
        above = [item for item in candidates if item >= price]
        if above:
            return min(above)
        return price * 1.03
    if candidates:
        return max(candidates)
    return None


def _stop_price(snapshot: QuoteSnapshot) -> Optional[float]:
    support = _support_price(snapshot)
    if support:
        return support * 0.985
    if snapshot.price:
        return snapshot.price * 0.97
    return None


def _holding_weakness_confirmed(snapshot: QuoteSnapshot) -> bool:
    """Require price confirmation before turning a negative change into a sell action."""
    price = snapshot.price
    if price is None:
        return False
    if snapshot.ma20 and price < snapshot.ma20:
        return True
    if snapshot.ma5 and price < snapshot.ma5:
        return True
    if snapshot.open_price and price < snapshot.open_price:
        return True
    if snapshot.pre_close and price < snapshot.pre_close and snapshot.low and price <= snapshot.low * 1.01:
        return True
    return False


def _risk_reward_text(snapshot: QuoteSnapshot) -> str:
    price = snapshot.price
    stop = _stop_price(snapshot)
    target = _resistance_price(snapshot)
    if not price or not stop or not target or price <= stop:
        return "N/A"
    risk = price - stop
    reward = max(0.0, target - price)
    if risk <= 0:
        return "N/A"
    ratio = reward / risk
    return f"{ratio:.1f}:1"


def _line_or_na(value: Optional[float]) -> str:
    return _format_price(value) if value is not None else "N/A"


def _has_concrete_plan(signal: CommanderSignal) -> bool:
    if signal.trigger_line == "N/A" or signal.defense_line == "N/A":
        return False
    if signal.action not in COMMANDER_ACTIONS:
        return False
    return True


def _option_window_text(config: Any) -> str:
    min_days = int(getattr(config, "us_commander_option_min_dte", 14))
    max_days = int(getattr(config, "us_commander_option_max_dte", 45))
    max_days = max(min_days, max_days)
    if min_days <= 14 and max_days <= 45:
        return "2-6周"
    if max_days <= 60:
        return "3-8周"
    return f"{min_days}-{max_days}天"


def _option_budget_text(config: Any) -> str:
    risk_pct = float(getattr(config, "us_commander_option_max_risk_pct", 1.0))
    return f"单笔最多按账户 {risk_pct:.1f}% 以内的小额试错，期权权利金可能亏光"


def _option_plan_text(
    *,
    direction: str,
    snapshot: Optional[QuoteSnapshot],
    trigger: Optional[float],
    defense: Optional[float],
    config: Any,
    reason: str,
) -> str:
    if not bool(getattr(config, "us_commander_options_enabled", True)):
        return "期权功能关闭。"
    window = _option_window_text(config)
    budget = _option_budget_text(config)
    if direction == "call":
        strike = trigger or (snapshot.price * 1.02 if snapshot and snapshot.price else None)
        return (
            f"CALL 只做条件单思路：期限看 {window}，行权价看 {_line_or_na(strike)} 附近或略高一档；"
            f"只有站稳触发价再考虑，没站稳就不做。{budget}。理由：{reason}"
        )
    if direction == "put":
        strike = defense or (snapshot.price * 0.98 if snapshot and snapshot.price else None)
        return (
            f"PUT 只做保护/看跌思路：期限看 {window}，行权价看 {_line_or_na(strike)} 附近或略低一档；"
            f"只有跌破防守价或市场明显转坏再考虑。{budget}。理由：{reason}"
        )
    return "不做期权：当前没有清晰方向，期权时间损耗太快。"


def _action_plain_label(action: str) -> str:
    mapping = {
        "继续持有": "持有：先拿着，但按防守价执行",
        "减仓观察": "卖/减仓：先少拿一点，别硬扛",
        "禁止追高": "不买：涨太快，先别追",
        "等回踩": "等：回到舒服位置再看",
        "突破确认再看": "买入观察：站稳关键价再考虑",
        "加入关注": "只关注：先放进观察池",
        "风险优先处理": "卖/防守：先保护本金",
    }
    return mapping.get(action, action)


def _looks_like_price_line(value: str) -> bool:
    text = str(value or "").strip().replace(",", "")
    if not text or text == "N/A" or "%" in text:
        return False
    try:
        float(text)
    except ValueError:
        return False
    return True


def _price_line_value(value: str) -> Optional[float]:
    text = str(value or "").strip().replace(",", "")
    if not text or text == "N/A" or "%" in text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _risk_reward_value(value: str) -> Optional[float]:
    text = str(value or "").strip()
    if not text or text == "N/A" or ":" not in text:
        return None
    try:
        return float(text.split(":", 1)[0])
    except ValueError:
        return None


def _strike_reference_text(value: Optional[float]) -> str:
    if value is None:
        return "关键价"
    if value >= 100:
        return f"{round(value):.0f}"
    if value >= 20:
        return f"{value:.0f}"
    return f"{value:.1f}"


def _direct_position_size(signal: CommanderSignal) -> str:
    rr = _risk_reward_value(signal.risk_reward)
    if rr is not None and rr > 1.5 and signal.score >= 72:
        return "1/3"
    return "1/4"


def _direct_command_for_signal(signal: CommanderSignal, config: Any) -> CommanderCommand:
    trigger = signal.trigger_line
    defense = signal.defense_line
    trigger_value = _price_line_value(trigger)
    defense_value = _price_line_value(defense)
    rr = _risk_reward_value(signal.risk_reward)
    window = _option_window_text(config)
    evidence = "；".join(signal.evidence[:2])
    why = f"{signal.status}。{signal.plain_explanation}"
    if evidence:
        why = f"{why} 依据：{evidence}。"

    if not signal.quote_actionable:
        return CommanderCommand(
            conclusion="结论：数据不一致，先不交易",
            stock=(
                f"不减仓、不加仓；等券商实时价或下一次可靠报价确认。"
                f"关键价先记 {trigger}，但现在不按它行动。"
            ),
            option="不做期权；行情不可信时，CALL/PUT 都容易建立在错误价格上。",
            cancel=f"行情可信度恢复前，取消所有买/卖/期权动作；{signal.quote_warning}",
            why=why,
        )

    if signal.category == "market":
        if signal.action == "风险优先处理":
            return CommanderCommand(
                conclusion="结论：主线转弱，今天先防守",
                stock="相关个股不加仓；已经跌破防守价的，先减 1/3。",
                option=f"不单独追 ETF 期权；只有你的持仓同步破位，才看 {window} PUT 保护。",
                cancel="主线重新转强前，不做新的进攻动作。",
                why=why,
            )
        return CommanderCommand(
            conclusion="结论：主线支持持有，但别追",
            stock="已有仓继续按防守线拿；不要只因为指数强就加仓。",
            option=f"不急做期权；个股站稳自己的触发价后，才看 {window} CALL。",
            cancel="如果主线转弱，所有买入想法降级。",
            why=why,
        )

    if signal.category == "opportunity":
        if signal.action == "禁止追高":
            return CommanderCommand(
                conclusion="结论：现在不买，涨太快",
                stock=f"不追；等回到 {trigger} 附近再重新评估。",
                option="不做 CALL；涨太快时 CALL 容易买在情绪最热的位置。",
                cancel=f"跌破 {defense}，这条机会先取消。",
                why=why,
            )
        if signal.action == "突破确认再看":
            if rr is None or rr < 1.0:
                return CommanderCommand(
                    conclusion="结论：现在不买，性价比太差",
                    stock=f"就算站上 {trigger}，今天也先不进；等回踩出更好买点。",
                    option="不做 CALL；风险收益比低于 1，时间损耗不划算。",
                    cancel=f"跌破 {defense}，取消买入想法。",
                    why=f"{why} 风险收益比 {signal.risk_reward}，这笔不值得硬做。",
                )
            size = _direct_position_size(signal)
            strike = _strike_reference_text(trigger_value)
            return CommanderCommand(
                conclusion=f"结论：站稳 {trigger} 后试买 {size}",
                stock=f"只有站稳 {trigger} 才动，最多试 {size} 仓；没站稳不买。",
                option=f"站稳后再看 {window} CALL，行权价参考 {strike} 附近或略高一档。",
                cancel=f"跌破 {defense}，取消买入想法。",
                why=why,
            )
        return CommanderCommand(
            conclusion="结论：现在不买，只观察",
            stock=f"先放观察池；站稳 {trigger} 前不动。",
            option="不做期权；现在不是进场点。",
            cancel=f"跌破 {defense}，从机会池降级。",
            why=why,
        )

    if signal.action in {"风险优先处理", "减仓观察"}:
        strike = _strike_reference_text(defense_value)
        return CommanderCommand(
            conclusion="结论：现在减 1/3，先防守",
            stock=f"先减 1/3；没有重新站回 {trigger} 前，不加仓。",
            option=f"若继续跌破 {defense}，可看 {window} PUT，行权价参考 {strike} 附近或略低一档。",
            cancel=f"重新站回 {trigger}，暂停继续减仓；跌破 {defense}，继续降风险。",
            why=why,
        )
    if signal.action == "禁止追高":
        return CommanderCommand(
            conclusion="结论：持有，不追",
            stock=f"有仓先拿；新仓不买。冲不上 {signal.target_line}，可减 1/4 锁利润。",
            option="不做 CALL；短线过热时，期权容易亏时间价值。",
            cancel=f"跌破 {defense}，减 1/3。",
            why=why,
        )
    if signal.status == "跌幅未被关键价确认":
        return CommanderCommand(
            conclusion="结论：先持有，不减仓",
            stock=f"不加仓也不减仓；只有跌破 {defense} 才考虑减 1/3。",
            option="不做 PUT；只是跌幅不好看，还没确认破位。",
            cancel=f"重新站回 {trigger}，风险提示解除；跌破 {defense} 再处理。",
            why=why,
        )
    if signal.action == "继续持有":
        return CommanderCommand(
            conclusion="结论：持有，不加仓",
            stock=f"已有仓继续拿；站稳 {trigger} 才考虑加 1/4，跌破 {defense} 减 1/3。",
            option="不做期权；等更明确的突破或破位。",
            cancel=f"跌破 {defense}，执行减仓；没跌破就不乱动。",
            why=why,
        )
    return CommanderCommand(
        conclusion=f"结论：现在不买，等 {trigger} 站稳",
        stock=f"只有站稳 {trigger} 后，最多试 1/4 仓；没站稳不买。",
        option="不做期权；方向没确认前 CALL/PUT 都容易被震荡消耗。",
        cancel=f"跌破 {defense}，取消买入想法；已有仓考虑减 1/3。",
        why=why,
    )


def _is_immediate_action_signal(signal: CommanderSignal) -> bool:
    if not signal.quote_actionable:
        return False
    if signal.category == "market":
        return signal.action == "风险优先处理"
    if signal.category == "holding":
        return signal.action in {"风险优先处理", "减仓观察", "禁止追高"}
    if signal.category == "opportunity":
        rr = _risk_reward_value(signal.risk_reward)
        return signal.action == "突破确认再看" and rr is not None and rr >= 1.0
    return False


def _term_explanations(signal: CommanderSignal, *, max_items: int = 2) -> List[str]:
    text = " ".join([
        signal.action,
        signal.status,
        signal.trigger_line,
        signal.defense_line,
        signal.target_line,
        signal.risk_reward,
        signal.plain_explanation,
        signal.learning_note,
        " ".join(signal.evidence),
        signal.option_instruction,
        signal.option_plan,
    ])
    explanations: List[str] = []
    seen_concepts = set()
    candidates = [
        ("MA20", "ma20", "MA20 就是 20日均线，可以理解为近一个月很多人看的防线。"),
        ("20日均线", "ma20", "20日均线就是近一个月很多人看的防线。"),
        ("乖离率", "bias", "乖离率就是价格涨太快，离正常节奏有点远。"),
        ("VIX", "vix", "VIX 是恐慌指数，上升通常代表市场更紧张。"),
        ("CALL", "call", "CALL 是看涨期权，适合用小额资金押“站稳后继续涨”。"),
        ("PUT", "put", "PUT 是看跌/保护期权，适合用小额资金防“继续跌”。"),
        ("风险收益比", "risk_reward", "风险收益比就是这笔值不值得冒险，越高越划算。"),
        ("突破确认", "breakout", "突破确认不是看到涨就追，而是等价格站稳关键价。"),
        ("减仓观察", "trim", "减仓观察不是清仓，是先少拿一点，别硬扛。"),
    ]
    for term, concept, explanation in candidates:
        if term in text and concept not in seen_concepts:
            seen_concepts.add(concept)
            explanations.append(explanation)
        if len(explanations) >= max_items:
            break
    if not explanations and signal.learning_note:
        explanations.append(signal.learning_note)
    return explanations[:max_items]


def _market_temperature(snapshots: Dict[str, QuoteSnapshot]) -> MarketTemperature:
    score = 50.0
    evidence: List[str] = []

    def add(code: str, weight: float, positive_when_up: bool = True, label: str = "") -> None:
        nonlocal score
        snapshot = snapshots.get(code)
        if not snapshot or snapshot.change_pct is None:
            return
        if not _quote_is_actionable(snapshot):
            return
        direction = 1 if positive_when_up else -1
        score += snapshot.change_pct * weight * direction
        if abs(snapshot.change_pct) >= 0.3:
            name = label or PLAIN_MARKET_LABELS.get(code, code)
            evidence.append(f"{name} {_format_pct(snapshot.change_pct)}")

    add("SPY", 7.0, True, "大盘")
    add("QQQ", 6.0, True, "科技")
    add("SMH", 4.0, True, "半导体")
    add("VIX", 3.5, False, "恐慌情绪")
    add("HYG", 5.0, True, "信用风险偏好")
    add("TLT", 3.0, True, "利率压力")
    add("UUP", 2.5, False, "美元压力")
    add("GLD", 1.5, False, "避险情绪")

    final_score = _clamp_score(score)
    if final_score >= 68:
        stance = "可小幅进攻"
        summary = "市场温度偏暖，可以看强势股，但仍等触发位，不追高。"
    elif final_score <= 42:
        stance = "偏防守"
        summary = "市场温度偏冷，先保护持仓，新增机会只观察不急做。"
    else:
        stance = "攻守平衡"
        summary = "市场温度中性，持仓按防守线执行，机会等确认。"

    lines = [
        _market_mood_line(code, snapshots[code])
        for code in DEFAULT_RISK_PROXY_ORDER
        if code in snapshots
    ]
    if evidence:
        summary = f"{summary} 主要线索：{'；'.join(evidence[:4])}。"
    return MarketTemperature(score=final_score, stance=stance, summary=summary, lines=lines)


def _signal_dict(signal: CommanderSignal) -> Dict[str, Any]:
    return {
        "code": signal.code,
        "category": signal.category,
        "priority": signal.priority,
        "score": signal.score,
        "action": signal.action,
        "status": signal.status,
        "trigger_line": signal.trigger_line,
        "defense_line": signal.defense_line,
        "target_line": signal.target_line,
        "risk_reward": signal.risk_reward,
        "evidence": signal.evidence,
        "confidence": signal.confidence,
        "plain_explanation": signal.plain_explanation,
        "learning_note": signal.learning_note,
        "stock_instruction": signal.stock_instruction,
        "option_instruction": signal.option_instruction,
        "option_plan": signal.option_plan,
        "quote_quality_level": signal.quote_quality_level,
        "quote_actionable": signal.quote_actionable,
        "quote_warning": signal.quote_warning,
    }


def _commander_memory_dir() -> Path:
    raw_dir = os.getenv(
        "US_COMMANDER_MEMORY_DIR",
        "~/Library/Application Support/us-intraday-radar/commander-state",
    )
    return Path(raw_dir).expanduser()


def _commander_memory_path(match: IntradayWindowMatch) -> Path:
    return _commander_memory_dir() / f"us-commander-state-{match.now.strftime('%Y%m%d')}.json"


def _load_commander_memory(match: IntradayWindowMatch) -> Dict[str, Any]:
    path = _commander_memory_path(match)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[IntradayRadar] 指挥官记忆读取失败，将按首次运行处理: %s", exc)
        return {}


def _write_commander_memory(match: IntradayWindowMatch, decision: CommanderDecision) -> str:
    path = _commander_memory_path(match)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "date": match.now.strftime("%Y-%m-%d"),
        "window": match.window.key,
        "updated_at": match.now.isoformat(),
        "market": {
            "score": decision.market.score,
            "stance": decision.market.stance,
            "summary": decision.market.summary,
        },
        "signals": {
            signal.code: _signal_dict(signal)
            for signal in decision.holding_signals + decision.market_signals + decision.opportunity_signals
        },
    }
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _apply_commander_memory(signals: List[CommanderSignal], previous_state: Dict[str, Any]) -> None:
    previous_signals = previous_state.get("signals") if isinstance(previous_state, dict) else None
    if not isinstance(previous_signals, dict):
        return
    for signal in signals:
        old = previous_signals.get(signal.code)
        if not isinstance(old, dict):
            signal.change_note = "新信号"
            continue
        old_action = str(old.get("action") or "")
        old_score = _as_float(old.get("score")) or 0.0
        score_delta = signal.score - old_score
        if old_action and old_action != signal.action:
            signal.change_note = f"动作变化：{old_action} → {signal.action}"
        elif score_delta >= 12:
            signal.change_note = "较上次变强"
        elif score_delta <= -12:
            signal.change_note = "较上次变弱"
        else:
            signal.change_note = "较上次基本不变"


def _holding_commander_signal(
    code: str,
    snapshot: Optional[QuoteSnapshot],
    *,
    config: Any,
    market: MarketTemperature,
) -> CommanderSignal:
    if not snapshot or snapshot.price is None:
        return CommanderSignal(
            code=code,
            category="holding",
            priority=3,
            score=0,
            action="加入关注",
            status="数据不足",
            trigger_line="等行情恢复",
            defense_line="N/A",
            target_line="N/A",
            risk_reward="N/A",
            evidence=["当前拿不到可用行情"],
            confidence=0.2,
            plain_explanation="数据不足，只观察，不做动作。",
            learning_note="数据缺口：没有价格就没有交易计划，先不凭感觉行动。",
            stock_instruction="不买/不卖：数据不足，先不动作",
            option_instruction="不做期权",
            option_plan="没有可靠价格时不碰期权。",
            quote_quality_level="低",
            quote_actionable=False,
            quote_warning="当前拿不到可用行情",
        )

    if not _quote_is_actionable(snapshot):
        trigger = _resistance_price(snapshot)
        defense = _stop_price(snapshot) or _support_price(snapshot)
        warning = _quote_quality_text(snapshot)
        return CommanderSignal(
            code=code,
            category="holding",
            priority=3,
            score=35,
            action="继续持有",
            status="行情未确认",
            trigger_line=_line_or_na(trigger),
            defense_line=_line_or_na(defense),
            target_line=_line_or_na(trigger),
            risk_reward="N/A",
            evidence=[
                warning,
                f"现价 {_format_price(snapshot.price)}",
                f"涨跌 {_format_pct(snapshot.change_pct)}",
            ],
            confidence=0.25,
            plain_explanation="当前免费行情源不够可靠，先不按这个价格买卖，等开盘价或券商实时价确认。",
            learning_note="行情可信度：报价要同时有价格、时间和交易阶段，才适合拿来做动作。",
            stock_instruction="不买/不卖：等行情确认",
            option_instruction="不做期权",
            option_plan="不做期权：行情不可信时，CALL/PUT 都容易建立在错误价格上。",
            quote_quality_level=_quality_label(snapshot.quality.level),
            quote_actionable=False,
            quote_warning=warning,
        )

    multiplier = _risk_style_multiplier(config)
    holding_threshold = float(getattr(config, "us_intraday_alert_holding_change_pct", 2.5)) * multiplier
    bias_threshold = float(getattr(config, "bias_threshold", 5.0)) * multiplier
    change = snapshot.change_pct or 0.0
    support = _support_price(snapshot)
    resistance = _resistance_price(snapshot)
    stop = _stop_price(snapshot)
    trigger = resistance
    defense = stop
    evidence = [
        f"涨跌 {_format_pct(snapshot.change_pct)}",
        f"现价 {_format_price(snapshot.price)}",
    ]
    if snapshot.ma20:
        evidence.append(f"MA20 {_format_price(snapshot.ma20)}")
    if snapshot.bias_pct is not None:
        evidence.append(f"乖离率 {snapshot.bias_pct:+.2f}%")

    if snapshot.ma20 and snapshot.price < snapshot.ma20:
        action = "风险优先处理"
        status = "跌破近期防线"
        score = 95
        trigger = snapshot.ma20
        defense = stop
        plain = "先防守，别加仓；只有重新站回近期防线，才恢复观察。"
        note = "MA20：20日均线，常被用作波段持仓的重要防线。"
        stock_instruction = "卖/减仓：先防守，别加仓"
        option_instruction = "PUT保护优先"
        option_plan = _option_plan_text(
            direction="put",
            snapshot=snapshot,
            trigger=trigger,
            defense=defense,
            config=config,
            reason="价格跌破近一个月防线，先保护仓位。",
        )
        priority = 0
    elif change <= -abs(holding_threshold) and _holding_weakness_confirmed(snapshot):
        action = "减仓观察"
        status = "盘中明显走弱"
        score = 84
        trigger = resistance
        defense = stop
        plain = "今天转弱，先检查仓位和止损线，不急着补仓。"
        note = "减仓观察：不是立刻清仓，而是先把风险降下来，等重新转强。"
        stock_instruction = "卖/减仓：先少拿一点，别硬扛"
        option_instruction = "PUT观察"
        option_plan = _option_plan_text(
            direction="put",
            snapshot=snapshot,
            trigger=trigger,
            defense=defense,
            config=config,
            reason="盘中转弱，若继续跌破防守价，可以用小额 PUT 做保护。",
        )
        priority = 0
    elif change <= -abs(holding_threshold):
        action = "继续持有"
        status = "跌幅未被关键价确认"
        score = 52
        trigger = resistance
        defense = support or stop
        plain = "只看到跌幅还不够，价格没有确认跌破关键防线前，不直接减仓。"
        note = "确认破位：不是看到跌幅就卖，而是价格真的跌破关键防线才处理。"
        stock_instruction = "持有不加：等关键价确认"
        option_instruction = "不做期权"
        option_plan = "不做期权：跌幅没有关键价确认，PUT 容易做在假信号上。"
        priority = 2
    elif snapshot.bias_pct is not None and snapshot.bias_pct > bias_threshold:
        action = "禁止追高"
        status = "短线涨太快"
        score = 80
        trigger = support
        defense = stop
        plain = "涨得快但位置不舒服，等回踩到计划区再看。"
        note = "乖离率：价格离短期均线太远，代表短线追高风险。"
        stock_instruction = "持有不追：有仓先拿，没仓不买"
        option_instruction = "不做CALL"
        option_plan = "不建议追 CALL：涨太快时买 CALL，容易买在情绪最热的时候。"
        priority = 1
    elif change >= abs(holding_threshold):
        action = "继续持有"
        status = "强势运行"
        score = 74 if market.score >= 42 else 66
        trigger = resistance
        defense = support or stop
        plain = "走势偏强，已有仓位先拿着；只有突破确认才考虑加。"
        note = "突破确认：不是看到上涨就追，而是等价格站上压力位。"
        stock_instruction = "持有：先拿着，突破后再考虑加"
        option_instruction = "CALL观察"
        option_plan = _option_plan_text(
            direction="call",
            snapshot=snapshot,
            trigger=trigger,
            defense=defense,
            config=config,
            reason="已有强势，但要等站稳上方关键价，避免追高。",
        )
        priority = 1
    elif snapshot.ma5 and snapshot.ma10 and snapshot.ma20 and snapshot.ma5 > snapshot.ma10 > snapshot.ma20:
        action = "继续持有"
        status = "趋势还没坏"
        score = 58
        trigger = resistance
        defense = support or stop
        plain = "没有新动作，按原计划持有，破防守线再处理。"
        note = "多头排列：短期均线在中长期均线上方，说明趋势仍偏正。"
        stock_instruction = "持有：没坏就先拿着"
        option_instruction = "不做期权"
        option_plan = "不做期权：信号不够强，期权会被时间损耗消耗。"
        priority = 2
    else:
        action = "突破确认再看"
        status = "方向未确认"
        score = 45
        trigger = resistance
        defense = support or stop
        plain = "现在信号不够强，等站上触发线再说。"
        note = "确认信号：先让价格证明自己，再行动。"
        stock_instruction = "不买：等站稳关键价"
        option_instruction = "不做期权"
        option_plan = "不做期权：方向没确认，CALL/PUT 都容易被震荡消耗。"
        priority = 3

    if market.score <= 42 and action in {"继续持有", "突破确认再看"}:
        score = max(0, score - 8)
        evidence.append("市场温度偏冷")

    return CommanderSignal(
        code=code,
        category="holding",
        priority=priority,
        score=_clamp_score(score),
        action=action,
        status=status,
        trigger_line=_line_or_na(trigger),
        defense_line=_line_or_na(defense),
        target_line=_line_or_na(resistance),
        risk_reward=_risk_reward_text(snapshot),
        evidence=evidence,
        confidence=0.75 if score >= 70 else 0.55,
        plain_explanation=plain,
        learning_note=note,
        stock_instruction=stock_instruction,
        option_instruction=option_instruction,
        option_plan=option_plan,
        quote_quality_level=_quality_label(snapshot.quality.level),
        quote_actionable=snapshot.quality.is_actionable,
        quote_warning=_quote_quality_text(snapshot),
    )


def _market_commander_signals(
    snapshots: Dict[str, QuoteSnapshot],
    *,
    config: Any,
    market: MarketTemperature,
) -> List[CommanderSignal]:
    vix_threshold = float(getattr(config, "us_intraday_alert_vix_change_pct", 5.0))
    index_threshold = float(getattr(config, "us_intraday_alert_index_change_pct", 1.0))
    signals: List[CommanderSignal] = []

    vix = snapshots.get("VIX")
    if vix and _quote_is_actionable(vix) and vix.change_pct is not None and abs(vix.change_pct) >= vix_threshold:
        action = "风险优先处理" if vix.change_pct > 0 else "继续持有"
        signals.append(CommanderSignal(
            code="VIX",
            category="market",
            priority=1,
            score=86 if vix.change_pct > 0 else 72,
            action=action,
            status="恐慌情绪升温" if vix.change_pct > 0 else "恐慌情绪降温",
            trigger_line=f"VIX {_format_pct(vix.change_pct)}",
            defense_line="新开仓先降速" if vix.change_pct > 0 else "按持仓防守线执行",
            target_line="等 VIX 降温" if vix.change_pct > 0 else "观察科技/半导体能否延续",
            risk_reward="N/A",
            evidence=[f"VIX {_format_pct(vix.change_pct)}"],
            confidence=0.7,
            plain_explanation="市场情绪变化会影响所有成长股，先决定今天进攻还是防守。",
            learning_note="VIX：恐慌指数，上升通常代表市场更紧张。",
            stock_instruction="减少进攻" if vix.change_pct > 0 else "持有观察",
            option_instruction="PUT保护优先" if vix.change_pct > 0 else "不急做期权",
            option_plan=(
                "市场恐慌升温时，若持仓同步跌破防守价，可优先看小额 PUT 保护。"
                if vix.change_pct > 0
                else "恐慌降温时不急着买期权，先看持仓是否站稳。"
            ),
        ))

    for code, label in (("QQQ", "科技主线"), ("SMH", "半导体主线"), ("SPY", "大盘")):
        snapshot = snapshots.get(code)
        if not snapshot or snapshot.change_pct is None or abs(snapshot.change_pct) < index_threshold:
            continue
        if not _quote_is_actionable(snapshot):
            continue
        weaker = snapshot.change_pct < 0
        signals.append(CommanderSignal(
            code=code,
            category="market",
            priority=2,
            score=78 if weaker else 70,
            action="风险优先处理" if weaker else "继续持有",
            status=f"{label}{'转弱' if weaker else '偏强'}",
            trigger_line=f"{label} {_format_pct(snapshot.change_pct)}",
            defense_line="若主线转弱，个股进攻信号降权",
            target_line="看持仓是否跟随主线",
            risk_reward="N/A",
            evidence=[f"{code} {_format_pct(snapshot.change_pct)}"],
            confidence=0.65,
            plain_explanation=f"{label}会影响相关个股，先看主线再看单票。",
            learning_note=f"{code}：用来观察{label}的方向。",
            stock_instruction="卖/防守：相关个股降速" if weaker else "持有：主线还支持",
            option_instruction="PUT观察" if weaker else "CALL观察",
            option_plan=(
                f"{label}转弱时，相关持仓若跌破防守价，再看小额 PUT；没跌破不急。"
                if weaker
                else f"{label}偏强时，相关候选只有站上触发价，才看 CALL。"
            ),
        ))
    return signals


def _opportunity_commander_signal(
    snapshot: QuoteSnapshot,
    *,
    config: Any,
    market: MarketTemperature,
) -> Optional[CommanderSignal]:
    if snapshot.price is None:
        return None
    if not _quote_is_actionable(snapshot):
        return None
    bias_threshold = float(getattr(config, "bias_threshold", 5.0)) * _risk_style_multiplier(config)
    raw_score = _opportunity_score(snapshot, bias_threshold=bias_threshold)
    bullish = bool(snapshot.ma5 and snapshot.ma10 and snapshot.ma20 and snapshot.ma5 > snapshot.ma10 > snapshot.ma20)
    safe_bias = snapshot.bias_pct is None or snapshot.bias_pct <= bias_threshold
    if market.score <= 42 and raw_score < 4:
        return None
    if not bullish and raw_score < 3:
        return None

    support = _support_price(snapshot)
    resistance = _resistance_price(snapshot)
    stop = _stop_price(snapshot)
    if snapshot.bias_pct is not None and snapshot.bias_pct > bias_threshold:
        action = "禁止追高"
        status = "机会但短线过热"
        score = 55
        plain = "可以放进观察池，但现在不适合追。"
        note = "观察池：先记录候选，不等于马上买。"
        stock_instruction = "不买：涨太快，等回落"
        option_instruction = "不做CALL"
        option_plan = "不建议追 CALL：短线过热时，哪怕方向对，也容易被回落和时间损耗伤到。"
    elif bullish and safe_bias and (snapshot.change_pct or 0) >= 0.5:
        action = "突破确认再看"
        status = "趋势候选"
        score = 64 + raw_score * 4 + max(0, market.score - 50) * 0.25
        plain = "有趋势基础，但必须站上触发线才值得进一步看。"
        note = "风险收益比：用可能上行空间和止损距离比较，太差就不做。"
        stock_instruction = "买入观察：站上关键价才考虑"
        option_instruction = "CALL观察"
        option_plan = _option_plan_text(
            direction="call",
            snapshot=snapshot,
            trigger=resistance,
            defense=stop or support,
            config=config,
            reason="趋势候选，但必须等价格站稳触发价。",
        )
    else:
        action = "加入关注"
        status = "候选观察"
        score = 56 + raw_score * 3
        plain = "先加入观察，不急着动手。"
        note = "加入关注：先盯条件，不代表买入。"
        stock_instruction = "只关注：还不到买点"
        option_instruction = "不做期权"
        option_plan = "不做期权：现在只是观察，不是进场点。"

    return CommanderSignal(
        code=snapshot.code,
        category="opportunity",
        priority=3,
        score=_clamp_score(score),
        action=action,
        status=status,
        trigger_line=_line_or_na(resistance),
        defense_line=_line_or_na(stop or support),
        target_line=_line_or_na(resistance),
        risk_reward=_risk_reward_text(snapshot),
        evidence=[
            f"涨跌 {_format_pct(snapshot.change_pct)}",
            f"现价 {_format_price(snapshot.price)}",
            f"机会分 {raw_score:.1f}",
        ],
        confidence=0.55,
        plain_explanation=plain,
        learning_note=note,
        stock_instruction=stock_instruction,
        option_instruction=option_instruction,
        option_plan=option_plan,
        quote_quality_level=_quality_label(snapshot.quality.level),
        quote_actionable=snapshot.quality.is_actionable,
        quote_warning=_quote_quality_text(snapshot),
    )


def build_us_commander_decision(
    *,
    config: Any,
    match: IntradayWindowMatch,
    snapshots: Dict[str, QuoteSnapshot],
    previous_state: Optional[Dict[str, Any]] = None,
) -> CommanderDecision:
    holding_codes = _dedupe_codes(getattr(config, "portfolio_stock_list", []) or [])
    opportunity_max = int(getattr(config, "us_commander_max_opportunities", 3))
    max_actions = int(getattr(config, "us_commander_max_actions", 5))
    min_alert_score = int(getattr(config, "us_commander_min_alert_score", 70))

    market = _market_temperature(snapshots)
    holding_signals = [
        _holding_commander_signal(code, snapshots.get(code), config=config, market=market)
        for code in holding_codes
    ]
    market_signals = _market_commander_signals(snapshots, config=config, market=market)

    opportunity_signals = []
    holding_set = set(holding_codes)
    for code, snapshot in snapshots.items():
        if code in holding_set or code in US_RISK_PROXIES:
            continue
        signal = _opportunity_commander_signal(snapshot, config=config, market=market)
        if signal:
            opportunity_signals.append(signal)
    opportunity_signals.sort(key=lambda item: (item.score, _opportunity_score(snapshots[item.code], bias_threshold=5.0)), reverse=True)
    opportunity_signals = opportunity_signals[:opportunity_max]

    all_signals = holding_signals + market_signals + opportunity_signals
    if bool(getattr(config, "us_commander_memory_enabled", True)) and previous_state:
        _apply_commander_memory(all_signals, previous_state)

    action_candidates = [
        signal for signal in all_signals
        if (
            signal.score >= min_alert_score
            and _has_concrete_plan(signal)
            and _is_immediate_action_signal(signal)
        )
    ]
    action_candidates.sort(key=lambda item: (item.priority, -item.score, item.code))
    return CommanderDecision(
        market=market,
        holding_signals=holding_signals,
        market_signals=market_signals,
        opportunity_signals=opportunity_signals,
        action_signals=action_candidates[:max_actions],
    )


def _commander_signal_line(
    signal: CommanderSignal,
    *,
    compact: bool = False,
    show_term_explanations: bool = True,
    config: Any = None,
) -> str:
    command = _direct_command_for_signal(signal, config)
    if compact:
        return (
            f"- **{signal.code}｜{command.conclusion}**：股票：{command.stock}｜"
            f"期权：{command.option}｜"
            f"取消：{command.cancel}｜{signal.change_note}"
        )

    term_text = ""
    if show_term_explanations:
        explanations = _term_explanations(signal, max_items=1)
        if explanations:
            term_text = f"\n  专业词：{explanations[0]}"

    return (
        f"- **{signal.code}｜{command.conclusion}**\n"
        f"  股票：{command.stock}\n"
        f"  期权：{command.option}\n"
        f"  取消：{command.cancel}\n"
        f"  为什么：{command.why}\n"
        f"  较上次：{signal.change_note}。"
        f"{term_text}"
    )


def _commander_summary(decision: CommanderDecision) -> str:
    data_hits = [item.code for item in decision.holding_signals if not item.quote_actionable]
    if data_hits:
        return f"今天主策略：先校验行情。{'、'.join(data_hits[:3])} 的数据不够可靠，先不买不卖，等券商价或下一次可靠报价确认。"
    risk_hits = [item.code for item in decision.action_signals if item.action in {"风险优先处理", "减仓观察"}]
    opportunity_hits = [
        item.code for item in decision.opportunity_signals
        if item.score >= 70 and (_risk_reward_value(item.risk_reward) or 0) >= 1.0
    ]
    if risk_hits:
        return f"今天主策略：防守。先处理 {'、'.join(risk_hits[:3])}，该减 1/3 就减，不加仓硬扛。"
    if decision.market.score <= 42:
        return "今天主策略：防守。不急着买，先看有没有该卖/该减的。"
    if opportunity_hits:
        return f"今天主策略：小仓进攻。只盯 {'、'.join(opportunity_hits[:3])}，站稳触发价才试 1/4 或 1/3。"
    return "今天主策略：观望/持有。没到关键价，不买也不卖。"


def _pre_open_fast_mode_enabled(config: Any, match: IntradayWindowMatch) -> bool:
    return (
        match.window.key == "pre_open"
        and bool(getattr(config, "us_intraday_pre_open_fast_mode", True))
    )


def _brief_summary_text(decision: CommanderDecision) -> str:
    summary = _commander_summary(decision)
    for prefix in ("今天主策略：", "主策略："):
        if summary.startswith(prefix):
            summary = summary[len(prefix):]
    return summary.split("。", 1)[0].strip() or "观望"


def _brief_signal_text(signal: CommanderSignal, config: Any) -> str:
    if not signal.quote_actionable:
        return f"{signal.code}｜不交易｜行情不一致，等确认"
    trigger = signal.trigger_line
    defense = signal.defense_line
    if signal.category == "opportunity":
        rr = _risk_reward_value(signal.risk_reward)
        if signal.action == "突破确认再看" and rr is not None and rr >= 1.0:
            return f"{signal.code}｜站稳 {trigger} 试{_direct_position_size(signal)}｜破 {defense} 取消"
        return f"{signal.code}｜不买｜等更好价格"
    if signal.action in {"风险优先处理", "减仓观察"}:
        return f"{signal.code}｜减1/3｜站回 {trigger} 暂停"
    if signal.action == "禁止追高":
        return f"{signal.code}｜持有不追｜破 {defense} 减1/3"
    if signal.status == "跌幅未被关键价确认":
        return f"{signal.code}｜先持有｜破 {defense} 再减"
    if signal.action == "继续持有":
        return f"{signal.code}｜持有不加｜破 {defense} 减1/3"
    return f"{signal.code}｜不买｜站稳 {trigger} 再看"


def _format_us_commander_brief_report(
    *,
    config: Any,
    match: IntradayWindowMatch,
    decision: CommanderDecision,
) -> str:
    title = "盘前" if match.window.key == "pre_open" else match.window.label
    max_lines = int(getattr(config, "us_commander_brief_max_lines", 8))

    priority_signals: List[CommanderSignal] = []
    seen = set()
    for signal in decision.action_signals:
        if signal.code not in seen:
            priority_signals.append(signal)
            seen.add(signal.code)
    for signal in decision.holding_signals:
        if signal.code in seen:
            continue
        if (
            not signal.quote_actionable
            or signal.action in {"风险优先处理", "减仓观察", "禁止追高"}
            or signal.status == "跌幅未被关键价确认"
        ):
            priority_signals.append(signal)
            seen.add(signal.code)
    if not priority_signals:
        priority_signals = decision.holding_signals[: min(3, len(decision.holding_signals))]

    watch_lines = [
        _brief_signal_text(signal, config)
        for signal in priority_signals[:max_lines]
    ] or ["暂无必须处理"]

    buy_lines: List[str] = []
    for signal in decision.opportunity_signals:
        rr = _risk_reward_value(signal.risk_reward)
        if signal.quote_actionable and signal.action == "突破确认再看" and rr is not None and rr >= 1.0:
            buy_lines.append(_brief_signal_text(signal, config))
        if len(buy_lines) >= 3:
            break
    if not buy_lines:
        buy_lines = ["暂无"]

    return "\n".join([
        f"{title}｜主策略：{_brief_summary_text(decision)}",
        "",
        "要看：",
        *watch_lines,
        "",
        "可买：",
        *buy_lines,
        "",
        "仅辅助判断，不自动交易。",
    ]).strip() + "\n"


def _format_us_commander_report(
    *,
    config: Any,
    match: IntradayWindowMatch,
    decision: CommanderDecision,
    commander_note: Optional[str] = None,
) -> str:
    forced_note = " | 手动测试" if match.forced else ""
    now_text = match.now.strftime("%m-%d %H:%M ET")
    show_term_explanations = bool(getattr(config, "us_commander_show_term_explanations", True))
    max_learning_notes = int(getattr(config, "us_commander_max_learning_notes", 3))
    action_lines = [
        _commander_signal_line(
            signal,
            show_term_explanations=show_term_explanations,
            config=config,
        )
        for signal in decision.action_signals
    ] or ["- 暂无必须马上买/卖的信号；没到价就不动。"]

    holding_lines = [
        _commander_signal_line(
            signal,
            compact=True,
            show_term_explanations=show_term_explanations,
            config=config,
        )
        for signal in decision.holding_signals
    ] or ["- 未配置真实持仓列表。"]

    opportunity_lines = []
    for signal in decision.opportunity_signals:
        if _has_concrete_plan(signal):
            opportunity_lines.append(
                _commander_signal_line(
                    signal,
                    show_term_explanations=show_term_explanations,
                    config=config,
                )
            )
    if not opportunity_lines:
        opportunity_lines = ["- 暂无可操作机会；别为了交易而交易。"]

    learning_notes = []
    seen_notes = set()
    for signal in decision.action_signals + decision.holding_signals + decision.opportunity_signals:
        note = signal.learning_note.strip()
        if note and note not in seen_notes:
            seen_notes.add(note)
            learning_notes.append(f"- {note}")
        if len(learning_notes) >= max_learning_notes:
            break

    report = [
        f"# 美股智慧指挥官：{match.window.label}",
        "",
        f"{now_text}{forced_note} | {match.window.focus}",
        "提醒：这是条件型辅助判断，不是自动买卖指令。",
        "",
        "## 今天主策略",
        _commander_summary(decision),
        f"- 市场温度：{decision.market.stance} {decision.market.score}/100。{decision.market.summary}",
    ]
    low_quality_lines = [
        f"- **{signal.code}**：{signal.quote_warning}"
        for signal in decision.holding_signals
        if not signal.quote_actionable
    ]
    if low_quality_lines:
        report.extend([
            "",
            "## 行情校验",
            "以下标的行情可信度不足，本次只观察，不给买/卖/期权动作：",
            *low_quality_lines[:5],
        ])
    if commander_note:
        report.extend(["", "## 指挥官补充", commander_note.strip()])
    report.extend([
        "",
        "## 需要你马上看",
        *action_lines,
        "",
        "## 你的持仓",
        *holding_lines,
        "",
        "## 市场温度",
        *(decision.market.lines or ["- 暂无市场环境数据。"]),
        "",
        "## 可以盯的机会",
        *opportunity_lines,
        "",
        "## 今天顺便学一个词",
        *(learning_notes or ["- 触发条件：先让价格到达计划位置，再考虑行动。"]),
        "",
        "## 纪律提醒",
        "- 没有触发价就不行动；先保护本金，再考虑机会。",
        "- 期权只作为小额条件战术或保护思路，权利金可能亏光，不能当成重仓押注。",
    ])
    return "\n".join(report).strip() + "\n"


def build_us_commander_report(
    *,
    config: Any,
    match: IntradayWindowMatch,
    snapshots: Dict[str, QuoteSnapshot],
    previous_state: Optional[Dict[str, Any]] = None,
    commander_note: Optional[str] = None,
) -> Tuple[str, CommanderDecision]:
    decision = build_us_commander_decision(
        config=config,
        match=match,
        snapshots=snapshots,
        previous_state=previous_state,
    )
    if bool(getattr(config, "us_commander_brief_mode", True)):
        report = _format_us_commander_brief_report(
            config=config,
            match=match,
            decision=decision,
        )
        return report, decision
    report = _format_us_commander_report(
        config=config,
        match=match,
        decision=decision,
        commander_note=commander_note,
    )
    return report, decision


def _should_call_commander_llm(config: Any, match: IntradayWindowMatch, decision: CommanderDecision) -> bool:
    if _pre_open_fast_mode_enabled(config, match):
        return False
    mode = str(getattr(config, "us_commander_llm_mode", "triggered") or "triggered").lower()
    if mode in {"off", "false", "none"}:
        return False
    if mode == "always":
        return True
    min_alert_score = int(getattr(config, "us_commander_min_alert_score", 70))
    if match.window.key in COMMANDER_LLM_KEY_WINDOWS:
        return True
    return any(signal.score >= min_alert_score + 10 for signal in decision.action_signals)


def _build_commander_llm_note(
    *,
    config: Any,
    match: IntradayWindowMatch,
    decision: CommanderDecision,
) -> Optional[str]:
    if not _should_call_commander_llm(config, match, decision):
        return None
    model = (
        getattr(config, "agent_litellm_model", "")
        or getattr(config, "litellm_model", "")
        or ""
    ).strip()
    if not model:
        return None
    try:
        import litellm
    except Exception as exc:
        logger.debug("[IntradayRadar] litellm unavailable for commander note: %s", exc)
        return None

    signals = [
        {
            "code": item.code,
            "action": item.action,
            "status": item.status,
            "score": item.score,
            "trigger": item.trigger_line,
            "defense": item.defense_line,
            "quote_actionable": item.quote_actionable,
            "quote_warning": item.quote_warning,
            "evidence": item.evidence[:3],
        }
        for item in decision.action_signals[:5]
    ]
    prompt = (
        "你是美股盘中投资指挥助手。请用中文给出不超过120字的补充判断，"
        "只基于给定信号，不得编造新闻或价格。行情不可信时不得给买卖或期权动作。"
        "格式：现在最重要的是...；如果...就...；否则...\n"
        f"窗口：{match.window.label}\n"
        f"市场：{decision.market.stance} {decision.market.score}/100 {decision.market.summary}\n"
        f"信号：{json.dumps(signals, ensure_ascii=False)}"
    )
    try:
        response = litellm.completion(
            model=model,
            messages=[
                {"role": "system", "content": "你只输出中文短句，不输出 JSON，不给无条件买卖指令，不能覆盖规则层风控拦截。"},
                {"role": "user", "content": prompt},
            ],
            timeout=20,
        )
        content = response.choices[0].message.content if response and response.choices else ""
    except Exception as exc:
        logger.warning("[IntradayRadar] 指挥官 LLM 补充失败，使用规则版报告: %s", exc)
        return None
    content = (content or "").strip()
    if len(content) > 260:
        content = content[:260].rstrip() + "..."
    return content or None


def _market_mood_line(code: str, snapshot: QuoteSnapshot) -> str:
    label = PLAIN_MARKET_LABELS.get(code, code)
    if not _quote_is_actionable(snapshot):
        return f"- {label}：行情可信度{_quality_label(snapshot.quality.level)}，只观察不下结论。{_quote_quality_text(snapshot)}"
    change = snapshot.change_pct
    if change is None:
        return f"- {label}：暂无清晰变化"

    abs_change = abs(change)
    if code == "VIX":
        state = "升温" if change > 0 else "降温"
        if abs_change < 2:
            state = "基本平稳"
        return f"- {label}：{state}（{_format_pct(change)}）。它代表市场紧张程度。"
    if code == "TLT":
        state = "偏大" if change < -0.3 else "缓和" if change > 0.3 else "变化不大"
        return f"- {label}：利率压力{state}（{_format_pct(change)}）。长债跌通常表示利率压力偏大。"
    if code == "HYG":
        state = "升温" if change < -0.3 else "缓和" if change > 0.3 else "平稳"
        return f"- {label}：信用风险{state}（{_format_pct(change)}）。它能观察风险偏好。"
    if code == "UUP":
        state = "偏强" if change > 0.3 else "偏弱" if change < -0.3 else "平稳"
        return f"- {label}：美元{state}（{_format_pct(change)}）。美元太强时成长股常承压。"
    if code == "GLD":
        state = "升温" if change > 0.3 else "降温" if change < -0.3 else "平稳"
        return f"- {label}：避险情绪{state}（{_format_pct(change)}）。黄金偏强常代表资金偏谨慎。"

    state = "偏强" if change > 0.3 else "偏弱" if change < -0.3 else "平稳"
    return f"- {label}：{state}（{_format_pct(change)}）"


def _plain_item_line(code: str, snapshot: QuoteSnapshot, reason: str, action: str) -> str:
    return (
        f"- **{code}**：{reason}。建议：{action}。"
        f"涨跌 {_format_pct(snapshot.change_pct)}，价格 {_format_price(snapshot.price)}"
    )


def _plain_summary(
    *,
    action_items: List[Tuple[int, str, str]],
    holding_codes: set[str],
    match: IntradayWindowMatch,
) -> str:
    holding_hits = [code for _, code, _ in action_items if code in holding_codes]
    if holding_hits:
        joined = "、".join(holding_hits[:3])
        return f"现在先看你的持仓：{joined} 有变化，先处理风险再看机会。"
    if action_items:
        joined = "、".join(code for _, code, _ in action_items[:3])
        return f"市场有变化，先看 {joined}，不要急着追。"
    if match.window.key in {"power_hour", "close_15"}:
        return "暂时没有必须立刻处理的信号，尾盘按原计划检查仓位。"
    return "现在不用急着动，按计划观察，不追高。"


def build_us_intraday_readable_report(
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
    max_action_items = int(getattr(config, "us_intraday_max_action_items", 5))
    bias_threshold = float(getattr(config, "bias_threshold", 5.0))

    action_items: List[Tuple[int, str, str]] = []
    for code in holding_codes:
        snapshot = snapshots.get(code)
        if not snapshot:
            continue
        reason = _plain_risk_reason(
            snapshot,
            code=code,
            holding_codes=holding_codes,
            holding_threshold=holding_threshold,
            index_threshold=index_threshold,
            vix_threshold=vix_threshold,
            bias_threshold=bias_threshold,
        )
        if reason:
            action = _plain_action_for_snapshot(
                snapshot,
                holding_threshold=holding_threshold,
                bias_threshold=bias_threshold,
            )
            action_items.append((0, code, _plain_item_line(code, snapshot, reason, action)))

    for code in DEFAULT_RISK_PROXY_ORDER:
        snapshot = snapshots.get(code)
        if not snapshot or code in holding_codes:
            continue
        reason = _plain_risk_reason(
            snapshot,
            code=code,
            holding_codes=holding_codes,
            holding_threshold=holding_threshold,
            index_threshold=index_threshold,
            vix_threshold=vix_threshold,
            bias_threshold=bias_threshold,
        )
        if reason:
            label = PLAIN_MARKET_LABELS.get(code, code)
            line = f"- {label}：{reason}（{_format_pct(snapshot.change_pct)}）。建议：先看风险，不急着加仓。"
            action_items.append((1, label, line))

    action_items.sort(key=lambda item: item[0])
    action_lines = [line for _, _, line in action_items[:max_action_items]]
    if not action_lines:
        action_lines = ["- 暂无必须立刻处理的信号；先按原计划观察。"]

    market_lines = [
        _market_mood_line(code, snapshots[code])
        for code in DEFAULT_RISK_PROXY_ORDER
        if code in snapshots
    ]

    holding_lines = []
    for code in sorted(holding_codes):
        snapshot = snapshots.get(code)
        if not snapshot:
            holding_lines.append(f"- **{code}**：暂时拿不到行情，先不动作。")
            continue
        reason = _plain_risk_reason(
            snapshot,
            code=code,
            holding_codes=holding_codes,
            holding_threshold=holding_threshold,
            index_threshold=index_threshold,
            vix_threshold=vix_threshold,
            bias_threshold=bias_threshold,
        )
        action = _plain_action_for_snapshot(
            snapshot,
            holding_threshold=holding_threshold,
            bias_threshold=bias_threshold,
        )
        if reason:
            holding_lines.append(
                f"- **{code}**：{reason}。建议：{action}。"
                f"涨跌 {_format_pct(snapshot.change_pct)}，价格 {_format_price(snapshot.price)}"
            )
        else:
            holding_lines.append(
                f"- **{code}**：{action}。涨跌 {_format_pct(snapshot.change_pct)}，价格 {_format_price(snapshot.price)}"
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
        action = _plain_action_for_snapshot(
            snapshot,
            holding_threshold=holding_threshold,
            bias_threshold=bias_threshold,
        )
        opportunity_lines.append(
            f"- **{snapshot.code}**：{action}。涨跌 {_format_pct(snapshot.change_pct)}，价格 {_format_price(snapshot.price)}"
        )
    if not opportunity_lines:
        opportunity_lines.append("- 暂无值得新增关注的机会，别为了交易而交易。")

    forced_note = " | 手动测试" if match.forced else ""
    now_text = match.now.strftime("%m-%d %H:%M ET")
    summary = _plain_summary(
        action_items=action_items,
        holding_codes=holding_codes,
        match=match,
    )
    report = [
        f"# 美股盘中行动卡片：{match.window.label}",
        "",
        f"{now_text}{forced_note} | {match.window.focus}",
        "提醒：这是辅助判断，不是自动买卖指令。",
        "",
        "## 一句话结论",
        summary,
        "",
        "## 需要你处理",
        *action_lines,
        "",
        "## 你的持仓",
        *(holding_lines or ["- 未配置真实持仓列表。"]),
        "",
        "## 市场环境",
        *(market_lines or ["- 暂无市场环境数据。"]),
        "",
        "## 可以关注",
        *opportunity_lines,
        "",
        "## 当前动作",
        f"- {match.window.focus}。没有触发就别动，触发风险就先保护本金。",
    ]
    return "\n".join(report).strip() + "\n"


def _dedupe_marker_name(match: IntradayWindowMatch) -> str:
    return f"us-intraday-sent-{match.now.strftime('%Y%m%d')}-{match.window.key}"


def _local_marker_dir() -> Path:
    raw_dir = os.getenv(
        "US_INTRADAY_LOCAL_MARKER_DIR",
        "~/Library/Application Support/us-intraday-radar/markers",
    )
    return Path(raw_dir).expanduser()


def _local_dedupe_marker_exists(marker_name: str, lookback_hours: int) -> bool:
    marker_path = _local_marker_dir() / marker_name
    if not marker_path.exists():
        return False
    cutoff = datetime.now(ZoneInfo("UTC")) - timedelta(hours=max(1, int(lookback_hours)))
    try:
        modified = datetime.fromtimestamp(marker_path.stat().st_mtime, tz=ZoneInfo("UTC"))
    except OSError as exc:
        logger.warning("[IntradayRadar] 本地去重 marker 读取失败，继续发送: %s", exc)
        return False
    return modified >= cutoff


def _default_dedupe_marker_exists(marker_name: str, lookback_hours: int) -> bool:
    if os.getenv("US_INTRADAY_LOCAL_MODE", "").lower() == "true":
        return _local_dedupe_marker_exists(marker_name, lookback_hours)
    return _github_artifact_marker_exists(
        marker_name=marker_name,
        lookback_hours=lookback_hours,
    )


def _parse_github_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _github_artifact_marker_exists(
    *,
    marker_name: str,
    lookback_hours: int,
    repo: Optional[str] = None,
    token: Optional[str] = None,
    current_run_id: Optional[str] = None,
) -> bool:
    repo = repo or os.getenv("GITHUB_REPOSITORY")
    token = token or os.getenv("GITHUB_TOKEN")
    current_run_id = current_run_id or os.getenv("GITHUB_RUN_ID")
    if not repo or not token:
        logger.info("[IntradayRadar] GitHub artifact 去重缺少 repo/token，跳过去重检查")
        return False

    cutoff = datetime.now(ZoneInfo("UTC")) - timedelta(hours=max(1, int(lookback_hours)))
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "daily-stock-analysis-intraday-radar",
    }

    for page in range(1, 4):
        params = urlencode({"per_page": 100, "page": page})
        url = f"https://api.github.com/repos/{repo}/actions/artifacts?{params}"
        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=12) as response:
                import json
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            logger.warning("[IntradayRadar] GitHub artifact 去重查询失败，继续发送: %s", exc)
            return False

        artifacts = payload.get("artifacts") or []
        if not artifacts:
            return False
        for artifact in artifacts:
            if artifact.get("name") != marker_name or artifact.get("expired"):
                continue
            created_at = _parse_github_datetime(str(artifact.get("created_at") or ""))
            if created_at and created_at < cutoff:
                continue
            workflow_run = artifact.get("workflow_run") or {}
            run_id = str(workflow_run.get("id") or "")
            if current_run_id and run_id == str(current_run_id):
                continue
            return True
    return False


def _write_dedupe_marker(match: IntradayWindowMatch) -> str:
    os.makedirs("reports", exist_ok=True)
    marker_name = _dedupe_marker_name(match)
    path = os.path.join("reports", marker_name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{marker_name}\n")
    return path


def _write_local_dedupe_marker(match: IntradayWindowMatch) -> str:
    marker_dir = _local_marker_dir()
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker_name = _dedupe_marker_name(match)
    marker_path = marker_dir / marker_name
    marker_path.write_text(f"{marker_name}\n", encoding="utf-8")
    return str(marker_path)


def _quote_audit_line(snapshot: QuoteSnapshot) -> str:
    quality = snapshot.quality
    quote_time = quality.quote_time.strftime("%Y-%m-%d %H:%M ET") if quality.quote_time else "N/A"
    warnings = "；".join(quality.warnings[:3]) if quality.warnings else "无"
    return (
        f"| {snapshot.code} | {_format_price(snapshot.price)} | {_format_pct(snapshot.change_pct)} | "
        f"{_quality_label(quality.level)} | {quality.session or 'unknown'} | {quote_time} | "
        f"{quality.price_field or 'N/A'} | {quality.change_pct_field or 'N/A'} | {warnings} |"
    )


def build_us_intraday_technical_report(
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
            risk_lines.append(
                f"- **{code}**：{reason}，现价 {_format_price(snapshot.price)}，"
                f"涨跌 {_format_pct(snapshot.change_pct)}，{_quote_quality_text(snapshot)}"
            )

    if not risk_lines:
        risk_lines.append("- 暂无必须立刻处理的异常；按计划观察，不追高。")

    market_lines = []
    for code in DEFAULT_RISK_PROXY_ORDER:
        snapshot = snapshots.get(code)
        if snapshot:
            market_lines.append(
                f"- **{code}**：{_format_pct(snapshot.change_pct)} | "
                f"{_format_price(snapshot.price)} | 可信度 {_quality_label(snapshot.quality.level)}"
            )

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
            f"现价 {_format_price(snapshot.price)} | MA20 {_format_price(snapshot.ma20)} | "
            f"可信度 {_quality_label(snapshot.quality.level)}"
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

    audit_lines = [
        "| 标的 | 价格 | 涨跌 | 可信度 | 阶段 | 报价时间 | 价格字段 | 涨跌字段 | 警告 |",
        "| --- | ---: | ---: | --- | --- | --- | --- | --- | --- |",
    ]
    audit_lines.extend(_quote_audit_line(snapshot) for snapshot in snapshots.values())

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
        "## 行情审计",
        *audit_lines,
        "",
        "## 当前指导",
        f"- **{match.window.label}**：{match.window.focus}。若信号没有触发，按原仓位计划执行；若触发风险，先处理风险再看机会。",
    ]
    return "\n".join(report).strip() + "\n"


def build_us_intraday_radar_report(
    *,
    config: Any,
    match: IntradayWindowMatch,
    snapshots: Dict[str, QuoteSnapshot],
) -> str:
    if bool(getattr(config, "us_commander_enabled", False)):
        previous_state = (
            _load_commander_memory(match)
            if bool(getattr(config, "us_commander_memory_enabled", True))
            else {}
        )
        commander_report, _ = build_us_commander_report(
            config=config,
            match=match,
            snapshots=snapshots,
            previous_state=previous_state,
        )
        if bool(getattr(config, "us_intraday_show_technical_details", False)):
            technical_report = build_us_intraday_technical_report(
                config=config,
                match=match,
                snapshots=snapshots,
            )
            return f"{commander_report}\n---\n\n## 技术细节\n{technical_report}"
        return commander_report

    readable_enabled = bool(getattr(config, "us_intraday_readable_report", True))
    jargon_level = str(getattr(config, "us_intraday_jargon_level", "explained")).lower()
    if readable_enabled and jargon_level in {"plain", "explained"}:
        readable_report = build_us_intraday_readable_report(
            config=config,
            match=match,
            snapshots=snapshots,
        )
        if bool(getattr(config, "us_intraday_show_technical_details", False)):
            technical_report = build_us_intraday_technical_report(
                config=config,
                match=match,
                snapshots=snapshots,
            )
            return f"{readable_report}\n---\n\n## 技术细节\n{technical_report}"
        return readable_report
    return build_us_intraday_technical_report(
        config=config,
        match=match,
        snapshots=snapshots,
    )


def _write_report(report: str, *, match: IntradayWindowMatch, suffix: str = "") -> str:
    os.makedirs("reports", exist_ok=True)
    stamp = match.now.strftime("%Y%m%d")
    path = os.path.join("reports", f"us_intraday_radar_{stamp}_{match.window.key}{suffix}.md")
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
    dedupe_checker: Optional[Callable[[str, int], bool]] = None,
) -> Tuple[bool, str]:
    """Run one US intraday radar cycle.

    Returns (sent_or_skipped_successfully, message).
    """
    match = resolve_us_intraday_window(
        enabled=bool(getattr(config, "us_intraday_radar_enabled", False)),
        configured_windows=getattr(config, "us_intraday_windows", []),
        tolerance_minutes=int(getattr(config, "us_intraday_window_tolerance_minutes", 12)),
        catchup_minutes=int(getattr(config, "us_intraday_catchup_minutes", 45)),
        close_catchup_minutes=int(getattr(config, "us_intraday_close_catchup_minutes", 120)),
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

    dedupe_enabled = bool(getattr(config, "us_intraday_dedupe_enabled", True))
    if send_notification and dedupe_enabled and not force_run:
        marker_name = _dedupe_marker_name(match)
        lookback_hours = int(getattr(config, "us_intraday_dedupe_lookback_hours", 24))
        marker_exists = (
            dedupe_checker(marker_name, lookback_hours)
            if dedupe_checker is not None
            else _default_dedupe_marker_exists(marker_name, lookback_hours)
        )
        if marker_exists:
            message = f"[IntradayRadar] 跳过：{marker_name} 已发送过"
            logger.info(message)
            return True, message

    if fetcher_manager is None:
        from data_provider import DataFetcherManager
        fetcher_manager = DataFetcherManager()

    holding_codes = _dedupe_codes(getattr(config, "portfolio_stock_list", []) or [])
    stock_codes = _dedupe_codes(getattr(config, "stock_list", []) or [])
    risk_codes = [code for code in DEFAULT_RISK_PROXY_ORDER if code in stock_codes or code in US_RISK_PROXIES]
    pre_open_fast_mode = _pre_open_fast_mode_enabled(config, match)
    opportunity_pool = [] if pre_open_fast_mode else [
        code for code in stock_codes
        if code not in set(holding_codes) and code not in US_RISK_PROXIES
    ]
    codes = _dedupe_codes(holding_codes + risk_codes + opportunity_pool)

    snapshots = build_quote_snapshots(
        codes,
        fetcher_manager,
        now=match.now,
        freshness_minutes=int(getattr(config, "us_intraday_quote_freshness_minutes", 20)),
        require_fresh_quotes=bool(getattr(config, "us_intraday_require_fresh_quotes", True)),
        include_daily_history=not pre_open_fast_mode,
    )
    technical_report = build_us_intraday_technical_report(config=config, match=match, snapshots=snapshots)
    commander_decision: Optional[CommanderDecision] = None
    if bool(getattr(config, "us_commander_enabled", False)):
        previous_state = (
            _load_commander_memory(match)
            if bool(getattr(config, "us_commander_memory_enabled", True))
            else {}
        )
        telegram_report, commander_decision = build_us_commander_report(
            config=config,
            match=match,
            snapshots=snapshots,
            previous_state=previous_state,
        )
        commander_note = _build_commander_llm_note(
            config=config,
            match=match,
            decision=commander_decision,
        )
        if commander_note:
            telegram_report = _format_us_commander_report(
                config=config,
                match=match,
                decision=commander_decision,
                commander_note=commander_note,
            )
        if bool(getattr(config, "us_intraday_show_technical_details", False)):
            telegram_report = f"{telegram_report}\n---\n\n## 技术细节\n{technical_report}"
    else:
        telegram_report = build_us_intraday_radar_report(config=config, match=match, snapshots=snapshots)
    path = _write_report(technical_report, match=match)
    if telegram_report != technical_report:
        _write_report(telegram_report, match=match, suffix="_telegram")
    logger.info("[IntradayRadar] 盘中雷达已保存: %s", path)

    if not send_notification:
        return True, path

    if notifier is None:
        from src.notification import NotificationService
        notifier = NotificationService()
    sent = notifier.send(telegram_report)
    if sent and dedupe_enabled and not force_run:
        marker_path = _write_dedupe_marker(match)
        logger.info("[IntradayRadar] 去重 marker 已保存: %s", marker_path)
        if os.getenv("US_INTRADAY_LOCAL_MODE", "").lower() == "true":
            local_marker_path = _write_local_dedupe_marker(match)
            logger.info("[IntradayRadar] 本地去重 marker 已保存: %s", local_marker_path)
        if commander_decision and bool(getattr(config, "us_commander_memory_enabled", True)):
            commander_state_path = _write_commander_memory(match, commander_decision)
            logger.info("[IntradayRadar] 指挥官记忆已保存: %s", commander_state_path)
    return bool(sent), path if sent else "盘中雷达推送失败"
