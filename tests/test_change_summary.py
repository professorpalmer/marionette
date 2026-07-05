"""Tests for harness.change_summary: delta summaries over full re-dumps."""

from harness.change_summary import (
    Region,
    line_diff_regions,
    summarize_change,
)


def _lines(n, prefix="line"):
    return "\n".join("{0}{1}".format(prefix, i) for i in range(n))


def test_no_change_identical():
    text = "a\nb\nc"
    assert summarize_change(text, text) == "no change"
    assert line_diff_regions(text, text) == []


def test_no_change_empty_both():
    assert summarize_change("", "") == "no change"


def test_new_file():
    new = "a\nb\nc"
    assert summarize_change("", new) == "new file, 3 lines"
    assert summarize_change("", "solo") == "new file, 1 line"


def test_deleted():
    old = "a\nb\nc\nd"
    assert summarize_change(old, "") == "deleted, 4 lines"
    assert summarize_change("solo", "") == "deleted, 1 line"


def test_added_only():
    old = "a\nb\nc"
    new = "a\nb\nx\ny\nc"
    summary = summarize_change(old, new)
    assert "region(s)" not in summary  # plural handling produces "regions"
    assert "changed across 1 region" in summary
    # Two lines inserted, none removed.
    assert "(+2/-0)" in summary
    regions = line_diff_regions(old, new)
    assert len(regions) == 1
    assert regions[0].added == 2
    assert regions[0].removed == 0


def test_removed_only():
    old = "a\nb\nc\nd\ne"
    new = "a\ne"
    summary = summarize_change(old, new)
    assert "changed across 1 region" in summary
    assert "(+0/-3)" in summary
    regions = line_diff_regions(old, new)
    assert len(regions) == 1
    assert regions[0].added == 0
    assert regions[0].removed == 3


def test_mixed_regions():
    old = "a\nb\nc\nd\ne\nf\ng\nh"
    new = "a\nB\nc\nd\ne\nf\ng\nX\nY"
    summary = summarize_change(old, new)
    regions = line_diff_regions(old, new)
    assert len(regions) == 2
    assert "changed across 2 regions" in summary
    # First region is a replace of line 2, second changes the tail.
    assert regions[0].added >= 1 and regions[0].removed >= 1


def test_region_capping():
    # Build many separated changed regions.
    old_lines = []
    new_lines = []
    for i in range(50):
        old_lines.append("keep{0}".format(i))
        old_lines.append("old{0}".format(i))
        new_lines.append("keep{0}".format(i))
        new_lines.append("new{0}".format(i))
    old = "\n".join(old_lines)
    new = "\n".join(new_lines)

    regions = line_diff_regions(old, new)
    assert len(regions) == 50

    summary = summarize_change(old, new, max_regions=5)
    assert "(+45 more regions)" in summary
    # Only 5 region tuples should be spelled out before the suffix.
    assert summary.count("(+1/-1)") == 5
    assert "changed across 50 regions" in summary


def test_capping_singular_suffix():
    old_lines = []
    new_lines = []
    for i in range(3):
        old_lines.append("keep{0}".format(i))
        old_lines.append("old{0}".format(i))
        new_lines.append("keep{0}".format(i))
        new_lines.append("new{0}".format(i))
    old = "\n".join(old_lines)
    new = "\n".join(new_lines)
    summary = summarize_change(old, new, max_regions=2)
    assert "(+1 more region)" in summary


def test_max_regions_floor():
    old = "a\nb\nc\nd"
    new = "A\nb\nC\nd"
    # max_regions below 1 is clamped to 1.
    summary = summarize_change(old, new, max_regions=0)
    assert "more region" in summary


def test_summary_never_dumps_full_contents():
    # A large file with a single small change must produce a bounded summary
    # that is far smaller than the input and contains no file content lines.
    old = _lines(5000, prefix="content_")
    new = old + "\nappended_final_line"
    summary = summarize_change(new_old_helper := old, new)

    assert len(summary) < 200
    assert len(summary) < len(new)
    # None of the bulk content lines leak into the summary.
    assert "content_2500" not in summary
    assert "appended_final_line" not in summary
    assert "new file" not in summary


def test_summary_bounded_for_many_regions():
    old = _lines(2000, prefix="x")
    # Change every other line -> many regions, but capped output.
    new_lines = []
    for i in range(2000):
        if i % 2 == 0:
            new_lines.append("changed{0}".format(i))
        else:
            new_lines.append("x{0}".format(i))
    new = "\n".join(new_lines)
    summary = summarize_change(old, new, max_regions=10)
    assert "more regions" in summary
    # Bounded regardless of thousands of changed lines.
    assert len(summary) < 400
    assert len(summary) < len(new)


def test_region_dataclass_fields():
    r = Region(start_line=1, end_line=3, added=2, removed=1)
    assert (r.start_line, r.end_line, r.added, r.removed) == (1, 3, 2, 1)


def test_context_param_accepted():
    old = "a\nb\nc"
    new = "a\nB\nc"
    # context is accepted and does not alter the compact summary.
    assert summarize_change(old, new, context=3) == summarize_change(old, new)
