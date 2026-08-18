---
name: tableau-good-fixture
description: >
  Test fixture for cross-compat-check's Tableau domain rule pack. Queries Tableau datasources for
  a report while following the org's Tableau cross-compatibility requirements (R1-R7). Used to
  prove the scanner reports zero Tableau-domain findings on a compliant skill.
license: MIT
compatibility: >
  Uses the standard TABLEAU_PAT_NAME/TABLEAU_PAT_VALUE credential contract in Claude Code; in
  Cowork it relies entirely on the session's pre-authorized Tableau connectors.
---

# tableau-good-fixture

Connects to Tableau to pull a report, using whichever connection method is appropriate for the
current environment, decided up front from credential presence -- never by trial and error.

## Connecting

In Claude Code (or any environment with user-supplied Tableau credentials), connect directly
using those credentials. In Cowork, never assume a specific `mcp__<server>__` tool prefix --
pattern-match on the "Tableau" server name across whatever prefix resolves this session. Prefer
the desktop-bridge Tableau extension first; fall back to the cloud-side Tableau connector,
including failover mid-task if the bridge drops. Use ToolSearch to load the connector's schema
before first use -- this is loading a known connector, not shopping for an alternative server.
Never trigger an authorization or elicitation flow to any MCP server or gateway -- the configured
connectors are already authorized, and a tool that demands new auth is the wrong tool.

## Credentials

Reads `TABLEAU_PAT_NAME` and `TABLEAU_PAT_VALUE` from the environment (required), with
`TABLEAU_SERVER` and `TABLEAU_SITE` optional. Missing credentials fail fast with a clear message.
Per-user tokens only. Token values are never printed or logged -- only whether one was found.

```python
import os

pat_name = os.environ.get("TABLEAU_PAT_NAME")
pat_value = os.environ.get("TABLEAU_PAT_VALUE")
if not pat_name or not pat_value:
    raise SystemExit("Missing TABLEAU_PAT_NAME/TABLEAU_PAT_VALUE -- set both before running.")
print("credentials loaded")
```

Runs on a stock Python 3.9+ interpreter with stdlib only -- no host-specific CLI or virtualenv
assumptions.
