import asyncio
import aiohttp
import time
from datetime import datetime, timedelta, timezone
from config import (
    OI_BASE_URL, OI_THRESHOLD, OI_CONCURRENCY, OI_INTERVAL_MINUTES,
    OI_EXPIRE_MINUTES, OI_USE_WHITELIST, OI_WHITELIST
)
from database import redis_client   # ⭕ 写入 Redis

OI_KEY = "OI_SYMBOLS"  # Redis 中存放当前 OI 异动币
oi_records = {}        # 本地仍保留，用于打印 & 过期判断

# 计算下一个周期对齐（例如每 5 分钟一次）
def align_to_period():
    now = datetime.now(timezone.utc)
    aligned = (now.minute // OI_INTERVAL_MINUTES) * OI_INTERVAL_MINUTES
    return now.replace(minute=aligned, second=0, microsecond=0)

async def wait_for_next_period():
    aligned = align_to_period()
    next_t = aligned + timedelta(minutes=OI_INTERVAL_MINUTES)
    wait_s = (next_t - datetime.now(timezone.utc)).total_seconds()
    if wait_s > 0:
        print(f"⏸ 等待 {wait_s:.1f} 秒进入下个 OI 扫描周期…")
        await asyncio.sleep(wait_s)

async def fetch_json(session, url, params=None):
    try:
        async with session.get(url, params=params, timeout=10) as r:
            return await r.json()
    except:
        return None

async def get_usdt_symbols(session):
    url = f"{OI_BASE_URL}/fapi/v1/exchangeInfo"
    data = await fetch_json(session, url)
    if not data:
        return []

    now_ts = datetime.now(timezone.utc).timestamp() * 1000  # 当前时间 毫秒
    min_online_ms = 30 * 24 * 60 * 60 * 1000  # 30天

    return [
        x["symbol"]
        for x in data["symbols"]
        if x.get("contractType") == "PERPETUAL"
        and x.get("quoteAsset") == "USDT"
        and x.get("status") == "TRADING"
        and x.get("onboardDate") is not None
        and (now_ts - x["onboardDate"]) >= min_online_ms  # ⭕ 上市 ≥ 30天
    ]

async def get_oi_change(session, symbol):
    url = f"{OI_BASE_URL}/futures/data/openInterestHist"
    params = {"symbol": symbol, "period": "5m", "limit": 2}
    data = await fetch_json(session, url, params)
    if not isinstance(data, list) or len(data) < 2:
        return None
    try:
        oi_old = float(data[0]["sumOpenInterestValue"])
        oi_now = float(data[1]["sumOpenInterestValue"])
        change = (oi_now - oi_old) / oi_old * 100
        return symbol, change, oi_now
    except:
        return None

async def run_scan():
    global oi_records

    async with aiohttp.ClientSession() as session:
        symbols = OI_WHITELIST[:] if OI_USE_WHITELIST else await get_usdt_symbols(session)

        sem = asyncio.Semaphore(OI_CONCURRENCY)
        tasks = []

        for s in symbols:
            async def t(sym=s):
                async with sem:
                    return await get_oi_change(session, sym)
            tasks.append(t())

        results = []
        for coro in asyncio.as_completed(tasks):
            r = await coro
            if r:
                results.append(r)

        now = datetime.now()

        # 更新与新增异动
        for sym, chg, oi in results:
            if abs(chg) >= OI_THRESHOLD:
                oi_records[sym] = {
                    "expire": now + timedelta(minutes=OI_EXPIRE_MINUTES),
                    "change": chg,
                    "oi": oi,
                }
                redis_client.sadd(OI_KEY, sym)   # ⭕ 写入 Redis，集合去重

        # 清理过期
        for sym in list(oi_records.keys()):
            if oi_records[sym]["expire"] < now:
                print(f"❎ {sym} 超过 {OI_EXPIRE_MINUTES} 分钟无异动 → 移除")
                del oi_records[sym]
                redis_client.srem(OI_KEY, sym)  # ⭕ 从 Redis 移除

        print("--------------------------------------------------------------")
        print(f"🕒 {now.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"🔥 当前OI异动池: {len(oi_records)}")
        for sym, v in oi_records.items():
            print(f"  {sym} | 变化: {v['change']:.2f}% | OI: {v['oi']:.2f}")
        print("--------------------------------------------------------------\n")

async def scheduler():
    while True:
        await wait_for_next_period()
        await run_scan()

if __name__ == "__main__":
    asyncio.run(scheduler())
