# Troubleshooting Guide

This guide covers common issues, error messages, and solutions for Actions Manager. Use this reference to quickly resolve problems during development, deployment, and operation.

## Table of Contents

- [Quick Diagnostics](#quick-diagnostics)
- [Installation Issues](#installation-issues)
- [Authentication Issues](#authentication-issues)
- [Backend Issues](#backend-issues)
- [Frontend Issues](#frontend-issues)
- [Database Issues](#database-issues)
- [Docker Issues](#docker-issues)
- [GitHub API Issues](#github-api-issues)
- [Performance Issues](#performance-issues)
- [Getting Help](#getting-help)

## Quick Diagnostics

Start here for any issue:

**For Docker Self-Hosted (single container on port 8080):**

```bash
# Check service status
docker compose -f docker-compose.self-hosted.yml ps

# View logs
docker compose -f docker-compose.self-hosted.yml logs --tail=100

# Check connectivity (self-hosted runs everything on port 8080)
curl -I http://localhost:8080          # Frontend, served by nginx
curl http://localhost:8080/healthz     # Backend health, proxied by nginx

# Check disk space
df -h

# Check memory
free -h
```

## Installation Issues

### npm install Takes Too Long or Hangs

**Symptom:** `npm install` in frontend directory takes over 15 minutes or appears hung.

**Solution:**
```bash
# This is expected! Frontend npm install takes 12-13 minutes
# DO NOT cancel the process

# If truly stuck after 20 minutes:
cd frontend
rm -rf node_modules package-lock.json
npm cache clean --force
npm install
```

**Prevention:** Wait patiently. This is normal for the first install.

### pip install Fails with SSL Errors

**Symptom:**
```
SSL: CERTIFICATE_VERIFY_FAILED
Could not fetch URL https://pypi.org/simple/
```

**Solution:**
```bash
cd backend
pip install --trusted-host pypi.org --trusted-host pypi.python.org --trusted-host files.pythonhosted.org -r requirements.txt
```

**Root Cause:** Network environment with SSL interception or certificate issues.

### Docker Build Fails with Network Errors

**Symptom:** Docker build fails downloading packages.

**Solution:**
```bash
# Most installs don't need to build: pull the pre-built self-hosted image instead
docker compose -f docker-compose.self-hosted.yml pull
```

If you do build from source (`Dockerfile.self-hosted`), retry once the network is stable;
don't turn off TLS verification for pip or npm to get past it.

**Note:** Use GitHub Actions workflow for reliable builds in production.

## Authentication Issues

### OAuth Flow Fails

**Symptom:** After GitHub authorization, redirected to an error page or the callback never completes.

**Diagnostic Checks:**
1. Verify the **Authorization callback URL** in your GitHub OAuth app matches your `VITE_BACKEND_URL` + `/auth/callback` exactly (e.g. `http://localhost:8080/auth/callback`)
2. Check Client ID and Secret in your `.env.self-hosted` (self-hosted) or `backend/.env` (local dev) file
3. Verify the application is running on the expected port

**Solutions:**

**For Docker Self-Hosted:**
```bash
# The GitHub OAuth app callback MUST match VITE_BACKEND_URL + /auth/callback
# For default settings:
http://localhost:8080/auth/callback

# For a custom port (e.g. 9000):
http://localhost:9000/auth/callback

# For a production domain:
https://yourdomain.com/auth/callback
```

**For Local Development (backend/frontend running separately):**
```bash
# OAuth app callback MUST point at the backend:
http://localhost:8000/auth/callback
```

**Common Mistakes:**
- ❌ Using `127.0.0.1` instead of `localhost`
- ❌ Including trailing slash in callback URL
- ❌ Wrong port number (e.g. 3000 or 8000 for self-hosted)
- ❌ Missing `/auth/callback` path
- ❌ Changing the host port (`PORT`) without updating `APP_URL` AND the GitHub OAuth app callback URL

### "Invalid Credentials" Error

**Solution:**
```bash
# Regenerate Client Secret in GitHub OAuth app
# Update the environment file
# For self-hosted:
nano .env.self-hosted
# For local development:
nano backend/.env

# Set the correct values
GITHUB_CLIENT_ID=new_value
GITHUB_CLIENT_SECRET=new_value

# Self-hosted: recreate the container so it reads the new values
# (restart keeps the environment the container was created with)
docker compose -f docker-compose.self-hosted.yml up -d
```

### Session Expires Immediately

**Symptom:** Logged out right after login.

**Diagnostic:**
```bash
# Check SECRET_KEY is set (prints set/not set, never the value)
docker compose -f docker-compose.self-hosted.yml exec app sh -c '[ -n "$SECRET_KEY" ] && echo "SECRET_KEY is set" || echo "SECRET_KEY is not set"'

# Check clock sync
date
```

**Solution:**
```bash
# Generate new SECRET_KEY
openssl rand -hex 32

# Add to .env.self-hosted
SECRET_KEY=generated_value

# Recreate the container so it reads the new value
docker compose -f docker-compose.self-hosted.yml up -d
```

## Backend Issues

### Backend Won't Start

**Symptom:** The container exits immediately or restarts in a loop.

**Diagnostic:**
```bash
# View full logs. If startup validation rejects the configuration, the log
# lists every violation it found.
docker compose -f docker-compose.self-hosted.yml logs app

# Check the host port is free (8080 by default)
netstat -tlnp | grep 8080
```

**Common Causes:**

1. **Missing Environment Variables**
   ```bash
   # Verify required vars are set
   GITHUB_CLIENT_ID=...
   GITHUB_CLIENT_SECRET=...
   ```

2. **Port Already in Use**
   ```bash
   # Find the process using the host port
   lsof -i :8080
   
   # Stop it, or move the container to another host port. Compose reads PORT
   # from a .env file next to docker-compose.self-hosted.yml, so later runs keep it.
   echo "PORT=9000" >> .env
   # Also update APP_URL in .env.self-hosted (if set) and the OAuth app callback URL
   docker compose -f docker-compose.self-hosted.yml up -d
   ```

3. **Database Connection Error** (PostgreSQL only)
   ```bash
   # The self-hosted compose file has no database service. The container
   # reaches your database over the network, and localhost inside the
   # container is the container itself.
   POSTGRES_HOST=your-db-host  # NOT localhost
   ```
   See [PostgreSQL Connection Failed](#postgresql-connection-failed).

### "Module Not Found" Errors

**Symptom:**
```
ModuleNotFoundError: No module named 'workflows'
```

**Solution:**
```bash
# For running tests outside Docker
PYTHONPATH=./backend pytest backend/tests

# For development
source venv/bin/activate
cd backend
uvicorn main:app --reload
```

### API Returns 500 Internal Server Error

**Diagnostic:**
```bash
# Check backend logs for stack trace
docker compose -f docker-compose.self-hosted.yml logs --tail=50 app

# Check the SQLite database (the default) exists in the data volume
docker compose -f docker-compose.self-hosted.yml exec app ls -la /app/data

# Check connectivity to GitHub from the container
docker compose -f docker-compose.self-hosted.yml exec app curl -sSI https://api.github.com
```

**Solutions:**
- Review stack trace in logs
- Check GitHub token is valid
- Verify database file exists and is writable
- Check GitHub API rate limits

## Frontend Issues

### Frontend Won't Start

**Local development only.** The self-hosted image has no frontend container: nginx serves
the built frontend from the same container as the backend, so see Backend Won't Start above.

**Symptom:** `npm run dev` or `npm start` fails.

**Diagnostic:**
```bash
# Check the dev server port (Vite is configured for 3000)
netstat -tlnp | grep 3000
```

**Solutions:**

1. **Dependencies Not Installed**
   ```bash
   cd frontend
   rm -rf node_modules
   npm install
   ```

2. **Port Already in Use**
   ```bash
   # Run the dev server on another port (Vite ignores PORT)
   npm run dev -- --port 3001
   ```

### Build Fails with ESLint Errors

**Symptom:**
```
Treating warnings as errors because process.env.CI = true
```

**Solution:**
```bash
# Always use CI=false for builds
cd frontend
CI=false npm run build

# Or set in Dockerfile
ENV CI=false
```

### White Screen After Loading

**Symptom:** Frontend loads but shows blank page.

**Diagnostic:**
```bash
# Check browser console for errors (F12)
# Common issues:
# - CORS errors
# - API endpoint unreachable
# - JavaScript errors
```

**Solutions:**

1. **Backend Not Running**
   ```bash
   # Self-hosted
   curl http://localhost:8080/healthz

   # Local development (backend on port 8000)
   curl http://localhost:8000/healthz
   ```

2. **CORS Configuration** (local development; self-hosted serves the frontend and API from one origin)
   ```bash
   # Verify VITE_FRONTEND_URL in backend/.env
   VITE_FRONTEND_URL=http://localhost:3000
   ```

3. **API URL Mismatch** (local development)
   ```bash
   # Check frontend environment
   VITE_BACKEND_URL=http://localhost:8000
   ```

## Database Issues

### "Purged N orphaned row(s)" on First Startup After Upgrading

**Symptom:**
```
🔄 Purging rows orphaned while SQLite foreign keys were disabled...
   pass 1: removed 214 orphaned row(s) across 6 table(s)
✅ Purged 214 orphaned row(s):
```

**This is expected, not an error.** Earlier versions never enabled SQLite's
`foreign_keys` pragma, which is off by default, so `ON DELETE CASCADE` never
fired. Deleting a project left its memberships, secrets, pull request records,
notification history and other child rows behind as orphans referencing a
project that no longer existed.

Foreign keys are now enforced, and a one-time migration removes those leftover
rows so the database matches its own constraints. The count reflects how much
had accumulated — a large number on a long-running install is normal. The
migration is idempotent, so later startups report
`No orphaned rows found`.

PostgreSQL deployments are unaffected: PostgreSQL has always enforced these
constraints, so orphans could never accumulate there and the migration skips.

**Note:** deleting a project now also deletes that project's notification
delivery history, which previously survived. This is intentional.

### SQLite Database Locked

**Symptom:**
```
sqlite3.OperationalError: database is locked
```

**Solution:**
```bash
# Restart the container; SQLite releases its locks when the process exits
docker compose -f docker-compose.self-hosted.yml restart app
```

The database is `/app/data/actions_manager.db` in the data volume. If an
`actions_manager.db-journal` file sits next to it, don't delete it: SQLite needs it to roll
back an interrupted write. If the error keeps coming back, back up the database first (see
[Backup & Recovery](DEPLOYMENT.md#backup--recovery)) before investigating further.

### PostgreSQL Connection Failed

**Symptom:**
```
could not connect to server: Connection refused
```

**Diagnostic:**
```bash
# A bad database setting stops the app at startup, so the container restarts in a
# loop and `exec` can't reach it. `run --rm` starts a one-off container with the
# same settings instead.

# Connect with the application's own settings (importing database connects)
docker compose -f docker-compose.self-hosted.yml run --rm -u appuser app \
  python -c "import database; print('connected')"

# List the connection settings the container sees (the password is left out).
# DATABASE_URL, when set, overrides POSTGRES_* and contains the password.
docker compose -f docker-compose.self-hosted.yml run --rm app sh -c 'env | grep -E "^POSTGRES_(HOST|PORT|DB|USER)="; if [ -n "$DATABASE_URL" ]; then echo "DATABASE_URL is set and overrides POSTGRES_*"; fi'
```

**Solutions:**

1. **Wrong Host Name**
   ```bash
   # localhost inside the container is the container itself. Use the database
   # server's hostname, or its service name if it runs in the same compose project
   POSTGRES_HOST=postgres  # NOT localhost
   ```

2. **Wrong Credentials**
   ```bash
   # Verify these match the database's own settings
   POSTGRES_USER=actionsmanager
   POSTGRES_PASSWORD=your_password
   ```

3. **Database Doesn't Exist**
   ```bash
   # Create it on the database server (the application image has no psql)
   psql -U actionsmanager -c "CREATE DATABASE actionsmanager"
   ```

### Database Migration Failed

The container runs migrations at every start and exits when one fails, so it restarts in a loop.

**Solution:**
```bash
# See which migration failed and why
docker compose -f docker-compose.self-hosted.yml logs app | grep -iA20 migration

# After fixing the cause, run the migrations once outside the restart loop
docker compose -f docker-compose.self-hosted.yml stop app
docker compose -f docker-compose.self-hosted.yml run --rm --user appuser app python run_migrations.py
docker compose -f docker-compose.self-hosted.yml start app
```

## Docker Issues

### "No Space Left on Device"

**Diagnostic:**
```bash
docker system df
df -h
```

**Solution:**
```bash
# Remove unused images
docker image prune -a

# Remove unused volumes
docker volume prune

# Remove unused containers
docker container prune

# Full cleanup (careful!)
docker system prune -a --volumes
```

### Container Keeps Restarting

**Diagnostic:**
```bash
# Check the container's state
docker compose -f docker-compose.self-hosted.yml ps -a

# View logs
docker compose -f docker-compose.self-hosted.yml logs --tail=100 app

# Check exit code
docker inspect -f '{{.State.ExitCode}}' $(docker compose -f docker-compose.self-hosted.yml ps -aq app)
```

**Common Causes:**
- Application crash on startup
- Missing environment variables
- Port conflicts
- Out of memory

### Cannot Connect to Docker Daemon

**Symptom:**
```
Cannot connect to the Docker daemon. Is the docker daemon running?
```

**Solution:**
```bash
# Start Docker service
sudo systemctl start docker

# Add user to docker group
sudo usermod -aG docker $USER
newgrp docker

# Check Docker is running
docker ps
```

## GitHub API Issues

### Rate Limit Exceeded

**Symptom:**
```
API rate limit exceeded
```

**Diagnostic:**
```bash
# Check rate limit status
curl -H "Authorization: token YOUR_TOKEN" https://api.github.com/rate_limit
```

**Solutions:**
- Wait for rate limit reset (shown in API response)
- Use authenticated requests (higher limits)
- Implement caching

### Insufficient Permissions

**Symptom:**
```
Resource not accessible by integration
```

**Solution:**
1. Re-authorize GitHub OAuth with correct scopes
2. Check repository access permissions
3. Verify OAuth app has necessary permissions

### Workflow Deployment Failed

**Symptom:** Workflow created in Actions Manager but not visible on GitHub.

**Diagnostic:**
```bash
# Check API response in backend logs
docker compose -f docker-compose.self-hosted.yml logs app | grep -i workflow

# Manually test GitHub API
curl -H "Authorization: token YOUR_TOKEN" \
  https://api.github.com/repos/OWNER/REPO/contents/.github/workflows/test.yml
```

**Solutions:**
- Verify GitHub token has `workflow` scope
- Check repository permissions
- Verify workflow YAML is valid
- Check if branch protection prevents workflow changes

## Performance Issues

### Slow Response Times

**Diagnostic:**
```bash
# Check resource usage
docker stats

# Check database performance
# For SQLite: stop the app first, since VACUUM needs the database to itself
# (the image has no sqlite3 CLI; Python's sqlite3 module does the same)
docker compose -f docker-compose.self-hosted.yml stop app
docker compose -f docker-compose.self-hosted.yml run --rm -u appuser app \
  python -c "import sqlite3; c = sqlite3.connect('file:/app/data/actions_manager.db?mode=rw', uri=True); c.execute('ANALYZE'); c.execute('VACUUM'); c.close()"
docker compose -f docker-compose.self-hosted.yml start app

# For PostgreSQL, on the database server
psql -U actionsmanager -d actionsmanager -c "VACUUM ANALYZE"
```

**Solutions:**
- Increase container resources
- Optimize database queries
- Enable caching
- Use PostgreSQL instead of SQLite
- Add database indexes

### High Memory Usage

**Diagnostic:**
```bash
# Check container memory
docker stats --no-stream

# Check system memory
free -h
```

**Solutions:**
```bash
# Limit container memory in docker-compose.self-hosted.yml
services:
  app:
    mem_limit: 1g
    mem_reservation: 512m

# Recreate the container to apply it
docker compose -f docker-compose.self-hosted.yml up -d
```

### Frontend Slow to Load

**Solutions:**
- Build with production optimizations
- Enable gzip compression
- Use CDN for static assets
- Minimize bundle size
- Lazy load components

## Getting Help

### Before Asking for Help

1. **Check logs:**
   ```bash
   docker compose -f docker-compose.self-hosted.yml logs --tail=100 app
   ```

2. **Search existing issues:**
   - [GitHub Issues](https://github.com/greywolflabs/actions-manager/issues)

3. **Try basic diagnostics:**
   - Restart the container: `docker compose -f docker-compose.self-hosted.yml restart app`
   - Check connectivity: `curl http://localhost:8080/healthz`
   - Verify configuration: compare `.env.self-hosted` with `.env.self-hosted.example` (never paste secrets into an issue)

### Creating a Bug Report

Include:
- **Description:** What went wrong?
- **Steps to reproduce:** How to trigger the issue?
- **Expected behavior:** What should happen?
- **Actual behavior:** What actually happens?
- **Environment:**
  - OS and version
  - Docker version
- **Logs:** Relevant log excerpts
- **Screenshots:** If applicable

### Where to Get Help

- **GitHub Issues:** Bug reports
- **GitHub Discussions:** Questions, feature ideas and community support
- **Documentation:** [docs/README.md](README.md)

## Related Documentation

- **[Development Guide](DEVELOPMENT.md)** - Local development setup
- **[Deployment Guide](DEPLOYMENT.md)** - Production deployment
- **[Architecture](ARCHITECTURE.md)** - System architecture
- **[Environment Variables](ENVIRONMENT_VARIABLES.md)** - Configuration reference

---

**Last Updated:** 2026-02-14  
**Version:** 1.0
