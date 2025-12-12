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

last_account_refresh = None
last_cycle_job = None

async def schedule_loop_async():
    print("⏳ 启动最简调度循环（周期触发 → 下载K线 → 投喂AI + 自动交易）")

    while True:
        now = datetime.now(timezone.utc)
        # 每分钟刷新账户与持仓，保证及时反应
        global last_account_refresh
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        if last_account_refresh != minute_key:
            last_account_refresh = minute_key
            get_account_status()

        # 每 3 分钟执行一次完整流程（监控池构建、K线下载、指标、AI、交易）
        global last_cycle_job
        cycle_key = now.strftime("%Y-%m-%d %H:") + f"{(now.minute // 3) * 3:02d}"
        if last_cycle_job != cycle_key:
            last_cycle_job = cycle_key

            # 🔄 再次刷新持仓缓存（冗余但确保及时）
            get_account_status()

            # 🔥 合成监控池 = 主流币 + 持仓币 + OI异动币
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
                        "open_long", "open_short",
                        "close_long", "close_short",
                        "reverse",
                        "stop_loss", "take_profit",
                        "update_stop_loss", "update_take_profit",
                        "increase_position", "decrease_position"  # ← 新增的
                    }
                    exec_list = []     # 最终需要执行的信号

                    for sig in ai_res:
                        symbol = sig.get("symbol")
                        action = sig.get("action")

                        if not symbol or not action:
                            continue

                        # ---- 止盈止损 ----
                        sl = sig.get("stop_loss")
                        tp = sig.get("take_profit")

                        # AI 有可能返回：position_size、quantity、qty
                        position_size = sig.get("position_size") or sig.get("order_value") or sig.get("amount")

                        # ---- 仅执行允许的操作 ----
                        if action in valid_actions:
                            execute_trade(
                                symbol=symbol,
                                action=action,
                                stop_loss=sl,
                                take_profit=tp,
                                position_size=position_size
                            )
                            exec_list.append(sig)

                    # 如果真的有执行动作 → 推送 & 日志
                    if exec_list:
                        await send_tg_trade_signal(exec_list)
                        print(f"🟢 执行交易: {exec_list}")
                    # 如果没有要执行的动作 → 保持安静，不打印任何多余内容

                else:
                    print("⚠ AI 未返回有效信号，不推送，不下单")

            finally:
                # 🧹 清理 Redis 旧 K线
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
