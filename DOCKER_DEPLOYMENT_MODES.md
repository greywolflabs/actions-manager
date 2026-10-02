# Self-Hosted Docker Deployment

This document describes the ActionsManager self-hosted container: how it is built, how it starts, and what it checks. The first public beta is **Self-Hosted only**; Cloud/SaaS is not an active beta offering (see [LICENSING_MODEL.md](LICENSING_MODEL.md)).

> For first-time self-hosted beta setup, use the simplified install guide: **[INSTALLATION.md](INSTALLATION.md)**.

## Build and Startup Rules

The backend validates its configuration at startup (`backend/mode_validation.py`).
If a rule is broken it prints every violation to stderr and refuses to start,
rather than running with weakened security guarantees.

| Topic | Self-hosted build |
|-------|--------------------|
| Compose file | `docker-compose.self-hosted.yml` |
| Env template | `.env.self-hosted.example` |
| `INSTALLATION_MODE` | `self-hosted`, the only mode the image serves |
| `ENVIRONMENT` | `production` recommended; `development` only for local testing |
| Billing | Free during beta; optional future `LICENSE_KEY` behavior |
| Database | SQLite (default) or PostgreSQL |
| `LICENSE_KEY` | Optional |
| Tier override env vars (`ACCOUNT_TYPE`, `TIER`, `PLAN`, `FORCE_TIER`, `OVERRIDE_TIER`) | **Refused at startup** |
| `ENVIRONMENT` values other than `production` / `development` | **Refused at startup** |
| Non-local `http://` `APP_URL` | **Refused at startup** unless `ALLOW_INSECURE_HTTP=true`; `https://` and `http://localhost` are fine |
| `USE_MOCK_RESPONSES=true`, `DEBUG_MODE=true` | Allowed only with `ENVIRONMENT=development` |
| Missing/invalid license behaviour | Falls back to Free tier |

> **Note:** `ACTIONS_MANAGER_SKIP_MODE_VALIDATION` is honored only when
> `INSTALLATION_MODE=self-hosted` **and** `ENVIRONMENT=development`. It is
> ignored in production.

---

## Self-Hosted Deployment

### Architecture

The self-hosted deployment uses a **single container** that combines both frontend and backend:

```
┌─────────────────────────────────────────┐
│      Single Container (Port 8080)        │
├─────────────────────────────────────────┤
│  ┌─────────────────────────────────┐   │
│  │       Nginx (Port 8080)          │   │
│  │  - Serves frontend static files  │   │
│  │  - Proxies API requests          │   │
│  │  - Handles WebSocket routing     │   │
│  └──────────┬──────────────────────┘   │
│             │ proxies to               │
│             ↓                           │
│  ┌─────────────────────────────────┐   │
│  │   FastAPI Backend (Port 8000)    │   │
│  │  - Python/FastAPI application    │   │
│  │  - SQLite database (default)     │   │
│  │  - License-based tier management │   │
│  └─────────────────────────────────┘   │
│                                          │
│  Frontend build served as static files  │
└─────────────────────────────────────────┘
```

### Key Features

✅ **Simplified Installation**
- Single `docker compose` command
- One container to manage
- No port conflicts
- Official GHCR self-hosted image supports `linux/amd64` and `linux/arm64`

✅ **Single Port**
- Everything on port 8080
- Easier firewall configuration
- Simpler reverse proxy setup
- Compatible with rootless Podman

✅ **Built-in Nginx**
- Serves frontend efficiently
- API request proxying
- WebSocket support

✅ **Flexible Database**
- SQLite by default (no setup needed)
- Optional PostgreSQL for larger deployments

✅ **Beta Availability**
- Free during beta
- No paid plans currently available
- Optional license-key code path exists for future/commercial behavior
- No marketplace dependencies for self-hosted beta

### Files

- `Dockerfile.self-hosted` - Multi-stage Dockerfile combining frontend and backend
- `docker-compose.self-hosted.yml` - Single service configuration
- `.env.self-hosted.example` - Environment template

### Database Migrations

At every container start the entrypoint runs `run_migrations.py`, which applies
database migrations (new tables and new columns on existing tables) before the
API starts. Upgrading keeps the data on the `/app/data` volume. If a migration
fails, the container exits instead of serving an out-of-date schema.

### Deployment Commands

```bash
# Use INSTALLATION.md for the supported first-run commands:
# - Option 1: Docker Run
# - Option 2: Docker Compose
# This document is architecture/reference only.
```

### Environment Variables

Required:
- `SECRET_KEY` - encrypts saved personal access tokens. Generate it once with
  `openssl rand -hex 32` and keep it the same across restarts. Startup doesn't check it,
  but without it PAT sign-in fails with a 503, and the GitHub token from an OAuth sign-in
  is kept only in memory, so it's lost when the container restarts.
- `ALLOW_INSECURE_HTTP=true` - while you serve plain HTTP (no HTTPS in front of the
  container). Without it, PAT sign-in is refused, even from `http://localhost:8080`, and a
  non-loopback `http://` `APP_URL` stops the container starting. Remove it once you're
  behind HTTPS.

Set for you:
- `INSTALLATION_MODE=self-hosted` - set by `docker-compose.self-hosted.yml` and enforced by
  the image's start script

Optional:
- `APP_URL` - the URL browsers use to reach the server, e.g. `https://actions.example.com`.
  Needed for GitHub OAuth, which requires a fixed callback URL, and also used for GitHub
  webhooks and the links in pull request bodies. With PAT sign-in only, you can leave it
  unset: the frontend uses the browser's address.
- `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` - GitHub OAuth app credentials, only for
  OAuth sign-in
- `LICENSE_KEY` - optional self-hosted license key; no paid plans are currently available during beta
- `DATABASE_URL` - PostgreSQL connection (defaults to SQLite)

See [docs/ENVIRONMENT_VARIABLES.md](docs/ENVIRONMENT_VARIABLES.md) for the full reference.

### OAuth Configuration

Register GitHub OAuth app with:
- **Homepage URL**: `http://localhost:8080` (or your domain)
- **Callback URL**: `http://localhost:8080/auth/callback`

---

## Hardening Recommendations for Beta Operators

- [ ] Use HTTPS with SSL/TLS certificate (update port 80 → 443)
- [ ] Set up regular backups (SQLite or PostgreSQL)
- [ ] Configure monitoring and logging
- [ ] Update OAuth callback URL to production domain
- [ ] Consider PostgreSQL for >100 concurrent users

---

## Troubleshooting

**Container won't start:**
- Check logs: `docker compose -f docker-compose.self-hosted.yml logs`
- Verify port 8080 is available
- Check .env.self-hosted file exists

**Can't access application:**
- Verify container is running: `docker compose -f docker-compose.self-hosted.yml ps`
- Check firewall rules for port 8080
- Review nginx logs in container

**`no image found in image index for architecture arm64` when pulling:**
- Pull the latest image again so Docker refreshes manifest metadata:
  `docker compose -f docker-compose.self-hosted.yml pull`
- Then restart with the refreshed image:
  `docker compose -f docker-compose.self-hosted.yml up -d`

**GitHub OAuth error: "The redirect_uri is not associated with this application":**

This error means your GitHub OAuth App's callback URL doesn't match what the application is using.

**Solution:**
1. Go to GitHub Settings → Developer settings → OAuth Apps
2. Find your OAuth application
3. Update the **Authorization callback URL** to: `http://localhost:8080/auth/callback`
4. Also update **Homepage URL** to: `http://localhost:8080`
5. Save changes
6. Set `APP_URL=http://localhost:8080` in `.env.self-hosted` (OAuth needs it for the callback URL) and
   recreate the container: `docker compose -f docker-compose.self-hosted.yml up -d`

**Common mistakes:**
- Using `http://localhost:8000/auth/callback` (local dev port, not Docker port)
- Using `http://localhost:3000` (frontend dev port, not combined container port)
- Missing `/auth/callback` path in the callback URL
- Using `https://` instead of `http://` for localhost

**Build fails with "out of memory" error (Podman/Docker):**

This typically occurs during the frontend build stage. Solutions:

1. **Increase container memory** (Recommended):
   ```bash
   # Docker Desktop: Settings → Resources → Memory (set to 4GB+)
   # Podman on macOS: 
   podman machine stop
   podman machine set --memory 4096
   podman machine start
   ```

2. **Reduce Node.js memory usage**:
   - `Dockerfile.self-hosted` runs the frontend build as
     `NODE_OPTIONS="--max-old-space-size=4096" npm run build`. That value overrides the
     stage's `ENV NODE_OPTIONS="--max-old-space-size=2048"`, which only `npm ci` uses.
   - For systems with less than 4GB RAM, lower the value on that `npm run build` line,
     then rebuild with the command in item 3:
     ```dockerfile
     NODE_OPTIONS="--max-old-space-size=2048" npm run build
     ```

3. **Use Docker instead of Podman** (if issues persist). Building needs the dev overlay,
   which adds the `build:` section the base compose file doesn't have:
   ```bash
   docker compose -f docker-compose.self-hosted.yml -f docker-compose.self-hosted.dev.yml up --build
   ```

4. **Skip the local build**: pull the pre-built image instead. Building the frontend on
   the host first doesn't help, because the image runs `npm run build` itself.
   ```bash
   docker compose -f docker-compose.self-hosted.yml pull
   docker compose -f docker-compose.self-hosted.yml up -d
   ```
