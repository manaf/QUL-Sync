# QUL-Sync

Keeps the Tahfeez Al Quran app's data in step with the
[Quranic Universal Library (QUL)](https://qul.tarteel.ai): when QUL corrects
or adds a tafseer, or fixes a reciter's timings, a pull request with an
ayah-by-ayah report appears in the matching data repo. Nothing reaches
phones until the owner merges it.

## The two jobs

| Job | When | What it does |
|---|---|---|
| **QUL check** (`sync/check.py`) | twice a day, 06:07 and 18:07 Gulf time (02:07 / 14:07 UTC) | Reads QUL's [change log](https://qul.tarteel.ai/change_logs) down to the last entry it saw. For each change to a tafseer or reciter the app uses: downloads it, converts it to our file format and compares it **by text** with our copy. Real differences → a pull request in that data repo (an open one is updated). New QUL tafseers → a pull request (a new language goes into `Tafseer-Additional`; empty ones are skipped). New reciters and changes to resources built into the app (fonts, page layout, word data, the bundled Jalalayn) are reported only. Opens one summary issue here, which emails the owner. |
| **Fingerprint sync** (`sync/fingerprints.py`) | hourly | Keeps the per-file fingerprints in `Tafseer-Index/languages.json` (`juz_fp`) and `Recitation-Index/reciters.json` (`files`) in step with the data repos — whatever changed them — and clears the jsDelivr cache. Phones re-download only files whose fingerprint changed. Also publishes merged new tafseers. |

Both can be started by hand: **Actions → (job) → Run workflow**.

GitHub fires only some scheduled runs in this repo (measured Nov 2026: about 1 in 7). The fingerprint sync therefore runs a **watchdog**: if no QUL check has completed for 7 days (`last_checked_at` in `sync/state.json`), it opens an issue — which emails you — at most once a week.

## Reviewing an update

1. GitHub emails you about the summary issue here ("QUL check …").
2. Open the pull request linked in it. It shows what changed per ayah
   (~~removed~~ **added**) and links QUL's change log entry.
3. **Merge** to publish (phones get it within about an hour, next time the
   app opens), or **Close** to reject. A rejected new tafseer is not
   proposed again.

## Secrets (Settings → Secrets and variables → Actions)

| Secret | What | Renew |
|---|---|---|
| `QUL_TOKEN` | Value of the `remember_user_token` cookie after signing in at qul.tarteel.ai with "Remember me" ticked (DevTools → Application → Cookies). | About monthly — the QUL check fails with "QUL login token has expired" and GitHub emails you. |
| `DATA_TOKEN` | Fine-grained personal access token with **only** the data repos (`Tafseer-*`, `Tahfeez-Quran-Tafseer`, `Recitations`, `Recitation-Index`, `Tafseer-Additional`) and **Contents: read and write**, **Pull requests: read and write**. No admin rights, no other repos. | Before it expires (GitHub emails a week ahead): Settings → Developer settings → Fine-grained tokens → Regenerate, then paste the new value here. |

## Dry run → live

`sync/config.json` → `"dry_run": true` makes both jobs report what they
would do without pushing or opening anything. To go live: set it to
`false`, and set `last_change_log` in `sync/state.json` to `0` so the first
live run re-checks every change it saw during the dry run.

## Files

| File | |
|---|---|
| `sync/config.json` | dry run switch, repo names, the 8 reciters (QUL id → app slug), resources built into the app |
| `sync/repos.json` | every tafseer the app offers: QUL id → repo + folder (new ones are added on merge) |
| `sync/state.json` | last change log entry seen, rejected/empty tafseer ids, pending new tafseers, reciters already seen |
| `sync/lang_table.json` | language name → code, native name, direction (for new languages) |
| `sync/juz_index.json` | juz of every ayah (splits tafseers into 30 files) |
| `sync/cache/*.json` | blob id → fingerprint per data file, so unchanged files are never downloaded again |

## File formats (must match the data repos byte for byte)

- Tafseer, most repos: QUL's ayah-group pointers replaced by the text.
- Tafseer, `Tahfeez-Quran-Tafseer` (imported by hand): QUL's pointers
  kept, except a pointer into another juz gets the text — the app only
  follows pointers inside one juz file.
- Ayah-by-ayah reciters: one file per surah, `"s:a"` → `{audio_url, segments[, duration]}`.
- Surah-by-surah reciters: `audio.json` (QUL's `surah.json`) + `segments.json`.

All verified identical to the repos on 2026-11-03.

## Running locally

```bash
cd sync
QUL_TOKEN=... DATA_TOKEN=$(gh auth token) python3 check.py --local --dry-run
DATA_TOKEN=$(gh auth token) python3 fingerprints.py --local --dry-run
```

`--local` prints the report instead of opening an issue and doesn't save
state. Standard library only (Python 3.9+).
