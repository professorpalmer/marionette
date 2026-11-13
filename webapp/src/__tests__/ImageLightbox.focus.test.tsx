import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import ImageLightbox from "../components/conversation/ImageLightbox";

function Harness() {
  const [url, setUrl] = useState<string | null>(null);
  return <>
    <button type="button" onClick={() => setUrl("data:image/png;base64,AAAA")}>Open image</button>
    <ImageLightbox url={url} onClose={() => setUrl(null)} />
  </>;
}

describe("ImageLightbox focus", () => {
  it("focuses Close, closes on Escape, and returns focus to the opener", async () => {
    render(<Harness />);
    const opener = screen.getByRole("button", { name: "Open image" });
    opener.focus();
    fireEvent.click(opener);
    const dialog = await screen.findByRole("dialog", { name: "Image preview" });
    await waitFor(() => expect(screen.getByTitle("Close")).toHaveFocus());
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(opener).toHaveFocus());
  });
});
