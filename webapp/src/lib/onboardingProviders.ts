import type { ProviderInfo } from "./api";

export type OnboardingCopy = {
  tagline: string;
  blurb: string;
  keyUrl?: string;
};

/** First-run tile order. OpenRouter is the keys-only default. */
export const ONBOARDING_ORDER = [
  "openrouter",
  "anthropic",
  "openai",
  "gemini",
  "xai",
  "deepseek",
  "opencode-go",
  "openai-codex",
  "nous",
  "zai",
  "minimax",
  "nvidia",
] as const;

/** Too much ceremony for first-run (pilot-only plan, or multi-cred). Settings still has them. */
export const ONBOARDING_SKIP = new Set(["cursor-cli", "bedrock"]);

export const ONBOARDING_COPY: Record<string, OnboardingCopy> = {
  openrouter: {
    tagline: "one key, many models",
    blurb: "Hosts hundreds of models behind a single key. Good default for new installs.",
    keyUrl: "https://openrouter.ai/keys",
  },
  anthropic: {
    tagline: "Claude models",
    blurb: "Direct Anthropic API. Claude for chat and agentic workers.",
    keyUrl: "https://console.anthropic.com/settings/keys",
  },
  openai: {
    tagline: "GPT-class models",
    blurb: "Official OpenAI API. GPT-class pilots and workers on your key.",
    keyUrl: "https://platform.openai.com/api-keys",
  },
  gemini: {
    tagline: "Gemini models",
    blurb: "Google AI Studio key. Gemini for chat and swarms.",
    keyUrl: "https://aistudio.google.com/apikey",
  },
  xai: {
    tagline: "Grok models",
    blurb: "Direct xAI API. Grok for chat and agentic workers.",
    keyUrl: "https://console.x.ai/",
  },
  deepseek: {
    tagline: "DeepSeek models",
    blurb: "Direct DeepSeek API. Strong coding models at API rates.",
    keyUrl: "https://platform.deepseek.com/api_keys",
  },
  "opencode-go": {
    tagline: "one subscription, many models",
    blurb: "OpenCode Go subscription key. Reseller catalog for chat and workers.",
    keyUrl: "https://opencode.ai/docs/go/",
  },
  "openai-codex": {
    tagline: "ChatGPT plan",
    blurb: "Paste a Codex token, or sign in from Settings after you skip. Full stack either way.",
    keyUrl: "https://chatgpt.com",
  },
  nous: {
    tagline: "Hermes / Nous Portal",
    blurb: "Nous Portal key or OAuth token. Hermes-class models for chat and workers.",
    keyUrl: "https://portal.nousresearch.com",
  },
  zai: {
    tagline: "GLM models",
    blurb: "Z.AI / GLM API key. Full stack chat and swarms.",
    keyUrl: "https://z.ai",
  },
  minimax: {
    tagline: "MiniMax models",
    blurb: "MiniMax API key. Full stack chat and swarms.",
    keyUrl: "https://www.minimax.io",
  },
  nvidia: {
    tagline: "NIM models",
    blurb: "NVIDIA NIM key. Hosted open models for chat and workers.",
    keyUrl: "https://build.nvidia.com",
  },
};

const FALLBACK_COPY: OnboardingCopy = {
  tagline: "API key",
  blurb: "One Full stack key runs chat and swarms. No other platform install.",
};

export function onboardingCopy(name: string): OnboardingCopy {
  return ONBOARDING_COPY[name] || FALLBACK_COPY;
}

export function isOnboardableProvider(provider: ProviderInfo): boolean {
  if (ONBOARDING_SKIP.has(provider.name)) return false;
  if (provider.worker_capability === "full_stack") return true;
  if (provider.worker_capability == null && ONBOARDING_COPY[provider.name]) return true;
  return false;
}

export function onboardableProviders(providers: ProviderInfo[]): ProviderInfo[] {
  return providers.filter(isOnboardableProvider).sort((a, b) => {
    const ai = ONBOARDING_ORDER.indexOf(a.name as (typeof ONBOARDING_ORDER)[number]);
    const bi = ONBOARDING_ORDER.indexOf(b.name as (typeof ONBOARDING_ORDER)[number]);
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });
}

export function defaultOnboardingProvider(providers: ProviderInfo[]): string {
  const list = onboardableProviders(providers);
  if (list.some((p) => p.name === "openrouter")) return "openrouter";
  return list[0]?.name || "";
}

export function openOnboardingKeyUrl(url: string): void {
  const ipc = (window as unknown as { harnessIPC?: { openExternal?: (href: string) => void } }).harnessIPC;
  if (ipc && typeof ipc.openExternal === "function") {
    try {
      ipc.openExternal(url);
      return;
    } catch {
      // Fall through to window.open.
    }
  }
  window.open(url, "_blank", "noopener,noreferrer");
}
