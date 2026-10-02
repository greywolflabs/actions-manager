# Deployment Guide

This guide covers deploying Actions Manager in production environments, including CI/CD pipeline configuration, Docker deployment, and operational best practices.

## Table of Contents

- [Deployment Overview](#deployment-overview)
- [Self-Hosted Deployment](#self-hosted-deployment)
- [CI/CD Pipeline](#cicd-pipeline)
- [Production Checklist](#production-checklist)
- [Monitoring & Operations](#monitoring--operations)
- [Backup & Recovery](#backup--recovery)

## Deployment Overview

Actions Manager runs as a single self-hosted container on port 8080, with SQLite by default or PostgreSQL.

For the container architecture and startup rules, see [DOCKER_DEPLOYMENT_MODES.md](../DOCKER_DEPLOYMENT_MODES.md).

## Self-Hosted Deployment

### Quick Install

The fastest way to deploy self-hosted:

```bash
curl -fsSL https://raw.githubusercontent.com/greywolflabs/actions-manager/main/install.sh | bash
```

Or clone and run locally:

```bash
git clone https://github.com/greywolflabs/actions-manager.git
cd actions-manager
./install.sh
```

The installer will:
- ✅ Check system requirements
- ✅ Prompt for GitHub OAuth credentials
- ✅ Configure license key (optional)
- ✅ Generate secure SECRET_KEY
- ✅ Build and start containers
- ✅ Display access information

### Manual Self-Hosted Deployment

For custom configurations:

```bash
# 1. Copy environment template
cp .env.self-hosted.example .env.self-hosted

# 2. Edit configuration
nano .env.self-hosted

# 3. Build and start
docker compose -f docker-compose.self-hosted.yml up --build -d
```

**Access:** `http://localhost:8080`

For complete self-hosted installation guide, see [SELF_HOSTED_INSTALL.md](SELF_HOSTED_INSTALL.md).

### Self-Hosted Configuration

Required environment variables in `.env.self-hosted`:

```bash
# GitHub OAuth (Required)
GITHUB_CLIENT_ID=your_github_client_id
GITHUB_CLIENT_SECRET=your_github_client_secret

# Application URLs
FRONTEND_URL=http://localhost:8080
BACKEND_URL=http://localhost:8080

# Security
SECRET_KEY=generate_with_openssl_rand_hex_32

# License (Optional - defaults to Free tier)
LICENSE_KEY=your_jwt_license_key

# Database (Optional - defaults to SQLite)
# POSTGRES_USER=actionsmanager
# POSTGRES_PASSWORD=secure_password
# POSTGRES_DB=actionsmanager
# POSTGRES_HOST=postgres
# POSTGRES_PORT=5432
```

### Self-Hosted Production Checklist

- [ ] Configure SSL/TLS certificate
- [ ] Set strong SECRET_KEY
- [ ] Update OAuth callback URL to production domain
- [ ] Configure PostgreSQL (recommended for production)
- [ ] Set up regular backups
- [ ] Configure monitoring
- [ ] Review and apply security headers
- [ ] Test disaster recovery procedure

## CI/CD Pipeline

Actions Manager includes a comprehensive CI/CD pipeline with automated testing, security scanning, and deployment.

### Pipeline Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    GitHub Actions Workflow                  │
├─────────────────────────────────────────────────────────────┤
│  Trigger: PR to develop, push to release/**, tag, Manual    │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │   Lint       │  │   Test       │  │   Build      │    │
│  │              │  │              │  │              │    │
│  │ • Black      │  │ • pytest     │  │ • Backend    │    │
│  │ • flake8     │  │ • Vitest     │  │ • Frontend   │    │
│  │ • ESLint     │  │ • Coverage   │  │ • Docker     │    │
│  └──────────────┘  └──────────────┘  └──────────────┘    │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │   Security   │  │   SBOM       │  │   Deploy     │    │
│  │              │  │              │  │              │    │
│  │ • Trivy      │  │ • CycloneDX  │  │ • GHCR       │    │
│  │ • Bandit     │  │ • Supply     │  │              │    │
│  │ • Gitleaks   │  │   Chain      │  │              │    │
│  └──────────────┘  └──────────────┘  └──────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

### Pipeline Features

1. **Code Quality**
   - Python: Black, flake8
   - JavaScript: ESLint, Prettier
   - Dockerfile: Hadolint

2. **Testing**
   - Backend: pytest with coverage
   - Frontend: Vitest with coverage
   - Integration tests

3. **Security Scanning**
   - Container: Trivy
   - Python: Bandit
   - Secrets: Gitleaks
   - Dependencies: Dependabot

4. **SBOM Generation**
   - CycloneDX format
   - Supply chain security
   - Vulnerability tracking

5. **Deployment**
   - Docker images to GHCR

### Running Pipeline Locally

```bash
# Backend quality checks
cd backend
black . && flake8 . && pytest

# Frontend quality checks
cd ../frontend
npm run lint && npm test

# Docker build test (the dev overlay adds the build section)
cd ..
docker compose -f docker-compose.self-hosted.yml -f docker-compose.self-hosted.dev.yml build
```

## Production Checklist

### Pre-Deployment

- [ ] Review architecture diagram
- [ ] Provision infrastructure (servers, database, etc.)
- [ ] Configure DNS records
- [ ] Obtain SSL/TLS certificates
- [ ] Create GitHub OAuth app
- [ ] Configure environment variables
- [ ] Review security settings

### Deployment

- [ ] Deploy application
- [ ] Verify backend health (`/healthz` endpoint)
- [ ] Verify frontend loads
- [ ] Test OAuth flow
- [ ] Test core functionality
- [ ] Configure monitoring
- [ ] Set up log aggregation
- [ ] Test backup/restore procedure

### Post-Deployment

- [ ] Monitor application metrics
- [ ] Review logs for errors
- [ ] Test from external network
- [ ] Verify SSL/TLS configuration
- [ ] Document any custom configuration
- [ ] Train team on operations
- [ ] Create runbook for common tasks

## Monitoring & Operations

### Health Checks

```bash
# Backend health (nginx proxies /healthz to the backend)
curl http://localhost:8080/healthz

# Frontend (served by nginx in the same container)
curl -I http://localhost:8080

# Container status and logs
docker compose -f docker-compose.self-hosted.yml ps
docker compose -f docker-compose.self-hosted.yml logs --tail=100 app
```

### Metrics to Monitor

1. **Application Metrics**
   - Request rate
   - Response time
   - Error rate
   - Active users

2. **Infrastructure Metrics**
   - CPU usage
   - Memory usage
   - Disk usage
   - Network I/O

3. **Database Metrics**
   - Connection pool usage
   - Query performance
   - Database size

4. **External Service Metrics**
   - GitHub API rate limits
   - GitHub API response times
   - OAuth success rate

### Log Management

```bash
# Follow the container's logs (nginx and the backend share one container)
docker compose -f docker-compose.self-hosted.yml logs -f app

# Export logs
docker compose -f docker-compose.self-hosted.yml logs --no-color app > actions-manager.log 2>&1
```

### Recommended Monitoring Tools

- **Prometheus + Grafana** - Metrics and dashboards
- **ELK Stack** - Log aggregation and analysis
- **Sentry** - Error tracking
- **UptimeRobot** - Uptime monitoring

## Backup & Recovery

ActionsManager has a supported backup format covering everything needed to rebuild an
installation: every table, the schema version it was written at, and saved credentials in
their encrypted form. Take one before every upgrade.

This section is the operator reference — scheduling, automation, and recovery. For a walkthrough
of the screens with screenshots, see [Backup & Restore](https://actionsmanager.io/features/backup-restore.html).

### Creating a backup

**From the UI** — sign in as a workspace admin, open the account menu, and choose
**Backup**. The panel shows what the archive will contain and downloads it in one click.

**From the command line:**

```bash
docker compose -f docker-compose.self-hosted.yml exec -u appuser app \
  python backup_cli.py backup --out /app/data/backup-$(date +%Y%m%d).tar.gz

docker compose -f docker-compose.self-hosted.yml cp \
  app:/app/data/backup-$(date +%Y%m%d).tar.gz ./
```

The same command works against SQLite and PostgreSQL — the archive is interchangeable, so a
backup taken from SQLite restores into PostgreSQL and vice versa.

### SECRET_KEY

Saved GitHub tokens are stored encrypted and are backed up **still encrypted**. They can only
be decrypted by an installation using the same `SECRET_KEY`.

- Store `SECRET_KEY` somewhere separate from your backups. A backup without it cannot recover
  saved tokens; a backup *with* it in the same place defeats the encryption.
- Restoring under a different key still works — everything except saved tokens is recovered,
  and each user re-enters their PAT. The restore warns you when it detects this.

Backup archives contain your full application data. Treat them as sensitive.

### Checking a backup

Verifies checksums and schema compatibility without changing anything:

```bash
docker compose -f docker-compose.self-hosted.yml exec -u appuser app \
  python backup_cli.py validate --in /app/data/backup-20260811.tar.gz
```

### Restoring

**Into a fresh installation** — start the new container and browse to it. Before the first
account signs in, the sign-in screen offers **Restore from a backup**: upload the archive,
review the summary, and confirm. Once anyone signs in, that option disappears and restoring
becomes a command-line operation.

**From the command line**, which also covers an installation that no longer starts:

```bash
# Report what would happen, without writing
docker compose -f docker-compose.self-hosted.yml exec -u appuser app \
  python backup_cli.py restore --in /app/data/backup-20260811.tar.gz --dry-run

# Apply it
docker compose -f docker-compose.self-hosted.yml exec -u appuser app \
  python backup_cli.py restore --in /app/data/backup-20260811.tar.gz
```

Restoring replaces all existing data. It refuses an installation that already has users
unless you pass `--force`.

A restore validates integrity and schema compatibility *before* changing anything, applies
the data in a single transaction, and runs any migrations the backup predates. A backup from
a newer version of ActionsManager is refused — upgrade first, then restore.

Sign-in sessions are deliberately not carried across a restore, so everyone signs in again
afterwards.

### Recovering from a failed upgrade

1. Roll the image back to the version you upgraded from.
2. Restore the backup you took before upgrading.
3. Confirm the application starts and your projects are present.
4. Report the failure with the migration output before retrying the upgrade.

### Automated backups

```bash
# Daily: write inside the container, copy it out, then prune the host copies.
0 2 * * * cd /path/to/actions-manager && \
  docker compose -f docker-compose.self-hosted.yml exec -T -u appuser app \
  python backup_cli.py backup --out /app/data/backup-$(date +\%Y\%m\%d).tar.gz && \
  docker compose -f docker-compose.self-hosted.yml cp \
  app:/app/data/backup-$(date +\%Y\%m\%d).tar.gz ./backups/ && \
  docker compose -f docker-compose.self-hosted.yml exec -T app \
  rm -f /app/data/backup-$(date +\%Y\%m\%d).tar.gz && \
  find ./backups -name 'backup-*.tar.gz' -mtime +14 -delete
```

Keep the archives off the data volume. Left in `/app/data` they grow without
bound and eventually fill the volume the database lives on.

### Raw database copies

A cold copy of the database file or a `pg_dump` is still a valid disaster-recovery artifact,
and remains the right tool for point-in-time snapshots at the infrastructure layer:

```bash
# SQLite — stop the app first so the copy is consistent
docker compose -f docker-compose.self-hosted.yml stop app
docker compose -f docker-compose.self-hosted.yml cp app:/app/data/actions_manager.db ./actions_manager.db.backup
docker compose -f docker-compose.self-hosted.yml start app

# PostgreSQL — the image has no pg_dump; run it wherever the PostgreSQL
# client can reach the database, with the DATABASE_URL from .env.self-hosted
set -o pipefail
pg_dump "postgresql://USER:PASSWORD@HOST:5432/DBNAME" | gzip > backup.sql.gz
```

Unlike a `backup_cli.py` archive, these carry no schema-version metadata and no integrity
check, so nothing warns you when a dump predates your current schema or was truncated.

## Troubleshooting

For common deployment issues, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

### Quick Diagnostics

```bash
# Check container status
docker compose -f docker-compose.self-hosted.yml ps -a

# View container logs
docker compose -f docker-compose.self-hosted.yml logs --tail=100 app

# Check disk space
df -h

# Check memory
free -h

# Test network connectivity
curl -I https://api.github.com

# Verify an environment variable reached the container
docker compose -f docker-compose.self-hosted.yml run --rm app printenv GITHUB_CLIENT_ID
```

## Related Documentation

- **[Self-Hosted Installation](SELF_HOSTED_INSTALL.md)** - Complete self-hosted guide
- **[Environment Variables](ENVIRONMENT_VARIABLES.md)** - Configuration reference
- **[Architecture](ARCHITECTURE.md)** - System design
- **[Troubleshooting](TROUBLESHOOTING.md)** - Common issues

---

**Last Updated:** 2026-02-14  
**Version:** 1.0
