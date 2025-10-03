name: Sleeper sync
on:
  schedule:
    - cron: "*/30 * * * *"  # every 30 min (UTC). Adjust as you like.
  workflow_dispatch: {}
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Export league snapshot
        run: |
          python sleeper_sync.py --league 1265837618587762688 --season 2025 --out ./docs
          cp ./docs/league_1265837618587762688_2025/league_state.json ./docs/league_state.json
      - name: Commit & push
        run: |
          git config user.name "github-actions"
          git config user.email "actions@github.com"
          git add -A
          git commit -m "update league data" || true
          git push
