-- Settle a request once its usage is known, in one step: reconcile its TPM
-- reservation (tpm_reconcile.lua), add its cost, in micro-dollars, to the
-- month's spend (budget.lua), and count its attempt's success against
-- `provider`'s circuit breaker (routing/lua/breaker.lua; '' for none), so a
-- success costs no call of its own. Returns the TPM balance.
--
-- A library (needs token_bucket.lua, tpm_reconcile.lua, budget.lua and breaker.lua).

local function settle(tenant, tpm_limit, delta, cost, provider, window_ms)
  local balance = reconcile(tenant, tpm_limit, delta)
  add_spend(tenant, cost, now_seconds())
  if provider ~= '' then
    breaker_success(provider, breaker_now_ms(), window_ms)
  end
  return balance
end
