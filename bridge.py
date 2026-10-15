"""
Feishu <=> Claude Code Bridge

Connects to Feishu via WebSocket, routes messages through file-based
inbox/outbox protocol that Claude Code reads/writes.

Usage:
    python bridge.py              Start bridge (foreground)
    python bridge.py --once       Send pending outbox messages, then exit
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

BRIDGE_DIR = Path(__file__).parent.resolve()
INBOX_PATH = BRIDGE_DIR / "inbox.json"
OUTBOX_PATH = BRIDGE_DIR / "outbox.json"
STATE_PATH = BRIDGE_DIR / "bridge_state.json"

# A clean clone has no logs/ directory, and logging.FileHandler opens the file at
# import time - so create the directory first or `python bridge.py` dies before it
# starts (previously only start-bridge.bat created it).
(BRIDGE_DIR / "logs").mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Bridge] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(BRIDGE_DIR / "logs" / "bridge.log", encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger("feishu-bridge")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    import yaml
    cfg_path = BRIDGE_DIR / "config.yaml"
    if not cfg_path.exists():
        logger.error("config.yaml not found at %s", cfg_path)
        sys.exit(1)
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Atomic file I/O
# ---------------------------------------------------------------------------

def atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")  # inbox.json -> inbox.json.tmp (matches responder.py)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_json_array(path: Path) -> list:
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_json_array(path: Path, data: list) -> None:
    atomic_write_json(path, data)


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------

MAX_INBOX_SIZE = 500

def write_inbox(message: dict) -> None:
    inbox = load_json_array(INBOX_PATH)
    inbox.append(message)
    if len(inbox) > MAX_INBOX_SIZE:
        inbox = inbox[-MAX_INBOX_SIZE:]
    save_json_array(INBOX_PATH, inbox)
    logger.info("Inbox: message from %s (%d chars)", (message.get("sender_name") or message.get("sender_id") or "?"), len(message.get("text", "")))


# ---------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------

def load_outbox() -> list:
    return load_json_array(OUTBOX_PATH)


def save_outbox(data: list) -> None:
    save_json_array(OUTBOX_PATH, data)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"seen_ids": []}
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"seen_ids": []}


def save_state(state: dict) -> None:
    atomic_write_json(STATE_PATH, state)


# ---------------------------------------------------------------------------
# Feishu Client
# ---------------------------------------------------------------------------

def build_lark_client(cfg: dict):
    import lark_oapi as lark
    from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN

    domain = FEISHU_DOMAIN if cfg.get("domain", "feishu") == "feishu" else LARK_DOMAIN
    return (
        lark.Client.builder()
        .app_id(cfg["app_id"])
        .app_secret(cfg["app_secret"])
        .domain(domain)
        .log_level(lark.LogLevel.WARNING)
        .build()
    )


def send_message(client, chat_id: str, text: str, reply_to: Optional[str] = None) -> bool:
    """Send a text message. Returns True on success."""
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody, ReplyMessageRequest, ReplyMessageRequestBody

    content = json.dumps({"text": text}, ensure_ascii=False)
    msg_uuid = str(uuid.uuid4())

    try:
        if reply_to:
            body = (
                ReplyMessageRequestBody.builder()
                .content(content)
                .msg_type("text")
                .uuid(msg_uuid)
                .build()
            )
            req = (
                ReplyMessageRequest.builder()
                .message_id(reply_to)
                .request_body(body)
                .build()
            )
            resp = client.im.v1.message.reply(req)
        else:
            body = (
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(content)
                .uuid(msg_uuid)
                .build()
            )
            req = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(body)
                .build()
            )
            resp = client.im.v1.message.create(req)

        if resp.success():
            logger.info("Sent message to %s%s", chat_id, f" (reply to {reply_to})" if reply_to else "")
            return True
        else:
            logger.error("Send failed: code=%s msg=%s", resp.code, resp.msg)
            return False
    except Exception as e:
        logger.error("Send error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Message parsing
# ---------------------------------------------------------------------------

def extract_text(message: Any) -> str:
    """Extract plain text from a Feishu message object."""
    try:
        content_str = message.content or "{}"
        content = json.loads(content_str) if isinstance(content_str, str) else content_str
    except json.JSONDecodeError:
        return "[无法解析的消息]"

    msg_type = message.message_type or "unknown"

    if msg_type == "text":
        return content.get("text", "")

    if msg_type == "post":
        parts = []
        post = content.get("zh_cn") or content.get("content") or {}
        if isinstance(post, dict):
            for row in post.get("content", []):
                if isinstance(row, list):
                    for el in row:
                        if isinstance(el, dict):
                            parts.append(el.get("text", ""))
        return "\n".join(parts) if parts else "[富文本消息]"

    if msg_type == "image":
        return f"[图片: {content.get('image_key', '')}]"
    if msg_type == "file":
        return f"[文件: {content.get('file_name', '')}]"
    if msg_type == "audio":
        return f"[语音消息]"
    if msg_type == "media":
        return f"[媒体消息]"
    if msg_type == "sticker":
        return "[表情]"

    return f"[{msg_type} 消息]"


# ---------------------------------------------------------------------------
# WebSocket event handler
# ---------------------------------------------------------------------------

class BridgeEventHandler:
    """Handles Feishu WebSocket events, writing to inbox."""

    def __init__(self, client):
        self.client = client
        self.state = load_state()

    def _is_duplicate(self, message_id: str) -> bool:
        if message_id in self.state.get("seen_ids", []):
            return True
        self.state.setdefault("seen_ids", []).append(message_id)
        # Keep last 2048 IDs
        if len(self.state["seen_ids"]) > 2048:
            self.state["seen_ids"] = self.state["seen_ids"][-2048:]
        save_state(self.state)
        return False

    def on_message(self, data) -> bool:
        try:
            event = data.event
            message = event.message
            sender = event.sender

            if not message or not sender:
                return False

            message_id = getattr(message, "message_id", None)
            if not message_id or self._is_duplicate(message_id):
                return False

            raw_sender_id = getattr(sender.sender_id, "open_id", None)
            sender_id = str(raw_sender_id) if raw_sender_id else ""
            chat_id = str(getattr(message, "chat_id", "") or "")
            chat_type = str(getattr(message, "chat_type", "p2p") or "p2p")
            parent_id = str(getattr(message, "parent_id", None) or "")
            thread_id = str(getattr(message, "thread_id", None) or "")
            root_id = str(getattr(message, "root_id", None) or "")

            text = str(extract_text(message))

            # Don't echo our own messages.
            # NOTE: sender_id is an open_id (ou_...) and can never equal an app_id
            # (cli_...), so the old comparison against an app_id literal was dead
            # code. The real gate is sender_type; FEISHU_BOT_OPEN_ID makes the
            # "not our own post" intent explicit for apps that report their own
            # messages as sender_type == "user".
            own_open_id = os.environ.get("FEISHU_BOT_OPEN_ID", "").strip()
            if "bot" in str(getattr(sender, "sender_type", "")).lower() or (own_open_id and sender_id == own_open_id):
                return False

            reply_to = parent_id or thread_id or root_id or None
            if reply_to is not None:
                reply_to = str(reply_to)

            # Record who was @-mentioned and the sender kind. In group text Feishu
            # only leaves placeholders ("@_user_1"), so a responder cannot tell
            # whether it was addressed without the mentions list. sender_type lets
            # the responder refuse bot-to-bot chatter (loop guard).
            mentions = []
            for m in (getattr(message, "mentions", None) or []):
                try:
                    mid = getattr(m, "id", None)
                    mentions.append({
                        "key": str(getattr(m, "key", "") or ""),
                        "name": str(getattr(m, "name", "") or ""),
                        "open_id": str(getattr(mid, "open_id", "") or "") if mid else "",
                    })
                except Exception:
                    continue
            sender_type = str(getattr(sender, "sender_type", "") or "")

            entry = {
                "id": str(message_id),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "sender_id": sender_id,
                "sender_name": "",
                "sender_type": sender_type,
                "chat_id": chat_id,
                "chat_type": chat_type,
                "text": text,
                "mentions": mentions,
                "reply_to": reply_to if reply_to else None,
                "processed": False,
            }
            write_inbox(entry)
            return True
        except Exception as e:
            logger.error("Error handling message: %s", e, exc_info=True)
            return False


# ---------------------------------------------------------------------------
# Outbox poller
# ---------------------------------------------------------------------------

MAX_SEND_ATTEMPTS = 5


async def poll_outbox(client, interval: float = 1.0):
    """Periodically check outbox.json and send pending messages.

    Gives up after MAX_SEND_ATTEMPTS failed sends for one entry (e.g. when
    ``reply_to`` points at a message id Feishu no longer accepts). Without this
    cap a single bad entry retried forever, once per second.
    """
    logger.info("Outbox poller started (interval=%.1fs, max attempts=%d)",
                interval, MAX_SEND_ATTEMPTS)
    while True:
        try:
            outbox = load_outbox()
            modified = False
            for entry in outbox:
                if entry.get("sent", False):
                    continue
                if entry.get("attempts", 0) >= MAX_SEND_ATTEMPTS:
                    entry["sent"] = True
                    entry["status_note"] = f"gave up after {MAX_SEND_ATTEMPTS} failed sends"
                    entry["failed_at"] = datetime.now(timezone.utc).isoformat()
                    modified = True
                    logger.error("Outbox: giving up on entry reply_to=%s after %d attempts",
                                 entry.get("reply_to"), MAX_SEND_ATTEMPTS)
                    continue
                success = send_message(
                    client,
                    entry.get("chat_id", ""),
                    entry.get("text", ""),
                    entry.get("reply_to"),
                )
                if success:
                    entry["sent"] = True
                    entry["sent_at"] = datetime.now(timezone.utc).isoformat()
                else:
                    entry["attempts"] = entry.get("attempts", 0) + 1
                modified = True
            if modified:
                save_outbox(outbox)
        except Exception as e:
            logger.error("Outbox poll error: %s", e)
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# WebSocket runner (in thread)
# ---------------------------------------------------------------------------

def run_ws_client(cfg: dict, handler: BridgeEventHandler):
    """Run Feishu WebSocket client in a dedicated thread with its own event loop.

    Pattern mirrors _run_official_feishu_ws_client in
    gateway/platforms/feishu.py line 1279.
    """
    import lark_oapi as lark
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
    import lark_oapi.ws.client as _ws_client_module

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # The lark SDK stores its own reference to the event loop; we must
    # patch it in this thread so ws_client.start() finds the right loop.
    _ws_client_module.loop = loop

    try:
        encrypt_key = cfg.get("encrypt_key", "")
        verification_token = cfg.get("verification_token", "")
        dispatcher = (
            EventDispatcherHandler.builder(encrypt_key, verification_token)
            .register_p2_im_message_receive_v1(handler.on_message)
            .build()
        )

        domain = lark.core.const.FEISHU_DOMAIN if cfg.get("domain", "feishu") == "feishu" else lark.core.const.LARK_DOMAIN

        ws_client = lark.ws.Client(
            app_id=cfg["app_id"],
            app_secret=cfg["app_secret"],
            domain=domain,
            event_handler=dispatcher,
            log_level=lark.LogLevel.INFO,
        )

        logger.info("Feishu WebSocket client starting (app_id=%s...)", cfg["app_id"][:8])
        ws_client.start()
    except Exception as e:
        logger.error("WebSocket client error: %s", e, exc_info=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main_async(cfg: dict):
    client = build_lark_client(cfg)
    logger.info("Bridge initialized")

    handler = BridgeEventHandler(client)

    # Start WebSocket in background thread
    import threading
    ws_thread = threading.Thread(
        target=run_ws_client,
        args=(cfg, handler),
        daemon=True,
        name="feishu-ws",
    )
    ws_thread.start()
    logger.info("WebSocket thread started")

    # Poll outbox in the main event loop
    await poll_outbox(client)


def main():
    cfg = load_config()
    if not cfg.get("app_id") or not cfg.get("app_secret"):
        logger.error("app_id and app_secret are required in config.yaml")
        sys.exit(1)

    if "--once" in sys.argv:
        client = build_lark_client(cfg)
        outbox = load_outbox()
        if not outbox:
            logger.info("Outbox is empty, nothing to send")
            return
        pending = [e for e in outbox if not e.get("sent")]
        if not pending:
            logger.info("All %d outbox entries are already sent", len(outbox))
            return
        logger.info("Sending %d pending messages", len(pending))
        for entry in pending:
            send_message(client, entry.get("chat_id", ""), entry.get("text", ""), entry.get("reply_to"))
            entry["sent"] = True
            entry["sent_at"] = datetime.now(timezone.utc).isoformat()
        save_outbox(outbox)
        logger.info("Done")
        return

    logger.info("=== Feishu Bridge Starting ===")
    logger.info("App ID: %s...", cfg["app_id"][:8])
    logger.info("Inbox:  %s", INBOX_PATH)
    logger.info("Outbox: %s", OUTBOX_PATH)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Handle Ctrl+C gracefully
    try:
        import signal
        if hasattr(signal, "SIGINT"):
            def _shutdown(*_):
                logger.info("Shutting down...")
                loop.stop()
            signal.signal(signal.SIGINT, _shutdown)
    except Exception:
        pass  # signal may not work on all Windows setups

    try:
        loop.run_until_complete(main_async(cfg))
    except KeyboardInterrupt:
        logger.info("Bridge stopped")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
