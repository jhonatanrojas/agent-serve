from pathlib import Path
import git


class RepoManager:
    def __init__(self, repo_path: str | Path):
        self.repo_path = Path(repo_path).resolve()
        self.repo = git.Repo(str(self.repo_path))

    def ensure_branch(self, branch: str):
        if branch in {"main", "master"}:
            raise ValueError("No se permite trabajar sobre main/master")
        if branch in [h.name for h in self.repo.heads]:
            self.repo.heads[branch].checkout()
        else:
            self.repo.create_head(branch).checkout()

    def ensure_task_branch(self, task_id: str) -> str:
        safe = str(task_id).strip().replace(" ", "-").replace("/", "-")
        branch = f"task/{safe}"
        self.ensure_branch(branch)
        return branch

    def status(self) -> str:
        return self.repo.git.status("--short") or "working tree clean"
