from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from sopranos.config import ROSTER_PATH
from sopranos.db.connection import connect, init_db
from sopranos.utils.episode_paths import find_episode, list_season_episodes
from sopranos.utils.timestamps import seconds_to_hhmmss

# Mirrors sopranos.pipeline.orchestrator.STAGE_ORDER. Kept here as a plain literal
# so the CLI (query/serve/stats/roster) doesn't import the heavy ingest pipeline.
STAGE_ORDER = ["probe", "audio", "asr", "shots", "scenes", "keyframes", "label", "index"]

app = typer.Typer(help="The Sopranos scene-indexed query CLI")
roster_app = typer.Typer(help="Cast roster management")
app.add_typer(roster_app, name="roster")
console = Console()


@app.callback()
def _root() -> None:
    init_db()


@app.command()
def ingest(
    season: int | None = typer.Option(None, "--season", "-s", help="Process entire season"),
    episode: str | None = typer.Option(None, "--episode", "-e", help="Process single episode like S01E03"),
    force_from: str | None = typer.Option(None, "--force-from",
                                          help=f"Re-run from this stage onward. One of: {STAGE_ORDER}"),
) -> None:
    """Run the full pipeline for a season or one episode."""
    if force_from is not None and force_from not in STAGE_ORDER:
        console.print(f"[red]--force-from must be one of {STAGE_ORDER}[/red]")
        raise typer.Exit(2)

    from sopranos.pipeline.orchestrator import run_episode

    if episode:
        ref = find_episode(episode)
        run_episode(ref, force_from=force_from)
        return
    if season is None:
        console.print("[red]Specify --season N or --episode SxxEyy[/red]")
        raise typer.Exit(2)
    refs = list_season_episodes(season)
    console.print(f"Processing season {season}: {len(refs)} episodes")
    for ref in refs:
        try:
            run_episode(ref, force_from=force_from)
        except Exception as e:
            console.print(f"[red][{ref.code}] failed: {e}[/red]")


@app.command()
def query(
    text: str = typer.Argument(..., help="Search query (keywords + character/location/etc.)"),
    top: int = typer.Option(10, "--top", "-n", help="Number of results"),
    play: int | None = typer.Option(None, "--play",
                                    help="1-based rank of result to launch in ffplay"),
    debug: bool = typer.Option(False, "--debug", help="Print parsed filter"),
) -> None:
    """Search indexed scenes: FTS5 keyword + rule-based facet matching. No LLM, no tokens."""
    from sopranos.query.parse_local import parse_query_local
    from sopranos.query.search import search
    from sopranos.query.play import play as play_hit

    qf, _ = parse_query_local(text)
    if debug:
        console.print_json(qf.model_dump_json(indent=2))

    hits, relaxed = search(qf, top_k=top)
    if not hits:
        console.print("[yellow]No matching scenes.[/yellow]")
        return
    if relaxed:
        console.print(f"[yellow]Relaxed filters to find results: {', '.join(relaxed)}[/yellow]")

    table = Table(title=f"Results for: {text}")
    table.add_column("#"); table.add_column("Episode"); table.add_column("Time")
    table.add_column("Loc"); table.add_column("Chars"); table.add_column("Score"); table.add_column("Summary")
    for i, h in enumerate(hits, 1):
        table.add_row(
            str(i),
            f"S{h.season:02d}E{h.episode:02d} {h.title}",
            seconds_to_hhmmss(h.start_s),
            (h.location_name or "?")[:30],
            ", ".join(h.characters[:3]) + (f" +{len(h.characters)-3}" if len(h.characters) > 3 else ""),
            f"{h.similarity:.2f}",
            (h.summary or "")[:80],
        )
    console.print(table)

    if play is not None:
        if play < 1 or play > len(hits):
            console.print(f"[red]--play {play} out of range (1..{len(hits)})[/red]")
            raise typer.Exit(2)
        play_hit(hits[play - 1])


@app.command()
def serve(
    directory: str = typer.Option("dist", "--dir", help="Static bundle directory to serve"),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host"),
    port: int = typer.Option(8000, "--port", "-p", help="Bind port"),
) -> None:
    """Preview the built static site locally. Range-capable, like a real static host.

    Build it first with: python scripts/build_static_site.py
    """
    from sopranos.web.range_server import make_server

    root = Path(directory).resolve()
    if not root.is_dir():
        console.print(f"[red]{root} not found — run `python scripts/build_static_site.py` first.[/red]")
        raise typer.Exit(2)
    console.print(f"[green]Serving {root}[/green] → http://{host}:{port}  (Ctrl-C to stop)")
    with make_server(root, host, port) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            console.print("\nStopped.")


@app.command()
def stats() -> None:
    """Report ingest progress and API spend."""
    with connect() as conn:
        ep_count = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        sc_count = conn.execute("SELECT COUNT(*) FROM scenes").fetchone()[0]
        char_count = conn.execute("SELECT COUNT(DISTINCT character_name) FROM scene_characters").fetchone()[0]
        usage_rows = conn.execute(
            "SELECT kind, SUM(input_tokens), SUM(cache_creation_tokens), "
            "SUM(cache_read_tokens), SUM(output_tokens) FROM api_usage GROUP BY kind"
        ).fetchall()

    console.print(f"Episodes processed: {ep_count}")
    console.print(f"Scenes indexed:    {sc_count}")
    console.print(f"Distinct characters: {char_count}")
    console.print()
    console.print("API usage:")
    for kind, inp, cwrite, cread, out in usage_rows:
        # Haiku 4.5 pricing per million tokens: input $1, cache_write $1.25, cache_read $0.10, output $5.
        # Anthropic reports input_tokens, cache_creation, cache_read as mutually exclusive.
        cost = inp * 1e-6 * 1.0 + cwrite * 1e-6 * 1.25 + cread * 1e-6 * 0.10 + out * 1e-6 * 5.0
        console.print(f"  {kind}: input={inp}, cache_write={cwrite}, cache_read={cread}, output={out}  -> est ${cost:.2f}")


@app.command()
def qa(
    action: str = typer.Argument(..., help="'sample' to pull random scenes for review"),
    n: int = typer.Option(20, "--n", help="Number of scenes to sample"),
) -> None:
    """QA helpers."""
    if action != "sample":
        console.print(f"Unknown qa action: {action}")
        raise typer.Exit(2)
    with connect() as conn:
        rows = conn.execute(
            "SELECT s.id, s.scene_index, s.start_s, s.end_s, s.summary, s.location_name, "
            "e.season, e.episode, e.file_path "
            "FROM scenes s JOIN episodes e ON e.id = s.episode_id ORDER BY RANDOM() LIMIT ?",
            (n,),
        ).fetchall()
        for r in rows:
            chars = [row["character_name"] for row in conn.execute(
                "SELECT character_name FROM scene_characters WHERE scene_id = ?",
                (r["id"],),
            ).fetchall()]
            print(f"S{r['season']:02d}E{r['episode']:02d} scene {r['scene_index']} "
                  f"{seconds_to_hhmmss(r['start_s'])}-{seconds_to_hhmmss(r['end_s'])}")
            print(f"  loc: {r['location_name']}")
            print(f"  chars: {', '.join(chars)}")
            print(f"  summary: {r['summary']}")
            print(f"  ffplay -ss {r['start_s']:.2f} -t {r['end_s']-r['start_s']:.2f} \"{r['file_path']}\"")
            print()


@roster_app.command("show")
def roster_show() -> None:
    data = json.loads(Path(ROSTER_PATH).read_text())
    for k, v in data.items():
        print(f"- {v['canonical_name']} [{k}]: {', '.join(v.get('aliases', [])) or '(no aliases)'}")


@roster_app.command("add")
def roster_add(
    canonical_name: str = typer.Argument(...),
    alias: list[str] = typer.Option([], "--alias"),
    description: str = typer.Option("", "--description"),
    first_seen: str = typer.Option("", "--first-seen"),
) -> None:
    data = json.loads(Path(ROSTER_PATH).read_text()) if ROSTER_PATH.exists() else {}
    key = canonical_name.lower().replace(" ", "_").replace(".", "").replace("'", "")
    data[key] = {
        "canonical_name": canonical_name,
        "aliases": list(alias),
        "description": description,
        "first_seen": first_seen,
    }
    Path(ROSTER_PATH).write_text(json.dumps(data, indent=2))
    print(f"Added/updated: {canonical_name}")


if __name__ == "__main__":
    app()
