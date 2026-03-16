from dataclasses import dataclass
from typing import Literal

FailureType = Literal["tool", "lint", "test", "loop", "timeout", "unknown"]
ActionType = Literal["retry", "pause"]


@dataclass
class RecoveryDecision:
    action: ActionType
    strategy: str
    reason: str


class RecoveryAgent:
    def classify_failure(self, status: str, result: str) -> FailureType:
        text = (result or "").lower()
        if status == "loop_detected" or "loop detectado" in text:
            return "loop"
        if "timeout" in text:
            return "timeout"
        if "ruff" in text or "lint" in text or "flake8" in text:
            return "lint"
        if "test" in text or "pytest" in text:
            return "test"
        if status == "error" and ("tool" in text or "ejecutando `" in text):
            return "tool"
        if status == "error":
            return "tool"
        return "unknown"

    def decide(self, failure_type: FailureType, attempt_count: int) -> RecoveryDecision:
        # límite general
        if attempt_count >= 3:
            return RecoveryDecision("pause", "stop_after_max_attempts", "máximo de intentos alcanzado")

        # fallos de alto riesgo tras 2 intentos
        if failure_type in {"loop", "timeout"} and attempt_count >= 2:
            return RecoveryDecision("pause", "stop_on_repeated_high_risk", f"fallo repetido de tipo {failure_type}")

        strategies = {
            "tool": "retry_with_simpler_scope",
            "lint": "retry_with_validation_context",
            "test": "retry_with_test_context",
            "loop": "retry_with_alt_tooling",
            "timeout": "retry_with_shorter_actions",
            "unknown": "retry_conservative",
        }
        strategy = strategies.get(failure_type, "retry_conservative")
        return RecoveryDecision("retry", strategy, f"reintento por fallo {failure_type}")
