# Player nickname privacy

Custom player nicknames are excluded from public SleeperAgent exports. The API readers in `sleeper_sync.py`, `tools/enrich_keeper_data.py`, and `tools/cache_draft_history.py` remove nickname fields while decoding responses, before raw roster data is written. Real player names, roster ownership, scoring, transactions, keeper costs, and non-nickname metadata remain intact. This does not edit anything in Sleeper.

Every scheduled sync additionally scrubs the entire `docs/` tree (including saved historical snapshots and supported archives), then verifies it before committing or uploading a Pages artifact. An unexpected nickname representation fails publication rather than being silently retained. Logs report only counts/status, never removed values. The five-minute schedule is unchanged.

Offline validation:

```sh
python -m unittest discover -s tests -v
python tools/scrub_nicknames.py --root docs --check
```

To sanitize a separately generated export before sharing it:

```sh
python tools/scrub_nicknames.py --root /path/to/export
```

The one-time cleanup also sanitized the current tips of the existing feature/fix branches and removed old `github-pages` workflow artifacts. Its receipt is `docs/nickname_privacy_cleanup.json`. The migration tools do not force-push or delete branches.

## Important: old Git history is separate

Cleaning the current files does **not** erase earlier Git commits, pull-request references, cached commit views, forks, or previously downloaded copies. No Git history was rewritten by this cleanup. A history purge requires a separately coordinated rewrite and force-push, which changes commit IDs and can affect local clones and pull requests. Even a rewrite cannot recall someone else's downloaded copy. Do not describe this repository as fully purged of all historical copies until that separate work has been addressed.

GitHub's guidance: https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository
