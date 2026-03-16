from src.work_item import WorkItem


def map_notion_page_to_work_item(page: dict) -> WorkItem:
    props = page.get("properties", {})

    def _txt(name: str) -> str:
        value = props.get(name, {})
        if isinstance(value, dict):
            if value.get("type") == "title":
                arr = value.get("title", [])
                return "".join(x.get("plain_text", "") for x in arr)
            if value.get("type") == "rich_text":
                arr = value.get("rich_text", [])
                return "".join(x.get("plain_text", "") for x in arr)
            if value.get("type") == "select":
                sel = value.get("select") or {}
                return sel.get("name", "")
            if value.get("type") == "status":
                sel = value.get("status") or {}
                return sel.get("name", "")
        return ""

    return WorkItem(
        id=page.get("id", ""),
        title=_txt("Name") or _txt("Title") or "(sin título)",
        description=_txt("Description") or _txt("Task") or "",
        repo_hint=_txt("Repository") or _txt("Repo") or "",
        page_id=page.get("id", ""),
        status=_txt("Status") or "todo",
        priority=_txt("Priority") or "",
        source="notion",
    )
