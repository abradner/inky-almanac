"""Shared plumbing for adapters: an HTTP/FTP text getter and a disk cache.

Not a provider itself. Every adapter takes a `get_text(url) -> str`; in
production the background refresher passes the real one (`get_text`) and
the request path passes `offline`, so requests only ever read the caches the
refresher keeps warm (a slow feed must not make the frame's 60 s poll time
out).
"""

import json
import threading
import urllib.request

TTL = 30 * 60
STALE_AFTER = 12 * 3600


class CacheOnly(OSError):
    pass


def offline(url):
    """get_text for the request path: never touch the network."""
    raise CacheOnly("request path is cache-only")


def get_text(url, ua, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def cached(cfg, name, fetch, now, ttl=TTL, check=None, stale_after=STALE_AFTER, log="conditions"):
    """Fetch through a disk cache; on failure use the cache if not too stale.

    Returns (data, fetched_at) or (None, None). `check(data)` must not raise
    for data to be cached: a 200 with junk must never replace a good cache.
    """
    path = cfg.cache_dir / name
    old = None
    if path.exists():
        try:
            old = json.loads(path.read_text())
        except ValueError:
            old = None
    if old and now - old["fetched_at"] < ttl:
        return old["data"], old["fetched_at"]
    try:
        data = fetch()
        if check:
            check(data)
        tmp = path.with_suffix(".%d.tmp" % threading.get_ident())  # two refresher threads
        tmp.write_text(json.dumps({"fetched_at": now, "data": data}))
        tmp.replace(path)
        return data, now
    except Exception as e:
        if not isinstance(e, CacheOnly):
            print(f"{log}: {name} fetch failed ({e!r})")
        if old and now - old["fetched_at"] < stale_after:
            return old["data"], old["fetched_at"]
        return None, None
