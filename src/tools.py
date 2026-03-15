import os
import git
from pathlib import Path
from src.notion import notion_mcp
from src.serena import serena_mcp

REPO_PATH = os.getenv("REPO_PATH", "/root/agent-serve")


def git_pull() -> str:
    try:
        repo = git.Repo(REPO_PATH)
        result = repo.remotes.origin.pull()
        return f"git pull OK: {result[0].commit.hexsha[:7]}"
    except Exception as e:
        return f"git pull error: {e}"


def git_push(message: str) -> str:
    try:
        repo = git.Repo(REPO_PATH)
        repo.git.add(A=True)
        if not repo.index.diff("HEAD"):
            return "Nada que commitear"
        repo.index.commit(message)
        repo.remotes.origin.push()
        return f"git push OK: {message}"
    except Exception as e:
        return f"git push error: {e}"


def create_spec(title: str, content: str) -> str:
    try:
        specs_dir = Path(REPO_PATH) / "specs"
        specs_dir.mkdir(exist_ok=True)
        filename = title.lower().replace(" ", "-") + ".md"
        filepath = specs_dir / filename
        filepath.write_text(f"# {title}\n\n{content}\n")
        return f"Spec creada: specs/{filename}"
    except Exception as e:
        return f"Error creando spec: {e}"


def read_file(path: str) -> str:
    try:
        return Path(path).read_text()
    except Exception as e:
        return f"Error leyendo archivo: {e}"


def write_file(path: str, content: str) -> str:
    try:
        Path(path).write_text(content)
        return f"Archivo escrito: {path}"
    except Exception as e:
        return f"Error escribiendo archivo: {e}"


# Tool definitions para LiteLLM
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "git_pull",
            "description": "Hace git pull del repositorio",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_push",
            "description": "Hace git add, commit y push",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string", "description": "Mensaje del commit"}},
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_spec",
            "description": "Crea un archivo de especificación en la carpeta specs/",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Lee el contenido de un archivo",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Escribe contenido en un archivo",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
]

def notion_tool(tool_name: str, arguments: dict) -> str:
    return notion_mcp.call_tool(tool_name, arguments)


def serena_tool(tool_name: str, arguments: dict) -> str:
    return serena_mcp.call_tool(tool_name, arguments)


def _load_mcp_tools(mcp_client):
    tools, names = [], []
    try:
        for t in mcp_client.list_tools():
            tools.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
                },
            })
            names.append(t["name"])
    except Exception:
        pass
    return tools, names


_notion_tools, _notion_tool_names = _load_mcp_tools(notion_mcp)
_serena_tools, _serena_tool_names = _load_mcp_tools(serena_mcp)

TOOLS = TOOLS + _notion_tools + _serena_tools

TOOL_MAP = {
    "git_pull": lambda args: git_pull(),
    "git_push": lambda args: git_push(args["message"]),
    "create_spec": lambda args: create_spec(args["title"], args["content"]),
    "read_file": lambda args: read_file(args["path"]),
    "write_file": lambda args: write_file(args["path"], args["content"]),
    **{name: lambda args, n=name: notion_tool(n, args) for name in _notion_tool_names},
    **{name: lambda args, n=name: serena_tool(n, args) for name in _serena_tool_names},
}
