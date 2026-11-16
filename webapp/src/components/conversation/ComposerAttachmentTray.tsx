/**
 * Composer attachments as one row of fixed height. The row scrolls to the
 * side, so a drop of 40 files does not make the composer taller.
 */
import type { WheelEvent } from "react";
import { File, FileArchive, FileCode, FileSpreadsheet, FileText, X, type LucideIcon } from "lucide-react";
import type { InputDocument } from "../../lib/api";
import { ImageResource } from "./ImageResource";

export type TrayImage = { path: string; name: string; src: string };

const ARCHIVE_EXTS = new Set(["zip", "tar", "gz", "tgz", "bz2", "xz", "7z"]);
const SHEET_EXTS = new Set(["csv", "tsv", "xls", "xlsx"]);
const TEXT_EXTS = new Set(["md", "txt", "pdf", "rtf", "rst", "tex", "log"]);

export function documentName(document: InputDocument): string {
  const raw = document.name || ("ref" in document ? document.ref : document.path) || "file";
  return raw.split(/[\\/]/).pop() || raw;
}

export function documentExt(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

/**
 * Split a file stem for a middle cut: the head truncates, the tail stays.
 * "Game (Disc 1)" and "Game (Disc 2)" keep the part that differs.
 */
export function middleCut(name: string, ext: string, tail = 8): [string, string] {
  const stem = ext ? name.slice(0, name.length - ext.length - 1) : name;
  if (stem.length <= tail * 2) return [stem, ""];
  return [stem.slice(0, -tail), stem.slice(-tail)];
}

export function documentIcon(ext: string): LucideIcon {
  if (ARCHIVE_EXTS.has(ext)) return FileArchive;
  if (SHEET_EXTS.has(ext)) return FileSpreadsheet;
  if (TEXT_EXTS.has(ext)) return FileText;
  if (ext) return FileCode;
  return File;
}

export function attachmentSummary(images: number, documents: number): string {
  const parts: string[] = [];
  if (images) parts.push(`${images} ${images === 1 ? "image" : "images"}`);
  if (documents) parts.push(`${documents} ${documents === 1 ? "file" : "files"}`);
  return parts.join(", ");
}

/** A mouse wheel moves the row to the side. Trackpads already do. */
function scrollSideways(event: WheelEvent<HTMLUListElement>) {
  const list = event.currentTarget;
  if (list.scrollWidth <= list.clientWidth) return;
  if (Math.abs(event.deltaY) <= Math.abs(event.deltaX)) return;
  list.scrollLeft += event.deltaY;
}

const REMOVE_CLASS =
  "absolute top-0 right-0 flex items-center justify-center rounded-bl p-0.5 bg-black/60 text-txt hover:text-risk "
  + "opacity-0 group-hover/att:opacity-100 focus-visible:opacity-100 transition-opacity";

export default function ComposerAttachmentTray({
  images,
  documents,
  onPreviewImage,
  onRemoveImage,
  onRemoveDocument,
  onClear,
}: {
  images: TrayImage[];
  documents: InputDocument[];
  onPreviewImage: (path: string) => void;
  onRemoveImage: (index: number) => void;
  onRemoveDocument: (index: number) => void;
  onClear: () => void;
}) {
  const total = images.length + documents.length;
  if (total === 0) return null;
  return (
    <div className="flex items-center gap-2 px-3 pt-2.5">
      <ul
        aria-label="Attachments"
        onWheel={scrollSideways}
        className="flex min-w-0 flex-1 items-center gap-1.5 overflow-x-auto scrollbar-none pr-6 [mask-image:linear-gradient(to_right,black_calc(100%-24px),transparent)]"
      >
        {images.map((image, index) => (
          <li
            key={image.path}
            className="group/att relative h-10 w-10 shrink-0 overflow-hidden rounded-lg border border-edge bg-panel/50 select-none"
          >
            <button
              type="button"
              aria-label={`Preview ${image.name}`}
              onClick={() => onPreviewImage(image.path)}
              className="block h-full w-full focus:outline-none focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent"
            >
              <ImageResource src={image.src} alt={image.name} className="h-full w-full cursor-pointer object-cover transition-opacity hover:opacity-90" />
            </button>
            <button type="button" aria-label={`Remove ${image.name}`} title="Remove image" onClick={() => onRemoveImage(index)} className={REMOVE_CLASS}>
              <X size={11} />
            </button>
          </li>
        ))}
        {documents.map((document, index) => {
          const name = documentName(document);
          const ext = documentExt(name);
          const Icon = documentIcon(ext);
          const [head, tail] = middleCut(name, ext);
          return (
            <li
              key={`${index}:${"ref" in document ? document.ref : document.path}`}
              title={name}
              className="group/att relative flex h-10 max-w-[200px] shrink-0 items-center gap-2 overflow-hidden rounded-lg border border-edge bg-panel/50 pl-1 pr-5 select-none"
            >
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-panel2 text-muted">
                <Icon size={15} aria-hidden />
              </span>
              <span className="flex min-w-0 flex-col leading-tight">
                <span className="flex min-w-0 text-ui-11 text-txt">
                  <span className="truncate">{head}</span>
                  {tail && <span className="shrink-0 whitespace-pre">{tail}</span>}
                </span>
                <span className="text-ui-9 uppercase tracking-wide text-muted">{ext || "file"}</span>
              </span>
              <button type="button" aria-label={`Remove document ${name}`} title="Remove file" onClick={() => onRemoveDocument(index)} className={REMOVE_CLASS}>
                <X size={11} />
              </button>
            </li>
          );
        })}
      </ul>
      {total > 1 && (
        <div className="flex shrink-0 items-center gap-2 text-ui-10 text-muted select-none">
          <span>{attachmentSummary(images.length, documents.length)}</span>
          <button type="button" onClick={onClear} className="rounded px-1 hover:text-txt focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent">
            Clear
          </button>
        </div>
      )}
    </div>
  );
}
