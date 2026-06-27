from __future__ import annotations

import concurrent.futures
from typing import Optional, Callable

from .base import Driver, DriverResponse, SYSTEM_PROMPT

AGGREGATOR_SYSTEM_PROMPT = "You are an aggregator that synthesizes the best single answer from multiple proposer candidates, reconciling disagreements and avoiding simple concatenation."


class MoADriver:
    supports_streaming = False

    def __init__(
        self,
        name: str,
        proposers: list,
        aggregator: str,
        *,
        reach: str = "openrouter",
        max_workers: int = 4,
        temperature: float = 0.0,
        builder: Optional[Callable] = None,
    ) -> None:
        self.name = name
        self.proposer_names = proposers
        self.aggregator_name = aggregator
        self.reach = reach
        self.max_workers = max_workers
        self.temperature = temperature

        if builder is None:
            from pmharness.registry import build as default_builder
            self.builder = default_builder
        else:
            self.builder = builder

        self.proposer_drivers = [self.builder(p, reach=self.reach) for p in self.proposer_names]
        self.aggregator_driver = self.builder(self.aggregator_name, reach=self.reach)

    def complete(self, task_prompt: str, *, system: str = SYSTEM_PROMPT) -> DriverResponse:
        futures = {}
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            for prop in self.proposer_drivers:
                fut = executor.submit(prop.complete, task_prompt, system=system)
                futures[fut] = prop

            responses = []
            for fut in concurrent.futures.as_completed(futures):
                prop = futures[fut]
                try:
                    resp = fut.result()
                    responses.append((prop, resp))
                except Exception as e:
                    responses.append((prop, DriverResponse(text="", error=str(e), model=prop.name)))

        successful_proposers = []
        proposer_tokens_in = 0
        proposer_tokens_out = 0
        errors = []

        # Maintain order or process as completed. Let's process in order of self.proposer_drivers for deterministic prompts.
        resp_by_prop = {p: r for p, r in responses}
        for prop in self.proposer_drivers:
            resp = resp_by_prop.get(prop)
            if not resp:
                continue
            proposer_tokens_in += resp.tokens_in or 0
            proposer_tokens_out += resp.tokens_out or 0
            if resp.error:
                errors.append(f"{prop.name}: {resp.error}")
            else:
                successful_proposers.append((prop.name, resp.text))

        if not successful_proposers:
            err_msg = "all MoA proposers failed: " + "; ".join(errors)
            return DriverResponse(
                text="",
                tokens_in=proposer_tokens_in,
                tokens_out=proposer_tokens_out,
                model=self.name,
                error=err_msg,
                meta={
                    "moa": {
                        "proposers": self.proposer_names,
                        "aggregator": self.aggregator_name,
                        "proposer_tokens_in": proposer_tokens_in,
                        "proposer_tokens_out": proposer_tokens_out,
                        "n_proposers_ok": 0,
                    }
                },
            )

        agg_prompt = f"Original task:\n{task_prompt}\n\nHere are candidate answers proposed by different models:\n\n"
        for i, (p_name, text) in enumerate(successful_proposers, 1):
            agg_prompt += f"Proposal {i} ({p_name}):\n{text}\n\n"
        agg_prompt += "Please synthesize these proposals into the single best answer. Reconcile disagreements, improve accuracy, and provide a single cohesive output. Do not just concatenate them."

        agg_response = self.aggregator_driver.complete(agg_prompt, system=AGGREGATOR_SYSTEM_PROMPT)

        total_tokens_in = (agg_response.tokens_in or 0) + proposer_tokens_in
        total_tokens_out = (agg_response.tokens_out or 0) + proposer_tokens_out

        moa_meta = {
            "proposers": self.proposer_names,
            "aggregator": self.aggregator_name,
            "proposer_tokens_in": proposer_tokens_in,
            "proposer_tokens_out": proposer_tokens_out,
            "n_proposers_ok": len(successful_proposers),
        }

        # Keep existing meta if aggregator returned any, merge in moa_meta
        merged_meta = dict(agg_response.meta) if agg_response.meta else {}
        merged_meta["moa"] = moa_meta

        return DriverResponse(
            text=agg_response.text,
            tokens_in=total_tokens_in,
            tokens_out=total_tokens_out,
            latency_ms=agg_response.latency_ms,
            model=self.name,
            error=agg_response.error,
            meta=merged_meta,
        )

    def chat(self, messages: list, *, tools: Optional[list] = None, system: Optional[str] = None) -> DriverResponse:
        if tools:
            return DriverResponse(
                text="",
                model=self.name,
                error="MoA is a planner/review virtual-model and cannot be used as the tool-calling executor; use a single strong model for execution",
            )

        futures = {}
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            for prop in self.proposer_drivers:
                fut = executor.submit(prop.chat, messages, tools=None, system=system)
                futures[fut] = prop

            responses = []
            for fut in concurrent.futures.as_completed(futures):
                prop = futures[fut]
                try:
                    resp = fut.result()
                    responses.append((prop, resp))
                except Exception as e:
                    responses.append((prop, DriverResponse(text="", error=str(e), model=prop.name)))

        successful_proposers = []
        proposer_tokens_in = 0
        proposer_tokens_out = 0
        errors = []

        resp_by_prop = {p: r for p, r in responses}
        for prop in self.proposer_drivers:
            resp = resp_by_prop.get(prop)
            if not resp:
                continue
            proposer_tokens_in += resp.tokens_in or 0
            proposer_tokens_out += resp.tokens_out or 0
            if resp.error:
                errors.append(f"{prop.name}: {resp.error}")
            else:
                successful_proposers.append((prop.name, resp.text))

        if not successful_proposers:
            err_msg = "all MoA proposers failed: " + "; ".join(errors)
            return DriverResponse(
                text="",
                tokens_in=proposer_tokens_in,
                tokens_out=proposer_tokens_out,
                model=self.name,
                error=err_msg,
                meta={
                    "moa": {
                        "proposers": self.proposer_names,
                        "aggregator": self.aggregator_name,
                        "proposer_tokens_in": proposer_tokens_in,
                        "proposer_tokens_out": proposer_tokens_out,
                        "n_proposers_ok": 0,
                    },
                    "tool_calls": [],
                },
            )

        flat_history = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            flat_history.append(f"{role.upper()}: {content}")
        flat_history_str = "\n".join(flat_history)

        agg_prompt = f"Original conversation history:\n{flat_history_str}\n\nHere are candidate answers proposed by different models:\n\n"
        for i, (p_name, text) in enumerate(successful_proposers, 1):
            agg_prompt += f"Proposal {i} ({p_name}):\n{text}\n\n"
        agg_prompt += "Please synthesize these proposals into the single best reply. Reconcile disagreements, improve accuracy, and provide a single cohesive output. Do not just concatenate them."

        agg_response = self.aggregator_driver.complete(agg_prompt, system=AGGREGATOR_SYSTEM_PROMPT)

        total_tokens_in = (agg_response.tokens_in or 0) + proposer_tokens_in
        total_tokens_out = (agg_response.tokens_out or 0) + proposer_tokens_out

        moa_meta = {
            "proposers": self.proposer_names,
            "aggregator": self.aggregator_name,
            "proposer_tokens_in": proposer_tokens_in,
            "proposer_tokens_out": proposer_tokens_out,
            "n_proposers_ok": len(successful_proposers),
        }

        merged_meta = dict(agg_response.meta) if agg_response.meta else {}
        merged_meta["moa"] = moa_meta
        merged_meta["tool_calls"] = []

        return DriverResponse(
            text=agg_response.text,
            tokens_in=total_tokens_in,
            tokens_out=total_tokens_out,
            latency_ms=agg_response.latency_ms,
            model=self.name,
            error=agg_response.error,
            meta=merged_meta,
        )
