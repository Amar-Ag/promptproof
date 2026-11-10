import type { Grade, Outcome, ScanStatus, Severity } from "../api/types";

export function Loading({ what }: { what: string }): React.JSX.Element {
  return (
    <p className="state state--loading" role="status">
      Loading {what}…
    </p>
  );
}

/** An API failure is not an empty result and must never look like one. */
export function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}): React.JSX.Element {
  return (
    <div className="state state--error" role="alert">
      <p>{message}</p>
      {onRetry ? (
        <button type="button" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  );
}

export function EmptyState({
  title,
  hint,
}: {
  title: string;
  hint?: string;
}): React.JSX.Element {
  return (
    <div className="state state--empty">
      <p className="state__title">{title}</p>
      {hint ? <p className="state__hint">{hint}</p> : null}
    </div>
  );
}

export function GradeBadge({ grade }: { grade: Grade | null }): React.JSX.Element {
  if (grade === null) {
    return (
      <span className="grade grade--none" title="Nothing was determined — every probe errored">
        —
      </span>
    );
  }
  return (
    <span className={`grade grade--${grade.toLowerCase()}`} aria-label={`Grade ${grade}`}>
      {grade}
    </span>
  );
}

const STATUS_LABEL: Record<ScanStatus, string> = {
  pending: "Queued",
  running: "Running",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

/**
 * `pending` and `running` are deliberately distinct: a queued scan that has not
 * started is how a stalled queue becomes visible to the user, not just to us.
 */
export function StatusTag({ status }: { status: ScanStatus }): React.JSX.Element {
  return <span className={`tag tag--${status}`}>{STATUS_LABEL[status]}</span>;
}

export function SeverityTag({ severity }: { severity: Severity }): React.JSX.Element {
  return <span className={`tag tag--sev-${severity}`}>{severity}</span>;
}

const OUTCOME_LABEL: Record<Outcome, string> = {
  exploited: "Exploited",
  resisted: "Resisted",
  errored: "Not determined",
};

export function OutcomeTag({ outcome }: { outcome: Outcome }): React.JSX.Element {
  return <span className={`tag tag--${outcome}`}>{OUTCOME_LABEL[outcome]}</span>;
}

/**
 * Renders attacker-authored text and raw model output.
 *
 * This content is hostile by construction — one whole probe suite is about
 * exfiltration through rendered markdown images — so it is rendered as plain
 * text and never as HTML or markdown. React escapes children by default; this
 * component exists so that the intent is explicit and any future change here is
 * obvious in review.
 */
export function InertText({ children }: { children: string }): React.JSX.Element {
  return <pre className="inert">{children}</pre>;
}

export function Progress({
  resolved,
  total,
}: {
  resolved: number;
  total: number;
}): React.JSX.Element {
  const pct = total > 0 ? Math.round((resolved / total) * 100) : 0;
  return (
    <div className="progress">
      <div className="progress__bar">
        <div className="progress__fill" style={{ width: `${pct}%` }} />
      </div>
      <span className="progress__label">
        {resolved} / {total} probes
      </span>
    </div>
  );
}

export function elapsed(from: string, to: string | null): string {
  const start = new Date(from).getTime();
  const end = to ? new Date(to).getTime() : Date.now();
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${seconds % 60}s`;
}
