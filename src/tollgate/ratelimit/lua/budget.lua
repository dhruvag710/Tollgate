-- Monthly budgets: each tenant's month-to-date spend in integer micro-dollars,
-- under tg:spend:{tenant}:{yyyymm}. Integers, so the counter sums exactly what
-- the ledger's NUMERIC(12,6) costs do. The month is UTC, from Redis server
-- time, so every replica rolls over at the same instant.
--
-- A library: composed in front of the scripts that use it (ratelimit/limiter.py).

-- 'YYYYMM' of the UTC month holding `seconds` since the epoch (days to civil
-- date, after H. Hinnant; valid from 1970 on).
local function utc_month(seconds)
  local z = math.floor(seconds / 86400) + 719468
  local era = math.floor(z / 146097)
  local doe = z - era * 146097
  local yoe = math.floor(
    (doe - math.floor(doe / 1460) + math.floor(doe / 36524) - math.floor(doe / 146096)) / 365)
  local doy = doe - (365 * yoe + math.floor(yoe / 4) - math.floor(yoe / 100))
  local mp = math.floor((5 * doy + 2) / 153)
  local month = mp < 10 and mp + 3 or mp - 9
  local year = yoe + era * 400 + (month <= 2 and 1 or 0)
  return string.format('%04d%02d', year, month)
end

local function spend_key(tenant, month)
  return 'tg:spend:' .. tenant .. ':' .. month
end

-- A budget as the key cache holds it ('12.3456') in micro-dollars, exactly:
-- read as digits, never through a float.
local function micro_usd(decimal)
  local whole, frac = string.match(decimal, '^(%d+)%.?(%d*)$')
  return tonumber(whole) * 1000000 + tonumber(string.sub(frac .. '000000', 1, 6))
end

-- Add a request's cost, once known, to its month's spend. Only to a counter
-- that exists: a missing one is refilled from the ledger (usage/budget.py)
-- with SET NX, and creating it here would turn that refill into a no-op and
-- lose all the month's spend before this request.
local function add_spend(tenant, cost, now)
  if cost <= 0 then
    return
  end
  local key = spend_key(tenant, utc_month(now))
  if redis.call('EXISTS', key) == 1 then
    redis.call('INCRBY', key, cost)
  end
end
