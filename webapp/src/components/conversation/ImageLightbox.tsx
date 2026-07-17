/**
 * Fullscreen image lightbox for transcript / attachment previews.
 */

import { X } from "lucide-react";

export default function ImageLightbox({
  url,
  onClose,
}: {
  url: string | null;
  onClose: () => void;
}) {
  if (!url) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/85 backdrop-blur-sm transition-opacity animate-in fade-in duration-200"
      onClick={onClose}
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
    </div>
  );
}
