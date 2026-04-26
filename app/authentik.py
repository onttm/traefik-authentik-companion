"""Authentik API client — provisions proxy providers, applications, and outpost membership."""

import logging
import requests

log = logging.getLogger(__name__)


class AuthentikClient:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self._s = requests.Session()
        self._s.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    # ── low-level helpers ────────────────────────────────────────────────────

    def _get(self, path: str, params: dict = None) -> dict:
        resp = self._s.get(f"{self.url}{path}", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, data: dict) -> dict:
        resp = self._s.post(f"{self.url}{path}", json=data, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, data: dict) -> dict:
        resp = self._s.patch(f"{self.url}{path}", json=data, timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ── startup discovery ────────────────────────────────────────────────────

    def get_flow_uuid(self, slug: str) -> str:
        data = self._get("/api/v3/flows/instances/", {"slug": slug})
        results = data.get("results", [])
        if not results:
            raise RuntimeError(f"Flow not found: {slug!r}")
        return results[0]["pk"]

    def get_outpost(self, name: str) -> dict:
        data = self._get("/api/v3/outposts/instances/", {"search": name})
        for outpost in data.get("results", []):
            if outpost["name"] == name:
                return outpost
        raise RuntimeError(f"Outpost not found: {name!r}")

    # ── per-host provisioning ────────────────────────────────────────────────

    def find_provider(self, external_host: str) -> int | None:
        """Return pk of an existing proxy provider for external_host, or None."""
        data = self._get("/api/v3/providers/proxy/", {"search": external_host})
        for p in data.get("results", []):
            if p.get("external_host") == external_host:
                return p["pk"]
        return None

    def create_provider(
        self,
        name: str,
        external_host: str,
        auth_flow: str,
        invalidation_flow: str,
        cookie_domain: str,
    ) -> int:
        result = self._post("/api/v3/providers/proxy/", {
            "name": name,
            "authorization_flow": auth_flow,
            "invalidation_flow": invalidation_flow,
            "external_host": external_host,
            "mode": "forward_single",
            "cookie_domain": cookie_domain,
        })
        return result["pk"]

    def application_exists(self, slug: str) -> bool:
        data = self._get("/api/v3/core/applications/", {"search": slug})
        return any(a.get("slug") == slug for a in data.get("results", []))

    def create_application(
        self, name: str, slug: str, provider_pk: int, launch_url: str
    ) -> None:
        self._post("/api/v3/core/applications/", {
            "name": name,
            "slug": slug,
            "provider": provider_pk,
            "meta_launch_url": launch_url,
            "policy_engine_mode": "any",
        })

    def add_provider_to_outpost(self, outpost: dict, provider_pk: int) -> None:
        current = list(outpost.get("providers") or [])
        if provider_pk in current:
            return
        self._patch(f"/api/v3/outposts/instances/{outpost['pk']}/", {
            "name": outpost["name"],
            "type": outpost["type"],
            "providers": current + [provider_pk],
        })
