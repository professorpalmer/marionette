import { afterEach, describe, expect, it, vi } from "vitest";
import {
  copyTranscriptId,
  downloadTextFile,
  formatSessionExportMarkdown,
  sessionExportFilename,
  transcriptIdOf,
  transcriptRelpathOf,
} from "../lib/sessionExport";

describe("sessionExport", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("puts the full transcript id in the download name", () => {
    expect(sessionExportFilename("My Export Test Session!", "a1b2c3d4e5f6", "json"))
      .toBe("My_Export_Test_Session-a1b2c3d4e5f6.json");
    expect(sessionExportFilename("unknown-id", "unknown-id", "md")).toBe("unknown-id.md");
  });

  it("prefers transcript_id and a stable on-disk locator", () => {
    expect(transcriptIdOf({ session_id: "old", transcript_id: "new" })).toBe("new");
    expect(transcriptRelpathOf({ transcript_id: "abc123" })).toBe("transcripts/abc123.json");
  });

  it("writes markdown a later agent can grep by transcript id", () => {
    const md = formatSessionExportMarkdown({
      transcript_id: "a1b2c3d4e5f6",
      session_id: "a1b2c3d4e5f6",
      transcript_relpath: "transcripts/a1b2c3d4e5f6.json",
      title: "My Export Test Session!",
      created: 1_700_000_000,
      exported_at: 1_700_000_100,
      messages: [
        { role: "user", content: "hello pilot" },
        { role: "assistant", content: "hello human" },
      ],
    });
    expect(md).toContain("# My Export Test Session!");
    expect(md).toContain("**Transcript ID:** a1b2c3d4e5f6");
    expect(md).toContain("**Session ID:** a1b2c3d4e5f6");
    expect(md).toContain("**On disk:** `transcripts/a1b2c3d4e5f6.json`");
    expect(md).toContain("## User");
    expect(md).toContain("hello pilot");
    expect(md).toContain("## Assistant");
    expect(md).toContain("hello human");
  });

  it("downloads a blob instead of navigating to /api/sessions/export", () => {
    const clicks: string[] = [];
    const created: Array<{ download: string; href: string; click: () => void }> = [];
    vi.stubGlobal("URL", {
      createObjectURL: () => "blob:session-export",
      revokeObjectURL: () => undefined,
    });
    const nativeCreate = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation((tag: string) => {
      if (tag !== "a") return nativeCreate(tag);
      const a = {
        href: "",
        download: "",
        click() {
          clicks.push(`${this.download}|${this.href}`);
        },
      };
      created.push(a);
      return a as unknown as HTMLElement;
    });
    vi.spyOn(document.body, "appendChild").mockImplementation((node) => node);
    vi.spyOn(document.body, "removeChild").mockImplementation((node) => node);

    downloadTextFile("My_Export-a1b2c3d4e5f6.md", "# hi", "text/markdown");

    expect(clicks).toEqual(["My_Export-a1b2c3d4e5f6.md|blob:session-export"]);
    expect(created[0]?.href).not.toContain("/api/sessions/export");
  });

  it("copies the transcript id for other sessions", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    await expect(copyTranscriptId("a1b2c3d4e5f6")).resolves.toBe(true);
    expect(writeText).toHaveBeenCalledWith("a1b2c3d4e5f6");
  });
});
