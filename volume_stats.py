import json
import time
import requests
from database import redis_client
from config import OI_BASE_URL as BASE
import math
import logging

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# =========================
# 🔗 URL mapping
# =========================
URLS = {
    "OPEN_INTEREST": BASE + "/fapi/v1/openInterest?symbol={symbol}",
    "FUNDING_RATE": BASE + "/fapi/v1/premiumIndex?symbol={symbol}",
    "TICKER_24HR": BASE + "/fapi/v1/ticker/24hr?symbol={symbol}",
    "OI_HISTORY": BASE + "/futures/data/openInterestHist?symbol={symbol}&period={period}&limit={limit}",
    "TOP_POS_RATIO": BASE + "/futures/data/topLongShortPositionRatio?symbol={symbol}&period={period}&limit={limit}",
    "TOP_ACC_RATIO": BASE + "/futures/data/topLongShortAccountRatio?symbol={symbol}&period={period}&limit={limit}",
    "GLOBAL_ACC_RATIO": BASE + "/futures/data/globalLongShortAccountRatio?symbol={symbol}&period={period}&limit={limit}",
}

VOLUME_INTERVALS = ["5m", "15m", "1h", "4h", "1d"]

# =========================
# 🔐 Cache system
# =========================
_cached = {
    "oi": {},
    "funding": {},
    "24hr": {},
    "oi_hist": {},
    "top_pos": {},
    "top_acc": {},
    "global_acc": {},
}


def _cache_get(group, key, ttl):
    now = time.time()
    item = _cached[group].get(key)
    return item["value"] if item and now - item["ts"] < ttl else None


def _cache_set(group, key, value):
    _cached[group][key] = {"value": value, "ts": time.time()}


# =========================
# 🧱 Klines
# =========================
def load_klines(symbol, interval, limit=100):
    key = f"historical_data:{symbol}:{interval}"
    raw = redis_client.hgetall(key)
    if not raw:
        return []

    items = sorted(raw.items(), key=lambda x: int(x[0]))
    return [json.loads(v) for _, v in items][-limit:]


def calc_volume_compare(klines):
    if not klines:
        return None

    sub = klines[-100:]
    vols = [float(k.get("Volume", 0)) for k in sub]
    avg = sum(vols) / len(vols) if vols else 0
    cur = vols[-1] if vols else 0

    return {
        "current_volume": round(cur, 2),
        "average_volume_100": round(avg, 2),
        "ratio": round(cur / avg, 2) if avg > 0 else 0,
    }

# =========================
# 📌 Core API Wrappers
# =========================
def get_open_interest(symbol):
    key = symbol
    cached = _cache_get("oi", key, ttl=60)
    if cached is not None:
        return cached

    try:
        data = requests.get(URLS["OPEN_INTEREST"].format(symbol=symbol), timeout=5).json()
        value = float(data.get("openInterest"))
    except Exception:
        value = None

    _cache_set("oi", key, value)
    return value

def get_funding_rate(symbol):
    key = symbol
    cached = _cache_get("funding", key, ttl=60)
    if cached is not None:
        return cached

    try:
        data = requests.get(URLS["FUNDING_RATE"].format(symbol=symbol), timeout=5).json()
        value = float(data.get("lastFundingRate"))
    except Exception:
        value = None

    _cache_set("funding", key, value)
    return value

def get_24hr_change(symbol):
    key = symbol
    cached = _cache_get("24hr", key, ttl=60)
    if cached is not None:
        return cached

    try:
        j = requests.get(URLS["TICKER_24HR"].format(symbol=symbol), timeout=5).json()
        result = {
            "priceChange": float(j.get("priceChange", 0)),
            "priceChangePercent": float(j.get("priceChangePercent", 0)),
            "lastPrice": float(j.get("lastPrice", 0)),
            "highPrice": float(j.get("highPrice", 0)),
            "lowPrice": float(j.get("lowPrice", 0)),
            "volume": float(j.get("volume", 0)),
            "quoteVolume": float(j.get("quoteVolume", 0)),
        }
    except Exception:
        result = None

    _cache_set("24hr", key, result)
    return result
    
# =========================
# 📈 OI History
# =========================
def get_oi_history(symbol, period="1h", limit=10):
    key = f"{symbol}_{period}_{limit}"
    cached = _cache_get("oi_hist", key, ttl=120)
    if cached is not None:
        return cached

    try:
        raw = requests.get(URLS["OI_HISTORY"].format(symbol=symbol, period=period, limit=limit), timeout=6).json()
        result = [{
            "timestamp": int(i["timestamp"]),
            "openInterest": float(i["sumOpenInterest"]),
            "openInterestValue": float(i["sumOpenInterestValue"]),
        } for i in raw]
    except Exception:
        result = None

    _cache_set("oi_hist", key, result)
    return result

# =========================
# 🦈 Big player / global long-short ratios
# =========================
def _fetch_lsr(group, url_key, symbol, period, limit):
    key = f"{symbol}_{period}_{limit}"
    cached = _cache_get(group, key, ttl=120)
    if cached is not None:
        return cached

    try:
        raw = requests.get(
            URLS[url_key].format(symbol=symbol, period=period, limit=limit),
            timeout=6
        ).json()
        result = [{
            "timestamp": int(i["timestamp"]),
            "ratio": float(i["longShortRatio"]),
            "long": float(i["longAccount"]),
            "short": float(i["shortAccount"]),
        } for i in raw]
    except Exception:
        result = None

    _cache_set(group, key, result)
    return result

def get_top_position_ratio(symbol, period="1h", limit=30):
    return _fetch_lsr("top_pos", "TOP_POS_RATIO", symbol, period, limit)

def get_top_account_ratio(symbol, period="1h", limit=30):
    return _fetch_lsr("top_acc", "TOP_ACC_RATIO", symbol, period, limit)

def get_global_account_ratio(symbol, period="1h", limit=30):
    return _fetch_lsr("global_acc", "GLOBAL_ACC_RATIO", symbol, period, limit)

# =========================
# 🎯 Normalization helpers
# =========================
def normalize(value, min_v, max_v):
    if value is None:
        return 0
    return max(0, min(1, (value - min_v) / (max_v - min_v)))


def normalize_inverse(value, min_v, max_v):
    """For reverse logic metrics: crowd bullish = bearish sentiment"""
    if value is None:
        return 0
    return max(0, min(1, (max_v - value) / (max_v - min_v)))


# =========================
# 💡 Smart Sentiment Score
# =========================
def calc_smart_sentiment(symbol, interval):
    """Multi-timeframe smart sentiment with adaptive lookback for top/global ratios"""
    try:
        # ===== Load Kline data ===== #
        kl = load_klines(symbol, interval)
        if not kl:
            raise ValueError("Kline data missing")

        volume = calc_volume_compare(kl)
        cur_oi = get_open_interest(symbol)
        funding = get_funding_rate(symbol)

        # ===== Adaptive lookback (N bars) ===== #
        lookback_map = {"5m": 20, "15m": 12, "1h": 5, "4h": 3, "1d": 2}
        N = lookback_map.get(interval, 3)

        # ===== Fetch top/global ratios with lookback ===== #
        top_pos = get_top_position_ratio(symbol, interval, N)
        top_acc = get_top_account_ratio(symbol, interval, N)
        global_acc = get_global_account_ratio(symbol, interval, N)

        # ===== Average over lookback for smoothing ===== #
        top_pos_val = sum([x["ratio"] for x in top_pos]) / len(top_pos) if top_pos else None
        top_acc_val = sum([x["ratio"] for x in top_acc]) / len(top_acc) if top_acc else None
        global_val = sum([x["ratio"] for x in global_acc]) / len(global_acc) if global_acc else None
        vol_ratio = volume["ratio"] if volume else 1.0

        # ===== OI NORMALIZED ===== #
        oi_hist = get_oi_history(symbol, "1h", limit=10)
        if oi_hist:
            oi_values = [x["openInterest"] for x in oi_hist]
            min_oi = min(oi_values)
            max_oi = max(oi_values)
            if max_oi == min_oi:
                oi_score = 0.5
            else:
                oi_score = normalize(cur_oi, min_oi, max_oi)
        else:
            oi_score = 0.5

        # ===== Normalized scores ===== #
        funding_score = normalize(funding, -0.02, 0.02) if funding is not None else 0  # 保留方向
        big_player_score = normalize(top_pos_val, 0.9, 2.0) if top_pos_val else 0
        big_account_score = normalize(top_acc_val, 0.9, 2.0) if top_acc_val else 0
        crowd_inverse_score = normalize_inverse(global_val, 0.8, 1.2) if global_val else 0
        volume_score = normalize(vol_ratio, 0.5, 3.0) if volume else 0

        # ===== Weights by timeframe ===== #
        weights_map = {
            "5m":  {"oi":0.1,"fund":0.2,"big":0.2,"big_acc":0.1,"retail":0.2,"vol":0.2},
            "15m": {"oi":0.15,"fund":0.2,"big":0.25,"big_acc":0.1,"retail":0.2,"vol":0.1},
            "1h":  {"oi":0.2,"fund":0.2,"big":0.25,"big_acc":0.1,"retail":0.15,"vol":0.1},
            "4h":  {"oi":0.25,"fund":0.15,"big":0.3,"big_acc":0.1,"retail":0.2,"vol":0.0},
            "1d":  {"oi":0.3,"fund":0.1,"big":0.35,"big_acc":0.1,"retail":0.2,"vol":0.0},
        }
        weights = weights_map.get(interval, weights_map["1h"])

        # ===== Final sentiment score ===== #
        score = (
            weights["oi"] * oi_score +
            weights["fund"] * funding_score +
            weights["big"] * big_player_score +
            weights["big_acc"] * big_account_score +
            weights["retail"] * crowd_inverse_score +
            weights["vol"] * volume_score
        )
        score_100 = int(max(0, min(100, score * 100)))

        # ===== Strategy tag ===== #
        if score_100 >= 85:
            sentiment_tag = "🟢 Strong Long"
        elif score_100 >= 65:
            sentiment_tag = "🟡 Long Bias"
        elif score_100 >= 45:
            sentiment_tag = "⚪ Neutral"
        elif score_100 >= 25:
            sentiment_tag = "🔵 Short Bias"
        else:
            sentiment_tag = "🔴 Strong Short"

        return {
            "symbol": symbol,
            "interval": interval,
            "sentiment_score": score_100,
            "tag": sentiment_tag,
            "factors": {
                "open_interest": round(oi_score, 3),
                "funding_rate": round(funding_score, 3),
                "big_whales": round(big_player_score, 3),
                "big_accounts": round(big_account_score, 3),
                "retail_inverse": round(crowd_inverse_score, 3),
                "volume_emotion": round(volume_score, 3),
            }
        }

    except Exception as e:
        logging.error(f"Sentiment calc error: {e}")
        return {
            "symbol": symbol,
            "interval": interval,
            "sentiment_score": 50,
            "tag": "⚪ Neutral",
            "factors": {
                "open_interest": 0,
                "funding_rate": 0,
                "big_whales": 0,
                "big_accounts": 0,
                "retail_inverse": 0,
                "volume_emotion": 0,
            }
        }
