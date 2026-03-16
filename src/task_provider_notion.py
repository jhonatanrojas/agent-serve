import json
import re
from src.notion import notion_mcp
from src.task_mapper import WorkItem, map_notion_page_to_work_item


class NotionTaskProvider:
    def list_tasks(self, notion_database_id: str) -> list[WorkItem]:
        if not notion_database_id:
            return []
        raw = notion_mcp.call_tool("notion_query_database", {"database_id": notion_database_id})
        data = self._parse_payload(raw)
        pages = data.get("results", []) if isinstance(data, dict) else []
        return [map_notion_page_to_work_item(p) for p in pages if isinstance(p, dict)]

    def update_task_status(self, page_id: str, status: str):
        if not page_id:
            return
        notion_mcp.call_tool(
            "notion_update_page",
            {"page_id": page_id, "properties": {"Status": {"status": {"name": status}}}},
        )

    @staticmethod
    def _parse_payload(raw: str) -> dict:
        try:
            return json.loads(raw)
        except Exception:
            pass
        m = re.search(r"(\{.*\})", raw or "", flags=re.DOTALL)
        if not m:
            return {}
        try:
            return json.loads(m.group(1))
        except Exception:
            return {}
