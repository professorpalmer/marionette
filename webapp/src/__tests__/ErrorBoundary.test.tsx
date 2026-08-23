import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ErrorBoundary from "../components/ErrorBoundary";

let explode = true;

function Boom() {
  if (explode) throw new Error("kaboom");
  return <div>recovered child</div>;
}

describe("ErrorBoundary", () => {
  it("shows fallback UI and recovers on Try again", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    explode = true;
    render(
      <ErrorBoundary label="Test pane">
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByText(/Test pane crashed/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: /Try again/i })).toBeTruthy();
    expect(screen.getByText(/kaboom/)).toBeTruthy();

    explode = false;
    fireEvent.click(screen.getByRole("button", { name: /Try again/i }));
    expect(screen.getByText("recovered child")).toBeTruthy();
    spy.mockRestore();
  });
});
