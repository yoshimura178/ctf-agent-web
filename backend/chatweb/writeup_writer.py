"""AI-powered Writeup Writer."""

import json
import logging
from typing import Any

from backend.chatweb import WebChatClient
from backend.config import Settings
from backend.local_tasks import ChallengeMeta, SolveContext, _summarize_trace

logger = logging.getLogger(__name__)

WRITEUP_PROMPT_TEMPLATE = """\
# CTF Write-up Generator

Generate a standardized submission-style CTF writeup for a solved challenge.

Default behavior:
- During an active competition, optimize for speed, clarity, and reproducibility
- Keep writeups short enough that a teammate or organizer can validate the solve quickly
- Always produce a `submission`-style writeup
- Prefer one complete solve script from challenge data to final flag

## Templates
### Submission Format

```markdown
---
title: "<Challenge Name>"
ctf: "<CTF Event Name>"
date: YYYY-MM-DD
category: web|pwn|crypto|reverse|forensics|osint|malware|misc
difficulty: easy|medium|hard
flag_format: "flag{{...}}"
author: "<your name or team>"
---

# <Challenge Name>

## Summary

<1-2 sentences: what the challenge was and the core technique. Keep it direct.>

## Solution

### Step 1: <Action>

<Explain the key observation in 3-8 short lines. Keep it direct.>

```python
<one complete solving script from provided challenge data to printing the final flag>
```

### Step 2: <Action> (optional)

<Only add this when a second short step genuinely helps readability, such as separating the core observation from final verification.>

## Flag

```
flag{{example_flag_here}}
```
```

## Quality Guidelines
DO:
- Explain just enough for fast verification
- Include one complete solving path, not multiple alternative routes
- Include one complete script that goes all the way to the final flag
- Show actual output (truncated if very long) to prove the approach worked

DON'T:
- Copy-paste raw terminal dumps without explanation
- Paste several partial snippets that force the reader to reconstruct the final solve
- Include irrelevant tangents that don't contribute to the solution

## Challenge Data
{metadata}

## Log of actions taken by the solver
{trace_summary}

Please generate ONLY the final Markdown writeup. Do not include any conversational filler.
"""


class WriteupWriter:
    def __init__(self, settings: Settings, model: str | None = None) -> None:
        self.settings = settings
        provider = "chatgpt"
        self.provider = provider
        self.model = model or self._default_model(provider)
        self.client = WebChatClient(
            provider=provider,
            model=self.model,
            user_data_dir=getattr(settings, "webchat_browser_user_data_dir", ""),
            profile_directory=getattr(settings, "webchat_browser_profile", ""),
            headless=getattr(settings, "webchat_headless", False),
        )

    @staticmethod
    def _default_model(provider: str) -> str:
        return "gpt-5.5-high"

    async def generate_writeup(
        self, meta: ChallengeMeta, flag: str, solved_at: str, solve_context: SolveContext
    ) -> str:
        meta_dict = {
            "name": meta.name,
            "category": meta.category,
            "flag": flag,
            "solved_at": solved_at,
        }
        metadata_str = json.dumps(meta_dict, indent=2)

        trace_summary_data = (
            _summarize_trace(solve_context.trace_path)
            if solve_context and solve_context.trace_path
            else {"tool_steps": [], "solving_scripts": []}
        )
        
        trace_str = json.dumps(trace_summary_data, indent=2)
        if len(trace_str) > 100000:
            trace_str = trace_str[:100000] + "\n...[TRUNCATED]"

        prompt = WRITEUP_PROMPT_TEMPLATE.format(metadata=metadata_str, trace_summary=trace_str)

        logger.info("Starting WebChatClient for AI writeup generation (%s/%s)...", self.provider, self.model)
        await self.client.start()
        try:
            response = await self.client.send_and_receive(prompt)
            response = response.strip()
            if response.startswith("```markdown"):
                response = response[11:]
            if response.endswith("```"):
                response = response[:-3]
            return response.strip()
        finally:
            await self.client.stop()
