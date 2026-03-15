import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional


MAX_SAME_TOOL_CALLS = int(__import__('os').getenv("AGENT_MAX_SAME_TOOL_CALLS", "3"))
MAX_SAME_RESULT = int(__import__('os').getenv("AGENT_MAX_SAME_RESULT", "2"))


@dataclass
class LoopGuard:
    tool_call_counts: dict = field(default_factory=lambda: defaultdict(int))
    result_counts: dict = field(default_factory=lambda: defaultdict(int))
    last_progress_step: int = 0
    step: int = 0

    def _hash_call(self, tool_name: str, args: dict) -> str:
        key = json.dumps({"tool": tool_name, "args": args}, sort_keys=True)
        return hashlib.md5(key.encode()).hexdigest()

    def _hash_result(self, result: str) -> str:
        return hashlib.md5(result.strip().encode()).hexdigest()

    def record_call(self, tool_name: str, args: dict) -> Optional[str]:
        """Registra una tool call. Retorna mensaje de loop si se detecta, None si OK."""
        self.step += 1
        call_hash = self._hash_call(tool_name, args)
        self.tool_call_counts[call_hash] += 1
        count = self.tool_call_counts[call_hash]

        if count > MAX_SAME_TOOL_CALLS:
            return (
                f"🔁 Loop detectado: `{tool_name}` fue llamada {count} veces "
                f"con los mismos argumentos sin producir cambios.\n"
                f"Causa probable: el modelo está atascado en un ciclo.\n"
                f"Acción recomendada: reformula la instrucción o verifica el estado del repo."
            )
        return None

    def record_result(self, tool_name: str, result: str) -> Optional[str]:
        """Registra el resultado de una tool. Retorna mensaje de loop si se detecta."""
        result_hash = self._hash_result(result)
        self.result_counts[result_hash] += 1
        count = self.result_counts[result_hash]

        if count > MAX_SAME_RESULT:
            return (
                f"🔁 Loop detectado: `{tool_name}` produjo el mismo resultado {count} veces.\n"
                f"Causa probable: la operación no está generando cambios reales.\n"
                f"Acción recomendada: verifica el estado actual antes de continuar."
            )
        # Hay resultado nuevo → hay progreso
        self.last_progress_step = self.step
        return None
