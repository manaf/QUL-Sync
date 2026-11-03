"""Job A — daily QUL check.

1. Read QUL's change log (newest first) down to the last entry seen.
2. For each entry about a tafseer or reciter the app uses: download it,
   convert it to our file format and compare it ayah by ayah with our
   copy. Real differences become a pull request in that data repo (an
   open one is updated); no differences are just noted.
3. New tafseers on QUL become a pull request too (a new language goes to
   the shared Tafseer-Additional repo); empty ones are skipped. New
   reciters and changes to resources built into the app are reported
   only.
4. Everything is summarised in one issue in this repo, opened by
   github-actions[bot] so GitHub emails the owner.

With "dry_run" in config.json (or --dry-run) nothing is pushed or opened
in the data repos; the issue says what would have happened. --local
prints the report instead of opening an issue and leaves state.json alone.
"""
import json
import os
import sys
import traceback
import unicodedata
import re
from datetime import date

from common import CONFIG, OWNER, GitHub, Qul, QulTokenExpired, load, save, env, strip_tags
from convert import (coverage, compare_recitation, compare_tafseer, juz_of, surah_recitation_files,
                     tafseer_files, ayah_recitation_files, word_changes)

REPOS = CONFIG['repos']
LANGS = load('lang_table.json')
# QUL's language labels → the names repos.json uses
LANG_FIX = {'Central khmer': 'Khmer', 'Fulah': 'Fulani', 'Tagalog': 'Filipino', 'uyghur': 'Uyghur', 'Sinhalese': 'Sinhala', 'Azerbaijani': 'Azeri'}
MAX_ROWS = 40


def slugify(text):
    s = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii').lower()
    s = re.sub(r'[^a-z0-9]+', '-', s.replace("'", '').replace('(', '').replace(')', '')).strip('-')
    return re.sub(r'^(tafsir|tafseer)-', '', s)


class Report:
    def __init__(self, dry):
        self.dry = dry
        self.items = []      # (section, markdown line)
        self.errors = []

    def add(self, section, line):
        self.items.append((section, line))

    def markdown(self):
        order = ['Ready for your review', 'Would open a pull request (dry run)', 'New tafseers',
                 'Built into the app (needs an app update)', 'New reciters on QUL (not added)',
                 'Checked: no differences']
        out = []
        for sec in order:
            lines = [l for s, l in self.items if s == sec]
            if lines:
                out.append(f'### {sec}\n' + '\n'.join(f'- {l}' for l in lines))
        if self.errors:
            out.append('### Problems\n' + '\n'.join(f'- {e}' for e in self.errors))
        return '\n\n'.join(out)

    def worth_an_issue(self):
        return self.errors or any(s != 'Checked: no differences' for s, _ in self.items)


def changes_table(changes):
    rows = ['| Ayah | Change |', '|---|---|']
    for key, old, new in changes[:MAX_ROWS]:
        if not old:
            snippet = 'added'
        elif not new:
            snippet = 'removed'
        else:
            snippet = ' '.join(word_changes(old, new)) or 'formatting only'
        rows.append(f'| {key} | {snippet.replace("|", "∣")} |')
    if len(changes) > MAX_ROWS:
        rows.append(f'| … | and {len(changes) - MAX_ROWS} more |')
    return '\n'.join(rows)


def log_block(logs):
    return '\n'.join(f'> **{l["title"]}** ({l["date"]}) — {l["text"]} [QUL change log #{l["id"]}](https://qul.tarteel.ai/change_logs/{l["id"]})'
                     for l in logs)


def publish(gh, report, repo, branch, files, title, body, dry, summary):
    if dry:
        report.add('Would open a pull request (dry run)', f'{title} — {summary} → `{repo}`')
        return None
    gh.commit_files(repo, branch, files, title)
    pr, created = gh.open_or_update_pr(repo, branch, title, body)
    report.add('Ready for your review', f'[{title}]({pr["html_url"]}) — {summary}' + ('' if created else ' (updated)'))
    return pr['html_url']


def tafseer_update(gh, qul, report, t, logs, dry):
    fmt = 'pointers' if t['repo'] == REPOS['featured'] else 'resolved'
    files = qul.download('tafsir', t['qul_id'])
    qul_json = json.loads(next(v for k, v in files.items() if k.endswith('.json')))
    new = tafseer_files(qul_json, fmt)
    commit = gh.head(t['repo'])
    have = gh.blobs(t['repo'], commit)
    old = {j: gh.raw(t['repo'], commit, f'{t["slug"]}/{j}.json').decode('utf-8')
           for j in range(1, 31) if f'{t["slug"]}/{j}.json' in have}
    changes = compare_tafseer(old, new)
    name = f'{t["name"]} ({t["language"]})'
    if not changes:
        report.add('Checked: no differences', f'{name} — QUL change log #{logs[0]["id"]} is already in our copy')
        return
    juz = sorted({juz_of(k) for k, _, _ in changes})
    out = {f'{t["slug"]}/{j}.json': new[j].encode('utf-8') for j in juz}
    title = f'QUL update: {name}'
    extra = ''
    if f'tafsir/{t["qul_id"]}' in CONFIG['bundled_in_app']:
        extra = '\n\n⚠️ This tafseer is also **built into the app**; that copy only changes with an app update.'
    body = (f'{log_block(logs)}\n\n**{len(changes)} ayah(s) changed** in juz {", ".join(map(str, juz))}:\n\n'
            f'{changes_table(changes)}\n\nMerging publishes it: within about an hour the fingerprints update and '
            f'phones that already have this tafseer re-download only the changed juz.{extra}\n\n'
            f'Opened by the daily QUL check in {OWNER}/QUL-Sync.')
    publish(gh, report, t['repo'], f'qul/tafsir-{t["qul_id"]}', out, title, body, dry,
            f'{len(changes)} ayah(s) in juz {", ".join(map(str, juz))}')


def recitation_update(gh, qul, report, rid, rec, logs, dry):
    files = qul.download('recitation', rid)
    if rec['format'] == 'ayah':
        new = ayah_recitation_files(json.loads(next(v for k, v in files.items() if k.endswith('.json'))))
    else:
        new = surah_recitation_files(files)
    repo = REPOS['recitations']
    commit = gh.head(repo)
    have = gh.blobs(repo, commit)
    old = {n: gh.raw(repo, commit, f'{rec["slug"]}/{n}').decode('utf-8') for n in new if f'{rec["slug"]}/{n}' in have}
    changes = compare_recitation(old, new)
    name = f'reciter {rec["slug"]}'
    if not changes:
        report.add('Checked: no differences', f'{name} — QUL change log #{logs[0]["id"]} is already in our copy')
        return
    out = {f'{rec["slug"]}/{n}': new[n].encode('utf-8') for n, _ in changes}
    title = f'QUL update: {name}'
    rows = '\n'.join(f'- `{n}`: {what}' for n, what in changes[:MAX_ROWS])
    body = (f'{log_block(logs)}\n\n**{len(changes)} file(s) changed:**\n{rows}\n\nMerging publishes it: within about an hour '
            f'the fingerprints update and phones re-download only these files.\n\nOpened by the daily QUL check in {OWNER}/QUL-Sync.')
    publish(gh, report, repo, f'qul/recitation-{rid}', out, title, body, dry, f'{len(changes)} file(s)')


def language_of(tags, repos):
    known = set(repos) | set(LANGS) | set(LANG_FIX)
    for tag in tags:
        if tag in known:
            return LANG_FIX.get(tag, tag)
    return LANG_FIX.get(tags[-1], tags[-1]) if tags else 'Unknown'


def new_tafseer(gh, qul, report, state, rid, name, tags, repos, dry):
    language = language_of(tags, repos)
    files = qul.download('tafsir', rid)
    qul_json = json.loads(next(v for k, v in files.items() if k.endswith('.json')))
    cov = coverage(qul_json)
    if cov == 0:
        report.add('New tafseers', f'{name} ({language}, QUL id {rid}) — skipped: QUL\'s copy has no text')
        state['ignored_tafsir_ids'].append(int(rid))
        return
    if language == 'Arabic':
        repo = REPOS['arabic']
    elif language in repos and repos[language]['repos']:
        repo = repos[language]['repos'][0]
    else:
        repo = REPOS['additional']
    taken = {t['slug'] for info in repos.values() for t in info['tafsirs'] if t['repo'] == repo}
    taken |= {p['slug'] for p in state['pending_new'].values() if p['repo'] == repo}
    slug = base = slugify(name) or f'qul-{rid}'
    n = 2
    while slug in taken:
        slug, n = f'{base}-{n}', n + 1
    new = tafseer_files(qul_json, 'resolved')
    meta = LANGS.get(language)
    sample_key = next(k for k in qul_json if strip_tags(str(qul_json[k].get('text', '')) if isinstance(qul_json[k], dict) else ''))
    sample = strip_tags(qul_json[sample_key]['text'])[:300]
    is_new_lang = language not in repos
    lang_line = (f'**New language for the app:** {language}' +
                 (f' — code `{meta["code"]}`, native name "{meta["native"]}", {"right-to-left" if meta["rtl"] else "left-to-right"}.'
                  if meta else ' — **not in the language table**: before merging, add its code, native name and direction '
                               'to `sync/lang_table.json` in QUL-Sync.')) if is_new_lang else f'Language: {language} (already in the app).'
    title = f'New tafseer from QUL: {name} ({language})'
    body = (f'QUL published a new tafseer: **{name}** (QUL id {rid}).\n\n{lang_line}\n\n'
            f'- Ayahs with text: **{cov} of 6236**\n- Goes into `{repo}/{slug}/` (30 juz files)\n\n'
            f'Sample ({sample_key}):\n> {sample}\n\nAfter merging, it appears in the app within about an hour for users who '
            f'{"choose " + language if is_new_lang else "have " + language + " selected"}.\n\nOpened by the daily QUL check in {OWNER}/QUL-Sync.')
    url = publish(gh, report, repo, f'qul/new-tafsir-{rid}', {f'{slug}/{j}.json': new[j].encode('utf-8') for j in new},
                  title, body, dry, f'{cov} ayahs with text → `{repo}/{slug}`')
    if url:
        state['pending_new'][str(rid)] = {'qul_id': int(rid), 'name': name, 'language': language,
                                          'repo': repo, 'slug': slug, 'branch': f'qul/new-tafsir-{rid}', 'pr': url}


def check_pending(gh, state):
    """Rejected new-tafseer pull requests are not proposed again."""
    for rid, p in list(state['pending_new'].items()):
        prs = gh.pulls(p['repo'], p['branch'])
        if prs and prs[0]['state'] == 'closed' and not prs[0].get('merged_at'):
            state['ignored_tafsir_ids'].append(int(rid))
            del state['pending_new'][rid]


def test_notice():
    """Run workflow → "Send a test notice": confirms the emails arrive."""
    bot = GitHub(env('GITHUB_TOKEN'))
    repo = os.environ.get('GITHUB_REPOSITORY', f'{OWNER}/QUL-Sync').split('/')[1]
    issue = bot.issue(repo, 'Test notice from QUL-Sync',
                      f'@{CONFIG["notify"]} This is a test. If this reached your inbox, update notices from the daily '
                      f'QUL check will too. You can close this issue.')
    print('Opened', issue['html_url'])


def self_test():
    """Run workflow → "Self-test": proves DATA_TOKEN can do what live mode
    needs, without changing any file on main. Opens a test pull request in
    Tafseer-Additional (then closes it and deletes its branch), and creates
    and deletes a test branch in the index and data repos."""
    gh = GitHub(env('DATA_TOKEN'))
    branch = 'qul/self-test'
    results, ok = [], True

    def step(name, fn):
        nonlocal ok
        try:
            detail = fn()
            results.append(f'✅ {name}' + (f' — {detail}' if detail else ''))
        except Exception as e:
            ok = False
            results.append(f'❌ {name} — {e}')

    repo = REPOS['additional']
    pr = {}

    def open_pr():
        gh.commit_files(repo, branch, {'self-test/README.md': f'QUL-Sync self-test {date.today()}\n'.encode()},
                        'QUL-Sync self-test (closed automatically)')
        p, _ = gh.open_or_update_pr(repo, branch, 'QUL-Sync self-test (closed automatically)',
                                    'Opened by the self-test to prove the access token works. It is closed and its branch deleted at once.')
        pr.update(p)
        files = [f['filename'] for f in gh.get(f'/repos/{OWNER}/{repo}/pulls/{p["number"]}/files')]
        if files != ['self-test/README.md']:
            raise RuntimeError(f'unexpected files in the pull request: {files}')
        return f'#{p["number"]}'

    step(f'{repo}: commit on a branch + open a pull request', open_pr)
    if pr:
        step(f'{repo}: close the test pull request',
             lambda: gh.patch(f'/repos/{OWNER}/{repo}/pulls/{pr["number"]}', {'state': 'closed'}) and None)
    step(f'{repo}: delete the test branch', lambda: gh.delete(f'/repos/{OWNER}/{repo}/git/refs/heads/{branch}'))
    for other in (REPOS['tafseer_index'], REPOS['recitation_index'], REPOS['recitations'], 'Tafseer-French'):
        def branch_roundtrip(other=other):
            gh.post(f'/repos/{OWNER}/{other}/git/refs', {'ref': f'refs/heads/{branch}', 'sha': gh.head(other)})
            gh.delete(f'/repos/{OWNER}/{other}/git/refs/heads/{branch}')
        step(f'{other}: write access (create + delete a test branch)', branch_roundtrip)

    body = ('Self-test **passed** — live mode can open pull requests and update the indexes.' if ok
            else 'Self-test **failed** — see below. Check that DATA_TOKEN includes these repos with Contents and Pull requests: read and write.')
    body += '\n\n' + '\n'.join(f'- {r}' for r in results) + '\n\nNo file on main was changed.'
    bot = GitHub(env('GITHUB_TOKEN'))
    me = os.environ.get('GITHUB_REPOSITORY', f'{OWNER}/QUL-Sync').split('/')[1]
    bot.issue(me, f'Self-test {"passed" if ok else "FAILED"}', f'@{CONFIG["notify"]} {body}')
    print(body)
    if not ok:
        sys.exit(1)


def main():
    if '--test-notice' in sys.argv:
        return test_notice()
    if '--self-test' in sys.argv:
        return self_test()
    dry = CONFIG['dry_run'] or '--dry-run' in sys.argv
    local = '--local' in sys.argv
    state = load('state.json', {}) or {}
    state.setdefault('last_change_log', 0)
    state.setdefault('ignored_tafsir_ids', list(CONFIG['excluded_tafseer_ids']))
    state.setdefault('pending_new', {})
    report = Report(dry)
    gh = GitHub(env('DATA_TOKEN'))
    repos = load('repos.json')
    by_id = {t['qul_id']: {**t, 'language': lang} for lang, info in repos.items() for t in info['tafsirs']}
    reciters = CONFIG['reciters']
    newest = state['last_change_log']
    failed_ids = []
    try:
        qul = Qul(env('QUL_TOKEN'))
        logs = qul.change_logs(state['last_change_log'])
        newest = max([l['id'] for l in logs] + [newest])
        groups = {}
        for l in logs:  # newest first; one download per resource
            groups.setdefault((l['kind'], l['resource_id']), []).append(l)
        for (kind, rid), group in groups.items():
            try:
                if f'{kind}/{rid}' in CONFIG['bundled_in_app']:
                    report.add('Built into the app (needs an app update)',
                               f'{CONFIG["bundled_in_app"][f"{kind}/{rid}"]}: **{group[0]["title"]}** — '
                               f'[change log #{group[0]["id"]}](https://qul.tarteel.ai/change_logs/{group[0]["id"]})')
                if kind == 'tafsir' and rid.isdigit() and int(rid) in by_id:
                    tafseer_update(gh, qul, report, by_id[int(rid)], group, dry)
                elif kind == 'recitation' and rid in reciters:
                    recitation_update(gh, qul, report, rid, reciters[rid], group, dry)
            except QulTokenExpired:
                raise
            except Exception as e:
                failed_ids += [l['id'] for l in group]
                report.errors.append(f'{kind}/{rid} (change log #{group[0]["id"]}): {e}')
                traceback.print_exc()

        listing = qul.listing('tafsir')
        known = set(by_id) | set(state['ignored_tafsir_ids']) | {int(k) for k in state['pending_new']}
        for rid, item in sorted(listing.items(), key=lambda x: int(x[0])):
            if int(rid) in known:
                continue
            name = item['name']
            try:
                new_tafseer(gh, qul, report, state, rid, name, item['tags'], repos, dry)
            except QulTokenExpired:
                raise
            except Exception as e:
                report.errors.append(f'new tafseer {name} (QUL id {rid}): {e}')
                traceback.print_exc()

        rec_listing = qul.listing('recitation')
        if state.get('seen_recitation_ids') is None:
            state['seen_recitation_ids'] = sorted(map(int, rec_listing))  # first run: baseline, no report
        else:
            for rid, item in rec_listing.items():
                if int(rid) not in state['seen_recitation_ids']:
                    report.add('New reciters on QUL (not added)', f'[{item["name"]}](https://qul.tarteel.ai/resources/recitation/{rid}) (id {rid})')
                    state['seen_recitation_ids'].append(int(rid))
        if not dry:
            check_pending(gh, state)
    except QulTokenExpired as e:
        report.errors.append(f'**{e}** Sign in at qul.tarteel.ai ("Remember me" ticked), copy the `remember_user_token` '
                             f'cookie value and paste it into this repo\'s secret **QUL_TOKEN** (Settings → Secrets and variables → Actions).')
        newest = state['last_change_log']
    except Exception as e:
        report.errors.append(f'The check stopped: {e}')
        traceback.print_exc()
        newest = state['last_change_log']

    # Retry failed entries next time: don't move past the oldest failure.
    if failed_ids:
        newest = min(newest, min(failed_ids) - 1)
    state['last_change_log'] = max(state['last_change_log'], newest)
    state['ignored_tafsir_ids'] = sorted(set(state['ignored_tafsir_ids']))
    # GitHub switches off scheduled jobs in a public repo after 60 days
    # without a commit; this changes (and gets committed) once a month.
    state['heartbeat'] = date.today().strftime('%Y-%m')

    title = f'{"[Dry run] " if dry else ""}QUL check {date.today()}'
    body = report.markdown() or 'Nothing new.'
    if local:
        print(f'# {title}\n\n{body}')
    else:
        save('state.json', state)
        if report.worth_an_issue():
            bot = GitHub(env('GITHUB_TOKEN'))
            repo = os.environ.get('GITHUB_REPOSITORY', f'{OWNER}/QUL-Sync').split('/')[1]
            note = 'Dry run — nothing was pushed or opened.\n\n' if dry else ''
            bot.issue(repo, title, f'@{CONFIG["notify"]} {note}{body}')
        print(body)
    if report.errors:
        sys.exit(1)


if __name__ == '__main__':
    main()
