#!/usr/bin/env python3
import json, html, pathlib, datetime as dt

DOCS = pathlib.Path("docs")
DOCS.mkdir(exist_ok=True)

def iter_leagues():
    for league_dir in sorted(DOCS.glob("league_*")):
        if not league_dir.is_dir():
            continue
        parts = league_dir.name.split("_")
        if len(parts) != 2 or parts[0] != "league":
            continue
        lid = parts[1]
        name = f"League {lid}"
        gen = ""
        state_p = league_dir / "state.json"
        if state_p.exists():
            try:
                data = json.loads(state_p.read_text("utf-8"))
                lid = str(data.get("league", {}).get("league_id") or lid)
                name = data.get("league", {}).get("name", name)
                gen = data.get("generated_at", "")
            except Exception:
                pass
        yield name, lid, gen

def main():
    out = []
    out.append('<!doctype html><meta charset="utf-8"><title>SleeperAgent export</title>')
    out.append('<h1>SleeperAgent export</h1>')
    out.append(f'<p style="font:12px/1.2 monospace">built_at: {dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}</p>')

    for name, lid, gen in iter_leagues():
        base = f"league_{lid}/"
        links = [
            f'<a href="{base}state.json">state.json</a>',
            f'<a href="league_state_{lid}.html">HTML mirror</a>',
            f'<a href="{base}teams.json">teams</a>',
            f'<a href="{base}schedule.json">schedule</a>',
            f'<a href="{base}transactions.json">transactions</a>',
            f'<a href="{base}players_min.json">players_min</a>',
            f'<a href="{base}manifest.json">manifest</a>',
            f'<a href="{base}diff.json">diff</a>',
        ]
        gen_str = f" — generated_at: {html.escape(gen)}" if gen else ""
        out.append(f'  <div>• {html.escape(name)} (ID {lid}) — ' + " | ".join(links) + gen_str + "</div>')

    (DOCS / "index.html").write_text("\n".join(out), encoding="utf-8")

if __name__ == "__main__":
    main()
