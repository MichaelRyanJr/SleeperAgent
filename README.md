# SleeperAgent

Exports current Sleeper league data to GitHub Pages while retaining stable league URLs across season renewals. The scheduled `Sleeper sync` workflow resolves the current leagues, exports them, and rebuilds the hub.

## Available players

Each league export now includes:

- `available_players.html`: searchable, position-filterable HTML table. Rows are server-rendered, so readers without JavaScript still receive the entire default list.
- `available_players.json`: timestamped default candidate list with source URLs/timestamps, counts, ownership exclusions, league status, and limitations.
- `available_players.csv`: the same default players with snapshot time and league ID on every row.
- `available_players_all.json`: every league-eligible unrostered catalog entry, including inactive/retired/unsigned entries hidden from the default list. This is an audit/search universe, NOT a recommendation list.

Use the hub's **Available players (searchable)** link. Stable paths are `docs/league_1181689020258160640/available_players.json` (keeper league) and `docs/league_1265837618587762688/available_players.json` (redraft league), with matching HTML/CSV paths. Existing publishing automatically includes these files in the per-league manifests and diffs.

### Meaning and safeguards

Availability is computed from the full NFL player catalog minus ALL fantasy roster holdings (`players`, `starters`, `reserve`, and `taxi`). Player IDs are normalized; empty/zero lineup placeholders are ignored. Position eligibility follows the league's lineup slots, including flex and IDP slots.

During pre-draft or an unsettled draft, current-season assignments are also excluded, including manually assigned picks without an `is_keeper` flag. Such snapshots are **provisional**: carried-over rosters can still hide players that will be released after the draft. Do not treat them as the final post-draft waiver wire. Once the league/drafts have settled, current rosters become authoritative; a player drafted and later dropped is not excluded forever by draft history.

`unrostered_at_snapshot` does not mean an immediate free-agent add. Per-player waiver locks, exact claim deadlines, and private pending claims are not available from the documented public API. `waiver_status` stays `unknown` and `claim_deadline` stays null. `can_add_now` is false when league adds are disabled and otherwise null, never guessed true. Use Sleeper to submit or confirm an acquisition.

The default view hides catalog entries marked retired/inactive and unaffiliated entries without active metadata. Injured NFL players and active unsigned players are retained. The all view prevents hidden entries from being lost. No ADP, fantasy projection, or ranking is invented; rows are alphabetical within position.

### Freshness

Rosters/drafts are fetched every normal sync. The snapshot records individual source fetch times and a one-hour `stale_after` advisory; the HTML warns when that threshold passes. A scheduled workflow is not a real-time feed, especially during a fast draft. API requests are sequential, not an atomic league-wide transaction.

The full catalog's trimmed public metadata is cached in `docs/_cache/nfl_players_catalog.json` for 24 hours and shared across leagues and workflow runs. `sources.players.fetched_at` is the original fetch timestamp even on a cache hit; injury/depth-chart metadata can therefore be older than ownership. This follows Sleeper's guidance to limit full-catalog downloads. For current injury decisions, consult current team reports separately.

Malformed/partial roster responses and catalog download failures fail the sync instead of publishing a falsely authoritative pool. `--skip-players` writes an explicitly invalid availability snapshot, not a claim that zero free agents exist. Empty draft-pick responses overwrite old files so old picks cannot leak into new snapshots.

Player `current_season_draft_round` is context from the current draft, NOT an automatically assigned keeper cost for a future season.

## Tests

Run from the repository root:

```sh
python -m unittest discover -s tests -v
```

The tests use mocked API responses and require no network or additional dependencies. A separate read-only GitHub Actions workflow runs the suite on code changes and pull requests.

API source: https://docs.sleeper.com/
