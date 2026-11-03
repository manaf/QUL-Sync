"""Shared helpers: HTTP, GitHub API, jsDelivr purge, QUL access, JSON files.

Standard library only, so the GitHub Actions job needs no installs.
"""
import base64
import gzip
import html
import io
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / 'config.json').read_text())
OWNER = CONFIG['owner']
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0 Safari/537.36'

try:
    import certifi
    SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL = ssl.create_default_context()


class HttpError(Exception):
    def __init__(self, status, url, body=b''):
        super().__init__(f'HTTP {status} for {url}: {body[:300]!r}')
        self.status = status
        self.body = body


def request(url, method='GET', headers=None, data=None, retries=3, timeout=120):
    """Returns (status, body bytes, response headers). Retries network
    errors and 5xx; 4xx raises HttpError at once."""
    hdrs = {'User-Agent': UA, 'Accept-Encoding': 'gzip', **(headers or {})}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout, context=SSL) as r:
                body = r.read()
                if r.headers.get('Content-Encoding') == 'gzip':
                    body = gzip.decompress(body)
                return r.status, body, r.headers
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code < 500 or attempt == retries - 1:
                raise HttpError(e.code, url, body)
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == retries - 1:
                raise
        time.sleep(3 * (attempt + 1))


# ─── JSON files ───────────────────────────────────────────────────────

def compact(obj) -> str:
    """The byte format every data repo file uses."""
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'))


def load(name, default=None):
    p = ROOT / name
    return json.loads(p.read_text()) if p.exists() else default


def save(name, obj, one_per_line=False):
    p = ROOT / name
    p.parent.mkdir(parents=True, exist_ok=True)
    if one_per_line:  # big {key: small value} maps: readable git diffs
        lines = [f'  {json.dumps(k)}: {json.dumps(obj[k], ensure_ascii=False, sort_keys=True)}' for k in sorted(obj)]
        p.write_text('{\n' + ',\n'.join(lines) + '\n}\n')
    else:
        p.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + '\n')


# ─── GitHub ───────────────────────────────────────────────────────────

class GitHub:
    API = 'https://api.github.com'

    def __init__(self, token):
        self.token = token

    def _call(self, method, path, payload=None, params=None):
        url = self.API + path + ('?' + urllib.parse.urlencode(params) if params else '')
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers['Content-Type'] = 'application/json'
        _, body, _ = request(url, method, headers, data)
        return json.loads(body) if body else None

    def get(self, path, **params):
        return self._call('GET', path, params=params or None)

    def post(self, path, payload):
        return self._call('POST', path, payload)

    def patch(self, path, payload):
        return self._call('PATCH', path, payload)

    def put(self, path, payload):
        return self._call('PUT', path, payload)

    def delete(self, path):
        return self._call('DELETE', path)

    # repo content
    def head(self, repo, branch='main'):
        return self.get(f'/repos/{OWNER}/{repo}/git/ref/heads/{branch}')['object']['sha']

    def blobs(self, repo, commit):
        """{path: (blob sha, size)} for every file at a commit."""
        tree = self.get(f'/repos/{OWNER}/{repo}/git/trees/{commit}', recursive='1')
        if tree.get('truncated'):
            raise RuntimeError(f'{repo}: file list truncated by GitHub')
        return {e['path']: (e['sha'], e.get('size', 0)) for e in tree['tree'] if e['type'] == 'blob'}

    def raw(self, repo, ref, path) -> bytes:
        url = f'https://raw.githubusercontent.com/{OWNER}/{repo}/{ref}/{urllib.parse.quote(path)}'
        return request(url, headers={'Authorization': f'Bearer {self.token}'} if self.token else None)[1]

    def put_file(self, repo, path, content: bytes, message, branch='main'):
        """Create or replace one file on a branch (index files)."""
        try:
            sha = self.get(f'/repos/{OWNER}/{repo}/contents/{path}', ref=branch)['sha']
        except HttpError as e:
            if e.status != 404:
                raise
            sha = None
        payload = {'message': message, 'content': base64.b64encode(content).decode(), 'branch': branch}
        if sha:
            payload['sha'] = sha
        return self.put(f'/repos/{OWNER}/{repo}/contents/{path}', payload)

    def commit_files(self, repo, branch, files: dict, message, base='main'):
        """Commit {path: bytes} on top of `base` and point `branch` at it
        (created or force-moved). Uses the Git Data API, so nothing is
        cloned — Tafseer-Arabic-Other alone is 2.4 GB checked out."""
        base_sha = self.head(repo, base)
        base_tree = self.get(f'/repos/{OWNER}/{repo}/git/commits/{base_sha}')['tree']['sha']
        entries = []
        for path, content in sorted(files.items()):
            blob = self.post(f'/repos/{OWNER}/{repo}/git/blobs',
                             {'content': base64.b64encode(content).decode(), 'encoding': 'base64'})
            entries.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
        tree = self.post(f'/repos/{OWNER}/{repo}/git/trees', {'base_tree': base_tree, 'tree': entries})
        commit = self.post(f'/repos/{OWNER}/{repo}/git/commits',
                           {'message': message, 'tree': tree['sha'], 'parents': [base_sha]})
        try:
            self.patch(f'/repos/{OWNER}/{repo}/git/refs/heads/{branch}', {'sha': commit['sha'], 'force': True})
        except HttpError as e:
            if e.status not in (404, 422):
                raise
            self.post(f'/repos/{OWNER}/{repo}/git/refs', {'ref': f'refs/heads/{branch}', 'sha': commit['sha']})
        return commit['sha']

    # pull requests + issues
    def pulls(self, repo, branch, state='all'):
        return self.get(f'/repos/{OWNER}/{repo}/pulls', head=f'{OWNER}:{branch}', state=state)

    def open_or_update_pr(self, repo, branch, title, body):
        """Returns (pr, created). An open PR for the branch is updated."""
        open_prs = self.pulls(repo, branch, 'open')
        if open_prs:
            pr = self.patch(f'/repos/{OWNER}/{repo}/pulls/{open_prs[0]["number"]}', {'title': title, 'body': body})
            return pr, False
        pr = self.post(f'/repos/{OWNER}/{repo}/pulls', {'title': title, 'head': branch, 'base': 'main', 'body': body})
        return pr, True

    def issue(self, repo, title, body):
        return self.post(f'/repos/{OWNER}/{repo}/issues', {'title': title, 'body': body})


def purge_jsdelivr(repo, path):
    try:
        request(f'https://purge.jsdelivr.net/gh/{OWNER}/{repo}@main/{path}', retries=2, timeout=60)
    except Exception:
        pass  # the CDN catches up within ~12 h anyway; the app falls back to GitHub


# ─── QUL ──────────────────────────────────────────────────────────────

QUL = 'https://qul.tarteel.ai'


class QulTokenExpired(Exception):
    pass


class Qul:
    """qul.tarteel.ai pages. Listings and change logs are public; file
    downloads need the remember_user_token cookie of a signed-in user."""

    def __init__(self, remember_token=None):
        self.cookie = ''
        if remember_token:
            self.cookie = f'remember_user_token={remember_token}'
            _, _, headers = self._get('/resources/tafsir', raw=True)
            for sc in headers.get_all('Set-Cookie') or []:
                m = re.match(r'(_quran_com-community_session=[^;]+)', sc)
                if m:
                    self.cookie += '; ' + m.group(1)

    def _get(self, path, raw=False):
        time.sleep(1)  # one request a second: be a light client
        status, body, headers = request(QUL + path, headers={'Cookie': self.cookie} if self.cookie else None)
        return (status, body, headers) if raw else body.decode('utf-8', 'replace')

    def change_logs(self, after_id):
        """Change log entries newer than `after_id`, newest first:
        [{id, date, title, text, kind, resource_id, resource_name}]."""
        out = []
        for page in range(1, 50):
            text = self._get(f'/change_logs?page={page}')
            arts = re.findall(r'<article.*?</article>', text, re.S)
            if not arts:
                break
            for a in arts:
                log = re.search(r'href="/change_logs/(\d+)"', a)
                if not log:
                    continue
                lid = int(log.group(1))
                if lid <= after_id:
                    return out
                res = re.search(r'href="/resources/([a-z_-]+)/([^"/]+)"[^>]*>([^<]*)<', a)
                title = re.search(r'<h2[^>]*>(.*?)</h2>', a, re.S)
                date = re.search(r'<span>([A-Z][a-z]+ \d{1,2}, \d{4}[^<]*)</span>', a)
                desc = re.search(r'trix-content[^>]*>(.*?)</div>', a, re.S)
                out.append({
                    'id': lid,
                    'date': date.group(1).strip() if date else '',
                    'title': strip_tags(title.group(1)) if title else '',
                    'text': strip_tags(desc.group(1)) if desc else '',
                    'kind': res.group(1) if res else '',
                    'resource_id': res.group(2) if res else '',
                    'resource_name': html.unescape(res.group(3)).strip() if res else '',
                })
        return out

    def listing(self, kind):
        """{resource id: {name, tags}} from /resources/<kind> (tafsir,
        recitation). `tags` are the labels on the resource's card — for a
        tafseer, one of them is its language."""
        text = self._get(f'/resources/{kind}')
        items = {}
        for m in re.finditer(rf'href="/resources/{kind}/(\d+)"[^>]*>(.*?)</a>', text, re.S):
            rid, name = m.group(1), strip_tags(m.group(2))
            if name and rid not in items:
                window = text[m.end():m.end() + 4000]
                tags = [html.unescape(t).strip() for t in re.findall(r'title="About ([^"]+)"', window)]
                items[rid] = {'name': name, 'tags': tags}
        return items

    def download(self, kind, rid) -> dict:
        """{file name: bytes} from a resource's JSON download (a zip)."""
        text = self._get(f'/resources/{kind}/{rid}')
        m = re.search(rf'href="(/resources/{kind}/[a-f0-9]{{32}}/download[^"]*)"[^>]*>\s*Download json', text)
        if not m:
            raise QulTokenExpired('No download link on the resource page: the QUL login token has expired.')
        _, body, _ = self._get(m.group(1), raw=True)
        z = zipfile.ZipFile(io.BytesIO(body))
        return {i.filename.rsplit('/', 1)[-1]: z.read(i) for i in z.infolist() if not i.is_dir()}


def strip_tags(s):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', s))).strip()


def env(name, required=True):
    v = os.environ.get(name, '').strip()
    if required and not v:
        raise SystemExit(f'Missing secret/env {name}')
    return v
