-- The exact-match response cache (SPEC Phase 2.3): one string per cached
-- completion, its JSON, under tg:exact:{tenant}:{hash}, where hash is the
-- request's key (cache/policy.py) and the tenant keeps tenants apart. ''
-- for a hash means the request isn't cached: nothing is read or written.
--
-- A library: the admission scripts read an entry after admitting a request,
-- and the settle script stores one, so the cache costs no call of its own.

local function exact_key(tenant, hash)
  return 'tg:exact:' .. tenant .. ':' .. hash
end

local function exact_get(tenant, hash)
  if hash == '' then
    return false
  end
  return redis.call('GET', exact_key(tenant, hash))
end

local function exact_put(tenant, hash, entry, ttl_s)
  if hash == '' then
    return
  end
  redis.call('SET', exact_key(tenant, hash), entry, 'EX', ttl_s)
end
