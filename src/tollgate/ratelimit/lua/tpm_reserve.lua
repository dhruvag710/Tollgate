-- RPM and TPM in one step: take one request token and `reservation` TPM
-- tokens, or neither, so no request consumes one limit while failing the
-- other. Both buckets refill their whole limit over a minute.
--
-- Returns {outcome, wait_seconds, remaining_requests, remaining_tokens}, where
-- outcome is 'ok', 'requests' or 'tokens' (that limit is exhausted; retry in
-- wait_seconds), or 'too_large' (the reservation exceeds the TPM limit itself,
-- so waiting won't help). wait_seconds is a string: Lua numbers in a reply are
-- truncated to integers.
--
-- A library (needs token_bucket.lua): composed in front of the scripts that use it.

local function reserve(tenant, rpm_limit, tpm_limit, reservation)
  local now = now_seconds()
  local rpm_key, tpm_key = 'tg:rl:rpm:' .. tenant, 'tg:rl:tpm:' .. tenant
  local rpm_rate, tpm_rate = rpm_limit / 60, tpm_limit / 60
  local requests = bucket_level(rpm_key, rpm_limit, rpm_rate, now)
  local tokens = bucket_level(tpm_key, tpm_limit, tpm_rate, now)

  if reservation > tpm_limit then
    return {'too_large', '0', math.floor(requests), math.floor(tokens)}
  end
  local outcome, wait = 'ok', 0
  if tokens < reservation then
    outcome, wait = 'tokens', (reservation - tokens) / tpm_rate
  end
  if requests < 1 then
    outcome, wait = 'requests', math.max(wait, (1 - requests) / rpm_rate)
  end
  if outcome ~= 'ok' then
    return {outcome, string.format('%.3f', wait), math.floor(requests), math.floor(tokens)}
  end

  bucket_set(rpm_key, requests - 1, now)
  bucket_set(tpm_key, tokens - reservation, now)
  return {'ok', '0', math.floor(requests - 1), math.floor(tokens - reservation)}
end
