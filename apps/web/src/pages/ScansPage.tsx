import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { ScanPage as ScanPageData } from "../api/types";
import {
  EmptyState,
  ErrorState,
  GradeBadge,
  Loading,
  StatusTag,
  elapsed,
} from "../components/States";
import { useAsync } from "../hooks";

export function ScansPage(): React.JSX.Element {
  const { result, reload } = useAsync<ScanPageData>(() => api.scans());

  return (
    <section className="panel">
      <h1>Scans</h1>

      {result.state === "loading" ? <Loading what="scans" /> : null}
      {result.state === "error" ? (
        <ErrorState message={result.message} onRetry={reload} />
      ) : null}
      {result.state === "loaded" && result.data.items.length === 0 ? (
        <EmptyState
          title="No scans yet."
          hint="Open a target and run one to see results here."
        />
      ) : null}

      {result.state === "loaded" && result.data.items.length > 0 ? (
        <table className="table">
          <thead>
            <tr>
              <th>Target</th>
              <th>Suites</th>
              <th>Status</th>
              <th>Grade</th>
              <th>Exploited</th>
              <th>Duration</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {result.data.items.map((scan) => {
              const exploited = scan.counts
                ? Object.values(scan.counts.exploited).reduce((a, b) => a + b, 0)
                : null;
              const running = scan.status === "pending" || scan.status === "running";
              return (
                <tr key={scan.id}>
                  <td>
                    {scan.target_name} <span className="muted">v{scan.target_version}</span>
                  </td>
                  <td className="muted">{scan.suites.length}</td>
                  <td>
                    <StatusTag status={scan.status} />
                    {/* A failed scan shows its reason here, without being opened. */}
                    {scan.error ? <p className="row-error">{scan.error}</p> : null}
                  </td>
                  <td>
                    <GradeBadge grade={scan.grade} />
                  </td>
                  <td>{exploited === null ? "—" : exploited}</td>
                  <td className="muted">
                    {running
                      ? `${elapsed(scan.queued_at, null)} elapsed`
                      : elapsed(scan.started_at ?? scan.queued_at, scan.finished_at)}
                  </td>
                  <td>
                    <Link to={`/scans/${scan.id}`}>Open</Link>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : null}
    </section>
  );
}
