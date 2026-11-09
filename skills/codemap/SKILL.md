---
name: codemap
description: Find where something lives in a git repo in one call before grepping or reading files. Use whenever you need to locate the file, function or line for a task ("where is X", "where does Y happen", "what calls Z", "what breaks if I change W"), including inside subagents doing code search.
---

# codemap

Run from inside the repo (or pass `--root <repo>`). Use `python3` if `python` is not found.

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/codemap.py" ask "<task or question in plain words>"
python "${CLAUDE_PLUGIN_ROOT}/scripts/codemap.py" impact <file|symbol>   # importers depth 2 + tests to run
python "${CLAUDE_PLUGIN_ROOT}/scripts/codemap.py" file <path>            # outline instead of reading a big file
```

Output: `EDIT` (file:line-range, best first), `CALLERS`, `TESTS`, `RUN`, then a confidence line.

- `confidence: high`: trust the first EDIT entry and read only that range.
- `confidence: medium`: the answer is almost always in the EDIT list. Read those ranges in order before grepping.
- `confidence: low`: grep with the words it prints.
- Put exact strings in quotes or write routes as-is (`/api/x/y`). They are searched verbatim and every file containing them is listed.
- Paths are relative to the repo root. The first call in a repo builds the index (seconds), later calls take well under a second.
