from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


Status = Literal["running", "completed", "cancelled", "error", "loop_detected", "limit_reached"]


@dataclass
class ToolCall:
    name: str
    args: dict
    result: str
    iteration: int
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class TaskContext:
    message: str
    status: Status = "running"
    iterations: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    error: str = ""
    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    finished_at: str = ""

    def record_tool(self, name: str, args: dict, result: str, iteration: int):
        self.tool_calls.append(ToolCall(name=name, args=args, result=result, iteration=iteration))
        # Detectar archivos modificados por tools de escritura
        if name in ("write_file", "create_spec") and "path" in args:
            path = args["path"]
            if path not in self.modified_files:
                self.modified_files.append(path)
        if name == "git_push":
            self.modified_files.append("[git commit]")

    def finish(self, status: Status, error: str = ""):
        self.status = status
        self.error = error
        self.finished_at = datetime.utcnow().isoformat()

    def summary(self) -> str:
        lines = [
            f"📋 **Resumen de tarea**",
            f"• Status: `{self.status}`",
            f"• Iteraciones: {self.iterations}",
            f"• Tools ejecutadas: {len(self.tool_calls)}",
        ]
        if self.modified_files:
            lines.append(f"• Archivos modificados: {', '.join(self.modified_files)}")
        if self.error:
            lines.append(f"• Error: {self.error}")
        return "\n".join(lines)
