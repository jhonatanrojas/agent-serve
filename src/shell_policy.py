import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Callable

TOOL_TIMEOUT_SECONDS = int(os.getenv("AGENT_TOOL_TIMEOUT_SECONDS", "45"))
TOOL_OUTPUT_LIMIT = int(os.getenv("AGENT_TOOL_OUTPUT_LIMIT", "2000"))

_default_allow = {
    "git_pull", "git_push", "git_create_branch", "git_status", "git_diff_summary", "git_commit", "git_push_branch", "git_approve_push",
    "create_spec", "read_file", "write_file",
    "add_memory", "search_memory", "get_all_memories",
    "web_search", "sql_query", "list_tables",
    "schedule_task", "list_tasks", "remove_task",
    "list_dir", "find_file", "find_symbol",
}


def _allowed_tools() -> set[str]:
    raw = os.getenv("AGENT_TOOL_ALLOWLIST", "").strip()
    if not raw:
        return set(_default_allow)
    return {x.strip() for x in raw.split(",") if x.strip()}


def is_tool_allowed(name: str) -> tuple[bool, str]:
    allowed = _allowed_tools()
    if name in allowed:
        return True, ""

    allow_dynamic = os.getenv("AGENT_ALLOW_DYNAMIC_MCP_TOOLS", "true").lower() in {"1", "true", "yes"}
    if allow_dynamic and (name.startswith("API-") or name.startswith("serena_") or name.startswith("notion_")):
        return True, ""

    return False, f"Tool no permitida por policy: `{name}`"


def truncate_output(text: str) -> str:
    if text is None:
        return ""
    if len(text) <= TOOL_OUTPUT_LIMIT:
        return text
    return text[:TOOL_OUTPUT_LIMIT] + "\n...[output truncado por policy]"


def run_with_policy(name: str, fn: Callable[[], str]) -> str:
    ok, reason = is_tool_allowed(name)
    if not ok:
        return reason

    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(fn)
        try:
            result = fut.result(timeout=TOOL_TIMEOUT_SECONDS)
            return truncate_output(str(result))
        except TimeoutError:
            return f"Tool timeout ({TOOL_TIMEOUT_SECONDS}s): `{name}`"
        except Exception as e:
            return f"Error ejecutando `{name}`: {e}"
