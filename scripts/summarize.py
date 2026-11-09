"""One-time (then incremental) meaning layer for codemap: a cheap model describes every indexed file in plain words.

usage: python summarize.py [--root <repo>] [--workers 16]
Writes <repo>/.codemap/summaries.json  {path: {"h": content-hash, "s": summary}}. Only new/changed files are sent.
codemap.py indexes these summaries so a question in the user's words ("sign in", "draw the button") finds code
that uses different words ("authenticate", "renderSubmit").
"""
import argparse, concurrent.futures as cf, hashlib, json, os, sys, threading
import anthropic

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import codemap  # reuse its file list and secret filter

MODEL = 'claude-haiku-5-5'
MAX_CHARS = 400_000  # ~100k tokens: whole file for any real source file
SYSTEM = """You index a codebase for a search tool used by coding agents.
Given one file, write a plain-text description that helps someone find this file from a question.
Format, no markdown, under 120 words:
Line 1: what the file does, in one sentence.
Line 2: "Also called:" the domain words a developer might use for these concepts, including synonyms and the code's own terms (e.g. login / authenticate, remove / purge).
Line 3: "Answers:" 3 to 5 short questions a developer would ask that this file answers (where is X, how does Y decide Z, what limit does W use).
Name the specific things the file does or refuses; skip generic words like utility, helper, module."""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default=os.getcwd())
    ap.add_argument('--workers', type=int, default=16)
    ap.add_argument('--only', nargs='*', help='summarize just these paths and print them (prompt check)')
    ap.add_argument('--auto', action='store_true', help='started by codemap in the background: capped, removes its lock')
    ap.add_argument('--max-files', type=int, default=5000, help='refuse bigger runs in --auto mode (cost guard)')
    a = ap.parse_args()
    root = codemap.find_root(a.root)
    out_path = os.path.join(root, '.codemap', 'summaries.json')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    done = json.load(open(out_path, encoding='utf-8')) if os.path.exists(out_path) else {}
    # git worktrees of one repo share summaries: a file with the same content hash is never paid for twice
    seed = {}
    common = codemap.git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir').strip()
    main_sum = os.path.join(os.path.dirname(common), '.codemap', 'summaries.json') if common else ''
    if main_sum and os.path.normcase(os.path.abspath(main_sum)) != os.path.normcase(os.path.abspath(out_path)) and os.path.exists(main_sum):
        try:
            seed = json.load(open(main_sum, encoding='utf-8'))
        except (OSError, ValueError):
            pass
    reused = 0
    todo, files = [], set(codemap.candidates(root))
    for p in sorted(files):
        try:
            text = open(os.path.join(root, p), encoding='utf-8', errors='replace').read()
        except OSError:
            continue
        h = hashlib.sha1(text.encode('utf-8', 'replace')).hexdigest()[:16]
        if done.get(p, {}).get('h') != h and text.strip():
            if seed.get(p, {}).get('h') == h:
                done[p] = seed[p]; reused += 1
            else:
                todo.append((p, h, text))
    if a.only:
        todo = [t for t in todo if t[0] in a.only]
    done = {p: v for p, v in done.items() if p in files}  # drop deleted files
    print(f'{len(todo)} files to summarize ({len(done)} cached, {reused} reused from the main checkout)')
    if reused:
        json.dump(done, open(out_path, 'w', encoding='utf-8'))
    if a.auto and len(todo) > a.max_files:  # ~$0.0008 a file: a 5,000-file run is about $4
        print(f'refusing: {len(todo)} > --max-files {a.max_files}; run summarize.py by hand to go ahead')
        return
    if not todo:
        return  # leave summaries.json untouched: its timestamp is what tells codemap to reload
    client = anthropic.Anthropic(max_retries=8)
    lock, usage = threading.Lock(), [0, 0]

    def one(item):
        p, h, text = item
        if len(text) > MAX_CHARS:  # say so instead of silently cutting: the model sees the gap
            text = text[:MAX_CHARS] + f'\n\n[... file continues, {len(text) - MAX_CHARS} more characters not shown]'
        msg = client.messages.create(
            model=MODEL, max_tokens=2048, output_config={'effort': 'low'},
            system=[{'type': 'text', 'text': SYSTEM, 'cache_control': {'type': 'ephemeral'}}],
            messages=[{'role': 'user', 'content': f'File: {p}\n\n{text}'}])
        s = next((b.text for b in msg.content if b.type == 'text'), '').strip()
        with lock:
            usage[0] += msg.usage.input_tokens + (msg.usage.cache_read_input_tokens or 0)
            usage[1] += msg.usage.output_tokens
        return p, h, s, msg.stop_reason

    failed = 0
    with cf.ThreadPoolExecutor(a.workers) as ex:
        futs = [ex.submit(one, t) for t in todo]
        for i, f in enumerate(cf.as_completed(futs), 1):
            try:
                p, h, s, stop = f.result()
            except anthropic.APIStatusError as e:  # non-retryable after SDK retries: report, keep going
                failed += 1; print('error', e.status_code, str(e)[:120]); continue
            except anthropic.APIConnectionError as e:
                failed += 1; print('connection error', str(e)[:120]); continue
            if stop == 'refusal' or not s:
                failed += 1; continue
            done[p] = {'h': h, 's': s}
            if a.only:
                print(f'--- {p}\n{s}')
            if i % 100 == 0:
                json.dump(done, open(out_path, 'w', encoding='utf-8'))
                print(f'{i}/{len(todo)}')
    json.dump(done, open(out_path, 'w', encoding='utf-8'))
    cost = usage[0] / 1e6 * 0.10 + usage[1] / 1e6 * 0.50
    print(f'done: {len(done)} summaries, {failed} failed, ~{usage[0]:,} in / {usage[1]:,} out tokens, ~${cost:.2f}')


if __name__ == '__main__':
    try:
        main()
    finally:
        if '--auto' in sys.argv:
            try:
                os.remove(os.path.join(codemap.find_root(sys.argv[sys.argv.index('--root') + 1]), '.codemap', 'summarize.lock'))
            except (OSError, ValueError, IndexError):
                pass
