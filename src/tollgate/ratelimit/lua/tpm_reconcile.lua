-- Settle a TPM reservation once actual usage is known: `delta` is tokens used
-- minus tokens reserved. A negative delta refunds, a positive one charges; a
-- charge may take the bucket below zero, and refill pays it back.
--
-- A library (needs token_bucket.lua): composed in front of the scripts that use it.

local function reconcile(tenant, tpm_limit, delta)
  local now = now_seconds()
  local key = 'tg:rl:tpm:' .. tenant
  local tokens = math.min(tpm_limit, bucket_level(key, tpm_limit, tpm_limit / 60, now) - delta)
  bucket_set(key, tokens, now)
  return math.floor(tokens)
end
