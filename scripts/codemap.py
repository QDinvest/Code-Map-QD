#!/usr/bin/env python3
"""codemap: task -> where to edit, callers, tests, run command. Stdlib only."""
import argparse, ast, json, math, os, re, sqlite3, subprocess, sys, time

CODE = {'.ts': 'ts', '.tsx': 'ts', '.js': 'ts', '.jsx': 'ts', '.mjs': 'ts', '.cjs': 'ts',
        '.sol': 'sol', '.py': 'py',
        # no symbols: found by path, header and text chunks (deploy scripts, migrations, agent/skill files)
        '.sql': 'txt', '.ps1': 'txt', '.sh': 'txt', '.md': 'txt', '.yml': 'txt', '.yaml': 'txt', '.toml': 'txt'}
SECRET = re.compile(r'(^|/)(secrets?|\.env[^/]*|[^/]*\.(pem|key|p12|pfx)|id_rsa[^/]*|node_modules)(/|$)', re.I)
OLD = re.compile(r'(^|/)(legacy|old|deprecated|archived?|_old|backup)(/|$)', re.I)
TEST = re.compile(r'(\.test\.|\.spec\.|\.t\.sol$|(^|/)(__tests__|tests?|e2e)/)')
STOP = set('a an the of to in for and or how where what is are do does fix add make change update find code file '
           'function when with from this that my i it be on at by we use used using should not please can want need'.split())
EXTS = ['.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.sol', '.py']
SCHEMA = """
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime REAL, size INT, lang TEXT);
CREATE TABLE IF NOT EXISTS syms(id INTEGER PRIMARY KEY, file TEXT, name TEXT, kind TEXT, s INT, e INT, sig TEXT);
CREATE TABLE IF NOT EXISTS imps(file TEXT, spec TEXT);
CREATE TABLE IF NOT EXISTS edges(src TEXT, dst TEXT);
CREATE INDEX IF NOT EXISTS i_syms ON syms(file); CREATE INDEX IF NOT EXISTS i_symn ON syms(name);
CREATE INDEX IF NOT EXISTS i_edst ON edges(dst); CREATE INDEX IF NOT EXISTS i_esrc ON edges(src);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS sfts USING fts5(file UNINDEXED, mw, tokenize='porter unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(file UNINDEXED, symid UNINDEXED, sw, dw, pw, bw, tokenize='porter unicode61');
"""


def words(s):
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', s)
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', s)
    return [w for w in re.split(r'[^A-Za-z0-9]+', s.lower()) if w]


def git(root, *a):
    return subprocess.run(['git', '-C', root, *a], capture_output=True, text=True, encoding='utf-8').stdout


def find_root(start):
    r = git(start, 'rev-parse', '--show-toplevel').strip()
    return r or start


# ---------- parsing ----------
TS_PATS = [
    (re.compile(r'^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\*?\s+(\w+)'), 'fn'),
    (re.compile(r'^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)'), 'class'),
    (re.compile(r'^\s*(?:export\s+)?(?:declare\s+)?(interface|type|enum)\s+(\w+)'), 'type'),
    (re.compile(r'^\s*(?:export\s+)?const\s+(\w+)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*(?::[^=]+)?=>'), 'fn'),
    (re.compile(r'^\s*export\s+const\s+(\w+)'), 'const'),
    (re.compile(r'^(?:const|let)\s+([A-Z][A-Z0-9_]+)\b'), 'const'),  # top-level UPPER_CASE constant
    # express/CommonJS style: req.header = function header() / Foo.prototype.bar = async () =>
    (re.compile(r'^\s*(?:[\w$]+\.)+([\w$]+)\s*=\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*=>|[\w$]+\s*=>)'), 'fn'),
    (re.compile(r'''^\s*(?:Object\.)?define\w*\(\s*[\w.$]+\s*,\s*['"]([\w$]+)['"]'''), 'getter'),  # defineGetter(req, 'xhr', ...)
    (re.compile(r'^\s+(?:public\s+|private\s+|protected\s+|static\s+|async\s+|readonly\s+)*(\w+)\s*(?:<[^>]*>)?\([^)]*\)\s*(?::\s*[^{;]+)?\{'), 'method'),
]
SOL_PAT = re.compile(r'^\s*(contract|interface|library|abstract contract|function|event|modifier|struct|enum|error)\s+(\w+)')
NOTMETH = {'if', 'for', 'while', 'switch', 'catch', 'return', 'function', 'else', 'await', 'super', 'import'}
TS_IMP = re.compile(r'''(?:from|import|require\()\s*\(?\s*['"]([^'"]+)['"]''')
STR = re.compile(r'''(["'`])(?:\\.|(?!\1).)*\1''')
CONT = ('=', '(', '[', ',', '=>', '?', ':', '&&', '||', '+', '|', '<')
CONT_NEXT = ('|', '.', '?', ':', '&&', '||', '+', '=>')


def brace_end(lines, i):
    depth, paren, seen = 0, 0, False
    for j in range(i, min(len(lines), i + 400)):
        t = STR.sub('', lines[j].split('//')[0])
        depth += t.count('{') - t.count('}')
        paren += t.count('(') + t.count('[') - t.count(')') - t.count(']')
        if '{' in t:
            seen = True
        if seen and depth <= 0 and paren <= 0:  # `opts: { a: T },` inside params is not the body
            return j + 1
        nxt = lines[j + 1].lstrip() if j + 1 < len(lines) else ''
        if not seen and paren <= 0 and not t.rstrip().endswith(CONT) and not nxt.startswith(CONT_NEXT):
            return j + 1  # statement ended: no open bracket, no dangling operator
    return min(len(lines), i + 400)


def doc_above(lines, i):
    out = []
    for j in range(i - 1, max(i - 40, -1), -1):  # whole doc block: the first line usually says what it does
        t = lines[j].strip()
        if not t.startswith(('*', '/*', '//')) and not t.endswith('*/'):
            break
        out.append(t.strip('/* '))
    return ' '.join(reversed(out))[:600]


def header_of(text):
    # long headers close past the window: keep the start
    m = re.search(r'/\*.*?(?:\*/|$)', text[:4000], re.S) or re.match(r'(?:\s*//[^\n]*\n)+', text)
    return m.group(0)[:700] if m else ''


def parse_brace(text, lang):
    lines = text.split('\n')
    syms, imps = [], []
    for i, ln in enumerate(lines):
        if lang == 'sol':
            m = SOL_PAT.match(ln)
            if m:
                syms.append((m.group(2), m.group(1).split()[-1], i + 1, brace_end(lines, i), ln.strip()[:80].rstrip('{ '), doc_above(lines, i)))
            m = re.match(r'''\s*import\s+(?:.*from\s+)?['"]([^'"]+)['"]''', ln)
            if m:
                imps.append(m.group(1))
            continue
        if ln.lstrip().startswith(('//', '*', '/*')):
            continue
        for pat, kind in TS_PATS:
            m = pat.match(ln)
            if m:
                name = m.group(m.lastindex)
                if kind == 'method' and name in NOTMETH:
                    continue
                syms.append((name, kind, i + 1, brace_end(lines, i), ln.strip()[:80].rstrip('{ '), doc_above(lines, i)))
                break
        if 'import' in ln or 'require' in ln or 'from' in ln:
            imps += TS_IMP.findall(ln)
    return syms, imps, header_of(text)


def parse_py(text):
    syms, imps = [], []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return syms, imps, ''
    lines = text.split('\n')
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = 'class' if isinstance(n, ast.ClassDef) else 'fn'
            syms.append((n.name, kind, n.lineno, n.end_lineno or n.lineno, lines[n.lineno - 1].strip()[:100].rstrip(': '), (ast.get_docstring(n) or '')[:300]))
        elif isinstance(n, ast.Import):
            imps += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            imps.append('.' * n.level + (n.module or ''))
    for n in tree.body:  # module constants: USER_AGENT = ..., SUPPORTED_DECODERS = {...}
        tgt = n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, ast.AnnAssign) else []
        for t in tgt:
            if isinstance(t, ast.Name) and not t.id.startswith('__'):
                syms.append((t.id, 'const', n.lineno, n.end_lineno or n.lineno, lines[n.lineno - 1].strip()[:100], ''))
    return syms, imps, ast.get_docstring(tree) or ''


# ---------- index ----------
CHUNK, CHUNK_STEP = 30, 20  # overlapping windows so a phrase on a boundary still lands in one chunk
VERSION = 12  # bump when parsing or schema changes: forces a clean rebuild


def _wipe(path):
    for x in ('', '-wal', '-shm'):
        try:
            os.remove(path + x)
        except OSError:
            pass


def connect(root):
    d = os.path.join(root, '.codemap')
    os.makedirs(d, exist_ok=True)
    gi = os.path.join(d, '.gitignore')
    if not os.path.exists(gi):
        open(gi, 'w').write('*\n')
    path = os.path.join(d, 'index.db')
    for _ in range(3):
        db = None
        try:
            db = sqlite3.connect(path, timeout=10)
            ver = db.execute('PRAGMA user_version').fetchone()[0]
            if ver == VERSION:
                return db  # set up already: open without writing, many agent sessions start at once
            if ver != 0:
                db.close(); _wipe(path); continue
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)
            db.execute(f'PRAGMA user_version={VERSION}')
            return db
        except sqlite3.OperationalError:
            raise  # locked: never delete a live index
        except sqlite3.DatabaseError:
            if db:
                db.close()  # Windows cannot delete an open file
            _wipe(path)  # corrupt: rebuild
    raise RuntimeError('cannot open index')


def candidates(root):
    out = git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split('\0')
    if not any(out):  # not a git repo (needs --root): walk, capped
        skip = {'.git', 'node_modules', 'dist', 'build', 'venv', '.venv', '__pycache__', '.codemap', 'target'}
        for dp, dn, fn in os.walk(root):
            dn[:] = [x for x in dn if x not in skip]
            out += [os.path.relpath(os.path.join(dp, f), root).replace(os.sep, '/') for f in fn]
            if len(out) > 20000:
                break
    return [p for p in out if p and os.path.splitext(p)[1] in CODE and not SECRET.search(p)]


def load_summaries(root, db):
    """summaries.json (written by summarize.py) -> sfts, only when the file changed."""
    path = os.path.join(root, '.codemap', 'summaries.json')
    stamp = str(os.path.getmtime(path)) if os.path.exists(path) else ''
    if (db.execute("SELECT v FROM meta WHERE k='summaries'").fetchone() or [''])[0] == stamp:
        return
    try:
        data = json.load(open(path, encoding='utf-8')) if stamp else {}
    except (OSError, ValueError):
        return  # half-written by a running summarize.py: keep the old copy
    with db:
        db.execute('DELETE FROM sfts')
        db.executemany('INSERT INTO sfts(file,mw) VALUES(?,?)', [(f, ' '.join(words(v['s']))) for f, v in data.items()])
        db.execute("INSERT OR REPLACE INTO meta VALUES('summaries',?)", (stamp,))


VENV_PY = [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.venv', *x)
           for x in (('Scripts', 'python.exe'), ('bin', 'python'))]


def start_summaries(root):
    """Meaning layer, kept fresh in the background: summarize.py sends only new/changed files to a cheap model.
    Needs the codemap venv (anthropic SDK) and an API key; opt out with CODEMAP_NO_SUMMARIES=1."""
    py = next((x for x in VENV_PY if os.path.exists(x)), None)
    if not py or os.environ.get('CODEMAP_NO_SUMMARIES') or not os.environ.get('ANTHROPIC_API_KEY'):
        return
    lock = os.path.join(root, '.codemap', 'summarize.lock')
    try:
        if time.time() - os.path.getmtime(lock) < 1800:
            return  # one run at a time; a crashed run's lock expires after 30 min
    except OSError:
        pass
    open(lock, 'w').write(str(os.getpid()))
    log = open(os.path.join(root, '.codemap', 'summarize.log'), 'a')
    flags = 0x00000008 | 0x00000200 if os.name == 'nt' else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([py, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'summarize.py'), '--root', root, '--auto'],
                     stdout=log, stderr=log, stdin=subprocess.DEVNULL, creationflags=flags, start_new_session=os.name != 'nt')


def refresh(root, db):
    try:
        load_summaries(root, db)
    except sqlite3.OperationalError:
        pass  # another agent is writing
    cur = {r[0]: (r[1], r[2]) for r in db.execute('SELECT path,mtime,size FROM files')}
    paths = candidates(root)
    stats, changed = {}, []
    for p in paths:
        try:
            st = os.stat(os.path.join(root, p))
        except OSError:
            continue
        stats[p] = (st.st_mtime, st.st_size)
        if cur.get(p) != stats[p]:
            changed.append(p)
    gone = [p for p in cur if p not in stats]
    if changed or gone or not os.path.exists(os.path.join(root, '.codemap', 'summaries.json')):
        start_summaries(root)
    if not changed and not gone:
        return
    try:
        with db:
            for p in gone + changed:
                if p in cur:
                    db.execute('DELETE FROM fts WHERE rowid IN (SELECT id FROM syms WHERE file=?) OR rowid=-(SELECT rowid FROM files WHERE path=?)', (p, p))
                    db.execute('DELETE FROM syms WHERE file=?', (p,))
                    db.execute('DELETE FROM files WHERE path=?', (p,))
                    db.execute('DELETE FROM imps WHERE file=?', (p,))
            for p in changed:
                lang = CODE[os.path.splitext(p)[1]]
                try:
                    text = open(os.path.join(root, p), encoding='utf-8', errors='replace').read()
                except OSError:
                    continue
                if lang == 'txt':
                    syms, imps, head = [], [], text[:700]
                else:
                    syms, imps, head = parse_py(text) if lang == 'py' else parse_brace(text, lang)
                frow = db.execute('INSERT INTO files VALUES(?,?,?,?)', (p, *stats[p], lang)).lastrowid
                db.execute('INSERT INTO fts(rowid,file,symid,sw,dw,pw,bw) VALUES(?,?,0,?,?,?,?)', (-frow, p, '', ' '.join(words(head)), ' '.join(words(p)), ''))
                for name, kind, s, e, sig, doc in syms:
                    cid = db.execute('INSERT INTO syms(file,name,kind,s,e,sig) VALUES(?,?,?,?,?,?)', (p, name, kind, s, e, sig)).lastrowid
                    db.execute('INSERT INTO fts(rowid,file,symid,sw,dw,pw,bw) VALUES(?,?,?,?,?,?,?)', (cid, p, cid, ' '.join(words(name)), ' '.join(words(doc)), '', ''))
                # text chunks (code, comments, strings): find the line even deep inside a big function
                lines = text.split('\n')
                for k in range(0, len(lines), CHUNK_STEP):
                    part = lines[k:k + CHUNK]
                    body = list(dict.fromkeys(w for w in words('\n'.join(part)) if w not in STOP and len(w) > 1))
                    if not body:
                        continue
                    sig = next((x.strip()[:80] for x in part if x.strip()), '')
                    cid = db.execute("INSERT INTO syms(file,name,kind,s,e,sig) VALUES(?,'','chunk',?,?,?)", (p, k + 1, k + len(part), sig)).lastrowid
                    db.execute("INSERT INTO fts(rowid,file,symid,sw,dw,pw,bw) VALUES(?,?,?,'','','',?)", (cid, p, cid, ' '.join(body)))
                    if k + CHUNK >= len(lines):
                        break
                db.executemany('INSERT INTO imps VALUES(?,?)', [(p, s) for s in set(imps)])
            resolve_edges(db)
    except sqlite3.OperationalError:
        pass  # another agent is writing; use the existing index


def resolve_edges(db):
    allp = [r[0] for r in db.execute('SELECT path FROM files')]
    pset = set(allp)
    suffix = {}
    for p in allp:
        parts = os.path.splitext(p)[0].split('/')
        for k in range(len(parts)):
            suffix.setdefault('/'.join(parts[k:]), []).append(p)
    db.execute('DELETE FROM edges')
    rows = []
    for f, spec in db.execute('SELECT file,spec FROM imps').fetchall():
        dst = resolve(f, spec, pset, suffix)
        if dst and dst != f:
            rows.append((f, dst))
    db.executemany('INSERT INTO edges VALUES(?,?)', rows)


def resolve(f, spec, pset, suffix):
    def tryp(b):
        b = os.path.normpath(b).replace('\\', '/')
        stem = re.sub(r'\.(m|c)?jsx?$', '', b)
        for c in [b, *(stem + e for e in EXTS), *(stem + '/index' + e for e in EXTS), stem + '/__init__.py']:
            if c in pset:
                return c
    if f.endswith('.py'):
        lv = len(spec) - len(spec.lstrip('.'))
        mod = spec[lv:].replace('.', '/')
        base = os.path.dirname(f)
        for _ in range(lv - 1):
            base = os.path.dirname(base)
        if lv:
            return tryp(os.path.join(base, mod)) if mod else None
        return (suffix.get(mod) or [None])[0]
    if spec.startswith('.'):
        return tryp(os.path.join(os.path.dirname(f), spec))
    if spec.startswith(('@/', '~/')) or (not spec.startswith('@') and '/' in spec) or f.endswith('.sol'):
        key = re.sub(r'\.(sol|m?[jt]sx?)$', '', spec.lstrip('@~/'))
        hits = suffix.get(key)
        if hits:
            return min(hits, key=len)
    return None


# ---------- queries ----------
# weights tuned on 47 benchmark questions; CMP env overrides for tuning
P = {'bw': 1.0, 'covp': 2.0, 'sf': 0.6, 'cf': 1.0, 'md': 0.6, 'mw': 2.0, 'legacy': 0.4, 'qlit': 1.5, 'testw': 0.4, 'join': 2, 'files': 8, 'fns': 4, 'cs': 0.5, **json.loads(os.environ.get('CMP') or '{}')}
# irregular verbs the porter stemmer cannot join: a question says "saw", the code says "seen"
IRREG = {}
for g in ('see saw seen', 'show showed shown', 'send sent', 'make made', 'run ran', 'hold held', 'build built',
          'pay paid', 'sell sold', 'buy bought', 'keep kept', 'leave left', 'lose lost', 'tell told', 'write wrote written',
          'choose chose chosen', 'begin began begun', 'break broke broken', 'get got gotten', 'give gave given',
          'take took taken', 'find found', 'hide hid hidden', 'drive drove driven', 'rise rose risen', 'freeze froze frozen', 'forget forgot forgotten', 'feed fed', 'lead led', 'meet met', 'win won', 'shut', 'spend spent', 'bring brought', 'think thought', 'catch caught', 'fall fell fallen'):
    for w in g.split():
        IRREG[w] = g.split()


def ask(root, db, query, out):
    idents = [t for t in re.findall(r'[A-Za-z_][A-Za-z0-9_]{3,}', query) if re.search(r'[a-z][A-Z]|_|^[A-Z]{2,}$', t)]
    qw = [w for w in words(query) if w not in STOP and len(w) > 1]
    exact = []
    for t in idents:  # case/underscore-insensitive: fetch_user == fetchUser
        for r in db.execute("SELECT id,file,name,kind,s,e,sig FROM syms WHERE lower(replace(name,'_',''))=? LIMIT 10", (t.lower().replace('_', ''),)):
            exact.append(r)
    joined = []  # "kill switch" -> KillSwitch, "user agent" -> USER_AGENT: a boost in ranking, not a fact
    raw, kinds = words(query), set(words(query)) & {'class', 'interface', 'type', 'const'}
    for n in (3, 2):
        for i in range(len(raw) - n + 1):
            if not any(x in STOP for x in raw[i:i + n]):
                for r in db.execute("SELECT id,file,kind FROM syms WHERE kind!='chunk' AND lower(replace(name,'_',''))=? LIMIT 10", (''.join(raw[i:i + n]),)):
                    joined.append((r[1], r[0], set(raw[i:i + n]), (r[2] in kinds) + 1))
    named = []
    for fn in re.findall(r'[\w./-]+\.(?:tsx?|jsx?|mjs|cjs|sol|py|sql|md|ps1|sh|ya?ml|toml)\b', query):
        named += [r[0] for r in db.execute('SELECT path FROM files WHERE path=? OR path LIKE ? LIMIT 3', (fn, '%/' + fn))]
    scopes = scopes_of(db, query)
    lit, prose = literal_hits(root, db, query, scopes)
    if not (qw or exact or named or lit or prose):
        out.append('confidence: low, no searchable words in query; fall back to grep')
        return
    uw = list(dict.fromkeys(qw))
    rows_sc, rows_w, symsc, cover, fscore = {}, {}, {}, {}, {}
    nfiles = db.execute('SELECT COUNT(*) FROM files').fetchone()[0] or 1
    nchunks = db.execute("SELECT COUNT(*) FROM syms WHERE kind='chunk'").fetchone()[0] or 1
    chunk_rows = set()
    nsumm = db.execute('SELECT COUNT(*) FROM sfts').fetchone()[0] or 1
    for w in uw:  # per row: rarity x where it matched. Words must concentrate in one row, not spread over a big file
        pat = '(' + ' OR '.join(f'"{v}"' + ('*' if len(v) >= 4 else '') for v in IRREG.get(w, [w])) + ')'
        hit, files = {}, set()
        for col, wt in (('sw', 3.0), ('pw', 2.0), ('dw', 1.0)):
            for fid, symid in db.execute('SELECT file,symid FROM fts WHERE fts MATCH ?', (f'{col}:{pat}',)):
                k = (fid, int(symid))
                hit[k] = max(hit.get(k, 0), wt)
                files.add(fid)
        if len(files) > 0.5 * nfiles:
            continue  # in most files' names/docs: carries no signal
        idf = math.log((nfiles + 1) / (len(files) + 0.5))
        for k, wt in hit.items():
            rows_sc[k] = rows_sc.get(k, 0) + wt * idf
            rows_w.setdefault(k, set()).add(w)
        shit = [r[0] for r in db.execute('SELECT file FROM sfts WHERE sfts MATCH ?', (f'mw:{pat}',))]
        sidf = math.log((nsumm + 1) / (len(shit) + 0.5))
        for fid in shit:
            k = (fid, 0)
            rows_sc[k] = rows_sc.get(k, 0) + P['mw'] * sidf
            rows_w.setdefault(k, set()).add(w)
        bhit = [(fid, int(symid)) for fid, symid in db.execute('SELECT file,symid FROM fts WHERE fts MATCH ?', (f'bw:{pat}',))]
        bidf = math.log((nchunks + 1) / (len(bhit) + 0.5))  # chunk text is judged against other chunks
        for k in bhit:
            rows_sc[k] = rows_sc.get(k, 0) + bidf
            rows_w.setdefault(k, set()).add(w)
            chunk_rows.add(k)
    for k in chunk_rows:  # many task words inside one 30-line window is the strong signal, not one rare word
        rows_sc[k] *= P['bw'] * (len(rows_w[k]) / len(uw)) ** P['covp']
    for fid, sid, ws, mult in joined:  # the words, written together, are a symbol's whole name
        k = (fid, sid)
        rows_sc[k] = rows_sc.get(k, 0) + P['join'] * mult * len(ws)
        rows_w.setdefault(k, set()).update(ws)
    bestsym, bestchunk = {}, {}
    for (fid, sid), sc in rows_sc.items():
        if sid:
            symsc[sid] = sc
            best = bestchunk if (fid, sid) in chunk_rows else bestsym
            if sc > best.get(fid, (0, 0))[0]:
                best[fid] = (sc, sid)
    for fid in {k[0] for k in rows_sc}:
        bs, bc = bestsym.get(fid, (0, 0)), bestchunk.get(fid, (0, 0))
        fscore[fid] = rows_sc.get((fid, 0), 0) + P['sf'] * bs[0] + P['cf'] * bc[0]
        cover[fid] = rows_w.get((fid, 0), set()).union(*(rows_w.get((fid, x[1]), set()) for x in (bs, bc) if x[1]))
    ranked = []
    want_tests = bool({'test', 'tests', 'spec'} & set(uw))
    want_old = bool({'legacy', 'old', 'deprecated', 'archive', 'archived'} & set(uw))
    top0 = max(fscore.values(), default=1)
    for f in prose:  # quoted text found verbatim: lift the file, but it is not proof
        fscore[f] = fscore.get(f, 0.5 * top0) * P['qlit']
        cover.setdefault(f, set())
    for f, sc in fscore.items():
        if TEST.search(f) and not want_tests:
            sc *= P['testw']
        if f.endswith('.md') and not f.startswith(('.claude/agents/', '.claude/commands/')):  # docs restate code; agent files are the thing
            sc *= P['md']
        if OLD.search(f) and not want_old:  # superseded copies of the code: same words, wrong place
            sc *= P['legacy']
        if scopes and not any(f.startswith(x + '/') for x in scopes):
            continue
        if re.search(r'(^|/)(vendor|generated|dist|build|abi)/', f):
            sc *= 0.5
        ranked.append((sc, f))
    ranked.sort(reverse=True)
    if os.environ.get('CMDBG'):
        for sc, f in ranked[:int(os.environ['CMDBG'])]:
            bs = bestsym.get(f, (0, 0))
            print(f'DBG {sc:6.1f} file={rows_sc.get((f, 0), 0):5.1f} best={bs[0]:5.1f} cov={sorted(cover[f])} {f}', file=sys.stderr)
    rank_of = {f: i for i, (_, f) in enumerate(ranked)}
    exact_files = list(dict.fromkeys(named + [r[1] for r in exact] + sorted(lit, key=lambda f: rank_of.get(f, 1e9))))
    listing = re.search(r'\b(list|every|all)\b.*\bfiles?\b|\bwhich files\b', query, re.I)
    # 110 benchmark q: top 8 files with their best functions; the agent picks by meaning (right fn listed 91%)
    files = (exact_files + [f for _, f in ranked if f not in exact_files])[:12 if listing else P['files']]
    out.append('FILES (best first): pick the function whose name fits and read only its lines; none fits: file <path>')
    for f in files:
        syms = db.execute("SELECT id,name,kind,s,e FROM syms WHERE file=? AND kind!='chunk'", (f,)).fetchall()
        byid = {x[0]: x for x in syms}
        leaf = [x for x in syms if not any(y[3] >= x[3] and y[4] <= x[4] and (y[3], y[4]) != (x[3], x[4]) for y in syms)]
        sc = {}
        for (fid, sid), v in rows_sc.items():
            if fid != f or not sid:
                continue
            if sid in byid:  # matched by name/doc
                sc[sid] = sc.get(sid, 0) + v
            else:  # a 30-line body chunk: credit the innermost functions it overlaps
                ch = db.execute('SELECT s,e FROM syms WHERE id=?', (sid,)).fetchone()
                for x in leaf:
                    if x[3] <= ch[1] and ch[0] <= x[4]:
                        sc[x[0]] = sc.get(x[0], 0) + P['cs'] * v
        for r in exact:
            if r[1] == f:
                sc[r[0]] = sc.get(r[0], 0) + 100
        items = [f'line {lit[f][0]}'] if f in lit else []
        items += [f'{byid[k][1]} {byid[k][3]}-{byid[k][4]}' for _, k in sorted(((v, k) for k, v in sc.items()), reverse=True)[:P['fns']]]
        out.append(f'  {f}  ' + ' · '.join(items))

def scopes_of(db, query):
    """A path in the question that is a real folder (`web/src`) limits the answer to it; it is not text to find."""
    out = []
    for t in re.findall(r'[\w.-]+(?:/[\w.-]+)+/?', query):
        t = t.strip('/')
        if db.execute('SELECT 1 FROM files WHERE path LIKE ? LIMIT 1', (t + '/%',)).fetchone():
            out.append(t)
    return out


def literal_hits(root, db, query, scopes=()):
    """Verbatim search. Routes/packages (`/api/x/y`, `@scope/pkg`) are facts: {file: (line, text)}.
    Quoted prose ("dust hidden") is only a hint, also tried as dustHidden / dust_hidden, returned separately."""
    facts, prose = {}, {}
    for t in re.findall(r'[@\w.-]*/[\w./@-]+', query):
        if len(t) >= 6 and t.strip('/') not in scopes:
            facts.update(_grep(root, db, [t], 30))
    for a, b in re.findall(r'"([^"]{4,})"|`([^`]{4,})`', query):
        ws = (a or b).split()
        variants = [a or b] + ([ws[0].lower() + ''.join(w.capitalize() for w in ws[1:]), '_'.join(w.lower() for w in ws)] if len(ws) > 1 else [])
        prose.update(_grep(root, db, variants, 12))
    return facts, prose


def _grep(root, db, pats, cap):
    hits = {}
    for l in git(root, 'grep', '-n', '-i', '-F', '-I', *[x for p_ in pats for x in ('-e', p_)]).split('\n'):
        m = re.match(r'(.+?):(\d+):(.*)', l)
        if m and m.group(1) not in hits:
            hits[m.group(1)] = (int(m.group(2)), m.group(3).strip()[:80])
    idx = {r[0] for r in db.execute('SELECT path FROM files')} if hits else set()
    hits = {f: v for f, v in hits.items() if f in idx}
    return hits if 0 < len(hits) <= cap else {}  # more than that and the text is too common to point anywhere


def run_cmd(root, f):
    d = os.path.dirname(f)
    while True:
        pj = os.path.join(root, d, 'package.json')
        if os.path.exists(pj):
            try:
                if 'test' in json.load(open(pj, encoding='utf-8')).get('scripts', {}):
                    return (f'cd {d} && ' if d else '') + 'npm test'
            except (OSError, ValueError):
                pass
        if os.path.exists(os.path.join(root, d, 'foundry.toml')):
            return (f'cd {d} && ' if d else '') + 'forge test'
        if not d:
            return None
        d = os.path.dirname(d)


def impact(root, db, target, out):
    # accepts "path", "symbol", "symbol in path", "path symbol", "Name interface"
    toks = target.split()
    f = next((t for t in toks if db.execute('SELECT 1 FROM files WHERE path=?', (t,)).fetchone()), None)
    sym = next((t for t in toks if t != f and re.fullmatch(r'[A-Za-z_$][\w$]*', t) and t not in STOP
                and t not in ('interface', 'class', 'type', 'function', 'const', 'method')), None)
    if sym and not f:  # several definitions can share a name: take the one other files import most
        cands = [r[0] for r in db.execute("SELECT DISTINCT file FROM syms WHERE name=? AND kind!='chunk'", (sym,))]
        f = max(cands, key=lambda c: (not TEST.search(c), db.execute('SELECT COUNT(*) FROM edges WHERE dst=?', (c,)).fetchone()[0]), default=None)
    if not f:
        out.append('not found: ' + target); return

    def uses(p):  # importer that names the symbol, not just the file
        try:
            return sym in open(os.path.join(root, p), encoding='utf-8', errors='replace').read()
        except OSError:
            return False
    lvl, seen, tests = [f], {f}, []
    for depth in (1, 2):
        nxt = []
        for p in lvl:
            for (s,) in db.execute('SELECT src FROM edges WHERE dst=?', (p,)):
                if s not in seen:
                    seen.add(s); nxt.append(s)
        nxt.sort(key=lambda p: (bool(sym) and depth == 1 and not uses(p), bool(TEST.search(p)), p))  # real users, source before tests
        out.append(f'DEPTH{depth} ({len(nxt)})')
        out += [f'  {p}' for p in nxt[:15]] + ([f'  ... +{len(nxt) - 15}'] if len(nxt) > 15 else [])
        tests += [p for p in nxt if TEST.search(p)]
        lvl = nxt
    out.append('TESTS'); out += [f'  {p}' for p in dict.fromkeys(tests)][:8] or ['  none found']


def outline(root, db, path, out):
    if not db.execute('SELECT 1 FROM files WHERE path=?', (path,)).fetchone():
        out.append('not indexed: ' + path); return
    for s, e, k, n in db.execute("SELECT s,e,kind,name FROM syms WHERE file=? AND kind!='chunk' ORDER BY s", (path,)):
        out.append(f'  {s}-{e} {k} {n}')
    out.append('IMPORTS'); out += [f'  {r[0]}' for r in db.execute('SELECT dst FROM edges WHERE src=?', (path,))]
    out.append('IMPORTED-BY'); out += [f'  {r[0]}' for r in db.execute('SELECT src FROM edges WHERE dst=? LIMIT 20', (path,))]


SNIPPET = """## codemap
Before grepping a task, run: python "{path}" ask "<task in plain words> <4-8 words the code likely uses>"
e.g. ask "where are passwords hidden in logs mask redact obfuscate sensitive secret"
It lists the best files with their best functions (name start-end). Read only the function that fits.
None fits: file <path> (outline) instead of reading the whole file. Also: impact <file|symbol> (importers + tests).
"""


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['ask', 'impact', 'file', 'init', 'hook'])
    ap.add_argument('arg', nargs='*')
    ap.add_argument('--root')
    ap.add_argument('--write', action='store_true')
    a = ap.parse_args()
    start = a.root or os.getcwd()
    if not a.root and not git(start, 'rev-parse', '--show-toplevel').strip() and a.cmd != 'init':
        if a.cmd != 'hook':
            print('codemap: not inside a git repo; pass --root <dir>. Fall back to grep.')
        return
    root = find_root(start)
    if a.cmd == 'init':
        snip = SNIPPET.replace('{path}', os.path.abspath(__file__).replace(os.sep, '/'))
        print(snip)
        if a.write:
            open(os.path.join(root, 'CLAUDE.md'), 'a', encoding='utf-8').write('\n' + snip)
            print('appended to CLAUDE.md')
        return
    q = ' '.join(a.arg)
    for attempt in range(3):
        out = []
        try:
            db = connect(root)
            if a.cmd == 'hook':  # never index at session start: it can block for seconds
                n = db.execute('SELECT COUNT(*) FROM files').fetchone()[0]
                out.append(f'codemap: {n} files indexed. ' if n else 'codemap: index builds on first use. ')
                me = os.path.abspath(__file__).replace(os.sep, '/')
                out[0] += f'Before grepping a task run: python "{me}" ask "<task> <4-8 words the code likely uses>" (also: impact <file|symbol>, file <path>). Read only the listed function that fits; none fits: file <path>, not the whole file.'
            else:
                refresh(root, db)
                {'ask': ask, 'impact': impact}.get(a.cmd, outline)(root, db, q.replace(os.sep, '/') if a.cmd == 'file' else q, out)
            break
        except (sqlite3.OperationalError, MemoryError):  # another process held the index (SQLite reports some clashes as NOMEM)
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1)); continue
            out = [] if a.cmd == 'hook' else ['codemap busy (index locked by another process). Fall back to grep.']
        except Exception as e:  # never hand an agent a traceback
            out = [] if a.cmd == 'hook' else [f'codemap error ({type(e).__name__}: {e}). Fall back to grep.']
            break
    print('\n'.join(out))


if __name__ == '__main__':
    main()
