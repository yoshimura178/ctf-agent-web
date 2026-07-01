"""Rich terminal UI helpers."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from backend.solver_base import FLAG_FOUND, SolverResult


@dataclass
class AgentStatus:
    model: str
    state: str = "queued"
    action: str = "queued"
    detail: str = ""
    steps: int = 0
    started_at: float = field(default_factory=time.monotonic)
    action_started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None


class TerminalStatusTracker:
    """Tracks per-agent status for the normal terminal UI."""

    def __init__(self, challenge: str, model_specs: Sequence[str]) -> None:
        self.challenge = challenge
        self.started_at = time.monotonic()
        self._lock = RLock()
        self._agents = {spec: AgentStatus(model=spec) for spec in model_specs}

    def update(
        self,
        model: str,
        *,
        state: str | None = None,
        action: str | None = None,
        detail: str | None = None,
        steps: int | None = None,
    ) -> None:
        with self._lock:
            agent = self._agents.setdefault(model, AgentStatus(model=model))
            if state is not None:
                agent.state = state
                if state in {"flag_found", "gave_up", "cancelled", "error", "finished"}:
                    agent.finished_at = time.monotonic()
            if action is not None and action != agent.action:
                agent.action = action
                agent.action_started_at = time.monotonic()
            if detail is not None:
                agent.detail = detail
            if steps is not None:
                agent.steps = steps

    def snapshot(self) -> list[AgentStatus]:
        with self._lock:
            return [
                AgentStatus(
                    model=agent.model,
                    state=agent.state,
                    action=agent.action,
                    detail=agent.detail,
                    steps=agent.steps,
                    started_at=agent.started_at,
                    action_started_at=agent.action_started_at,
                    finished_at=agent.finished_at,
                )
                for agent in self._agents.values()
            ]

    def render(self):
        elapsed = _format_duration(time.monotonic() - self.started_at)
        table = Table(title=f"Live Solver Status - {self.challenge} - elapsed {elapsed}", show_header=True, header_style="bold")
        table.add_column("Model", overflow="fold", ratio=3)
        table.add_column("State", width=12)
        table.add_column("Current action", overflow="fold", ratio=2)
        table.add_column("Action time", justify="right", width=11)
        table.add_column("Total time", justify="right", width=10)
        table.add_column("Steps", justify="right", width=6)
        table.add_column("Detail", overflow="fold", ratio=3)

        now = time.monotonic()
        for agent in self.snapshot():
            end = agent.finished_at or now
            state_text = Text(agent.state, style=_status_style(agent.state))
            table.add_row(
                agent.model,
                state_text,
                agent.action,
                _format_duration(now - agent.action_started_at if not agent.finished_at else end - agent.action_started_at),
                _format_duration(end - agent.started_at),
                str(agent.steps),
                _truncate(agent.detail, 120),
            )
        return Panel(table, border_style="cyan")


class TerminalUI:
    """Small terminal UI layer for normal, non-debug runs."""

    def __init__(self, console: Console, debug: bool = False) -> None:
        self.console = console
        self.debug = debug

    def startup(
        self,
        *,
        run_mode: str,
        tasks_dir: str,
        challenge: str | None,
        model_specs: Sequence[str],
        image: str,
        max_challenges: int,
    ) -> None:
        if self.debug:
            self.console.print("[bold]CTF Agent web[/bold]")
            self.console.print(f"  Mode: {run_mode}")
            self.console.print(f"  Tasks: {tasks_dir}")
            if challenge:
                self.console.print(f"  Challenge: {challenge}")
            self.console.print(f"  Models: {', '.join(model_specs)}")
            self.console.print(f"  Image: {image}")
            self.console.print(f"  Max challenges: {max_challenges}")
            self.console.print()
            return

        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold cyan")
        table.add_column()
        table.add_row("Mode", run_mode)
        table.add_row("Tasks", tasks_dir)
        if challenge:
            table.add_row("Challenge", challenge)
        table.add_row("Models", str(len(model_specs)))
        table.add_row("Image", image)
        table.add_row("Max challenges", str(max_challenges))
        self.console.print(Panel(table, title="CTF Agent web", border_style="cyan"))

        model_table = Table(title="Enabled Models", show_header=True, header_style="bold")
        model_table.add_column("#", justify="right", style="dim", width=4)
        model_table.add_column("Model")
        for idx, spec in enumerate(model_specs, start=1):
            model_table.add_row(str(idx), spec)
        self.console.print(model_table)
        self.console.print()

    def challenge_started(self, name: str, category: str) -> None:
        if self.debug:
            self.console.print(f"[bold]Challenge:[/bold] {name} ({category})")
            return
        body = Table.grid(padding=(0, 2))
        body.add_column(style="bold cyan")
        body.add_column()
        body.add_row("Name", name)
        body.add_row("Category", category or "Unknown")
        self.console.print(Panel(body, title="Challenge", border_style="blue"))

    @asynccontextmanager
    async def live_status(self, tracker: TerminalStatusTracker):
        if self.debug:
            yield
            return

        live = Live(tracker.render(), console=self.console, refresh_per_second=2, transient=False)
        live.start()
        stop_event = asyncio.Event()

        async def _refresh() -> None:
            while not stop_event.is_set():
                live.update(tracker.render())
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=0.5)
                except TimeoutError:
                    pass

        task = asyncio.create_task(_refresh())
        try:
            yield
        finally:
            stop_event.set()
            await asyncio.gather(task, return_exceptions=True)
            live.update(tracker.render())
            live.stop()

    def coordinator_started(self) -> None:
        if self.debug:
            self.console.print("[bold]Starting webchat coordinator (Ctrl+C to stop)...[/bold]\n")
            return
        self.console.print(
            Panel(
                "Webchat coordinator is running.\nPress Ctrl+C to stop.",
                title="Coordinator",
                border_style="blue",
            )
        )

    def single_result(
        self,
        result: SolverResult | None,
        last_results: Mapping[str, SolverResult],
    ) -> None:
        if self.debug:
            if result and result.status == FLAG_FOUND:
                self.console.print(f"\n[bold green]FLAG FOUND:[/bold green] {result.flag}")
            else:
                self.console.print("\n[bold red]No flag found.[/bold red]")
                if last_results:
                    self.console.print("\n[bold]Solver diagnostics:[/bold]")
                    for model_spec, solver_result in last_results.items():
                        detail = solver_result.findings_summary or solver_result.status
                        self.console.print(f"  {model_spec}: {solver_result.status} - {detail}")
            return

        if result and result.status == FLAG_FOUND:
            self.console.print(Panel(f"[bold green]{result.flag}[/bold green]", title="FLAG FOUND", border_style="green"))
        else:
            self.console.print(Panel("[bold red]No flag found.[/bold red]", title="Result", border_style="red"))

        if last_results:
            self.console.print(self._diagnostics_table(last_results))

    def final_results(self, results: Mapping[str, Any]) -> None:
        if self.debug:
            self.console.print("\n[bold]Final Results:[/bold]")
            for challenge, data in results.items():
                flag = data.get("flag", "no flag") if isinstance(data, dict) else "no flag"
                self.console.print(f"  {challenge}: {flag}")
            return

        table = Table(title="Final Results", show_header=True, header_style="bold")
        table.add_column("Challenge")
        table.add_column("Flag")
        for challenge, data in results.items():
            flag = data.get("flag", "no flag") if isinstance(data, dict) else "no flag"
            table.add_row(str(challenge), str(flag))
        self.console.print(table)

    def _diagnostics_table(self, results: Mapping[str, SolverResult]) -> Table:
        table = Table(title="Solver Diagnostics", show_header=True, header_style="bold")
        table.add_column("Model", overflow="fold")
        table.add_column("Status", width=14)
        table.add_column("Steps", justify="right", width=6)
        table.add_column("Trace", overflow="fold")
        table.add_column("Detail", overflow="fold")
        for model_spec, solver_result in results.items():
            status_style = _status_style(solver_result.status)
            detail = _truncate(solver_result.findings_summary or solver_result.status, 220)
            table.add_row(
                model_spec,
                Text(solver_result.status, style=status_style),
                str(solver_result.step_count),
                solver_result.log_path or "-",
                detail,
            )
        return table


def _status_style(status: str) -> str:
    if status == FLAG_FOUND:
        return "green bold"
    if status in {"running", "tool"}:
        return "cyan"
    if status in {"starting", "verifying", "cooldown"}:
        return "yellow"
    if status == "error":
        return "red"
    if status == "gave_up":
        return "yellow"
    if status == "cancelled":
        return "dim"
    return ""


def _truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
