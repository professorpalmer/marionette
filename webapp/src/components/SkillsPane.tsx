import { useEffect, useState } from "react";
import { GraduationCap, Check, X, Archive, Sparkles, Plus } from "lucide-react";
import { api } from "../lib/api";

// Self-learning panel: review AUTO-DISTILLED candidate skills (PENDING) and
// approve/reject. Approved skills load into the pilot's context next session.
// The human gate is the point -- a bad auto-skill is worse than none.
export default function SkillsPane({ embedded = false }: { embedded?: boolean }) {
  const [skills, setSkills] = useState<any[]>([]);
  const [rules, setRules] = useState<any[]>([]);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");
  const [expanded, setExpanded] = useState<string>("");
  const [newRuleText, setNewRuleText] = useState("");
  const [newRuleScope, setNewRuleScope] = useState("global");
  const [newSkillName, setNewSkillName] = useState("");
  const [newSkillDesc, setNewSkillDesc] = useState("");
  const [newSkillBody, setNewSkillBody] = useState("");
  const [formError, setFormError] = useState("");

  const refresh = () => {
    api.skills().then(setSkills).catch(() => {});
    api.rules().then(setRules).catch(() => {});
  };
  useEffect(() => { refresh(); const t = setInterval(refresh, 5000); return () => clearInterval(t); }, []);

  const distill = async () => {
    setBusy("distill"); setMsg("");
    try {
      const r = await api.skillDistill();
      const sk = r.skill?.status === "proposed" ? `skill: ${r.skill.name}` : `skill: ${r.skill?.status || "none"}`;
      const ru = r.rules?.status === "proposed" ? `rules: ${r.rules.proposed?.length || 0}` : `rules: ${r.rules?.status || "none"}`;
      setMsg(`Distilled -- ${sk}, ${ru}`);
      await refresh();
    } finally { setBusy(""); }
  };
  // The backend answers HTTP 200 {ok:false} when a slug can't be found, so a
  // bare await+refresh made a failed click look like a silent no-op. Surface
  // the failure in the message strip instead.
  const act = async (slug: string, call: (slug: string) => Promise<{ ok: boolean }>, verb: string) => {
    setBusy(slug); setMsg("");
    try {
      const res = await call(slug);
      if (!res.ok) { setMsg(`${verb} failed -- item not found (try Distill again)`); return; }
      await refresh();
    } catch {
      setMsg(`${verb} failed`);
    } finally { setBusy(""); }
  };
  const approve = (slug: string) => act(slug, api.skillApprove, "Approve");
  const reject = (slug: string) => act(slug, api.skillReject, "Reject");
  const approveRule = (slug: string) => act(slug, api.ruleApprove, "Approve");
  const rejectRule = (slug: string) => act(slug, api.ruleReject, "Reject");

  const addRule = async () => {
    const text = newRuleText.trim();
    if (!text) { setFormError("Rule text is required"); return; }
    setFormError(""); setBusy("add-rule");
    try {
      await api.ruleAdd(text, newRuleScope.trim() || "global");
      setNewRuleText("");
      setMsg("Rule added");
      await refresh();
    } catch {
      setFormError("Failed to add rule");
    } finally { setBusy(""); }
  };

  const addSkill = async () => {
    const name = newSkillName.trim();
    if (!name) { setFormError("Skill name is required"); return; }
    setFormError(""); setBusy("add-skill");
    try {
      await api.skillAdd(name, newSkillDesc.trim(), newSkillBody.trim());
      setNewSkillName(""); setNewSkillDesc(""); setNewSkillBody("");
      setMsg("Skill added");
      await refresh();
    } catch {
      setFormError("Failed to add skill");
    } finally { setBusy(""); }
  };

  const pendingRules = rules.filter((r) => r.state === "pending");
  const activeRules = rules.filter((r) => r.state === "active");

  const pending = skills.filter((s) => s.state === "pending");
  const active = skills.filter((s) => s.state === "active");

  return (
    <div className={embedded ? "text-[12px] flex flex-col gap-2" : "flex flex-col h-full text-[12px]"}>
      {!embedded && (
        <div className="flex items-center justify-between px-3 py-2 border-b border-edge">
          <span className="uppercase tracking-wider text-[10px] text-faint font-medium flex items-center gap-1.5">
            <GraduationCap size={11} /> Skills
          </span>
          <button onClick={distill} disabled={busy === "distill"}
            className="text-[10px] flex items-center gap-1 px-1.5 h-5 rounded bg-accent2 text-accent hover:brightness-125 disabled:opacity-40">
            <Sparkles size={10} /> Distill session
          </button>
        </div>
      )}

      <div className={embedded ? "space-y-2" : "flex-1 overflow-y-auto p-2 flex flex-col gap-2"}>
        {embedded && (
          <div className="flex justify-between items-center bg-panel2/40 border border-edge/30 rounded p-2 mb-1">
            <span className="text-faint text-[10px]">Analyze current session to propose new skills and rules.</span>
            <button onClick={distill} disabled={busy === "distill"}
              className="text-[10px] flex items-center gap-1 px-2 py-1 rounded bg-accent2 text-accent hover:brightness-125 disabled:opacity-40">
              <Sparkles size={10} /> Distill session
            </button>
          </div>
        )}
        {msg && <div className="text-[10px] text-muted px-1">{msg}</div>}
        {formError && <div className="text-[10px] text-risk px-1">{formError}</div>}

        <div className="border border-edge/50 rounded-lg p-2 bg-panel2/30 space-y-2">
          <div className="uppercase tracking-wider text-[10px] text-faint font-semibold flex items-center gap-1">
            <Plus size={10} /> Add rule
          </div>
          <input
            type="text"
            placeholder="Always run tests before claiming done"
            value={newRuleText}
            onChange={(e) => setNewRuleText(e.target.value)}
            className="w-full bg-panel2 border border-edge rounded px-2 py-1 text-txt placeholder:text-faint text-[11px] focus:outline-none focus:border-accent"
          />
          <input
            type="text"
            placeholder="Scope (default: global)"
            value={newRuleScope}
            onChange={(e) => setNewRuleScope(e.target.value)}
            className="w-full bg-panel2 border border-edge rounded px-2 py-1 text-txt placeholder:text-faint text-[11px] focus:outline-none focus:border-accent font-mono"
          />
          <button onClick={addRule} disabled={busy === "add-rule"}
            className="w-full h-6 rounded bg-accent2 text-accent text-[10px] font-medium hover:brightness-125 disabled:opacity-40">
            Add rule
          </button>
        </div>

        <div className="border border-edge/50 rounded-lg p-2 bg-panel2/30 space-y-2">
          <div className="uppercase tracking-wider text-[10px] text-faint font-semibold flex items-center gap-1">
            <Plus size={10} /> Add skill
          </div>
          <input
            type="text"
            placeholder="Skill name"
            value={newSkillName}
            onChange={(e) => setNewSkillName(e.target.value)}
            className="w-full bg-panel2 border border-edge rounded px-2 py-1 text-txt placeholder:text-faint text-[11px] focus:outline-none focus:border-accent"
          />
          <input
            type="text"
            placeholder="When to use this skill"
            value={newSkillDesc}
            onChange={(e) => setNewSkillDesc(e.target.value)}
            className="w-full bg-panel2 border border-edge rounded px-2 py-1 text-txt placeholder:text-faint text-[11px] focus:outline-none focus:border-accent"
          />
          <textarea
            placeholder="Numbered steps (markdown)"
            value={newSkillBody}
            onChange={(e) => setNewSkillBody(e.target.value)}
            rows={3}
            className="w-full bg-panel2 border border-edge rounded px-2 py-1 text-txt placeholder:text-faint text-[11px] focus:outline-none focus:border-accent font-mono resize-y"
          />
          <button onClick={addSkill} disabled={busy === "add-skill"}
            className="w-full h-6 rounded bg-accent2 text-accent text-[10px] font-medium hover:brightness-125 disabled:opacity-40">
            Add skill
          </button>
        </div>

        {pending.length > 0 && (
          <div>
            <div className="uppercase tracking-wider text-[10px] text-warn mb-1 px-1">Pending review ({pending.length})</div>
            {pending.map((s) => (
              <div key={s.slug} className="border border-warn/30 rounded-lg p-2 bg-warn/5 mb-1.5">
                <div className="font-medium text-txt">{s.name}</div>
                <div className="text-faint text-[10px] mt-0.5">{s.description}</div>
                <button onClick={() => setExpanded(expanded === s.slug ? "" : s.slug)}
                  className="text-accent text-[10px] mt-1">{expanded === s.slug ? "hide" : "view steps"}</button>
                {expanded === s.slug && <pre className="text-[10px] text-muted whitespace-pre-wrap mt-1 font-mono">{s.body}</pre>}
                <div className="flex gap-1.5 mt-2">
                  <button onClick={() => approve(s.slug)} disabled={busy === s.slug}
                    className="flex-1 h-6 rounded bg-good/20 text-good text-[10px] font-medium flex items-center justify-center gap-1"><Check size={11} /> Approve</button>
                  <button onClick={() => reject(s.slug)} disabled={busy === s.slug}
                    className="flex-1 h-6 rounded bg-risk/15 text-risk text-[10px] font-medium flex items-center justify-center gap-1"><X size={11} /> Reject</button>
                </div>
              </div>
            ))}
          </div>
        )}

        <div>
          <div className="uppercase tracking-wider text-[10px] text-faint mb-1 px-1">Active ({active.length})</div>
          {active.length === 0 && <div className="text-faint text-[10px] px-1">No active skills yet. Distill a finished session to propose one.</div>}
          {active.map((s) => (
            <div key={s.slug} className="border border-edge rounded-lg p-2 bg-panel2/40 mb-1.5">
              <div className="flex items-center gap-2">
                <span className="font-medium text-txt flex-1 truncate">{s.name}</span>
                <span className="text-faint text-[10px]">used {s.used_count}x</span>
                <button onClick={() => reject(s.slug)} title="Archive" className="text-muted hover:text-risk"><Archive size={11} /></button>
              </div>
              <div className="text-faint text-[10px] mt-0.5">{s.description}</div>
            </div>
          ))}
        </div>

        {pendingRules.length > 0 && (
          <div>
            <div className="uppercase tracking-wider text-[10px] text-warn mb-1 px-1">Pending rules ({pendingRules.length})</div>
            {pendingRules.map((r) => (
              <div key={r.slug} className="border border-warn/30 rounded-lg p-2 bg-warn/5 mb-1.5">
                <div className="text-txt text-[11px]">{r.text}</div>
                <div className="flex gap-1.5 mt-1.5">
                  <button onClick={() => approveRule(r.slug)} disabled={busy === r.slug}
                    className="flex-1 h-6 rounded bg-good/20 text-good text-[10px] font-medium flex items-center justify-center gap-1"><Check size={11} /> Approve</button>
                  <button onClick={() => rejectRule(r.slug)} disabled={busy === r.slug}
                    className="flex-1 h-6 rounded bg-risk/15 text-risk text-[10px] font-medium flex items-center justify-center gap-1"><X size={11} /> Reject</button>
                </div>
              </div>
            ))}
          </div>
        )}

        <div>
          <div className="uppercase tracking-wider text-[10px] text-faint mb-1 px-1">Active rules ({activeRules.length})</div>
          {activeRules.length === 0 && <div className="text-faint text-[10px] px-1">No active rules yet.</div>}
          {activeRules.map((r) => (
            <div key={r.slug} className="border border-edge rounded-lg p-2 bg-panel2/40 mb-1.5 flex items-center gap-2">
              <span className="text-txt text-[11px] flex-1">{r.text}</span>
              <button onClick={() => rejectRule(r.slug)} title="Archive" className="text-muted hover:text-risk"><Archive size={11} /></button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
