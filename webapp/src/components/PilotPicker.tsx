import { useEffect, useState, useRef } from "react";
import { ChevronDown, Check } from "lucide-react";
import { api, type Config } from "../lib/api";

export default function PilotPicker({ config }: {
  config: Config | null;
}) {
  const [models, setModels] = useState<string[]>([]);
  const [current, setCurrent] = useState("");
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (config) {
      setModels(config.models || [config.driver]);
      setCurrent(config.driver);
    }
  }, [config]);

  useEffect(() => {
    const handleOpen = () => setIsOpen(true);
    window.addEventListener("harness-open-model-picker", handleOpen);
    return () => window.removeEventListener("harness-open-model-picker", handleOpen);
  }, []);

  useEffect(() => {
    if (!isOpen) return;
    const handleOutsideClick = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    };
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setIsOpen(false);
      }
    };
    document.addEventListener("mousedown", handleOutsideClick);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handleOutsideClick);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [isOpen]);

  const swap = async (m: string) => {
    const prev = current;
    setCurrent(m);
    setIsOpen(false);
    try {
      await api.swapPilot(m);
      // notify the rest of the app (StatusBar, etc.) so config-derived UI refreshes
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch (e: any) {
      // Swap refused (e.g. 409 -- a turn is streaming) or failed. Revert the
      // label so the picker never lies about the active model, and surface a
      // brief reason instead of silently appearing broken.
      setCurrent(prev);
      const msg = String(e?.message || e || "");
      window.dispatchEvent(new CustomEvent("harness-toast", {
        detail: msg.includes("409") || msg.includes("in progress")
          ? "Stop the current turn before switching models"
          : "Model switch failed -- try again",
      }));
    }
  };

  if (!config) return null;

  const currentLabel = current ? current.split(":").pop() : "";

  return (
    <div className="relative inline-block" ref={containerRef}>
      <button
        onClick={() => setIsOpen((prev) => !prev)}
        title="Pilot model"
        className="flex items-center gap-1 text-[11px] text-muted hover:text-txt rounded-md px-2 h-[22px] bg-transparent hover:bg-panel2 border border-edge/40 transition select-none"
      >
        <span className="truncate max-w-[170px]">{currentLabel}</span>
        <ChevronDown size={11} className="shrink-0 opacity-60" />
      </button>

      {isOpen && (
        <div className="absolute left-0 bottom-full mb-1 z-50 min-w-[180px] bg-panel border border-edge rounded-lg shadow-lg py-1 overflow-hidden">
          {models.map((m) => {
            const isSelected = m === current;
            const shortName = m.split(":").pop() || "";
            return (
              <div
                key={m}
                onClick={() => swap(m)}
                className={`flex items-center justify-between px-3 py-1.5 text-[11.5px] hover:bg-panel2 cursor-pointer transition select-none ${
                  isSelected ? "text-accent font-medium bg-panel2/40" : "text-txt/90"
                }`}
              >
                <span className="truncate max-w-[170px]" title={m}>{shortName}</span>
                {isSelected && <Check size={11} className="shrink-0 ml-2" />}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
