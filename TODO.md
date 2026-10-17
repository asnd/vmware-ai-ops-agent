# Implementation follow-ups

The core agent, collectors, analysis, correlation, actions, CLI, configuration,
deployment manifests, and tests are implemented. This file replaces the original
skeleton checklist; source code and tests in this repository are authoritative.

See [ROADMAP.md](ROADMAP.md) for feature priorities and implementation status.
See [docs/security.md](docs/security.md) before enabling automatic remediation.

## Remaining work

- Persist remediation decisions and approval history across process restarts.
- Validate deployment templates and safety configuration as part of CI.
- Maintain release notes in CHANGELOG.md as releases are prepared.
