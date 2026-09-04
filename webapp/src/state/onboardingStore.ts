/**
 * First-run onboarding state. Persists gate decisions to localStorage so reload
 * keeps the wizard from nagging after skip or successful provider setup.
 */

const LS_CONFIGURED = "pmharness.onboarding.configured";
const LS_FIRST_RUN_SKIPPED = "pmharness.onboarding.firstRunSkipped";
const LS_WIZARD_SEEN = "pmharness.wizardSeen";

export type OnboardingFlow =
  | "idle"
  | "starting"
  | "awaiting_user"
  | "polling"
  | "external_pending"
  | "submitting"
  | "success"
  | "confirming_model"
  | "error";

export type OnboardingMode = "apikey" | "oauth";

export type OnboardingState = {
  configured: boolean | null;
  flow: OnboardingFlow;
  mode: OnboardingMode;
  firstRunSkipped: boolean;
  manual: boolean;
  localEndpoint: string;
  providerStatusByModel: Record<string, unknown>;
};

type Listener = (state: OnboardingState) => void;

function readConfigured(): boolean | null {
  try {
    const raw = localStorage.getItem(LS_CONFIGURED);
    if (raw === "1") return true;
    if (raw === "0") return false;
    // Migrate legacy wizard-dismissed flag as "already handled".
    if (localStorage.getItem(LS_WIZARD_SEEN) === "1") return true;
  } catch {
    // Private mode / quota — treat as unknown.
  }
  return null;
}

function readFirstRunSkipped(): boolean {
  try {
    if (localStorage.getItem(LS_FIRST_RUN_SKIPPED) === "1") return true;
    if (localStorage.getItem(LS_WIZARD_SEEN) === "1") return true;
  } catch {
    // ignore
  }
  return false;
}

function persistConfigured(value: boolean): void {
  try {
    localStorage.setItem(LS_CONFIGURED, value ? "1" : "0");
    if (value) localStorage.setItem(LS_WIZARD_SEEN, "1");
  } catch {
    // ignore
  }
}

function persistFirstRunSkipped(): void {
  try {
    localStorage.setItem(LS_FIRST_RUN_SKIPPED, "1");
    localStorage.setItem(LS_WIZARD_SEEN, "1");
  } catch {
    // ignore
  }
}

let state: OnboardingState = {
  configured: readConfigured(),
  flow: "idle",
  mode: "apikey",
  firstRunSkipped: readFirstRunSkipped(),
  manual: false,
  localEndpoint: "",
  providerStatusByModel: {},
};

const listeners = new Set<Listener>();

function emit(): void {
  for (const listener of listeners) listener(state);
}

function patch(partial: Partial<OnboardingState>): void {
  state = { ...state, ...partial };
  emit();
}

function rehydrateFromStorage(): void {
  state = {
    configured: readConfigured(),
    flow: "idle",
    mode: "apikey",
    firstRunSkipped: readFirstRunSkipped(),
    manual: false,
    localEndpoint: "",
    providerStatusByModel: {},
  };
}

export function getOnboardingState(): OnboardingState {
  return state;
}

export function subscribeOnboarding(listener: Listener): () => void {
  listeners.add(listener);
  listener(state);
  return () => {
    listeners.delete(listener);
  };
}

function inspectFixtureActive(): boolean {
  try {
    return (window as Window & { __HARNESS_INSPECT__?: boolean }).__HARNESS_INSPECT__ === true;
  } catch {
    return false;
  }
}

/** True when the first-run wizard should auto-open on launch. */
export function shouldAutoOpenOnboardingWizard(
  s: OnboardingState = state,
): boolean {
  if (inspectFixtureActive()) return false;
  if (s.firstRunSkipped || s.manual) return false;
  if (s.configured === true) return false;
  return true;
}

export function setConfigured(configured: boolean): void {
  persistConfigured(configured);
  patch({ configured });
}

export function setFlow(flow: OnboardingFlow): void {
  patch({ flow });
}

export function setMode(mode: OnboardingMode): void {
  patch({ mode });
}

export function skipFirstRun(): void {
  persistFirstRunSkipped();
  patch({ firstRunSkipped: true });
}

export function setManual(manual: boolean): void {
  patch({ manual });
}

export function setLocalEndpoint(localEndpoint: string): void {
  patch({ localEndpoint });
}

export function setProviderStatus(
  model: string,
  status: unknown,
): void {
  patch({
    providerStatusByModel: { ...state.providerStatusByModel, [model]: status },
  });
}

export function resetOnboardingForTests(clearStorage = true): void {
  if (clearStorage) {
    try {
      localStorage.removeItem(LS_CONFIGURED);
      localStorage.removeItem(LS_FIRST_RUN_SKIPPED);
      localStorage.removeItem(LS_WIZARD_SEEN);
    } catch {
      // ignore
    }
  }
  rehydrateFromStorage();
  emit();
}
