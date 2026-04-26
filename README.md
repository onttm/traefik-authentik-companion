# authentik-companion

Watches the Traefik API and automatically provisions Authentik Proxy Provider + Application + Outpost membership for every subdomain protected by `chain-authentik`.

Designed as a companion to [cf-companion](https://github.com/tiredofit/docker-traefik-cloudflare-companion) — both tools poll the same Traefik source. Goal: converge into a unified `stack-companion` that handles Cloudflare DNS and Authentik provisioning in one pass.

## How it works

1. Polls `GET /api/http/routers` on the Traefik API every `POLL_INTERVAL` seconds
2. Filters for routers whose middleware list contains `AUTHENTIK_MIDDLEWARE` (default: `chain-authentik`)
3. For each new `Host()` found:
   - Creates a **Proxy Provider** (`forward_single` mode, scoped cookie domain)
   - Creates an **Application** linked to the provider
   - Adds the provider to the configured **Outpost** (defaults to embedded outpost)
4. Persists provisioned hosts to `/data/provisioned.json` across restarts

Covers both **file-provider** rules (`app-*.yml`) and **Docker-label** routers — Traefik merges all sources into a single API response.

Provision-only: existing apps are never deleted. Remove stale apps manually in the Authentik UI.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `TRAEFIK_URL` | *(required)* | Traefik API base URL, e.g. `http://traefik:8080` |
| `AUTHENTIK_URL` | *(required)* | Authentik base URL, e.g. `http://authentik:9000` |
| `AUTHENTIK_TOKEN_FILE` | `/run/secrets/authentik_token` | Path to API token file (Docker secret) |
| `AUTHENTIK_TOKEN` | — | Token value directly (overrides file) |
| `AUTHENTIK_OUTPOST_NAME` | `authentik Embedded Outpost` | Outpost to add providers to |
| `AUTHENTIK_MIDDLEWARE` | `chain-authentik` | Middleware substring to match |
| `AUTHENTIK_AUTH_FLOW` | `default-authentication-flow` | Auth flow slug |
| `AUTHENTIK_INVALIDATION_FLOW` | `default-provider-invalidation-flow` | Invalidation flow slug |
| `POLL_INTERVAL` | `60` | Seconds between Traefik polls |
| `LOG_LEVEL` | `INFO` | Python log level |
| `STATE_FILE` | `/data/provisioned.json` | Persistent state path |

## Authentik API token

Create a token in Authentik → **Admin Interface → Directory → Tokens → Create**. The user needs the `can_impersonate` permission or full admin access.

Store it as a Docker secret:

```bash
echo -n "your-token-here" | sudo tee /home/sysadmin/docker/secrets/authentik_token > /dev/null
sudo chmod 600 /home/sysadmin/docker/secrets/authentik_token
```

## Deployrr / docker-compose usage

See the [deployrr-tools community app](https://github.com/onttm/deployrr-tools/tree/main/community-apps/authentik-companion) for the ready-to-use `compose.yml` and `manifest.json`.

## Future: cf-companion integration

Both authentik-companion and cf-companion watch the Traefik router list for the same event: a new protected subdomain appearing. The planned integration path:

1. **Phase 1 (now):** run independently, same poll cadence
2. **Phase 2:** shared Traefik discovery module / library
3. **Phase 3:** single `stack-companion` container that provisions both Cloudflare DNS and Authentik in one poll cycle, with a pluggable provider interface
