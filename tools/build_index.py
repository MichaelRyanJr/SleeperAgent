#!/usr/bin/env python3
import json, html, pathlib, datetime as dt

DOCS = pathlib.Path("docs")
DOCS.mkdir(exist_ok=True)

def collect_rows():
    for league_dir in sorted(DOCS.glob("league_*")):
        if not league_dir.is_dir():
            continue
        parts = league_dir.name.split("_")
        if len(parts) != 2 or parts[0] != "league":
            continue
        lid = parts[1]
        name = "League {}".format(lid)
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
    out.append('<p style="font:12px/1.2 monospace">built_at: {}</p>'.format(
        dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    ))

    for name, lid, gen in collect_rows():
        base_fs = DOCS / "league_{}".format(lid)     # filesystem check
        base_href = "league_{}/".format(lid)         # link shown on page

        def have(fname: str) -> bool:
            return (base_fs / fname).exists()

        links = []
        # Always show state + HTML mirror
        links.append('<a href="{}state.json">state.json</a>'.format(base_href))
        links.append('<a href="league_state_{}.html">HTML mirror</a>'.format(lid))
        # Only show these if the file exists to avoid dead links
        for fname, label in [
            ("teams.json","teams"),
            ("schedule.json","schedule"),
            ("transactions.json","transactions"),
            ("players_min.json","players_min"),
            ("manifest.json","manifest"),
            ("diff.json","diff"),
        ]:
            if have(fname):
                links.append('<a href="{}{}">{}</a>'.format(base_href, fname, label))

        gen_str = ' &mdash; generated_at: {}'.format(html.escape(gen)) if gen else ''
        out.append('  <div>- {} (ID {}) &mdash; '.format(html.escape(name), lid)
                   + ' | '.join(links) + gen_str + '</div>')

    (DOCS / "index.html").write_text("\n".join(out), encoding="utf-8")

if __name__ == "__main__":
    main()
