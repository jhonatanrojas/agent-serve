# Risk Backlog for Runtime Evolutions

Este documento lista riesgos complejos detectados durante la evolución incremental del runtime.

## 1) Reanudación granular por fase/subtarea (mitigado parcialmente)

**Estado actual**
- `resume_run(run_id)` ahora reutiliza el mismo `run_id` y omite subtareas ya completadas (`subtask_completed`) para reducir retrabajo.
- Aun así, el pipeline puede reevaluar etapas previas (planning/analysis) para reconstruir contexto.

**Riesgo residual**
- Continuidad no 100% exacta por punto de ejecución interno.

**Plan de mitigación (tareas)**
1. Persistir `next_action` explícito por fase (`analyze|code_subtask_i|review|validate`).
2. Persistir índice de subtarea y subtareas completadas/pendientes de forma canónica.
3. Extraer fases de supervisor en funciones reentrantes por etapa (entrada/salida determinística).
4. Implementar `resume_run` para saltar directamente a la etapa pendiente sin recomputar previas.
5. Añadir tests de integración para resume en cada fase.

## 2) Crecimiento de estado JSON en SQLite (mitigado)

**Estado**
- Mitigado con límites en historial de eventos/checkpoints/validaciones.

**Siguiente mejora opcional**
- Migrar a tablas normalizadas (`run_events`, `run_checkpoints`) para escalabilidad y consultas más eficientes.

## 3) Git-gate global compartido entre corridas (mitigado)

**Estado**
- Corregido: el gate ahora se evalúa por `branch_name`.
- La validación y aprobación de push se aplican por branch (no estado global único).
