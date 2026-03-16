from __future__ import annotations

from src.work_item import WorkItem


class TaskQueue:
    DONE_STATUSES = {"done", "completed"}

    @staticmethod
    def _is_done(item: WorkItem) -> bool:
        return (item.status or "").lower() in TaskQueue.DONE_STATUSES

    @classmethod
    def next_ready(cls, items: list[WorkItem]) -> WorkItem | None:
        by_id = {i.id.upper(): i for i in items}
        for item in items:
            if cls._is_done(item) or (item.status or "").lower() == "in_progress":
                continue
            blocked = False
            for dep in item.depends_on or []:
                dep_item = by_id.get(dep.upper())
                if dep_item and not cls._is_done(dep_item):
                    blocked = True
                    break
            if not blocked:
                return item
        return None
