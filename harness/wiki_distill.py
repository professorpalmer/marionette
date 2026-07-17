from __future__ import annotations

"""Wiki grounding + distill mixin for ConversationalSession.

Extracted mechanically from harness/conversation.py to continue decomposing the
ConversationalSession god-object, matching ToolDispatchMixin / LocalJobsMixin
contract: these methods operate through `self` (``_wiki``, ``_turn_economy``,
``_session_findings``, ``pilot``, ``config``, …) provided by the concrete
class -- the mixin defines no state and no __init__.

Busy lifecycle lives on BusyControlMixin; ``send`` / ``_send_locked_inner``
live on SendLoopMixin; turn-trailer assembly stays on ConversationalSession.
``TurnEconomy.record_wiki_grounding`` wiring inside ``_build_turn_wiki_section``
is preserved verbatim.

Method Resolution Order keeps behavior identical: ``_build_turn_wiki_section``,
``distill``, ``prepare_wiki_pages``, etc. still resolve via inheritance.
"""

import os
import re

from .skill_distiller import distill_session, distill_rules
from .wiki import session_digest


def _slugify(s: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "session")[:60]


class WikiDistillMixin:
    """Mixin holding wiki grounding, wiki ingest, and skill/rule distill helpers.

    The concrete class (ConversationalSession) supplies the state these
    methods read/write via `self`. This mixin defines no __init__ and no
    instance state of its own.
    """

    _WIKI_GROUNDING_MAX_CHARS = 8000  # ~2k tokens at chars//4

    def _wiki_grounding_query(self, user_message: str) -> str:
        """Build a compact wiki search query from the user turn and repo."""
        parts: list[str] = []
        repo = str(getattr(self.config, "repo", "") or "").strip()
        if repo:
            base = os.path.basename(os.path.normpath(repo))
            if base and base not in (".", ".."):
                parts.append(base)
        msg = (user_message or "").strip()
        if msg:
            parts.append(msg[:400])
        return " ".join(parts).strip()

    def _build_turn_wiki_section(self, user_message: str) -> str:
        wiki_section = ""
        if not self._wiki.configured:
            return wiki_section
        try:
            query = self._wiki_grounding_query(user_message)
            if not query:
                return wiki_section
            hits = self._wiki.search_pages(query, limit=5)
            if not hits:
                self._wiki_cache_key = user_message
                self._wiki_cache_section = ""
                self._wiki_cache_pages = 0
                return wiki_section

            authoritative = (
                "WIKI HAS ALREADY BEEN QUERIED FOR THIS TURN. Relevant notes and "
                "decisions from your durable wiki are provided in the section below. "
                "USE THIS as your primary source for prior decisions and findings. "
                "Do NOT call query_wiki to re-fetch what is already here unless the "
                "question is outside this injected slice.\n"
            )
            lines = [authoritative, "### Wiki grounding (auto-injected)"]
            budget = self._WIKI_GROUNDING_MAX_CHARS - len(authoritative) - 40
            per_hit = max(120, budget // max(1, len(hits)))
            for hit in hits:
                title = str(hit.get("title") or hit.get("slug") or "").strip()
                slug = str(hit.get("slug") or "").strip()
                snippet = str(hit.get("snippet") or "").strip()
                if len(snippet) > per_hit:
                    snippet = snippet[:per_hit].rstrip() + "…"
                label = title or slug or "untitled"
                if slug and slug != label:
                    label = f"{label} ({slug})"
                lines.append(f"- {label}: {snippet}" if snippet else f"- {label}")

            wiki_section = "\n".join(lines)
            if len(wiki_section) > self._WIKI_GROUNDING_MAX_CHARS:
                wiki_section = wiki_section[: self._WIKI_GROUNDING_MAX_CHARS].rstrip() + "…"

            try:
                from pmharness.registry import resolve_price

                price_in, _ = resolve_price(self.config.driver)
                self._turn_economy.record_wiki_grounding(
                    len(wiki_section),
                    len(hits),
                    price_in=price_in,
                )
            except Exception:
                pass

            self._wiki_cache_key = user_message
            self._wiki_cache_section = wiki_section
            self._wiki_cache_pages = len(hits)
        except Exception:
            pass
        return wiki_section

    def _wiki_grounding_fields(self) -> dict:
        """Compact wiki grounding stats for context/usage APIs."""
        try:
            from pmharness.registry import resolve_price

            price_in, _ = resolve_price(self.config.driver)
            return self._turn_economy.wiki_grounding_fields(price_in)
        except Exception:
            return {
                "wiki_groundings": 0,
                "wiki_tokens_fed": 0,
                "wiki_pages_fed": 0,
                "wiki_estimated_reinference_tokens": 0,
                "wiki_estimated_savings_usd": 0.0,
            }

    def _after_wiki_ingest(self) -> None:
        """Notify server after a successful wiki write so graph/status cache refreshes."""
        cb = getattr(self, "_on_wiki_ingest", None)
        if cb is None:
            return
        try:
            cb()
        except Exception:
            pass  # best-effort, like ingest itself

    def _maybe_ingest(self, user_message: str, prose: list, findings: list) -> None:
        """Auto-ingest a session digest to the wiki when enabled and there are
        real findings worth capturing. Never fires the orchestrator (token-spend)."""
        # accumulate for self-learning distillation (independent of wiki config)
        if findings:
            self._session_findings.extend(findings)
            if not self._first_objective:
                self._first_objective = user_message
        if not (self._wiki_auto and self._wiki.configured and findings):
            return
        try:
            digest = session_digest(user_message, prose, findings)
            slug = f"harness-{_slugify(user_message)}"
            r = self._wiki.ingest(slug, digest, note="auto-captured by pm-harness",
                                  run_orchestrator=False)
            if getattr(r, "ok", False):
                self._after_wiki_ingest()
        except Exception:
            pass  # wiki capture is best-effort; never break the conversation

    def prepare_wiki_pages(self) -> dict:
        """Run the LOCAL pilot model to structure this session's digest into
        entity/concept/decision wiki pages (the "backwards" orchestration pass),
        cheaply, without a frontier orchestrator.

        Returns {"status", "pages": [...], "auto_ingested"?: bool, "reason"?}.
        status: prepared | empty | error | not_configured | no_signal.
        Human-gated by default: pages are PREPARED and returned for approval.
        With HARNESS_WIKI_ORCHESTRATE=auto they are also ingested immediately.
        Never raises -- best-effort.
        """
        if not self._wiki.configured:
            return {"status": "not_configured", "pages": []}
        # Only act when there is genuinely new durable signal this session.
        if len(self._session_findings) <= self._wiki_prepared_hwm or not self._session_findings:
            return {"status": "no_signal", "pages": []}
        try:
            from .wiki_orchestrator import prepare_pages
            digest = self._build_transcript_digest() or session_digest(
                self._first_objective or "(session)", [], self._session_findings)
            res = prepare_pages(self.pilot, self._first_objective or "(session)", digest)
        except Exception as e:
            return {"status": "error", "pages": [], "reason": str(e)}

        self._wiki_prepared_hwm = len(self._session_findings)
        pages = res.get("pages", [])
        if res.get("status") != "prepared" or not pages:
            return {"status": res.get("status", "empty"), "pages": []}

        if self._wiki_orchestrate_auto:
            ingested = self.ingest_prepared_pages(pages)
            return {"status": "prepared", "pages": pages,
                    "auto_ingested": True, "ingested": ingested}
        return {"status": "prepared", "pages": pages, "auto_ingested": False}

    def ingest_prepared_pages(self, pages: list) -> int:
        """File approved structured pages into the wiki, one source each, with
        run_orchestrator=False (the local model already did the structuring).
        Returns the count successfully ingested. Best-effort."""
        if not self._wiki.configured or not pages:
            return 0
        count = 0
        for p in pages:
            try:
                kind = (p.get("kind") or "concept").strip()
                title = (p.get("title") or "").strip()
                slug = (p.get("slug") or _slugify(title)).strip()
                body = (p.get("body") or "").strip()
                if not slug or not body:
                    continue
                content = f"# {title}\n\n{body}\n" if title else body
                r = self._wiki.ingest(
                    f"{kind}-{slug}", content,
                    note=f"pm-harness local orchestration ({kind})",
                    run_orchestrator=False)
                if getattr(r, "ok", False):
                    count += 1
            except Exception:
                continue
        if count > 0:
            self._after_wiki_ingest()
        return count

    def _build_transcript_digest(self) -> str:
        lines = []
        for msg in self.export_display_transcript():
            role = msg.get("role", "")
            text = msg.get("text", "")
            if role and text:
                lines.append(f"{role.upper()}: {text}")
        return "\n".join(lines)

    def _maybe_auto_distill(self):
        """If auto-distill is enabled and there is new signal, propose
        PENDING candidates and yield a 'distilled' event. Best-effort."""
        if not self._auto_distill:
            return None

        has_new_findings = len(self._session_findings) > self._distilled_findings_hwm
        has_new_turns = self._turn_count > self._distilled_turns_hwm
        has_new_corrections = len(self._corrections) > self._distilled_corrections_hwm

        if not (has_new_findings or has_new_turns or has_new_corrections):
            return None

        self._distilled_findings_hwm = len(self._session_findings)
        self._distilled_turns_hwm = self._turn_count
        self._distilled_corrections_hwm = len(self._corrections)

        try:
            return self.distill()
        except Exception as e:
            return {"status": "error", "reason": str(e)}

    def distill(self) -> dict:
        """Propose PENDING candidate skill(s) AND rule(s) from this session's
        accumulated findings. Human approval required before either loads into
        context. Returns a combined status dict."""
        out = {}
        extra_context = ""
        non_verification_findings = [f for f in self._session_findings if f.get("type") != "verification"]

        is_hard = (self._total_tool_calls >= 8) or getattr(self, "_error_then_recovery_seen", False)
        if len(non_verification_findings) < 2 and is_hard:
            extra_context = self._build_transcript_digest()

        try:
            out["skill"] = distill_session(
                self.pilot,
                self._first_objective or "(session)",
                self._session_findings,
                self._skills,
                extra_context=extra_context
            )
        except Exception as e:
            out["skill"] = {"status": "error", "reason": str(e)}
        try:
            out["rules"] = distill_rules(
                self.pilot,
                self._first_objective or "(session)",
                self._session_findings,
                self._rules,
                corrections=self._corrections
            )
        except Exception as e:
            out["rules"] = {"status": "error", "reason": str(e)}
        return out
