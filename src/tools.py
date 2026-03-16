import os
import git
import urllib.request
import urllib.error
import json as _json
from pathlib import Path
from src.notion import notion_mcp
from src.serena import serena_mcp
from src.memory import add_memory, search_memory, get_all_memories
from src.search import web_search
from src.database import sql_query, list_tables
from src.scheduler import schedule_task, list_tasks, remove_task
from src.git_gate import can_commit, can_push, approve_push, clear_push_approval
from src.path_sandbox import resolve_repo_path, PathSandboxError

from src.workspace_context import get_active_repo_path


def _repo() -> git.Repo:
    return git.Repo(str(get_active_repo_path()))


def git_pull() -> str:
    try:
        repo = _repo()
        branch = repo.active_branch.name
        # Si la branch tiene upstream, pull normal
        tracking = repo.active_branch.tracking_branch()
        if tracking:
            result = repo.remotes.origin.pull()
            return f"git pull OK: {result[0].commit.hexsha[:7]}"
        # Sin upstream: fetch origin y merge desde default branch
        repo.remotes.origin.fetch()
        try:
            default = repo.git.symbolic_ref("refs/remotes/origin/HEAD").split("/")[-1]
        except Exception:
            default = "main"
        repo.git.merge(f"origin/{default}", "--no-edit")
        return f"git pull OK (merged origin/{default} into {branch})"
    except Exception as e:
        return f"git pull error: {e}"


def git_create_branch(name: str) -> str:
    try:
        branch = (name or "").strip()
        if not branch:
            return "git create branch error: branch vacío"
        repo = _repo()
        if repo.is_dirty(untracked_files=True):
            return "git create branch error: repositorio sucio"
        if branch in [h.name for h in repo.heads]:
            repo.heads[branch].checkout()
            return f"git checkout OK: {branch}"
        repo.create_head(branch).checkout()
        return f"git create branch OK: {branch}"
    except Exception as e:
        return f"git create branch error: {e}"


def git_status() -> str:
    try:
        repo = _repo()
        branch = repo.active_branch.name
        changed = repo.git.status('--short')
        return f"Branch: {branch}\n{changed or 'working tree clean'}"
    except Exception as e:
        return f"git status error: {e}"


def git_diff_summary(max_files: int = 20) -> str:
    try:
        repo = _repo()
        files = repo.git.diff('--name-only').splitlines()[:max_files]
        if not files:
            return "Sin cambios en diff"
        lines = []
        for f in files:
            try:
                stat = repo.git.diff('--numstat', '--', f).strip()
                lines.append(f"- {f}: {stat or 'sin stat'}")
            except Exception:
                lines.append(f"- {f}")
        return "\n".join(lines)
    except Exception as e:
        return f"git diff summary error: {e}"


def git_commit(message: str) -> str:
    try:
        repo = _repo()
        branch = repo.active_branch.name
        ok, reason = can_commit(branch)
        if not ok:
            return reason
        repo.git.add(A=True)
        if not repo.index.diff("HEAD"):
            return "Nada que commitear"
        repo.index.commit(message)
        clear_push_approval(branch)
        return f"git commit OK: {message}"
    except Exception as e:
        return f"git commit error: {e}"


def _ensure_ssh_remote(repo) -> None:
    """Convierte la URL del remote origin a SSH si está en HTTPS."""
    url = repo.remotes.origin.url
    if url.startswith("https://github.com/"):
        ssh_url = url.replace("https://github.com/", "git@github.com:", 1)
        if not ssh_url.endswith(".git"):
            ssh_url += ".git"
        repo.remotes.origin.set_url(ssh_url)


def git_push_branch(branch: str | None = None) -> str:
    try:
        repo = _repo()
        _ensure_ssh_remote(repo)
        target = (branch or repo.active_branch.name).strip()
        ok, reason = can_push(target)
        if not ok:
            return reason
        repo.remotes.origin.push(target)
        clear_push_approval(target)
        return f"git push OK: {target}"
    except Exception as e:
        return f"git push error: {e}"


def git_approve_push(branch: str) -> str:
    return approve_push(branch)


def git_push(message: str) -> str:
    # Compatibilidad: conservar tool existente como flujo commit+push con gates.
    commit_result = git_commit(message)
    if not commit_result.startswith("git commit OK"):
        return commit_result
    return git_push_branch(None)


def create_github_pr(title: str, body: str, head: str, base: str = "main") -> dict:
    """Crea un PR en GitHub. Retorna {"url": ..., "number": ...} o {"error": ...}."""
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        return {"error": "GITHUB_TOKEN no configurado"}
    try:
        repo = _repo()
        remote_url = repo.remotes.origin.url
        # Normalizar SSH → HTTPS para parsear owner/repo
        if remote_url.startswith("git@"):
            # git@github.com:owner/repo.git → owner/repo
            path = remote_url.split(":", 1)[-1].rstrip(".git")
        else:
            path = remote_url.rstrip("/").removesuffix(".git").split("github.com/", 1)[-1]
        owner, repo_name = path.split("/", 1)

        payload = _json.dumps({"title": title, "body": body, "head": f"{owner}:{head}", "base": base}).encode()
        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo_name}/pulls",
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read())
            return {"url": data["html_url"], "number": data["number"]}
    except urllib.error.HTTPError as e:
        return {"error": f"GitHub API {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"error": str(e)}


def create_spec(title: str, content: str) -> str:
    try:
        specs_dir = get_active_repo_path() / "specs"
        specs_dir.mkdir(exist_ok=True)
        filename = title.lower().replace(" ", "-") + ".md"
        filepath = specs_dir / filename
        filepath.write_text(f"# {title}\n\n{content}\n")
        return f"Spec creada: specs/{filename}"
    except Exception as e:
        return f"Error creando spec: {e}"


def read_file(path: str) -> str:
    try:
        safe_path = resolve_repo_path(path)
        return safe_path.read_text()
    except PathSandboxError as e:
        return f"Error leyendo archivo: {e}"
    except Exception as e:
        return f"Error leyendo archivo: {e}"


def write_file(path: str, content: str) -> str:
    try:
        safe_path = resolve_repo_path(path)
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        safe_path.write_text(content)
        return f"Archivo escrito: {safe_path}"
    except PathSandboxError as e:
        return f"Error escribiendo archivo: {e}"
    except Exception as e:
        return f"Error escribiendo archivo: {e}"


# Tool definitions para LiteLLM
def codex_exec(prompt: str, writable_paths: list[str] | None = None) -> str:
    """Ejecuta una tarea de código con Codex CLI usando la sesión activa (~/.codex/auth.json)."""
    import subprocess, os, signal
    repo_path = str(get_active_repo_path())
    cmd = [
        "codex", "exec",
        "-c", "sandbox_permissions=[\"disk-full-read-access\"]",
        "-c", f'sandbox_permissions+=[{{"write-path":"{repo_path}"}}]',
        prompt,
    ]
    try:
        proc = subprocess.Popen(
            cmd, cwd=repo_path,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,  # nuevo grupo de procesos
        )
        try:
            stdout, stderr = proc.communicate(timeout=90)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait()
            return "codex exec: timeout (90s)"
        output = (stdout or "") + (stderr or "")
        return output[:2000] if output else "codex exec: sin output"
    except Exception as e:
        return f"codex exec error: {e}"


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
            "description": "Compatibilidad: commit + push (aplica approval gate)",
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
            "name": "git_create_branch",
            "description": "Crea o cambia a una branch de trabajo",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Muestra branch y estado del working tree",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff_summary",
            "description": "Resumen de archivos cambiados en diff",
            "parameters": {
                "type": "object",
                "properties": {"max_files": {"type": "integer", "default": 20}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "Realiza git add + commit (bloqueado si validation no OK)",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_push_branch",
            "description": "Hace push de una branch (requiere aprobación explícita)",
            "parameters": {
                "type": "object",
                "properties": {"branch": {"type": "string"}},
                "required": ["branch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_approve_push",
            "description": "Aprueba explícitamente push para una branch",
            "parameters": {
                "type": "object",
                "properties": {"branch": {"type": "string"}},
                "required": ["branch"],
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
    {"type": "function", "function": {"name": "add_memory", "description": "Guarda una memoria persistente sobre el usuario o proyecto", "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {"name": "search_memory", "description": "Busca memorias relevantes por query", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "get_all_memories", "description": "Lista todas las memorias guardadas", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "web_search", "description": "Busca información en internet con DuckDuckGo", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer", "default": 5}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "sql_query", "description": "Ejecuta una query SQL en la base de datos SQLite local", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "list_tables", "description": "Lista las tablas de la base de datos SQLite", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "schedule_task", "description": "Programa una tarea recurrente con expresión cron", "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}, "cron_expr": {"type": "string", "description": "5 campos: minuto hora día mes día_semana. Ej: '0 9 * * 1' = lunes 9am"}, "command": {"type": "string"}}, "required": ["task_id", "cron_expr", "command"]}}},
    {"type": "function", "function": {"name": "list_tasks", "description": "Lista las tareas programadas", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "remove_task", "description": "Elimina una tarea programada", "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {"name": "codex_exec", "description": "Ejecuta una tarea de implementación de código usando Codex CLI (sesión activa). Úsalo para subtareas de codificación complejas.", "parameters": {"type": "object", "properties": {"prompt": {"type": "string", "description": "Instrucción de implementación para Codex"}}, "required": ["prompt"]}}},
]

def notion_tool(tool_name: str, arguments: dict) -> str:
    return notion_mcp.call_tool(tool_name, arguments)


_SERENA_OUTPUT_LIMIT = 3000  # chars máx para tools Serena verbosas

def serena_tool(tool_name: str, arguments: dict) -> str:
    # Inyectar límite de output para tools que lo soportan
    if tool_name in ("list_dir", "find_file", "find_symbol", "search_files_by_name"):
        arguments = {**arguments, "max_answer_chars": _SERENA_OUTPUT_LIMIT}
    result = serena_mcp.call_tool(tool_name, arguments)
    # Truncar igualmente por si acaso
    if isinstance(result, str) and len(result) > _SERENA_OUTPUT_LIMIT:
        result = result[:_SERENA_OUTPUT_LIMIT] + "\n...[truncado]"
    return result


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
    "git_create_branch": lambda args: git_create_branch(args["name"]),
    "git_status": lambda args: git_status(),
    "git_diff_summary": lambda args: git_diff_summary(args.get("max_files", 20)),
    "git_commit": lambda args: git_commit(args["message"]),
    "git_push_branch": lambda args: git_push_branch(args["branch"]),
    "git_approve_push": lambda args: git_approve_push(args["branch"]),
    "create_spec": lambda args: create_spec(args["title"], args["content"]),
    "read_file": lambda args: read_file(args["path"]),
    "write_file": lambda args: write_file(args["path"], args["content"]),
    "add_memory": lambda args: add_memory(args["text"]),
    "search_memory": lambda args: search_memory(args["query"]),
    "get_all_memories": lambda args: get_all_memories(),
    "web_search": lambda args: web_search(args["query"], args.get("max_results", 5)),
    "sql_query": lambda args: sql_query(args["query"]),
    "list_tables": lambda args: list_tables(),
    "schedule_task": lambda args: schedule_task(args["task_id"], args["cron_expr"], args["command"]),
    "list_tasks": lambda args: list_tasks(),
    "remove_task": lambda args: remove_task(args["task_id"]),
    "codex_exec": lambda args: codex_exec(args["prompt"], args.get("writable_paths")),
    **{name: lambda args, n=name: notion_tool(n, args) for name in _notion_tool_names},
    **{name: lambda args, n=name: serena_tool(n, args) for name in _serena_tool_names},
}
