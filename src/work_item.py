from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime



def now_iso() -> str:
    return datetime.utcnow().isoformat()


@dataclass
class WorkItem:
    id: str
    title: str
    description: str = ""
    status: str = "todo"
    source: str = "local"
    repo_hint: str = ""
    page_id: str = ""
    priority: str = ""
    depends_on: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "source": self.source,
            "repo_hint": self.repo_hint,
            "page_id": self.page_id,
            "priority": self.priority,
            "depends_on": list(self.depends_on or []),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorkItem":
        return cls(
            id=data.get("id", ""),
            title=data.get("title", "(sin título)"),
            description=data.get("description", ""),
            status=data.get("status", "todo"),
            source=data.get("source", "local"),
            repo_hint=data.get("repo_hint", ""),
            page_id=data.get("page_id", ""),
            priority=data.get("priority", ""),
            depends_on=list(data.get("depends_on", []) or []),
            created_at=data.get("created_at") or now_iso(),
            updated_at=data.get("updated_at") or now_iso(),
        )
