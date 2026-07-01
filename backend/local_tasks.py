"""Local task source and human flag verification.

Local tasks live in:

    task/<challenge>/README.md

The README is the challenge statement. Every other file or directory under the
task folder is mirrored into the runtime challenge's distfiles directory so the
existing sandbox mount layout stays unchanged.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from backend.prompts import ChallengeMeta


SOLVED_FILENAME = "SOLVED.txt"
WRITEUP_FILENAME = "WRITEUP.md"


@dataclass
class SubmitResult:
    status: str
    message: str
    display: str


@dataclass
class SolveContext:
    model_spec: str = ""
    trace_path: str = ""
    findings_summary: str = ""
    method: str = ""


class TerminalFlagVerifier:
    """Process-wide terminal queue for human flag verification."""

    def __init__(self) -> None:
        self._lock = None
        self._pending_lock = None
        self._pending = 0
        self._counter = 0

    async def _get_locks(self) -> tuple[asyncio.Lock, asyncio.Lock]:
        loop = asyncio.get_running_loop()
        if getattr(self, "_loop", None) is not loop:
            self._lock = asyncio.Lock()
            self._pending_lock = asyncio.Lock()
            self._loop = loop
        return self._lock, self._pending_lock

    async def verify(self, challenge_name: str, flag: str, model_spec: str = "") -> bool:
        lock, pending_lock = await self._get_locks()
        async with pending_lock:
            self._counter += 1
            request_id = self._counter
            self._pending += 1
            queued_ahead = self._pending - 1

        if queued_ahead:
            print(
                f"\nQueued flag #{request_id} from {challenge_name}; "
                f"{queued_ahead} verification(s) ahead."
            )

        async with lock:
            async with pending_lock:
                self._pending -= 1
                remaining = self._pending

            print()
            print("=" * 72)
            print(f"Pending flag #{request_id}")
            print(f"Challenge: {challenge_name}")
            if model_spec:
                print(f"Model: {model_spec}")
            if remaining:
                print(f"Queued after this: {remaining}")
            print(f"FLAG: {flag}")
            print("=" * 72)
            answer = await asyncio.to_thread(input, "is that correct (y/n): ")

        return answer.strip().lower() in {"y", "yes"}


TERMINAL_FLAG_VERIFIER = TerminalFlagVerifier()


def _slugify(name: str) -> str:
    import re

    slug = re.sub(r'[<>:"/\\|?*.\x00-\x1f]', "", name.lower().strip())
    slug = re.sub(r"[\s_]+", "-", slug)
    return re.sub(r"-+", "-", slug).strip("-") or "challenge"


def _copy_task_distfiles(task_dir: Path, dist_dir: Path) -> None:
    dist_dir.mkdir(parents=True, exist_ok=True)
    for child in task_dir.iterdir():
        if child.name.lower() in {"readme.md", SOLVED_FILENAME.lower(), WRITEUP_FILENAME.lower()}:
            continue
        dest = dist_dir / child.name
        if dest.exists():
            if dest.is_dir():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        if child.is_dir():
            shutil.copytree(child, dest)
        else:
            shutil.copy2(child, dest)


def build_meta_from_task(task_dir: str | Path) -> ChallengeMeta:
    task_path = Path(task_dir)
    readme = task_path / "README.md"
    description = readme.read_text(encoding="utf-8", errors="replace")
    return ChallengeMeta(
        name=task_path.name,
        category="local",
        description=description.strip(),
    )


def _markdown_code_fence(text: str) -> str:
    if "```" not in text:
        return f"```\n{text}\n```"
    return f"````\n{text}\n````"


def _quote_front_matter(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _flag_format(flag: str) -> str:
    import re

    match = re.match(r"^([A-Za-z0-9_.-]+)\{.*\}$", flag)
    if match:
        return f"{match.group(1)}{{...}}"
    return flag


def _truncate_text(text: str, limit: int = 1600) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n... [truncated, {len(text)} chars total]"


def _load_trace_events(trace_path: str) -> list[dict[str, Any]]:
    path = Path(trace_path)
    if not trace_path or not path.exists():
        return []

    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _parse_tool_args(raw_args: Any) -> dict[str, Any]:
    if isinstance(raw_args, dict):
        return raw_args
    if not isinstance(raw_args, str):
        return {}
    try:
        parsed = json.loads(raw_args)
    except json.JSONDecodeError:
        return {"raw": raw_args}
    return parsed if isinstance(parsed, dict) else {"raw": raw_args}


def _summarize_trace(trace_path: str) -> dict[str, Any]:
    events = _load_trace_events(trace_path)
    calls: dict[tuple[int, str], dict[str, Any]] = {}
    latest_model_response = ""

    for event in events:
        event_type = event.get("type")
        if event_type == "model_response":
            latest_model_response = str(event.get("text") or "")
            continue
        if event_type == "tool_call":
            step = int(event.get("step") or 0)
            tool = str(event.get("tool") or "")
            calls[(step, tool)] = {
                "step": step,
                "tool": tool,
                "args": _parse_tool_args(event.get("args")),
                "result": "",
            }
            continue
        if event_type == "tool_result":
            step = int(event.get("step") or 0)
            tool = str(event.get("tool") or "")
            calls.setdefault(
                (step, tool),
                {"step": step, "tool": tool, "args": {}, "result": ""},
            )["result"] = str(event.get("result") or "")

    tool_steps = sorted(calls.values(), key=lambda item: item["step"])
    solving_scripts = []
    for item in tool_steps:
        if item["tool"] != "write_file":
            continue
        path = str(item["args"].get("path") or "")
        content = str(item["args"].get("content") or "")
        lower_path = path.lower()
        if content and (
            lower_path.endswith((".py", ".sh"))
            or "solve" in lower_path
            or "exploit" in lower_path
        ):
            solving_scripts.append({"path": path, "content": content})

    return {
        "trace_path": trace_path,
        "tool_steps": tool_steps,
        "solving_scripts": solving_scripts,
        "latest_model_response": latest_model_response,
    }


def _tool_step_to_markdown(item: dict[str, Any]) -> str:
    tool = item["tool"]
    args = item["args"]
    result = _truncate_text(str(item.get("result") or ""), 1200)

    if tool == "bash":
        command = str(args.get("command") or "")
        body = f"```bash\n{command}\n```"
    elif tool == "read_file":
        body = f"Read `{args.get('path', '')}`."
    elif tool == "write_file":
        body = f"Wrote `{args.get('path', '')}`."
    elif tool == "list_files":
        body = f"Listed `{args.get('path', '/challenge/distfiles')}`."
    else:
        body = f"`{tool}` with args `{json.dumps(args, ensure_ascii=False)}`."

    if result:
        body += f"\n\nOutput:\n{_markdown_code_fence(result)}"
    return body


def build_writeup_template(
    meta: ChallengeMeta,
    flag: str,
    solved_at: datetime,
    model_spec: str = "",
    solve_context: SolveContext | None = None,
) -> str:
    """Build a submission-style WRITEUP.md scaffold for a confirmed solve."""
    category = meta.category if meta.category and meta.category != "local" else "misc"
    context = solve_context or SolveContext(model_spec=model_spec)
    trace_summary = _summarize_trace(context.trace_path)
    effective_model = context.model_spec or model_spec
    model_line = f"- Model: `{effective_model}`\n" if effective_model else ""
    trace_line = f"- Trace: `{context.trace_path}`\n" if context.trace_path else ""
    challenge_statement = meta.description.strip() or "No challenge statement provided."
    date = solved_at.date().isoformat()
    solved_at_text = solved_at.isoformat()
    method = context.method or context.findings_summary or "The solver confirmed the candidate flag through the terminal verification flow."
    script_sections = []
    for script in trace_summary["solving_scripts"][:2]:
        suffix = Path(script["path"]).suffix.lower()
        language = "bash" if suffix == ".sh" else "python" if suffix == ".py" else ""
        script_sections.append(
            f"Solver-created script `{script['path']}`:\n\n"
            f"```{language}\n{_truncate_text(script['content'], 5000)}\n```"
        )
    if not script_sections:
        script_sections.append(
            "No standalone solve script was captured in the trace. The reproducible path below is reconstructed from tool calls."
        )
    script_text = "\n\n".join(script_sections)

    key_steps = [
        item
        for item in trace_summary["tool_steps"]
        if item["tool"] in {"bash", "read_file", "list_files", "web_fetch", "write_file", "view_image"}
    ][:8]
    if key_steps:
        trace_steps = "\n\n".join(
            f"#### Tool step {item['step']}: `{item['tool']}`\n\n{_tool_step_to_markdown(item)}"
            for item in key_steps
        )
    else:
        trace_steps = "No tool trace was available for this solve."

    latest_model = trace_summary["latest_model_response"].strip()
    model_note = (
        f"\n\nLatest model response before confirmation:\n{_markdown_code_fence(_truncate_text(latest_model, 1200))}"
        if latest_model
        else ""
    )

    return f"""---
title: {_quote_front_matter(meta.name)}
ctf: "local"
date: {date}
category: {category}
difficulty: unknown
flag_format: {_quote_front_matter(_flag_format(flag))}
author: "operator"
---

# {meta.name}

## Summary

This challenge was solved locally. The final flag was confirmed by the operator after the winning solver produced a candidate.

## Solution

### Step 1: Key observation

{method}

{model_line}- Solved at: `{solved_at_text}`
{trace_line}

### Step 2: Reproduce the solve

{script_text}

### Step 3: Evidence from the winning trace

{trace_steps}{model_note}

## Challenge

{_markdown_code_fence(challenge_statement)}

## Flag

{_markdown_code_fence(flag)}
"""


def prepare_task_runtime_dir(task_dir: str | Path, runtime_root: str | Path) -> tuple[str, ChallengeMeta]:
    task_path = Path(task_dir)
    if not (task_path / "README.md").exists():
        raise FileNotFoundError(f"No README.md found in local task: {task_path}")

    meta = build_meta_from_task(task_path)
    challenge_dir = Path(runtime_root) / _slugify(meta.name)
    dist_dir = challenge_dir / "distfiles"
    challenge_dir.mkdir(parents=True, exist_ok=True)
    _copy_task_distfiles(task_path, dist_dir)

    meta_data = {
        "name": meta.name,
        "category": meta.category,
        "description": meta.description,
        "connection_info": meta.connection_info,
        "tags": meta.tags,
    }
    (challenge_dir / "metadata.yml").write_text(
        yaml.dump(meta_data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return str(challenge_dir), meta


@dataclass
class LocalTaskClient:
    """Local challenge source backed by task directories."""

    tasks_dir: str = "task"
    runtime_root: str = "challenges-local"

    _challenge_dirs: dict[str, str] = field(default_factory=dict)
    _challenge_metas: dict[str, ChallengeMeta] = field(default_factory=dict)
    _pending_writeups: set = field(default_factory=set)

    async def close(self) -> None:
        if self._pending_writeups:
            import logging
            logger = logging.getLogger(__name__)
            logger.info("Waiting for %d pending AI writeup(s) to finish...", len(self._pending_writeups))
            await asyncio.gather(*self._pending_writeups, return_exceptions=True)

    def scan_tasks(self) -> dict[str, Path]:
        root = Path(self.tasks_dir)
        if not root.exists():
            return {}
        tasks: dict[str, Path] = {}
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "README.md").exists():
                tasks[child.name] = child
        return tasks

    def prepare_all(self) -> None:
        challenge_dirs: dict[str, str] = {}
        challenge_metas: dict[str, ChallengeMeta] = {}
        for name, task_path in self.scan_tasks().items():
            ch_dir, meta = prepare_task_runtime_dir(task_path, self.runtime_root)
            challenge_dirs[name] = ch_dir
            challenge_metas[name] = meta
        self._challenge_dirs = challenge_dirs
        self._challenge_metas = challenge_metas

    def _solved_names_from_files(self) -> set[str]:
        return {
            name
            for name, task_path in self.scan_tasks().items()
            if (task_path / SOLVED_FILENAME).exists()
        }

    def _write_solved_file(self, challenge_name: str, flag: str, solved_at: datetime, model_spec: str = "") -> None:
        task_path = self.scan_tasks().get(challenge_name)
        if not task_path:
            raise FileNotFoundError(f'Local task "{challenge_name}" not found for solved marker')

        lines = [
            f"challenge: {challenge_name}",
            f"flag: {flag}",
            f"solved_at: {solved_at.isoformat()}",
            "source: operator",
        ]
        if model_spec:
            lines.append(f"model: {model_spec}")
        (task_path / SOLVED_FILENAME).write_text("\n".join(lines) + "\n", encoding="utf-8")

    async def _async_write_writeup_file(
        self,
        challenge_name: str,
        flag: str,
        solved_at: datetime,
        model_spec: str = "",
        solve_context: SolveContext | None = None,
    ) -> None:
        task_path = self.scan_tasks().get(challenge_name)
        if not task_path:
            raise FileNotFoundError(f'Local task "{challenge_name}" not found for writeup')
        writeup_path = task_path / WRITEUP_FILENAME
        if writeup_path.exists():
            return
        meta = build_meta_from_task(task_path)
        
        async def _do_writeup():
            await asyncio.sleep(5)
            import logging
            logger = logging.getLogger(__name__)
            try:
                from backend.chatweb.writeup_writer import WriteupWriter
                from backend.config import Settings
                logger.info("Generating AI writeup for %s...", challenge_name)
                writer = WriteupWriter(Settings())
                content = await writer.generate_writeup(meta, flag, solved_at.isoformat(), solve_context)
            except Exception as e:
                logger.warning("AI Writeup generation failed: %s. Falling back to default template.", e)
                content = build_writeup_template(meta, flag, solved_at, model_spec, solve_context)
            writeup_path.write_text(content, encoding="utf-8")

        task = asyncio.create_task(_do_writeup())
        self._pending_writeups.add(task)
        task.add_done_callback(self._pending_writeups.discard)

    @property
    def challenge_dirs(self) -> dict[str, str]:
        self.prepare_all()
        return dict(self._challenge_dirs)

    @property
    def challenge_metas(self) -> dict[str, ChallengeMeta]:
        self.prepare_all()
        return dict(self._challenge_metas)

    async def fetch_challenge_stubs(self) -> list[dict[str, Any]]:
        self.prepare_all()
        solved_names = self._solved_names_from_files()
        return [
            {
                "id": idx + 1,
                "name": meta.name,
                "category": meta.category,
            }
            for idx, meta in enumerate(self._challenge_metas.values())
        ]

    async def fetch_solved_names(self) -> set[str]:
        return self._solved_names_from_files()

    async def fetch_all_challenges(self) -> list[dict[str, Any]]:
        self.prepare_all()
        solved_names = self._solved_names_from_files()
        return [
            {
                "id": idx + 1,
                "name": meta.name,
                "category": meta.category,
                "description": meta.description,
                "connection_info": meta.connection_info,
                "tags": meta.tags,
                "hints": meta.hints,
                "local_dir": self._challenge_dirs[meta.name],
            }
            for idx, meta in enumerate(self._challenge_metas.values())
        ]

    async def pull_challenge(self, challenge: dict[str, Any], output_dir: str) -> str:
        name = challenge.get("name", "")
        self.prepare_all()
        if name not in self._challenge_dirs:
            raise RuntimeError(f'Local task "{name}" not found')
        return self._challenge_dirs[name]

    async def submit_flag(
        self,
        challenge_name: str,
        flag: str,
        model_spec: str = "",
        solve_context: SolveContext | None = None,
    ) -> SubmitResult:
        """Ask the operator to verify a candidate flag in the terminal."""
        normalized = flag.strip()
        if not normalized:
            return SubmitResult("incorrect", "empty flag", "Empty flag - nothing to verify.")

        if challenge_name in self._solved_names_from_files():
            return SubmitResult("already_solved", "already solved", f'ALREADY SOLVED - "{challenge_name}" has SOLVED.txt.')

        if await TERMINAL_FLAG_VERIFIER.verify(challenge_name, normalized, model_spec):
            solved_at = datetime.now(timezone.utc)
            self._write_solved_file(challenge_name, normalized, solved_at, model_spec)
            await self._async_write_writeup_file(challenge_name, normalized, solved_at, model_spec, solve_context)
            return SubmitResult("correct", "confirmed by operator", f'CORRECT - "{normalized}" accepted by operator.')
        return SubmitResult("incorrect", "rejected by operator", f'INCORRECT - "{normalized}" rejected by operator.')

    async def close(self) -> None:
        """Wait for any pending background tasks to complete."""
        if getattr(self, "_pending_writeups", None):
            await asyncio.gather(*self._pending_writeups, return_exceptions=True)

