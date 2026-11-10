// Data fetching lives here; components render state.

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "./api/client";

export type Async<T> =
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "loaded"; data: T };

/** Fetch once, with explicit loading and error states and a manual refresh. */
export function useAsync<T>(fetcher: () => Promise<T>, deps: unknown[] = []): {
  result: Async<T>;
  reload: () => void;
} {
  const [result, setResult] = useState<Async<T>>({ state: "loading" });
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setResult({ state: "loading" });

    fetcher()
      .then((data) => {
        if (!cancelled) setResult({ state: "loaded", data });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setResult({
          state: "error",
          message: error instanceof ApiError ? error.message : "Something went wrong.",
        });
      });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { result, reload };
}

const MIN_INTERVAL_MS = 2000;
const MAX_INTERVAL_MS = 30000;

/**
 * Poll until `isDone` says to stop.
 *
 * Two behaviours are load-bearing and both are asserted by tests: polling stops
 * at a terminal state rather than running forever, and a failed poll backs off
 * instead of hammering an API that is already struggling.
 */
export function usePoll<T>(
  fetcher: () => Promise<T>,
  isDone: (data: T) => boolean,
  deps: unknown[] = [],
): { result: Async<T>; reload: () => void } {
  const [result, setResult] = useState<Async<T>>({ state: "loading" });
  const [nonce, setNonce] = useState(0);
  const intervalRef = useRef(MIN_INTERVAL_MS);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    intervalRef.current = MIN_INTERVAL_MS;
    setResult({ state: "loading" });

    const tick = async (): Promise<void> => {
      try {
        const data = await fetcher();
        if (cancelled) return;
        setResult({ state: "loaded", data });
        intervalRef.current = MIN_INTERVAL_MS;

        if (isDone(data)) return; // Terminal: schedule nothing further.
        timer = setTimeout(tick, intervalRef.current);
      } catch (error: unknown) {
        if (cancelled) return;
        setResult({
          state: "error",
          message: error instanceof ApiError ? error.message : "Something went wrong.",
        });
        intervalRef.current = Math.min(intervalRef.current * 2, MAX_INTERVAL_MS);
        timer = setTimeout(tick, intervalRef.current);
      }
    };

    void tick();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { result, reload };
}
