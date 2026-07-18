# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Instead, use GitHub's
private vulnerability reporting:

**https://github.com/anirudhlath/alfred-home-service/security/advisories/new**

You'll get an acknowledgement within 72 hours. Please include reproduction steps and
the affected component (auth, channels, notifications, integrations, SDK, …).

## Supported versions

Pre-1.0: only the latest release (and `main`) receives security fixes.

## Scope notes

Alfred is a self-hosted system that controls a real home. Reports about
authentication (WebAuthn, session cookies, trusted-network gating), the secrets
manager, notification credentials, or prompt-injection paths into home-controlling
tools are especially welcome.
