// The one place this app talks to the network.
//
// Every error becomes an ApiError carrying a message worth showing a user, so no
// component has to decide what a 422 body looks like.

import type {
  ModelChoice,
  ScanCreated,
  ScanDetail,
  ScanPage,
  Suite,
  Target,
  TargetFamily,
} from "./types";

const BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  readonly status: number;
  /** Field-specific validation messages, keyed by field name. */
  readonly fieldErrors: Record<string, string>;

  constructor(status: number, message: string, fieldErrors: Record<string, string> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.fieldErrors = fieldErrors;
  }
}

interface FastApiValidationItem {
  loc?: (string | number)[];
  msg?: string;
}

function parseError(status: number, body: unknown): ApiError {
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;

    if (typeof detail === "string") {
      return new ApiError(status, detail);
    }

    // FastAPI validation errors: a list of items with a `loc` path. Map them onto
    // field names so the form can show each message against its own input.
    if (Array.isArray(detail)) {
      const fieldErrors: Record<string, string> = {};
      const messages: string[] = [];
      for (const raw of detail as FastApiValidationItem[]) {
        const field = raw.loc?.filter((p) => p !== "body").join(".") ?? "";
        const msg = raw.msg ?? "Invalid value";
        messages.push(msg);
        if (field) fieldErrors[field] = msg;
      }
      return new ApiError(status, messages.join("; ") || "Validation failed", fieldErrors);
    }
  }
  return new ApiError(status, `Request failed with status ${status}`);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(init?.headers ?? {}),
      },
    });
  } catch {
    // A network failure is not an empty result, and must not be rendered as one.
    throw new ApiError(0, "Could not reach the API. Is the stack running?", {});
  }

  if (!response.ok) {
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      // No JSON body; the status alone has to carry the message.
    }
    throw parseError(response.status, body);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  suites: () => request<Suite[]>("/api/suites"),
  models: () => request<ModelChoice[]>("/api/models"),

  targets: () => request<TargetFamily[]>("/api/targets"),
  target: (id: string) => request<Target>(`/api/targets/${id}`),
  targetFamily: (id: string) => request<TargetFamily>(`/api/target-families/${id}`),

  createTarget: (input: {
    name: string;
    kind: "prompt";
    system_prompt: string;
    model: string;
  }) =>
    request<Target>("/api/targets", {
      method: "POST",
      body: JSON.stringify(input),
    }),

  createVersion: (
    targetId: string,
    input: { kind: "prompt"; system_prompt: string; model: string },
  ) =>
    request<Target>(`/api/targets/${targetId}/versions`, {
      method: "POST",
      body: JSON.stringify(input),
    }),

  scans: (cursor?: string | null) =>
    request<ScanPage>(`/api/scans${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`),

  scan: (id: string) => request<ScanDetail>(`/api/scans/${id}`),

  submitScan: (input: { target_id: string; suites: string[] }) =>
    request<ScanCreated>("/api/scans", {
      method: "POST",
      body: JSON.stringify(input),
    }),
};
