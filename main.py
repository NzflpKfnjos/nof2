import sys

# Windows CMD 默认编码可能是 GBK，打印 emoji 会触发 UnicodeEncodeError；这里强制 UTF-8 并降级替换不可编码字符
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import time
import threading
from notifier import message_worker
from database import clear_redis, IS_FAKE_REDIS
from kline_fetcher import fetch_all
from indicators import calculate_signal
from config import monitor_symbols, timeframes
import asyncio
from scheduler import schedule_loop_async
from deepseek_batch_pusher import _is_ready_for_push, push_batch_to_deepseek
import subprocess
import signal
import os
import oi

async def run_async():
    await schedule_loop_async()

def main():
    clear_redis()
    threading.Thread(target=message_worker, daemon=True).start()

    # fetch_all()

    oi_proc = None
    if IS_FAKE_REDIS:
        threading.Thread(target=lambda: asyncio.run(oi.scheduler()), daemon=True).start()
        print("📡 OI 异动监控模块已启动（内存缓存模式，线程共享）")
    else:
        oi_proc = subprocess.Popen([sys.executable, "oi.py"])   # ⬅ 保存句柄
        print("📡 OI 异动监控模块已启动")
    
    print("⏳ 启动异步调度循环")
    try:
        asyncio.run(run_async())

    except KeyboardInterrupt:
        print("\n⚠ 捕获 Ctrl+C → 准备退出...")

    finally:
        # 🔥 优雅关闭子进程 OI 监控模块
        if oi_proc:
            try:
                oi_proc.terminate()
                print("🛑 已终止 OI 监控模块")
            except:
                pass

        print("👋 程序已退出")
        
if __name__ == "__main__":
    # os.environ['http_proxy'] = 'http://127.0.0.1:7890'
    # os.environ['HTTP_PROXY'] = 'http://127.0.0.1:7890'

    # os.environ['https_proxy'] = 'http://127.0.0.1:7890'
    # os.environ['HTTPS_PROXY'] = 'http://127.0.0.1:7890'
    main()
