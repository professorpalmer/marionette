# Design: AST preview for hash edits

## Problem

A hash edit's confirmation payload shows a text-level result ("applied 2
ops") but nothing structural. For Python files, a cheap stdlib `ast` pass can
say *what changed shape-wise* — functions/classes added, removed, or with a
changed signature — which is a stronger reviewability signal than character
counts, and catches "the edit accidentally deleted a function" immediately.

## Shape

- New module `harness/ast_preview.py` (stdlib only, uses `ast`).
- `structural_diff(before: str, after: str) -> dict`:

  ```json
  {"available": true,
   "added": ["helper_b"], "removed": [],
   "changed": ["Class.method_a"]}
  ```

  - Symbols are dotted paths (`Class.method`) for nested defs; top-level
    functions and classes are bare names.
  - "changed" means same symbol present in both versions with a different
    signature (args/defaults/decorators ignored beyond the arg spec — the
    comparison is `ast.unparse`-free and 3.9-safe: it compares the tuple of
    argument names + counts of defaults).
  - Unparseable input on either side returns `{"available": false}` — never
    raises.
- Wiring: in `_do_hash_edit` (`harness/tool_dispatch.py`), when
  `HARNESS_AST_PREVIEW=1`, the target is a `.py` file, and the write pass
  succeeds, compute the diff between the original and new text and stash it;
  the hash_edit branch in `harness/conversation.py` merges it into the
  `action_result` event data as `ast_preview`.

## Integration map

| Piece | Location |
| --- | --- |
| Diff module | `harness/ast_preview.py` (new) |
| Compute site | `harness/tool_dispatch.py` `_do_hash_edit` write path |
| Event surfacing | `harness/conversation.py` hash_edit success branch |
| Tests | `tests/test_ast_preview.py` (new) |

## Kill switch

`HARNESS_AST_PREVIEW=1` enables (default OFF).

## Non-goals (v1)

- Non-Python languages.
- Body-level semantic diffing (only add/remove/signature-change).
- Blocking an edit on a structural finding.

## Acceptance

- Added/removed/renamed function detected (rename = one removed + one added).
- Signature change detected as "changed".
- Syntax-error input returns `{"available": false}`; disabled by default.
