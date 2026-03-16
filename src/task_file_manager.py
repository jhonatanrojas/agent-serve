from __future__ import annotations

from datetime import datetime

from src.task_store import TaskStore
from src.work_item import WorkItem


class TaskFileManager:
    def __init__(self, workspace_path: str):
        self.store = TaskStore(workspace_path)

    def _path_for(self, item: WorkItem):
        return self.store.tasks_dir / f"{item.id}.md"

    def create_task_file(self, item: WorkItem):
        self.store.ensure_initialized()
        path = self._path_for(item)
        path.write_text(self._render(item), encoding="utf-8")

    def update_task_file(self, item: WorkItem, note: str | None = None):
        self.store.ensure_initialized()
        path = self._path_for(item)
        if not path.exists():
            path.write_text(self._render(item), encoding="utf-8")
            return
        content = self._render(item)
        if note:
            content += f"\n\n## Historial\n- {datetime.utcnow().isoformat()}: {note}\n"
        path.write_text(content, encoding="utf-8")

    def _render(self, item: WorkItem) -> str:
        deps = ", ".join(item.depends_on) if item.depends_on else "-"
        return (
            f"# {item.id} - {item.title}\n\n"
            f"- status: {item.status}\n"
            f"- source: {item.source}\n"
            f"- depends_on: {deps}\n"
            f"- created_at: {item.created_at}\n"
            f"- updated_at: {item.updated_at}\n\n"
            f"## Descripción\n{item.description or '(sin descripción)'}\n"
        )
