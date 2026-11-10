## Issue

Closes #

> Repeat the keyword for every issue: `Closes #1, closes #2, closes #3`.
> GitHub honours only the first issue in a bare `Closes #1, #2, #3` list and
> silently leaves the rest open.

Spec requirement: `R-00?-?`

## Acceptance criteria

Reproduce the issue's checklist here and give evidence against each — the test, the commit, or
what you observed. A reviewer should be able to verify acceptance without reading the diff
twice.

- [ ] …

## Tests

- What was added:
- **Verified to fail against the pre-change code:** yes / no — how:
- `make test` passes locally: yes / no

## Hard rules check

- [ ] No secret or user content added to a log, span attribute, or metric label
- [ ] Outbound requests to user-supplied URLs go through the SSRF guard
- [ ] No probe payload is evaluated, shell-interpolated, or templated into SQL
- [ ] Trace context still survives the API → worker boundary
- [ ] No applied migration was edited
- [ ] `make up` is still one command
- [ ] Docs affected by this change are updated in this PR

## Deliberately not done

What was left out, and why.

## Role

Authored by: `role`
Reviewed by: `reviewer` (must not be the author)
