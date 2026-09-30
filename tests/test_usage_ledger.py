"""The usage ledger's guarantees, one test each."""
from __future__ import annotations

import random

import pytest

from pmharness.drivers.base import DriverResponse
from pmharness.drivers.metering import ProviderCall, attribution, metered
from harness.usage_ledger import recorder
from harness.usage_ledger.rates import RateCard, RateLookup, TokenClasses, card_from_models_dev, price
from harness.usage_ledger.recorder import event_from_call
from harness.usage_ledger.store import UsageLedger
from harness.usage_ledger.summary import summarize

# models.dev's published OpenCode Go rate for mimo-v2.6-pro (per MTok).
MIMO = card_from_models_dev({"input": 0.435, "output": 0.87, "cache_read": 0.003625})
SONNET = card_from_models_dev({
    "input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75,
    "context_over_200k": {"input": 6, "output": 22.5, "cache_read": 0.6, "cache_write": 7.5},
})


def _call(driver="opencode-go:mimo-v2.6-pro", model="mimo-v2.6-pro", base_url="https://opencode.ai/zen/go/v1",
          tokens_in=1_000_000, tokens_out=1_000, cache_read=950_000, cache_write=0, cost=None, **attr):
    return ProviderCall(
        driver=driver, model=model, served_model="", base_url=base_url, started_at=1.0, ended_at=2.0,
        tokens_in=tokens_in, tokens_out=tokens_out, cache_read_tokens=cache_read,
        cache_write_tokens=cache_write, cache_write_5m_tokens=0, cache_write_1h_tokens=0,
        reasoning_tokens=0, token_basis="provider", provider_cost_usd=cost, billing_hint="",
        error=None, attribution=attr,
    )


def _rates(card):
    return lambda provider, model, candidates=(): RateLookup(card, "models.dev" if card else None, 1.0 if card else None)


def test_subscription_usage_is_zero_cash_with_an_exact_list_equivalent():
    event = event_from_call(_call(), rates=_rates(MIMO))
    assert (event.provider, event.billing, event.cost_basis, event.cash_usd) == ("opencode-go", "plan", "included", 0.0)
    expected = (50_000 * 0.435 + 950_000 * 0.003625 + 1_000 * 0.87) / 1e6
    assert event.list_usd == pytest.approx(expected)
    # A generic "cache reads cost 10% of input" guess is ~2.5x higher here.
    assert event.list_usd < (50_000 * 0.435 + 950_000 * 0.0435 + 1_000 * 0.87) / 1e6 / 2


def test_metered_calls_prefer_the_providers_reported_cost():
    event = event_from_call(_call(driver="openrouter:z-ai/glm-5.3", model="z-ai/glm-5.3",
                                  base_url="https://openrouter.ai/api/v1", cost=0.0421), rates=_rates(MIMO))
    assert (event.billing, event.cost_basis, event.cash_usd) == ("metered", "reported", 0.0421)
    assert event.list_usd is not None  # list equivalent still kept for comparison


def test_an_unknown_price_is_unpriced_never_zero():
    event = event_from_call(_call(driver="openrouter:new/model", model="new/model",
                                  base_url="https://openrouter.ai/api/v1"), rates=_rates(None))
    assert (event.cost_basis, event.cash_usd, event.list_usd) == ("unpriced", None, None)
    summary = summarize([event])
    assert summary["unpriced_calls"] == 1 and summary["spent_usd"] == 0.0
    assert summary["spent_confidence"] == "none"


def test_loopback_is_local_but_a_remote_local_endpoint_is_metered():
    local = event_from_call(_call(driver="local:openai-compatible-127-0-0-1-8080/qwen", model="qwen",
                                  base_url="http://127.0.0.1:8080/v1"), rates=_rates(None))
    remote = event_from_call(_call(driver="local:openai-compatible-api-example/kimi", model="kimi",
                                   base_url="https://api.example.ai/v1"), rates=_rates(None))
    assert (local.billing, local.cash_usd) == ("local", 0.0)
    assert (remote.billing, remote.cost_basis, remote.provider) == ("metered", "unpriced", "api.example.ai")


def test_context_tiers_and_cache_writes_are_priced_at_their_own_rates():
    small = TokenClasses.split(tokens_in=100_000, tokens_out=0, cache_read=0, cache_write=10_000)
    assert price(small, SONNET) == pytest.approx((90_000 * 3 + 10_000 * 3.75) / 1e6)
    big = TokenClasses.split(tokens_in=300_000, tokens_out=1_000, cache_read=200_000, cache_write=0)
    assert price(big, SONNET) == pytest.approx((100_000 * 6 + 200_000 * 0.6 + 1_000 * 22.5) / 1e6)


def test_history_is_never_repriced(tmp_path):
    ledger = UsageLedger(str(tmp_path / "l.sqlite"))
    ledger.append(event_from_call(_call(driver="openrouter:a/b", model="a/b",
                                        base_url="https://openrouter.ai/api/v1", session_id="s"),
                                  rates=_rates(RateCard(1.0, 2.0))))
    before = summarize(ledger.events(session_id="s"))["spent_usd"]
    # Rates change later (or the session switches models): the old row stands.
    ledger.append(event_from_call(_call(driver="openrouter:a/b", model="a/b",
                                        base_url="https://openrouter.ai/api/v1", session_id="other"),
                                  rates=_rates(RateCard(100.0, 200.0))))
    assert summarize(ledger.events(session_id="s"))["spent_usd"] == before
    assert ledger.events(session_id="s")[0].rates == {"input": 1.0, "output": 2.0}


def test_breakdowns_always_add_up_to_the_headline():
    rng = random.Random(7)
    routes = [
        ("openrouter:z-ai/glm-5.3", "z-ai/glm-5.3", "https://openrouter.ai/api/v1"),
        ("opencode-go:mimo-v2.6-pro", "mimo-v2.6-pro", "https://opencode.ai/zen/go/v1"),
        ("anthropic:claude-sonnet-5", "claude-sonnet-5", "https://api.anthropic.com"),
        ("local:x/y", "y", "http://127.0.0.1:1/v1"),
    ]
    events = []
    for _ in range(300):
        d, m, u = rng.choice(routes)
        prompt = rng.randint(1, 400_000)
        events.append(event_from_call(
            _call(driver=d, model=m, base_url=u, tokens_in=prompt, tokens_out=rng.randint(0, 5000),
                  cache_read=rng.randint(0, prompt), cost=rng.choice([None, rng.random() / 10]),
                  purpose=rng.choice(["pilot", "compaction", "vision"]), session_id="s"),
            rates=_rates(rng.choice([MIMO, SONNET, None]))))
    s = summarize(events)
    assert sum(r["calls"] for r in s["by_route"]) == s["calls"] == sum(p["calls"] for p in s["by_purpose"].values())
    assert sum(r["cash_usd"] for r in s["by_route"]) == pytest.approx(s["spent_usd"])
    assert sum(r["tokens"] for r in s["by_route"]) == s["tokens"]
    assert s["plan"]["cash_usd"] + s["local"]["cash_usd"] == pytest.approx(
        sum(e.cash_usd for e in events if e.billing != "metered" and e.cash_usd is not None))


def test_a_metered_driver_lands_in_the_ledger_with_attribution(tmp_path):
    ledger = UsageLedger(str(tmp_path / "l.sqlite"))
    uninstall = recorder.install(ledger)

    @metered
    class OpenRouterish:
        name = "openrouter:z-ai/glm-5.3"
        model = "z-ai/glm-5.3"
        base_url = "https://openrouter.ai/api/v1"

        def chat(self, messages, **kw):
            return DriverResponse(text="x", tokens_in=2_000, tokens_out=100,
                                  meta={"cache_read_tokens": 1_500, "provider_cost_usd": 0.0007})

    try:
        with attribution(session_id="sess-1", purpose="pilot", turn=2):
            OpenRouterish().chat([])
    finally:
        uninstall()
    [row] = ledger.events(session_id="sess-1")
    assert (row.provider, row.billing, row.purpose, row.turn) == ("openrouter", "metered", "pilot", 2)
    assert (row.input_uncached, row.cache_read, row.output) == (500, 1_500, 100)
    assert (row.cost_basis, row.cash_usd) == ("reported", 0.0007)


def test_a_sessions_pilot_is_attributed_on_any_thread(tmp_path):
    import threading

    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    @metered
    class Pilot:
        name = "openrouter:a/b"
        model = "a/b"
        base_url = "https://openrouter.ai/api/v1"

        def complete(self, prompt, **kw):
            return DriverResponse(text="x", tokens_in=10, tokens_out=1, meta={"provider_cost_usd": 0.001})

    ledger = UsageLedger(str(tmp_path / "l.sqlite"))
    uninstall = recorder.install(ledger)
    try:
        s = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path / "st")))
        s.pilot = Pilot()
        s.harness_session_id = "bound-later"  # the session id is bound after the pilot
        worker = threading.Thread(target=s.pilot.complete, args=("p",))  # no context copy
        worker.start()
        worker.join()
        with attribution(purpose="compaction"):
            s.pilot.complete("p")
    finally:
        uninstall()
    rows = ledger.events(session_id="bound-later")
    assert [r.purpose for r in rows] == ["pilot", "compaction"]


def test_swarm_workers_join_the_session_view():
    from harness.usage_ledger.swarm import swarm_events

    report = {"actual_cost": {"tasks": [
        {"task_id": "t1", "model_id": "cursor/grok-4-6", "billing": "plan", "tokens_in": 5_000,
         "tokens_out": 500, "marginal_cost_usd": 0.0, "priced": True, "api_equivalent_cost_usd": 0.02},
        {"task_id": "t2", "model_id": "agentic/moonshotai/kimi-k3", "billing": "api", "tokens_in": 8_000,
         "tokens_out": 900, "cache_read_tokens": 6_000, "marginal_cost_usd": 0.011, "priced": True,
         "api_equivalent_cost_usd": 0.011},
        {"task_id": "t3", "model_id": "agentic/new/model", "billing": "api", "tokens_in": 100,
         "tokens_out": 10, "marginal_cost_usd": 0.0, "priced": False},
    ]}}
    rows = list(swarm_events("s", [("job-1", report)]))
    assert [(r.billing, r.cost_basis, r.cash_usd) for r in rows] == [
        ("plan", "included", 0.0), ("metered", "computed", 0.011), ("metered", "unpriced", None)]
    assert rows[1].cache_read == 6_000 and rows[1].input_uncached == 2_000
    s = summarize(rows)
    assert s["spent_usd"] == pytest.approx(0.011) and s["unpriced_calls"] == 1
    assert s["plan"]["list_usd"] == pytest.approx(0.02)
    assert s["by_job"]["job-1"]["calls"] == 3


def test_reconciliation_reports_unrecorded_spend_and_never_blocks(tmp_path):
    from harness.usage_ledger import reconcile
    from harness.usage_ledger.store import ledger_for

    state = str(tmp_path)
    ledger_for(state).append(event_from_call(
        _call(driver="openrouter:a/b", model="a/b", base_url="https://openrouter.ai/api/v1", cost=0.40),
        rates=_rates(None)))
    now = 2.5  # the fake call above was recorded at t=2.0, inside today's window
    reconcile._cache.clear()
    import threading
    import time as _time

    release = threading.Event()

    def slow_fetch(key):
        release.wait(5)
        return {"day_usd": 1.00, "month_usd": 3.00}

    started = _time.monotonic()
    assert reconcile.openrouter_reconciliation(state, key="k", fetch=slow_fetch, now=now) is None
    assert _time.monotonic() - started < 1.0  # returned without waiting on the provider
    release.set()
    for _ in range(100):
        got = reconcile.openrouter_reconciliation(state, key="k", fetch=slow_fetch, now=now)
        if got:
            break
        _time.sleep(0.02)
    assert got["day"] == {"ledger_usd": 0.4, "account_usd": 1.0, "unrecorded_usd": 0.6}
    assert got["month"]["unrecorded_usd"] == pytest.approx(2.6)
