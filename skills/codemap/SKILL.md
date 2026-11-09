---
name: codemap
description: Find where something lives in a git repo in one call before grepping or reading files. Use whenever you need to locate the file, function or line for a task ("where is X", "where does Y happen", "what calls Z", "what breaks if I change W"), including inside subagents doing code search.
---

# codemap

Run from inside the repo (or pass `--root <repo>`). Use `python3` if `python` is not found.

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/codemap.py" ask "<task in plain words> <4-8 words the code likely uses>"
python "${CLAUDE_PLUGIN_ROOT}/scripts/codemap.py" impact <file|symbol>   # importers depth 2 + tests to run
python "${CLAUDE_PLUGIN_ROOT}/scripts/codemap.py" file <path>            # outline instead of reading a big file
```

- Add 4-8 words the code probably uses: synonyms, identifier guesses, terms of art. Questions and code often use different words; this closes the gap (right function listed 91% with them, 75% without, on 110 test questions).
  Example: `ask "where are passwords hidden in logs mask redact obfuscate sensitive secret"`.
- Output: the best files, one per line, each with its best functions as `name start-end`. Pick the function whose name fits and read only those lines.
- If no listed function fits but a file looks right, run `file <path>` and read only the function whose name fits. An outline costs a few hundred tokens; the whole file often costs thousands.
- If nothing fits, grep.
- Put exact strings in quotes or write routes as-is (`/api/x/y`). They are searched verbatim and every file containing them is listed.
- Paths are relative to the repo root. The first call in a repo builds the index (seconds), later calls take well under a second.
