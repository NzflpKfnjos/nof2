import asyncio
from datetime import datetime, timezone
from config import monitor_symbols, mainstream_symbols
from indicators import calculate_signal_single
from deepseek_batch_pusher import push_batch_to_deepseek
from kline_fetcher import fetch_all
from ai_trade_notifier import send_tg_trade_signal
from position_cache import position_records
from account_positions import get_account_status
from database import redis_client
from trader import execute_trade

tf_order = ["1d", "4h", "1h", "15m", "5m"]
last_trigger = {tf: None for tf in tf_order}
first_run = True  # ← 新增

async def schedule_loop_async():
    print("⏳ 启动最简调度循环（周期触发 → 下载K线 → 投喂AI + 自动交易）")

    global first_run

    while True:
        now = datetime.now(timezone.utc)
        m = now.minute
        h = now.hour
        current_key = None

        # ⭐ 初次执行：直接指定一个 fake key
        if first_run:
            current_key = "first_run"
            first_run = False
        else:
            if h == 0 and m == 0:
                current_key = "1d"
            elif h % 4 == 0 and m == 0:
                current_key = "4h"
            elif m == 0:
                current_key = "1h"
            elif m % 15 == 0:
                current_key = "15m"
            elif m % 5 == 0:
                current_key = "5m"

        if current_key:
            mark = now.strftime("%Y-%m-%d %H:%M")

            # first_run 不需要检查 last_trigger
            if current_key != "first_run" and last_trigger[current_key] == mark:
                await asyncio.sleep(1)
                continue

            if current_key != "first_run":
                last_trigger[current_key] = mark

            print("🚀 首次启动 → 立即执行一次调度" if current_key == "first_run" else f"⏱ 触发 {current_key}")

            # --- 以下保持你原代码不变 ---
            get_account_status()

            raw_oi = redis_client.smembers("OI_SYMBOLS") or set()
            oi_symbols = list(raw_oi)
            pos_symbols = list(position_records)

            monitor_symbols[:] = list(
                dict.fromkeys(mainstream_symbols + pos_symbols + oi_symbols)
            )

            print(f"🔍 监控池: {monitor_symbols} (共 {len(monitor_symbols)} 个币)")

            fetch_all()

            print("📌 所有 K 线下载完成 → 计算指标")
            for sym in monitor_symbols:
                calculate_signal_single(sym)

            try:
                ai_res = await push_batch_to_deepseek()

                if ai_res and isinstance(ai_res, list):
                    valid_actions = {
                        "open_long", "open_short", "close_long", "close_short",
                        "reverse", "stop_loss", "take_profit",
                        "update_stop_loss", "update_take_profit",
                        "increase_position", "decrease_position"
                    }

                    exec_list = []
                    for sig in ai_res:
                        symbol = sig.get("symbol")
                        action = sig.get("action")
                        if not symbol or not action:
                            continue

                        sl = sig.get("stop_loss")
                        tp = sig.get("take_profit")

                        qty = (
                            sig.get("quantity") or 
                            sig.get("qty") or 
                            sig.get("position_size")
                        )

                        amt = sig.get("amount") or sig.get("order_value")

                        if action in valid_actions:
                            execute_trade(
                                symbol=symbol,
                                action=action,
                                stop_loss=sl,
                                take_profit=tp,
                                quantity=qty,
                                amount=amt
                            )
                            exec_list.append(sig)

                    if exec_list:
                        await send_tg_trade_signal(exec_list)
                        print(f"🟢 执行交易: {exec_list}")

                else:
                    print("⚠ AI 未返回有效信号，不推送，不下单")

            finally:
                valid = set(monitor_symbols)
                for key in redis_client.keys("historical_data:*"):
                    k = key if isinstance(key, str) else key.decode()
                    parts = k.split(":")
                    if len(parts) == 3:
                        _, symbol, _ = parts
                        if symbol not in valid:
                            redis_client.delete(key)
                            print(f"🗑 清理无效缓存币: {symbol}")

            print("🎯 本轮调度完成\n")

        await asyncio.sleep(1)
