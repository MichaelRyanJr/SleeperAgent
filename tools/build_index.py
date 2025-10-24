#!/usr/bin/env python3
import json, html, pathlib

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

def exists(path: pathlib.Path) -> bool:
    return path.exists()

out = []
out.append('<!doctype html><meta charset="utf-8"><title>SleeperAgent export</title>')
out.append('<h1>SleeperAgent export</h1>')
for name, lid, gen in rows:
    base = pathlib.Path(f"league_{lid}")
    links = []
    for fname, label in [
        ("state.json", "state.json"),
        ("teams.json", "teams"),
        ("schedule.json", "schedule"),
        ("transactions.json", "transactions"),
        ("players_min.json", "players_min"),
        ("manifest.json", "manifest"),
        ("diff.json", "diff"),
    ]:
        if exists(base / fname):
            links.append(f'<a href="{base}/{fname}">{label}</a>')
    gen_str = f" — generated_at: {html.escape(gen)}" if gen else ""
    out.append(f'  <div>• {html.escape(name)} (ID {lid}) — ' + " | ".join(links) + gen_str + "</div>")

(DOCS / "index.html").write_text("\n".join(out), encoding="utf-8")
