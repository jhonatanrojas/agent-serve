from __future__ import annotations

from src.task_provider_notion import NotionTaskProvider
from src.task_queue import TaskQueue
from src.task_store import TaskStore
from src.work_item import WorkItem


class TaskSourceRouter:
    def __init__(self, workspace: dict):
        self.workspace = workspace
        self.mode = (workspace.get("task_mode") or "local").lower()
        self.local = TaskStore(workspace["repo_path"])
        self.notion = NotionTaskProvider()

    def list_tasks(self) -> list[WorkItem]:
        notion_id = self.workspace.get("notion_database_id", "")
        if self.mode == "notion":
            return self.notion.list_tasks(notion_id)
        local_items = self.local.list_items()
        if self.mode == "hybrid":
            return local_items + self.notion.list_tasks(notion_id)
        return local_items

    def next_task(self) -> WorkItem | None:
        if self.mode == "notion":
            items = self.list_tasks()
            return TaskQueue.next_ready(items)
        return TaskQueue.next_ready(self.local.list_items())
