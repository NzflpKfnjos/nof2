import argparse
import math
import os
import sys
import time
from datetime import datetime
import unicodedata
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, getcontext
from typing import Dict, List, Optional, Set, Tuple

from binance.client import Client
from binance.exceptions import BinanceAPIException


SL_ORDER_TYPES = {"STOP", "STOP_MARKET"}

getcontext().prec = 28

_symbol_price_filter_cache: Dict[str, Decimal] = {}
_last_sl_update_at: Dict[Tuple[str, str], float] = {}
_sl_snapshot_cache: Dict[Tuple[str, str], Tuple[float, int, float]] = {}
_last_rate_limit_notice_ts: float = 0.0
_rate_limit_backoff_sec: float = 0.0
_sl_action_history: List[str] = []


def _load_keys():
    api_key = os.environ.get("BINANCE_API_KEY")
    api_secret = os.environ.get("BINANCE_API_SECRET")
    if api_key and api_secret:
        return api_key, api_secret

    try:
        from config import BINANCE_API_KEY, BINANCE_API_SECRET  # type: ignore

        if BINANCE_API_KEY and BINANCE_API_SECRET:
            return BINANCE_API_KEY, BINANCE_API_SECRET
    except Exception:
        pass

    raise RuntimeError(
        "缺少币安 API Key/Secret：请设置环境变量 BINANCE_API_KEY/BINANCE_API_SECRET，或在 config.py 填写。"
    )


def _build_client():
    api_key, api_secret = _load_keys()
    client = Client(
        api_key=api_key,
        api_secret=api_secret,
        requests_params={"timeout": 20},
    )
    client.recvWindow = 10000
    return client


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _display_width(text: str) -> int:
    width = 0
    for ch in str(text):
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _truncate_to_width(text: str, width: int) -> str:
    if width <= 0:
        return ""
    text = str(text)
    if _display_width(text) <= width:
        return text
    out: List[str] = []
    used = 0
    for ch in text:
        if unicodedata.combining(ch):
            out.append(ch)
            continue
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out)


def _pad(text: str, width: int, align: str) -> str:
    text = _truncate_to_width(str(text), width)
    pad_len = max(0, width - _display_width(text))
    if align == "right":
        return (" " * pad_len) + text
    if align == "center":
        left = pad_len // 2
        right = pad_len - left
        return (" " * left) + text + (" " * right)
    return text + (" " * pad_len)


def _get_tick_size(client: Client, symbol: str) -> float:
    tick = _symbol_price_filter_cache.get(symbol)
    if tick is not None:
        return float(tick)

    info = client.futures_exchange_info()
    for s in info.get("symbols", []):
        if s.get("symbol") != symbol:
            continue
        for f in s.get("filters", []):
            if f.get("filterType") == "PRICE_FILTER":
                tick_str = str(f.get("tickSize") or "0.01")
                tick_dec = Decimal(tick_str)
                _symbol_price_filter_cache[symbol] = tick_dec
                return float(tick_dec)

    _symbol_price_filter_cache[symbol] = Decimal("0.01")
    return 0.01


def _normalize_price_floor(client: Client, symbol: str, price: float) -> float:
    tick = _symbol_price_filter_cache.get(symbol)
    if tick is None:
        _get_tick_size(client, symbol)
        tick = _symbol_price_filter_cache.get(symbol) or Decimal("0.01")
    if tick <= 0:
        return float(price)
    p = Decimal(str(price))
    n = (p / tick).to_integral_value(rounding=ROUND_FLOOR)
    q = (n * tick).quantize(tick, rounding=ROUND_DOWN)
    return float(q)


def _normalize_price_ceil(client: Client, symbol: str, price: float) -> float:
    tick = _symbol_price_filter_cache.get(symbol)
    if tick is None:
        _get_tick_size(client, symbol)
        tick = _symbol_price_filter_cache.get(symbol) or Decimal("0.01")
    if tick <= 0:
        return float(price)
    p = Decimal(str(price))
    n = (p / tick).to_integral_value(rounding=ROUND_CEILING)
    q = (n * tick).quantize(tick, rounding=ROUND_DOWN)
    return float(q)


def _format_stop_price(client: Client, symbol: str, position_side: str, price: float) -> str:
    tick = _symbol_price_filter_cache.get(symbol)
    if tick is None:
        _get_tick_size(client, symbol)
        tick = _symbol_price_filter_cache.get(symbol) or Decimal("0.01")
    rounding = ROUND_FLOOR if position_side == "LONG" else ROUND_CEILING
    p = Decimal(str(price))
    n = (p / tick).to_integral_value(rounding=rounding)
    q = (n * tick).quantize(tick, rounding=ROUND_DOWN)
    return format(q, "f")


def _get_position_side(p: dict, qty: float) -> str:
    ps = (p.get("positionSide") or "").upper()
    if ps in {"LONG", "SHORT", "BOTH"}:
        return ps
    return "LONG" if qty > 0 else "SHORT"


def _fetch_positions_snapshot(client: Client):
    account = client.futures_account(requests_params={"timeout": 20})

    wallet_balance = float(account.get("totalWalletBalance") or 0)
    total_unrealized = float(account.get("totalUnrealizedProfit") or 0)
    available_balance = float(account.get("availableBalance") or 0)

    raw_positions: List[dict] = []
    symbols_need_mark: List[str] = []
    for p in account.get("positions", []):
        qty = float(p.get("positionAmt") or 0)
        if qty == 0:
            continue
        symbol = p.get("symbol") or ""
        if not symbol:
            continue
        raw_positions.append(p)
        if _safe_float(p.get("markPrice"), 0.0) <= 0:
            symbols_need_mark.append(symbol)

    mark_dict = _fetch_mark_prices_for_symbols(client, symbols_need_mark) if symbols_need_mark else {}

    positions = []
    for p in raw_positions:
        qty = float(p.get("positionAmt") or 0)
        symbol = p.get("symbol") or ""
        entry = float(p.get("entryPrice") or 0)
        mark = _safe_float(p.get("markPrice"), 0.0)
        if mark <= 0:
            mark = float(mark_dict.get(symbol, entry))
        pnl = float(p.get("unrealizedProfit") or 0)
        leverage = int(float(p.get("leverage") or 0))
        position_side = _get_position_side(p, qty)
        side = "做多" if position_side == "LONG" else "做空" if position_side == "SHORT" else position_side

        qty_abs = abs(qty)
        notional = qty_abs * mark
        denom = qty_abs * entry
        pnl_pct = (pnl / denom * 100.0) if denom > 0 else 0.0

        positions.append(
            {
                "symbol": symbol,
                "side": side,
                "position_side": position_side,
                "qty": qty_abs,
                "entry": entry,
                "mark": mark,
                "notional": notional,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "leverage": leverage,
                "sl_price": None,
                "sl_count": 0,
            }
        )

    positions.sort(key=lambda x: abs(x["notional"]), reverse=True)

    return {
        "wallet_balance": wallet_balance,
        "available_balance": available_balance,
        "total_unrealized": total_unrealized,
        "positions": positions,
    }


def _clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def _fetch_open_orders(client: Client) -> List[dict]:
    try:
        return client.futures_get_open_orders(requests_params={"timeout": 20}) or []
    except Exception:
        return []


def _fetch_open_algo_orders(client: Client) -> List[dict]:
    try:
        return client.futures_get_open_orders(conditional=True, requests_params={"timeout": 20}) or []
    except Exception:
        return []


def _fetch_open_orders_by_symbol(client: Client, symbol: str) -> List[dict]:
    try:
        return client.futures_get_open_orders(symbol=symbol, requests_params={"timeout": 20}) or []
    except Exception:
        return []


def _fetch_open_algo_orders_by_symbol(client: Client, symbol: str) -> List[dict]:
    try:
        return client.futures_get_open_orders(symbol=symbol, conditional=True, requests_params={"timeout": 20}) or []
    except Exception:
        return []


def _fetch_mark_prices_for_symbols(client: Client, symbols: List[str]) -> Dict[str, float]:
    mark: Dict[str, float] = {}
    for s in symbols:
        if not s:
            continue
        try:
            r = client.futures_mark_price(symbol=s, requests_params={"timeout": 20})
            mp = _safe_float(r.get("markPrice"), 0.0)
            if mp > 0:
                mark[s] = mp
        except Exception:
            continue
    return mark


def _is_reduce_only_or_close_position(order: dict) -> bool:
    v1 = order.get("reduceOnly")
    v2 = order.get("closePosition")
    return str(v1).lower() == "true" or str(v2).lower() == "true"


def _collect_stop_orders(
    open_orders: List[dict], algo_orders: List[dict], symbol: str, position_side: str
) -> List[dict]:
    orders: List[dict] = []

    for o in open_orders:
        if (o.get("symbol") or "") != symbol:
            continue
        if (o.get("positionSide") or "").upper() != position_side:
            continue
        if (o.get("type") or "").upper() not in SL_ORDER_TYPES:
            continue
        if not _is_reduce_only_or_close_position(o):
            continue
        stop_price = _safe_float(o.get("stopPrice"), 0.0)
        if stop_price <= 0:
            continue
        orders.append(
            {
                "source": "base_order",
                "symbol": symbol,
                "position_side": position_side,
                "type": (o.get("type") or "").upper(),
                "stop_price": stop_price,
                "orderId": o.get("orderId"),
            }
        )

    for o in algo_orders:
        if (o.get("symbol") or "") != symbol:
            continue
        if (o.get("positionSide") or "").upper() != position_side:
            continue
        if (o.get("orderType") or "").upper() not in SL_ORDER_TYPES:
            continue
        stop_price = _safe_float(o.get("triggerPrice"), 0.0)
        if stop_price <= 0:
            continue
        orders.append(
            {
                "source": "algo_order",
                "symbol": symbol,
                "position_side": position_side,
                "type": (o.get("orderType") or "").upper(),
                "stop_price": stop_price,
                "algoId": o.get("algoId"),
                "clientAlgoId": o.get("clientAlgoId"),
            }
        )

    return orders


def _pick_current_sl_price(position_side: str, stop_orders: List[dict]) -> Optional[float]:
    prices = [o["stop_price"] for o in stop_orders if _safe_float(o.get("stop_price"), 0.0) > 0]
    if not prices:
        return None
    if position_side == "LONG":
        return max(prices)
    if position_side == "SHORT":
        return min(prices)
    return None


def _enrich_snapshot_with_sl(client: Client, snapshot: dict, *, refresh_sec: float) -> None:
    now_ts = time.time()
    for p in snapshot.get("positions", []):
        symbol = p["symbol"]
        position_side = p["position_side"]

        cache_key = (symbol, position_side)
        cached = _sl_snapshot_cache.get(cache_key)
        if cached:
            sl_price, sl_count, ts = cached
            if now_ts - ts < float(refresh_sec):
                p["sl_price"] = sl_price if sl_price > 0 else None
                p["sl_count"] = int(sl_count)
                continue

        open_orders = _fetch_open_orders_by_symbol(client, symbol)
        algo_orders = _fetch_open_algo_orders_by_symbol(client, symbol)
        stop_orders = _collect_stop_orders(open_orders, algo_orders, symbol, position_side)
        sl_price = _pick_current_sl_price(position_side, stop_orders)
        sl_count = len(stop_orders)

        p["sl_count"] = sl_count
        p["sl_price"] = sl_price
        _sl_snapshot_cache[cache_key] = (float(sl_price or 0.0), int(sl_count), now_ts)


def _cancel_existing_sl_orders(client: Client, symbol: str, position_side: str, stop_orders: List[dict]) -> List[str]:
    canceled: List[str] = []
    for o in stop_orders:
        if o.get("source") == "base_order":
            order_id = o.get("orderId")
            if order_id is None:
                continue
            try:
                client.futures_cancel_order(symbol=symbol, orderId=order_id)
                canceled.append(f"orderId={order_id}")
            except Exception:
                pass
        elif o.get("source") == "algo_order":
            algo_id = o.get("algoId")
            client_algo_id = o.get("clientAlgoId")
            if not algo_id and not client_algo_id:
                continue
            try:
                client.futures_cancel_algo_order(symbol=symbol, algoId=algo_id, clientAlgoId=client_algo_id)
                if algo_id:
                    canceled.append(f"algoId={algo_id}")
                elif client_algo_id:
                    canceled.append(f"clientAlgoId={client_algo_id}")
            except Exception:
                pass
    return canceled


def _place_stop_market_close_position(client: Client, symbol: str, position_side: str, stop_price: float) -> dict:
    if position_side == "LONG":
        side = "SELL"
    elif position_side == "SHORT":
        side = "BUY"
    else:
        return {}

    stop_price_str = _format_stop_price(client, symbol, position_side, stop_price)
    return client.futures_create_order(
        symbol=symbol,
        side=side,
        positionSide=position_side,
        type="STOP_MARKET",
        stopPrice=stop_price_str,
        closePosition=True,
        workingType="MARK_PRICE",
    )


def _append_sl_history(lines: List[str], *, max_keep: int, log_file: str) -> None:
    if not lines:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for line in lines:
        _sl_action_history.append(f"[{now}] {line}")
    if len(_sl_action_history) > int(max_keep):
        del _sl_action_history[: len(_sl_action_history) - int(max_keep)]
    if log_file:
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                for line in lines:
                    f.write(f"[{now}] {line}\n")
        except Exception:
            pass


def _auto_step_sl_to_breakeven(
    client: Client,
    snapshot: dict,
    *,
    step_pct: float,
    min_interval_sec: float,
    buffer_ticks: int,
    dry_run: bool,
    symbols_filter: Optional[Set[str]],
    verbose: bool,
    mode: str,
    trail_pct: float,
    activate_profit_pct: float,
) -> List[str]:
    messages: List[str] = []
    now_ts = time.time()

    for p in snapshot.get("positions", []):
        symbol = p["symbol"]
        if symbols_filter and symbol not in symbols_filter:
            continue

        position_side = p["position_side"]
        entry = float(p["entry"])
        mark = float(p["mark"])

        if entry <= 0 or mark <= 0:
            continue

        liangping = entry  # 两平/保本价：这里按开仓价处理
        tick = _get_tick_size(client, symbol)
        buffer_price = max(tick * float(max(0, buffer_ticks)), 0.0)

        profit_pct = 0.0
        if position_side == "LONG":
            profit_pct = (mark - entry) / entry * 100.0 if entry > 0 else 0.0
        elif position_side == "SHORT":
            profit_pct = (entry - mark) / entry * 100.0 if entry > 0 else 0.0

        if position_side == "LONG":
            if liangping > (mark - buffer_price):
                if verbose:
                    messages.append(
                        f"跳过：{symbol} 做多 未到两平（标记价 {mark:.6f} < 两平价 {liangping:.6f}），为避免止损立即触发不推进"
                    )
                continue
        elif position_side == "SHORT":
            if liangping < (mark + buffer_price):
                if verbose:
                    messages.append(
                        f"跳过：{symbol} 做空 未到两平（标记价 {mark:.6f} > 两平价 {liangping:.6f}），为避免止损立即触发不推进"
                    )
                continue
        else:
            continue

        key = (symbol, position_side)
        last_ts = _last_sl_update_at.get(key, 0.0)
        if now_ts - last_ts < float(min_interval_sec):
            continue

        open_orders = _fetch_open_orders_by_symbol(client, symbol)
        algo_orders = _fetch_open_algo_orders_by_symbol(client, symbol)
        stop_orders = _collect_stop_orders(open_orders, algo_orders, symbol, position_side)
        current_sl = _pick_current_sl_price(position_side, stop_orders)

        new_sl: Optional[float] = None
        mode = (mode or "").strip().lower()

        if mode in {"lock_profit", "lock-profit"}:
            if profit_pct < float(activate_profit_pct):
                if verbose:
                    messages.append(
                        f"跳过：{symbol} {('做多' if position_side=='LONG' else '做空')} 浮盈 {profit_pct:.2f}% < 触发阈值 {float(activate_profit_pct):.2f}%"
                    )
                continue
            if position_side == "LONG":
                trail_candidate = mark * (1.0 - float(trail_pct) / 100.0)
                target = max(liangping, trail_candidate)
                target = min(target, mark - buffer_price)
                if current_sl is not None and current_sl > 0:
                    target = max(target, current_sl)
                new_sl = _normalize_price_floor(client, symbol, float(target))
            else:  # SHORT
                trail_candidate = mark * (1.0 + float(trail_pct) / 100.0)
                target = min(liangping, trail_candidate)
                target = max(target, mark + buffer_price)
                if current_sl is not None and current_sl > 0:
                    target = min(target, current_sl)
                new_sl = _normalize_price_ceil(client, symbol, float(target))

        else:
            step_amount = abs(liangping) * (float(step_pct) / 100.0)
            step_amount = max(step_amount, tick)
            step_amount = max(step_amount, 0.0)

            if current_sl is None or current_sl <= 0:
                new_sl = liangping
            else:
                if position_side == "LONG":
                    if current_sl >= liangping:
                        continue
                    new_sl = min(current_sl + step_amount, liangping)
                elif position_side == "SHORT":
                    if current_sl <= liangping:
                        continue
                    new_sl = max(current_sl - step_amount, liangping)

            if new_sl is None:
                continue

            if position_side == "LONG":
                new_sl = min(new_sl, mark - buffer_price)
                new_sl = _normalize_price_floor(client, symbol, float(new_sl))
            else:  # SHORT
                new_sl = max(new_sl, mark + buffer_price)
                new_sl = _normalize_price_ceil(client, symbol, float(new_sl))

        if new_sl is None:
            continue

        if current_sl is not None and current_sl > 0 and abs(float(new_sl) - float(current_sl)) < tick:
            continue

        if dry_run:
            messages.append(
                f"模拟：{symbol} {('做多' if position_side=='LONG' else '做空')} 止损 {current_sl or 0:.6f} -> {new_sl:.6f}（两平 {liangping:.6f}）"
            )
            _last_sl_update_at[key] = now_ts
            continue

        try:
            canceled = _cancel_existing_sl_orders(client, symbol, position_side, stop_orders)
            order = _place_stop_market_close_position(client, symbol, position_side, new_sl)
            _last_sl_update_at[key] = now_ts
            oid = order.get("orderId") or order.get("clientOrderId") or ""
            canceled_txt = f"；已撤销 {len(canceled)} 个旧止损" if canceled else "；无旧止损可撤销"
            oid_txt = f"；新止损单 {oid}" if oid else ""
            messages.append(
                f"已更新：{symbol} {('做多' if position_side=='LONG' else '做空')} 止损 {current_sl or 0:.6f} -> {new_sl:.6f}（两平 {liangping:.6f}）{canceled_txt}{oid_txt}"
            )
        except Exception as e:
            _last_sl_update_at[key] = now_ts
            messages.append(f"失败：{symbol} {position_side} 更新止损异常：{e}")

    return messages


def _print_snapshot(snapshot: dict, symbols_filter: Optional[Set[str]], auto_sl_enabled: bool):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    wallet = snapshot["wallet_balance"]
    available = snapshot["available_balance"]
    unreal = snapshot["total_unrealized"]

    print(
        f"[{now}] 钱包余额={wallet:.4f}  可用余额={available:.4f}  未实现盈亏={unreal:.4f}  自动止损={'开启' if auto_sl_enabled else '关闭'}"
    )
    cols = [
        ("合约", 12, "left"),
        ("方向", 6, "left"),
        ("数量", 14, "right"),
        ("开仓价", 14, "right"),
        ("标记价", 14, "right"),
        ("未实现盈亏", 14, "right"),
        ("收益率", 8, "right"),
        ("杠杆", 4, "right"),
        ("止损价", 14, "right"),
        ("两平价", 14, "right"),
    ]
    total_width = sum(w for _, w, _ in cols) + (len(cols) - 1)
    line = "-" * total_width
    print(line)
    print(" ".join(_pad(label, w, align) for label, w, align in cols))
    print(line)

    rows = 0
    for p in snapshot["positions"]:
        if symbols_filter and p["symbol"] not in symbols_filter:
            continue
        rows += 1
        sl = p.get("sl_price")
        sl_str = f"{float(sl):.6f}" if sl else "-"
        row = [
            (p["symbol"], 12, "left"),
            (p["side"], 6, "left"),
            (f"{p['qty']:.6f}", 14, "right"),
            (f"{p['entry']:.6f}", 14, "right"),
            (f"{p['mark']:.6f}", 14, "right"),
            (f"{p['pnl']:.4f}", 14, "right"),
            (f"{p['pnl_pct']:.2f}%", 8, "right"),
            (f"{p['leverage']:d}", 4, "right"),
            (sl_str, 14, "right"),
            (f"{p['entry']:.6f}", 14, "right"),
        ]
        print(" ".join(_pad(text, w, align) for text, w, align in row))

    if rows == 0:
        print("(当前无持仓)")


def main(argv: List[str]) -> int:
    global _last_rate_limit_notice_ts
    global _rate_limit_backoff_sec

    parser = argparse.ArgumentParser(description="实时查看币安合约持仓：标记价/收益/多空，并可自动把止损逐步推到两平价")
    parser.add_argument("--interval", type=float, default=1.0, help="刷新间隔秒数（默认 1）")
    parser.add_argument("--once", action="store_true", help="只获取一次后退出")
    parser.add_argument("--no-clear", action="store_true", help="不清屏（默认会清屏刷新）")
    parser.add_argument(
        "--symbols",
        type=str,
        default="",
        help="只显示指定交易对，逗号分隔，例如 BTCUSDT,ETHUSDT",
    )
    parser.add_argument(
        "--auto-sl",
        action="store_true",
        help="自动把止损价一步一步推到两平/保本价（会真实撤单+下 STOP_MARKET，请确认账号为实盘）",
    )
    parser.add_argument(
        "--sl-mode",
        type=str,
        default="lock_profit",
        help="自动止损模式：lock_profit(锁盈，盈利时止损>=开仓价并随价格上移) 或 breakeven(只推到两平；默认 lock_profit)",
    )
    parser.add_argument(
        "--sl-trail-pct",
        type=float,
        default=0.5,
        help="锁盈模式下：止损距离标记价的回撤百分比（默认 0.5%%；越小越贴近，越容易被扫）",
    )
    parser.add_argument(
        "--sl-activate-profit-pct",
        type=float,
        default=0.5,
        help="锁盈模式触发阈值：浮盈达到该百分比才开始推止损（默认 0.5%%）",
    )
    parser.add_argument(
        "--sl-step-pct",
        type=float,
        default=0.05,
        help="每次止损推进的步长（百分比，默认 0.05 表示 0.05%%，会自动不小于 tickSize）",
    )
    parser.add_argument(
        "--sl-min-interval",
        type=float,
        default=5.0,
        help="同一合约同一方向最小更新间隔秒数（默认 5）",
    )
    parser.add_argument(
        "--sl-buffer-ticks",
        type=int,
        default=2,
        help="止损与当前标记价保持的最小间隔（tick 数，默认 2）",
    )
    parser.add_argument(
        "--sl-refresh",
        type=float,
        default=5.0,
        help="止损价/止损单刷新频率秒数（默认 5，过低会更容易触发限流）",
    )
    parser.add_argument(
        "--sl-verbose",
        action="store_true",
        help="输出自动止损的跳过原因/更多日志（信息会比较多）",
    )
    parser.add_argument(
        "--sl-log-lines",
        type=int,
        default=30,
        help="显示最近多少条止损操作记录（默认 30）",
    )
    parser.add_argument(
        "--sl-log-file",
        type=str,
        default="",
        help="将止损操作记录追加写入文件（例如 sl.log；默认不写）",
    )
    parser.add_argument(
        "--sl-log-keep",
        type=int,
        default=200,
        help="内存中保留的止损操作记录条数（默认 200）",
    )
    parser.add_argument(
        "--sl-dry-run",
        action="store_true",
        help="只打印将要更新的止损，不真实下单/撤单",
    )
    args = parser.parse_args(argv)

    symbols_filter = None
    if args.symbols.strip():
        symbols_filter = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}

    client = _build_client()

    try:
        while True:
            try:
                snapshot = _fetch_positions_snapshot(client)
                _rate_limit_backoff_sec = 0.0

                if snapshot.get("positions"):
                    _enrich_snapshot_with_sl(client, snapshot, refresh_sec=float(args.sl_refresh))

                auto_messages: List[str] = []
                if args.auto_sl:
                    auto_messages = _auto_step_sl_to_breakeven(
                        client,
                        snapshot,
                        step_pct=float(args.sl_step_pct),
                        min_interval_sec=float(args.sl_min_interval),
                        buffer_ticks=int(args.sl_buffer_ticks),
                        dry_run=bool(args.sl_dry_run),
                        symbols_filter=symbols_filter,
                        verbose=bool(args.sl_verbose),
                        mode=str(args.sl_mode or ""),
                        trail_pct=float(args.sl_trail_pct),
                        activate_profit_pct=float(args.sl_activate_profit_pct),
                    )
                    _append_sl_history(
                        auto_messages,
                        max_keep=int(args.sl_log_keep),
                        log_file=str(args.sl_log_file or ""),
                    )

                if not args.no_clear:
                    _clear_screen()
                _print_snapshot(snapshot, symbols_filter, bool(args.auto_sl))
                if args.auto_sl:
                    n = max(0, int(args.sl_log_lines))
                    if n > 0 and _sl_action_history:
                        print()
                        print("止损操作记录（最近 {} 条）".format(min(n, len(_sl_action_history))))
                        print("-" * 40)
                        for line in _sl_action_history[-n:]:
                            print(line)
            except BinanceAPIException as e:
                code = getattr(e, "code", None)
                if code == -1003:
                    now_ts = time.time()
                    if now_ts - _last_rate_limit_notice_ts > 5.0:
                        _last_rate_limit_notice_ts = now_ts
                        print(
                            "⚠ 触发 Binance 频率限制(-1003)：当前请求过于频繁/同IP程序过多，已自动退避等待…",
                            file=sys.stderr,
                        )
                    _rate_limit_backoff_sec = 3.0 if _rate_limit_backoff_sec <= 0 else min(30.0, _rate_limit_backoff_sec * 2.0)
                    time.sleep(_rate_limit_backoff_sec)
                else:
                    print(f"❌ Binance 接口异常：{e}", file=sys.stderr)
            except Exception as e:
                print(f"❌ 运行异常：{e}", file=sys.stderr)

            if args.once:
                return 0
            time.sleep(max(0.2, float(args.interval)))
    except KeyboardInterrupt:
        print("\n已退出。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
