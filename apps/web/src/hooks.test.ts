import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./api/client";
import { usePoll } from "./hooks";

describe("usePoll", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  it("stops polling once the data reaches a terminal state", async () => {
    // Running, running, then completed. Nothing should be requested after that.
    const statuses = ["running", "running", "completed"];
    let calls = 0;
    const fetcher = vi.fn(async () => {
      // noUncheckedIndexedAccess: the fallback keeps the type honest rather
      // than asserting the index is in range.
      const status = statuses[Math.min(calls, statuses.length - 1)] ?? "completed";
      calls += 1;
      return { status };
    });

    renderHook(() =>
      usePoll(fetcher, (data: { status: string }) => data.status === "completed"),
    );

    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));

    // Two more intervals elapse, taking it to the terminal state.
    await vi.advanceTimersByTimeAsync(2100);
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
    await vi.advanceTimersByTimeAsync(2100);
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(3));

    const callsAtTerminal = fetcher.mock.calls.length;

    // Well past several more intervals: a scan left polling forever is a bug
    // that costs the API one request per viewer per two seconds, indefinitely.
    await vi.advanceTimersByTimeAsync(30_000);
    expect(fetcher).toHaveBeenCalledTimes(callsAtTerminal);
  });

  it("keeps polling while the data is not terminal", async () => {
    const fetcher = vi.fn(async () => ({ status: "running" }));

    renderHook(() =>
      usePoll(fetcher, (data: { status: string }) => data.status === "completed"),
    );

    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(2100);
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
  });

  it("surfaces an error rather than rendering it as an empty result", async () => {
    const fetcher = vi.fn(async () => {
      throw new ApiError(500, "The API fell over");
    });

    const { result } = renderHook(() => usePoll(fetcher, () => true));

    await waitFor(() => expect(result.current.result.state).toBe("error"));
    if (result.current.result.state !== "error") throw new Error("expected an error state");
    expect(result.current.result.message).toBe("The API fell over");
  });

  it("waits strictly longer after each failure instead of hammering the API", async () => {
    // The interval doubles before the retry is scheduled, so a failing poll
    // waits 4s, then 8s, then 16s. The property that matters is that each gap
    // is longer than the last and never drops below the 2s floor.
    const fetcher = vi.fn(async () => {
      throw new ApiError(503, "unavailable");
    });

    renderHook(() => usePoll(fetcher, () => true));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));

    // 2s after the first failure: still waiting, because it backed off.
    await vi.advanceTimersByTimeAsync(2100);
    expect(fetcher).toHaveBeenCalledTimes(1);

    // By 4s it has retried once.
    await vi.advanceTimersByTimeAsync(2100);
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));

    // The gap has grown: 4s is no longer enough to trigger the next attempt.
    await vi.advanceTimersByTimeAsync(4100);
    expect(fetcher).toHaveBeenCalledTimes(2);

    // By 8s it has.
    await vi.advanceTimersByTimeAsync(4100);
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(3));
  });

  it("never polls faster than the two-second floor on success", async () => {
    const fetcher = vi.fn(async () => ({ status: "running" }));

    renderHook(() =>
      usePoll(fetcher, (data: { status: string }) => data.status === "completed"),
    );
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));

    await vi.advanceTimersByTimeAsync(1900);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
