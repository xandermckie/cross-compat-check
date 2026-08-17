# cross-compat-check

A Claude Code / Cowork skill that audits a skill you've authored for places where it would
behave differently — or silently break — depending on whether it's loaded by Claude Code
(reading straight off disk) or by Cowork (which only ever sees a skill via a claude.ai account
upload, the Skills API, or a full plugin install). Run it before you check a skill into GitHub
to share.

It catches things like:

- Claude Code-only frontmatter fields (`argument-hint`, `disable-model-invocation`, `model`,
  `hooks`, etc.) that hard-error when a skill is uploaded/enabled for Cowork, since that path
  only accepts a strict 6-field spec: `name`, `description`, `license`, `compatibility`,
  `metadata`, `allowed-tools`
- Shell-injection syntax (`` !`command` ``) and `${CLAUDE_*}` variable substitution, both of
  which only run inside Claude Code and arrive as literal text everywhere else
- Tool references that only exist on one product — `AskUserQuestion`, `SendUserFile`,
  `TaskCreate`/`TaskUpdate`, the `mcp__remote-devices__*` device bridge, and hooks are
  Cowork/Claude-Code-specific in ways that aren't obvious from reading a skill body. Includes a
  specific check for the subagent-spawning tool being named `Task` in Claude Code and `Agent` in
  Cowork.
- Hardcoded absolute paths, Cowork-only filesystem phrasing ("outputs directory", "uploads
  directory"), and undocumented environment variable requirements

It doesn't just flag issues — it walks you through each one conversationally (similar to the
`grill-me` skill's Q&A style), explains the risk in plain terms, proposes a concrete fix, and
applies the ones you approve.

## Install

**Claude Code** — clone this repo into your personal skills directory:

```bash
git clone https://github.com/xandermckie/cross-compat-check.git ~/.claude/skills/cross-compat-check
```

or clone it anywhere and symlink `~/.claude/skills/cross-compat-check` to it. Claude Code picks
up new skills without a restart.

**Cowork** — enable it for your claude.ai account from the Skills settings (or Customize in the
desktop app sidebar), or drop it into a project's `.claude/skills/` if you're checking a repo's
own skills for portability from a cloud session.

## Usage

Once installed, just ask: *"Check my `<skill-name>` skill for cross-compatibility before I push
it"* — or point Claude at a skill directory directly. See `SKILL.md` for the full workflow, and
`references/compat-rules.md` for the reasoning behind every rule the scanner checks.

## How it works

`scripts/scan_skill.py` is a dependency-free Python script that does the deterministic pattern
matching (frontmatter keys, injection syntax, known tool names, hardcoded paths, env vars). The
skill reads its findings, adds the reasoning from `references/compat-rules.md`, and drives an
interactive review — it doesn't just dump a report.

```bash
python3 scripts/scan_skill.py <path-to-skill-or-SKILL.md> --json
```

## Development

`evals/evals.json` has the test prompts used to validate this skill (a deliberately broken
fixture, a clean one, and a legitimately Cowork-only one, each run with and without the skill
via [skill-creator](https://github.com/anthropics/claude-plugins-official)). See the skill's own
commit history for the eval results that validated it before first publishing.

## Notes and limitations

The tool-name lists in `references/compat-rules.md` (and mirrored in `scan_skill.py`) are a
best-effort snapshot of Claude Code and Cowork's tool surfaces, not a guaranteed-current spec —
both products evolve. If you find something stale, a PR updating the rule catalog is welcome.
