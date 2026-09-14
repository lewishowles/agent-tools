# Codex insights migration

## Why this exists

The unfinished Codex insights roadmap previously lived in the `Configuration/Agents` progress project. `agent-insights` now owns that future work. This note preserves the useful evidence and unfinished requirements before the old tasks are removed.

The existing implementation is a prototype and source of verified parsing lessons. It is not the architecture to move wholesale.

## Source implementation

The prototype lives in `~/Dev/Configuration/Agents`:

- `src/skills/codex-insights/scripts/codex_insights_extract.py`
- `src/skills/codex-insights/scripts/codex_insights_facets.py`
- `src/skills/codex-insights/scripts/codex_insights_facets_common.py`
- `src/skills/codex-insights/scripts/codex_insights_facets_observations.py`
- `src/skills/codex-insights/scripts/codex_insights_facets_patterns.py`
- `src/skills/codex-insights/scripts/codex_insights_facets_narrative.py`
- `src/skills/codex-insights/scripts/codex_insights_render.py`
- `src/skills/codex-insights/scripts/codex_insights_author.py`
- `scripts/audit/token_usage_*.py`
- `src/skills/codex-insights/`
- `src/skills/insights-review/`

Generated reports and diagnostics under `.agent/` are evidence, not source to migrate.

## Completed foundations to keep

Source task: `tsk_YsFS4gvSRujl9iRdQ4KgkA`, **Rework how codex-insights builds and groups behaviour patterns**.

The completed work established these useful rules:

- Detect corrections and approach changes only from authored `event_msg.payload.type == "user_message"` evidence. Never treat injected rules, skills, environment text, or transcript assessment as a user correction.
- Describe an observation with a typed structure: canonical action, tool, optional target type and value, repository, and outcome.
- Resolve targets only from unambiguous structured fields. A missing target is safer than an inferred target.
- Group patterns by `(kind, action, target type, target value, repository)` instead of lightly normalised raw payload text.
- Make file targets repository-relative and normalise only structured path values. Do not canonicalise free text that no longer belongs in the grouping key.
- Associate a correction or approach change with the preceding tool action when the relationship is structurally available. Fall back to a generic group when it is not.
- Keep promotion based on at least two distinct conversations, not repeated events inside one conversation.
- Preserve valid evidence references and the extraction, facets, and narrative provenance chain where those concepts remain useful.

### Codex wrapper parsing lessons

Codex tool calls in the measured corpus were wrapped in JavaScript passed to a generic `exec` event. The useful prototype logic learned to recover the real tool name and structured argument from forms such as:

- An inline call: `tools.exec_command({"cmd": "..."})`
- A variable followed by a call: `const patch = "..."; tools.apply_patch(patch)`

The parser must use deterministic JSON decoding of the wrapper's serialised literal. It must not parse shell arguments or arbitrary JavaScript as free text. When one event contains several tool calls, attributing the event-level status to one call is ambiguous, so the target must remain unresolved.

Measured prototype results showed why this matters:

- Inline-only parsing resolved very few real `apply_patch` and `exec_command` records.
- Supporting a variable assigned to a JSON literal substantially improved `apply_patch` resolution.
- Multi-call wrappers made event-level status attribution unsafe, so they must degrade to no target rather than select the first call.

### Known repository identity limitation

The prototype reduced a repository to its checkout directory name. Two unrelated repositories with the same directory name can merge, while two clones under different names can split. `agent-insights` must use the repository identity contract established for `agent-tools` instead.

## Unfinished requirements to reconsider

Source task: `tsk_r7QRWCojK-lJu8imbnU-3w`, **Rework which codex-insights findings are promoted and how they rank**.

One chunk was completed before the task stopped:

- Remove `successful_behaviour` based only on a tool exiting successfully.
- Define `recovery` only when a friction event is followed by a resolving action in the same conversation.
- Promote recovery only when it meets the normal distinct-conversation threshold.

Two chunks remained unfinished:

### Rank findings by recurrence

- Order findings by distinct conversations descending, then occurrences descending, then a fixed kind priority.
- Prefer this order over lexical sorting by kind and key.
- Carry distinct-conversation and occurrence counts onto each finding.
- Use a resolved structured target when one exists; otherwise retain an explicit generic label.
- Test ordering and the kind-priority tie-break deterministically.

The prototype proposed this priority:

1. correction
2. approach change
3. verification gap
4. retry
5. tool failure
6. rollback
7. interruption
8. configuration touch
9. recovery

This ordering is a hypothesis, not a permanent product rule. `agent-insights` should verify it against representative reports before adopting it.

### Render frequency and explain the model

- Show distinct-conversation and occurrence counts for every finding.
- Explain the typed descriptor, grouping key, supported kinds, recovery definition, and ranking rule in the consuming skill.
- Keep extraction and calculation in the CLI. The skill should interpret the resulting evidence rather than own the report pipeline.

## Shelved work to discard

Source task: `tsk_5fN_M7O_Pxx8pny9eZWSYw`, **Name a specific target for more Codex insights finding kinds**.

The task tried to reuse existing target and configuration status for retry, tool failure, successful behaviour, and ledger-derived verification gaps. A corpus check found that these targets were raw command or tool payloads, and apparent configuration matches were incidental filenames inside arguments. The task was shelved because a wrong target is worse than a generic label.

Do not revive this task. The typed descriptor and structure-only extraction work supersedes it. A future target may be added only when the relevant adapter exposes an unambiguous structured relationship.

## Other unfinished insights tasks

These tasks lived outside the Codex insights release but belong to the same future `agent-insights` product. Their useful requirements move here; their old progress records should not remain as competing plans.

### Reproducible audit metrics

Source task: `tsk_guOQY7Ln2LsgevA1ddPpZA`, **Make the audit metrics reproducible**.

Useful requirements:

- Accept absolute `--since` and `--until` bounds, with relative windows as a convenience.
- Produce identical figures for identical fully elapsed bounds and unchanged source data.
- Provide machine-readable metrics for tool activity, payload size, and repeated calls without exposing raw results.
- Report empty and partial windows explicitly.
- Keep image sizes and heuristic command classifications descriptive rather than presenting them as exact token usage.

These requirements belong in the `agent-insights` index and report tests. The five separate audit scripts and a stored ignored-file baseline do not need to survive.

### Past remediation proposals

Source task: `tsk_gVRD83MU2PJiaFByO1cKDw`, **Close the actionable insights-review gaps**.

The task proposed five configuration changes based on an earlier audit:

- Verify unfamiliar contracts against authoritative evidence.
- Query Git immediately before reporting Git state.
- Delegate work already beyond the chosen threshold.
- Close and restart delegation cycles explicitly.
- Require convergence evidence for deterministic transformations.
- Check that proposed guidance could have intercepted the original failure.

These are old recommendations, not requirements for `agent-insights`. Do not apply them automatically or retain them as pending configuration work. A future report may propose an equivalent change only when current evidence supports it and `insights-review` accepts it through its normal human-review process.

### Claude skill-usage telemetry

Source task: `tsk_RIhxmDgQsK3G0nYoyqVAaA`, **Capture Claude skill-usage telemetry**.

The proposed hook was blocked because Claude exposed no reliable event for every automatic skill invocation. The task explicitly rejected transcript scraping as a substitute and warned that manual invocations would be invisible, so zero observations could not mean a skill was unused.

Do not build the proposed hook or TSV log. `agent-insights` may report skill usage only when a runtime adapter has authoritative structured evidence for that invocation. Otherwise it must report the measure as unsupported. Reconsider only when a runtime provides a proven invocation event or equivalent structured record.

### Cross-runtime token and activity report

Source task: `tsk_Mx8jO8Bc4VU7pxIXd8uZFQ`, **Attribute Claude and Codex token usage for improvement triage**.

Useful requirements:

- Keep Claude and Codex runtimes distinct from the tools they call.
- Reconcile exact counters with an explicit unattributed bucket.
- Label payload sizes and context-growth calculations as estimates when they are not runtime token counters.
- Report failures, repetition, delegation, empty windows, and partial data without copying raw conversation content.
- Produce stable Markdown and JSON for a fixed elapsed window.
- Allow fixtures to redirect transcript roots through `CLAUDE_CONFIG_DIR` and `CODEX_HOME` rather than reading real sessions.

This belongs in the `agent-insights` CLI and its fixtures, not in a portable skill-owned report. The interpretation skill must consume the CLI output.

## Migration approach

1. Freeze the existing Codex insights implementation. Change it only when a focused fix is needed to understand or extract proven behaviour.
2. Inventory each extractor, calculation, fixture, and report field by the question it answers.
3. Reuse verified parsers, field mappings, redacted fixtures, and calculations that fit the `agent-insights` adapter and normalised-record contracts.
4. Re-test every reused parser against current Claude or Codex records. Stored formats are not stable merely because the prototype once parsed them.
5. Do not carry over the current facet-module layout, authoring pipeline, renderer, command surface, or skill-owned orchestration by default.
6. Compare the new bounded output with representative existing reports for every retained measure.
7. Remove the old scripts, obsolete skills, generated audit machinery, and configuration references only after the new installed tool produces equivalent retained evidence.
8. Keep one small interpretation skill that consumes `agent-insights` JSON and proposes human-reviewed improvements.

## Retirement gate

The old implementation can be removed when:

- Codex and Claude adapters pass current synthetic, redacted, and representative local fixtures.
- Repeated runs skip unchanged sessions.
- Every retained measure names its source and partial-data state.
- Correction evidence cannot be created from injected instructions.
- Structure-only targets degrade safely when attribution is ambiguous.
- Recurrence counts and ranking are visible and deterministic.
- A stored analysis can be shown without reparsing source sessions.
- The installed `agent-insights` command has been used to produce and inspect one representative report.
- The replacement skill consumes structured output and contains no duplicate extraction or rendering pipeline.

Anything not needed to pass this gate should be removed rather than migrated.
