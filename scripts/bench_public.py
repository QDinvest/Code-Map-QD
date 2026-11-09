"""Public benchmark. Clone first (commits used for the README numbers: flask 49943b5, httpx b5addb6):
  git clone https://github.com/pallets/flask && git clone https://github.com/encode/httpx
  python scripts/bench_public.py   (run from the folder that holds both clones)
Tokens to reach the answer: codemap (ask + read the hit's line range) vs grep baseline (grep output + full Read of the right file).
Baseline is conservative: assumes the agent's first grep keyword is good and it reads only the one right file."""
import os, re, subprocess, sys
CM = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'codemap.py')
Q = [('flask', 'where is the session cookie signed', 'session', 'src/flask/sessions.py'),
     ('flask', 'how does flask load config from environment variables', 'envvar', 'src/flask/config.py'),
     ('flask', 'where does jsonify serialize responses', 'jsonify', 'src/flask/json/__init__.py'),
     ('flask', 'where is the cli command that runs the dev server', 'run_command', 'src/flask/cli.py'),
     ('flask', 'where are blueprints registered on the app', 'register_blueprint', 'src/flask/sansio/blueprints.py'),
     ('httpx', 'where are redirects followed', 'redirect', 'httpx/_client.py'),
     ('httpx', 'how are timeouts configured', 'Timeout', 'httpx/_config.py'),
     ('httpx', 'where is basic auth implemented', 'BasicAuth', 'httpx/_auth.py'),
     ('httpx', 'where are proxies selected from environment', 'proxies', 'httpx/_utils.py'),
     ('httpx', 'where is the retry on connection failure in transports', 'retries', 'httpx/_transports/default.py')]
tok = lambda s: len(s) // 4
tc = tg = 0
for repo, q, kw, ans in Q:
    out = subprocess.run([sys.executable, CM, '--root', repo, 'ask', q], capture_output=True, text=True).stdout
    m = re.search(r'^\s+(\S+):(\d+)-(\d+)', out, re.M)
    lines = open(f'{repo}/{m.group(1)}', encoding='utf-8').read().splitlines()
    c = tok(out) + tok('\n'.join(lines[int(m.group(2)) - 1:int(m.group(3))]))
    g_out = '\n'.join(subprocess.run(['git', '-C', repo, 'grep', '-n', kw], capture_output=True, text=True, encoding='utf-8').stdout.splitlines()[:250])
    g = tok(g_out) + tok('\n'.join(open(f'{repo}/{ans}', encoding='utf-8').read().splitlines()[:2000]))
    tc += c; tg += g
    print(f'{repo:6} codemap={c:6,} grep={g:6,} saved={1 - c / g:4.0%}  {q}')
print(f'TOTAL  codemap={tc:,} grep={tg:,} saved={1 - tc / tg:.0%}')
