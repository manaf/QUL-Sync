"""QUL downloads → the exact file formats in our data repos, and the
ayah-by-ayah comparison used for the pull request reports.

Formats (all verified byte-identical against the repos on 2026-11-03):
  tafseer, "resolved": QUL's ayah-group pointers replaced by the text
      (every repo filled by pull_qul.py)
  tafseer, "pointers": QUL's pointers kept, except a pointer into another
      juz gets the text — the app follows pointers within one juz file
      (the 4 tafseers imported by hand into Tahfeez-Quran-Tafseer)
  recitation, ayah-by-ayah: one file per surah, "s:a" → {audio_url,
      segments[, duration]}
  recitation, surah-by-surah: audio.json (= QUL surah.json) + segments.json
"""
import difflib
import json
import re

from common import ROOT, compact, strip_tags

_JUZ = json.loads((ROOT / 'juz_index.json').read_text())
POINTER = re.compile(r'^\d+:\d+$')


def juz_of(key):
    try:
        s, a = key.split(':')
        return _JUZ[s][int(a) - 1]
    except (KeyError, ValueError, IndexError):
        return None


# ─── tafseer ──────────────────────────────────────────────────────────

def _resolve(qul, key):
    v = qul.get(key)
    for _ in range(4):  # the value, then up to 3 pointer hops (as pull_qul.py)
        if isinstance(v, dict):
            return v.get('text', '') or ''
        if isinstance(v, str):
            v = qul.get(v)
            continue
        return ''
    return ''


def tafseer_files(qul_json: dict, fmt: str) -> dict:
    """{juz number: compact JSON text} for all 30 juz."""
    by_juz = {j: {} for j in range(1, 31)}
    for key, v in qul_json.items():
        j = juz_of(key)
        if j is None:
            continue
        if fmt == 'pointers' and isinstance(v, str):
            target = qul_json.get(v)
            keep = juz_of(v) == j and isinstance(target, dict) and target.get('text')
            by_juz[j][key] = v if keep else _resolve(qul_json, key)
        elif fmt == 'pointers' and isinstance(v, dict):
            by_juz[j][key] = v.get('text', '') or ''
        else:
            by_juz[j][key] = _resolve(qul_json, key)
    return {j: compact(d) for j, d in by_juz.items()}


def coverage(qul_json: dict) -> int:
    """Ayahs with author text (0 = an empty tafseer like QUL id 41)."""
    return sum(1 for k in qul_json if _resolve(qul_json, k))


def resolved_texts(juz_file: dict) -> dict:
    """"s:a" → text, following one in-file pointer hop (as the app does)."""
    out = {}
    for k, v in juz_file.items():
        if isinstance(v, str) and POINTER.match(v):
            t = juz_file.get(v)
            out[k] = t if isinstance(t, str) and not POINTER.match(t) else ''
        else:
            out[k] = v if isinstance(v, str) else ''
    return out


# ─── recitations ──────────────────────────────────────────────────────

def ayah_recitation_files(qul_json: dict) -> dict:
    """{"<surah>.json": compact text} for all 114 surahs."""
    by = {s: {} for s in range(1, 115)}
    for key, e in qul_json.items():
        if not isinstance(e, dict):
            continue
        try:
            s = int(key.split(':')[0])
        except ValueError:
            continue
        c = {'audio_url': e.get('audio_url', ''), 'segments': e.get('segments', [])}
        if e.get('duration') is not None:
            c['duration'] = e['duration']
        by.setdefault(s, {})[key] = c
    return {f'{s}.json': compact(d) for s, d in by.items()}


def surah_recitation_files(files: dict) -> dict:
    return {
        'audio.json': compact(json.loads(files['surah.json'])),
        'segments.json': compact(json.loads(files['segments.json'])),
    }


# ─── comparison for reports ───────────────────────────────────────────

def word_changes(old: str, new: str, ctx=6, limit=3):
    """A few "…before [-old-] [+new+] after…" snippets, tags stripped."""
    a, b = strip_tags(old).split(' '), strip_tags(new).split(' ')
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == 'equal':
            continue
        pre, post = ' '.join(a[max(0, i1 - ctx):i1]), ' '.join(a[i2:i2 + ctx])
        out.append(f'…{pre} ~~{" ".join(a[i1:i2])}~~ **{" ".join(b[j1:j2])}** {post}…'.replace('~~~~', '').replace('****', ''))
        if len(out) >= limit:
            break
    return out


def compare_tafseer(old_files: dict, new_files: dict) -> list:
    """Changed ayahs by text (format differences ignored):
    [(key, old text, new text)] in Quran order."""
    changed = []
    for j in range(1, 31):
        old = resolved_texts(json.loads(old_files.get(j) or '{}'))
        new = resolved_texts(json.loads(new_files[j]))
        for k in sorted(set(old) | set(new), key=lambda k: tuple(map(int, k.split(':')))):
            if old.get(k, '') != new.get(k, ''):
                changed.append((k, old.get(k, ''), new.get(k, '')))
    return changed


def compare_recitation(old_files: dict, new_files: dict) -> list:
    """[(file, what changed)] for files whose content differs."""
    out = []
    for name in sorted(new_files, key=lambda n: (not n[0].isdigit(), int(n.split('.')[0]) if n[0].isdigit() else 0, n)):
        if (old_files.get(name) or '') == new_files[name]:
            continue
        old = json.loads(old_files.get(name) or '{}')
        new = json.loads(new_files[name])
        keys = [k for k in set(old) | set(new) if old.get(k) != new.get(k)]
        timing = sum(1 for k in keys if (old.get(k) or {}).get('segments') != (new.get(k) or {}).get('segments'))
        audio = sum(1 for k in keys if (old.get(k) or {}).get('audio_url') != (new.get(k) or {}).get('audio_url'))
        out.append((name, f'{len(keys)} entries changed ({timing} timings, {audio} audio links)'))
    return out
