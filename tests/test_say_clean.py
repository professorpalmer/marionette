from harness.text_clean import clean_say

def test_clean_say_normal_untouched():
    normal_text = "I have checked the rule store and it looks correct. Let me run the tests now."
    assert clean_say(normal_text) == normal_text

def test_clean_say_strips_tool_echo():
    polluted = (
        "I will execute python to check the database.\n"
        "USER: (run_command 'python -c ...' completed with exit code 1)\n"
        "Let me try another approach."
    )
    expected = (
        "I will execute python to check the database.\n"
        "Let me try another approach."
    )
    assert clean_say(polluted) == expected

def test_clean_say_strips_traceback():
    polluted = (
        "I encountered an error during import:\n"
        "Traceback (most recent call last):\n"
        "  File \"harness/server.py\", line 12, in <module>\n"
        "    import missing_module\n"
        "ModuleNotFoundError: No module named 'missing_module'\n"
        "I will install the package now."
    )
    expected = (
        "I encountered an error during import:\n"
        "I will install the package now."
    )
    assert clean_say(polluted) == expected

def test_clean_say_strips_chained_traceback():
    polluted = (
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
        "I will resolve this."
    )
    expected = (
        "I encountered a chained error:\n"
        "I will resolve this."
    )
    assert clean_say(polluted) == expected

def test_clean_say_collapses_long_ls():
    polluted = (
        "Here are the files in the folder:\n"
        "AGENTS.md\n"
        "harness/conversation.py\n"
        "harness/pilot.py\n"
        "pyproject.toml\n"
        "requirements.txt\n"
        "tests/test_rules.py\n"
        "That's all of them."
    )
    expected = (
        "Here are the files in the folder:\n"
        "(output collapsed)\n"
        "That's all of them."
    )
    assert clean_say(polluted) == expected

def test_clean_say_no_collapse_short_ls():
    polluted = (
        "A few files:\n"
        "AGENTS.md\n"
        "pyproject.toml\n"
        "And we are done."
    )
    # Since it's only 2 files, it shouldn't collapse them
    assert clean_say(polluted) == polluted

def test_clean_say_keeps_backticks_intact():
    polluted = (
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
    # The lines are <= 80 chars, no common words, but they are inside backticks so they shouldn't be collapsed.
    assert clean_say(polluted) == polluted

def test_clean_say_fallback():
    # If the text has only pollution
    polluted = "USER: (run_command 'x' completed with exit code 1)"
    assert clean_say(polluted) == "Working..."
    
    polluted_with_traceback = (
        "Traceback (most recent call last):\n"
        "  File \"harness/server.py\", line 12\n"
        "IndexError: list index out of range"
    )
    assert clean_say(polluted_with_traceback) == "Working..."

def test_clean_say_collapses_newlines():
    text = "Line 1\n\n\n\nLine 2\n\n\nLine 3"
    expected = "Line 1\n\nLine 2\n\nLine 3"
    assert clean_say(text) == expected
