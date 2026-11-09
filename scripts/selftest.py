"""Self-check: builds a throwaway git repo, asserts parsing, import graph, ask, impact."""
import os, subprocess, sys, tempfile
os.environ['CODEMAP_NO_SUMMARIES'] = '1'  # never call a paid API from the self-test
cm = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'codemap.py')
assert chr(8) not in open(cm, encoding='utf-8').read(), 'backspace char in codemap.py: a \b regex was mangled'
F = {
 'src/auth/token.ts': "/** Verify the login token and expiry. */\nexport function verifyAuthToken(t: string) {\n  return t.length > 3\n}\n",
 'src/api/login.ts': "import { verifyAuthToken } from '../auth/token'\nexport const loginRoute = (req: any) => verifyAuthToken(req.t)\n",
 'src/auth/token.test.ts': "import { verifyAuthToken } from './token'\ntest('x', () => verifyAuthToken('abcd'))\n",
 'src/billing/invoice.py': '"""Create and send customer invoices."""\nfrom .tax import add_tax\ndef make_invoice(n):\n    return add_tax(n)\n',
 'src/billing/tax.py': 'def add_tax(n):\n    return n * 1.2\n',
 'contracts/Vault.sol': "// Holds user deposits.\ncontract Vault {\n  function withdrawAll() external {}\n}\n",
 'secrets/keys.ts': "export function leakedSecret() {}\n",
 'src/price.ts': "export function getSolPrice() {\n  return 1\n}\n",
 'src/graph/View.tsx': "export function draw(\n  data: any,\n  opts: { w: number },\n) {\n  d3.select('svg').append('circle')\n  return data\n}\n",
 'package.json': '{"scripts":{"test":"jest"}}',
}
def run(d, *a):
    return subprocess.run([sys.executable, cm, *a], cwd=d, capture_output=True, text=True, encoding='utf-8').stdout
with tempfile.TemporaryDirectory() as d:
    for p, t in F.items():
        os.makedirs(os.path.dirname(os.path.join(d, p)) or d, exist_ok=True)
        open(os.path.join(d, p), 'w', encoding='utf-8').write(t)
    subprocess.run(['git', 'init', '-q'], cwd=d); subprocess.run(['git', 'add', '-A'], cwd=d)
    o = run(d, 'ask', 'where do we verify the login token')
    assert 'src/auth/token.ts:2-4' in o, o
    assert 'src/api/login.ts' in o.split('CALLERS')[1], o          # import edge resolved
    assert 'token.test.ts' in o.split('TESTS')[1], o                # test linked
    assert 'npm test' in o and 'confidence: high' in o, o
    assert 'leakedSecret' not in run(d, 'ask', 'leakedSecret'), 'secrets must not be indexed'
    assert 'src/auth/token.ts' in run(d, 'ask', 'verify_auth_token'), 'underscore identifier'
    assert 'src/billing/tax.py' in run(d, 'ask', 'where is tax added')  # python ast
    assert 'src/billing/tax.py' in run(d, 'impact', 'add_tax') or 'invoice.py' in run(d, 'impact', 'src/billing/tax.py')  # py relative import
    assert 'contracts/Vault.sol' in run(d, 'ask', 'withdraw deposits from vault')  # solidity
    assert run(d, 'ask', 'fix login.ts').splitlines()[1].strip().startswith('src/api/login.ts'), 'named file first'
    assert 'src/price.ts' in run(d, 'ask', 'fetch the SOL price'), '"sol" is a word, not only an extension'
    o = run(d, 'ask', 'append svg circle')
    assert 'src/graph/View.tsx:1-' in o, o  # body text indexed
    assert '1-7 fn draw' in run(d, 'file', 'src/graph/View.tsx')  # multi-line params range
    assert 'src/price.ts' in run(d, 'ask', 'see "getSolPrice()" in price.ts').split('\n')[1]  # literal + named file
    assert 'no searchable words' in run(d, 'ask', 'the a of')
    open(os.path.join(d, 'src/auth/token.ts'), 'a').write('export function newThing() {}\n')   # staleness
    assert 'newThing' in run(d, 'ask', 'newThing')
    open(os.path.join(d, '.codemap/index.db'), 'wb').write(b'garbage')                           # corrupt index recovers
    assert 'src/auth/token.ts' in run(d, 'ask', 'verifyAuthToken')
print('selftest ok')
