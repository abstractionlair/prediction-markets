# Security Policy

## Reporting a vulnerability

Please report security issues privately via a GitHub Security Advisory
(the repository's **Security → Advisories → Report a vulnerability**). I aim to
acknowledge reports within a few days.

## Secret management

This repository contains no live credentials. Secrets are supplied at runtime through
environment variables; on the deployment host they are stored in a vault and rotated.
Example configuration files use placeholder values only.

## Automated scanning

Every push and pull request is scanned for committed secrets by
[gitleaks](https://github.com/gitleaks/gitleaks) (see
`.github/workflows/gitleaks.yml`). Any known historical finding is documented, with its
rotation status, in `.gitleaksignore`.
