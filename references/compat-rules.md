# Cross-compatibility rule catalog

**Tool inventory and frontmatter-spec facts last verified: 2026-08-17**, against Claude Code's
published skills documentation and this session's own Cowork tool listing. The frontmatter
6-field spec and dynamic-injection behavior (rules 1-3) come from Claude Code's docs directly and
should stay accurate as long as that page's URL structure doesn't change; the tool-name lists in
rule 4 are a snapshot of two products that both ship updates regularly, so they're the part most
likely to drift. If you're reading this more than a few months after the date above, or a
tool-reference finding doesn't feel right, don't trust the list blindly -- spot-check the tool
against the current session's own tool listing (for Cowork-side tools) or current Claude Code
docs (for Claude-Code-side ones), and update this file's date when you do.

This is the source of truth `scan_skill.py` codes against, and what you explain to the user
when walking through findings. Every rule below is grounded in one of two places:

- **Confirmed** — documented, mechanical behavior (Claude Code's skills docs, the Agent Skills
  spec, or the plugin schema shared by Cowork and Claude Code). Present these as facts.
- **Heuristic** — a pattern that's usually a problem but depends on how the skill is actually
  distributed/used. Present these as "worth checking," not certainties, and say why.

Two products, two loading paths — this is the root cause of every rule here:

- **Claude Code (CLI)**, reading a skill straight off disk (`~/.claude/skills/`, `.claude/skills/`,
  or a plugin's `skills/`), honors every frontmatter field Claude Code defines and runs the full
  set of body features (shell injection, `${CLAUDE_*}` substitution, hooks).
- **Cowork**, whether desktop or cloud, never reads a local `~/.claude/skills/` folder. A
  standalone skill only reaches Cowork by being uploaded/enabled for the user's claude.ai
  account (or, for cloud sessions, committed into a repo's `.claude/skills/`). The account-upload
  path — which is also what `package_skill.py` and the Skills API enforce — validates frontmatter
  against a strict 6-field allowlist and hard-errors on anything else. Body-level dynamic
  features that only make sense as a local preprocessing step (shell injection, `${CLAUDE_*}`
  variables) either don't run in that path or arrive as literal text.
- **Plugins** (`.plugin` files with `skills/*/SKILL.md` inside) are a separate, richer
  distribution path that Cowork also supports install-time — `${CLAUDE_PLUGIN_ROOT}` and the
  full component set (agents, hooks, MCP servers) are part of that shared schema. But hooks are
  "rarely used" and agents "uncommonly used" in practice on the Cowork side, so treat plugin-only
  features as a place to double check behavior rather than assume parity.

So "works unmodified in both" really means: **written narrowly enough to survive the strictest
of the two loading paths** — the bare-skill account-upload path — while still reading naturally
as a normal Claude Code skill.

## 1. Frontmatter fields (Confirmed)

Claude Code accepts every field below. Outside Claude Code — a claude.ai account upload, the
Skills API, or `package_skill.py` packaging, all of which is how a standalone skill reaches
Cowork — **only** these six survive:

```
name  description  license  compatibility  metadata  allowed-tools
```

Any other key causes a hard error at upload/package time:
`Unexpected key(s) in SKILL.md frontmatter: <field>. Allowed properties are: ...`

Flag every frontmatter key that is *not* in that set of six. Common offenders and what they're
for (so you can explain the tradeoff, not just say "remove it"):

| Field | Purpose in Claude Code | Fix |
|---|---|---|
| `when_to_use` | Extra trigger phrases appended to `description` | Fold the phrasing straight into `description` instead of a separate field |
| `argument-hint` | Autocomplete hint for `/name arg` | Drop it, or describe expected input in the body instead |
| `arguments` | Named positional `$name` substitution | Rewrite body to use `$ARGUMENTS` as free text, or just describe the expected input format in prose and let Claude parse it |
| `disable-model-invocation` | Restricts to manual `/name` invocation | There's no Cowork equivalent of manual-only invocation via frontmatter — if this matters, say so in the skill's body ("only run this when the user explicitly asks") and accept it's advisory rather than enforced outside Claude Code |
| `user-invocable` | Hides from `/` menu, model-only | Same as above — express intent in prose, not frontmatter |
| `disallowed-tools` | Removes tools from the pool for the turn | Express as an instruction in the body ("do not use tool X for this") |
| `model` / `effort` | Per-invocation model/effort override | Drop; can't be replicated outside Claude Code frontmatter |
| `context: fork` / `agent` / `background` | Runs the skill as a subagent | This is a structural choice, not just metadata — see rule 4 below |
| `hooks` | Registers session hooks when the skill loads | Not part of the 6-field spec at all; move hook logic out of the skill or accept Claude-Code-only behavior |
| `paths` | Auto-loads only for matching file globs | Drop; describe the applicable context in the body/description instead |
| `shell` | Chooses bash vs PowerShell for injected commands | Only matters if you're using shell injection, which rule 2 already flags |

`name`, `description`, `license`, `compatibility`, `metadata`, and `allowed-tools` are always
safe. `allowed-tools` in particular is worth keeping — it's a permission pre-grant, not a
Claude-Code-only convenience, and it's part of the portable spec.

## 2. Dynamic context injection in the body (Confirmed)

Claude Code preprocesses two body-level syntaxes before Claude ever sees the skill:

- Inline: `` !`command` `` (a line starting with `!`, or `!` right after whitespace, followed by
  a backtick-quoted shell command)
- Fenced: a code block opened with ` ```! `

Both run the command locally and splice the output into the skill text. Outside Claude Code —
same three paths as above (account upload, Skills API, `package_skill.py`) — this doesn't run.
The literal `` !`command` `` text (or, in a Cowork session running a skill synced from the
account, a `[shell command execution disabled by policy]`-style placeholder) is what Claude
actually sees. A skill that leans on this to inject a git diff, file listing, etc. silently loses
that context outside Claude Code — it doesn't error, it just goes quiet, which is worse.

**Fix**: replace the injection with an explicit instruction in the body telling Claude to run the
command itself with the Bash tool (or `Read`/`Grep`) as a normal step, and use the output from
that point on. This is slightly more token cost (Claude issues a real tool call) but behaves
identically everywhere, and you don't need `allowed-tools` pre-approval for it to work — it'll
just prompt for permission the first time in Claude Code, or run directly in Cowork's sandbox.

## 3. `${CLAUDE_*}` and `$ARGUMENTS`-family substitution (Confirmed)

`${CLAUDE_SESSION_ID}`, `${CLAUDE_SKILL_DIR}`, `${CLAUDE_PROJECT_DIR}`, `${CLAUDE_PLUGIN_ROOT}`,
`${CLAUDE_PLUGIN_DATA}`, and the `$ARGUMENTS` / `$N` / `$name` argument placeholders are all
substituted by Claude Code before the model sees the text. Outside Claude Code they arrive as
literal characters — `${CLAUDE_SKILL_DIR}/scripts/foo.py` shows up in the prompt exactly like
that, not as a real path.

Exception: `${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_PLUGIN_DATA}` are part of the plugin schema, and
Cowork does support installing full plugins (not just bare skills) — so if this skill will only
ever be distributed as part of a `.plugin`, and never uploaded as a standalone skill, this is
lower risk. Ask which distribution path the user actually intends before treating this as a hard
break; if they're not sure, treat it as a hard break, since "standalone skill on GitHub" is the
more common sharing pattern and that's the strict path.

**Fix**: for scripts, tell Claude explicitly to locate and run the script relative to the skill's
own directory ("run `scripts/scan_skill.py` from this skill's folder") rather than building the
path with a substitution variable. For `$ARGUMENTS`-style input, describe the expected input in
prose and let Claude extract it from what the user actually typed.

## 4. Tool name references that don't exist on both surfaces (Heuristic, but usually right)

This is the one that's easy to miss because it doesn't throw an error anywhere — the skill body
just tells Claude to "use tool X," and if X isn't in the toolset on one side, Claude either
silently skips the step, hallucinates a substitute, or gets confused mid-task.

**Known Cowork-side tools with no Claude Code equivalent** (best-effort list, verify against the
current session's tool listing if in doubt — these are all things that showed up as first-class
tools in a Cowork system prompt):

- `AskUserQuestion` — structured multiple-choice clarifying questions with a rendered UI
- `SendUserFile` — delivers a file into the conversation as a downloadable/previewable card
- `SendUserMessage` — sends verbatim text the user reads outside normal turn output
- `ShowOnboardingRolePicker` — Cowork onboarding UI
- `ScheduleWakeup` — dynamic `/loop` self-scheduling
- `TaskCreate` / `TaskUpdate` / `TaskList` / `TaskGet` / `TaskOutput` / `TaskStop` — Cowork's
  rendered task-list widget
- `mcp__remote-devices__*` — the bridge to the user's local desktop filesystem/MCP servers
- `ReportFindings`, `SuggestSkills`, `SuggestConnectors`, `ListConnectors` — Cowork-specific UI/
  recommendation surfaces
- `mcp__claude-code-remote__*` (`create_trigger`, `send_later`, etc.) — Cowork's cloud scheduled-
  task system

**Known Claude Code-side conventions with no Cowork equivalent:**

- Hooks (`PreToolUse`, `PostToolUse`, etc. via `settings.json` or a plugin's `hooks/hooks.json`) —
  the docs explicitly call these "rarely used in Cowork"
- `TodoWrite` — Claude Code's own task-list tool. **Don't confuse this with Cowork's
  `TaskCreate`/`TaskUpdate`** — they serve a similar purpose (a visible task list) but are
  different tools with different names. A skill written against one will not "just work" against
  the other; it needs to reference the concept generically ("keep a task list if your tools
  support one") or branch explicitly.
- `claude plugin validate`, `claude plugin eval`, and other `claude` CLI subprocess calls — these
  assume a local Claude Code CLI binary and don't exist inside a Cowork sandbox
- Slash-command self-invocation assumptions (`/deploy`, `/my-skill`) as something the *skill
  itself* shells out to or expects the user to type mid-flow — Cowork's equivalent is the `Skill`
  tool with an `args` parameter, not a typed slash command

**A specific, easy-to-miss trap**: the subagent-spawning tool is named `Task` in Claude Code and
`Agent` in Cowork. A skill that says "spawn a Task subagent to do X" reads as nonsense in Cowork —
there's a `TaskCreate` tool there, but it makes a to-do item, not a subagent.

**Tools that are safe on both** (core, roughly stable across both products): `Read`, `Write`,
`Edit`, `Bash`, `Grep`, `Glob`, `WebSearch`, `WebFetch`, and some form of subagent spawning
(different name, same idea).

**Fix, and this is the pattern worth teaching, not just applying**: don't try to pick one tool
name and hope it resolves on both sides. Write the instruction as a check-and-fallback, the same
way `create-cowork-plugin`'s own SKILL.md does it for `claude plugin validate`:

> Run `claude plugin validate <path>` to check the plugin structure. If this command is
> unavailable (e.g., when running inside Cowork), verify the structure manually: [manual steps]

That's the reusable shape: name the preferred tool/command, then give an explicit, concrete
fallback for when it's missing, rather than assuming either surface. Recommend this rewrite for
every tool reference that isn't in the "safe on both" list above.

## 5. Filesystem and delivery assumptions (Heuristic)

- **Hardcoded absolute paths** (`/home/...`, `/root/...`, `/Users/...`, `/mnt/user-data/...`,
  `C:\Users\...`, `~/Downloads`, etc.) are fragile on both sides but for different reasons — a
  Claude Code user's home directory obviously varies, and Cowork's cloud sandbox filesystem
  layout is nothing like a local machine's. Flag any absolute path literal and recommend either a
  relative reference (Claude Code) or an instruction to ask the environment for the right
  location rather than assuming one.
- **"Save it to the outputs directory" / "uploads directory"** phrasing is a Cowork-specific
  convention (files delivered to the user go through `SendUserFile`; user-provided files land
  under an uploads path). A Claude Code skill has no such directories — it just operates on the
  local filesystem the CLI was launched in, and "delivering" a file to the user means nothing
  special beyond writing it to disk. If a skill's instructions assume either convention
  unconditionally, flag it.
- **Assuming a "device bridge" is available** (anything implying `mcp__remote-devices__*`) only
  makes sense when a Cowork desktop app is connected. Claude Code has no equivalent concept at
  all — there is no "device" separate from the machine Claude Code is already running on.

**Fix**: describe file delivery in terms of the outcome ("make sure the user ends up with file X"),
and let the body branch on what's actually available rather than assuming one delivery mechanism.

## 6. Environment variables and credentials (Heuristic)

References to specific env vars -- `os.environ[...]`, `os.environ.get(...)`, `os.getenv(...)`,
`process.env.X`/`process.env['X']` in Python/JS, or bare `$VAR`/`${VAR}` reads in a bundled
`.sh` script -- that aren't things the skill itself defines are a portability risk: Cowork's
sandbox starts clean aside from what the platform sets, and inherited shell environment in
Claude Code varies by user. Flag any env var read that isn't obviously platform-provided (a
short allowlist of genuinely universal shell/OS vars like `PATH`, `HOME`, `TMPDIR` is exempt),
and recommend documenting required env vars explicitly (a README, or the `compatibility` field)
rather than assuming they're set.

This is the one rule where the fix and the "did they already fix it" check are the same field:
once a skill's `compatibility` text mentions a flagged variable by name, the scanner stops
re-flagging it. Don't propose adding a `compatibility` note for something it already says --
check the field's actual text (folded across its continuation lines, not just the first line)
before recommending this fix. This only applies to env-var findings; a confirmed frontmatter,
injection, or substitution finding still needs an actual rewrite, since a `compatibility` note
can't excuse a hard upload error the way it can excuse an undocumented credential requirement.

## 7. Documenting the gap you can't close (Confirmed as good practice)

Not everything is fixable — sometimes a skill's whole point requires a tool that only exists on
one side (e.g., a skill built entirely around the device bridge). When a finding can't be
resolved with a rewrite, the right outcome isn't to force it, it's to say so explicitly using the
`compatibility` frontmatter field (up to 500 characters, part of the portable 6-field spec) —
e.g. `compatibility: Requires the Cowork desktop app with a connected device; not usable from
Claude Code.` That's still "checked into GitHub" honestly — it just isn't claiming portability it
doesn't have.

## 8. Plugin-level checks (only when scope includes plugin context)

If the skill lives inside a plugin (`.claude-plugin/plugin.json` present, or the skill sits under
a `skills/` directory with siblings like `agents/`, `hooks/`, `.mcp.json`):

- Hooks and agents are valid in both products' shared schema, but the docs note they're "rarely"
  / "uncommonly" used on the Cowork side — flag their presence as "verify this actually behaves
  as expected in Cowork," not as an error.
- Any path in `hooks/hooks.json` or `.mcp.json` should use `${CLAUDE_PLUGIN_ROOT}`, never a
  hardcoded path — this one substitution *is* safe across both, since it's part of the shared
  plugin schema, not the bare-skill upload path.
- `plugin.json` itself only strictly requires `name`; extra undocumented fields are lower risk
  than SKILL.md's strict allowlist, but keep it to the documented fields (`name`, `version`,
  `description`, `author`, `homepage`, `repository`, `license`, `keywords`, plus the optional
  `commands`/`agents`/`hooks`/`mcpServers` path overrides).

## 11. Execution verification -- actually running what's safe to run (Confirmed, when triggered)

Rules 1-10 are all text pattern matching: inferring risk from what a file *says*, never
confirming what actually happens when it runs. That's necessary for most cross-compat checks
(the whole point is comparing behavior across a platform the scanner isn't running on), but it
means every "heuristic" finding is a guess dressed up as a warning -- not a reproduced fact.

The scanner closes part of that gap itself, automatically, every run (no flag needed unless you
pass `--no-execute`): it actually runs each bundled script through its own language's real
syntax checker (`python3 -m py_compile`, `bash -n`, `node --check` -- pure parsing, nothing
executes), and actually attempts `python3 -c "import <module>"` for every third-party import a
Python script makes at module scope. Both are safe to run unconditionally: a syntax check never
executes the file's logic, and importing a module is standard, low-risk practice (bounded by a
timeout in case something does unexpected work at import time) -- it's what `pip check`-style
tooling already does. This deliberately stops there. It does NOT run a script's actual business
logic -- that could hit live APIs, need real credentials, or have side effects, and choosing to
do that is a per-skill judgment call the calling skill should make explicitly with the user, not
something to do silently by default.

**A syntax error or a genuinely missing import is a `confirmed` finding, not a heuristic** --
it's a reproduced fact, not an inference, and it's not really a cross-compat issue at all: a
script that can't parse or can't import its own dependencies fails identically on both products.
Report these first, before anything else, since nothing about portability matters until the
script actually runs.

**Imports guarded by `try: import X / except ImportError:` are never flagged**, even if the
import fails in the scanner's own environment. That pattern means the author already decided the
dependency is optional and wrote a fallback -- catching that as a "confirmed" problem would
punish exactly the defensive coding this tool should want to see more of. The scanner still
probes guarded imports (so the verification summary can honestly say what was checked), it just
doesn't turn a guarded failure into a finding. This is exactly the shape of a good pattern found
in the wild: a skill's `us_holidays()` helper tries `import holidays`, falls back to a small
vendored federal-holiday table on `ImportError`, and documents in its own docstring that holiday
exclusion "NEVER silently degrades" as a result -- that's the model to point to when explaining
why this distinction matters, not just describing it abstractly.

**A successful import only proves availability in the scanner's own environment.** If a script
imports `python-dotenv` and the scanner's `import dotenv` actually succeeds, say exactly that --
and immediately caveat it: this scanning environment having the package installed says nothing
about whether Cowork's sandbox or the end user's Claude Code machine does. Don't let a passing
check read as "confirmed fine everywhere"; the honest claim is narrower than that.

**Always report the verification summary to the user, not just the findings it produced.** The
scanner returns a top-level `verification` object (`scripts_checked`, `syntax_ok`,
`syntax_failed`, `imports_checked`, `imports_failed`, `imports_guarded`) every run, even when
there's nothing to check (`scripts_checked: 0` with a `note` explaining why). Surfacing this --
"actually ran N scripts through a real syntax check and tried importing M third-party
dependencies; here's what passed" -- is what makes this check something you *ran*, not something
you *assumed*, and the user should see that distinction, not just the list of problems found.

## 9. Hardcoded MCP server/tool names (Heuristic)

MCP tools show up in the toolset as `mcp__<server>__<tool>`, and it's tempting to write a skill
step as "call `mcp__tableau__query_datasource`" the same way you'd name a first-party tool like
`Bash`. The difference: `<server>` isn't part of any spec -- it's whatever name the connector
happened to get configured under, and that's set per user, per org, or per session, not by the
tool's author. The same underlying Tableau/Salesforce/whatever integration can be
`mcp__tableau__`, `mcp__tableau-prod__`, or `mcp__acme-tableau__` depending entirely on who set
it up. On top of that, Cowork frequently defers MCP tools until something calls `ToolSearch` for
them -- a skill that assumes the exact name is already loaded can fail to find it even when the
right connector genuinely is connected.

This does NOT apply to Cowork's own two fixed platform bridges -- `mcp__remote-devices__*` (the
desktop bridge) and `mcp__claude-code-remote__*` (the scheduled-task system) -- those are stable,
first-party namespaces already covered by rule 4, not user-configured connectors. The scanner
exempts both prefixes from this rule for exactly that reason.

**Fix**: describe the capability, not the tool name -- "query the Tableau datasource for X" --
and tell Claude to resolve the actual tool at runtime: a `ToolSearch` call in Cowork (matching on
keywords like "tableau query-datasource"), or its already-loaded MCP tool list in Claude Code.
This is precisely the pattern a well-built skill in the wild already uses for exactly this
problem:

> MCP tools are often DEFERRED in Cowork -- run a ToolSearch for "tableau query-datasource" first
> and use whichever server resolves. Never assume a specific `mcp__<server>__` prefix; it varies
> by session (claude.ai connector, desktop-proxied server, org gateway).

That's the shape to teach: name what you're looking for, discover the real tool name at runtime,
never hardcode the middle segment.

## 10. Credential loading across two very different sandboxes (Heuristic)

Reading a required env var (rule 6) is only half the story -- the harder question is *how does
the value actually get into the process* on each product, because the two run in genuinely
different conditions:

- **Claude Code** runs on the user's own machine with a persistent shell. `export FOO=bar` in
  their profile, or a `.env` file sitting next to the skill's scripts, both work fine and persist
  across sessions.
- **Cowork's sandbox starts clean every session.** There's no shell profile to pre-export into,
  and the skill's own directory (synced from a claude.ai account or a plugin install) generally
  isn't a sensible place for a user to drop a private credentials file even if they could. The
  one place a user's own file reliably lands is wherever uploaded conversation attachments go
  (the session's uploads directory) -- which the skill has to know to check, since it's a
  different location than "next to the script."

Two concrete things to flag:

- **A `python-dotenv` dependency** (`from dotenv import ...`, `import dotenv`, `load_dotenv(`).
  It's a fine library, but it's an *extra* pip package -- not guaranteed present in Cowork's
  fixed sandbox image or on a Claude Code user's already-installed packages. A script that
  imports it unconditionally will `ImportError` before it ever reaches the "credential missing"
  message it was trying to produce, which is a worse failure than the one it was guarding against.
- **An env var read with no working setup path**, even once it's named in `compatibility` (rule
  6's suppression only means "the requirement is documented," not "there's a way to satisfy it on
  both products").

**Fix -- a small stdlib-only loader**, no extra dependency, that checks the environment first and
only then looks for a `.env`-style file in a couple of predictable places, ending in an error
that names the fix for *both* products explicitly rather than a bare `KeyError`/`None`:

```python
import os
from pathlib import Path

def load_env(var_names, extra_search_paths=()):
    missing = [v for v in var_names if v not in os.environ]
    if missing:
        for d in (Path(__file__).resolve().parent, *[Path(p) for p in extra_search_paths]):
            env_file = d / ".env"
            if not env_file.exists():
                continue
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            missing = [v for v in var_names if v not in os.environ]
            if not missing:
                break
    if missing:
        raise RuntimeError(
            f"Missing required env var(s): {', '.join(missing)}.\n"
            f"Claude Code: export them in your shell, or drop a .env file next to this script.\n"
            f"Cowork: upload a .env file into this conversation, then pass the uploads "
            f"directory in extra_search_paths -- or wire up a connector/secret that sets "
            f"these for you instead of a raw token."
        )
```

The point isn't that this exact function is mandatory -- it's the shape: environment first,
then a discoverable file in more than one plausible location, then a specific and actionable
error naming what to do on *each* product, never a silent `None` that surfaces as a confusing
failure three steps later.
