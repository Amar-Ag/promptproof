# Spec 003 — Reporting

What the user actually looks at. A scan takes minutes, so "waiting" is a designed state rather
than a spinner.

---

## R-003-1 — Scan list · MUST

**Story.** As a user returning to the tool, I want to see my scans and their outcomes at a
glance.

**Behaviour.** A reverse-chronological list of scans showing target name and version, suites
run, status, grade, exploited count, and duration. Paginated.

**Acceptance criteria**

- [ ] `GET /api/scans` returns scans newest first, with `limit` and `cursor` parameters and a
      default page size of 20.
- [ ] Each row shows target name, version, status, grade where present, exploited count, and
      duration; a running scan shows elapsed time instead of duration.
- [ ] The list renders distinct empty, loading and error states.
- [ ] A scan that is `failed` shows its reason in the list without the user opening it.
- [ ] Listing does not require an aggregate query over `findings` — satisfied by the
      denormalised `counts`.

---

## R-003-2 — Live progress · MUST

**Story.** As a user who just started a scan, I want to see it progressing and read findings as
they arrive, rather than staring at nothing for two minutes.

**Behaviour.** The scan view polls the scan and its findings while the scan is `pending` or
`running`, shows resolved/total progress, and appends findings as they are written. Polling
stops when the scan reaches a terminal state.

**Acceptance criteria**

- [ ] While `running`, the view shows `resolved / total` probe counts and updates without a
      manual refresh.
- [ ] Findings appear incrementally as they are written, not only at completion.
- [ ] Polling stops on `completed`, `failed` or `cancelled` — asserted by a frontend test
      counting requests after the terminal state.
- [ ] The polling interval is at least 2 seconds, and a failed poll backs off rather than
      hammering the API.
- [ ] A `pending` scan is clearly distinguished from a `running` one, so a stuck queue is
      visible to the user.

---

## R-003-3 — Report · MUST

**Story.** As a user, I want to know what got through, how bad it is, and what to change.

**Behaviour.** The report shows the grade, a per-suite breakdown, and the findings — exploited
first, then errored, then resisted — each expandable to show the payload, the response excerpt,
the judge's rationale, and remediation guidance. Metadata records the target version, corpus
version, judge model and timings.

**Acceptance criteria**

- [ ] The grade and per-suite exploited/resisted/errored counts are shown above the findings.
- [ ] Findings are ordered exploited, then errored, then resisted; within each group, by
      severity descending.
- [ ] Each finding can be expanded to show payload, response excerpt, judge rationale and
      remediation.
- [ ] Payload, excerpt and rationale are rendered as inert text — a finding whose content
      contains `<script>` or markdown image syntax renders literally, covered by a test.
- [ ] Errored findings state why they errored and are visually separated from resisted ones, so
      an error is never mistaken for a pass.
- [ ] Metadata shows target name and version, corpus version, judge model, and start/finish
      times.
- [ ] The report is reachable at a stable URL that can be shared and reloaded.

---

## R-003-4 — Compare versions · SHOULD

**Story.** As someone who just rewrote a prompt, I want to know whether it is actually better.

**Behaviour.** Given two scans of the same target family that ran overlapping suites, show the
grade change and, per probe, whether its outcome improved, regressed, or stayed the same.

**Acceptance criteria**

- [ ] `GET /api/scans/{a}/compare/{b}` returns per-probe `fixed`, `regressed`, `unchanged` or
      `not-comparable`, plus both grades.
- [ ] Comparing scans from different target families returns `400` with an explanation.
- [ ] Probes present in only one of the two scans are reported `not-comparable`, not silently
      dropped.
- [ ] The UI shows regressions first, because those are the reason to look.
- [ ] A probe that errored in either scan is `not-comparable` rather than counted as fixed.
