/**
 * Fullscreen image lightbox for transcript / attachment previews.
 */

import { ImageResource } from "./ImageResource";
import { useRef } from "react";
import { X } from "lucide-react";
import { Modal } from "../ui/overlay";

export default function ImageLightbox({
  url,
  onClose,
}: {
  url: string | null;
  onClose: () => void;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);

  return (
    <Modal
      open={url !== null}
      onClose={onClose}
      label="Image preview"
      initialFocus={closeRef}
      backdropClassName="fixed inset-0 z-50 bg-black/85 backdrop-blur-sm transition-opacity duration-200"
      viewportClassName="fixed inset-0 z-50 flex items-center justify-center"
      className="relative max-w-[90vw] max-h-[90vh] flex flex-col items-center justify-center outline-none"
    >
      {url ? (
        <>
          <button
            ref={closeRef}
            onClick={onClose}
            className="absolute -top-10 right-0 p-1.5 text-faint hover:text-txt bg-panel border border-edge rounded-full transition-colors focus:outline-none"
            title="Close"
          >
            <X size={16} />
          </button>
          <ImageResource
            src={url}
            alt="Enlarged screenshot"
            className="max-w-full max-h-[80vh] object-contain rounded-lg border border-edge shadow-2xl"
          />
        </>
      ) : null}
    </Modal>
  );
}
