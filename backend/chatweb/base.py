"""Provider-agnostic Chrome chat web client."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from backend.chatweb.chatgpt import ChatGPTWebAutomation
from backend.chatweb.parser import ChatWebProvider


PROVIDER_AUTOMATION = {
    "chatgpt": ChatGPTWebAutomation,
}


class WebChatClient:
    """Provider-agnostic Chrome wrapper.

    Browser lifecycle is fixed to undetected_chromedriver + Chrome. Provider-specific
    DOM selectors and UI flows live in `chatgpt.py`.
    """

    def __init__(
        self,
        provider: ChatWebProvider,
        model: str,
        user_data_dir: str = "",
        profile_directory: str = "",
        headless: bool = False,
    ) -> None:
        self.provider = provider
        self.model = model
        self.user_data_dir = user_data_dir
        self.profile_directory = profile_directory
        self.headless = headless
        self.driver: Any = None
        self._temp_dir: Any = None
        self._cloned_user_data_dir: str = ""
        automation_cls = PROVIDER_AUTOMATION.get(provider)
        if automation_cls is None:
            raise ValueError(f"Unsupported chat web provider: {provider}")
        self.automation = automation_cls(self)

    def _resolved_user_data_dir(self) -> str:
        """Return an absolute browser profile path for Chrome."""
        if not self.user_data_dir:
            return ""
        return str(Path(self.user_data_dir).expanduser().resolve())

    def _apply_common_browser_options(self, options: Any) -> None:
        user_data_dir = getattr(self, "_cloned_user_data_dir", "") or self._resolved_user_data_dir()
        if user_data_dir:
            options.add_argument(f"--user-data-dir={user_data_dir}")
        if self.profile_directory:
            options.add_argument(f"--profile-directory={self.profile_directory}")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        if self.headless:
            options.add_argument("--headless=new")

    async def start(self) -> None:
        await asyncio.to_thread(self._start_sync)

    async def stop(self) -> None:
        if self.driver:
            await asyncio.to_thread(self.driver.quit)
            self.driver = None
        
        if getattr(self, "_temp_dir", None):
            try:
                await asyncio.to_thread(self._temp_dir.cleanup)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning("Failed to cleanup temp Chrome profile: %s", e)
            self._temp_dir = None

    async def send_and_receive(self, message: str) -> str:
        return await asyncio.to_thread(self._send_and_receive_sync, message)

    async def send_files_and_receive(self, message: str, file_paths: list[str]) -> str:
        return await asyncio.to_thread(self._send_files_and_receive_sync, message, file_paths)

    def _start_sync(self) -> None:
        self._clone_profile()
        self.driver = self._start_chrome()
        self.open_chat()
        self.select_model(self.model)

    def _clone_profile(self) -> None:
        original = self._resolved_user_data_dir()
        if not original:
            self._cloned_user_data_dir = ""
            return
        
        orig_path = Path(original)
        if not orig_path.exists():
            self._cloned_user_data_dir = str(orig_path)
            return

        import tempfile
        import shutil
        import logging
        
        logger = logging.getLogger(__name__)
        self._temp_dir = tempfile.TemporaryDirectory(prefix=f"ctf-agent-chrome-{self.provider}-")
        temp_path = Path(self._temp_dir.name)
        
        logger.info("Cloning Chrome profile from %s to %s...", orig_path.name, temp_path.name)
        shutil.copytree(
            orig_path,
            temp_path,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(
                "*Cache*", "CacheStorage", "Crashpad", "History*", "Downloads", "Safe Browsing", "Service Worker", "Code Cache"
            )
        )
        self._cloned_user_data_dir = str(temp_path)

    def _start_chrome(self):
        try:
            import undetected_chromedriver as uc
        except ImportError as e:
            if getattr(e, "name", "") == "distutils":
                raise RuntimeError(
                    "undetected_chromedriver is installed, but Python cannot import "
                    "`distutils`. This package still depends on distutils on newer "
                    "Python versions. Run `uv sync` to install the setuptools "
                    "compatibility dependency."
                ) from e
            raise RuntimeError(
                "undetected_chromedriver is not installed. Run `uv sync` to install "
                "project dependencies."
            ) from e

        options = uc.ChromeOptions()
        self._apply_common_browser_options(options)
        try:
            return uc.Chrome(options=options, use_subprocess=True)
        except Exception as e:
            hint = (
                " Make sure Chrome is installed and fully updated. If the driver cache is stale, "
                "clear the undetected_chromedriver cache and run again."
            )
            raise RuntimeError(f"Could not start undetected Chrome: {e}.{hint}") from e

    def open_chat(self) -> None:
        if not self.driver:
            raise RuntimeError("Browser is not started.")
        self.driver.get(self.automation.url)

    def select_model(self, model: str) -> None:
        self.automation.select_model(model)

    def send_message(self, message: str) -> None:
        self.automation.send_message(message)

    def wait_for_response(self) -> str:
        return self.automation.wait_for_response()

    def _send_and_receive_sync(self, message: str) -> str:
        self.send_message(message)
        return self.wait_for_response()

    def upload_files(self, file_paths: list[str]) -> None:
        missing = [p for p in file_paths if not Path(p).exists()]
        if missing:
            raise FileNotFoundError(f"Upload file(s) not found: {', '.join(missing)}")
        self.automation.upload_files(file_paths)

    def _send_files_and_receive_sync(self, message: str, file_paths: list[str]) -> str:
        self.upload_files(file_paths)
        self.send_message(message)
        return self.wait_for_response()
