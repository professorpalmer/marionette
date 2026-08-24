/**
 * Fullscreen image lightbox for transcript / attachment previews.
 * Portaled with data-starting-style / data-instant.
 */

import { useRef } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import {
  mergeOverlayRootRef,
  overlayDataAttrs,
  useOverlayEnterLeave,
  useOverlayPortalHost,
} from "../../lib/overlayPortal";

export default function ImageLightbox({
  url,
  onClose,
}: {
  url: string | null;
  onClose: () => void;
}) {
  const open = Boolean(url);
  const overlay = useOverlayEnterLeave(open);
  const host = useOverlayPortalHost();
  const rootRef = useRef<HTMLDivElement>(null);

  if (!overlay.mounted || !url || !host) return null;

  return createPortal(
    <div
      ref={mergeOverlayRootRef(overlay, rootRef)}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/85 backdrop-blur-sm pointer-events-auto"
      onClick={onClose}
      data-testid="image-lightbox"
      {...overlayDataAttrs(overlay)}
    >
      <div
        className="relative max-w-[90vw] max-h-[90vh] flex flex-col items-center justify-center"
        onClick={(e) => e.stopPropagation()}
      >
        <button
          onClick={onClose}
          className="absolute -top-10 right-0 p-1.5 text-faint hover:text-txt bg-panel border border-edge rounded-full transition-all focus:outline-none"
          title="Close"
        >
          <X size={16} />
        </button>
        <img
          src={url}
          alt="Enlarged screenshot"
          className="max-w-full max-h-[80vh] object-contain rounded-lg border border-edge shadow-2xl"
        />
      </div>
    </div>,
    host,
  );
}
