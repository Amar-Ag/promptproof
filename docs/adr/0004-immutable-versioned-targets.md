# ADR-0004 — Targets are immutable and versioned

- **Status:** Accepted
- **Date:** 2026-11-09
- **Deciders:** architect, spec-analyst, human

## Context

The product's value is comparison: *is this prompt less exploitable than the one we shipped last
week?* That requires knowing exactly which prompt text produced a given report.

If editing a target mutated it in place, every historical report would silently start
describing a prompt that no longer exists.

## Decision

A `targets` row is immutable once written. Editing inserts a new row in the same
`target_families` lineage with `version + 1`. Scans reference a specific target version. There
is no API route that updates or deletes a target version.

## Consequences

**Good**

- Reports are permanently reproducible — the prompt text that produced them is still there.
- Version comparison ([R-003-4](../specs/003-reporting.md)) falls out of the data model instead
  of needing an audit log.
- No destructive edit path means no accidental loss of the thing you were trying to improve.

**Bad**

- Storage grows with every edit. A 20 KB prompt cap and realistic edit rates make this
  irrelevant.
- The UI has to make "latest version" explicit rather than implicit, and scans must let the
  user pick a version — slightly more interface.
- Version allocation needs concurrency protection: a unique constraint on
  `(family_id, version)`, with the race covered by an integration test.
- "Delete a target" becomes "delete a family", cascading its versions, scans and findings.

## Alternatives rejected

- **Mutable targets with an audit table** — two sources of truth for prompt text, and reports
  would point at the mutable one.
- **Content-addressed targets (hash of the prompt)** — deduplicates nicely but loses ordering
  and lineage, which are exactly what comparison needs.
