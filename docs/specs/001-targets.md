# Spec 001 — Targets

A **target** is the thing under test. Targets are immutable: editing one creates a new version
in the same family, so that any report stays reproducible and versions can be compared.

---

## R-001-1 — Register a prompt target · MUST

**Story.** As someone who maintains a system prompt, I want to register that prompt and the
model it runs on, so I can scan it without wiring promptproof into my application.

**Behaviour.** The user supplies a name, the system prompt text, and a model identifier.
promptproof stores it as version 1 of a new target family. The model identifier is validated
against the allowed model list; an unknown model is rejected with a message naming the
permitted values.

**Acceptance criteria**

- [ ] `POST /api/targets` with name, `kind=prompt`, `system_prompt` and `model` returns `201`
      and a body containing the target id, `family_id` and `version: 1`.
- [ ] A system prompt shorter than 1 character or longer than 20,000 characters is rejected
      with `422` and a message identifying the field.
- [ ] An unrecognised model is rejected with `422` listing the allowed models.
- [ ] A name that is empty or longer than 200 characters is rejected with `422`.
- [ ] The created target is returned by `GET /api/targets` and `GET /api/targets/{id}`.
- [ ] The system prompt text does not appear in any log line, span attribute, or metric label
      produced while handling the request.

---

## R-001-2 — Register an endpoint target · SHOULD

**Story.** As someone running an LLM app, I want to scan the deployed endpoint rather than the
prompt in isolation, so the scan covers my retrieval and tooling too.

**Behaviour.** The user supplies a name, an HTTPS URL, an optional auth header, and must
explicitly confirm they own or are authorised to test the endpoint. The URL is validated by the
SSRF guard at registration time and again before every request.

**Acceptance criteria**

- [ ] `POST /api/targets` with `kind=endpoint` requires `endpoint_url` and
      `ownership_confirmed: true`; omitting the confirmation returns `422` with an explanation.
- [ ] A URL whose scheme is not `https` is rejected, except when `SSRF_ALLOW_PRIVATE` is set,
      which is permitted only in development.
- [ ] A URL resolving to loopback, link-local, private, multicast, or reserved address space is
      rejected with `422` and a message that does not leak the resolved address.
- [ ] A stored auth header is never returned by any `GET` endpoint; the response shows only
      whether one is set.
- [ ] The auth header value does not appear in any log line, span attribute, or metric label.

---

## R-001-3 — Version a target · MUST

**Story.** As someone iterating on a prompt, I want to edit it and keep the old version, so I
can prove a change reduced exploitability.

**Behaviour.** Editing a target inserts a new row in the same family with `version + 1`.
Existing versions are never mutated or deleted. Scans always reference a specific version.

**Acceptance criteria**

- [ ] `POST /api/targets/{id}/versions` creates a new version in the same `family_id` with
      `version` incremented by exactly 1, and returns `201`.
- [ ] The previous version remains retrievable and byte-identical afterwards.
- [ ] `GET /api/targets/{id}` on an older version still returns its original prompt text.
- [ ] `GET /api/target-families/{family_id}` lists every version in descending version order.
- [ ] There is no API route that mutates or deletes an existing target version.
- [ ] Concurrent version creation on one family cannot produce two rows with the same version
      number — proven by an integration test issuing two simultaneous requests.

---

## R-001-4 — Manage targets in the UI · MUST

**Story.** As a user, I want to create and browse targets in the browser rather than with curl.

**Behaviour.** A list of target families showing name, kind, latest version and most recent
grade; a form to create a prompt target; a detail view listing versions with a button to scan
a given version.

**Acceptance criteria**

- [ ] The target list renders distinct empty, loading and error states — an API failure shows
      an actionable message, not an endless spinner.
- [ ] The create form reports server-side validation errors against the specific field.
- [ ] Submitting the form twice rapidly creates exactly one target.
- [ ] System prompt text is rendered as inert text, never as HTML or markdown.
- [ ] The detail view shows each version with its creation time and lets the user start a scan
      against any version, not only the latest.
