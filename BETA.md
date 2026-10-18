# ActionsManager Self-Hosted Beta

ActionsManager Self-Hosted is currently available as a **free beta preview** for testing, evaluation, and feedback.

This beta is **self-hosted only**: it runs on infrastructure you control.

## What beta means

- The beta is intended for testing, evaluation, and feedback.
- Features and limits may change before general availability.

## Self-Hosted Beta Limits

The following limits apply during the self-hosted beta:

| Resource | Beta Limit |
|---|---|
| Caller Workflow Projects | 4 |
| Reusable Workflow Projects | 2 |
| Secrets per project | 6 |
| Environment variables per project | 6 |
| GitHub environments per project | 6 |
| Repositories per project | 10 |
| Workflow run history | 30 days |

These limits are designed to support realistic product evaluation.

## No production guarantee

The beta is provided as-is, without warranty, SLA, support guarantee, uptime guarantee, production-readiness guarantee, or enterprise/compliance/security guarantee. Do not rely on the beta as a guaranteed production service.

## User responsibilities

Self-hosted operators are responsible for:

- Securing their own deployment and network access.
- Using HTTPS if the service is exposed beyond localhost.
- Protecting GitHub OAuth credentials, personal access tokens, webhook secrets, local environment files, database files, backups, and any API keys.
- Configuring GitHub access with the least privileges that fit their use case.
- Reviewing generated or edited workflow changes before applying, merging, or directly committing them.
- Choosing target repositories and branches carefully.
- Backing up application data before upgrades or configuration changes.
- Rotating credentials if they may have been exposed.

For installation steps, see [INSTALLATION.md](INSTALLATION.md). For private vulnerability reporting, see [SECURITY.md](SECURITY.md).
