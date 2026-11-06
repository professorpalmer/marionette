"""WikiDistillMixin grounding budget."""

from harness.wiki_distill import WikiDistillMixin


def test_wiki_grounding_budget_profile_aware():
    from harness.task_profile import DEEP, MICRO, STANDARD

    class _Stub(WikiDistillMixin):
        pass

    stub = _Stub()
    stub._task_profile = STANDARD
    assert stub._wiki_grounding_budget() == (3, 2500)
    stub._task_profile = DEEP
    assert stub._wiki_grounding_budget() == (5, 8000)
    stub._task_profile = MICRO
    assert stub._wiki_grounding_budget() == (5, 8000)
    stub._task_profile = "unknown"
    assert stub._wiki_grounding_budget() == (5, 8000)
    stub._task_profile = ""
    assert stub._wiki_grounding_budget() == (5, 8000)


