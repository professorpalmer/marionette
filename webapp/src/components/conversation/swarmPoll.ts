/**
 * Pure helpers for the swarm-results poll tick (pending jobs while idle).
 */

import { formatDistilledNotice, formatWikiAutoIngestNotice } from "./streamApply";

export type SwarmPollChrome =
  | { kind: "swarm_result"; data: any }
  | { kind: "pilot_resume" }
  | { kind: "distilled"; notice: string }
  | { kind: "wiki_auto"; notice: string }
  | { kind: "wiki_prepare"; pages: any[] }
  | { kind: "memory_propose"; id: string; text: string; category: string }
  | { kind: "ignore" };

/** Classify one swarm poll event into a chrome action (no side effects). */
export function classifySwarmPollEvent(evt: any): SwarmPollChrome {
  const anyEvt = evt as any;
  if (anyEvt.kind === "swarm_result" && anyEvt.data) {
    return { kind: "swarm_result", data: anyEvt.data };
  }
  if (anyEvt.kind === "pilot_resume") {
    return { kind: "pilot_resume" };
  }
  if (anyEvt.kind === "distilled" && anyEvt.data) {
    const notice = formatDistilledNotice(anyEvt.data);
    if (notice) return { kind: "distilled", notice };
    return { kind: "ignore" };
  }
  if (anyEvt.kind === "wiki_prepared" && anyEvt.data) {
    const d = anyEvt.data;
    const pages = d.pages || [];
    if (pages.length === 0) return { kind: "ignore" };
    if (d.auto_ingested) {
      const notice = formatWikiAutoIngestNotice(pages.length);
      return { kind: "wiki_auto", notice };
    }
    return { kind: "wiki_prepare", pages };
  }
  if (anyEvt.kind === "memory_propose" && anyEvt.data) {
    const d = anyEvt.data;
    const id = d.id || "";
    const text = (d.text || "").trim();
    if (id && text) {
      return { kind: "memory_propose", id, text, category: d.category || "general" };
    }
  }
  return { kind: "ignore" };
}

/** Deduplicate memory proposals by id. */
export function appendMemoryProposal<T extends { id: string }>(
  prev: T[],
  proposal: T,
): T[] {
  return prev.some((p) => p.id === proposal.id) ? prev : [...prev, proposal];
}
