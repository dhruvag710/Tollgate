-- Circuit breakers, one per provider, in Redis hashes tg:cb:{provider} (SPEC
-- Phase 2.1), shared by every replica. Times are milliseconds of Redis server
-- time. Each function takes `now`, so the transitions can be tested with a
-- clock of the test's own; every script Tollgate runs passes breaker_now_ms().
--
--   closed     Counts the provider's attempts over the last window_ms, a
--              sliding window approximated from two fixed ones: the current
--              window's {window_start, requests, failures}, plus the window
--              before's {prev_requests, prev_failures} weighted by how much of
--              it still lies within the last window_ms. (A window that simply
--              restarted from zero would let its first few attempts decide.)
--              A failure that brings failures to failure_ratio of requests,
--              once requests reach min_requests, opens it. A missing hash is
--              a closed breaker with nothing counted.
--   open       Requests skip the provider until open_until. After that, one
--              request may claim the probe (breaker_claim).
--   half_open  One request, the probe, is trying the provider until
--              probe_until, and every other request skips it. The probe's
--              verdict closes the breaker (a fresh window) or opens it again.
--              If no verdict comes (its replica died), another request may
--              claim the probe once probe_until has passed.
--
-- While the breaker is open or half-open, reports from requests other than
-- the probe are ignored: they come from attempts that started before it
-- opened, and say nothing about the provider now.
--
-- The failure ratio comes in parts per million, so comparing it is exact.
--
-- A library: composed in front of the scripts that use it
-- (routing/circuit_breaker.py, and the admission and settle scripts in
-- ratelimit/limiter.py and auth/cache.py).

local function breaker_now_ms()
  local t = redis.call('TIME')
  return tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
end

local function breaker_key(provider)
  return 'tg:cb:' .. provider
end

-- A time in ms as Redis stores it: integer digits. Lua's own number-to-string
-- conversion would round a large one into an exponent.
local function breaker_ms(value)
  return string.format('%d', math.floor(value))
end

-- What a request may do with `provider`: {'closed', 0}, use it; {'probe', 0},
-- claim its probe first; {'open', wait_ms}, skip it, for at least wait_ms.
local function breaker_view(provider, now)
  local s = redis.call('HMGET', breaker_key(provider), 'state', 'open_until', 'probe_until')
  if s[1] == 'open' then
    local wait = (tonumber(s[2]) or 0) - now
    if wait > 0 then
      return {'open', wait}
    end
    return {'probe', 0}
  end
  if s[1] == 'half_open' then
    local wait = (tonumber(s[3]) or 0) - now
    if wait > 0 then
      -- The probe's verdict is due any moment; a client told to wait shouldn't
      -- wait out the whole lease.
      return {'open', math.min(wait, 1000)}
    end
    return {'probe', 0}
  end
  return {'closed', 0}
end

-- The views of the providers named in args[first], args[first + 1], ...,
-- flattened: {state, wait_ms, state, wait_ms, ...}.
local function breaker_views(args, first, now)
  local out = {}
  for i = first, #args do
    local view = breaker_view(args[i], now)
    out[#out + 1] = view[1]
    out[#out + 1] = view[2]
  end
  return out
end

local function breaker_open(key, now, cooldown_ms)
  redis.call('DEL', key)
  redis.call('HSET', key, 'state', 'open', 'open_until', breaker_ms(now + cooldown_ms))
  return 'open'
end

-- Claim the probe of a provider whose cooldown is over. Returns 1 if request
-- `id` now holds it until now + lease_ms, else 0 (another request has it, or
-- the breaker isn't waiting for one).
local function breaker_claim(provider, id, lease_ms, now)
  if breaker_view(provider, now)[1] ~= 'probe' then
    return 0
  end
  redis.call('HSET', breaker_key(provider), 'state', 'half_open', 'probe', id,
    'probe_until', breaker_ms(now + lease_ms))
  return 1
end

-- The closed breaker's windows at `now`: {start, requests, failures,
-- prev_requests, prev_failures}, rolled forward if time has moved past the
-- current window.
local function breaker_windows(key, now, window_ms)
  local s = redis.call('HMGET', key, 'window_start', 'requests', 'failures',
    'prev_requests', 'prev_failures')
  local w = {tonumber(s[1]), tonumber(s[2]) or 0, tonumber(s[3]) or 0,
    tonumber(s[4]) or 0, tonumber(s[5]) or 0}
  if w[1] == nil or now - w[1] >= 2 * window_ms then
    return {now, 0, 0, 0, 0}  -- nothing counted within the last window
  end
  if now - w[1] >= window_ms then
    return {w[1] + window_ms, 0, 0, w[2], w[3]}
  end
  return w
end

-- Attempts and failures over the last window_ms, as estimated from `w`.
local function breaker_estimate(w, now, window_ms)
  local weight = 1 - (now - w[1]) / window_ms
  return w[2] + w[4] * weight, w[3] + w[5] * weight
end

local function breaker_save(key, w)
  redis.call('HSET', key, 'state', 'closed', 'window_start', breaker_ms(w[1]),
    'requests', w[2], 'failures', w[3], 'prev_requests', w[4], 'prev_failures', w[5])
end

-- Count an attempt that didn't fail. Not for a probe (breaker_probe_succeeded).
local function breaker_success(provider, now, window_ms)
  local key = breaker_key(provider)
  local state = redis.call('HGET', key, 'state')
  if state and state ~= 'closed' then
    return ''
  end
  local w = breaker_windows(key, now, window_ms)
  w[2] = w[2] + 1
  breaker_save(key, w)
  return ''
end

-- Count a failed attempt by request `id`: the probe's failure opens the
-- breaker again. Returns 'open' if this opened it, else ''.
local function breaker_failure(provider, id, now, window_ms, min_requests, ratio_ppm, cooldown_ms)
  local key = breaker_key(provider)
  local s = redis.call('HMGET', key, 'state', 'probe')
  if s[1] == 'half_open' and s[2] == id then
    return breaker_open(key, now, cooldown_ms)
  end
  if s[1] and s[1] ~= 'closed' then
    return ''
  end
  local w = breaker_windows(key, now, window_ms)
  w[2], w[3] = w[2] + 1, w[3] + 1
  local requests, failures = breaker_estimate(w, now, window_ms)
  if requests >= min_requests and failures * 1000000 >= ratio_ppm * requests then
    return breaker_open(key, now, cooldown_ms)
  end
  breaker_save(key, w)
  return ''
end

-- A provider's breaker as an operator sees it: {state, requests, failures,
-- wait_ms}, with requests and failures over the last window_ms (closed only;
-- rounded) and wait_ms until it may be probed (open) or the probe's lease
-- runs out (half_open).
local function breaker_status(provider, now, window_ms)
  local key = breaker_key(provider)
  local s = redis.call('HMGET', key, 'state', 'open_until', 'probe_until')
  if s[1] == 'open' or s[1] == 'half_open' then
    local until_ms = tonumber(s[1] == 'open' and s[2] or s[3]) or now
    return {s[1], 0, 0, math.max(0, until_ms - now)}
  end
  local requests, failures = breaker_estimate(breaker_windows(key, now, window_ms), now, window_ms)
  return {'closed', math.floor(requests + 0.5), math.floor(failures + 0.5), 0}
end

-- The probe answered: close the breaker, with a fresh window. Returns
-- 'closed' if this closed it, else '' (`id` no longer holds the probe).
local function breaker_probe_succeeded(provider, id)
  local key = breaker_key(provider)
  local s = redis.call('HMGET', key, 'state', 'probe')
  if s[1] == 'half_open' and s[2] == id then
    redis.call('DEL', key)
    return 'closed'
  end
  return ''
end

-- The probe ended with no verdict (its client left first): give it up, so the
-- next request may probe at once. Returns 'open' if released, else ''.
local function breaker_release(provider, id, now)
  local key = breaker_key(provider)
  local s = redis.call('HMGET', key, 'state', 'probe')
  if s[1] == 'half_open' and s[2] == id then
    redis.call('HDEL', key, 'probe', 'probe_until')
    redis.call('HSET', key, 'state', 'open', 'open_until', breaker_ms(now))
    return 'open'
  end
  return ''
end
