// Response shapes from the API.
//
// Hand-written for now and kept deliberately narrow. Generating these from the
// OpenAPI schema is part of issue #7's acceptance criteria and lands with the
// client generator; until then, any drift shows up as a type error here rather
// than as a runtime surprise.

export type TargetKind = "prompt" | "endpoint";
export type ScanStatus = "pending" | "running" | "completed" | "failed" | "cancelled";
export type Outcome = "exploited" | "resisted" | "errored";
export type Severity = "low" | "medium" | "high" | "critical";
export type Grade = "A" | "B" | "C" | "D" | "F";

export interface Suite {
  key: string;
  title: string;
  description: string;
  probe_count: number;
}

export interface ModelChoice {
  id: string;
  label: string;
}

export interface Target {
  id: string;
  family_id: string;
  name: string;
  version: number;
  kind: TargetKind;
  model: string | null;
  system_prompt: string | null;
  endpoint_url: string | null;
  has_auth_header: boolean;
  ownership_confirmed: boolean;
  created_at: string;
}

export interface TargetFamily {
  id: string;
  name: string;
  created_at: string;
  latest_version: number;
  kind: TargetKind;
  versions: Target[];
}

export interface SeverityCounts {
  low: number;
  medium: number;
  high: number;
  critical: number;
}

export interface ScanCounts {
  total: number;
  exploited: SeverityCounts;
  resisted: SeverityCounts;
  errored: number;
  by_suite: Record<string, { exploited: number; resisted: number; errored: number }>;
}

export interface Scan {
  id: string;
  target_id: string;
  target_name: string;
  target_version: number;
  suites: string[];
  status: ScanStatus;
  grade: Grade | null;
  counts: ScanCounts | null;
  probe_count: number;
  attempts: number;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  probe_corpus_version: string | null;
  judge_model: string | null;
}

export interface Finding {
  id: string;
  probe_id: string;
  suite: string;
  severity: Severity;
  outcome: Outcome;
  response_excerpt: string | null;
  judge_rationale: string | null;
  judge_confidence: number | null;
  error_reason: string | null;
  target_latency_ms: number | null;
  judge_latency_ms: number | null;
  created_at: string;
}

export interface ScanDetail {
  scan: Scan;
  resolved_count: number;
  findings: Finding[];
}

export interface ScanPage {
  items: Scan[];
  next_cursor: string | null;
}

export interface ScanCreated {
  id: string;
  status: ScanStatus;
  probe_count: number;
}

export const TERMINAL_STATUSES: readonly ScanStatus[] = [
  "completed",
  "failed",
  "cancelled",
];

export function isTerminal(status: ScanStatus): boolean {
  return TERMINAL_STATUSES.includes(status);
}
