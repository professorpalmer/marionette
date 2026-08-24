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
  "opencode-zen",
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
  "xai-oauth": {
    tagline: "SuperGrok plan",
    blurb: "Sign in with SuperGrok. Full stack Grok for chat and workers.",
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
  "opencode-zen": {
    tagline: "pay-as-you-go, including Ox Alpha",
    blurb: "OpenCode Zen catalog, including Ox Alpha Free. Separate from the Go subscription.",
    keyUrl: "https://opencode.ai/docs/zen/",
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
    tagline: "GLM Coding Plan",
    blurb: "Z.AI Coding Plan or API key. Full stack chat and swarms on GLM-5.2.",
    keyUrl: "https://z.ai/manage-apikey/apikey-list",
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


/** Wired OAuth start handlers in harness/api/providers.py. qwen/minimax stay out. */
export const FEATURED_OAUTH_ORDER = [
  "openai-codex",
  "anthropic",
  "xai-oauth",
  "nous",
] as const;

export type FeaturedOAuthName = (typeof FEATURED_OAUTH_ORDER)[number];

export const FEATURED_OAUTH = new Set<string>(FEATURED_OAUTH_ORDER);

export const FEATURED_LABELS: Record<FeaturedOAuthName, string> = {
  "openai-codex": "ChatGPT Codex",
  anthropic: "Claude Max",
  "xai-oauth": "xAI SuperGrok",
  nous: "Nous Portal",
};

const FEATURED_SYNTH: Record<FeaturedOAuthName, Pick<ProviderInfo, "display_name" | "env_var" | "base_url" | "api_mode">> = {
  "openai-codex": {
    display_name: "ChatGPT Codex",
    env_var: "OPENAI_CODEX_TOKEN",
    base_url: "",
    api_mode: "codex_responses",
  },
  anthropic: {
    display_name: "Claude Max",
    env_var: "ANTHROPIC_API_KEY",
    base_url: "",
    api_mode: "anthropic_messages",
  },
  "xai-oauth": {
    display_name: "xAI SuperGrok",
    env_var: "XAI_OAUTH_TOKEN",
    base_url: "",
    api_mode: "chat_completions",
  },
  nous: {
    display_name: "Nous Portal",
    env_var: "NOUS_API_KEY",
    base_url: "",
    api_mode: "chat_completions",
  },
};

export function isFeaturedOAuth(name: string): name is FeaturedOAuthName {
  return FEATURED_OAUTH.has(name);
}

export function featuredOAuthKind(name: string): "device" | "pkce" | null {
  if (name === "anthropic") return "pkce";
  if (isFeaturedOAuth(name)) return "device";
  return null;
}

function syntheticFeatured(name: FeaturedOAuthName, providers: ProviderInfo[]): ProviderInfo {
  const found = providers.find((p) => p.name === name);
  if (found) {
    return {
      ...found,
      display_name: FEATURED_LABELS[name] || found.display_name || found.name,
    };
  }
  const meta = FEATURED_SYNTH[name];
  return {
    name,
    display_name: meta.display_name,
    env_var: meta.env_var,
    base_url: meta.base_url,
    has_key: false,
    api_mode: meta.api_mode,
    worker_capability: "full_stack",
  };
}

/** Always the four wired OAuth providers. xai-oauth is a pool, not a GET /api/providers row. */
export function featuredOnboardingProviders(providers: ProviderInfo[]): ProviderInfo[] {
  return FEATURED_OAUTH_ORDER.map((name) => syntheticFeatured(name, providers));
}

/** Paste-key rows: onboardable providers that are not featured OAuth. */
export function keyOnboardingProviders(providers: ProviderInfo[]): ProviderInfo[] {
  return onboardableProviders(providers).filter((p) => !isFeaturedOAuth(p.name));
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
