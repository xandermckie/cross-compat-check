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
import json
import re
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

ENV_VAR_PATTERNS = [
    re.compile(r"os\.environ\[[\'\"]([A-Z_][A-Z0-9_]*)[\'\"]\]"),
    re.compile(r"process\.env\.([A-Z_][A-Z0-9_]*)"),
]

PLATFORM_PROVIDED_ENV = {"CLAUDE_PROJECT_DIR", "CLAUDE_SESSION_ID", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA", "PATH", "HOME"}


def parse_frontmatter(text):
    """Minimal, dependency-free frontmatter parser. Good enough for flat key: value pairs;
    doesn't need to fully understand YAML since we only care about top-level key names."""
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
        # indented continuation lines (multi-line strings, list items) are ignored for key
        # detection purposes -- we only need the set of top-level keys present.
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

    for pat in ENV_VAR_PATTERNS:
        for m in pat.finditer(text):
            var = m.group(1)
            if var in PLATFORM_PROVIDED_ENV:
                continue
            findings.append({
                "id": f"env-var-{var}-{m.start()}",
                "confidence": "heuristic",
                "category": "environment",
                "file": str(path),
                "line": line_number_for(text, m.start()),
                "snippet": m.group(0),
                "risk": f"Reads env var {var}, which isn't obviously platform-provided. Cowork's sandbox starts clean aside from what the platform sets.",
                "fix": "Document the required env var explicitly (README or the `compatibility` field) rather than assuming it's set.",
            })

    # Rule 7: compatibility field present when risky features exist
    risky_categories = {f["category"] for f in findings if f["confidence"] == "confirmed"}
    if risky_categories and "compatibility" not in fields:
        findings.append({
            "id": "missing-compatibility-field",
            "confidence": "heuristic",
            "category": "documentation",
            "file": str(path),
            "line": 1,
            "snippet": "(no compatibility field in frontmatter)",
            "risk": "This skill has confirmed cross-compat issues but no `compatibility` field documenting environment requirements.",
            "fix": 'Once the fixable issues above are resolved, if anything genuinely can\'t be made portable, add e.g. `compatibility: Requires Claude Code; not usable from Cowork.` so the limitation is explicit rather than discovered by trial and error.',
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
    args = parser.parse_args()

    target = Path(args.target).expanduser().resolve()
    skill_md = find_skill_md(target)
    skill_dir = skill_md.parent

    findings = scan_skill_md(skill_md) + scan_plugin_context(skill_dir)

    # also scan any bundled scripts for shell-injection-style patterns is out of scope --
    # scripts are executed, not preprocessed, so the injection/substitution rules don't apply
    # to them. Bundled scripts are still worth an env-var pass though.
    scripts_dir = skill_dir / "scripts"
    if scripts_dir.exists():
        for script_file in scripts_dir.rglob("*"):
            if script_file.is_file() and script_file.suffix in (".py", ".sh", ".js", ".ts"):
                script_text = script_file.read_text(encoding="utf-8", errors="replace")
                for pat in ENV_VAR_PATTERNS:
                    for m in pat.finditer(script_text):
                        var = m.group(1)
                        if var in PLATFORM_PROVIDED_ENV:
                            continue
                        findings.append({
                            "id": f"script-env-var-{script_file.name}-{var}-{m.start()}",
                            "confidence": "heuristic",
                            "category": "environment",
                            "file": str(script_file),
                            "line": line_number_for(script_text, m.start()),
                            "snippet": m.group(0),
                            "risk": f"Bundled script reads env var {var}, which isn't obviously platform-provided.",
                            "fix": "Document the required env var explicitly (README or the `compatibility` field).",
                        })
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

    result = {
        "skill_md": str(skill_md),
        "finding_count": len(findings),
        "confirmed_count": sum(1 for f in findings if f["confidence"] == "confirmed"),
        "heuristic_count": sum(1 for f in findings if f["confidence"] == "heuristic"),
        "findings": findings,
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Scanned {skill_md}")
        print(f"{result['finding_count']} finding(s): {result['confirmed_count']} confirmed, {result['heuristic_count']} heuristic\n")
        for f in findings:
            print(f"[{f['confidence'].upper()}] {f['category']} -- {f['file']}:{f['line']}")
            print(f"  {f['snippet']!r}")
            print(f"  risk: {f['risk']}")
            print(f"  fix:  {f['fix']}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
