"""What does one agent turn cost before it does any work? Runs `claude -p "Reply OK"` (no tools used) under
configurations that strip one layer at a time, and reports prompt tokens for each.
usage: python overhead.py <repo-dir>"""
import json, os, subprocess, sys
repo = sys.argv[1]
claude = 'claude.cmd' if os.name == 'nt' else 'claude'
CONFIGS = [
 ('full (what agents get)', []),
 ('no skills/commands list', ['--disable-slash-commands']),
 ('bare (no hooks, plugins, CLAUDE.md, skills)', ['--bare']),
]


def prompt_tokens(extra):
    p = subprocess.run([claude, '-p', 'Reply with the single word OK.', '--model', 'sonnet', '--output-format', 'json', *extra],
                       cwd=repo, capture_output=True, text=True, encoding='utf-8')
    try:
        r = json.loads(p.stdout)
    except ValueError:
        return None, p.stderr[:200]
    u = r.get('usage', {})
    return sum(u.get(k) or 0 for k in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')), r.get('total_cost_usd')


base = {}
for name, extra in CONFIGS:
    tok, cost = prompt_tokens(extra)
    base[name] = tok
    print(f'{name:45} {tok if tok is not None else "error":>8} tokens  {cost}')
