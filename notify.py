"""
Push (ntfy) and Windows desktop notifications for bid_sniper log records.

A log record is sent as a notification when it carries the extras built by
BidSniper.notify_extra (notify=True, plus a title, item URL and priority),
or when it is ERROR or worse.

Delivery runs on a background thread (QueueHandler + QueueListener), so a slow
or unreachable notification service can never delay a bid.

Config (config.json):
    "notifications": {
        "ntfy_server": "https://ntfy.sh",
        "ntfy_topic": "",            # empty disables phone push
        "windows_toast": true        # ignored on non-Windows
    }
"""

import logging
import os
import queue
import subprocess
import sys
from logging.handlers import QueueHandler, QueueListener
from typing import Dict, Optional

import requests

DEFAULT_NTFY_SERVER = "https://ntfy.sh"

_logger = logging.getLogger("shopgoodwill_notify")

# Shows a toast via the Windows Runtime APIs, using PowerShell's registered app ID.
# Title and body are passed through environment variables to avoid quoting issues.
_TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $template.GetElementsByTagName('text')
$texts.Item(0).AppendChild($template.CreateTextNode($env:SGW_TOAST_TITLE)) > $null
$texts.Item(1).AppendChild($template.CreateTextNode($env:SGW_TOAST_BODY)) > $null
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show([Windows.UI.Notifications.ToastNotification]::new($template))
"""


def _ascii(text: str) -> str:
    # HTTP header values must be latin-1; keep them plain ASCII to be safe
    return text.encode("ascii", "replace").decode("ascii").replace("?", "-")


def send_ntfy(
    server: str,
    topic: str,
    title: str,
    message: str,
    priority: int = 3,
    click_url: Optional[str] = None,
    timeout: float = 10,
) -> None:
    """
    Publish one message to ntfy, using header-based publishing:
    POST {server}/{topic} with the message as the body.
    """

    headers = {"X-Title": _ascii(title), "X-Priority": str(int(priority))}
    if click_url:
        headers["X-Click"] = click_url
    res = requests.post(
        f"{server.rstrip('/')}/{topic}",
        data=message.encode("utf-8"),
        headers=headers,
        timeout=timeout,
    )
    res.raise_for_status()


def send_windows_toast(title: str, message: str, timeout: float = 15) -> None:
    """
    Show a Windows toast notification. No-op on other platforms.
    """

    if sys.platform != "win32":
        return

    env = dict(os.environ, SGW_TOAST_TITLE=title, SGW_TOAST_BODY=message)
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _TOAST_PS,
        ],
        env=env,
        check=True,
        capture_output=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


class NotifyHandler(logging.Handler):
    """
    Sends qualifying log records to ntfy and/or Windows toasts.
    Meant to sit behind a QueueListener (see attach_notifiers).
    """

    def __init__(self, settings: Dict) -> None:
        super().__init__(level=logging.INFO)
        self.settings = settings  # the live "notifications" dict; edits apply immediately

    @staticmethod
    def should_notify(record: logging.LogRecord) -> bool:
        return bool(getattr(record, "notify", False)) or record.levelno >= logging.ERROR

    def emit(self, record: logging.LogRecord) -> None:
        if not self.should_notify(record):
            return

        title = getattr(record, "notify_title", None) or (
            "Sniper error" if record.levelno >= logging.ERROR else "Sniper"
        )
        message = record.getMessage()
        priority = getattr(record, "notify_priority", 4 if record.levelno >= logging.ERROR else 3)
        click_url = getattr(record, "notify_url", None)
        deliver(self.settings, title, message, priority, click_url)


def deliver(
    settings: Dict,
    title: str,
    message: str,
    priority: int = 3,
    click_url: Optional[str] = None,
) -> Dict[str, str]:
    """
    Send one notification to every enabled channel.
    Failures are logged (never raised) and reported in the return value.

    :return: {"ntfy": "sent"|"off"|"error: ...", "windows": ...}
    """

    result = {"ntfy": "off", "windows": "off"}

    topic = (settings.get("ntfy_topic") or "").strip()
    if topic:
        try:
            send_ntfy(
                settings.get("ntfy_server") or DEFAULT_NTFY_SERVER,
                topic,
                title,
                message,
                priority,
                click_url,
            )
            result["ntfy"] = "sent"
        except Exception as e:
            result["ntfy"] = f"error: {type(e).__name__}: {e}"
            _logger.error(f"ntfy notification failed - {type(e).__name__}: {e}")

    if settings.get("windows_toast", True):
        if sys.platform != "win32":
            result["windows"] = "not on Windows"
        else:
            try:
                send_windows_toast(title, message)
                result["windows"] = "sent"
            except Exception as e:
                detail = e
                if isinstance(e, subprocess.CalledProcessError):
                    detail = (e.stderr or b"").decode(errors="replace").strip()[:300]
                result["windows"] = f"error: {type(e).__name__}: {detail}"
                _logger.error(f"Windows notification failed - {detail}")

    return result


def attach_notifiers(logger: logging.Logger, config: Dict) -> QueueListener:
    """
    Route `logger`'s records through a queue to a NotifyHandler.
    Ensures config["notifications"] exists (so later edits are picked up live).

    :return: The started QueueListener (call .stop() on shutdown)
    """

    settings = config.setdefault("notifications", {})
    settings.setdefault("ntfy_server", DEFAULT_NTFY_SERVER)
    settings.setdefault("ntfy_topic", "")
    settings.setdefault("windows_toast", True)

    log_queue: "queue.Queue[logging.LogRecord]" = queue.Queue()
    logger.addHandler(QueueHandler(log_queue))
    listener = QueueListener(log_queue, NotifyHandler(settings), respect_handler_level=True)
    listener.start()
    return listener
