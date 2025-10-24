#!/usr/bin/env python3
import json, html, pathlib, datetime as dt

DOCS = pathlib.Path("docs")
DOCS.mkdir(exist_ok=True)

rows = []
for league_dir in sorted(DOCS.glob("league_*")):
    if not league_dir.is_dir():
        continue
    parts = league_dir.name.split("_")
    if len(parts) != 2 or parts[0] != "league":
        continue

    lid = parts[1]
    state_p = league_dir / "state.json"
    name = f"League {lid}"
    gen = ""

    if state_p.exists():
        try:
            data = json.loads(state_p.read_text("utf-8"))
            lid = str(data.get("league", {}).get("league_id") or lid)
            name = data.get("league", {}).get("name", name)
            gen = data.get("generated_at", "")
        except Exception:
            pass

    rows.append((name, lid, gen))

out = []
out.append('<!doctype html><meta charset="utf-8"><title>SleeperAgent export</title>')
out.append('<h1>SleeperAgent export</h1>')
out.append(f'<p style="font:12px/1.2 monospace">built_at: {dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}</p>')

# Unconditionally render the standard link set so they always appear.
STANDARD = [
    ("state.json", "state.json"),
    ("teams.json", "teams"),
    ("schedule.json", "schedule"),
    ("transactions.json", "transactions"),
    ("players_min.json", "players_min"),
    ("manifest.json", "manifest"),
    ("diff.json", "diff"),
]

for name, lid, gen in rows:
    href_base = f"league_{lid}/"
    links = [f'<a href="{href_base}{fname}">{label}</a>' for fname, label in STANDARD]
    gen_str = f" — generated_at: {html.escape(gen)}" if gen else ""
    out.append(f'  <div>• {html.escape(name)} (ID {lid}) — ' + " | ".join(links) + gen_str + "</div>")

(DOCS / "index.html").write_text("\n".join(out), encoding="utf-8")
