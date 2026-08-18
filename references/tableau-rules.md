# Tableau cross-compatibility requirements (Claude Code vs Claude Cowork)

This is a **domain-specific rule pack**, layered on top of `compat-rules.md`, not a replacement
for it. It only activates when the target skill is Tableau-related (heuristic: "tableau"
appears repeatedly in the skill's own text, or it reads `TABLEAU_*`-style env vars, or its
scripts talk about VizQL/VDS/datasource LUIDs). Everything in `compat-rules.md` still applies on
top of this -- a Tableau skill can fail rule 1 (frontmatter) the same as any other skill.

The requirements below are implementation-agnostic on purpose: file names, script structure, and
internal architecture are the skill's own business. What's checked is *behavior* -- what the
skill actually does when it runs on each product, not how it's organized internally.

**R1-R4 are hard failures. R5-R7 are warnings.** Some of these are mechanically checkable by
`scan_skill.py` (marked below); the rest require actually reading the skill's logic during the
walkthrough, the same way you'd verify any other judgment-call finding -- don't skip them just
because the scanner can't regex its way to an answer. A skill that looks compliant on a surface
read (e.g. "never assume a specific mcp__ prefix" appears in the text) can still violate the
*intent* of a rule elsewhere (a stated preference order that contradicts R2, for instance) --
reading the actual instructions end to end is part of this check, not optional.

## R1 -- Deterministic environment selection, no probing (hard failure)

The skill must state an explicit, deterministic rule for which connection method it uses,
decided *before* any connection attempt -- never discovered by trial-and-error auth attempts.

- **Claude Code** (or any environment where the user has supplied Tableau credentials, e.g. via
  environment variables or a credentials file): the skill may connect directly using those
  credentials.
- **Claude Cowork** (no user-supplied credentials -- the normal case): the skill must use the
  session's pre-authorized Tableau connectors and nothing else. It must not attempt a
  direct/credentialed path, and must not search for, prompt for, or otherwise acquire
  credentials.

**Not fully mechanically checkable.** A script can legitimately gate its transport choice on
*credential presence* (checking whether `TABLEAU_PAT_NAME`/`TABLEAU_PAT_VALUE` are set, with no
live connection attempt either way) without violating this rule -- that's still deterministic,
just implemented as a cheap presence check rather than a separate "which environment am I in"
flag. What DOES violate it: attempting a live connection first and using its success/failure as
the signal for which path to use, or documentation that frames the decision as "try this, and if
it fails, do that" rather than stating the rule up front. Read the actual selection logic (not
just the docs) before judging this one -- code that's correctly gated on presence can still have
misleading "try it and see" prose sitting next to it, which is worth fixing even if the
underlying behavior is fine, since it's what a future maintainer will copy.

## R2 -- Correct connector usage in Cowork (hard failure)

- **Preference order:** (1) the desktop-bridge Tableau extension -- tools proxied from the
  paired desktop device, identifiable by a device/bridge prefix containing "Tableau" (e.g.
  `mcp__remote-devices__Tableau__query-datasource`); (2) fallback: the cloud-side Tableau
  connector (e.g. `mcp__Tableau_MCP__query-datasource`), including failover mid-task if the
  bridge drops.
- **No hardcoded tool names as the sole lookup.** Exact MCP prefixes vary by session; the skill
  must describe prefix-tolerant identification (pattern-match on the "Tableau" server name), not
  a single literal tool string. A skill whose only instruction is a bare literal like
  `mcp__Tableau__query-datasource` fails this check. *(Mechanically checked: the scanner flags a
  hardcoded `mcp__*[Tt]ableau*__*` literal with no nearby pattern-matching/ToolSearch language.)*
- **Deferred tools:** connector tools in Cowork often require a `ToolSearch` to load their
  schemas before first call. The skill must account for this, and must frame `ToolSearch` as
  loading known connectors' schemas -- never as shopping for alternative servers.

**Read the actual stated preference order, don't just check that pattern-matching language
exists.** A skill can correctly avoid hardcoding a single literal tool name while still stating
the *wrong* preference order (e.g. explicitly preferring the cloud connector over the desktop
bridge) or omitting mid-task failover entirely. Both are R2 failures the scanner's text-pattern
check alone won't catch.

## R3 -- Never initiate authorization (hard failure)

The skill must never trigger an authorization or elicitation flow to any MCP server, gateway, or
service (e.g. an `/authorize` link from an org gateway such as `tag-mcp`). The configured
connectors are already authorized; a tool that demands new auth is by definition the wrong tool,
and the skill's instructions must say so explicitly. **This rule must appear both in the skill's
operating instructions and in any machine-readable artifact the model consumes mid-run** (plans,
query manifests, runbooks) -- mid-run is where the model actually strays, so a plan.json or
similar handed to Claude mid-task should carry the same instruction, not just the top-level
SKILL.md. *(Partially mechanically checked: the scanner flags authorization-flow language
combined with the absence of a "never authorize" style instruction. It cannot verify a
machine-readable artifact's runtime contents -- check any `--plan`/manifest output by hand.)*

## R4 -- Credential hygiene, whenever a credentialed path exists (hard failure)

- **Standard env-var contract:** `TABLEAU_PAT_NAME` + `TABLEAU_PAT_VALUE` required,
  `TABLEAU_SERVER` / `TABLEAU_SITE` optional with sane defaults -- so one setup serves every
  skill. *(Mechanically checked against the names actually read in bundled scripts.)*
- **Per-user tokens only** (Tableau permits one live session per PAT; shared tokens cause mutual
  session invalidation under concurrency).
- **Missing credentials must fail fast with a clear, actionable message** -- never a traceback,
  and never a fallback into credential-hunting.
- **Secrets are never printed, logged, echoed, or committed** (env files excluded from version
  control). *(Mechanically checked: a crude grep for `print`/logging calls mentioning a
  PAT/token/secret variable name, and for `.env` missing from `.gitignore` when one exists.)*

## R5 -- Environment portability (warning)

No absolute machine paths, no host-specific CLIs or configs (e.g. `mcporter`), no virtualenv or
pre-installed-package assumptions. Any shipped scripts must run on a stock Python 3.9+
interpreter with stdlib only (or explicitly declare and check their dependencies). **Quick
screen:** a grep for `/Users/`, `mcporter`, and `.venv` over the skill should come back empty.
*(Mechanically checked -- this one's a literal grep, same as the quick-screen description.)*

## R6 -- Transport-invariant queries (warning)

The same VizQL/query JSON must work verbatim on every path the skill supports -- direct REST,
desktop-bridge extension, cloud connector (the MCP `query-datasource` tools are thin wrappers
over the VizQL Data Service). Parameters not accepted by a given transport (e.g. `limit` on
direct VDS calls) must be handled client-side rather than by forking the query format per
transport. **Not mechanically checkable** -- this requires reading the actual query-building code
across each transport path and confirming the query payload itself is identical, with only
transport-level parameter handling differing (a client-side slice on one path, a passed-through
argument on another, is fine; two different query JSON shapes per transport is the violation).

## R7 -- Failure semantics and auditability (warning)

- "Tableau unreachable / not authenticated" must be distinguishable from partial data and from
  skill bugs (distinct exit codes, statuses, or explicit statements), so callers can react
  correctly.
- The skill's output should record which connection path actually produced the run, so
  compliance is verifiable after the fact. **Not mechanically checkable** -- confirm by reading
  the skill's documented exit codes/status fields and checking that its output artifact (a
  report, a pack, a log) actually records the transport/path used, not just that the skill
  *could* record it.
