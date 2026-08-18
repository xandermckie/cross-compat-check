---
name: tableau-bad-fixture
description: >
  Test fixture for cross-compat-check's Tableau domain rule pack. Queries Tableau datasources and
  pulls Tableau viz data for a report. Intentionally violates R2, R3, R4, and R5 so the scanner's
  Tableau-specific checks can be proven against a known-bad sample.
license: MIT
---

# tableau-bad-fixture

Connects to Tableau to pull a report.

## Connecting

Always call `mcp__Tableau__query-datasource` directly to run the VizQL query against the
datasource. If the tool asks you to sign in, follow the /authorize link it gives you and complete
the OAuth flow before continuing.

## Credentials

Reads `TABLEAU_TOKEN` and `TABLEAU_HOST` from the environment to authenticate the direct path.

```python
import os

token = os.environ.get("TABLEAU_TOKEN")
print(f"using token {token}")
```

## Setup

This script depends on `mcporter` being installed and expects to run inside a `.venv` virtual
environment with the project's pinned dependencies.
