# Security Policy

## Reporting a vulnerability

If you believe you've found a security issue in this project, please report it privately. **Do not open a public GitHub issue.**

**Preferred:** use [GitHub's private vulnerability reporting](https://github.com/Bitwise-Forge/icloud-shared-album-sync/security/advisories/new) — this opens a private advisory that only the Bitwise Forge team can see.

**Fallback:** email `contact@bitwiseforge.com` with:

- A description of the issue and its impact
- Steps to reproduce (a minimal recipe is more valuable than a full write-up)
- The version or commit SHA where you observed it

We aim to acknowledge receipt within a few business days. This is a community-supported open source project without a formal SLA, but security issues get prioritized over feature work.

## What counts as a security issue

- Anything that lets an attacker cause the tool to write files outside `OUTPUT_DIR` or overwrite files it doesn't own
- Anything that lets a malicious Shared Album URL cause code execution, path traversal, or resource exhaustion beyond a normal Apple response
- Anything that lets a network attacker (MITM) alter what's downloaded without detection
- Container escape or privilege escalation via the Docker image

Not security issues (open a regular issue instead):

- Bugs where the tool fails to sync or crashes on well-formed input
- Requests for features that would improve security posture (like content-hash verification of downloaded assets)

## Supported versions

Security fixes land on the `main` branch and the most recent tagged release. Older tags may not receive backports.

## Third-party dependencies

The runtime code depends only on the Python standard library. The Docker image is based on `python:3-slim`. Vulnerabilities in Python or the base image are the upstreams' responsibility, but if a CVE materially affects this project's ability to run safely, please report it here so we can pin a fix or ship a new image.
