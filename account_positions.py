import sys
import time
from binance.client import Client
from config import BINANCE_API_KEY, BINANCE_API_SECRET
from position_cache import position_records   # ← 引入缓存

# Windows CMD 默认编码可能是 GBK，打印 emoji 会触发 UnicodeEncodeError；这里强制 UTF-8 并降级替换不可编码字符
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 连接账户，拉高超时防止网络抖动
client = Client(
    api_key=BINANCE_API_KEY,
    api_secret=BINANCE_API_SECRET,
    requests_params={"timeout": 20}
)

# ===== 新版：设置 recvWindow 与服务器时间检测 =====
try:
    client.recvWindow = 10000  # 可选：10秒窗口，防止网络延迟
    # 检测服务器时间是否能正常获取
    server_time = client.futures_time()  # futures_time 不支持 requests_params
    print(f"Binance 时间同步完成，服务器时间: {server_time['serverTime']}")
except Exception as e:
    print("Binance 时间同步失败:", e)
    
# 🔥 全量账户数据缓存 — DeepSeek 投喂直接读取
account_snapshot = {
    "balance": 0.0,
    "available": 0.0,
    "total_unrealized": 0.0,
    "positions": []
}
tp_sl_cache = {}

TP_SL_TYPES = ["STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"]


def get_tp_sl_orders(symbol, position_side):
    """
    查询某持仓方向的所有 TP/SL（支持基础单 + 条件单）
    """
    orders = []

    # -------------------------------
    # 1️⃣ 基础挂单
    # -------------------------------
    try:
        open_orders = client.futures_get_open_orders(
            symbol=symbol,
            requests_params={"timeout": 20}
        )
    except Exception:
        open_orders = []

    for o in open_orders:
        if (
            o.get("positionSide") == position_side
            and o.get("type") in TP_SL_TYPES
            and o.get("status") in ["NEW", "PARTIALLY_FILLED"]
        ):
            orders.append({
                "orderId": o.get("orderId"),
                "type": o.get("type"),
                "side": o.get("side"),
                "positionSide": o.get("positionSide"),
                "stopPrice": float(o.get("stopPrice") or 0),
                "price": float(o.get("price") or 0),
                "status": o.get("status"),
                "source": "base_order"
            })

    # -------------------------------
    # 2️⃣ 条件单（未触发）
    # -------------------------------
    try:
        algo_orders = client.futures_get_open_orders(
            symbol=symbol,
            conditional=True,
            requests_params={"timeout": 20}
        )
    except Exception:
        algo_orders = []

    for o in algo_orders:
        if o.get("positionSide") == position_side and o.get("orderType") in TP_SL_TYPES:
            orders.append({
                "algoId": o.get("algoId"),
                "type": o.get("orderType"),
                "side": o.get("side"),
                "positionSide": o.get("positionSide"),
                "stopPrice": float(o.get("triggerPrice") or 0),
                "price": float(o.get("price") or 0),
                "status": o.get("algoStatus"),
                "source": "algo_order"
            })

    return orders

def get_account_status():
    data = client.futures_account(requests_params={"timeout": 20})  # /fapi/v2/account

    # 获取所有交易对的标记价格
    premium = client.futures_mark_price(requests_params={"timeout": 20})
    mark_dict = {item["symbol"]: float(item["markPrice"]) for item in premium}

    balance = float(data.get("totalWalletBalance", 0))
    available = float(data.get("availableBalance", 0))
    total_unrealized = float(data.get("totalUnrealizedProfit", 0))

    positions = []
    symbols = set()    # ⏳ ⬅ 用来实时覆盖持仓缓存

    tp_sl_cache.clear()  # 每次刷新缓存

    for p in data.get("positions", []):
        size = float(p.get("positionAmt") or 0)
        if size == 0:
            continue

        symbol = p.get("symbol", "")
        entry = float(p.get("entryPrice") or 0)
        mark = mark_dict.get(symbol, entry)
        pnl = float(p.get("unrealizedProfit") or 0)
        
        pos_side = "LONG" if size > 0 else "SHORT"

        # 收集持仓币
        symbols.add(symbol)

        # 🔥 查询该 symbol & direction 的 TP/SL
        orders = get_tp_sl_orders(symbol, pos_side)
        if symbol not in tp_sl_cache:
            tp_sl_cache[symbol] = {}
        tp_sl_cache[symbol][pos_side] = orders

        positions.append({
            "symbol": symbol,
            "size": size,
            "entry": entry,
            "mark_price": mark,
            "leverage": int(float(p.get("leverage") or 0)),
            "pnl": pnl,
        })

    # 更新持仓 symbol 缓存（不累积）
    position_records.clear()
    position_records.update(symbols)

    # 🔥 覆盖完整账户快照
    account_snapshot["balance"] = balance
    account_snapshot["available"] = available
    account_snapshot["total_unrealized"] = total_unrealized
    account_snapshot["positions"] = positions

    return account_snapshot

def get_open_positions():
    """返回当前持仓涉及的 symbol 列表（从缓存读取）"""
    return list(position_records)
