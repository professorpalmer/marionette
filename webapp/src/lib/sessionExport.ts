/** Session export helpers — authenticated payload -> blob download + transcript ID. */

export type SessionExportMessage = {
  role?: string;
  content?: unknown;
};

export type SessionExportPayload = {
  transcript_id?: string;
  session_id?: string;
  transcript_relpath?: string;
  title?: string;
  created?: number | null;
  exported_at?: number;
  messages?: SessionExportMessage[];
};

export function transcriptIdOf(payload: SessionExportPayload, fallback = ""): string {
  const id = String(payload.transcript_id || payload.session_id || fallback || "").trim();
  return id;
}

export function sanitizeExportFilenamePart(value: string): string {
  const cleaned = String(value || "")
    .replace(/[^a-zA-Z0-9\-_]/g, "_")
    .replace(/_+/g, "_")
    .replace(/^[_-]+|[_-]+$/g, "");
  return cleaned || "session";
}

/** Hermes-style `{title}-{id}.{ext}` so the file itself carries the transcript ID. */
export function sessionExportFilename(
  title: string,
  transcriptId: string,
  ext: "md" | "json",
): string {
  const idPart = sanitizeExportFilenamePart(transcriptId);
  const titlePart = sanitizeExportFilenamePart(title || idPart);
  if (titlePart.toLowerCase() === idPart.toLowerCase()) {
    return `${idPart}.${ext}`;
  }
  return `${titlePart}-${idPart}.${ext}`;
}

export function transcriptRelpathOf(payload: SessionExportPayload, fallbackId = ""): string {
  const explicit = String(payload.transcript_relpath || "").trim();
  if (explicit) return explicit;
  const id = sanitizeExportFilenamePart(transcriptIdOf(payload, fallbackId));
  return `transcripts/${id}.json`;
}

function formatLocalStamp(ts: number | null | undefined): string {
  if (ts == null || !Number.isFinite(Number(ts))) return "Unknown";
  const d = new Date(Number(ts) * 1000);
  if (Number.isNaN(d.getTime())) return "Unknown";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function messageContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (content == null) return "";
  try {
    return JSON.stringify(content);
  } catch {
    return String(content);
  }
}

export function formatSessionExportMarkdown(payload: SessionExportPayload, fallbackId = ""): string {
  const id = transcriptIdOf(payload, fallbackId);
  const title = String(payload.title || "Unknown Session");
  const relpath = transcriptRelpathOf(payload, id);
  const lines = [
    `# ${title}`,
    "",
    `**Transcript ID:** ${id}  `,
    `**Session ID:** ${id}  `,
    `**On disk:** \`${relpath}\`  `,
    `**Created:** ${formatLocalStamp(payload.created)}  `,
    `**Exported:** ${formatLocalStamp(payload.exported_at)}`,
    "",
  ];
  for (const msg of payload.messages || []) {
    const role = String(msg.role || "").replace(/^./, (ch) => ch.toUpperCase());
    lines.push(`## ${role}`, "", messageContent(msg.content), "");
  }
  return lines.join("\n");
}

export function downloadTextFile(filename: string, body: string, mime: string): void {
  const blob = new Blob([body], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export async function copyTranscriptId(transcriptId: string): Promise<boolean> {
  const id = String(transcriptId || "").trim();
  if (!id) return false;
  try {
    await navigator.clipboard.writeText(id);
    return true;
  } catch {
    return false;
  }
}
