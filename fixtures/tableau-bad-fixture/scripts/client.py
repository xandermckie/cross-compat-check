"""Bad-fixture helper script -- deliberately violates the Tableau rule pack for testing."""

import os

# R4: non-standard credential var name (not part of the SKILL.md blob, proving cross-file
# aggregation works -- the standard-contract check should see this too).
SITE_TOKEN = os.environ.get("TABLEAU_SITE_TOKEN")


def run():
    # R2: another hardcoded literal, in a different file than the one in SKILL.md.
    tool_name = "mcp__Tableau__query-datasource"
    return tool_name
