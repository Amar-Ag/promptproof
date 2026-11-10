import { useState } from "react";
import { useParams } from "react-router-dom";

import { api } from "../api/client";
import type { Finding, ScanDetail } from "../api/types";
import { isTerminal } from "../api/types";
import {
  ErrorState,
  GradeBadge,
  InertText,
  Loading,
  OutcomeTag,
  Progress,
  SeverityTag,
  StatusTag,
  elapsed,
} from "../components/States";
import { usePoll } from "../hooks";

export function ScanPage(): React.JSX.Element {
  const { scanId = "" } = useParams();

  // Polls while the scan is live and stops as soon as it reaches a terminal
  // state — see `usePoll`.
  const { result, reload } = usePoll<ScanDetail>(
    () => api.scan(scanId),
    (data) => isTerminal(data.scan.status),
    [scanId],
  );

  if (result.state === "loading") return <Loading what="scan" />;
  if (result.state === "error") {
    return <ErrorState message={result.message} onRetry={reload} />;
  }

  const { scan, resolved_count: resolved, findings } = result.data;
  const live = scan.status === "pending" || scan.status === "running";

  const exploitedTotal = scan.counts
    ? Object.values(scan.counts.exploited).reduce((a, b) => a + b, 0)
    : 0;

  return (
    <>
      <section className="panel">
        <header className="panel__header">
          <h1>
            {scan.target_name} <span className="muted">v{scan.target_version}</span>
          </h1>
          <StatusTag status={scan.status} />
        </header>

        {scan.status === "pending" ? (
          <p className="notice">
            Queued, not started yet. If this persists, no worker is picking up work.
          </p>
        ) : null}

        {scan.status === "failed" && scan.error ? (
          <p className="notice notice--error" role="alert">
            {scan.error}
          </p>
        ) : null}

        {live ? (
          <>
            <Progress resolved={resolved} total={scan.probe_count} />
            <p className="muted">{elapsed(scan.queued_at, null)} elapsed</p>
          </>
        ) : null}

        {!live ? (
          <div className="summary">
            <div className="summary__grade">
              <GradeBadge grade={scan.grade} />
              <span className="muted">
                {scan.grade === null
                  ? "No grade: nothing was determined"
                  : `${exploitedTotal} of ${scan.counts?.total ?? 0} probes got through`}
              </span>
            </div>
            {scan.counts ? <SuiteBreakdown counts={scan.counts.by_suite} /> : null}
          </div>
        ) : null}

        <dl className="meta">
          <div>
            <dt>Corpus</dt>
            <dd>{scan.probe_corpus_version ?? "—"}</dd>
          </div>
          <div>
            <dt>Judge</dt>
            <dd>{scan.judge_model ?? "—"}</dd>
          </div>
          <div>
            <dt>Suites</dt>
            <dd>{scan.suites.join(", ")}</dd>
          </div>
          <div>
            <dt>Queued</dt>
            <dd>{new Date(scan.queued_at).toLocaleString()}</dd>
          </div>
          <div>
            <dt>Finished</dt>
            <dd>
              {scan.finished_at ? new Date(scan.finished_at).toLocaleString() : "—"}
            </dd>
          </div>
          <div>
            <dt>Attempts</dt>
            <dd>{scan.attempts}</dd>
          </div>
        </dl>
      </section>

      <section className="panel">
        <h2>Findings</h2>
        {findings.length === 0 ? (
          <p className="muted">
            {live ? "Findings appear here as each probe resolves." : "No findings recorded."}
          </p>
        ) : (
          <ul className="findings">
            {findings.map((finding) => (
              <FindingRow key={finding.id} finding={finding} />
            ))}
          </ul>
        )}
      </section>
    </>
  );
}

function SuiteBreakdown({
  counts,
}: {
  counts: Record<string, { exploited: number; resisted: number; errored: number }>;
}): React.JSX.Element {
  return (
    <table className="table table--compact">
      <thead>
        <tr>
          <th>Suite</th>
          <th>Exploited</th>
          <th>Resisted</th>
          <th>Not determined</th>
        </tr>
      </thead>
      <tbody>
        {Object.entries(counts).map(([suite, c]) => (
          <tr key={suite}>
            <td>{suite}</td>
            <td className={c.exploited > 0 ? "bad" : ""}>{c.exploited}</td>
            <td>{c.resisted}</td>
            <td className={c.errored > 0 ? "warn" : ""}>{c.errored}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function FindingRow({ finding }: { finding: Finding }): React.JSX.Element {
  const [open, setOpen] = useState(finding.outcome === "exploited");

  return (
    <li className={`finding finding--${finding.outcome}`}>
      <button
        type="button"
        className="finding__head"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <OutcomeTag outcome={finding.outcome} />
        <SeverityTag severity={finding.severity} />
        <span className="finding__id">{finding.probe_id}</span>
        <span className="muted">{finding.suite}</span>
      </button>

      {open ? (
        <div className="finding__body">
          {finding.outcome === "errored" ? (
            <p className="notice notice--warn">
              Not determined: {finding.error_reason ?? "unknown reason"}. This is not a pass.
            </p>
          ) : null}

          {finding.judge_rationale ? (
            <>
              <h3>Why</h3>
              <InertText>{finding.judge_rationale}</InertText>
            </>
          ) : null}

          {finding.response_excerpt ? (
            <>
              <h3>Response excerpt</h3>
              <InertText>{finding.response_excerpt}</InertText>
            </>
          ) : null}

          <p className="muted">
            target {finding.target_latency_ms ?? "—"}ms · judge{" "}
            {finding.judge_latency_ms ?? "—"}ms
            {finding.judge_confidence !== null
              ? ` · confidence ${finding.judge_confidence}`
              : ""}
          </p>
        </div>
      ) : null}
    </li>
  );
}
