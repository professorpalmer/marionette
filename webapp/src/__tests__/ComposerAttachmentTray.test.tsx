// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ComposerAttachmentTray, {
  attachmentSummary,
  documentExt,
  documentIcon,
  documentName,
  middleCut,
} from "../components/conversation/ComposerAttachmentTray";
import { FileArchive, FileCode, File as FileIcon, FileText } from "lucide-react";

const noop = () => {};

describe("ComposerAttachmentTray", () => {
  it("renders nothing without attachments", () => {
    const { container } = render(
      <ComposerAttachmentTray images={[]} documents={[]} onPreviewImage={noop} onRemoveImage={noop} onRemoveDocument={noop} onClear={noop} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("puts every attachment in one list row with a count and Clear", () => {
    const documents = Array.from({ length: 40 }, (_, i) => ({ path: `/up/${i}.zip`, name: `Disc ${i}.zip` }));
    const onClear = vi.fn();
    const onRemoveDocument = vi.fn();
    render(
      <ComposerAttachmentTray
        images={[{ path: "/up/a.png", name: "a.png", src: "blob:a" }]}
        documents={documents}
        onPreviewImage={noop}
        onRemoveImage={noop}
        onRemoveDocument={onRemoveDocument}
        onClear={onClear}
      />,
    );
    const list = screen.getByRole("list", { name: "Attachments" });
    expect(list.className).toContain("overflow-x-auto");
    expect(list.className).not.toContain("flex-wrap");
    expect(list.querySelectorAll("li")).toHaveLength(41);
    expect(screen.getByText("1 image, 40 files")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Remove document Disc 3.zip" }));
    expect(onRemoveDocument).toHaveBeenCalledWith(3);
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(onClear).toHaveBeenCalledOnce();
  });

  it("hides the count and Clear for one attachment", () => {
    render(
      <ComposerAttachmentTray images={[]} documents={[{ path: "/up/x.md", name: "notes.md" }]} onPreviewImage={noop} onRemoveImage={noop} onRemoveDocument={noop} onClear={noop} />,
    );
    expect(screen.queryByRole("button", { name: "Clear" })).toBeNull();
    expect(screen.getByText("notes")).toBeTruthy();
    expect(screen.getByText("md")).toBeTruthy();
  });

  it("names, types and counts files", () => {
    expect(documentName({ path: "/a/b/c.tar.gz" })).toBe("c.tar.gz");
    expect(documentName({ ref: "r1", name: "C:\\dl\\x.pdf" })).toBe("x.pdf");
    expect(documentExt("c.tar.gz")).toBe("gz");
    expect(documentExt(".env")).toBe("");
    expect(documentIcon("zip")).toBe(FileArchive);
    expect(documentIcon("pdf")).toBe(FileText);
    expect(documentIcon("py")).toBe(FileCode);
    expect(documentIcon("")).toBe(FileIcon);
    expect(middleCut("Legend of Dragoon The (USA) (Disc 1).zip", "zip")).toEqual(["Legend of Dragoon The (USA) ", "(Disc 1)"]);
    expect(middleCut("notes.md", "md")).toEqual(["notes", ""]);
    expect(middleCut("Makefile", "")).toEqual(["Makefile", ""]);
    expect(attachmentSummary(2, 0)).toBe("2 images");
    expect(attachmentSummary(0, 1)).toBe("1 file");
  });
});
