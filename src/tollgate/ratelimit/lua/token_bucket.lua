-- Token buckets in Redis hashes {tokens, ts}: `capacity` tokens, refilled at
-- `rate` tokens per second. Time is always Redis server time, never the app's
-- clock: replicas' clocks skew, Redis has one.
--
-- A library: composed in front of the scripts that use it (ratelimit/limiter.py).

-- Refill windows are a minute, so state left untouched for two expires.
local BUCKET_TTL_MS = 120000

local function now_seconds()
  local t = redis.call('TIME')
  return tonumber(t[1]) + tonumber(t[2]) / 1000000
end

-- The bucket's level at `now`: full if it doesn't exist yet.
local function bucket_level(key, capacity, rate, now)
  local state = redis.call('HMGET', key, 'tokens', 'ts')
  local tokens, ts = tonumber(state[1]), tonumber(state[2])
  if tokens == nil or ts == nil then
    return capacity
  end
  return math.min(capacity, tokens + math.max(0, now - ts) * rate)
end

local function bucket_set(key, tokens, now)
  -- Explicit formats: Lua's default number-to-string keeps 14 digits and would
  -- drop the microseconds of `now`.
  redis.call('HSET', key, 'tokens', string.format('%.6f', tokens), 'ts', string.format('%.6f', now))
  redis.call('PEXPIRE', key, BUCKET_TTL_MS)
end
