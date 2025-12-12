import fnmatch
import threading
import redis
from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError
from config import REDIS_HOST, REDIS_PORT, REDIS_DB


class InMemoryPipeline:
    """Minimal pipeline stub to mimic redis-py pipeline behaviour."""

    def __init__(self, client):
        self.client = client
        self.ops = []

    def hset(self, key, field, value):
        self.ops.append(("hset", key, field, value))
        return self

    def execute(self):
        results = []
        for op in self.ops:
            name, *args = op
            func = getattr(self.client, name)
            results.append(func(*args))
        self.ops.clear()
        return results

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            return False
        self.execute()
        return True


class InMemoryRedis:
    """
    Very small in-process Redis mock.
    Only implements the commands used in this project.
    """

    def __init__(self):
        self._data = {}
        self._lock = threading.RLock()

    def keys(self, pattern="*"):
        with self._lock:
            return [k for k in self._data.keys() if fnmatch.fnmatch(k, pattern)]

    def delete(self, *keys):
        removed = 0
        with self._lock:
            for k in keys:
                if k in self._data:
                    del self._data[k]
                    removed += 1
        return removed

    def exists(self, key):
        with self._lock:
            return 1 if key in self._data else 0

    def hset(self, key, field, value):
        with self._lock:
            bucket = self._data.setdefault(key, {})
            if not isinstance(bucket, dict):
                bucket = {}
                self._data[key] = bucket
            bucket[str(field)] = value
        return 1

    def hget(self, key, field):
        with self._lock:
            bucket = self._data.get(key, {})
            return bucket.get(str(field)) if isinstance(bucket, dict) else None

    def hgetall(self, key):
        with self._lock:
            bucket = self._data.get(key, {})
            return dict(bucket) if isinstance(bucket, dict) else {}

    def hkeys(self, key):
        with self._lock:
            bucket = self._data.get(key, {})
            return list(bucket.keys()) if isinstance(bucket, dict) else []

    def sadd(self, key, *values):
        with self._lock:
            s = self._data.setdefault(key, set())
            if not isinstance(s, set):
                s = set()
                self._data[key] = s
            before = len(s)
            s.update(values)
            return len(s) - before

    def srem(self, key, *values):
        with self._lock:
            s = self._data.get(key, set())
            removed = 0
            for v in values:
                if v in s:
                    s.remove(v)
                    removed += 1
            return removed

    def smembers(self, key):
        with self._lock:
            s = self._data.get(key, set())
            return set(s) if isinstance(s, set) else set()

    def lpush(self, key, value):
        with self._lock:
            lst = self._data.setdefault(key, [])
            if not isinstance(lst, list):
                lst = []
                self._data[key] = lst
            lst.insert(0, value)
            return len(lst)

    def lrange(self, key, start, end):
        with self._lock:
            lst = self._data.get(key, [])
            if not isinstance(lst, list):
                return []
            # Redis lrange end is inclusive
            if end is None or end >= len(lst):
                end_idx = len(lst)
            else:
                end_idx = end + 1
            return lst[start:end_idx]

    def pipeline(self):
        return InMemoryPipeline(self)


def _init_redis_client():
    client = redis.StrictRedis(
        host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True
    )
    try:
        client.ping()
        return client, False
    except (RedisConnectionError, TimeoutError, OSError) as e:
        print(f"Redis 连接失败（{e}），将使用进程内内存缓存代替。建议启动 Redis 以获得持久化与多进程共享能力。")
        return InMemoryRedis(), True


redis_client, IS_FAKE_REDIS = _init_redis_client()


def clear_redis():
    keep = {
        "deepseek_analysis_request_history",
        "deepseek_analysis_response_history",
        "trading_records"
    }

    try:
        keys = redis_client.keys("*")
    except Exception as e:
        print(f"Redis 不可用，跳过清理（{e}）")
        return

    deleted = 0
    for key in keys:
        if key not in keep:
            try:
                redis_client.delete(key)
                deleted += 1
            except Exception:
                continue

    print(f"Redis 清理完成 — 删除 {deleted} 个键，保留历史记录")
