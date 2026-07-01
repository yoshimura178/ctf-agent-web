"""ChatGPT web DOM automation stubs."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


logger = logging.getLogger(__name__)

CHATGPT_MODEL_SELECTOR_TIMEOUT_SECONDS = 20.0
CHATGPT_MODEL_SELECTOR_RETRY_INTERVAL_SECONDS = 0.5
CHATGPT_RESPONSE_START_TIMEOUT_SECONDS = 30.0
CHATGPT_RESPONSE_DONE_TIMEOUT_SECONDS = 600.0
CHATGPT_RESPONSE_POLL_SECONDS = 0.5
CHATGPT_UPLOAD_INPUT_TIMEOUT_SECONDS = 10.0
CHATGPT_UPLOAD_READY_TIMEOUT_SECONDS = 45.0
CHATGPT_RUNNING_ICON = "#bbf3a9"
CHATGPT_IDLE_ICON = "#28eb3d"


CHATGPT_MODEL_PROFILES = {
    "o3-medium": {"model": "o3", "intelligence": "Medium"},
    "gpt-5.5-high": {"model": "GPT-5.5", "intelligence": "High"},
    "gpt-5.4-high": {"model": "GPT-5.4", "intelligence": "High"},
}


CHATGPT_SELECTORS = {
    # From the provided composer DOM.
    "composer": "div#prompt-textarea[contenteditable='true'][role='textbox']",
    "composer_placeholder": "p[data-empty-paragraph='true'][data-placeholder='Ask anything']",
    "fallback_textarea": "textarea[name='prompt-textarea']",
    "file_input": "input#upload-files, input[type='file']",
    "plus_button": "button[data-testid='composer-plus-btn'], button#composer-plus-btn",
    "upload_menu_items": (
        "[role='menuitem'], [role='option'], button, div[role='button'], "
        "[data-radix-collection-item], .__menu-item"
    ),
    "attachment_candidates": (
        "[data-testid*='attachment'], [data-testid*='file'], "
        "[class*='attachment'], [class*='file-preview'], [class*='upload']"
    ),
    "upload_busy_candidates": (
        "[aria-busy='true'], [aria-label*='Uploading'], [aria-label*='uploading'], "
        "[data-testid*='uploading'], [class*='uploading'], progress"
    ),
    # Provider/model picker selectors are intentionally broad until the real
    # picker/menu DOM is captured. The code filters these candidates by text and
    # aria/data attributes before clicking.
    "model_button_candidates": [
        "button.__composer-pill[aria-haspopup='menu']",
        "button[data-testid*='model']",
        "button[aria-label*='model']",
        "button[aria-haspopup='menu']",
    ],
    "intelligence_picker_content": "[data-testid='composer-intelligence-picker-content']",
    "model_submenu_trigger": (
        "[data-testid='composer-intelligence-picker-content'] "
        "[role='menuitem'][aria-haspopup='menu'], "
        "[data-testid='composer-intelligence-picker-content'] [data-has-submenu]"
    ),
    "intelligence_items": (
        "[data-testid='composer-intelligence-picker-content'] "
        "[role='menuitemradio']"
    ),
    "model_menu_items": (
        "[role='menuitemradio'], [role='menuitem'], [role='option'], "
        "button, div[role='button'], [data-radix-collection-item]"
    ),
    "assistant_messages": "[data-message-author-role='assistant']",
    "assistant_message_candidates": [
        "[data-message-author-role='assistant']",
        "article [data-message-author-role='assistant']",
        "[data-testid^='conversation-turn-'] [data-message-author-role='assistant']",
        ".markdown.prose",
        ".markdown",
    ],
    "streaming_indicator": "[data-testid='stop-button'], button[aria-label*='Stop']",
    "send_button": "button[data-testid='send-button'], button[aria-label*='Send']",
}


MODEL_BUTTON_HINTS = (
    "high",
    "medium",
    "instant",
    "model",
    "gpt",
    "chatgpt",
    "o3",
    "o4",
    "4o",
    "5",
)


class ChatGPTWebAutomation:
    url = "https://chatgpt.com/"

    def __init__(self, client: Any) -> None:
        self.client = client
        self._assistant_count_before_send = 0

    def select_model(self, model: str) -> None:
        """Best-effort ChatGPT model selection.

        The exact ChatGPT model picker DOM changes frequently. This method
        isolates the ChatGPT-specific flow and uses broad selectors plus text
        filtering. If no reliable picker is found, it leaves the current model
        unchanged instead of failing the whole solver.
        """
        driver = self._driver()
        profile = CHATGPT_MODEL_PROFILES.get(model, {"model": model, "intelligence": ""})
        model_label = profile["model"]
        intelligence_label = profile.get("intelligence", "")
        selected_model = False
        selected_intelligence = False

        self._wait_for_chat_ready()
        button = self._retry_find_model_button(model_label)
        if button is None:
            logger.warning(
                "ChatGPT model selector not found for %s after %.1fs. Keeping current UI model.",
                model_label,
                CHATGPT_MODEL_SELECTOR_TIMEOUT_SECONDS,
            )
            return

        self._click(button)
        self._open_model_submenu()
        item = self._find_menu_item(model_label)
        if item is None:
            logger.warning(
                "ChatGPT model menu opened but item %s was not found. Keeping current UI model.",
                model_label,
            )
        else:
            self._click(item)
            selected_model = True

        if intelligence_label:
            button = self._retry_find_model_button(intelligence_label)
            if button is None:
                logger.warning(
                    "ChatGPT intelligence selector not found for %s after %.1fs.",
                    intelligence_label,
                    CHATGPT_MODEL_SELECTOR_TIMEOUT_SECONDS,
                )
            else:
                self._click(button)
                selected_intelligence = self._select_intelligence(intelligence_label)

        if selected_model or selected_intelligence:
            try:
                WebDriverWait(driver, 5).until(
                    lambda _: (
                        self._visible_button_contains(model_label)
                        or self._visible_button_contains(intelligence_label)
                    )
                )
            except TimeoutException:
                logger.debug(
                    "ChatGPT profile selection did not visibly confirm: %s/%s",
                    model_label,
                    intelligence_label,
                )

    def send_message(self, message: str) -> None:
        """Send a message through ChatGPT's ProseMirror composer."""
        driver = self._driver()
        composer = WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, CHATGPT_SELECTORS["composer"]))
        )
        self._assistant_count_before_send = len(self._assistant_messages())

        placeholder = self._first_visible(CHATGPT_SELECTORS["composer_placeholder"])
        if placeholder is not None:
            self._click(placeholder)
        else:
            self._click(composer)

        self._replace_composer_text(composer, message)
        ActionChains(driver).send_keys(Keys.ENTER).perform()

    def wait_for_response(self) -> str:
        """Wait until ChatGPT finishes streaming and return the latest assistant text."""
        started = self._wait_until(
            lambda: (
                self._is_running()
                or len(self._assistant_messages()) > self._assistant_count_before_send
            ),
            CHATGPT_RESPONSE_START_TIMEOUT_SECONDS,
        )
        if not started:
            raise TimeoutError("Timed out waiting for ChatGPT response to start.")
        finished = self._wait_until(
            lambda: (
                not self._is_running()
                and len(self._assistant_messages()) > self._assistant_count_before_send
                and (self._is_idle() or not self._visible_svg_use_contains(CHATGPT_RUNNING_ICON))
            ),
            CHATGPT_RESPONSE_DONE_TIMEOUT_SECONDS,
        )
        if not finished:
            raise TimeoutError("Timed out waiting for ChatGPT response to finish.")

        response = self._latest_assistant_text()
        if not response:
            # Some ChatGPT DOM updates text just after the button returns to idle.
            self._wait_until(lambda: bool(self._latest_assistant_text()), 5.0)
            response = self._latest_assistant_text()
        if not response:
            raise RuntimeError("ChatGPT response finished, but no assistant text was found.")
        return response

    def upload_files(self, file_paths: list[str]) -> None:
        """Attach local files through ChatGPT's composer.

        The visible "Add photos & files" item opens the native OS picker, which
        WebDriver cannot control reliably. The stable automation path is to find
        ChatGPT's underlying file input and send absolute file paths to it.
        """
        paths = [str(Path(path).expanduser().resolve()) for path in file_paths]
        if not paths:
            return

        self._wait_for_chat_ready()
        file_input = self._find_file_input()
        if file_input is None:
            self._open_plus_menu()
            file_input = self._find_file_input()

        if file_input is None:
            upload_item = self._find_upload_menu_item()
            if upload_item is not None:
                self._click(upload_item)
                file_input = self._wait_for_file_input(CHATGPT_UPLOAD_INPUT_TIMEOUT_SECONDS)

        if file_input is None:
            raise RuntimeError(
                "ChatGPT upload file input was not found. The native file picker cannot "
                "be automated directly; capture the post-click DOM for the hidden "
                "input[type=file] if ChatGPT changed this flow."
            )

        self._send_paths_to_file_input(file_input, paths)
        self._wait_for_upload_ready([Path(path).name for path in paths])

    def _driver(self):
        driver = self.client.driver
        if not driver:
            raise RuntimeError("Browser is not started.")
        return driver

    def _find_model_button(self, label: str):
        driver = self._driver()
        label_norm = _normalize(label)
        candidate_buttons = []
        for selector in CHATGPT_SELECTORS["model_button_candidates"]:
            candidate_buttons.extend(driver.find_elements(By.CSS_SELECTOR, selector))

        visible_buttons = [button for button in candidate_buttons if _is_visible(button)]
        for button in visible_buttons:
            text = _element_text(button)
            if label_norm and label_norm in _normalize(text):
                return button

        hinted = [
            button
            for button in visible_buttons
            if any(hint in _normalize(_element_text(button)) for hint in MODEL_BUTTON_HINTS)
        ]
        if len(hinted) == 1:
            return hinted[0]
        if len(hinted) > 1:
            logger.debug("Multiple ChatGPT model button candidates found; using first hinted button.")
            return hinted[0]
        return None

    def _retry_find_model_button(self, label: str):
        deadline = time.monotonic() + CHATGPT_MODEL_SELECTOR_TIMEOUT_SECONDS
        last_count = 0
        while time.monotonic() < deadline:
            button = self._find_model_button(label)
            if button is not None:
                return button
            last_count = self._count_model_button_candidates()
            time.sleep(CHATGPT_MODEL_SELECTOR_RETRY_INTERVAL_SECONDS)
        logger.debug("ChatGPT model button candidates visible after retry: %d", last_count)
        return None

    def _wait_for_chat_ready(self) -> None:
        driver = self._driver()
        try:
            WebDriverWait(driver, CHATGPT_MODEL_SELECTOR_TIMEOUT_SECONDS).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, CHATGPT_SELECTORS["composer"]))
            )
        except TimeoutException:
            logger.debug("ChatGPT composer not detected before model selection retry.")

    def _count_model_button_candidates(self) -> int:
        driver = self._driver()
        count = 0
        for selector in CHATGPT_SELECTORS["model_button_candidates"]:
            count += sum(
                1
                for button in driver.find_elements(By.CSS_SELECTOR, selector)
                if _is_visible(button)
            )
        return count

    def _find_menu_item(self, label: str):
        driver = self._driver()
        label_norm = _normalize(label)
        try:
            WebDriverWait(driver, 5).until(
                EC.presence_of_all_elements_located(
                    (By.CSS_SELECTOR, CHATGPT_SELECTORS["model_menu_items"])
                )
            )
        except TimeoutException:
            return None

        for item in driver.find_elements(By.CSS_SELECTOR, CHATGPT_SELECTORS["model_menu_items"]):
            if not _is_visible(item):
                continue
            if _text_matches(_element_text(item), label_norm):
                return item
        return None

    def _open_model_submenu(self) -> None:
        driver = self._driver()
        try:
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, CHATGPT_SELECTORS["intelligence_picker_content"])
                )
            )
        except TimeoutException:
            logger.debug("ChatGPT intelligence picker content was not detected.")
            return

        submenu_triggers = [
            item
            for item in driver.find_elements(
                By.CSS_SELECTOR,
                CHATGPT_SELECTORS["model_submenu_trigger"],
            )
            if _is_visible(item)
        ]
        if not submenu_triggers:
            logger.debug("ChatGPT model submenu trigger was not found.")
            return

        trigger = submenu_triggers[0]
        try:
            ActionChains(driver).move_to_element(trigger).pause(0.2).perform()
        except Exception:
            pass
        try:
            trigger.click()
        except Exception:
            driver.execute_script("arguments[0].click();", trigger)

    def _select_intelligence(self, label: str) -> bool:
        driver = self._driver()
        label_norm = _normalize(label)
        try:
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, CHATGPT_SELECTORS["intelligence_picker_content"])
                )
            )
        except TimeoutException:
            return False

        for item in driver.find_elements(By.CSS_SELECTOR, CHATGPT_SELECTORS["intelligence_items"]):
            if not _is_visible(item):
                continue
            if _text_matches(_element_text(item), label_norm):
                self._click(item)
                return True
        logger.warning("ChatGPT intelligence item not found: %s", label)
        return False

    def _visible_button_contains(self, text: str) -> bool:
        driver = self._driver()
        needle = _normalize(text)
        if not needle:
            return False
        for button in driver.find_elements(By.CSS_SELECTOR, "button"):
            if _is_visible(button) and needle in _normalize(_element_text(button)):
                return True
        return False

    def _click(self, element) -> None:
        driver = self._driver()
        try:
            element.click()
        except Exception:
            driver.execute_script("arguments[0].click();", element)

    def _find_file_input(self):
        driver = self._driver()
        for element in driver.find_elements(By.CSS_SELECTOR, CHATGPT_SELECTORS["file_input"]):
            try:
                if (element.get_attribute("type") or "").lower() == "file":
                    return element
            except Exception:
                continue
        return None

    def _wait_for_file_input(self, timeout_seconds: float):
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            file_input = self._find_file_input()
            if file_input is not None:
                return file_input
            time.sleep(CHATGPT_RESPONSE_POLL_SECONDS)
        return None

    def _open_plus_menu(self) -> None:
        plus_button = self._first_visible(CHATGPT_SELECTORS["plus_button"])
        if plus_button is None:
            logger.debug("ChatGPT composer plus button was not found.")
            return
        self._click(plus_button)
        self._wait_until(lambda: self._find_upload_menu_item() is not None, 5.0)

    def _find_upload_menu_item(self):
        driver = self._driver()
        for item in driver.find_elements(By.CSS_SELECTOR, CHATGPT_SELECTORS["upload_menu_items"]):
            try:
                if not _is_visible(item):
                    continue
                if _text_matches(_element_text(item), "add photos & files"):
                    return item
            except Exception:
                continue
        return None

    def _send_paths_to_file_input(self, file_input, paths: list[str]) -> None:
        driver = self._driver()
        try:
            driver.execute_script(
                """
                const el = arguments[0];
                el.removeAttribute('hidden');
                el.style.display = 'block';
                el.style.visibility = 'visible';
                el.style.opacity = '1';
                el.style.height = '1px';
                el.style.width = '1px';
                """,
                file_input,
            )
        except Exception:
            pass
        file_input.send_keys("\n".join(paths))

    def _wait_for_upload_ready(self, file_names: list[str]) -> None:
        attached = self._wait_until(
            lambda: (
                self._uploaded_file_names_present(file_names)
                or self._has_attachment_candidate()
            ),
            CHATGPT_UPLOAD_READY_TIMEOUT_SECONDS,
        )
        if not attached:
            raise TimeoutError(
                "Timed out waiting for ChatGPT to show the uploaded attachment. "
                "Message was not sent because upload success was not confirmed."
            )

        upload_done = self._wait_until(
            lambda: not self._has_upload_busy_indicator(),
            CHATGPT_UPLOAD_READY_TIMEOUT_SECONDS,
        )
        if not upload_done:
            raise TimeoutError(
                "Timed out waiting for ChatGPT file upload/processing to finish. "
                "Message was not sent because the file may still be uploading."
            )
        time.sleep(1.0)

    def _uploaded_file_names_present(self, file_names: list[str]) -> bool:
        driver = self._driver()
        try:
            body_text = driver.execute_script("return document.body.innerText || '';") or ""
        except Exception:
            body_text = ""
        normalized_body = _normalize(body_text)
        return all(_normalize(name) in normalized_body for name in file_names)

    def _has_attachment_candidate(self) -> bool:
        driver = self._driver()
        for element in driver.find_elements(
            By.CSS_SELECTOR,
            CHATGPT_SELECTORS["attachment_candidates"],
        ):
            try:
                if _is_visible(element):
                    return True
            except Exception:
                continue
        return False

    def _has_upload_busy_indicator(self) -> bool:
        driver = self._driver()
        for element in driver.find_elements(
            By.CSS_SELECTOR,
            CHATGPT_SELECTORS["upload_busy_candidates"],
        ):
            try:
                if _is_visible(element):
                    return True
            except Exception:
                continue

        try:
            body_text = driver.execute_script("return document.body.innerText || '';") or ""
        except Exception:
            body_text = ""
        normalized_body = _normalize(body_text)
        busy_words = (
            "uploading",
            "processing",
            "attaching",
            "dang tai",
            "dang xu ly",
            "dang dinh kem",
        )
        return any(word in normalized_body for word in busy_words)

    def _first_visible(self, selector: str):
        driver = self._driver()
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            if _is_visible(element):
                return element
        return None

    def _replace_composer_text(self, composer, message: str) -> None:
        driver = self._driver()
        driver.execute_script(
            """
            const el = arguments[0];
            const text = arguments[1];
            el.focus();
            const selection = window.getSelection();
            const range = document.createRange();
            range.selectNodeContents(el);
            selection.removeAllRanges();
            selection.addRange(range);
            document.execCommand('insertText', false, text);
            el.dispatchEvent(new InputEvent('input', {
                bubbles: true,
                inputType: 'insertText',
                data: text
            }));
            """,
            composer,
            message,
        )

    def _wait_until(self, predicate, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return True
            except Exception:
                pass
            time.sleep(CHATGPT_RESPONSE_POLL_SECONDS)
        return False

    def _is_running(self) -> bool:
        return self._visible_svg_use_contains(CHATGPT_RUNNING_ICON)

    def _is_idle(self) -> bool:
        return self._visible_svg_use_contains(CHATGPT_IDLE_ICON)

    def _visible_svg_use_contains(self, symbol: str) -> bool:
        driver = self._driver()
        for element in driver.find_elements(By.CSS_SELECTOR, "svg use"):
            try:
                href = element.get_attribute("href") or ""
                if symbol in href and _is_svg_use_visible(driver, element):
                    return True
            except Exception:
                continue
        return False

    def _assistant_messages(self) -> list[Any]:
        driver = self._driver()
        for selector in CHATGPT_SELECTORS["assistant_message_candidates"]:
            messages = [
                element
                for element in driver.find_elements(By.CSS_SELECTOR, selector)
                if _is_visible(element) and _inner_text(driver, element).strip()
            ]
            if messages:
                return messages
        return []

    def _latest_assistant_text(self) -> str:
        messages = self._assistant_messages()
        if not messages:
            return ""
        return _inner_text(self._driver(), messages[-1]).strip()


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _text_matches(text: str, normalized_label: str) -> bool:
    normalized_text = _normalize(text)
    if not normalized_label:
        return False
    return normalized_text == normalized_label or normalized_label in normalized_text


def _element_text(element) -> str:
    parts = [
        element.text or "",
        element.get_attribute("aria-label") or "",
        element.get_attribute("data-testid") or "",
        element.get_attribute("title") or "",
    ]
    return " ".join(part for part in parts if part)


def _inner_text(driver, element) -> str:
    try:
        return driver.execute_script(
            "return arguments[0].innerText || arguments[0].textContent || '';",
            element,
        ) or ""
    except Exception:
        return element.text or ""


def _is_svg_use_visible(driver, element) -> bool:
    try:
        return bool(
            driver.execute_script(
                """
                const use = arguments[0];
                const svg = use.closest('svg') || use;
                const rect = svg.getBoundingClientRect();
                const style = window.getComputedStyle(svg);
                return rect.width > 0
                    && rect.height > 0
                    && style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && style.opacity !== '0';
                """,
                element,
            )
        )
    except Exception:
        return _is_visible(element)


def _is_visible(element) -> bool:
    try:
        return element.is_displayed()
    except Exception:
        return False
