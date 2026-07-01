"""Click CLI entry point."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click
from rich.console import Console

from backend.config import Settings
from backend.models import enabled_default_models
from backend.terminal_ui import TerminalStatusTracker, TerminalUI

console = Console()


def _setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.WARNING
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("aiodocker").setLevel(logging.WARNING)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-8s %(message)s", datefmt="%X"))
    logging.basicConfig(level=level, handlers=[handler], force=True)


@click.command()
@click.option("--image", default=None, help="Override Docker sandbox image name from config")
@click.option("--models", multiple=True, help="Model specs (default: all configured)")
@click.option("--challenge", default=None, help="Solve a single challenge directory")
@click.option("--tasks-dir", default=None, help="Override local tasks directory from config")
@click.option("--challenges-dir", default=None, help="Override runtime challenge directory from config")
@click.option("--coordinator-model", default=None, help="Model for coordinator")
@click.option("--max-challenges", default=None, type=int, help="Override max concurrent tasks from config")
@click.option("--msg-port", default=0, type=int, help="Operator message port (0 = auto)")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging and debug terminal mode")
def main(
    image: str | None,
    models: tuple[str, ...],
    challenge: str | None,
    tasks_dir: str | None,
    challenges_dir: str | None,
    coordinator_model: str | None,
    max_challenges: int | None,
    msg_port: int,
    verbose: bool,
) -> None:
    """CTF Agent — multi-model solver swarm.

    Run without --challenge to start the full coordinator (Ctrl+C to stop).
    """
    settings = Settings()
    debug_terminal = verbose or settings.terminal_debug
    _setup_logging(debug_terminal)

    if image is not None:
        settings.sandbox_image = image
    if tasks_dir is not None:
        settings.tasks_dir = tasks_dir
    if challenges_dir is not None:
        settings.local_challenges_dir = challenges_dir
    if max_challenges is not None:
        settings.max_concurrent_challenges = max_challenges
    max_challenges_effective = settings.max_concurrent_challenges

    model_specs = list(models) if models else enabled_default_models(settings)
    run_mode = "single challenge" if challenge else "coordinator"
    ui = TerminalUI(console, debug=debug_terminal)

    ui.startup(
        run_mode=run_mode,
        tasks_dir=settings.tasks_dir,
        challenge=challenge,
        model_specs=model_specs,
        image=settings.sandbox_image,
        max_challenges=max_challenges_effective,
    )

    if challenge:
        asyncio.run(_run_single(settings, challenge, model_specs, ui))
    else:
        asyncio.run(_run_coordinator(settings, model_specs, settings.local_challenges_dir, coordinator_model, ui, msg_port))


async def _run_single(
    settings: Settings,
    challenge_dir: str,
    model_specs: list[str],
    ui: TerminalUI,
) -> None:
    """Run a single challenge with a swarm."""
    from backend.agents.swarm import ChallengeSwarm
    from backend.local_tasks import LocalTaskClient, prepare_task_runtime_dir
    from backend.prompts import ChallengeMeta
    from backend.sandbox import cleanup_orphan_containers, configure_semaphore

    max_containers = settings.max_concurrent_challenges * len(model_specs)
    configure_semaphore(max_containers)
    await cleanup_orphan_containers()

    challenge_path = Path(challenge_dir)
    meta_path = challenge_path / "metadata.yml"
    if not meta_path.exists() and (challenge_path / "README.md").exists():
        prepared, _ = prepare_task_runtime_dir(challenge_path, settings.local_challenges_dir)
        challenge_path = Path(prepared)
        meta_path = challenge_path / "metadata.yml"
    if not meta_path.exists():
        console.print(f"[red]No metadata.yml or README.md found in {challenge_dir}[/red]")
        sys.exit(1)

    meta = ChallengeMeta.from_yaml(meta_path)
    ui.challenge_started(meta.name, meta.category)

    task_client = LocalTaskClient(tasks_dir=settings.tasks_dir, runtime_root=settings.local_challenges_dir)
    task_client.prepare_all()
    status_tracker = None if ui.debug else TerminalStatusTracker(meta.name, model_specs)

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_path),
        meta=meta,
        task_client=task_client,
        settings=settings,
        model_specs=model_specs,
        status_tracker=status_tracker,
    )

    try:
        if status_tracker:
            async with ui.live_status(status_tracker):
                result = await swarm.run()
        else:
            result = await swarm.run()
        from backend.solver_base import FLAG_FOUND
        ui.single_result(result if result and result.status == FLAG_FOUND else result, swarm.last_results)

    finally:
        await task_client.close()


async def _run_coordinator(
    settings: Settings,
    model_specs: list[str],
    challenges_dir: str,
    coordinator_model: str | None,
    ui: TerminalUI,
    msg_port: int = 0,
) -> None:
    """Run the full coordinator (continuous until Ctrl+C)."""
    from backend.sandbox import cleanup_orphan_containers, configure_semaphore

    max_containers = settings.max_concurrent_challenges * len(model_specs)
    configure_semaphore(max_containers)
    await cleanup_orphan_containers()
    ui.coordinator_started()

    from backend.agents.webchat_coordinator import run_webchat_coordinator
    results = await run_webchat_coordinator(
        settings=settings,
        model_specs=model_specs,
        challenges_root=challenges_dir,
        coordinator_model=coordinator_model,
        msg_port=msg_port,
    )

    ui.final_results(results.get("results", {}))


@click.command()
@click.argument("message")
@click.option("--port", default=9400, type=int, help="Coordinator message port")
@click.option("--host", default="127.0.0.1", help="Coordinator host")
def msg(message: str, port: int, host: str) -> None:
    """Send a message to the running coordinator."""
    import json
    import urllib.request

    body = json.dumps({"message": message}).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}/msg",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            console.print(f"[green]Sent:[/green] {data.get('queued', message[:200])}")
    except Exception as e:
        console.print(f"[red]Failed:[/red] {e}")
        console.print("Is the coordinator running?")
        sys.exit(1)


if __name__ == "__main__":
    main()
