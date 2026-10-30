"""Per-tenant token bucket in Redis (atomic Lua), shared by every gateway replica."""
from __future__ import annotations

import time

LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1]); local burst = tonumber(ARGV[2]); local now = tonumber(ARGV[3])
local t = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(t[1]); local ts = tonumber(t[2])
if tokens == nil then tokens = burst; ts = now end
tokens = math.min(burst, tokens + (now - ts) * rate)
local ok = 0
if tokens >= 1 then tokens = tokens - 1; ok = 1 end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, 60)
return ok
"""


class TokenBucket:
    def __init__(self, redis):
        self.r = redis
        self.script = redis.register_script(LUA)

    async def allow(self, tenant: str, rate: float, burst: float, now: float | None = None) -> bool:
        res = self.script(keys=[f"rl:{tenant}"], args=[rate, burst, now if now is not None else time.time()])
        if hasattr(res, "__await__"):
            res = await res
        return bool(int(res))
