# Risk Backlog for Runtime Evolutions

Este documento lista riesgos complejos detectados durante la evolución incremental del runtime.

## 1) Reanudación granular por fase/subtarea (mitigado)

**Estado actual**
- `resume_run(run_id)` ahora reutiliza el mismo `run_id` y omite subtareas ya completadas (`subtask_completed`) para reducir retrabajo.
- La reanudación salta directo a la acción pendiente cuando existe estado canónico persistido.

**Mitigación aplicada**
- Se persiste `next_action` por fase (`planning|analyze|code_subtask_i|review|validate|done`).
- Se persiste `current_subtask_index`, `completed_subtasks` y `spec` para continuidad canónica.
- `resume_run(run_id)` usa `next_action` y continúa desde la etapa pendiente sin rehacer todas las previas.

**Pendiente menor**
- Añadir tests de integración fin-a-fin para reanudación en cada fase.

## 2) Crecimiento de estado JSON en SQLite (mitigado)

**Estado**
- Mitigado con límites en historial de eventos/checkpoints/validaciones.

**Siguiente mejora opcional**
- Migrar a tablas normalizadas (`run_events`, `run_checkpoints`) para escalabilidad y consultas más eficientes.

## 3) Git-gate global compartido entre corridas (mitigado)

**Estado**
- Corregido: el gate ahora se evalúa por `branch_name`.
- La validación y aprobación de push se aplican por branch (no estado global único).
