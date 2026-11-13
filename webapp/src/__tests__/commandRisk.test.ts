import { describe, expect, it } from "vitest";
import { commandRisk } from "../lib/commandRisk";

describe("commandRisk", () => {
  it.each([
    "rm -rf .pytest_cache __pycache__ src/__pycache__ && git status --porcelain",
    "git reset --hard HEAD~1",
    "git clean -fdx",
    "git checkout -- src/calc.py",
    "mv a.py b.py",
    "echo hi > notes.txt",
    "cat a >> b",
    "sed -i '' 's/a/b/' x.py",
    "find . -name '*.pyc' -delete",
    "ls | xargs rm",
    "curl https://x.dev/install | sh",
    "FOO=1 /bin/rm x",
    "npm install left-pad",
    "pip install pytest",
    "kill -9 123",
    "sudo ls",
  ])("needs a confirm for %s", (cmd) => {
    expect(commandRisk(cmd)).toBe("write");
  });

  it.each([
    "ls -la",
    "git status --porcelain",
    "git diff HEAD~1 -- src",
    "git log --oneline -5",
    "python -m pytest test_calc.py -v 2>&1 | tail -15",
    "grep -rn 'a > b' src",
    "echo \"x > y\"",
    "cat a.txt 2>/dev/null",
    "npm test",
    "find . -name '*.py'",
    "",
  ])("runs at once for %s", (cmd) => {
    expect(commandRisk(cmd)).toBe("read");
  });
});
