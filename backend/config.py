"""Application settings from .env file + environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Local tasks
    # Directory containing task/<challenge>/README.md folders.
    tasks_dir: str = "task"
    # Runtime directory where task folders are normalized into metadata.yml + distfiles/.
    local_challenges_dir: str = "challenges-local"

    # Chat web
    # Browser user-data-dir that is already logged into the chat web accounts.
    webchat_browser_user_data_dir: str = ""
    # Browser profile inside the user-data-dir, for example Default or Profile 1.
    webchat_browser_profile: str = ""
    # Run browser in headless mode. Keep false while developing DOM selectors.
    webchat_headless: bool = False
    # Max chat/tool actions a solver may take before yielding back to swarm bump logic.
    webchat_max_steps_per_run: int = 25
    # Debug terminal mode. True keeps verbose logs; false uses the normal Rich terminal UI.
    terminal_debug: bool = False

    # Default web model toggles
    # Enable ChatGPT web solver profile for o3 with Medium intelligence.
    enable_chatgpt_o3_medium: bool = True
    # Enable ChatGPT web solver profile for GPT-5.5 with High intelligence.
    enable_chatgpt_gpt55_high: bool = True
    # Enable ChatGPT web solver profile for GPT-5.4 with High intelligence.
    enable_chatgpt_gpt54_high: bool = True

    # Infra
    # Docker image used for each solver sandbox.
    sandbox_image: str = "ctf-sandbox"
    # Max tasks/challenges solved in parallel. Containers ~= this value * enabled models.
    max_concurrent_challenges: int = 1
    # Memory limit per solver Docker container, e.g. 4g, 8192m.
    container_memory_limit: str = "4g"
    # CPU limit per solver Docker container. Supports fractional values like 0.5.
    container_cpu_limit: float = 2.0

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
