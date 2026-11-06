import pytest

from harness.text_clean import clean_say

_NORMAL = "I have checked the rule store and it looks correct. Let me run the tests now."
_SHORT_LS = (
    "A few files:\n"
    "AGENTS.md\n"
    "pyproject.toml\n"
    "And we are done."
)
# The lines are <= 80 chars with no common words, but they are inside backticks, so they stay.
_BACKTICKS = (
    "Look at this code:\n"
    "```python\n"
    "import os\n"
    "import sys\n"
    "import time\n"
    "import json\n"
    "import re\n"
    "import math\n"
    "```\n"
    "It is neat."
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(_NORMAL, _NORMAL, id="normal"),
        pytest.param(
            "I will execute python to check the database.\n"
            "USER: (run_command 'python -c ...' completed with exit code 1)\n"
            "Let me try another approach.",
            "I will execute python to check the database.\n"
            "Let me try another approach.",
            id="tool_echo",
        ),
        pytest.param(
            "I encountered an error during import:\n"
            "Traceback (most recent call last):\n"
            "  File \"harness/server.py\", line 12, in <module>\n"
            "    import missing_module\n"
            "ModuleNotFoundError: No module named 'missing_module'\n"
            "I will install the package now.",
            "I encountered an error during import:\n"
            "I will install the package now.",
            id="traceback",
        ),
        pytest.param(
            "I encountered a chained error:\n"
            "Traceback (most recent call last):\n"
            "  File \"x.py\", line 1, in <module>\n"
            "    raise ValueError\n"
            "ValueError\n"
            "\n"
            "During handling of the above exception, another exception occurred:\n"
            "\n"
            "Traceback (most recent call last):\n"
            "  File \"x.py\", line 3, in <module>\n"
            "    raise TypeError\n"
            "TypeError\n"
            "I will resolve this.",
            "I encountered a chained error:\n"
            "I will resolve this.",
            id="chained_traceback",
        ),
        pytest.param(
            "Here are the files in the folder:\n"
            "AGENTS.md\n"
            "harness/conversation.py\n"
            "harness/pilot.py\n"
            "pyproject.toml\n"
            "requirements.txt\n"
            "tests/test_rules.py\n"
            "That's all of them.",
            "Here are the files in the folder:\n"
            "(output collapsed)\n"
            "That's all of them.",
            id="long_ls",
        ),
        # Only 2 files, so no collapse.
        pytest.param(_SHORT_LS, _SHORT_LS, id="short_ls"),
        pytest.param(_BACKTICKS, _BACKTICKS, id="backticks"),
        # Text that is only pollution is empty after strip (never "Working...").
        pytest.param(
            "USER: (run_command 'x' completed with exit code 1)", "", id="only_tool_echo",
        ),
        pytest.param(
            "Traceback (most recent call last):\n"
            "  File \"harness/server.py\", line 12\n"
            "IndexError: list index out of range",
            "",
            id="only_traceback",
        ),
        pytest.param(
            "Line 1\n\n\n\nLine 2\n\n\nLine 3", "Line 1\n\nLine 2\n\nLine 3", id="newlines",
        ),
        # The old empty fallback was stored as transcript text. It is not spoken prose.
        pytest.param("Working...", "", id="working3"),
        pytest.param("Working..", "", id="working2"),
        pytest.param("working...", "", id="working_lower"),
    ],
)
def test_clean_say(text, expected):
    assert clean_say(text) == expected
