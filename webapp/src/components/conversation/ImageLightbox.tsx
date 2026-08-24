/**
 * Fullscreen image lightbox for transcript / attachment previews.
 */

import { useRef } from "react";
import { X } from "lucide-react";
import { OverlayPortal } from "../../lib/overlayPortal";

export default function ImageLightbox({
  url,
  onClose,
}: {
  url: string | null;
  onClose: () => void;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  return (
    <OverlayPortal
      open={url !== null}
      onClose={onClose}
      focusRootRef={panelRef}
      initialFocusRef={closeRef}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/85 backdrop-blur-sm transition-opacity duration-200"
      onBackdropClick={onClose}
    >
      {url ? (
        <div
          ref={panelRef}
          className="relative max-w-[90vw] max-h-[90vh] flex flex-col items-center justify-center"
          onClick={(e) => e.stopPropagation()}
        >
          <button
            ref={closeRef}
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
      ) : null}
    </OverlayPortal>
  );
}
