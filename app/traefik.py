"""Traefik API client — discovers routers protected by a given middleware."""

import re
import logging
import requests

log = logging.getLogger(__name__)

_HOST_RE = re.compile(r'Host\(`([^`]+)`\)')


class TraefikClient:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def get_protected_hosts(self, middleware_substring: str) -> list[dict]:
        """Return [{host, router}] for every active router using the given middleware.

        Covers both file-provider and Docker-label routers because Traefik merges
        all sources into a single /api/http/routers response.
        """
        try:
            resp = requests.get(f"{self.url}/api/http/routers", timeout=10)
            resp.raise_for_status()
        except Exception as exc:
            log.error("Traefik API unreachable: %s", exc)
            return []

        results = []
        for router in resp.json():
            middlewares = router.get("middlewares") or []
            if not any(middleware_substring in mw for mw in middlewares):
                continue
            rule = router.get("rule", "")
            for host in _HOST_RE.findall(rule):
                results.append({"host": host, "router": router.get("name", "")})
        return results
