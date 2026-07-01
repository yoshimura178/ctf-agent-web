"""Web chat solver backend using Chrome-driven chat sessions."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from pathlib import Path
from typing import Any

from backend.chatweb import WebChatClient, build_image_analysis_prompt, parse_chatweb_response
from backend.local_tasks import LocalTaskClient, SolveContext
from backend.loop_detect import LOOP_WARNING_MESSAGE, LoopDetector
from backend.message_bus import ChallengeMessageBus
from backend.models import model_id_from_spec, provider_from_spec, supports_vision, web_provider_from_spec
from backend.prompts import ChallengeMeta, build_prompt, list_distfiles
from backend.sandbox import DockerSandbox
from backend.solver_base import CANCELLED, ERROR, FLAG_FOUND, GAVE_UP, SolverResult
from backend.tools.core import (
    do_bash,
    do_check_findings,
    do_list_files,
    do_read_file,
    do_submit_flag,
    do_view_image,
    do_web_fetch,
    do_webhook_create,
    do_webhook_get_requests,
    do_write_file,
)
from backend.tracing import SolverTracer

logger = logging.getLogger(__name__)
DOM_TODO_PAUSE_LOCK = asyncio.Lock()


TOOL_PROTOCOL = """\
You must respond with exactly one JSON object and no prose.

To call a tool:
{"action":"tool","tool":"bash","args":{"command":"ls -la /challenge/distfiles","timeout_seconds":60}}

To report a candidate flag:
{"action":"final","flag":"flag{...}","method":"short explanation"}

Available tools:
- bash(command, timeout_seconds=60)
- read_file(path)
- write_file(path, content)
- list_files(path="/challenge/distfiles")
- submit_flag(flag)
- web_fetch(url, method="GET", body="")
- webhook_create()
- webhook_get_requests(uuid)
- check_findings()
- notify_coordinator(message)
- view_image(filename)

Use submit_flag for every candidate. The operator will verify it in the terminal.
"""


class WebChatSolver:
    """A solver backed by ChatGPT web UI automation."""

    def __init__(
        self,
        model_spec: str,
        challenge_dir: str,
        meta: ChallengeMeta,
        task_client: LocalTaskClient,
        settings: object,
        cancel_event: asyncio.Event | None = None,
        submit_fn=None,
        message_bus: ChallengeMessageBus | None = None,
        notify_coordinator=None,
        sandbox: DockerSandbox | None = None,
        owns_sandbox: bool | None = None,
        client: WebChatClient | None = None,
        status_tracker: object | None = None,
    ) -> None:
        self.model_spec = model_spec
        self.model_id = model_id_from_spec(model_spec)
        self.challenge_dir = challenge_dir
        self.meta = meta
        self.task_client = task_client
        self.settings = settings
        self.cancel_event = cancel_event or asyncio.Event()
        self.submit_fn = submit_fn
        self.message_bus = message_bus
        self.notify_coordinator = notify_coordinator
        self.status_tracker = status_tracker
        self._owns_sandbox = owns_sandbox if owns_sandbox is not None else (sandbox is None)
        self.sandbox = sandbox or DockerSandbox(
            image=getattr(settings, "sandbox_image", "ctf-sandbox"),
            challenge_dir=challenge_dir,
            memory_limit=getattr(settings, "container_memory_limit", "4g"),
            cpu_limit=getattr(settings, "container_cpu_limit", 2.0),
        )

        provider = web_provider_from_spec(model_spec)
        self.client = client or WebChatClient(
            provider=provider,
            model=self.model_id,
            user_data_dir=getattr(settings, "webchat_browser_user_data_dir", ""),
            profile_directory=getattr(settings, "webchat_browser_profile", ""),
            headless=getattr(settings, "webchat_headless", False),
        )
        self.loop_detector = LoopDetector()
        self.tracer = SolverTracer(meta.name, self.model_id)
        self.agent_name = f"{meta.name}/{self.model_id}"
        self._started = False
        self._step_count = 0
        self._flag: str | None = None
        self._confirmed = False
        self._findings = ""
        self._bump_insights: str | None = None

    async def start(self) -> None:
        self._status(state="starting", action="starting sandbox", detail=self.challenge_dir)
        if not self.sandbox._container:
            await self.sandbox.start()
        self._status(state="starting", action="detecting container arch")
        arch_result = await self.sandbox.exec("uname -m", timeout_s=10)
        container_arch = arch_result.stdout.strip() or "unknown"
        self._status(state="starting", action="building challenge prompt", detail=f"arch={container_arch}")
        prompt = build_prompt(
            self.meta,
            list_distfiles(self.challenge_dir),
            container_arch=container_arch,
            has_named_tools=True,
        )
        self._status(state="starting", action="opening chat web", detail=self.model_id)
        await self.client.start()
        try:
            self._status(state="starting", action="sending initial prompt", detail=f"{len(prompt)} chars")
            await self.client.send_and_receive(f"{TOOL_PROTOCOL}\n\n{prompt}")
        except NotImplementedError as e:
            await self._pause_on_dom_todo(e)
            raise
        self._started = True
        self._status(state="running", action="ready", detail="initial prompt accepted")
        self.tracer.event("start", challenge=self.meta.name, model=self.model_id, provider=provider_from_spec(self.model_spec))
        logger.info("[%s] Web chat solver started", self.agent_name)

    async def run_until_done_or_gave_up(self) -> SolverResult:
        if not self._started:
            await self.start()

        max_steps = int(getattr(self.settings, "webchat_max_steps_per_run", 25))
        t0 = time.monotonic()
        steps_before = self._step_count
        prompt = "Solve this CTF challenge. Use one JSON action now."
        if self._bump_insights:
            prompt = (
                "Your previous attempt did not find the flag. Insights from other agents:\n\n"
                f"{self._bump_insights}\n\nTry a different approach. Use one JSON action now."
            )
            self._bump_insights = None

        try:
            for _ in range(max_steps):
                if self.cancel_event.is_set():
                    return self._result(CANCELLED, steps_before, t0)

                try:
                    self._status(
                        state="running",
                        action="waiting for model response",
                        detail=f"step {self._step_count + 1}",
                        steps=self._step_count,
                    )
                    response = await self.client.send_and_receive(prompt)
                except NotImplementedError as e:
                    await self._pause_on_dom_todo(e)
                    raise
                self._status(state="running", action="parsing model response", detail=response[:160], steps=self._step_count)
                self.tracer.model_response(response[:1000], self._step_count)
                try:
                    action = parse_chatweb_response(response)
                except ValueError as e:
                    self._status(state="running", action="repairing invalid JSON", detail=str(e), steps=self._step_count)
                    prompt = f"Invalid response: {e}\nRespond again with exactly one JSON object."
                    continue

                if action.action == "tool":
                    result = await self._exec_tool(action.tool, action.args or {})
                    if self._confirmed and self._flag:
                        return self._result(FLAG_FOUND, steps_before, t0)
                    prompt = f"Tool result for {action.tool}:\n{result}\n\nUse one next JSON action."
                    continue

                if action.action == "final":
                    self._status(state="verifying", action="verifying candidate flag", detail=str(action.flag), steps=self._step_count)
                    self._findings = f"Flag candidate via {action.method}: {action.flag}"
                    if await self._verify_flag(action.flag, method=action.method):
                        return self._result(FLAG_FOUND, steps_before, t0)
                    prompt = (
                        f"Candidate flag was rejected by operator: {action.flag}\n"
                        "Continue solving with a different approach. Use one JSON action."
                    )
                    continue

                if action.action == "message":
                    self._status(state="running", action="received solver message", detail=action.message[:160], steps=self._step_count)
                    self._findings = action.message[:2000]
                    prompt = "Message noted. Continue solving with one JSON action."

            return self._result(GAVE_UP, steps_before, t0)
        except asyncio.CancelledError:
            return self._result(CANCELLED, steps_before, t0)
        except Exception as e:
            logger.error("[%s] Web chat solver error: %s", self.agent_name, e, exc_info=True)
            self._status(state=ERROR, action="solver error", detail=str(e), steps=self._step_count)
            self._findings = f"Error: {e}"
            self.tracer.event("error", error=str(e))
            return self._result(ERROR, steps_before, t0)

    def _solve_context(self, method: str = "") -> SolveContext:
        return SolveContext(
            model_spec=self.model_spec,
            trace_path=self.tracer.path,
            findings_summary=self._findings,
            method=method,
        )

    async def _pause_on_dom_todo(self, error: NotImplementedError) -> None:
        async with DOM_TODO_PAUSE_LOCK:
            print()
            print("=" * 72)
            print("Chat web DOM is not implemented yet.")
            print(f"Model: {self.model_spec}")
            print(f"Browser provider: {web_provider_from_spec(self.model_spec)}")
            print(f"Reason: {error}")
            print()
            print("The Chrome browser is being kept open for inspection/login/debug.")
            print("Fill selectors in backend/chatweb/, or press Enter to close this run.")
            print("=" * 72)
            await asyncio.to_thread(input, "Press Enter to stop this solver: ")

    async def _verify_flag(self, flag: str, method: str = "") -> bool:
        self._status(state="verifying", action="operator flag verification", detail=flag, steps=self._step_count)
        solve_context = self._solve_context(method)
        if self.submit_fn:
            display, confirmed = await self.submit_fn(flag, solve_context)
        else:
            display, confirmed = await do_submit_flag(
                self.task_client,
                self.meta.name,
                flag,
                model_spec=self.model_spec,
                solve_context=solve_context,
            )
        self.tracer.event("flag_candidate", flag=flag, confirmed=confirmed, display=display[:500])
        if confirmed:
            self._flag = flag
            self._confirmed = True
        return confirmed

    async def _exec_tool(self, name: str, args: dict[str, Any]) -> str:
        self._step_count += 1
        step = self._step_count
        self._status(
            state="tool",
            action=f"tool: {name}",
            detail=_tool_detail(name, args),
            steps=step,
        )
        self.tracer.tool_call(name, args, step)

        loop_status = self.loop_detector.check(name, args)
        if loop_status == "break":
            self.tracer.event("loop_break", tool=name, step=step)
            return LOOP_WARNING_MESSAGE

        if name == "bash":
            result = await do_bash(self.sandbox, args.get("command", ""), int(args.get("timeout_seconds", 60)))
        elif name == "read_file":
            result = str(await do_read_file(self.sandbox, args.get("path", "")))
        elif name == "write_file":
            result = await do_write_file(self.sandbox, args.get("path", ""), args.get("content", ""))
        elif name == "list_files":
            result = await do_list_files(self.sandbox, args.get("path", "/challenge/distfiles"))
        elif name == "submit_flag":
            flag = args.get("flag", "")
            result = "CORRECT" if await self._verify_flag(flag) else f"INCORRECT - {flag} rejected."
        elif name == "web_fetch":
            result = await do_web_fetch(args.get("url", ""), args.get("method", "GET"), args.get("body", ""))
        elif name == "webhook_create":
            result = await do_webhook_create()
        elif name == "webhook_get_requests":
            result = await do_webhook_get_requests(args.get("uuid", ""))
        elif name == "check_findings":
            result = await do_check_findings(self.message_bus, self.model_spec)
        elif name == "notify_coordinator":
            message = str(args.get("message", ""))
            if self.notify_coordinator:
                await self.notify_coordinator(message)
            result = "Coordinator notified."
        elif name == "view_image":
            viewed = await do_view_image(self.sandbox, args.get("filename", ""), supports_vision(self.model_spec))
            if isinstance(viewed, tuple):
                image_bytes, media_type = viewed
                result = await self._send_image_to_chatweb(args.get("filename", "image"), image_bytes, media_type)
            else:
                result = viewed
        else:
            result = f"Unknown tool: {name}"

        if loop_status == "warn":
            result = f"{result}\n\n{LOOP_WARNING_MESSAGE}"
        if step % 5 == 0:
            findings = await do_check_findings(self.message_bus, self.model_spec)
            if findings and "No new findings" not in findings:
                result = f"{result}\n\n---\n{findings}"
        self.tracer.tool_result(name, str(result), step)
        self._status(
            state="running",
            action=f"tool finished: {name}",
            detail=_truncate_status(str(result), 180),
            steps=step,
        )
        return str(result)

    async def _send_image_to_chatweb(self, filename: str, image_bytes: bytes, media_type: str) -> str:
        suffix = Path(filename).suffix or _suffix_from_media_type(media_type)
        with tempfile.NamedTemporaryFile(prefix="ctf-agent-image-", suffix=suffix, delete=False) as tmp:
            tmp.write(image_bytes)
            tmp_path = tmp.name
        prompt = build_image_analysis_prompt(Path(filename).name or Path(tmp_path).name, media_type)
        try:
            response = await self.client.send_files_and_receive(prompt, [tmp_path])
            return f"Image sent to chat web. Response:\n{response}"
        except NotImplementedError as e:
            return (
                f"Image was validated and staged for chat web upload, but upload is not wired yet: {e}\n"
                "Fallback: use bash image tools now, for example `file`, `exiftool`, `strings`, "
                "`binwalk`, `zsteg`, `steghide`, `stegseek`, `xxd`, `identify`, or `tesseract`."
            )
        except Exception as e:
            return (
                f"Image upload to chat web failed: {e}\n"
                "Fallback: use bash image tools now, for example `file`, `exiftool`, `strings`, "
                "`binwalk`, `zsteg`, `steghide`, `stegseek`, `xxd`, `identify`, or `tesseract`."
            )
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass

    def bump(self, insights: str) -> None:
        self._status(state="running", action="received bump", detail=insights[:180], steps=self._step_count)
        self._bump_insights = insights
        self.loop_detector.reset()
        self.tracer.event("bump", insights=insights[:500])

    def _result(self, status: str, steps_before: int, started_at: float) -> SolverResult:
        elapsed = time.monotonic() - started_at
        run_steps = self._step_count - steps_before
        self.tracer.event("finish", status=status, flag=self._flag, confirmed=self._confirmed)
        self._status(state=status, action="finished", detail=self._findings[:180], steps=self._step_count)
        return SolverResult(
            flag=self._flag,
            status=status,
            findings_summary=self._findings[:2000],
            step_count=run_steps,
            log_path=self.tracer.path,
        )

    async def stop(self) -> None:
        self._status(action="stopping", detail="closing tracer/browser/sandbox", steps=self._step_count)
        self.tracer.event("stop", step_count=self._step_count)
        self.tracer.close()
        await self.client.stop()
        if self._owns_sandbox and self.sandbox:
            await self.sandbox.stop()

    def _status(self, **kwargs) -> None:
        if not self.status_tracker:
            return
        update = getattr(self.status_tracker, "update", None)
        if update:
            update(self.model_spec, **kwargs)


def _suffix_from_media_type(media_type: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
        "image/tiff": ".tiff",
        "image/webp": ".webp",
    }.get(media_type, ".img")


def _tool_detail(name: str, args: dict[str, Any]) -> str:
    if name == "bash":
        return _truncate_status(str(args.get("command", "")), 180)
    if name in {"read_file", "write_file", "list_files", "view_image"}:
        return str(args.get("path") or args.get("filename") or "/challenge/distfiles")
    if name == "submit_flag":
        return str(args.get("flag", ""))
    if name == "web_fetch":
        return str(args.get("url", ""))
    if name in {"webhook_get_requests"}:
        return str(args.get("uuid", ""))
    if name == "notify_coordinator":
        return _truncate_status(str(args.get("message", "")), 180)
    return _truncate_status(str(args), 180)


def _truncate_status(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."
