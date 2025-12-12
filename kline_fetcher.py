import time
import json
import logging
import requests
from concurrent.futures import ThreadPoolExecutor
from config import monitor_symbols, timeframes
from database import redis_client

def fetch_historical(symbol, interval, limit=301):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    rkey = f"historical_data:{symbol}:{interval}"

    try:
        data = requests.get(url, timeout=5).json()
        now = int(time.time() * 1000)

        with redis_client.pipeline() as pipe:
            for k in data:
                ts, close_ts = k[0], k[6]
                if close_ts > now:
                    continue

                entry = json.dumps({
                    "Open": float(k[1]),
                    "High": float(k[2]),
                    "Low": float(k[3]),
                    "Close": float(k[4]),
                    "Volume": float(k[5]),
                    "TakerBuyVolume": float(k[9]),
                    "TakerSellVolume": float(k[5]) - float(k[9])
                })

                pipe.hset(rkey, ts, entry)
            pipe.execute()

    except Exception as e:
        logging.warning(f"{symbol} {interval} 历史获取失败: {e}")

def fetch_all():
    total_requests = len(monitor_symbols) * len(timeframes)
    print(f"⏳ 初始化下载中... 预计请求数: {total_requests}")

    start_time = time.time()

    time.sleep(2)
    with ThreadPoolExecutor(max_workers=8) as exe:
        for s in monitor_symbols:
            for tf in timeframes:
                exe.submit(fetch_historical, s, tf)

    elapsed = time.time() - start_time
    avg = elapsed / total_requests

    print(f"📌 历史数据初始化完成 ✓")
    print(f"⏱ 总耗时: {elapsed:.2f} 秒 (平均单请求: {avg:.3f} 秒)")

