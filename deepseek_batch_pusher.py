import json
import aiohttp
import asyncio
import logging
import time
import re
from concurrent.futures import ThreadPoolExecutor
from config import DEEPSEEK_API_KEY, DEEPSEEK_MODEL, DEEPSEEK_URL, OPEN_WHITELIST, MIN_QUOTE_VOLUME_USDT
from database import redis_client
from volume_stats import (
    calc_volume_compare, get_open_interest, get_funding_rate, get_24hr_change, calc_smart_sentiment,
    get_oi_history, get_top_position_ratio, get_top_account_ratio, get_global_account_ratio)
from account_positions import account_snapshot, tp_sl_cache

KEY_REQ = "deepseek_analysis_request_history"
KEY_RES = "deepseek_analysis_response_history"

# 批量缓存
batch_cache = {}
required_intervals = ["1d", "4h", "1h", "15m", "5m"]

# 添加到 batch
def add_to_batch(symbol, interval, klines, indicators):
    if symbol not in batch_cache:
        batch_cache[symbol] = {}
    batch_cache[symbol][interval] = {"klines": klines, "indicators": indicators}

# 判断是否可以推送
def _is_ready_for_push():
    for _, cycles in batch_cache.items():
        for tf in required_intervals:
            if tf not in cycles:
                return False
    return True

# 情绪分数转换交易信号
def sentiment_to_signal(score):
    if score >= 85:
        return "🚨 极端过热 | 警惕顶部反转"
    if score >= 70:
        return "🟢 牛势强劲 |"
    if score >= 50:
        return "⚪ 中性震荡 | 耐心等待突破"
    if score >= 30:
        return "🟡 恐慌缓解"
    return "🔥 极度恐慌"

def _read_prompt():
    """
    读取 prompt.txt 内容作为系统提示。
    如果文件不存在，则返回默认提示。
    """
    try:
        with open("prompt.txt", "r", encoding="utf-8") as f:
            text = f.read()
            whitelist = ", ".join([s for s in OPEN_WHITELIST if isinstance(s, str) and s.strip()])
            text = text.replace("{{OPEN_WHITELIST}}", whitelist or "（空）")
            text = text.replace("{{MIN_QUOTE_VOLUME_USDT}}", str(MIN_QUOTE_VOLUME_USDT))
            return text
    except Exception:
        return "你是一名专业量化策略分析引擎，请严格输出 JSON 数组或 JSON 对象形式的交易信号。"

###############################################
# 🔥 集中预拉取所有 API（线程池 + 异常安全）
###############################################
async def preload_all_api(dataset):
    results = {
        "funding": {},
        "p24": {},
        "oi": {},
        "sentiment": {},
        "oi_hist": {},
        "big_pos": {},
        "big_acc": {},
        "global_acc": {},
    }

    def safe_call(func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except:
            return None

    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=20)

    tasks = []
    for symbol, cycles in dataset.items():
        # 单 symbol
        tasks.append(loop.run_in_executor(executor, safe_call, get_funding_rate, symbol))
        tasks.append(loop.run_in_executor(executor, safe_call, get_24hr_change, symbol))
        tasks.append(loop.run_in_executor(executor, safe_call, get_open_interest, symbol))

        for interval in cycles.keys():
            key = f"{symbol}:{interval}"
            tasks.append(loop.run_in_executor(executor, safe_call, get_oi_history, symbol, interval, 10))
            tasks.append(loop.run_in_executor(executor, safe_call, get_top_position_ratio, symbol, interval, 1))
            tasks.append(loop.run_in_executor(executor, safe_call, get_top_account_ratio, symbol, interval, 1))
            tasks.append(loop.run_in_executor(executor, safe_call, get_global_account_ratio, symbol, interval, 1))
            tasks.append(loop.run_in_executor(executor, safe_call, calc_smart_sentiment, symbol, interval))

    # 执行任务
    completed = await asyncio.gather(*tasks)

    # 按顺序填充结果
    idx = 0
    for symbol, cycles in dataset.items():
        results["funding"][symbol] = completed[idx]; idx += 1
        results["p24"][symbol] = completed[idx]; idx += 1
        results["oi"][symbol] = completed[idx]; idx += 1
        for interval in cycles.keys():
            key = f"{symbol}:{interval}"
            results["oi_hist"][key] = completed[idx]; idx += 1
            results["big_pos"][key] = completed[idx]; idx += 1
            results["big_acc"][key] = completed[idx]; idx += 1
            results["global_acc"][key] = completed[idx]; idx += 1
            results["sentiment"][key] = completed[idx]; idx += 1

    return results

def _extract_decision_block(content: str):
    match = re.search(r"<decision>([\s\S]*?)</decision>", content, flags=re.I)
    if not match:
        return None
    block = match.group(1).strip()
    try:
        parsed = json.loads(block)
        if isinstance(parsed, list):
            return parsed
    except:
        pass
    return None

def _extract_all_json(content: str):
    results = []
    try:
        parsed = json.loads(content)
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict) and "action" in x]
    except:
        pass

    matches = re.findall(r'\{[^{}]*\}', content, flags=re.S)
    for m in matches:
        try:
            obj = json.loads(m)
            if isinstance(obj, dict) and "action" in obj:
                results.append(obj)
        except:
            pass
    return results if results else None
    
###############################################
# 🔥 新版 _format_dataset（不改变业务逻辑）
###############################################
def _format_dataset(dataset, preloaded):
    start_time = time.time()
    text = []
    append = text.append

    # ===== 账户资金 & 持仓 =====
    account = account_snapshot
    append("========= 📌 当前账户资金状态 =========")
    append(f"💰 总权益 Balance: {round(account['balance'], 4)}")
    append(f"🔓 可用余额 Available: {round(account['available'], 4)}")
    append(f"📉 总未实现盈亏 PnL: {round(account['total_unrealized'], 4)}")

    if account["positions"]:
        append("\n📌 当前持仓:")
        for p in account["positions"]:
            amt = float(p["size"])
            entry = float(p["entry"])
            mark = float(p["mark_price"])
            pnl = float(p["pnl"])
            lev = int(p["leverage"])
            side_icon = "🟢 多" if amt > 0 else "🔴 空"
            pnl_pct = round((mark - entry) / entry * 100, 2) if entry > 0 and amt > 0 else round((entry - mark) / entry * 100, 2) if entry > 0 else 0

            line = f"{p['symbol']} | {side_icon} | 数量 {abs(amt)} | 入场 {entry} → 当前价格 {mark} | 💵 盈亏 {pnl} ({pnl_pct}%)"
            pos_side = "LONG" if amt > 0 else "SHORT"
            tp_sl_orders = tp_sl_cache.get(p['symbol'], {}).get(pos_side, [])
            if tp_sl_orders:
                tp_sl_lines = [f"{o['type']}={o['stopPrice']}" for o in tp_sl_orders]
                line += " | TP/SL: " + ", ".join(tp_sl_lines)
            else:
                line += " | TP/SL: 无"
            append(line)
    else:
        append("\n📌 当前无持仓")

    # ===== 多周期循环 =====
    for symbol, cycles in dataset.items():
        append(f"\n============ {symbol} 多周期行情快照 ============")
        fr     = preloaded["funding"].get(symbol)
        p24    = preloaded["p24"].get(symbol)
        oi_now = preloaded["oi"].get(symbol)

        if p24:
            append(f"• 24h 涨跌幅: {p24['priceChangePercent']}% → 最新 {p24['lastPrice']} (高 {p24['highPrice']} / 低 {p24['lowPrice']})")
            append(f"• 24h 成交额: {round(p24['quoteVolume']/1e6, 2)}M USD")

        append(f"💰 当前资金费率 Funding Rate: {fr if fr is not None else '未知'}")

        for interval, data in cycles.items():
            kl = data["klines"]
            ind = data["indicators"]
            last = kl[-1]
            append(f"\n--- {interval} ---")
            append(f"📌 当前周期收盘价格: {last['Close']}")
            key = f"{symbol}:{interval}"

            oi_hist    = preloaded["oi_hist"].get(key)
            big_pos    = preloaded["big_pos"].get(key)
            big_acc    = preloaded["big_acc"].get(key)
            global_acc = preloaded["global_acc"].get(key)
            sentiment  = preloaded["sentiment"].get(key)

            append(f"🧱 当前永续未平仓量 OI: {oi_now if oi_now is not None else '未知'}")

            if oi_hist:
                arr = [round(i["openInterest"], 2) for i in oi_hist][-10:]
                append(f"•最新10条历史 OI 数据趋势: {arr}")

            if big_pos:
                b = big_pos[-1]
                append(f"• 大户持仓量多空比: {b['ratio']} (多 {b['long']}, 空 {b['short']})")
            if big_acc:
                b = big_acc[-1]
                append(f"• 大户账户数多空比: {b['ratio']} (多 {b['long']}, 空 {b['short']})")
            if global_acc:
                g = global_acc[-1]
                append(f"• 全网多空人数比: {g['ratio']} (多 {g['long']}, 空 {g['short']})")

            append("\n📌 CVD 指标:")
            for keycv in ["CVD", "CVD_MOM", "CVD_DIVERGENCE", "CVD_PEAKFLIP", "CVD_NORM"]:
                if keycv in ind:
                    append(f"{keycv}: {ind[keycv]}")

            if sentiment:
                try:
                    score = sentiment["sentiment_score"]
                    fac = sentiment["factors"]
                    append("\n📌 Smart Sentiment Score:")
                    append(f"🎯 情绪评分: {score}/100")
                    append("📊 分项因子(归一化):")
                    append(f"· OI情绪: {fac['open_interest']}")
                    append(f"· Funding情绪: {fac['funding_rate']}")
                    append(f"· 大户情绪: {fac['big_whales']}")
                    append(f"· 散户反向情绪: {fac['retail_inverse']}")
                    append(f"· 成交量情绪: {fac['volume_emotion']}")
                except Exception:
                    append("\n📌 Smart Sentiment Score: 计算失败")
            else:
                append("\n📌 Smart Sentiment Score: 计算失败")

            append("\n📌 波动率指标:")
            if "ATR" in ind:
                append(f"ATR: {ind['ATR']:.6f}")
            if "ATR_MA20" in ind:
                append(f"ATR 20周期均值: {ind['ATR_MA20']:.6f}")

            last_buy  = float(last["TakerBuyVolume"])
            last_sell = float(last["TakerSellVolume"])
            last_vol  = float(last["Volume"])
            ratio     = round(last_buy / last_vol * 100, 2) if last_vol > 0 else 0

            append("\n📌 主动交易量:")
            append(f"主动买入量(Taker Buy): {last_buy}")
            append(f"主动卖出量(Taker Sell): {last_sell}")
            append(f"主动买入占比: {ratio}%")

            vol_info = calc_volume_compare(kl)
            if vol_info:
                append("\n📌 成交量对比:")
                append(f"当前成交量: {vol_info['current_volume']}")
                append(f"100根均量: {vol_info['average_volume_100']}")
                append(f"当前/均量比值: {vol_info['ratio']}")

            opens   = [k["Open"] for k in kl]
            highs   = [k["High"] for k in kl]
            lows    = [k["Low"] for k in kl]
            closes  = [k["Close"] for k in kl]
            volumes = [k["Volume"] for k in kl]
            append("\n📌 K线数组格式从旧 → 新:")
            append(f"open: {opens}")
            append(f"high: {highs}")
            append(f"low: {lows}")
            append(f"close: {closes}")
            append(f"volume: {volumes}")

    # append("\n🧠 现在请分析并输出决策（简洁思维链 < 150 字 + JSON）")
    #调试完毕后可以不输出思维链,节约token
    append("\n🧠 请直接输出交易决策，不需要推理过程，只需JSON格式：")
    append("指令：只输出<decision>标签内的JSON数组，不要任何解释文字。")
    end_time = time.time()
    print(f"[_format_dataset] 函数执行耗时: {end_time - start_time:.3f} 秒")
    return "\n".join(text)

###############################################
# 🔥 DeepSeek 投喂
###############################################
async def push_batch_to_deepseek():
    if not _is_ready_for_push():
        return None

    dataset = batch_cache.copy()
    batch_cache.clear()
    timestamp = int(time.time() * 1000)
    loop = asyncio.get_running_loop()

    print("⏳ 预加载多周期数据……")
    preloaded = await preload_all_api(dataset)
    print("📌 预加载完成 ✓")

    formatted_dataset = await loop.run_in_executor(None, _format_dataset, dataset, preloaded)
    system_prompt = await loop.run_in_executor(None, _read_prompt)

    # 兼容阿里千问 DashScope 的兼容模式（OpenAI 接口路径 /chat/completions）
    endpoint = DEEPSEEK_URL.rstrip("/")
    if endpoint.endswith("/v1"):
        endpoint = f"{endpoint}/chat/completions"

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": formatted_dataset}
        ],
        "temperature": 0.1,
        "max_tokens": 8000,
        "stream": False
    }

    redis_client.lpush(KEY_REQ, json.dumps({
        "timestamp": timestamp,
        "request": formatted_dataset
    }, ensure_ascii=False))

    start = time.perf_counter()
    print("⏳ 正在请求 DeepSeek…")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(endpoint, json=payload,
                                    headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"}) as resp:
                raw = await resp.text()
                cost = round((time.perf_counter() - start) * 1000, 2)
                print(f"DeepSeek 已返回 | 耗时 {cost} ms")

                def parse_ai_response(raw):
                    try:
                        root = json.loads(raw)
                        content = root["choices"][0]["message"]["content"]
                    except:
                        return None
                    d = _extract_decision_block(content)
                    if d: return d
                    return _extract_all_json(content)

                signals = await loop.run_in_executor(None, parse_ai_response, raw)

                redis_client.lpush(KEY_RES, json.dumps({
                    "timestamp": timestamp,
                    "response_raw": raw,
                    "response_json": signals,
                    "status_code": resp.status,
                    "cost_ms": cost
                }, ensure_ascii=False))

                print(f"⏱ DeepSeek 响应耗时: {cost} ms   HTTP: {resp.status}")
                return signals

    except Exception as e:
        logging.error(f"❌ DeepSeek 调用失败：{e}")
        return None
