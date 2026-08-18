---
name: cross-compat-check
description: >
  Audits a skill you've authored for places where it behaves differently -- or silently breaks
  -- between Claude Code and Cowork, before you check it into GitHub. Detects Claude Code-only
  frontmatter fields, shell-injection/${CLAUDE_*} substitution that goes inert outside Claude
  Code, one-product-only tool references (AskUserQuestion, SendUserFile, TaskCreate/TaskUpdate,
  device bridge, hooks, TodoWrite, etc.), hardcoded paths and MCP server/tool prefixes,
  credential-loading gaps (python-dotenv dependency, env vars with no working setup path on
  Cowork's clean sandbox), and Cowork-only filesystem assumptions like an "outputs directory".
  Walks through findings one at a time with a risk explanation and concrete fix, applying what
  you approve. Use whenever checking a skill for cross-compatibility or portability before
  sharing it, or when a skill breaks on one product but not the other.
license: MIT
compatibility: Runs the bundled scan_skill.py with Python 3 (no external dependencies) and edits
  files with Edit -- works in both Claude Code and Cowork.
---

# cross-compat-check

You're auditing a skill someone else will `git clone` and use from either Claude Code (reading
straight off disk) or Cowork (which never reads local skill files directly -- it only sees a
skill via a claude.ai account upload, or as part of a full plugin install). Those are two
different loading paths with different rules, and a skill that looks fine in one can quietly
misbehave, or outright fail to package, in the other. Your job is to find every place that's
true and help fix it -- not to lint the skill for general quality.

Read `references/compat-rules.md` before your first walkthrough of a real skill -- it has the
full reasoning behind every rule the scanner checks, including which findings are hard facts
(documented, mechanical) versus which are heuristics worth a second look. You'll need that
context to explain findings well, since the point of this skill is teaching the user *why*
something breaks, not just handing them a checklist.

## Step 1: Figure out what you're scanning

Ask the user (if it isn't already obvious from context) which skill or plugin directory to
check. If they haven't told you the intended distribution path -- a standalone skill others will
add individually, or part of a plugin -- ask, since it changes how strictly rule 3 (the
`${CLAUDE_PLUGIN_ROOT}` substitution) applies. Don't ask more than you need to get started;
you'll surface anything else that matters as findings.

## Step 2: Run the scanner

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/scan_skill.py <path-to-skill-or-SKILL.md> --json
```

If `${CLAUDE_SKILL_DIR}` doesn't resolve (e.g. you're running this skill's own body outside
Claude Code), locate `scan_skill.py` relative to wherever this skill's files actually live and
run it directly with that path instead.

This gives you a structured list of findings, each with a `confidence` (`confirmed` or
`heuristic`), `category`, file/line, a `risk` explanation, and a suggested `fix`. Read
`compat-rules.md` alongside the raw findings -- the script tells you *what* it found, the
reference doc tells you *why it matters* and gives you the fuller fix pattern to propose (the
scanner's `fix` field is a short version; the doc has the reasoning and, for tool-reference
findings, the general "check-and-fallback" rewrite pattern worth teaching, not just applying).

The JSON also includes a top-level `verification` object -- the scanner doesn't just pattern-match
text, it actually runs each bundled script through a real syntax checker and actually attempts to
import every third-party Python dependency (see compat-rules.md rule 11 for exactly what that
does and doesn't cover). **Always report this to the user before the findings list**, even when
it found nothing wrong -- e.g. "actually ran 5 scripts through a syntax check and tried importing
2 third-party dependencies; all passed" is the evidence that this was actually verified, not
assumed, and the user should see that regardless of whether problems turned up. Any
`category: execution` findings (a real syntax error, a real failed import) are `confirmed` --
present these before every other finding, since nothing about cross-compat matters if the script
doesn't even run.

If the scan comes back with zero findings, say so plainly (including the verification summary)
and skip straight to Step 4 -- don't manufacture things to ask about.

## Step 3: Walk through findings one at a time (grill-me style)

Don't dump the whole findings list and ask "which do you want fixed." Go one at a time, in this
order: `confirmed` findings first (these are mechanical facts, not judgment calls), then
`heuristic` findings grouped by file so related ones are easy to compare.

For each finding:

1. **Show the snippet in context** -- enough surrounding text that the user recognizes what
   you're pointing at without having to go open the file themselves.
2. **Explain the risk in plain terms** -- what actually happens differently on the other
   product, not just "this field isn't allowed." If you know *why* (from compat-rules.md), say
   why -- that's the teaching part of this skill, and it's what makes the user better at writing
   portable skills next time instead of just fixing this one. For an `mcp-reference` finding
   (rule 9) specifically, don't just assert the risk -- actually check it: run `ToolSearch` (or,
   in Claude Code, look at your already-loaded tool list) for the exact hardcoded name right now,
   in this session, and tell the user whether it resolves here or not. Either answer is useful
   evidence ("it doesn't resolve in this session either" makes the risk concrete; "it does
   resolve here" is still worth saying, with the caveat that a different session/user/org may
   have it under a different name) -- but don't skip the check and just describe the theoretical
   risk when you have the tools to verify it directly.
3. **Propose a specific fix** -- not "consider rewriting this," but the actual replacement text
   or frontmatter change you'd make. For tool-reference findings, default to proposing the
   check-and-fallback rewrite pattern (see compat-rules.md rule 4) rather than just deleting the
   reference -- that usually preserves the skill's actual capability on the product where the
   tool does exist, instead of losing it everywhere.
4. **Ask before applying, one finding per question.** If the `AskUserQuestion` tool is available
   in this environment (it is in Cowork; it isn't in Claude Code), use it for this step -- a
   structured multiple-choice pop-up is a better fit for a long walkthrough than free text, and
   it's the same mechanism `grill-me` uses. Ask about exactly one finding per call: put the
   finding and its risk in the `question` text, and give 2-3 concrete options -- typically
   "Apply fix: <the specific rewrite>" as the recommended option, "Skip -- this is intentional"
   as the other, and a third option only when there's a genuinely different second fix worth
   offering (e.g. "Remove the field" vs. "Keep it and document as Claude-Code-only via
   `compatibility`"). `AskUserQuestion` always adds its own free-text option, so don't add an
   "Other" option yourself. Don't batch multiple findings into one call just because the tool
   technically accepts several questions at once -- the point is a sequential walkthrough the
   user can follow, not a form to fill out all at once.

   If `AskUserQuestion` isn't available, fall back to a plain conversational yes/no/"let me
   tweak it" question instead -- same one-at-a-time structure, just as normal text. This is
   worth noticing explicitly: it's the same check-and-fallback pattern rule 4 in
   `compat-rules.md` recommends for every tool reference you'll find in *other* people's skills.
   This skill's own walkthrough step has exactly the kind of environment-dependent tool
   reference it exists to catch -- so it practices what it checks, rather than assuming Cowork
   and hardcoding `AskUserQuestion` outright.

   Either way, once the user answers, apply the change with Edit immediately rather than
   batching edits for later -- that way if something looks wrong after the edit, it's easy to
   spot before you've moved three findings further on.

A few callibration notes so you don't over- or under-call things:

- Heuristic tool-reference findings for a tool the skill is *obviously* meant to use on purpose
  (e.g. a Cowork-only plugin-creation skill that legitimately uses `AskUserQuestion` throughout,
  because it's only ever meant to run in Cowork) aren't bugs. Say so, and skip them quickly --
  don't push a fallback rewrite onto a skill that was never meant to be portable in the first
  place. This is exactly the kind of judgment call the scanner can't make and you can.
- If several findings are really the same root cause (e.g. five separate `${CLAUDE_PROJECT_DIR}`
  hits because the skill leans on it throughout), say that up front and offer to fix the pattern
  once rather than making the user say "yes" five times for what's really one decision.
- If a finding truly can't be fixed without losing the skill's actual purpose (rule 7 in
  compat-rules.md), don't force a rewrite -- propose adding or updating the `compatibility`
  frontmatter field instead, so the limitation is documented rather than discovered by a
  confused user later.
- Hardcoded MCP tool names (rule 9) and a `python-dotenv` dependency (rule 10) do NOT have a
  `compatibility`-field escape valve the way a plain env-var read does -- naming the requirement
  in frontmatter documents it, but doesn't make a hardcoded `mcp__<server>__` prefix resolve on a
  different session, or make an unavailable pip package importable. These two need an actual
  rewrite; don't propose "just document it" for them.
- For env-var findings specifically, the fix is stronger than "add a compatibility note" -- the
  real goal is a setup path that actually works on Cowork's clean-per-session sandbox, not just a
  documented requirement. See compat-rules.md rule 10 for the stdlib-only loader pattern (checks
  `os.environ` first, then a discoverable `.env` file, then a specific per-product error) and
  propose it when a skill has more than one or two required credentials.

## Step 4: Re-scan and summarize

After applying fixes, run the scanner again to confirm the fixed findings are actually gone and
nothing new got introduced. Then give a short summary: what was fixed, what was intentionally
left as-is (and why -- e.g. "this skill is Cowork-only by design"), and whether the
`compatibility` field accurately reflects any remaining limitation. This is the point where it's
reasonable to tell the user the skill is ready to check into GitHub -- don't say that earlier.

## Notes on the scanner itself

- It's intentionally conservative about false negatives over false positives: it flags anything
  matching a known-risky pattern, even when it's probably fine, because a human skimming ten
  findings and dismissing two is cheaper than a human never being shown the one that mattered.
  Don't apologize for or downplay findings that turn out to be non-issues -- just say so and
  move on.
- The scanner reads SKILL.md and every markdown file under `references/`, plus any bundled
  `scripts/*.py`, `*.js`, `*.ts`, `*.sh` -- setup/runbook instructions (MCP discovery steps,
  credential wiring) often live in a reference doc rather than SKILL.md itself, and a hardcoded
  assumption there is just as real a break as one in the entry-point file.
- The tool-name lists in `compat-rules.md` (and mirrored in `scan_skill.py`) are a best-effort
  snapshot, not a guaranteed-current spec -- `compat-rules.md` carries a "last verified" date at
  the top for exactly this reason. If you're ever unsure whether something is genuinely
  Cowork-only or Claude-Code-only, say that uncertainty out loud rather than asserting it as
  fact -- and if you learn a tool's availability has changed, that's worth updating both the
  list and that date in `compat-rules.md` for next time.
- Rules 1-10 are text pattern matching -- inference, not proof. Rule 11 is the exception: every
  run, the scanner actually parses each bundled script with its real language checker and
  actually attempts every third-party Python import, so a syntax error or a missing dependency
  in the findings list is a reproduced fact, not a guess (that's why those land as `confirmed`).
  An import sitting inside `try: ... except ImportError:` is deliberately never flagged even if
  it fails here -- that's the author already handling an optional dependency correctly, and
  punishing it would be a false positive. A passing import check only proves the package is
  importable in *this* scanning environment, not in Cowork's sandbox or the end user's Claude
  Code install -- say that caveat out loud rather than implying the check guarantees portability.
