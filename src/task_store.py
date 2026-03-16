from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from src.work_item import WorkItem


class TaskStore:
    def __init__(self, workspace_path: str):
        self.workspace_path = Path(workspace_path)
        self.root = self.workspace_path / ".agent_tasks"
        self.tasks_dir = self.root / "tasks"
        self.tasks_json = self.root / "tasks.json"

    def ensure_initialized(self):
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        if not self.tasks_json.exists():
            self._save_payload({"version": 1, "next_id": 1, "items": []})

    def _load_payload(self) -> dict:
        self.ensure_initialized()
        try:
            return json.loads(self.tasks_json.read_text(encoding="utf-8"))
        except Exception:
            return {"version": 1, "next_id": 1, "items": []}

    def _save_payload(self, payload: dict):
        payload["updated_at"] = datetime.utcnow().isoformat()
        self.tasks_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def list_items(self) -> list[WorkItem]:
        payload = self._load_payload()
        return [WorkItem.from_dict(it) for it in payload.get("items", [])]

    def get_item(self, task_id: str) -> WorkItem | None:
        task_id = task_id.strip().upper()
        for item in self.list_items():
            if item.id.upper() == task_id:
                return item
        return None

    def add_item(self, title: str, description: str = "", depends_on: list[str] | None = None) -> WorkItem:
        payload = self._load_payload()
        next_id = int(payload.get("next_id", 1))
        task_id = f"TASK-{next_id:03d}"
        item = WorkItem(
            id=task_id,
            title=title.strip() or "(sin título)",
            description=description.strip(),
            depends_on=[d.strip().upper() for d in (depends_on or []) if d.strip()],
            source="local",
            status="todo",
        )
        items = payload.get("items", [])
        items.append(item.to_dict())
        payload["items"] = items
        payload["next_id"] = next_id + 1
        self._save_payload(payload)
        return item

    def upsert_item(self, item: WorkItem):
        payload = self._load_payload()
        found = False
        data = item.to_dict()
        for idx, current in enumerate(payload.get("items", [])):
            if current.get("id", "").upper() == item.id.upper():
                payload["items"][idx] = data
                found = True
                break
        if not found:
            payload.setdefault("items", []).append(data)
        self._save_payload(payload)

    def update_status(self, task_id: str, status: str):
        payload = self._load_payload()
        for current in payload.get("items", []):
            if current.get("id", "").upper() == task_id.upper():
                current["status"] = status
                current["updated_at"] = datetime.utcnow().isoformat()
        self._save_payload(payload)

    def export_json(self) -> str:
        self.ensure_initialized()
        return str(self.tasks_json)
