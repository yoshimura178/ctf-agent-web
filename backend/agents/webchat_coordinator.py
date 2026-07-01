"""Web chat coordinator runner."""

from __future__ import annotations

import logging
from typing import Any

from backend.agents import coordinator_core
from backend.agents.coordinator_loop import build_deps, run_event_loop
from backend.chatweb import WebChatClient, parse_chatweb_response
from backend.config import Settings
from backend.deps import CoordinatorDeps
from backend.models import model_id_from_spec

logger = logging.getLogger(__name__)


COORDINATOR_PROTOCOL = """\
You are the CTF swarm coordinator.

Respond with exactly one JSON object and no prose.

To call a tool:
{"action":"tool","tool":"fetch_challenges","args":{}}

When no tool is needed:
{"action":"message","message":"short status or decision"}

Available tools:
- fetch_challenges()
- get_solve_status()
- spawn_swarm(challenge_name)
- check_swarm_status(challenge_name)
- submit_flag(challenge_name, flag)
- kill_swarm(challenge_name)
- bump_agent(challenge_name, model_spec, insights)
- read_solver_trace(challenge_name, model_spec, last_n=20)
- broadcast(challenge_name, message)

Prefer spawning swarms for unsolved challenges. Use status and trace tools when
you need more context before bumping, broadcasting, or stopping a swarm.
"""


DEFAULT_COORDINATOR_MODEL = "gpt-5.5-high"
MAX_COORDINATOR_TOOL_STEPS = 20


async def run_webchat_coordinator(
    settings: Settings,
    model_specs: list[str],
    challenges_root: str,
    coordinator_model: str | None = None,
    msg_port: int = 0,
) -> dict[str, Any]:
    """Run the coordinator using the ChatGPT web UI."""

    task_client, deps = build_deps(
        settings=settings,
        model_specs=model_specs,
        challenges_root=challenges_root,
    )
    deps.msg_port = msg_port

    model = model_id_from_spec(coordinator_model or DEFAULT_COORDINATOR_MODEL)
    client = WebChatClient(
        provider="chatgpt",
        model=model,
        user_data_dir=getattr(settings, "webchat_browser_user_data_dir", ""),
        profile_directory=getattr(settings, "webchat_browser_profile", ""),
        headless=getattr(settings, "webchat_headless", False),
    )

    async def turn_fn(message: str) -> None:
        await _run_coordinator_turn(client, deps, message)

    try:
        logger.info("Starting web chat coordinator with model %s", model)
        await client.start()
        return await run_event_loop(deps=deps, task_client=task_client, turn_fn=turn_fn)
    finally:
        await client.stop()


async def _run_coordinator_turn(client: WebChatClient, deps: CoordinatorDeps, message: str) -> None:
    prompt = (
        f"{COORDINATOR_PROTOCOL}\n\n"
        f"Coordinator event:\n{message}\n\n"
        "Choose one JSON action now."
    )

    for _ in range(MAX_COORDINATOR_TOOL_STEPS):
        response = await client.send_and_receive(prompt)
        try:
            action = parse_chatweb_response(response)
        except ValueError as e:
            prompt = f"Invalid response: {e}\nRespond again with exactly one JSON object."
            continue

        if action.action == "message":
            logger.info("Coordinator message: %s", action.message[:500])
            return

        if action.action == "final":
            logger.info("Coordinator final response ignored: %s", action.method[:500])
            return

        if action.action != "tool":
            prompt = "Unknown action. Respond with a tool call or message JSON object."
            continue

        result = await _exec_coordinator_tool(deps, action.tool, action.args or {})
        prompt = (
            f"Tool result for {action.tool}:\n{result}\n\n"
            "Use one next JSON action. Call another tool if needed, otherwise send a message."
        )

    logger.warning("Coordinator reached max tool steps for one turn")


async def _exec_coordinator_tool(deps: CoordinatorDeps, name: str, args: dict[str, Any]) -> str:
    logger.info("Coordinator tool call: %s %s", name, args)

    if name == "fetch_challenges":
        return await coordinator_core.do_fetch_challenges(deps)
    if name == "get_solve_status":
        return await coordinator_core.do_get_solve_status(deps)
    if name == "spawn_swarm":
        return await coordinator_core.do_spawn_swarm(deps, str(args.get("challenge_name", "")))
    if name == "check_swarm_status":
        return await coordinator_core.do_check_swarm_status(deps, str(args.get("challenge_name", "")))
    if name == "submit_flag":
        return await coordinator_core.do_submit_flag(
            deps,
            str(args.get("challenge_name", "")),
            str(args.get("flag", "")),
        )
    if name == "kill_swarm":
        return await coordinator_core.do_kill_swarm(deps, str(args.get("challenge_name", "")))
    if name == "bump_agent":
        return await coordinator_core.do_bump_agent(
            deps,
            str(args.get("challenge_name", "")),
            str(args.get("model_spec", "")),
            str(args.get("insights", "")),
        )
    if name == "read_solver_trace":
        return await coordinator_core.do_read_solver_trace(
            deps,
            str(args.get("challenge_name", "")),
            str(args.get("model_spec", "")),
            _int_arg(args.get("last_n"), 20),
        )
    if name == "broadcast":
        return await coordinator_core.do_broadcast(
            deps,
            str(args.get("challenge_name", "")),
            str(args.get("message", "")),
        )

    return f"Unknown coordinator tool: {name}"


def _int_arg(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
