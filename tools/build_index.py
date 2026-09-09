#!/usr/bin/env python3
import datetime as dt
import html
import json
import pathlib

DOCS = pathlib.Path("docs")
DOCS.mkdir(exist_ok=True)


def collect_rows():
    for league_dir in sorted(DOCS.glob("league_*")):
        if not league_dir.is_dir():
            continue

        parts = league_dir.name.split("_")
        if len(parts) != 2 or parts[0] != "league":
            continue

        anchor_id = parts[1]
        current_id = anchor_id
        name = f"League {anchor_id}"
        generated = ""
        season = ""

        state_p = league_dir / "state.json"
        if state_p.exists():
            try:
                data = json.loads(state_p.read_text("utf-8"))
                current_id = str(
                    data.get("league", {}).get("league_id") or anchor_id
                )
                name = data.get("league", {}).get("name", name)
                generated = data.get("generated_at", "")
                season = str(data.get("season") or "")
            except Exception:
                pass

        yield name, anchor_id, current_id, season, generated


def main():
    out = []
    out.append(
        '<!doctype html><meta charset="utf-8"><title>SleeperAgent export</title>'
    )
    out.append("<h1>SleeperAgent export</h1>")
    out.append(
        '<p style="font:12px/1.2 monospace">built_at: {}</p>'.format(
            dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    )

    for name, anchor_id, current_id, season, generated in collect_rows():
        base_fs = DOCS / f"league_{anchor_id}"
        base_href = f"league_{anchor_id}/"

        def have(fname: str) -> bool:
            return (base_fs / fname).exists()

        links = [
            f'<a href="{base_href}state.json">state.json</a>',
            f'<a href="league_state_{anchor_id}.html">HTML mirror</a>',
        ]
        for fname, label in [
            ("available_players.html", "Available players (searchable)"),
            ("available_players.json", "available players JSON"),
            ("available_players.csv", "available players CSV"),
            ("available_players_all.json", "all unrostered catalog entries"),
            ("teams.json", "teams"),
            ("schedule.json", "schedule"),
            ("transactions.json", "transactions"),
            ("players_min.json", "players_min"),
            ("manifest.json", "manifest"),
            ("diff.json", "diff"),
        ]:
            if have(fname):
                links.append(f'<a href="{base_href}{fname}">{label}</a>')

        details = []
        if season:
            details.append(f"season {html.escape(season)}")
        if current_id != anchor_id:
            details.append(f"current Sleeper ID {html.escape(current_id)}")
            details.append(f"stable anchor {html.escape(anchor_id)}")
        else:
            details.append(f"ID {html.escape(anchor_id)}")
        if generated:
            details.append(f"generated_at: {html.escape(generated)}")
        if have("available_players.json"):
            try:
                available = json.loads((base_fs / "available_players.json").read_text("utf-8"))
                status = "unavailable" if not available.get("valid") else "provisional" if available.get("is_provisional") else "unrostered snapshot"
                details.append(f"{int(available.get('player_count', 0))} candidates ({status})")
            except (OSError, ValueError, TypeError):
                details.append("availability metadata unreadable")

        out.append(
            "  <div>- {} ({}) &mdash; {}</div>".format(
                html.escape(name),
                "; ".join(details),
                " | ".join(links),
            )
        )

    (DOCS / "index.html").write_text("\n".join(out), encoding="utf-8")


if __name__ == "__main__":
    main()
