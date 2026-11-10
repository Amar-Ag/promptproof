import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { ApiError, api } from "../api/client";
import type { Suite, TargetFamily } from "../api/types";
import { EmptyState, ErrorState, InertText, Loading } from "../components/States";
import { useAsync } from "../hooks";

export function TargetPage(): React.JSX.Element {
  const { familyId = "" } = useParams();
  const { result, reload } = useAsync<TargetFamily>(
    () => api.targetFamily(familyId),
    [familyId],
  );
  const suites = useAsync<Suite[]>(() => api.suites());

  if (result.state === "loading") return <Loading what="target" />;
  if (result.state === "error") {
    return <ErrorState message={result.message} onRetry={reload} />;
  }

  const family = result.data;

  return (
    <>
      <section className="panel">
        <h1>{family.name}</h1>
        <p className="lede">
          {family.versions.length} version{family.versions.length === 1 ? "" : "s"}. Scan any
          version, not only the latest — that is how you compare a rewrite against what you
          shipped.
        </p>
      </section>

      {suites.result.state === "error" ? (
        <ErrorState message={suites.result.message} onRetry={suites.reload} />
      ) : null}

      {family.versions.length === 0 ? (
        <EmptyState title="This target has no versions." />
      ) : (
        family.versions.map((target) => (
          <section className="panel" key={target.id}>
            <header className="panel__header">
              <h2>Version {target.version}</h2>
              <span className="muted">
                {new Date(target.created_at).toLocaleString()} · {target.model}
              </span>
            </header>

            {target.system_prompt ? <InertText>{target.system_prompt}</InertText> : null}

            <StartScan
              targetId={target.id}
              suites={suites.result.state === "loaded" ? suites.result.data : []}
            />
          </section>
        ))
      )}
    </>
  );
}

function StartScan({
  targetId,
  suites,
}: {
  targetId: string;
  suites: Suite[];
}): React.JSX.Element {
  const navigate = useNavigate();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Default to everything that has probes, but let the user narrow it.
  const effective = selected.size > 0 ? [...selected] : suites.map((s) => s.key);

  function toggle(key: string): void {
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  async function start(): Promise<void> {
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const created = await api.submitScan({ target_id: targetId, suites: effective });
      navigate(`/scans/${created.id}`);
    } catch (caught: unknown) {
      setError(caught instanceof ApiError ? caught.message : "Something went wrong.");
      setSubmitting(false);
    }
  }

  const probeCount = suites
    .filter((s) => effective.includes(s.key))
    .reduce((sum, s) => sum + s.probe_count, 0);

  return (
    <div className="start-scan">
      <fieldset>
        <legend>Suites</legend>
        {suites.length === 0 ? <span className="muted">Loading suites…</span> : null}
        {suites.map((suite) => (
          <label key={suite.key} className="checkbox" title={suite.description}>
            <input
              type="checkbox"
              checked={selected.size === 0 || selected.has(suite.key)}
              onChange={() => toggle(suite.key)}
            />
            {suite.title} <span className="muted">({suite.probe_count})</span>
          </label>
        ))}
      </fieldset>

      <div className="start-scan__actions">
        <button type="button" onClick={start} disabled={submitting || probeCount === 0}>
          {submitting ? "Starting…" : `Run scan (${probeCount} probes)`}
        </button>
        {error ? (
          <p className="form__error" role="alert">
            {error}
          </p>
        ) : null}
      </div>
    </div>
  );
}
