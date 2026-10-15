' Feishu Bridge - Claude responder (hidden, detached)
'
' Launch this AFTER the bridge. Window style 0 means no console window ever
' appears (launching powershell/cmd directly would flash one).
'
' Configure the placeholders below for your own deployment:
'   BRIDGE_CLAUDE_BIN      full path to the claude executable. Windows PATH lookup
'                          cannot resolve npm's claude.cmd from CreateProcess, so
'                          give the real .exe path.
'   BRIDGE_MENTION_NAMES   only these @-mentions are answered in groups
'   BRIDGE_CLAUDE_TOOLS    tool whitelist. Read-only files by default. The
'                          blackboard MCP entries below are optional - drop them
'                          if you do not run a blackboard MCP server.
'   BRIDGE_CLAUDE_CWD      working directory Claude runs in
'   BRIDGE_ALLOWED_SENDERS open_id allowlist (comma separated). Without it ANY
'                          human in the group can drive this machine.
'                          Note: open_id is per-app - the value differs for every
'                          Feishu app, so read it from the app that owns this bridge.
Option Explicit
Dim sh, env
Set sh = CreateObject("WScript.Shell")
Set env = sh.Environment("PROCESS")
env.Item("BRIDGE_CLAUDE_BIN") = "<path-to-claude.exe>"
env.Item("BRIDGE_MENTION_NAMES") = "claude"
env.Item("BRIDGE_CLAUDE_MODEL") = "haiku"
env.Item("BRIDGE_CLAUDE_TOOLS") = "Read,Grep,Glob,mcp__blackboard__blackboard_read,mcp__blackboard__blackboard_append,mcp__blackboard__blackboard_log"
env.Item("BRIDGE_CLAUDE_CWD") = "<your-working-directory>"
env.Item("BRIDGE_CLAUDE_MAX_TURNS") = "8"
env.Item("BRIDGE_CLAUDE_TIMEOUT") = "240"
env.Item("BRIDGE_POLL_SECONDS") = "3"
env.Item("BRIDGE_ALLOWED_SENDERS") = "ou_xxxxxxxxxxxxxxxx"
sh.CurrentDirectory = "<this-repo-directory>"
sh.Run """<path-to-python.exe>"" ""<this-repo-directory>\responder.py""", 0, False
