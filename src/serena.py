import json
import os
import subprocess
import threading

from src.workspace_context import get_active_repo_path

UV_PATH = os.path.expanduser("~/.local/bin/uvx")


class SerenaMCP:
    """Cliente para Serena MCP via stdio."""

    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()
        self._msg_id = 0
        self._project_path = None

    def _start(self):
        project_path = str(get_active_repo_path())
        if self._proc and self._proc.poll() is None and self._project_path == project_path:
            return
        self._stop()
        self._project_path = project_path
        self._proc = subprocess.Popen(
            [UV_PATH, "--from", "git+https://github.com/oraios/serena", "serena", "start-mcp-server", "--project", project_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._send(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "agent-serve", "version": "1.0"},
                },
            }
        )

    def _stop(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        self._proc = None

    def _send(self, payload: dict) -> dict:
        line = json.dumps(payload) + "\n"
        self._proc.stdin.write(line)
        self._proc.stdin.flush()
        response = self._proc.stdout.readline()
        return json.loads(response) if response else {}

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        with self._lock:
            try:
                self._start()
                self._msg_id += 1
                result = self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": self._msg_id,
                        "method": "tools/call",
                        "params": {"name": tool_name, "arguments": arguments},
                    }
                )
                content = result.get("result", {}).get("content", [])
                return content[0].get("text", str(result)) if content else str(result)
            except Exception as e:
                return f"Serena MCP error: {e}"

    def list_tools(self) -> list:
        with self._lock:
            try:
                self._start()
                self._msg_id += 1
                result = self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": self._msg_id,
                        "method": "tools/list",
                        "params": {},
                    }
                )
                return result.get("result", {}).get("tools", [])
            except Exception:
                return []


serena_mcp = SerenaMCP()
