import { useEffect, useState } from "react";
import { X, Check, AlertCircle, Sparkles } from "lucide-react";
import { api, type ProviderInfo, type RegistryModel, type PilotValidateResult } from "../lib/api";

interface RegistryWizardProps {
  onClose: () => void;
}

export default function RegistryWizard({ onClose }: RegistryWizardProps) {
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [settingsModels, setSettingsModels] = useState<string[]>([]);
  const [probedModels, setProbedModels] = useState<Record<string, { id: string }[]>>({});
  const [probeStatus, setProbeStatus] = useState<Record<string, { source: "live" | "static"; error?: string }>>({});
  const [probing, setProbing] = useState<Record<string, boolean>>({});
  const [probeErrors, setProbeErrors] = useState<Record<string, string>>({});

  // Pilot State
  const [pilot, setPilot] = useState<string>("");
  const [pilotValidation, setPilotValidation] = useState<PilotValidateResult | null>(null);

  // Roles State
  const [roles, setRoles] = useState<Record<string, number>>({});
  const [roleModels, setRoleModels] = useState<Record<string, string>>({});
  const [routingPolicy, setRoutingPolicy] = useState<string>("balanced");
  const [policies] = useState<string[]>(["balanced", "cheap", "quality", "escalating"]);

  // Registry State
  const [registryModels, setRegistryModels] = useState<RegistryModel[]>([]);
  const [showRegistryScores, setShowRegistryScores] = useState<boolean>(false);

  // General UI state
  const [loading, setLoading] = useState<boolean>(true);
  const [saving, setSaving] = useState<boolean>(false);
  const [loadingRecommend, setLoadingRecommend] = useState<boolean>(false);
  const [status, setStatus] = useState<string>("");
  const [error, setError] = useState<string>("");
  
  // API Key Inputs
  const [keyInputs, setKeyInputs] = useState<Record<string, string>>({});
  const [savingKey, setSavingKey] = useState<Record<string, boolean>>({});

  // Close on Escape key
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  // Load initial data
  const loadInitialData = async () => {
    setLoading(true);
    setError("");
    try {
      const [provs, settingsData, rolesData, registryData] = await Promise.all([
        api.providers(),
        api.settings(),
        api.getRoles(),
        api.getRegistry(),
      ]);
      setProviders(provs);
      setPilot(settingsData.driver);
      setSettingsModels(settingsData.models || []);
      setRoles(rolesData.roles || {});
      setRoutingPolicy(rolesData.routing_policy || "balanced");
      setRegistryModels(registryData.models || []);

      // Pre-fill some default roleModels from registry models if scores align
      const initialRoleModels: Record<string, string> = {};
      Object.keys(rolesData.roles || {}).forEach((role) => {
        const threshold = rolesData.roles[role];
        // find a registry model that has a score closest/appropriate to this threshold
        const matchingModel = registryData.models?.find(
          (m) => m.capability_score >= threshold
        );
        if (matchingModel) {
          initialRoleModels[role] = matchingModel.id;
        }
      });
      setRoleModels(initialRoleModels);
    } catch (err: any) {
      console.error("Failed to load setup wizard data", err);
      setError("Failed to load initial configuration. Please verify server status.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadInitialData();
  }, []);

  // Validate pilot on change
  useEffect(() => {
    if (pilot) {
      api.validatePilot(pilot)
        .then(setPilotValidation)
        .catch(() => {
          setPilotValidation({
            valid: false,
            resolved_model_id: null,
            provider: null,
            reason: "Validation service unavailable",
          });
        });
    }
  }, [pilot]);

  // Probe provider
  const handleProbe = async (providerName: string) => {
    setProbing((prev) => ({ ...prev, [providerName]: true }));
    setProbeErrors((prev) => ({ ...prev, [providerName]: "" }));
    try {
      const res = await api.probeProvider(providerName);
      setProbedModels((prev) => ({ ...prev, [providerName]: res.models }));
      setProbeStatus((prev) => ({
        ...prev,
        [providerName]: { source: res.source, error: res.error },
      }));
    } catch (err: any) {
      setProbeErrors((prev) => ({ ...prev, [providerName]: err?.message || "Probe failed" }));
    } finally {
      setProbing((prev) => ({ ...prev, [providerName]: false }));
    }
  };

  // Save key
  const handleSaveKey = async (providerName: string) => {
    const keyVal = keyInputs[providerName]?.trim();
    if (!keyVal) return;

    setSavingKey((prev) => ({ ...prev, [providerName]: true }));
    try {
      await api.updateSettings({ reach: providerName, api_key: keyVal });
      setKeyInputs((prev) => ({ ...prev, [providerName]: "" }));
      
      // Refresh providers list
      const updatedProvs = await api.providers();
      setProviders(updatedProvs);
      
      // Auto-probe after adding key
      handleProbe(providerName);
    } catch (err: any) {
      alert(`Failed to save key: ${err?.message || err}`);
    } finally {
      setSavingKey((prev) => ({ ...prev, [providerName]: false }));
    }
  };

  // Recommend defaults
  const handleRecommend = async () => {
    setLoadingRecommend(true);
    setStatus("");
    setError("");
    try {
      const rec = await api.recommend();
      
      if (rec.pilot || rec.pilot_driver) {
        setPilot(rec.pilot || rec.pilot_driver);
      }
      
      if (rec.roles) {
        // rec.roles maps role -> model ID. We set roleModels selection
        setRoleModels((prev) => ({
          ...prev,
          ...rec.roles,
        }));
        
        // Ensure any recommended models exist in registry with appropriate scores
        setRegistryModels((prevRegistry) => {
          const updatedRegistry = [...prevRegistry];
          Object.entries(rec.roles).forEach(([role, modelId]) => {
            const threshold = roles[role] || 50;
            const existingIdx = updatedRegistry.findIndex((m) => m.id === modelId);
            if (existingIdx >= 0) {
              // Ensure its score is at least the role's score
              if (updatedRegistry[existingIdx].capability_score < threshold) {
                updatedRegistry[existingIdx] = {
                  ...updatedRegistry[existingIdx],
                  capability_score: threshold,
                };
              }
            } else {
              // Add recommended model to registry
              updatedRegistry.push({
                id: modelId,
                adapter: "openai",
                capability_score: threshold,
                tags: ["worker", role],
                notes: "auto-added via recommendation",
              });
            }
          });
          return updatedRegistry;
        });
      }

      setStatus("Recommended setup populated! Review and click Save All.");
      setTimeout(() => setStatus(""), 4000);
    } catch (err: any) {
      setError(`Failed to fetch recommendations: ${err?.message || err}`);
    } finally {
      setLoadingRecommend(false);
    }
  };

  // Save everything
  const handleSaveAll = async () => {
    if (pilotValidation && !pilotValidation.valid) {
      setError("Cannot save with an invalid pilot model.");
      return;
    }

    setSaving(true);
    setStatus("Saving configuration...");
    setError("");

    try {
      // 1. Sync roleModels into registryModels scores to ensure eligibility
      const finalRegistryModels = [...registryModels];
      Object.entries(roleModels).forEach(([role, modelId]) => {
        if (!modelId) return;
        const threshold = roles[role] || 50;
        const idx = finalRegistryModels.findIndex((m) => m.id === modelId);
        if (idx >= 0) {
          if (finalRegistryModels[idx].capability_score < threshold) {
            finalRegistryModels[idx].capability_score = threshold;
          }
        } else {
          finalRegistryModels.push({
            id: modelId,
            adapter: "openai",
            capability_score: threshold,
            tags: ["worker", role],
          });
        }
      });

      // 2. Perform sequential POST saves
      await api.saveRoles({ overrides: roles, routing_policy: routingPolicy });
      await api.saveRegistry(finalRegistryModels);
      await api.updateSettings({ driver: pilot });

      // Mark wizard as completed
      localStorage.setItem("pmharness.wizardSeen", "1");

      setStatus("All settings saved successfully!");
      setTimeout(() => {
        onClose();
      }, 1500);
    } catch (err: any) {
      console.error("Save all failed", err);
      setError(`Failed to save configuration: ${err?.message || err}`);
    } finally {
      setSaving(false);
    }
  };

  // Combine probed + static models for dropdown choices
  const allProbedModels = Object.values(probedModels).flatMap((models) => models.map((m) => m.id));
  const modelOptions = Array.from(new Set([...settingsModels, ...allProbedModels])).filter(Boolean);

  if (loading) {
    return (
      <div className="fixed inset-0 bg-black/60 backdrop-blur-xs flex items-center justify-center z-50">
        <div className="bg-panel border border-edge rounded-lg p-6 max-w-sm w-full text-center space-y-3">
          <p className="text-muted font-medium">Loading wizard components...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 bg-black/70 backdrop-blur-xs flex items-center justify-center z-50 p-4">
      <div 
        className="bg-panel border border-edge rounded-lg shadow-2xl w-full max-w-3xl max-h-[90vh] flex flex-col text-[12px] text-txt"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-edge">
          <div className="flex items-center gap-2">
            <span className="font-bold text-[14px] uppercase tracking-wider text-txt">
              Provider & Model Setup
            </span>
            <span className="text-[10px] text-faint uppercase font-medium">wizard</span>
          </div>
          <button 
            onClick={onClose}
            className="text-faint hover:text-txt transition-colors p-1"
          >
            <X size={16} />
          </button>
        </div>

        {/* Scrollable Content */}
        <div className="flex-1 overflow-y-auto p-4 space-y-6">
          {error && (
            <div className="bg-risk/10 border border-risk/30 text-risk rounded p-3 flex items-start gap-2">
              <AlertCircle size={14} className="mt-0.5 shrink-0" />
              <div>{error}</div>
            </div>
          )}

          {status && (
            <div className="bg-accent/10 border border-accent/30 text-accent rounded p-3 flex items-start gap-2">
              <Check size={14} className="mt-0.5 shrink-0" />
              <div>{status}</div>
            </div>
          )}

          {/* Section A & B: Providers & Probe */}
          <div className="space-y-3">
            <div className="flex items-center justify-between border-b border-edge/60 pb-1.5">
              <span className="uppercase tracking-wider text-[11px] text-faint font-bold">
                1. API Key Providers & Live Probing
              </span>
              <span className="text-[10px] text-muted">Configure keys to load real models</span>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {providers.map((p) => {
                const isProbing = probing[p.name];
                const probeRes = probeStatus[p.name];
                const hasProbedModels = probedModels[p.name]?.length > 0;
                
                return (
                  <div key={p.name} className="bg-panel2 border border-edge rounded p-3 flex flex-col justify-between space-y-2">
                    <div>
                      <div className="flex items-center justify-between">
                        <span className="font-semibold text-txt text-[12px] uppercase">{p.name}</span>
                        <span className={`px-2 py-0.5 rounded-full text-[9px] uppercase font-bold ${
                          p.has_key 
                            ? "bg-accent/10 border border-accent/20 text-accent" 
                            : "bg-edge text-faint"
                        }`}>
                          {p.has_key ? "key set" : "no key"}
                        </span>
                      </div>
                      <div className="text-[10px] text-faint mt-0.5 font-mono">{p.env_var}</div>
                    </div>

                    {/* Inline password setter */}
                    <div className="flex gap-1.5 mt-2">
                      <input
                        type="password"
                        placeholder={p.has_key ? "replace key..." : "enter key..."}
                        value={keyInputs[p.name] || ""}
                        onChange={(e) => setKeyInputs({ ...keyInputs, [p.name]: e.target.value })}
                        className="flex-1 bg-panel border border-edge rounded px-2 py-1 text-[11px] font-mono text-txt focus:outline-none"
                      />
                      <button
                        onClick={() => handleSaveKey(p.name)}
                        disabled={savingKey[p.name] || !keyInputs[p.name]}
                        className="bg-accent/10 hover:bg-accent/20 text-accent border border-accent/30 hover:border-accent/50 rounded px-2.5 py-1 text-[11px] font-medium disabled:opacity-30 transition-colors"
                      >
                        {savingKey[p.name] ? "saving" : "save"}
                      </button>
                    </div>

                    {/* Probing block */}
                    {p.has_key && (
                      <div className="pt-2 border-t border-edge/40 flex items-center justify-between">
                        <button
                          onClick={() => handleProbe(p.name)}
                          disabled={isProbing}
                          className="bg-edge hover:bg-edge/80 text-txt rounded px-2.5 py-1 text-[10px] transition-colors font-medium"
                        >
                          {isProbing ? "Loading..." : "Load models"}
                        </button>

                        <div className="text-right text-[10px]">
                          {probeRes && (
                            <div className="flex flex-col">
                              <span className="text-muted">
                                Source: <span className="font-semibold text-txt uppercase">{probeRes.source}</span>
                              </span>
                              {hasProbedModels && (
                                <span className="text-accent">
                                  {probedModels[p.name].length} models found
                                </span>
                              )}
                              {probeRes.error && (
                                <span className="text-risk text-[9px] block max-w-[150px] truncate" title={probeRes.error}>
                                  {probeRes.error}
                                </span>
                              )}
                            </div>
                          )}
                          {probeErrors[p.name] && (
                            <span className="text-risk text-[9px]">{probeErrors[p.name]}</span>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>

          {/* Section C: Pilot Driver */}
          <div className="space-y-3 bg-panel2 border border-edge rounded p-3">
            <div className="flex items-center justify-between border-b border-edge pb-1.5">
              <span className="uppercase tracking-wider text-[11px] text-faint font-bold">
                2. Conversational Pilot Driver
              </span>
              <span className="text-[10px] text-muted">The main model you chat with</span>
            </div>

            <div className="space-y-2">
              <label className="block text-faint font-medium">Select Pilot Model</label>
              <select
                value={pilot}
                onChange={(e) => setPilot(e.target.value)}
                className="w-full bg-panel border border-edge rounded px-2.5 py-2 text-txt focus:outline-none focus:border-accent"
              >
                <option value="">-- select pilot model --</option>
                {modelOptions.map((opt) => (
                  <option key={opt} value={opt}>
                    {opt}
                  </option>
                ))}
              </select>

              {pilotValidation && (
                <div className="text-[11px] flex items-center gap-1.5 mt-1">
                  <span className="text-faint">Status:</span>
                  <span className={pilotValidation.valid ? "text-good font-semibold" : "text-risk font-semibold"}>
                    {pilotValidation.valid 
                      ? `valid -> ${pilotValidation.resolved_model_id}` 
                      : pilotValidation.reason}
                  </span>
                </div>
              )}
            </div>
          </div>

          {/* Section D: Roles */}
          <div className="space-y-3">
            <div className="flex items-center justify-between border-b border-edge/60 pb-1.5">
              <span className="uppercase tracking-wider text-[11px] text-faint font-bold">
                3. Task Worker Models & Capability thresholds
              </span>
              <div className="flex items-center gap-3">
                <button
                  onClick={handleRecommend}
                  disabled={loadingRecommend}
                  className="bg-accent/15 hover:bg-accent/25 text-accent border border-accent/30 hover:border-accent/50 rounded-full px-3 py-1 text-[10px] font-semibold flex items-center gap-1 transition-colors"
                >
                  <Sparkles size={11} />
                  {loadingRecommend ? "fetching recommendations" : "Recommended Setup"}
                </button>
              </div>
            </div>

            <div className="space-y-2.5">
              {Object.keys(roles).length === 0 ? (
                <div className="text-center py-4 text-faint">No task roles returned from server</div>
              ) : (
                Object.keys(roles).map((role) => {
                  const threshold = roles[role];
                  const selectedModel = roleModels[role] || "";
                  
                  return (
                    <div key={role} className="grid grid-cols-1 md:grid-cols-12 gap-3 items-center bg-panel2 border border-edge/40 p-2 rounded">
                      {/* Role Label */}
                      <div className="md:col-span-3 font-semibold text-txt uppercase text-[11px]">
                        {role}
                      </div>

                      {/* Worker Model Select */}
                      <div className="md:col-span-5">
                        <select
                          value={selectedModel}
                          onChange={(e) => setRoleModels({ ...roleModels, [role]: e.target.value })}
                          className="w-full bg-panel border border-edge rounded px-2 py-1 text-[11px] text-txt focus:outline-none"
                        >
                          <option value="">-- auto-select by capability --</option>
                          {modelOptions.map((opt) => (
                            <option key={opt} value={opt}>
                              {opt}
                            </option>
                          ))}
                        </select>
                      </div>

                      {/* Threshold Slider + Input */}
                      <div className="md:col-span-4 flex items-center gap-2">
                        <input
                          type="range"
                          min="0"
                          max="100"
                          value={threshold}
                          onChange={(e) => setRoles({ ...roles, [role]: parseInt(e.target.value) || 0 })}
                          className="flex-1 accent-accent"
                        />
                        <input
                          type="number"
                          min="0"
                          max="100"
                          value={threshold}
                          onChange={(e) => {
                            const val = Math.max(0, Math.min(100, parseInt(e.target.value) || 0));
                            setRoles({ ...roles, [role]: val });
                          }}
                          className="w-12 text-center bg-panel border border-edge rounded p-0.5 font-mono text-[11px] text-txt"
                        />
                      </div>
                    </div>
                  );
                })
              )}
            </div>
          </div>

          {/* Section E: Routing Policy */}
          <div className="space-y-3 bg-panel2 border border-edge rounded p-3">
            <div className="flex items-center justify-between border-b border-edge pb-1.5">
              <span className="uppercase tracking-wider text-[11px] text-faint font-bold">
                4. Global Routing Policy
              </span>
              <span className="text-[10px] text-muted">How model selection is optimized</span>
            </div>

            <div className="space-y-2">
              <label className="block text-faint font-medium">Select Policy</label>
              <select
                value={routingPolicy}
                onChange={(e) => setRoutingPolicy(e.target.value)}
                className="w-full bg-panel border border-edge rounded px-2.5 py-2 text-txt focus:outline-none focus:border-accent"
              >
                {policies.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
              <p className="text-[10px] text-muted">
                Balanced: selects appropriate strength for cost. Cheap: aggressively favors low-cost models. Quality: maximizes accuracy. Escalating: starts cheap and upgrades on failure.
              </p>
            </div>
          </div>

          {/* Section F: Registry Scores (Collapsible Advanced) */}
          <div className="border border-edge rounded overflow-hidden">
            <button
              onClick={() => setShowRegistryScores(!showRegistryScores)}
              className="w-full bg-panel2 px-3 py-2 text-left uppercase tracking-wider text-[10px] text-faint font-semibold flex justify-between items-center hover:bg-edge/40 transition-colors"
            >
              <span>5. Registry Capability Scores (Advanced)</span>
              <span>{showRegistryScores ? "collapse" : "expand"}</span>
            </button>

            {showRegistryScores && (
              <div className="p-3 space-y-2.5 bg-panel border-t border-edge">
                {registryModels.length === 0 ? (
                  <div className="text-center py-2 text-faint">No model records in local registry.json</div>
                ) : (
                  registryModels.map((m, idx) => (
                    <div key={m.id} className="grid grid-cols-1 md:grid-cols-12 gap-3 items-center border-b border-edge/30 pb-2 last:border-b-0">
                      <div className="md:col-span-5 font-mono text-[11px] text-txt truncate" title={m.id}>
                        {m.id}
                      </div>

                      {/* Read only tags */}
                      <div className="md:col-span-3 flex flex-wrap gap-1">
                        {m.tags && m.tags.map((t) => (
                          <span key={t} className="bg-edge text-faint px-1.5 py-0.5 rounded text-[9px] uppercase">
                            {t}
                          </span>
                        ))}
                      </div>

                      {/* Capability slider */}
                      <div className="md:col-span-4 flex items-center gap-2">
                        <input
                          type="range"
                          min="0"
                          max="100"
                          value={m.capability_score}
                          onChange={(e) => {
                            const val = parseInt(e.target.value) || 0;
                            const updated = [...registryModels];
                            updated[idx] = { ...m, capability_score: val };
                            setRegistryModels(updated);
                          }}
                          className="flex-1 accent-accent"
                        />
                        <input
                          type="number"
                          min="0"
                          max="100"
                          value={m.capability_score}
                          onChange={(e) => {
                            const val = Math.max(0, Math.min(100, parseInt(e.target.value) || 0));
                            const updated = [...registryModels];
                            updated[idx] = { ...m, capability_score: val };
                            setRegistryModels(updated);
                          }}
                          className="w-12 text-center bg-panel2 border border-edge rounded p-0.5 font-mono text-[11px]"
                        />
                      </div>
                    </div>
                  ))
                )}
              </div>
            )}
          </div>
        </div>

        {/* Footer/Save action */}
        <div className="px-4 py-3 border-t border-edge bg-panel2 flex items-center justify-between gap-4">
          <button
            onClick={onClose}
            className="border border-edge hover:bg-edge/40 text-txt rounded px-4 py-2 font-medium transition-colors"
          >
            Close
          </button>

          <button
            onClick={handleSaveAll}
            disabled={saving || (pilotValidation !== null && !pilotValidation.valid)}
            className="bg-accent text-txt hover:bg-accent/90 border border-accent/20 rounded px-5 py-2 font-bold disabled:opacity-30 transition-colors"
          >
            {saving ? "saving configuration" : "Save All"}
          </button>
        </div>
      </div>
    </div>
  );
}