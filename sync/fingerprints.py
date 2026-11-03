"""Job B — hourly fingerprint sync.

The app re-downloads a data file it already has only when that file's
fingerprint (first 8 hex chars of its MD5) changes in an index file:
  Tafseer-Index/languages.json   juz_fp: 30 per tafseer
  Recitation-Index/reciters.json files: per reciter, {name: fp}
This job keeps those in step with the data repos, whatever changed them
(a merged QUL pull request or a manual push): GitHub's file list gives
every file's blob id, and only files whose blob id changed since the last
run are downloaded (cache/*.json). Merged new-tafseer pull requests are
added to repos.json and the index here.

Pushes only when an index actually changes, then clears jsDelivr for the
index and the changed files. dry_run / --dry-run: report only. --local:
print, don't save caches.
"""
import copy
import hashlib
import json
import re
import sys
from datetime import date

from common import CONFIG, OWNER, GitHub, load, save, env, purge_jsdelivr

REPOS = CONFIG['repos']
LANGS = load('lang_table.json')


def fingerprint(body: bytes) -> str:
    return hashlib.md5(body).hexdigest()[:8]


def refresh(gh, cache, repo, wanted, changed):
    """Update cache entries for `wanted` paths in repo; returns the
    repo's file list. Appends changed paths to `changed`."""
    commit = gh.head(repo)
    blobs = gh.blobs(repo, commit)
    for path in wanted:
        if path not in blobs:
            continue
        sha, size = blobs[path]
        key = f'{repo}/{path}'
        if cache.get(key, {}).get('blob') == sha:
            continue
        body = gh.raw(repo, commit, path)
        if len(body) != size:
            raise RuntimeError(f'{key}: got {len(body)} bytes, GitHub lists {size}')
        if key in cache:
            changed.append((repo, path))
        cache[key] = {'blob': sha, 'fp': fingerprint(body)}
    return blobs


def tafseer_index_text(manifest):
    text = json.dumps(manifest, indent=2, ensure_ascii=False)
    text = re.sub(r'"juz_fp": \[\s*([^\]]*?)\s*\]',
                  lambda m: '"juz_fp": [' + ', '.join(x.strip() for x in m.group(1).split(',')) + ']', text)
    return text + '\n'


def tafseer_entry(t, fps):
    base = f'https://cdn.jsdelivr.net/gh/{OWNER}/{t["repo"]}@main/{t["slug"]}/{{juz}}.json'
    raw = f'https://raw.githubusercontent.com/{OWNER}/{t["repo"]}/main/{t["slug"]}/{{juz}}.json'
    return {'qul_id': t['qul_id'], 'slug': t['slug'], 'name': t['name'], 'repo': t['repo'],
            'juz_url': base, 'juz_url_fallback': raw, 'juz_fp': fps}


def sync_tafseer(gh, notes, dry):
    repos = load('repos.json')
    state = load('state.json', {}) or {}
    pending = state.get('pending_new', {})
    cache = load('cache/tafseer_fp.json', {})
    changed = []
    by_repo = {}
    for info in repos.values():
        for t in info['tafsirs']:
            by_repo.setdefault(t['repo'], []).append(t['slug'])
    for p in pending.values():
        by_repo.setdefault(p['repo'], [])
    promoted = []
    for repo, slugs in sorted(by_repo.items()):
        wanted = [f'{s}/{j}.json' for s in slugs for j in range(1, 31)]
        blobs = refresh(gh, cache, repo, wanted, changed)
        for rid, p in list(pending.items()):  # a merged new-tafseer PR: all 30 files on main
            if p['repo'] == repo and all(f'{p["slug"]}/{j}.json' in blobs for j in range(1, 31)):
                refresh(gh, cache, repo, [f'{p["slug"]}/{j}.json' for j in range(1, 31)], [])
                promoted.append(rid)

    index_repo = REPOS['tafseer_index']
    current = json.loads(gh.raw(index_repo, gh.head(index_repo), 'languages.json'))
    new = copy.deepcopy(current)
    for code, lang in new['languages'].items():
        for t in lang['tafsirs']:
            fps = [cache.get(f'{t["repo"]}/{t["slug"]}/{j}.json', {}).get('fp') for j in range(1, 31)]
            if None in fps:
                raise RuntimeError(f'{t["repo"]}/{t["slug"]}: a juz file is missing on GitHub')
            t['juz_fp'] = fps
    for rid in promoted:
        p = pending[rid]
        meta = LANGS.get(p['language'])
        if not meta:
            notes.append(f'New tafseer {p["name"]}: language "{p["language"]}" is not in sync/lang_table.json — add it, then it is published on the next run.')
            continue
        lang = new['languages'].setdefault(meta['code'], {
            'name_en': p['language'], 'name_native': meta['native'],
            'direction': 'rtl' if meta['rtl'] else 'ltr', 'repos': [p['repo']], 'tafsirs': []})
        if p['repo'] not in lang['repos']:
            lang['repos'].append(p['repo'])
        if not any(t['repo'] == p['repo'] and t['slug'] == p['slug'] for t in lang['tafsirs']):
            fps = [cache[f'{p["repo"]}/{p["slug"]}/{j}.json']['fp'] for j in range(1, 31)]
            lang['tafsirs'].append(tafseer_entry(p, fps))
        repos.setdefault(p['language'], {'language': p['language'], 'repos': [p['repo']], 'tafsirs': []})
        repos[p['language']]['tafsirs'].append({k: p[k] for k in ('qul_id', 'name', 'slug', 'repo')})
        if p['repo'] not in repos[p['language']]['repos']:
            repos[p['language']]['repos'].append(p['repo'])
        notes.append(f'New tafseer published: {p["name"]} ({p["language"]})')
        if not dry:
            del pending[rid]

    if new != current:
        new['updated'] = str(date.today())
        changed_files = sorted({f'{r}/{p}' for r, p in changed})
        notes.append(f'Tafseer index: {len(changed_files)} juz file(s) changed' +
                     (f' — e.g. {", ".join(changed_files[:5])}' if changed_files else ''))
        if not dry:
            gh.put_file(index_repo, 'languages.json', tafseer_index_text(new).encode('utf-8'),
                        f'Update fingerprints ({len(changed_files)} juz file(s) changed)')
            purge_jsdelivr(index_repo, 'languages.json')
            for r, p in changed:
                purge_jsdelivr(r, p)
    return cache, repos, state


def sync_recitations(gh, notes, dry):
    cache = load('cache/recitation_fp.json', {})
    index_repo, data_repo = REPOS['recitation_index'], REPOS['recitations']
    current = json.loads(gh.raw(index_repo, gh.head(index_repo), 'reciters.json'))
    commit = gh.head(data_repo)
    blobs = gh.blobs(data_repo, commit)
    changed = []
    wanted = [p for p in blobs if p.endswith('.json') and '/' in p and not p.endswith('/meta.json')]
    refresh(gh, cache, data_repo, wanted, changed)

    def order(name):
        stem = name.rsplit('.', 1)[0]
        return (0, int(stem), '') if stem.isdigit() else (1, 0, stem)

    new = copy.deepcopy(current)
    for r in new['reciters']:
        names = sorted((p.split('/', 1)[1] for p in wanted if p.startswith(r['slug'] + '/')), key=order)
        r['files'] = {n: cache[f'{data_repo}/{r["slug"]}/{n}']['fp'] for n in names}
    if new != current:
        new['updated'] = str(date.today())
        notes.append(f'Reciter index: {len(changed)} file(s) changed' +
                     (f' — e.g. {", ".join(p for _, p in changed[:5])}' if changed else ''))
        if not dry:
            gh.put_file(index_repo, 'reciters.json', (json.dumps(new, indent=2, ensure_ascii=False) + '\n').encode('utf-8'),
                        f'Update fingerprints ({len(changed)} file(s) changed)')
            purge_jsdelivr(index_repo, 'reciters.json')
            for r, p in changed:
                purge_jsdelivr(r, p)
    return cache


def main():
    dry = CONFIG['dry_run'] or '--dry-run' in sys.argv
    local = '--local' in sys.argv
    gh = GitHub(env('DATA_TOKEN'))
    notes = []
    t_cache, repos, state = sync_tafseer(gh, notes, dry)
    r_cache = sync_recitations(gh, notes, dry)
    print('\n'.join(notes) or 'Indexes already up to date.')
    save('cache/tafseer_fp.json', t_cache, one_per_line=True)
    save('cache/recitation_fp.json', r_cache, one_per_line=True)
    if local:
        return
    if not dry:
        save('repos.json', repos)
        save('state.json', state)
    if dry and notes:
        # Hourly runs: report each distinct "would change" once, not every hour.
        seen = load('cache/dry_run_reported.json', [])
        key = hashlib.md5('\n'.join(notes).encode()).hexdigest()
        if key not in seen:
            bot = GitHub(env('GITHUB_TOKEN'))
            body = '\n'.join(f'- {n}' for n in notes)
            bot.issue('QUL-Sync', f'[Dry run] Fingerprint sync {date.today()}',
                      f'@{CONFIG["notify"]} Dry run — the index files were not changed. This run would have:\n\n{body}')
            save('cache/dry_run_reported.json', seen + [key])


if __name__ == '__main__':
    main()
