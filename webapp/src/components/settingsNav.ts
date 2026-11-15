import type { LucideIcon } from "lucide-react";
import { Archive, Bell, Cpu, HardDrive, Info, Puzzle, ShieldCheck, SlidersHorizontal, Wrench, Zap } from "lucide-react";
import type { SettingsSection } from "./SettingsPane";

export type PageId = "models" | "local-models" | SettingsSection | "archived" | "about";

export type NavItem = {
  id: PageId;
  label: string;
  icon: LucideIcon;
  /** The names of the settings on the page. The shell search matches them. */
  keywords: string;
};

/** Settings pages in sidebar order. The search box filters this list. */
export const NAV: readonly NavItem[] = [
  {
    id: "providers",
    label: "Accounts",
    icon: Zap,
    keywords: "sign in sign out api key add key remove key turn on turn off check connect disconnect subscription oauth "
      + "claude max anthropic chatgpt codex openai cursor xai grok nous openrouter opencode go zen minimax gemini deepseek nvidia zai "
      + "credential pool rotate aws bedrock region inference profile external worker platforms adapters cli "
      + "github wiki repo provisioning device flow",
  },
  {
    id: "models",
    label: "Models",
    icon: Cpu,
    keywords: "pilot picker model catalog enable disable refresh provider list toggle",
  },
  {
    id: "local-models",
    label: "Local Models",
    icon: HardDrive,
    keywords: "local llama.cpp managed this machine download endpoint url remote service "
      + "display name model id context tokens api key discovered model",
  },
  {
    id: "general",
    label: "General",
    icon: SlidersHorizontal,
    keywords: "connect a provider setup wizard glass window tint transparent background budget steps "
      + "opt-ins auto-distill hash-anchored edits review edits auto-verify edits jev turn judgment "
      + "compact residual compaction catalog hybrid summary vault output style simplified technical english ste "
      + "usage cost tokens spend price active driver system info reach state directory repository",
  },
  {
    id: "safety",
    label: "Safety",
    icon: ShieldCheck,
    keywords: "use my chrome login browser profile cookies full-auto guard dangerous commands command timeout "
      + "max investigation steps reply output cap per-turn tool-call cap token ceiling worker reasoning "
      + "encrypted sessions encryption recovery code device access grants revoke",
  },
  {
    id: "notifications",
    label: "Notifications",
    icon: Bell,
    keywords: "desktop notifications completion sound observability queue messages",
  },
  {
    id: "plugins",
    label: "Plugins",
    icon: Puzzle,
    keywords: "agent plugins install plugin source path git url github force reinstall turn on turn off skills mcp servers",
  },
  {
    id: "archived",
    label: "Archived chats",
    icon: Archive,
    keywords: "archived chats sessions vault open unarchive restore",
  },
  {
    id: "advanced",
    label: "Advanced",
    icon: Wrench,
    keywords: "live ui vite hmr hot reload self dev schedules cron timezone daemon lifecycle hooks events command "
      + "chat archive ingest archived sessions compact ingested transcripts agent memory facts preferences "
      + "skills rules learned wiki graph portable-llm-wiki api base token",
  },
  {
    id: "about",
    label: "About",
    icon: Info,
    keywords: "version release commit checkout puppetmaster update check for updates release notes",
  },
];

export const PAGE_IDS: ReadonlySet<string> = new Set(NAV.map((n) => n.id));

function words(text: string): string[] {
  const whole = text.toLowerCase().split(/\s+/).filter(Boolean);
  return [...whole, ...whole.flatMap((word) => word.split(/[-./]/).filter(Boolean))];
}

/**
 * Pages where each word of the query starts a word of the label or keywords,
 * in sidebar order. "time" finds "timeout". "turn" finds "per-turn".
 */
export function matchSettingsPages(query: string): readonly NavItem[] {
  const wanted = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (wanted.length === 0) return NAV;
  return NAV.filter((item) => {
    const have = words(`${item.label} ${item.keywords}`);
    return wanted.every((want) => have.some((word) => word.startsWith(want)));
  });
}
