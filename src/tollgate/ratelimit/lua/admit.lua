-- A request's admission in one step: the tenant's monthly budget first, then
-- its rate limits (tpm_reserve.lua), so a request over budget takes nothing
-- from them. A request is admitted while spend is below the budget: requests
-- already in flight when it is crossed still finish, so spend can overshoot by
-- at most their cost (SPEC 4).
--
-- `budget` is the tenant's monthly budget as the key cache holds it, '' for
-- unlimited. Returns reserve()'s reply plus the month, {outcome, wait,
-- requests, tokens, month}, with two more outcomes: 'budget' (spend has
-- reached the budget) and 'spend_unknown' (the month has no spend counter yet:
-- the caller refills it from the ledger and asks again).
--
-- A library (needs token_bucket.lua, tpm_reserve.lua and budget.lua).

local function admit(tenant, budget, rpm_limit, tpm_limit, reservation)
  local month = utc_month(now_seconds())
  if budget ~= '' then
    local spent = redis.call('GET', spend_key(tenant, month))
    if not spent then
      return {'spend_unknown', '0', 0, 0, month}
    end
    if tonumber(spent) >= micro_usd(budget) then
      return {'budget', '0', 0, 0, month}
    end
  end
  local reply = reserve(tenant, rpm_limit, tpm_limit, reservation)
  reply[5] = month
  return reply
end
