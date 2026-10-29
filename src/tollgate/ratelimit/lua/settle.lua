-- Settle a request once its usage is known, in one step: reconcile its TPM
-- reservation (tpm_reconcile.lua) and add its cost, in micro-dollars, to the
-- month's spend (budget.lua). Returns the TPM balance.
--
-- A library (needs token_bucket.lua, tpm_reconcile.lua and budget.lua).

local function settle(tenant, tpm_limit, delta, cost)
  local balance = reconcile(tenant, tpm_limit, delta)
  add_spend(tenant, cost, now_seconds())
  return balance
end
