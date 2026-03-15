import os
import json
import subprocess
import threading


class NotionMCP:
    """Cliente simple para Notion MCP via stdio."""

    def __init__(self):
        self.api_key = os.getenv("NOTION_API_KEY")
        self._proc = None
        self._lock = threading.Lock()
        self._msg_id = 0

    def _start(self):
        if self._proc and self._proc.poll() is None:
            return
        self._proc = subprocess.Popen(
            ["notion-mcp-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "OPENAPI_MCP_HEADERS": json.dumps({
                "Authorization": f"Bearer {self.api_key}",
                "Notion-Version": "2022-06-28",
            })},
            text=True,
        )
        # Inicializar protocolo MCP
        self._send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "agent-serve", "version": "1.0"},
        }})

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
                result = self._send({
                    "jsonrpc": "2.0",
                    "id": self._msg_id,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                })
                content = result.get("result", {}).get("content", [])
                return content[0].get("text", str(result)) if content else str(result)
            except Exception as e:
                return f"Notion MCP error: {e}"

    def list_tools(self) -> list:
        with self._lock:
            try:
                self._start()
                self._msg_id += 1
                result = self._send({
                    "jsonrpc": "2.0",
                    "id": self._msg_id,
                    "method": "tools/list",
                    "params": {},
                })
                return result.get("result", {}).get("tools", [])
            except Exception as e:
                return []


notion_mcp = NotionMCP()
