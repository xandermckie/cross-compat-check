#!/usr/bin/env python3
"""
Deterministic scanner for cross-compat-check.

Scans a skill directory (a SKILL.md plus optional references/scripts/assets, optionally
sitting inside a plugin) for patterns that behave differently -- or not at all -- depending on
whether the skill is loaded by Claude Code (reading straight off disk) or by Cowork (which only
ever sees a skill via a claude.ai account upload / Skills API / package_skill.py, or as part of
a full plugin install).

This script does NOT try to be clever about intent. It finds patterns and reports them with a
confidence level; a human (or the calling skill, walking the user through each finding) decides
what to do about each one. See references/compat-rules.md for the reasoning behind every rule
coded here -- keep the two in sync if you change one.

Usage:
    python3 scan_skill.py <path-to-skill-dir-or-SKILL.md> [--json]

Output: human-readable report by default; --json for the structured findings list that the
calling skill should use to drive the interactive Q&A.
"""

import argparse
import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# --- Rule 1: frontmatter allowlist -----------------------------------------------------------

SPEC_SAFE_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}

CLAUDE_CODE_ONLY_FIELDS = {
    "when_to_use": "Extra trigger phrases. Fold this text into `description` instead.",
    "argument-hint": "Autocomplete hint. Drop it, or describe expected input in the body.",
    "arguments": "Named $name substitution. Use free-text $ARGUMENTS or describe the input format in prose.",
    "disable-model-invocation": "Manual-only invocation. Express intent in the body prose instead; there's no portable frontmatter equivalent.",
    "user-invocable": "Model-only invocation. Same as above -- express in prose.",
    "disallowed-tools": "Per-turn tool removal. Express as a body instruction instead.",
    "model": "Per-invocation model override. Not replicable outside Claude Code frontmatter -- drop it.",
    "effort": "Per-invocation effort override. Not replicable outside Claude Code frontmatter -- drop it.",
    "context": "Subagent-fork execution. Structural choice, not just metadata -- see tool-reference findings.",
    "agent": "Which subagent type to fork into. Same as `context` -- structural, not portable.",
    "background": "Whether a forked skill blocks the turn. Only meaningful alongside `context: fork`.",
    "hooks": "Session hooks registered on skill load. Not part of the 6-field spec at all.",
    "paths": "Glob-based auto-activation. Drop; describe applicable context in the body/description.",
    "shell": "Chooses bash vs PowerShell for injected commands. Moot once shell injection itself is removed.",
}

# --- Rule 4: known tool name references ------------------------------------------------------

COWORK_ONLY_TOOLS = [
    "AskUserQuestion", "SendUserFile", "SendUserMessage", "ShowOnboardingRolePicker",
    "ScheduleWakeup", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TaskOutput", "TaskStop",
    "ReportFindings", "SuggestSkills", "SuggestConnectors", "ListConnectors",
    "mcp__remote-devices__", "mcp__claude-code-remote__",
]

CLAUDE_CODE_ONLY_REFERENCES = [
    "TodoWrite", "claude plugin validate", "claude plugin eval", "claude --version",
    "settings.json", "hooks.json", "CLAUDE.md",
]

SUBAGENT_NAME_TRAP = [
    (r"\bTask tool\b", "Claude Code calls the subagent-spawning tool \"Task\". Cowork calls it \"Agent\" -- \"Task\" there means something else entirely (the to-do widget)."),
    (r"\bspawn a Task\b", "Same trap as above -- \"Task\" means subagent in Claude Code, to-do item in Cowork."),
]

# --- Rule 2/3: body-level dynamic features ----------------------------------------------------

SHELL_INJECTION_INLINE = re.compile(r"(^|\s)!`([^`]+)`")
SHELL_INJECTION_FENCED = re.compile(r"```!\s*\n(.*?)```", re.DOTALL)

CLAUDE_VARS = re.compile(r"\$\{(CLAUDE_[A-Z_]+)\}")
ARG_PLACEHOLDER = re.compile(r"(?<!\\)\$(ARGUMENTS(\[\d+\])?|\d+|[a-zA-Z_][a-zA-Z0-9_]*)\b")

# --- Rule 5: filesystem/delivery assumptions --------------------------------------------------

ABS_PATH_PATTERNS = [
    re.compile(r"/home/[\w./-]+"),
    re.compile(r"/root/[\w./-]+"),
    re.compile(r"/Users/[\w./-]+"),
    re.compile(r"/mnt/user-data/[\w./-]+"),
    re.compile(r"~/(Downloads|Desktop|Documents)[\w./-]*"),
    re.compile(r"[A-Za-z]:\\\\?Users\\\\?[\w.\\\\-]+"),
]

DELIVERY_PHRASES = [
    (re.compile(r"\boutputs? directory\b", re.IGNORECASE), "Cowork-specific convention -- Claude Code has no 'outputs directory'."),
    (re.compile(r"\buploads? directory\b", re.IGNORECASE), "Cowork-specific convention for user-provided files -- doesn't exist in Claude Code."),
    (re.compile(r"\bconnected device\b", re.IGNORECASE), "Assumes the Cowork desktop-app device bridge is available."),
]

# --- Rule 6: env vars ---------------------------------------------------------------------------

# Python and JS/TS read patterns. Order matters only in that each must capture the var name in
# group(1); os.environ.get() and os.getenv() are at least as common as the bracket/attribute
# forms below and were missed entirely before this list grew to cover them.
ENV_VAR_PATTERNS = [
    re.compile(r"os\.environ\[[\'\"]([A-Z_][A-Z0-9_]*)[\'\"]\]"),
    re.compile(r"os\.environ\.get\([\'\"]([A-Z_][A-Z0-9_]*)[\'\"]"),
    re.compile(r"os\.getenv\([\'\"]([A-Z_][A-Z0-9_]*)[\'\"]"),
    re.compile(r"process\.env\.([A-Z_][A-Z0-9_]*)"),
    re.compile(r"process\.env\[[\'\"]([A-Z_][A-Z0-9_]*)[\'\"]\]"),
]

# Bash/shell var reads ($VAR or ${VAR}), scanned separately since shell scripts don't match the
# Python/JS patterns above at all -- a bundled .sh script reading a secret was previously
# invisible to this scanner regardless of confidence level.
SHELL_ENV_VAR_PATTERN = re.compile(r"\$\{?([A-Z_][A-Z0-9_]*)\}?")

PLATFORM_PROVIDED_ENV = {
    "CLAUDE_PROJECT_DIR", "CLAUDE_SESSION_ID", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA",
    "CLAUDE_SKILL_DIR",
    # common shell/OS-provided vars -- flagging these in a bundled .sh script would be noise,
    # not a real portability finding, since every shell environment sets them.
    "PATH", "HOME", "USER", "PWD", "OLDPWD", "SHELL", "LANG", "LC_ALL", "TERM", "TMPDIR", "TMP",
    "TEMP", "HOSTNAME", "SHLVL", "IFS", "PS1", "PS2",
}

# --- Rule 9: hardcoded MCP server/tool name references ----------------------------------------

# Cowork's own bridge namespaces are stable, platform-level prefixes (already covered above in
# COWORK_ONLY_TOOLS) -- not a user-configured MCP connector, so they're exempt from this rule.
# Everything else matching mcp__<server>__<tool> is a *third-party* MCP connector, and that
# server-name segment is assigned however the connector happened to get configured -- it is not
# part of any spec, so a different user, org, or session can register the identical integration
# under a different name entirely.
STABLE_MCP_PREFIXES = ("mcp__remote-devices__", "mcp__claude-code-remote__")

MCP_TOOL_REF_PATTERN = re.compile(r"\bmcp__([\w-]+)__([\w-]+)\b")

# --- Rule 10: credential-loading assumptions (.env / dotenv) -----------------------------------

# python-dotenv is a real, common pattern for local credential loading, but it's an extra pip
# dependency -- not guaranteed present in Cowork's sandbox (a fixed preinstalled set) or on a
# Claude Code user's machine (whatever they happened to already `pip install`). A script that
# imports it and has no fallback will ImportError before it even gets to the "missing credential"
# error it was trying to produce.
DOTENV_PACKAGE_PATTERN = re.compile(r"\b(?:from\s+dotenv\s+import\s+\w+|import\s+dotenv|load_dotenv\s*\()")

# --- Tableau domain rule pack (references/tableau-rules.md) ------------------------------------
#
# Activates only when the target skill looks Tableau-related. This is layered on top of the
# general rules above, not a replacement -- a Tableau skill still gets rules 1-10 too. Only the
# mechanically-checkable slices of R1-R7 live here; R1's "is this actually deterministic" and
# R2's "is the stated preference order actually right" and R6/R7 require reading the skill's real
# logic, which is the calling skill's job during the walkthrough, not something regex can settle.

TABLEAU_KEYWORD_PATTERN = re.compile(r"\btableau\b", re.IGNORECASE)
TABLEAU_TECH_TERMS = re.compile(r"\bVizQL\b|\bVDS\b|datasource_luid|query-?datasource", re.IGNORECASE)
TABLEAU_ENV_VAR_PATTERN = re.compile(r"\bTABLEAU_[A-Z_]+\b")
TABLEAU_STANDARD_CREDENTIAL_VARS = {"TABLEAU_PAT_NAME", "TABLEAU_PAT_VALUE"}
TABLEAU_STANDARD_OPTIONAL_VARS = {"TABLEAU_SERVER", "TABLEAU_SITE"}

TABLEAU_MCP_LITERAL_PATTERN = re.compile(r"\bmcp__[\w-]*[Tt][Aa][Bb][Ll][Ee][Aa][Uu][\w-]*__[\w-]+\b")
PREFIX_TOLERANT_LANGUAGE_PATTERN = re.compile(
    r"pattern[- ]match|ToolSearch|prefix[- ]tolerant|never assume a specific|whichever (server|connector) resolves",
    re.IGNORECASE,
)

TABLEAU_SECRET_LOG_PATTERN = re.compile(
    r"\b(?:print|log(?:ger)?\.\w+)\s*\([^)]*\b(?:PAT_VALUE|pat_value|TABLEAU_TOKEN|tableau_token|token|secret)\b",
    re.IGNORECASE,
)

MCPORTER_PATTERN = re.compile(r"\bmcporter\b")
VENV_PATTERN = re.compile(r"\.venv\b")

AUTHORIZATION_FLOW_PATTERN = re.compile(r"/authorize\b|\belicitation\b|\bOAuth\b|\bauthorization flow\b", re.IGNORECASE)
NO_AUTH_INSTRUCTION_PATTERN = re.compile(r"never\s+(?:trigger|initiate|start)[^.]*authoriz", re.IGNORECASE)

# R1/R2/R6/R7 aren't fully mechanically checkable (see tableau-rules.md for why -- they require
# reading the skill's actual transport-selection logic, stated preference order, and per-transport
# query-building code, not just its text patterns). This checklist is surfaced to the calling
# skill alongside any mechanical findings so those questions don't get silently skipped just
# because the scanner can't answer them itself.
TABLEAU_MANUAL_REVIEW_CHECKLIST = [
    {
        "rule": "R1",
        "question": "Read the actual transport-selection logic (not just the docs): is which connection "
                     "method to use decided by credential PRESENCE, checked before any connection attempt "
                     "-- never by trying a connection and reacting to whether it succeeds? Also check "
                     "surrounding prose for 'try this first, and if it fails, try that' framing that reads "
                     "as trial-and-error even when the underlying code is correctly gated.",
    },
    {
        "rule": "R2",
        "question": "Read the skill's actual stated preference order for Cowork connectors: does it "
                     "prefer the desktop-bridge Tableau extension first, with the cloud-side connector as "
                     "fallback -- including mid-task failover if the bridge drops? A skill can avoid "
                     "hardcoding a literal tool name while still stating the wrong order, or omitting "
                     "failover entirely -- neither is caught by the mechanical hardcoded-literal check.",
    },
    {
        "rule": "R6",
        "question": "Read the query-building code across every transport path this skill supports: is "
                     "the same VizQL/query JSON sent verbatim on each, with only transport-unsupported "
                     "parameters (e.g. limit on direct VDS calls) handled client-side rather than by "
                     "forking the query format per transport?",
    },
    {
        "rule": "R7",
        "question": "Read the skill's documented exit codes/status fields and its actual output artifact "
                     "(report, pack, log): does it distinguish 'Tableau unreachable / not authenticated' "
                     "from partial data and from skill bugs, and does the output actually record which "
                     "connection path produced the run -- not just that it could?",
    },
]


def is_tableau_related(text_blobs):
    """text_blobs: iterable of strings (SKILL.md body, reference docs, bundled script contents).
    True if the skill looks Tableau-related, per the heuristic documented in tableau-rules.md:
    "tableau" appears more than once, or Tableau-specific tech terms/env vars show up at all
    (those are specific enough that even a single hit is a strong signal, unlike the bare word
    "tableau" which could plausibly appear once in an unrelated sentence)."""
    combined = "\n".join(text_blobs)
    if len(TABLEAU_KEYWORD_PATTERN.findall(combined)) >= 2:
        return True
    if TABLEAU_TECH_TERMS.search(combined):
        return True
    if TABLEAU_ENV_VAR_PATTERN.search(combined):
        return True
    return False


def scan_tableau_rules(skill_dir: Path, skill_md_path: Path, skill_md_text: str):
    """Domain rule pack for Tableau-connecting skills (references/tableau-rules.md), layered on
    top of every general rule above. Only activates when is_tableau_related() says yes. Only the
    mechanically-checkable slices of R2/R3/R4/R5 are implemented here as real findings; R1, the
    non-mechanical half of R2, R6, and R7 come back as a `manual_checklist` instead, since they
    require reading the skill's actual logic, not matching a pattern. Returns
    {"applicable": bool, "findings": [...], "manual_checklist": [...]}."""
    blobs = [(str(skill_md_path), skill_md_text)]

    references_dir = skill_dir / "references"
    if references_dir.exists():
        for doc_path in sorted(references_dir.rglob("*.md")):
            if doc_path.is_file():
                blobs.append((str(doc_path), doc_path.read_text(encoding="utf-8", errors="replace")))

    scripts_dir = skill_dir / "scripts"
    if scripts_dir.exists():
        for script_file in sorted(scripts_dir.rglob("*")):
            if script_file.is_file() and script_file.suffix in (".py", ".sh", ".js", ".ts"):
                blobs.append((str(script_file), script_file.read_text(encoding="utf-8", errors="replace")))

    if not is_tableau_related(b[1] for b in blobs):
        return {"applicable": False, "findings": [], "manual_checklist": []}

    findings = []
    all_text = "\n".join(b[1] for b in blobs)

    # R2 -- a hardcoded Tableau MCP tool literal is only a problem if the skill has NO
    # prefix-tolerant/pattern-matching language anywhere telling Claude how to resolve the tool
    # when the actual session's prefix differs. One example literal next to that language is fine
    # (that's how rule 9's general check already treats stable prefixes); a skill whose only
    # instruction IS the bare literal is the failure mode R2 describes.
    if not PREFIX_TOLERANT_LANGUAGE_PATTERN.search(all_text):
        for file_path, text in blobs:
            for m in TABLEAU_MCP_LITERAL_PATTERN.finditer(text):
                findings.append({
                    "id": f"tableau-r2-hardcoded-{Path(file_path).name}-{m.start()}",
                    "confidence": "heuristic",
                    "category": "tableau-r2",
                    "file": file_path,
                    "line": line_number_for(text, m.start()),
                    "snippet": text[max(0, m.start() - 20):m.start() + len(m.group(0)) + 10].replace("\n", " ").strip(),
                    "risk": f"Hardcodes the Tableau MCP tool name \"{m.group(0)}\" with no prefix-tolerant/pattern-matching language found anywhere in the skill (org rule R2). Exact MCP prefixes vary by session -- a skill whose only instruction is this bare literal fails to find the tool the moment the prefix differs.",
                    "fix": "Describe the capability (e.g. \"query the Tableau datasource\") and tell Claude to pattern-match on the \"Tableau\" server name across whatever prefix resolves this session -- via ToolSearch in Cowork or the already-loaded MCP tool list in Claude Code -- rather than assuming this exact literal. See tableau-rules.md R2.",
                })

    # R4 -- non-standard credential env-var contract. If the skill reads some TABLEAU_* var(s)
    # but not the standard PAT_NAME/PAT_VALUE pair, one setup no longer serves every Tableau
    # skill the way R4 intends.
    tableau_vars_seen = set(TABLEAU_ENV_VAR_PATTERN.findall(all_text))
    if tableau_vars_seen and not TABLEAU_STANDARD_CREDENTIAL_VARS <= tableau_vars_seen:
        missing = sorted(TABLEAU_STANDARD_CREDENTIAL_VARS - tableau_vars_seen)
        nonstandard = sorted(tableau_vars_seen - TABLEAU_STANDARD_CREDENTIAL_VARS - TABLEAU_STANDARD_OPTIONAL_VARS)
        findings.append({
            "id": "tableau-r4-nonstandard-env-contract",
            "confidence": "heuristic",
            "category": "tableau-r4",
            "file": str(skill_md_path),
            "line": 1,
            "snippet": f"TABLEAU_* vars found across the skill: {sorted(tableau_vars_seen)}",
            "risk": (
                f"Org rule R4 specifies a standard credential contract (TABLEAU_PAT_NAME + TABLEAU_PAT_VALUE "
                f"required; TABLEAU_SERVER/TABLEAU_SITE optional) so one setup serves every Tableau skill. "
                f"This skill is missing {missing}"
                + (f" and uses non-standard var(s) {nonstandard}" if nonstandard else "")
                + " -- a user who already set up credentials for another Tableau skill would need to set them up again, differently, for this one."
            ),
            "fix": "Rename to the standard contract (TABLEAU_PAT_NAME, TABLEAU_PAT_VALUE required; TABLEAU_SERVER, TABLEAU_SITE optional with sane defaults). See tableau-rules.md R4.",
        })

    # R4 -- secrets printed/logged
    for file_path, text in blobs:
        for m in TABLEAU_SECRET_LOG_PATTERN.finditer(text):
            findings.append({
                "id": f"tableau-r4-secret-logged-{Path(file_path).name}-{m.start()}",
                "confidence": "heuristic",
                "category": "tableau-r4",
                "file": file_path,
                "line": line_number_for(text, m.start()),
                "snippet": text[max(0, m.start() - 10):m.start() + len(m.group(0)) + 20].replace("\n", " ").strip(),
                "risk": "Looks like a token/secret value is being printed or logged (org rule R4: secrets are never printed, logged, echoed, or committed). Even a debug print can end up captured in a session transcript or CI log.",
                "fix": "Remove the token/secret value from the print/log call -- log that a credential was used, or which var supplied it, never the value itself.",
            })

    # R5 -- mcporter / .venv references. Overlaps generically with compat-rules.md rule 5, but
    # flagging it under the Tableau umbrella too keeps this checklist self-contained during the
    # walkthrough rather than requiring a cross-reference back to the general findings.
    for file_path, text in blobs:
        for pat, label in ((MCPORTER_PATTERN, "mcporter"), (VENV_PATTERN, ".venv")):
            for m in pat.finditer(text):
                findings.append({
                    "id": f"tableau-r5-{label}-{Path(file_path).name}-{m.start()}",
                    "confidence": "heuristic",
                    "category": "tableau-r5",
                    "file": file_path,
                    "line": line_number_for(text, m.start()),
                    "snippet": text[max(0, m.start() - 20):m.start() + 20].replace("\n", " ").strip(),
                    "risk": f"References \"{label}\" (org rule R5: no host-specific CLIs/configs, no virtualenv assumptions -- stdlib-only on a stock Python 3.9+). This assumes a specific machine setup that won't exist on every Claude Code user's machine or in Cowork's sandbox.",
                    "fix": "Remove the assumption -- run on a stock Python 3.9+ interpreter with stdlib only, or explicitly declare and check any real dependency instead of assuming a particular local tool or virtualenv is present.",
                })

    # R3 -- authorization-flow language anywhere in the skill, with no "never trigger
    # authorization" guardrail found anywhere in the skill either. Checked skill-wide rather than
    # per-file, since the guardrail belongs wherever the model reads instructions mid-run, which
    # may not be the same file that happens to mention the auth flow.
    if not NO_AUTH_INSTRUCTION_PATTERN.search(all_text):
        for file_path, text in blobs:
            for m in AUTHORIZATION_FLOW_PATTERN.finditer(text):
                findings.append({
                    "id": f"tableau-r3-auth-flow-{Path(file_path).name}-{m.start()}",
                    "confidence": "heuristic",
                    "category": "tableau-r3",
                    "file": file_path,
                    "line": line_number_for(text, m.start()),
                    "snippet": text[max(0, m.start() - 20):m.start() + len(m.group(0)) + 20].replace("\n", " ").strip(),
                    "risk": "Mentions an authorization/elicitation flow, but no \"never trigger authorization\" guardrail was found anywhere in the skill (org rule R3). The configured connectors are already authorized -- a tool that demands new auth is by definition the wrong tool, and that needs to be stated explicitly so the model doesn't follow an auth link mid-run.",
                    "fix": "Add an explicit instruction -- in both the operating instructions and any machine-readable artifact the model consumes mid-run (plans, query manifests, runbooks) -- that the skill must never initiate authorization to any MCP server/gateway/service. A tool demanding new auth is the wrong tool. See tableau-rules.md R3.",
                })

    return {"applicable": True, "findings": findings, "manual_checklist": TABLEAU_MANUAL_REVIEW_CHECKLIST}


def parse_frontmatter(text):
    """Minimal, dependency-free frontmatter parser. Good enough for flat key: value pairs and
    folded multi-line scalars (the `key: >` / plain-continuation style used throughout this
    skill's own SKILL.md); doesn't need to fully understand YAML since callers only need the set
    of top-level key names and, for a few fields like `compatibility`, the full text content --
    not list/nested-map structure."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fm_text = parts[1]
    body = parts[2]
    fields = {}
    current_key = None
    for raw_line in fm_text.splitlines():
        if not raw_line.strip():
            continue
        if raw_line[:1] not in (" ", "\t") and ":" in raw_line:
            key, _, val = raw_line.partition(":")
            key = key.strip()
            if key:
                fields[key] = val.strip()
                current_key = key
        elif current_key is not None:
            # Indented continuation line -- fold it onto the current key's value with a space,
            # same as YAML's folded-scalar (`>`) style. This only matters for reading a field's
            # *text* (e.g. checking whether `compatibility` mentions something); the set of
            # top-level keys detected above is unaffected either way.
            fields[current_key] = (fields[current_key] + " " + raw_line.strip()).strip()
    return fields, body


def line_number_for(text, index):
    return text.count("\n", 0, index) + 1


def scan_skill_md(path: Path):
    findings = []
    text = path.read_text(encoding="utf-8", errors="replace")
    fields, body = parse_frontmatter(text)

    for key in fields:
        if key not in SPEC_SAFE_FIELDS:
            explanation = CLAUDE_CODE_ONLY_FIELDS.get(
                key, "Not part of the 6-field portable spec (name, description, license, compatibility, metadata, allowed-tools)."
            )
            findings.append({
                "id": f"frontmatter-{key}",
                "confidence": "confirmed",
                "category": "frontmatter",
                "file": str(path),
                "line": 1,
                "snippet": f"{key}: {fields[key]}",
                "risk": f"`{key}` is a Claude Code-only frontmatter extension. Packaging or uploading this skill for Cowork/claude.ai will hard-error on it.",
                "fix": explanation,
            })

    # body offset for line-number math (frontmatter block precedes body)
    body_offset = text.index(body) if body in text else 0

    for m in SHELL_INJECTION_INLINE.finditer(body):
        findings.append({
            "id": f"shell-inject-{m.start()}",
            "confidence": "confirmed",
            "category": "dynamic-injection",
            "file": str(path),
            "line": line_number_for(text, body_offset + m.start()),
            "snippet": m.group(0).strip(),
            "risk": "Inline shell injection only runs in Claude Code. Outside it, this arrives as literal text (or a disabled-by-policy placeholder) and any context it was supposed to inject silently disappears.",
            "fix": "Replace with an explicit instruction telling Claude to run the command itself via Bash as a normal step, then use that output.",
        })

    for m in SHELL_INJECTION_FENCED.finditer(body):
        findings.append({
            "id": f"shell-inject-fenced-{m.start()}",
            "confidence": "confirmed",
            "category": "dynamic-injection",
            "file": str(path),
            "line": line_number_for(text, body_offset + m.start()),
            "snippet": m.group(0)[:120].strip(),
            "risk": "Fenced ```! shell injection only runs in Claude Code. Outside it, the block reaches Claude as literal text.",
            "fix": "Replace with an explicit instruction telling Claude to run the commands itself via Bash, then use that output.",
        })

    for m in CLAUDE_VARS.finditer(body):
        var = m.group(1)
        plugin_related = var in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA")
        findings.append({
            "id": f"claude-var-{var}-{m.start()}",
            "confidence": "confirmed" if not plugin_related else "heuristic",
            "category": "dynamic-substitution",
            "file": str(path),
            "line": line_number_for(text, body_offset + m.start()),
            "snippet": m.group(0),
            "risk": (
                f"${{{var}}} is substituted by Claude Code before the model sees it. Outside Claude Code it arrives as literal text."
                + (" It IS part of the shared plugin schema, so this is lower-risk if this skill will only ever ship as part of a .plugin install, not as a standalone skill upload." if plugin_related else "")
            ),
            "fix": "For script paths, tell Claude to locate and run the script relative to the skill's own directory instead of building the path with a substitution variable." if not plugin_related else "Confirm this skill is distributed as part of a plugin, not as a standalone skill upload -- if standalone, treat this the same as the non-plugin case.",
        })

    for name in COWORK_ONLY_TOOLS:
        for m in re.finditer(re.escape(name), body):
            findings.append({
                "id": f"cowork-tool-{name}-{m.start()}",
                "confidence": "heuristic",
                "category": "tool-reference",
                "file": str(path),
                "line": line_number_for(text, body_offset + m.start()),
                "snippet": body[max(0, m.start() - 30):m.start() + len(name) + 10].replace("\n", " ").strip(),
                "risk": f"'{name}' is a Cowork-side tool with no confirmed Claude Code equivalent. A step relying on it will silently fail to do what's intended when this skill runs under Claude Code.",
                "fix": "Rewrite as a check-and-fallback: name the preferred tool, then give an explicit fallback for when it's unavailable (see the create-cowork-plugin pattern for claude plugin validate).",
            })

    for name in CLAUDE_CODE_ONLY_REFERENCES:
        for m in re.finditer(re.escape(name), body):
            findings.append({
                "id": f"claudecode-ref-{name}-{m.start()}",
                "confidence": "heuristic",
                "category": "tool-reference",
                "file": str(path),
                "line": line_number_for(text, body_offset + m.start()),
                "snippet": body[max(0, m.start() - 30):m.start() + len(name) + 10].replace("\n", " ").strip(),
                "risk": f"'{name}' is a Claude Code-side convention/tool with no confirmed Cowork equivalent.",
                "fix": "Rewrite as a check-and-fallback, or describe the goal generically and let Claude pick the right mechanism for whichever surface it's running on.",
            })

    for pattern, note in SUBAGENT_NAME_TRAP:
        for m in re.finditer(pattern, body):
            findings.append({
                "id": f"subagent-name-trap-{m.start()}",
                "confidence": "heuristic",
                "category": "tool-reference",
                "file": str(path),
                "line": line_number_for(text, body_offset + m.start()),
                "snippet": body[max(0, m.start() - 30):m.start() + 30].replace("\n", " ").strip(),
                "risk": note,
                "fix": "Use a generic phrase like 'spawn a subagent to do X' rather than naming the tool, since the name differs across products.",
            })

    for pat in ABS_PATH_PATTERNS:
        for m in pat.finditer(body):
            findings.append({
                "id": f"abs-path-{m.start()}",
                "confidence": "heuristic",
                "category": "filesystem",
                "file": str(path),
                "line": line_number_for(text, body_offset + m.start()),
                "snippet": m.group(0),
                "risk": "Hardcoded absolute path. Fragile in Claude Code (varies per user) and almost certainly wrong in Cowork's cloud sandbox (different filesystem layout entirely).",
                "fix": "Use a relative reference, or an instruction that asks the environment for the right location rather than assuming one.",
            })

    for pat, note in DELIVERY_PHRASES:
        for m in pat.finditer(body):
            findings.append({
                "id": f"delivery-phrase-{m.start()}",
                "confidence": "heuristic",
                "category": "filesystem",
                "file": str(path),
                "line": line_number_for(text, body_offset + m.start()),
                "snippet": body[max(0, m.start() - 20):m.start() + 40].replace("\n", " ").strip(),
                "risk": note,
                "fix": "Describe the outcome ('make sure the user ends up with file X') and let the body branch on what's actually available rather than assuming one delivery mechanism.",
            })

    for m in MCP_TOOL_REF_PATTERN.finditer(body):
        prefix = f"mcp__{m.group(1)}__"
        if prefix in STABLE_MCP_PREFIXES:
            continue
        findings.append({
            "id": f"mcp-hardcoded-{m.group(1)}-{m.group(2)}-{m.start()}",
            "confidence": "heuristic",
            "category": "mcp-reference",
            "file": str(path),
            "line": line_number_for(text, body_offset + m.start()),
            "snippet": body[max(0, m.start() - 20):m.start() + len(m.group(0)) + 10].replace("\n", " ").strip(),
            "risk": f"Hardcodes the MCP server prefix \"{prefix}\" for tool \"{m.group(2)}\". That server-name segment isn't part of any spec -- a different user, org, or session can register the identical connector under a different prefix, and in Cowork many MCP tools are deferred until a ToolSearch call resolves them. A hardcoded prefix that doesn't match silently fails to find the tool.",
            "fix": "Describe the capability you need (e.g. \"query the Tableau datasource\") and tell Claude to resolve the actual tool -- via ToolSearch in Cowork, or its already-loaded MCP tool list in Claude Code -- rather than assuming this exact name. See compat-rules.md rule 9.",
        })

    for m in DOTENV_PACKAGE_PATTERN.finditer(body):
        findings.append({
            "id": f"dotenv-dependency-{m.start()}",
            "confidence": "heuristic",
            "category": "environment",
            "file": str(path),
            "line": line_number_for(text, body_offset + m.start()),
            "snippet": m.group(0),
            "risk": "Depends on the python-dotenv package, which isn't guaranteed installed in Cowork's sandbox or on a Claude Code user's machine -- this will ImportError before it even gets to the credential check it was trying to do.",
            "fix": "Read a .env-style file with a small stdlib-only parser instead (a few lines: split on the first \"=\", strip quotes/whitespace, skip blank/# lines) so there's no extra dependency to install. See compat-rules.md rule 10 for the pattern.",
        })

    # Rule 6/7 combined: an env var read only stays a "go fix this" finding if the skill hasn't
    # already documented it. Rule 7's actual fix for an env var IS the compatibility field -- so
    # once that field mentions the variable by name, the finding has already been acted on and
    # re-flagging it is just noise. Check case-insensitively since frontmatter authors won't
    # necessarily match the variable's exact casing in prose.
    compat_text = fields.get("compatibility", "")
    for pat in ENV_VAR_PATTERNS:
        for m in pat.finditer(text):
            var = m.group(1)
            if var in PLATFORM_PROVIDED_ENV:
                continue
            if var.lower() in compat_text.lower():
                continue
            findings.append({
                "id": f"env-var-{var}-{m.start()}",
                "confidence": "heuristic",
                "category": "environment",
                "file": str(path),
                "line": line_number_for(text, m.start()),
                "snippet": m.group(0),
                "risk": f"Reads env var {var}, which isn't obviously platform-provided. Cowork's sandbox starts clean aside from what the platform sets.",
                "fix": "Document the required env var explicitly (README or the `compatibility` field), and give it an actual working path to get set on both products -- see compat-rules.md rule 10 for the stdlib-only loader pattern that checks os.environ first and falls back to a discoverable .env-style file rather than just erroring.",
            })

    return findings


def scan_reference_docs(skill_dir: Path, compat_text: str):
    """Apply the same prose-level checks (tool references, filesystem assumptions, hardcoded MCP
    prefixes, env-var reads, dotenv dependency) to markdown files under references/ -- these are
    exactly where setup/runbook instructions tend to live (the actual MCP-discovery and
    credential-loading guidance a skill gives a user is often in a references/*.md, not
    SKILL.md itself), and a hardcoded assumption there is just as real a portability break as one
    in SKILL.md. Unlike SKILL.md, these files aren't preprocessed by Claude Code before the model
    reads them, so frontmatter/shell-injection/${CLAUDE_*}-substitution rules don't apply here."""
    findings = []
    references_dir = skill_dir / "references"
    if not references_dir.exists():
        return findings

    for doc_path in sorted(references_dir.rglob("*.md")):
        if not doc_path.is_file():
            continue
        doc_text = doc_path.read_text(encoding="utf-8", errors="replace")

        for name in COWORK_ONLY_TOOLS:
            for m in re.finditer(re.escape(name), doc_text):
                findings.append({
                    "id": f"cowork-tool-{doc_path.name}-{name}-{m.start()}",
                    "confidence": "heuristic",
                    "category": "tool-reference",
                    "file": str(doc_path),
                    "line": line_number_for(doc_text, m.start()),
                    "snippet": doc_text[max(0, m.start() - 30):m.start() + len(name) + 10].replace("\n", " ").strip(),
                    "risk": f"'{name}' is a Cowork-side tool with no confirmed Claude Code equivalent. A step relying on it will silently fail to do what's intended when this skill runs under Claude Code.",
                    "fix": "Rewrite as a check-and-fallback: name the preferred tool, then give an explicit fallback for when it's unavailable.",
                })

        for name in CLAUDE_CODE_ONLY_REFERENCES:
            for m in re.finditer(re.escape(name), doc_text):
                findings.append({
                    "id": f"claudecode-ref-{doc_path.name}-{name}-{m.start()}",
                    "confidence": "heuristic",
                    "category": "tool-reference",
                    "file": str(doc_path),
                    "line": line_number_for(doc_text, m.start()),
                    "snippet": doc_text[max(0, m.start() - 30):m.start() + len(name) + 10].replace("\n", " ").strip(),
                    "risk": f"'{name}' is a Claude Code-side convention/tool with no confirmed Cowork equivalent.",
                    "fix": "Rewrite as a check-and-fallback, or describe the goal generically and let Claude pick the right mechanism for whichever surface it's running on.",
                })

        for pattern, note in SUBAGENT_NAME_TRAP:
            for m in re.finditer(pattern, doc_text):
                findings.append({
                    "id": f"subagent-name-trap-{doc_path.name}-{m.start()}",
                    "confidence": "heuristic",
                    "category": "tool-reference",
                    "file": str(doc_path),
                    "line": line_number_for(doc_text, m.start()),
                    "snippet": doc_text[max(0, m.start() - 30):m.start() + 30].replace("\n", " ").strip(),
                    "risk": note,
                    "fix": "Use a generic phrase like 'spawn a subagent to do X' rather than naming the tool, since the name differs across products.",
                })

        for pat in ABS_PATH_PATTERNS:
            for m in pat.finditer(doc_text):
                findings.append({
                    "id": f"abs-path-{doc_path.name}-{m.start()}",
                    "confidence": "heuristic",
                    "category": "filesystem",
                    "file": str(doc_path),
                    "line": line_number_for(doc_text, m.start()),
                    "snippet": m.group(0),
                    "risk": "Hardcoded absolute path. Fragile in Claude Code (varies per user) and almost certainly wrong in Cowork's cloud sandbox (different filesystem layout entirely).",
                    "fix": "Use a relative reference, or an instruction that asks the environment for the right location rather than assuming one.",
                })

        for pat, note in DELIVERY_PHRASES:
            for m in pat.finditer(doc_text):
                findings.append({
                    "id": f"delivery-phrase-{doc_path.name}-{m.start()}",
                    "confidence": "heuristic",
                    "category": "filesystem",
                    "file": str(doc_path),
                    "line": line_number_for(doc_text, m.start()),
                    "snippet": doc_text[max(0, m.start() - 20):m.start() + 40].replace("\n", " ").strip(),
                    "risk": note,
                    "fix": "Describe the outcome ('make sure the user ends up with file X') and let the body branch on what's actually available rather than assuming one delivery mechanism.",
                })

        for m in MCP_TOOL_REF_PATTERN.finditer(doc_text):
            prefix = f"mcp__{m.group(1)}__"
            if prefix in STABLE_MCP_PREFIXES:
                continue
            findings.append({
                "id": f"mcp-hardcoded-{doc_path.name}-{m.group(1)}-{m.group(2)}-{m.start()}",
                "confidence": "heuristic",
                "category": "mcp-reference",
                "file": str(doc_path),
                "line": line_number_for(doc_text, m.start()),
                "snippet": doc_text[max(0, m.start() - 20):m.start() + len(m.group(0)) + 10].replace("\n", " ").strip(),
                "risk": f"Hardcodes the MCP server prefix \"{prefix}\" for tool \"{m.group(2)}\". That server-name segment isn't part of any spec -- a different user, org, or session can register the identical connector under a different prefix, and in Cowork many MCP tools are deferred until a ToolSearch call resolves them. A hardcoded prefix that doesn't match silently fails to find the tool.",
                "fix": "Describe the capability you need and tell Claude to resolve the actual tool -- via ToolSearch in Cowork, or its already-loaded MCP tool list in Claude Code -- rather than assuming this exact name. See compat-rules.md rule 9.",
            })

        for m in DOTENV_PACKAGE_PATTERN.finditer(doc_text):
            findings.append({
                "id": f"dotenv-dependency-{doc_path.name}-{m.start()}",
                "confidence": "heuristic",
                "category": "environment",
                "file": str(doc_path),
                "line": line_number_for(doc_text, m.start()),
                "snippet": m.group(0),
                "risk": "Recommends/depends on the python-dotenv package, which isn't guaranteed installed in Cowork's sandbox or on a Claude Code user's machine.",
                "fix": "Recommend a small stdlib-only .env parser instead so there's no extra dependency to install. See compat-rules.md rule 10 for the pattern.",
            })

        for pat in ENV_VAR_PATTERNS:
            for m in pat.finditer(doc_text):
                var = m.group(1)
                if var in PLATFORM_PROVIDED_ENV or var.startswith("CLAUDE_"):
                    continue
                if var.lower() in compat_text.lower():
                    continue
                findings.append({
                    "id": f"env-var-{doc_path.name}-{var}-{m.start()}",
                    "confidence": "heuristic",
                    "category": "environment",
                    "file": str(doc_path),
                    "line": line_number_for(doc_text, m.start()),
                    "snippet": m.group(0),
                    "risk": f"Reads env var {var}, which isn't obviously platform-provided. Cowork's sandbox starts clean aside from what the platform sets.",
                    "fix": "Document the required env var explicitly (README or the `compatibility` field), and give it an actual working path to get set on both products -- see compat-rules.md rule 10.",
                })

    return findings


def scan_plugin_context(skill_dir: Path):
    """Look for a surrounding plugin and flag plugin-level things worth a second look."""
    findings = []
    # walk upward looking for .claude-plugin/plugin.json
    current = skill_dir
    plugin_root = None
    for _ in range(4):
        if (current / ".claude-plugin" / "plugin.json").exists():
            plugin_root = current
            break
        if current.parent == current:
            break
        current = current.parent
    if not plugin_root:
        return findings

    hooks_json = plugin_root / "hooks" / "hooks.json"
    if hooks_json.exists():
        findings.append({
            "id": "plugin-hooks-present",
            "confidence": "heuristic",
            "category": "plugin",
            "file": str(hooks_json),
            "line": 1,
            "snippet": "hooks/hooks.json present",
            "risk": "Hooks are valid in both products' shared plugin schema, but documented as 'rarely used in Cowork' -- worth verifying the hook actually fires as expected there.",
            "fix": "Test this hook in an actual Cowork session before relying on it; don't assume parity with Claude Code.",
        })

    agents_dir = plugin_root / "agents"
    if agents_dir.exists() and any(agents_dir.iterdir()):
        findings.append({
            "id": "plugin-agents-present",
            "confidence": "heuristic",
            "category": "plugin",
            "file": str(agents_dir),
            "line": 1,
            "snippet": "agents/ present",
            "risk": "Custom subagents are valid in both products' shared schema, but documented as 'uncommonly used in Cowork' -- worth verifying invocation actually works there.",
            "fix": "Test subagent invocation in an actual Cowork session before relying on it.",
        })

    for cfg_name in ("hooks/hooks.json", ".mcp.json"):
        cfg_path = plugin_root / cfg_name
        if cfg_path.exists():
            cfg_text = cfg_path.read_text(encoding="utf-8", errors="replace")
            for pat in ABS_PATH_PATTERNS:
                for m in pat.finditer(cfg_text):
                    findings.append({
                        "id": f"plugin-abs-path-{cfg_name}-{m.start()}",
                        "confidence": "confirmed",
                        "category": "plugin",
                        "file": str(cfg_path),
                        "line": line_number_for(cfg_text, m.start()),
                        "snippet": m.group(0),
                        "risk": f"Hardcoded absolute path in {cfg_name}. This should use ${{CLAUDE_PLUGIN_ROOT}} instead -- that substitution IS safe across both products since it's part of the shared plugin schema.",
                        "fix": "Replace the hardcoded path with ${CLAUDE_PLUGIN_ROOT}/<relative path>.",
                    })

    return findings


# --- Execution verification: actually run what can safely be run --------------------------
#
# Everything above is text pattern matching -- inferring risk from what a file *says*, never
# confirming what actually happens when it runs. That's necessary (most cross-compat breaks are
# about a platform not being present to run against), but it means a "heuristic" finding is
# always a guess, never a reproduced fact. This section closes part of that gap: it actually
# executes the two things that are both safe to run unconditionally and genuinely diagnostic --
# real syntax parsing via each language's own checker (no side effects, nothing runs), and a
# real `import <module>` attempt for third-party Python dependencies (standard, low-risk -- it's
# what `pip check`-style tooling does, and it's bounded by a timeout in case a module does
# something unexpected at import time). It deliberately does NOT run a script's actual business
# logic -- that could hit live APIs, need real credentials, or have side effects, and deciding
# to do that is a per-skill judgment call for the calling skill to make explicitly with the
# user, not something this scanner does silently by default.

STDLIB_MODULE_NAMES = getattr(sys, "stdlib_module_names", None) or {
    "os", "sys", "re", "json", "argparse", "pathlib", "subprocess", "typing", "collections",
    "itertools", "functools", "datetime", "time", "math", "random", "logging", "shutil",
    "tempfile", "io", "csv", "sqlite3", "urllib", "http", "socket", "threading", "asyncio",
    "dataclasses", "enum", "abc", "copy", "hashlib", "hmac", "base64", "struct", "textwrap",
    "string", "unittest", "traceback", "warnings", "inspect", "importlib", "pickle", "queue",
    "xml", "html", "email", "zipfile", "tarfile", "gzip", "configparser", "platform",
}

SYNTAX_CHECK_TIMEOUT = 10
IMPORT_CHECK_TIMEOUT = 8


def check_python_syntax(script_file: Path):
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "py_compile", str(script_file)],
            capture_output=True, text=True, timeout=SYNTAX_CHECK_TIMEOUT,
        )
        return proc.returncode == 0, (proc.stderr.strip() or proc.stdout.strip())
    except subprocess.TimeoutExpired:
        return False, "py_compile timed out"
    except Exception as e:  # pragma: no cover -- defensive, e.g. interpreter missing
        return False, str(e)


def check_shell_syntax(script_file: Path):
    bash = shutil.which("bash")
    if not bash:
        return None, "bash not available in this environment to verify syntax"
    try:
        proc = subprocess.run(
            [bash, "-n", str(script_file)],
            capture_output=True, text=True, timeout=SYNTAX_CHECK_TIMEOUT,
        )
        return proc.returncode == 0, (proc.stderr.strip() or proc.stdout.strip())
    except subprocess.TimeoutExpired:
        return False, "bash -n timed out"
    except Exception as e:  # pragma: no cover
        return False, str(e)


def check_js_syntax(script_file: Path):
    node = shutil.which("node")
    if not node:
        return None, "node not available in this environment to verify syntax"
    try:
        proc = subprocess.run(
            [node, "--check", str(script_file)],
            capture_output=True, text=True, timeout=SYNTAX_CHECK_TIMEOUT,
        )
        return proc.returncode == 0, (proc.stderr.strip() or proc.stdout.strip())
    except subprocess.TimeoutExpired:
        return False, "node --check timed out"
    except Exception as e:  # pragma: no cover
        return False, str(e)


class _ImportVisitor(ast.NodeVisitor):
    """Collects top-level import module names, split into `unguarded` (module scope, or inside
    any block that isn't a try's own body) and `guarded` (sitting inside a `try:` block's body,
    specifically). That split matters: `try: import optional_pkg \\n except ImportError: ...` is
    the standard, correct way to handle an optional dependency -- the author already accounted
    for it possibly being missing. Treating that the same as an unconditional top-level import
    would punish exactly the defensive pattern this tool should be encouraging elsewhere."""

    def __init__(self):
        self.unguarded = set()
        self.guarded = set()
        self._try_depth = 0

    def visit_Try(self, node):
        self._try_depth += 1
        for stmt in node.body:
            self.visit(stmt)
        self._try_depth -= 1
        for handler in node.handlers:
            self.visit(handler)
        for stmt in node.orelse:
            self.visit(stmt)
        for stmt in node.finalbody:
            self.visit(stmt)

    def visit_Import(self, node):
        target = self.guarded if self._try_depth > 0 else self.unguarded
        for alias in node.names:
            target.add(alias.name.split(".")[0])

    def visit_ImportFrom(self, node):
        if node.level and node.level > 0:
            return
        if node.module:
            target = self.guarded if self._try_depth > 0 else self.unguarded
            target.add(node.module.split(".")[0])


def collect_top_level_python_imports(script_text: str):
    """Real imports parsed with ast (what the interpreter would actually try to load), not a
    regex guess. Returns (unguarded, guarded) module-name sets; relative imports (from . import
    x) are excluded from both since they're unambiguously local, not a dependency worth probing."""
    try:
        tree = ast.parse(script_text)
    except SyntaxError:
        return set(), set()
    visitor = _ImportVisitor()
    visitor.visit(tree)
    # a name that shows up both guarded (in one try block) and unguarded (elsewhere) is treated
    # as unguarded -- if there's an unconditional import anywhere, the try/except elsewhere
    # doesn't actually protect the script from that path failing.
    return visitor.unguarded, visitor.guarded - visitor.unguarded


def verify_third_party_import(module_name: str):
    try:
        proc = subprocess.run(
            [sys.executable, "-c", f"import {module_name}"],
            capture_output=True, text=True, timeout=IMPORT_CHECK_TIMEOUT,
        )
        detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ""
        return proc.returncode == 0, detail
    except subprocess.TimeoutExpired:
        return False, "import timed out (module may perform blocking/network work at import time)"
    except Exception as e:  # pragma: no cover
        return False, str(e)


def run_execution_verification(skill_dir: Path):
    """Returns (verification_summary, dynamic_findings). `verification_summary` is always
    populated, including when there are zero scripts, so the calling skill can honestly report
    what was actually checked rather than staying silent about verification never happening.
    `dynamic_findings` contains only CONFIRMED, reproduced problems -- a real syntax error, a
    real failed import -- never a guess."""
    findings = []
    scripts_dir = skill_dir / "scripts"
    if not scripts_dir.exists():
        return {
            "scripts_checked": 0, "syntax_ok": 0, "syntax_failed": 0, "syntax_unverifiable": 0,
            "imports_checked": [], "imports_failed": [], "imports_guarded": [],
            "note": "no scripts/ directory -- nothing to execute-verify",
        }, findings

    script_files = sorted(p for p in scripts_dir.rglob("*") if p.is_file() and p.suffix in (".py", ".sh", ".js", ".ts"))
    local_stems = {p.stem for p in script_files}

    scripts_checked = syntax_ok = syntax_failed = syntax_unverifiable = 0
    imports_checked, imports_failed, imports_guarded = [], [], []

    for script_file in script_files:
        scripts_checked += 1
        script_text = script_file.read_text(encoding="utf-8", errors="replace")

        if script_file.suffix == ".py":
            ok, detail = check_python_syntax(script_file)
            checker = "python3 -m py_compile"
        elif script_file.suffix == ".sh":
            ok, detail = check_shell_syntax(script_file)
            checker = "bash -n"
        else:
            ok, detail = check_js_syntax(script_file)
            checker = "node --check"

        if ok is None:
            syntax_unverifiable += 1
        elif ok:
            syntax_ok += 1
        else:
            syntax_failed += 1
            findings.append({
                "id": f"script-syntax-error-{script_file.name}",
                "confidence": "confirmed",
                "category": "execution",
                "file": str(script_file),
                "line": 1,
                "snippet": detail[:200],
                "risk": f"Actually ran this file through `{checker}` and it does NOT parse. This isn't a portability risk -- it's a bug that will fail identically on both products.",
                "fix": "Fix the syntax error shown above before anything else; nothing about cross-compat matters if the script can't even parse.",
            })

        if script_file.suffix == ".py" and ok:
            unguarded, guarded = collect_top_level_python_imports(script_text)

            for module_name in sorted(guarded):
                if module_name in STDLIB_MODULE_NAMES or module_name in local_stems or module_name.startswith("_"):
                    continue
                # Still probe it -- worth knowing for the summary -- but a try/except-guarded
                # import is, by convention, the author already handling this exact failure mode.
                # No finding either way: punishing the defensive pattern would be a false
                # positive, and a plain pass isn't worth a finding.
                passed, _ = verify_third_party_import(module_name)
                imports_checked.append(module_name)
                imports_guarded.append(module_name)
                if not passed:
                    imports_failed.append(module_name)

            for module_name in sorted(unguarded):
                if module_name in STDLIB_MODULE_NAMES or module_name in local_stems or module_name.startswith("_"):
                    continue
                passed, detail = verify_third_party_import(module_name)
                imports_checked.append(module_name)
                if not passed:
                    imports_failed.append(module_name)
                    findings.append({
                        "id": f"script-import-unavailable-{script_file.name}-{module_name}",
                        "confidence": "confirmed",
                        "category": "execution",
                        "file": str(script_file),
                        "line": 1,
                        "snippet": f"import {module_name}",
                        "risk": f"Actually ran `python3 -c \"import {module_name}\"` in this scanning environment and it failed: {detail or 'ModuleNotFoundError'}. If it's not importable here, it's not a safe bet it's preinstalled in Cowork's sandbox or on every Claude Code user's machine either.",
                        "fix": f"Vendor a stdlib-only replacement, or explicitly document `{module_name}` as a required pip dependency the user must install -- and confirm that's actually realistic on both products before relying on it.",
                    })

    return {
        "scripts_checked": scripts_checked,
        "syntax_ok": syntax_ok,
        "syntax_failed": syntax_failed,
        "syntax_unverifiable": syntax_unverifiable,
        "imports_checked": imports_checked,
        "imports_failed": imports_failed,
        "imports_guarded": imports_guarded,
    }, findings


def find_skill_md(target: Path) -> Path:
    if target.is_file() and target.name == "SKILL.md":
        return target
    candidate = target / "SKILL.md"
    if candidate.exists():
        return candidate
    raise FileNotFoundError(f"No SKILL.md found at or under {target}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", help="Path to a skill directory or its SKILL.md")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON instead of a human-readable report")
    parser.add_argument("--no-execute", action="store_true", help="Skip the execution-verification pass (real syntax/import checks) and only run the static text scan")
    args = parser.parse_args()

    target = Path(args.target).expanduser().resolve()
    skill_md = find_skill_md(target)
    skill_dir = skill_md.parent

    findings = scan_skill_md(skill_md) + scan_plugin_context(skill_dir)

    # Re-parse frontmatter once here (cheap) so the bundled-script env-var pass below can apply
    # the same "already documented in `compatibility`" suppression that scan_skill_md() applies
    # to env vars read directly in the SKILL.md body -- a script's env var deserves the same
    # courtesy once it's been documented, not a permanent re-flag.
    skill_text = skill_md.read_text(encoding="utf-8", errors="replace")
    skill_fields, _ = parse_frontmatter(skill_text)
    compat_text = skill_fields.get("compatibility", "")

    # References often carry the actual setup/runbook instructions (MCP discovery steps,
    # credential wiring) rather than SKILL.md itself -- scan them with the same suppression.
    findings += scan_reference_docs(skill_dir, compat_text)

    # Domain rule pack: only produces anything when the skill looks Tableau-related. Its
    # mechanical findings get folded into the main findings list like everything else; the
    # non-mechanical checklist (R1/R2-order/R6/R7) is surfaced separately since it isn't a
    # finding at all -- it's a list of questions the calling skill still has to answer by reading
    # the skill's real logic.
    tableau_result = scan_tableau_rules(skill_dir, skill_md, skill_text)
    findings += tableau_result["findings"]

    # also scan any bundled scripts for shell-injection-style patterns is out of scope --
    # scripts are executed, not preprocessed, so the injection/substitution rules don't apply
    # to them. Bundled scripts are still worth an env-var pass though.
    scripts_dir = skill_dir / "scripts"
    if scripts_dir.exists():
        for script_file in scripts_dir.rglob("*"):
            if not script_file.is_file():
                continue
            # Shell scripts read env vars as $VAR / ${VAR} -- a different syntax entirely from
            # the Python/JS accessor patterns, so they need their own pattern, not a shared one.
            if script_file.suffix == ".sh":
                patterns = [SHELL_ENV_VAR_PATTERN]
            elif script_file.suffix in (".py", ".js", ".ts"):
                patterns = ENV_VAR_PATTERNS
            else:
                continue
            script_text = script_file.read_text(encoding="utf-8", errors="replace")
            for pat in patterns:
                for m in pat.finditer(script_text):
                    var = m.group(1)
                    if var in PLATFORM_PROVIDED_ENV or var.startswith("CLAUDE_"):
                        continue
                    if var.lower() in compat_text.lower():
                        continue
                    findings.append({
                        "id": f"script-env-var-{script_file.name}-{var}-{m.start()}",
                        "confidence": "heuristic",
                        "category": "environment",
                        "file": str(script_file),
                        "line": line_number_for(script_text, m.start()),
                        "snippet": m.group(0),
                        "risk": f"Bundled script reads env var {var}, which isn't obviously platform-provided.",
                        "fix": "Document the required env var explicitly (README or the `compatibility` field), and give it a working cross-platform loading path -- see compat-rules.md rule 10.",
                    })
            # NOTE: this loop is a sibling of `for pat in patterns:` above, not nested inside it --
            # it used to be indented one level too deep, which ran the abs-path scan once per
            # env-var pattern (5x duplicate findings on a .py file) instead of once per script.
            for pat in ABS_PATH_PATTERNS:
                for m in pat.finditer(script_text):
                    findings.append({
                        "id": f"script-abs-path-{script_file.name}-{m.start()}",
                        "confidence": "heuristic",
                        "category": "filesystem",
                        "file": str(script_file),
                        "line": line_number_for(script_text, m.start()),
                        "snippet": m.group(0),
                        "risk": "Hardcoded absolute path in a bundled script.",
                        "fix": "Use a path relative to the script's own location (e.g. via the script's __file__/dirname), not a hardcoded absolute path.",
                    })
            for m in MCP_TOOL_REF_PATTERN.finditer(script_text):
                prefix = f"mcp__{m.group(1)}__"
                if prefix in STABLE_MCP_PREFIXES:
                    continue
                findings.append({
                    "id": f"script-mcp-hardcoded-{script_file.name}-{m.group(1)}-{m.group(2)}-{m.start()}",
                    "confidence": "heuristic",
                    "category": "mcp-reference",
                    "file": str(script_file),
                    "line": line_number_for(script_text, m.start()),
                    "snippet": script_text[max(0, m.start() - 20):m.start() + len(m.group(0)) + 10].replace("\n", " ").strip(),
                    "risk": f"Hardcodes the MCP server prefix \"{prefix}\" for tool \"{m.group(2)}\". That server-name segment isn't part of any spec -- a different user, org, or session can register the identical connector under a different prefix.",
                    "fix": "Describe the capability needed and let Claude resolve the actual tool name at runtime rather than embedding this exact string. See compat-rules.md rule 9.",
                })
            for m in DOTENV_PACKAGE_PATTERN.finditer(script_text):
                findings.append({
                    "id": f"script-dotenv-dependency-{script_file.name}-{m.start()}",
                    "confidence": "heuristic",
                    "category": "environment",
                    "file": str(script_file),
                    "line": line_number_for(script_text, m.start()),
                    "snippet": m.group(0),
                    "risk": "Depends on the python-dotenv package, which isn't guaranteed installed in Cowork's sandbox or on a Claude Code user's machine -- this will ImportError before it even gets to the credential check it was trying to do.",
                    "fix": "Read a .env-style file with a small stdlib-only parser instead (split each line on the first \"=\", strip quotes/whitespace, skip blank/# lines). See compat-rules.md rule 10 for the pattern.",
                })

    if args.no_execute:
        verification = {
            "scripts_checked": 0, "syntax_ok": 0, "syntax_failed": 0, "syntax_unverifiable": 0,
            "imports_checked": [], "imports_failed": [], "imports_guarded": [],
            "note": "--no-execute passed -- execution verification was skipped; the findings below are text-pattern inference only, not reproduced.",
        }
    else:
        verification, exec_findings = run_execution_verification(skill_dir)
        findings += exec_findings
        # Real evidence beats a guess: if a script imports something we already flagged
        # heuristically (dotenv is the only one right now) and the import actually succeeded in
        # THIS environment, say so on the existing finding rather than leaving it as an
        # unqualified guess -- still not proof it's available on Cowork or the end user's
        # machine, but "confirmed importable here, unverified elsewhere" is a strictly more
        # honest statement than silence.
        if "dotenv" in verification["imports_checked"] and "dotenv" not in verification["imports_failed"]:
            for f in findings:
                if f["id"].startswith("dotenv-dependency") or f["id"].startswith("script-dotenv-dependency"):
                    f["risk"] += " (Actually checked: python-dotenv imports successfully in this scanning environment -- that does NOT confirm it's present in Cowork's sandbox or on the end user's machine, which is a different Python install entirely.)"

    result = {
        "skill_md": str(skill_md),
        "finding_count": len(findings),
        "confirmed_count": sum(1 for f in findings if f["confidence"] == "confirmed"),
        "heuristic_count": sum(1 for f in findings if f["confidence"] == "heuristic"),
        "findings": findings,
        "verification": verification,
        "tableau_domain_pack": {
            "applicable": tableau_result["applicable"],
            "manual_checklist": tableau_result["manual_checklist"],
        },
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Scanned {skill_md}")
        if verification.get("scripts_checked", 0) or verification.get("note"):
            print(
                f"Execution check: {verification.get('scripts_checked', 0)} script(s) checked, "
                f"{verification.get('syntax_ok', 0)} syntax-ok, {verification.get('syntax_failed', 0)} syntax-failed, "
                f"{len(verification.get('imports_checked', []))} import(s) probed, {len(verification.get('imports_failed', []))} failed"
                + (f" -- {verification['note']}" if verification.get("note") else "")
            )
        print(f"{result['finding_count']} finding(s): {result['confirmed_count']} confirmed, {result['heuristic_count']} heuristic\n")
        if tableau_result["applicable"]:
            print(
                "Tableau domain rule pack activated (references/tableau-rules.md) -- "
                f"{len(tableau_result['manual_checklist'])} item(s) need manual review beyond the findings below.\n"
            )
        for f in findings:
            print(f"[{f['confidence'].upper()}] {f['category']} -- {f['file']}:{f['line']}")
            print(f"  {f['snippet']!r}")
            print(f"  risk: {f['risk']}")
            print(f"  fix:  {f['fix']}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
