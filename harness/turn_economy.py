"""Session-scoped TurnEconomy facade over existing context-economy helpers.

Thin delegation only — no behavior changes, no budget-governor merge, no
pilot-loop control flow. Call sites in ConversationalSession stay on the
underlying helpers until a later PR migrates them through this facade.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .append_only_context import append_only_setting, should_enable_append_only
from .compaction_advisor import advice_payload, assess_layer_pressure
from .context_budget import BudgetConfig, CompactionCallback, enforce_turn_budget, maybe_persist_result
from .tool_output_savings import make_compaction_callback
from .turn_budget import parse_turn_budget
from .wiki_grounding_savings import try_record_grounding


class TurnEconomy:
    """Session-scoped wrappers around persist / batch / directive / grounding helpers.

    ``offload_policy`` and ``spill_registry`` remain behind ``maybe_persist_result`` /
    ``enforce_turn_budget``; this facade does not reimplement them.
    """

    def __init__(
        self,
        state_dir: str,
        session_id: str,
        job_id: Optional[str] = None,
        config: Optional[BudgetConfig] = None,
    ) -> None:
        self.state_dir = state_dir
        self.session_id = session_id or "default"
        self.job_id = job_id
        self.config = config if config is not None else BudgetConfig()

    def persist_tool_result(
        self,
        content: str,
        tool_call_id: str,
        *,
        threshold: Optional[int] = None,
        head_tail: Optional[bool] = None,
        dedupe: bool = False,
        on_compaction: Optional[CompactionCallback] = None,
    ) -> str:
        """Delegate to ``maybe_persist_result`` with session spill + savings callback."""
        callback = on_compaction
        if callback is None:
            callback = make_compaction_callback(
                state_dir=self.state_dir,
                session_id=self.session_id,
                tool_call_id=tool_call_id,
                job_id=self.job_id,
            )
        return maybe_persist_result(
            content=content,
            result_id=tool_call_id,
            state_dir=self.state_dir,
            config=self.config,
            threshold=threshold,
            head_tail=head_tail,
            dedupe=dedupe,
            on_compaction=callback,
            spill_session_id=self.session_id,
        )

    def enforce_tool_batch(
        self,
        messages: List[Dict[str, Any]],
        *,
        on_compaction: Optional[CompactionCallback] = None,
    ) -> List[Dict[str, Any]]:
        """Delegate to ``enforce_turn_budget`` with session savings ids."""
        return enforce_turn_budget(
            tool_messages=messages,
            state_dir=self.state_dir,
            config=self.config,
            on_compaction=on_compaction,
            savings_session_id=self.session_id,
            savings_job_id=self.job_id,
        )

    def parse_output_directive(self, text: str) -> Optional[dict[str, Any]]:
        """Delegate to ``parse_turn_budget``."""
        return parse_turn_budget(text)

    def resolve_append_only(self, base_url: str = "", driver_name: str = "") -> bool:
        """Delegate to append-only setting + enablement helpers."""
        return should_enable_append_only(
            append_only_setting(),
            base_url,
            driver_name,
        )

    def record_wiki_grounding(
        self,
        chars: int,
        pages: int,
        *,
        price_in: Optional[float] = None,
    ) -> None:
        """Delegate to ``try_record_grounding`` for this session."""
        try_record_grounding(
            state_dir=self.state_dir,
            session_id=self.session_id,
            chars=chars,
            pages=pages,
            price_in=price_in,
        )

    def advise_compaction(
        self,
        max_context_tokens: int,
        *,
        snapshot: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Delegate to compaction advisor helpers.

        With ``snapshot``, calls ``assess_layer_pressure``. Without, loads the
        latest session snapshot via ``advice_payload``.
        """
        if snapshot is not None:
            return assess_layer_pressure(snapshot, max_context_tokens)
        return advice_payload(self.state_dir, self.session_id, max_context_tokens)
