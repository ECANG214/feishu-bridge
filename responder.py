#!/usr/bin/env python3
"""Claude responder for the feishu-bridge.

Closes the loop the bridge leaves open:

    Feishu -> bridge.py -> inbox.json -> [THIS] -> claude -p -> outbox.json -> Feishu

Guardrails (all of them are deliberate):
  1. Group messages are handled ONLY when Claude is @-mentioned (uses the
     `mentions` list that bridge.py records). Direct messages are always handled.
  2. Bot senders are refused (`sender_type` != "user"), so two bots can never
     ping-pong each other into a loop.
  3. Single-threaded: exactly one `claude` call runs at a time, in arrival order.
  4. Stop by creating a `STOP` file next to this script (or by killing the pid).
  5. Tools default to READ-ONLY (Read,Grep,Glob). Writing/executing is opt-in via
     BRIDGE_CLAUDE_TOOLS, because anyone in the group can then drive this machine.
  6. Optional sender allowlist via BRIDGE_ALLOWED_SENDERS (comma-separated open_ids).
  7. TEAM_CONTEXT is appended to the system prompt so the bot knows about the
     shared team record (team-agent/AGENT-BRIEFING.md) before answering.

Config comes from environment variables (see start-responder-hidden.vbs) with the
defaults below. Every reply and every failure is logged to logs/responder.log.
"""

import json
import os
import subprocess
import sys
import time
import datetime
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent
INBOX = BRIDGE / "inbox.json"
OUTBOX = BRIDGE / "outbox.json"
STATE = BRIDGE / "responder_state.json"
STOP = BRIDGE / "STOP"
LOG = BRIDGE / "logs" / "responder.log"

POLL_SECONDS = float(os.environ.get("BRIDGE_POLL_SECONDS", "3"))
CLAUDE_BIN = os.environ.get("BRIDGE_CLAUDE_BIN", "claude")
MODEL = os.environ.get("BRIDGE_CLAUDE_MODEL", "haiku")          # haiku -> deepseek-flash
TOOLS = os.environ.get("BRIDGE_CLAUDE_TOOLS", "Read,Grep,Glob")  # read-only by default
MAX_TURNS = os.environ.get("BRIDGE_CLAUDE_MAX_TURNS", "8")
TIMEOUT = float(os.environ.get("BRIDGE_CLAUDE_TIMEOUT", "240"))
WORKDIR = os.environ.get("BRIDGE_CLAUDE_CWD", r"<work-dir>")
MENTION_NAMES = [n.strip().lower() for n in os.environ.get("BRIDGE_MENTION_NAMES", "claude").split(",") if n.strip()]
ALLOWED_SENDERS = [s.strip() for s in os.environ.get("BRIDGE_ALLOWED_SENDERS", "").split(",") if s.strip()]
KEEP_CONTEXT = os.environ.get("BRIDGE_KEEP_CONTEXT", "1") not in ("0", "false", "no")

SYSTEM_RULES = (
    "你正在飞书群聊里以「Claude」的身份回复消息。规则：\n"
    "1. 只输出要发到群里的回复正文，不要任何前言、后缀或解释性包装。\n"
    "2. 用中文，简洁直接；这是群聊，不要长篇大论。\n"
    "3. 你真的有工具可用：需要看文件就读，别凭空猜。\n"
    "4. 做不到的事要明确说做不到和原因，不要编造结果。\n"
    "5. 若被要求写入或执行而你没有相应工具权限，直接说明「当前只有只读权限」。"
)
TEAM_CONTEXT = (
    "你是本机多 agent 协作体系里的一员，代号「Claude」。体系现状：\n"
    "- 总控台：DSH（DeepSeek Harness）；团队前台：Hermes 5 个 profile（1号泉/2号黑塔/3号往昔/4号罅光/5号式仪）；施工队：你。\n"
    "- 三家共用同一个 DeepSeek 账号，但互不共享记忆与上下文，一切交接必须落到文件。\n"
    "你的团队资料（用 Read/Grep/Glob 自行查阅）：\n"
    "- 共享事实源（开工前先读）：C:\\team-agent\\AGENT-BRIEFING.md\n"
    "- 交接记录：C:\\team-agent\\decisions\\<日期>-session-log.md\n"
    "- 历次项目产物：C:\\team-agent\\projects\\\n"
    "回答涉及本机体系、通道、约定、历史结论的问题前，先读 AGENT-BRIEFING.md 再答，不要凭印象。\n"
    "你目前只有只读工具（Read/Grep/Glob）：不能写文件、不能执行命令。若结论需要留痕，在回复里明确写「需要人工或其它 agent 记录到黑板」。"
)



def log(msg: str) -> None:
    line = f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [Responder] {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_json(path: Path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as exc:
        log(f"WARN cannot parse {path.name}: {exc}")
        return default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def addressed_to_claude(entry: dict) -> bool:
    if entry.get("chat_type") != "group":
        return True  # DM
    names = [str(m.get("name", "")).lower() for m in (entry.get("mentions") or [])]
    if any(any(want in name for name in names) for want in MENTION_NAMES):
        return True
    if not names:  # older entries recorded before mentions existed
        return "claude" in str(entry.get("text", "")).lower()
    return False


def should_handle(entry: dict) -> tuple:
    stype = str(entry.get("sender_type", "user")).lower()
    if stype and stype != "user":
        return False, f"sender_type={stype} (bot loop guard)"
    if ALLOWED_SENDERS and entry.get("sender_id") not in ALLOWED_SENDERS:
        return False, "sender not in BRIDGE_ALLOWED_SENDERS"
    if not addressed_to_claude(entry):
        return False, "not @-mentioned"
    return True, ""


def call_claude(prompt: str, session_id: str):
    argv = [CLAUDE_BIN, "-p", prompt, "--output-format", "json",
            "--max-turns", str(MAX_TURNS), "--allowedTools", TOOLS,
            "--append-system-prompt", TEAM_CONTEXT]
    if MODEL:
        argv += ["--model", MODEL]
    if session_id and KEEP_CONTEXT:
        argv += ["--resume", session_id]
    started = time.time()
    kwargs = {}
    if os.name == "nt":
        # CREATE_NO_WINDOW: the responder runs hidden, so its child must not
        # flash a console window on the desktop.
        kwargs["creationflags"] = 0x08000000
    try:
        proc = subprocess.run(argv, cwd=WORKDIR, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=TIMEOUT, **kwargs)
    except subprocess.TimeoutExpired:
        return None, session_id, f"超时（超过 {int(TIMEOUT)} 秒）"
    except FileNotFoundError:
        return None, session_id, f"找不到 {CLAUDE_BIN} 可执行文件"
    elapsed = time.time() - started
    out = (proc.stdout or "").strip()
    if not out:
        err = (proc.stderr or "").strip().replace("\n", " ")[:300]
        return None, session_id, f"claude 没有输出（exit={proc.returncode}）{err}"
    try:
        data = json.loads(out.splitlines()[-1])
    except json.JSONDecodeError:
        return out[:4000], session_id, None
    text = (data.get("result") or "").strip()
    new_sid = data.get("session_id") or session_id
    cost = data.get("total_cost_usd")
    subtype = data.get("subtype")
    log(f"claude done in {elapsed:.1f}s subtype={subtype} turns={data.get('num_turns')} "
        f"costUSD(list)={cost}")
    if not text:
        return None, new_sid, f"claude 返回空结果（subtype={subtype}）"
    return text, new_sid, None


def main() -> None:
    if STOP.exists():
        STOP.unlink()
    state = load_json(STATE, {}) or {}
    sessions = state.get("sessions", {})
    log(f"started  poll={POLL_SECONDS}s model={MODEL} tools={TOOLS} cwd={WORKDIR} "
        f"mention_names={MENTION_NAMES} allowlist={ALLOWED_SENDERS or 'any human'}")

    while True:
        if STOP.exists():
            log("STOP file detected - exiting cleanly")
            state["sessions"] = sessions
            save_json(STATE, state)
            return

        entries = load_json(INBOX, [])
        if not isinstance(entries, list):
            entries = []
        dirty = False
        for entry in entries:
            if entry.get("processed"):
                continue
            entry["processed"] = True
            dirty = True
            mid = entry.get("id")
            try:
                ok, why = should_handle(entry)
                if not ok:
                    log(f"skip {mid}: {why}")
                    continue
                prompt = (f"{SYSTEM_RULES}\n\n--- 群消息 ---\n"
                          f"发送者: {entry.get('sender_name', '?')}\n"
                          f"内容: {entry.get('text', '')}")
                log(f"handling {mid} from {entry.get('sender_name')} "
                    f"chat={entry.get('chat_id')} text={str(entry.get('text'))[:60]!r}")
                reply, new_sid, err = call_claude(prompt, sessions.get(entry.get("chat_id", "")))
                if new_sid:
                    sessions[entry.get("chat_id", "")] = new_sid
                if err:
                    reply = f"（Claude 这边出问题了：{err}）"
                outbox = load_json(OUTBOX, [])
                if not isinstance(outbox, list):
                    outbox = []
                outbox.append({
                    "chat_id": entry.get("chat_id", ""),
                    "text": reply,
                    "reply_to": entry.get("id"),
                    "sent": False,
                })
                save_json(OUTBOX, outbox)
                log(f"queued reply for {mid} ({len(reply)} chars)")
            except Exception as exc:  # noqa: BLE001 - never die on one bad message
                log(f"ERROR handling {mid}: {type(exc).__name__}: {exc}")
        if dirty:
            save_json(INBOX, entries)
            state["sessions"] = sessions
            state["updated_at"] = datetime.datetime.now().isoformat()
            save_json(STATE, state)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(0)
