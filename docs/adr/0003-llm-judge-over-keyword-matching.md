# ADR-0003 — An LLM judge with declared success criteria, not keyword matching

- **Status:** Accepted
- **Date:** 2026-11-09
- **Deciders:** architect, probe-author, human

## Context

Something has to decide whether a probe succeeded. The target's response is free text, and
"success" means *the model followed the attacker instead of its own instructions* — which is a
semantic property, not a string property.

## Decision

Each probe declares `success_criteria` in natural language. An independent judge model, from a
different model family than the default target, is given the payload, the response and those
criteria, and returns a structured verdict: outcome, rationale, confidence.

## Consequences

**Good**

- Catches compliance expressed as paraphrase, summary, or partial leak — which is how it
  actually shows up.
- Avoids the worst false positive in this domain: a model that *refuses* while quoting the
  attack back, which trips any keyword check looking for the attack's own wording.
- The rationale is stored, so a human can disagree with the verdict on evidence. A regex gives
  no rationale to argue with.
- Criteria live next to the payload in the probe file, so adding a probe does not mean writing
  detection code.

**Bad**

- Verdicts are non-deterministic, so two runs of the same scan can differ. Mitigated by
  recording the judge model and corpus version on every scan, and by treating a grade as a
  signal rather than a measurement.
- Doubles model calls per probe, and doubles the exposure to rate limits.
- The judge is now a component that can be wrong, so it needs its own fixture tests:
  a recorded refusal must judge `resisted`, a recorded compliant paraphrase must judge
  `exploited`.
- Judge failure has to be a first-class outcome (`errored`), distinct from a pass.

## Alternatives rejected

- **Keyword or regex matching** — false negatives on paraphrase, false positives on
  attack-quoting refusals. Cheap and wrong.
- **The target model judging itself** — a model grading its own output is not evidence. This is
  why the judge is pinned to a different family, asserted by a configuration test.
- **Human review only** — accurate and unusable; the whole point is that this runs on every
  prompt edit.
