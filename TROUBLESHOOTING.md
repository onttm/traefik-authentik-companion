# Troubleshooting & Debugging Reference

Accumulated from the first production deployment on Authentik 2025.12.1. Covers every
non-obvious failure mode hit during initial bring-up. If something breaks, start here.

---

## Quick diagnostic scripts

All scripts assume you have `docker exec` access to both `authentik` and
`traefik-authentik-companion` containers.

### Check token validity and user state

```bash
docker exec authentik ak shell -c "
from authentik.core.models import Token, User
t = Token.objects.filter(identifier='authentik-companion').first()
u = User.objects.filter(username='authentik-companion').first()
if t:
    print('token key prefix:', t.key[:8], '...')
    print('expiring:', t.expiring)
else:
    print('ERROR: token not found')
if u:
    print('user active:', u.is_active)
    print('is_superuser:', u.is_superuser)
    print('type:', u.type)
else:
    print('ERROR: user not found')
" 2>&1 | tail -15
```

### Check RBAC group/role wiring

```bash
docker exec authentik ak shell -c "
from authentik.core.models import User, Group as AKGroup
from authentik.rbac.models import Role

u = User.objects.get(username='authentik-companion')
print('Groups:', list(u.ak_groups.values_list('name', flat=True)))
group = AKGroup.objects.filter(name='authentik-companion').first()
if group:
    print('Group roles:', list(group.roles.values_list('name', flat=True)))
    role = group.roles.first()
    if role:
        print('Role perms:', sorted(p.codename for p in role.group.permissions.all()))
print('has_perm view_flow:', u.has_perm('authentik_flows.view_flow'))
" 2>&1 | tail -15
```

### Test API access from inside the authentik container

```bash
docker exec authentik ak shell -c "
import requests
from authentik.core.models import Token
token = Token.objects.get(identifier='authentik-companion').key
h = {'Authorization': 'Bearer ' + token}
base = 'http://localhost:9000'
for path in [
    '/api/v3/flows/instances/?slug=default-authentication-flow',
    '/api/v3/outposts/instances/',
    '/api/v3/core/applications/bazarr/',
]:
    r = requests.get(base + path, headers=h)
    print(r.status_code, path)
" 2>/dev/null
```

### Check for orphaned providers (no linked application)

```bash
docker exec authentik ak shell -c "
from authentik.providers.proxy.models import ProxyProvider
for p in ProxyProvider.objects.all().order_by('pk'):
    linked = hasattr(p, 'application') and p.application is not None
    app = p.application.slug if linked else 'ORPHANED'
    print(f'pk={p.pk:3d}  {p.name!r:30s}  app={app!r}')
" 2>/dev/null
```

---

## Known failure modes

### 403 on all API calls despite valid token

**Symptom:**
```
requests.exceptions.HTTPError: 403 Client Error: Forbidden for url:
http://authentik:9000/api/v3/flows/instances/?slug=default-authentication-flow
```

**Root cause (Authentik 2025.10+):** Authentik's API enforces permissions through its
RBAC system (User → AKGroup → Role → Permission). Direct Django `user_permissions` set
on the user object are **not checked** by the API layer. If the service account is not
wired through an RBAC group/role, every API call returns 403 regardless of what
`user_permissions` contains.

**Diagnosis:** Run the RBAC check script above. Look for empty `Groups:` or `Group roles:`.

**Fix:** Run the full setup script from the README Step 1. The RBAC group/role wiring is
the critical part. Key steps:
1. Create `AKGroup(name='authentik-companion')`
2. Create `Role(name='authentik-companion')`
3. If `role.group is None`, create a Django `auth.Group` and link it: `role.group = dj_group; role.save()`
4. Call `role.assign_perms(list_of_permission_objects)`
5. `ak_group.roles.add(role)` and `user.ak_groups.add(ak_group)`

---

### Applications list returns empty results (count > 0 but results: [])

**Symptom:** `GET /api/v3/core/applications/` returns HTTP 200 with `count: N` but
`results: []`.

**Root cause:** Authentik uses `django-guardian` object-level permissions to filter the
application list queryset. The service account has model-level RBAC permission (which
allows the request) but has no per-object guardian grants for admin-created applications,
so the queryset returns empty.

**This does NOT affect:**
- `GET /api/v3/flows/instances/` — flows are not guardian-filtered
- `GET /api/v3/providers/proxy/` — providers are not guardian-filtered
- `GET /api/v3/core/applications/{slug}/` — the retrieve endpoint bypasses list filtering

**Fix (already in code):** `find_application()` uses the direct slug endpoint
(`GET /api/v3/core/applications/{slug}/`) instead of the list endpoint. If you see this
behavior on a new endpoint, check whether a direct-retrieve URL exists.

**Do not use `is_superuser=True`** to work around this — it violates the minimum-privilege
design. Use the direct endpoint approach instead.

---

### Provider lookup returns None for existing provider (→ 400 name conflict)

**Symptom:**
```
Poll cycle failed: 400 Client Error: Bad Request for url: .../api/v3/providers/proxy/
```
With error body: `{"name": ["Provider with this name already exists."]}`

**Root cause:** The old `find_provider()` used `?search=<URL>` which Authentik matches
against provider **names**, not the `external_host` field. The URL string never matches
a provider name, so `find_provider` always returns None and tries to create a duplicate.

**Fix (already in code):** `find_provider()` fetches all providers with `?page_size=500`
and filters client-side by `external_host`. No search parameter.

**Cleanup:** Orphaned providers (created before the fix was deployed) must be deleted
manually:
```bash
docker exec authentik ak shell -c "
from authentik.providers.proxy.models import ProxyProvider
for p in ProxyProvider.objects.filter(external_host='https://your-host.example.com'):
    linked = hasattr(p, 'application') and p.application is not None
    print(f'pk={p.pk} linked={linked} name={p.name!r}')
    if not linked:
        p.delete()
        print('  deleted')
" 2>/dev/null
```

---

### Application slug mismatch between Traefik hostname and Authentik

**Symptom:**
```
Provider pk=11 already exists
POST /api/v3/core/applications/ → 400: {"provider":["Application with this provider already exists."]}
```

**Root cause:** The companion derives the application slug from the Traefik hostname
(e.g. `qbit.distraktr.com` → slug `qbit`). But a manually-created Authentik application
may use a different slug (e.g. `qbittorrent`). `find_application("qbit")` returns 404,
so the companion tries to create a new application — which fails because provider pk=11
is already linked to `qbittorrent`.

**Fix (already in code):** When `find_provider()` finds an existing provider, the code
calls `get_provider_application_slug(provider_pk)` which reads `assigned_application_slug`
from `GET /api/v3/providers/proxy/{pk}/`. The companion then looks up the application
using that slug instead of the hostname-derived one.

---

### Docker socket-proxy: "No connection adapters were found for 'tcp://...'"

**Symptom:**
```
WARNING docker: Docker API unavailable, skipping label read:
No connection adapters were found for 'tcp://socket-proxy:2375/containers/json'
```

**Root cause:** The `requests` library does not support the `tcp://` scheme. Docker
socket-proxies speak plain HTTP — the correct scheme is `http://`.

**Fix (already in code):** `DockerClient.__init__` rewrites `tcp://` → `http://`
automatically. If you see this warning, the container is running an old image.
Rebuild and recreate.

---

### Container starts but immediately exits (not a crash loop)

Check the secrets mount. The token file must exist and be readable:
```bash
docker exec traefik-authentik-companion cat /run/secrets/authentik_token | wc -c
```
Should print a non-zero character count. If the file is missing, the container exits
before the first log line. Check your `secrets:` block in `compose.yml` and confirm
the secret file exists on the host.

---

### STALE_ACTION=remove: "Delete permissions check failed" on startup

**Symptom:**
```
ERROR: Delete permissions check failed. Grant delete permissions with:
  docker exec authentik ak shell -c "..."
```

The companion probes delete permissions on startup by attempting `DELETE` on a
nonexistent resource. 404 = permission granted (object just doesn't exist). 403 = no
permission. If you see this, the RBAC role is missing `delete_application` or
`delete_proxyprovider`.

**Fix:** Run the "Existing install: enable STALE_ACTION=remove" command from the README.

---

### authorization_flow set to login flow — authenticated users looped back to login

**Symptom:** Users complete login successfully but are immediately redirected back to the Authentik login page on every access. Plex-federated users (no local password) hit a `404` loop at `/if/flow/default-authentication-flow/undefined`. Local users (akadmin) appear to work because they can complete the password stage repeatedly, but they are also going through the wrong flow.

**Root cause:** The proxy provider's `authorization_flow` field was set to the authentication flow UUID (`default-authentication-flow`) instead of an implicit-consent flow (`default-provider-authorization-implicit-consent`). These are two different flow types:

- `authentication_flow` — runs when the user is **not logged in** (login page)
- `authorization_flow` — runs when the user **is already logged in** and requests app access (consent)

Setting both to the same login flow means every access attempt — even from already-authenticated users — triggers the login flow again. The session is valid, so Authentik tries to skip to the end of the login flow, hits an undefined stage, and renders a 404.

**Diagnosis:** Check any proxy provider's authorization flow:

```bash
docker exec authentik ak shell -c "
from authentik.providers.proxy.models import ProxyProvider
from authentik.flows.models import Flow
consent_slug = 'default-provider-authorization-implicit-consent'
consent_uuid = Flow.objects.get(slug=consent_slug).pk
bad = [(p.name, str(p.authorization_flow_id)) for p in ProxyProvider.objects.all()
       if str(p.authorization_flow_id) != str(consent_uuid)]
print('Wrong authorization_flow:', len(bad))
for name, fid in bad:
    print(' ', name, fid)
" 2>/dev/null
```

**Fix:** Bulk-update all providers to the correct authorization flow:

```bash
docker exec authentik ak shell -c "
from authentik.providers.proxy.models import ProxyProvider
from authentik.flows.models import Flow
consent = Flow.objects.get(slug='default-provider-authorization-implicit-consent')
updated = ProxyProvider.objects.all().update(authorization_flow=consent)
print('Updated', updated, 'providers')
" 2>/dev/null
```

After running this, restart the embedded outpost container (or restart Authentik) so it reloads the provider configs. Then delete any active Authentik sessions for affected users so they go through the corrected flow on next access.

**Prevention:** This bug was in the companion's `create_provider()` call — it passed the auth flow UUID for both `authentication_flow` and `authorization_flow`. The fix resolves both flow slugs independently at startup (`AUTHENTIK_AUTH_FLOW` and `AUTHENTIK_AUTHZ_FLOW`) and passes them separately. Verify on startup that the two logged UUIDs differ.

---

## Authentik 2025.x permission system notes

These findings apply to Authentik 2025.10–2025.12.1. May change in later versions.

| API endpoint | Guardian-filtered? | Notes |
|---|---|---|
| `GET /api/v3/flows/instances/` | No | Returns all flows with model-level permission |
| `GET /api/v3/outposts/instances/` | No | Returns all outposts |
| `GET /api/v3/providers/proxy/` | No | Returns all providers |
| `GET /api/v3/core/groups/` | No | Returns all groups |
| `GET /api/v3/policies/bindings/` | No | Returns all bindings |
| `GET /api/v3/core/applications/` | **Yes** | Empty results without per-object guardian grants |
| `GET /api/v3/core/applications/{slug}/` | No | Direct retrieve bypasses list filter |

**RBAC role creation gotcha:** `Role.objects.create()` or `get_or_create()` does NOT
automatically create the backing Django `auth.Group`. You must create it manually and
link it: `role.group = dj_group; role.save()`. Without this, `role.group` is None and
`role.assign_perms()` raises `AttributeError: 'NoneType' object has no attribute 'permissions'`.

**`assign_perms` signature:** Takes `Permission` objects or `"app_label.codename"` strings,
plus an optional `obj` for object-level grants. Pass `obj=None` (default) for model-level
(global) permissions.

---

## State file

The companion persists provisioned hosts to `/data/provisioned.json` inside the container,
mapped to `$DOCKERDIR/appdata/traefik-authentik-companion/data/provisioned.json` on the host.

If the state file is deleted or empty, the companion treats all 44+ Traefik routers as new
on the next poll and attempts to provision all of them. With the current code this is safe —
it finds existing providers/applications and skips creation — but it generates noisy logs
and takes several minutes to complete. It is not harmful to let it run.

To inspect the current state:
```bash
cat $DOCKERDIR/appdata/traefik-authentik-companion/data/provisioned.json | python3 -m json.tool
```
