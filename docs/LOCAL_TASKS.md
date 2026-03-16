# Local-first task system

El agente ahora puede operar sin Notion usando un backlog local por workspace.

## Estructura

Cada workspace (`repo_path`) tendrá:

- `.agent_tasks/tasks.json` → índice oficial de backlog
- `.agent_tasks/tasks/TASK-XXX.md` → archivo vivo por tarea

## Formato oficial de `tasks.json`

```json
{
  "version": 1,
  "next_id": 4,
  "updated_at": "2026-01-01T00:00:00",
  "items": [
    {
      "id": "TASK-001",
      "title": "Implementar endpoint health",
      "description": "Agregar /healthz y pruebas",
      "status": "todo",
      "source": "local",
      "repo_hint": "",
      "page_id": "",
      "priority": "",
      "depends_on": [],
      "created_at": "2026-01-01T00:00:00",
      "updated_at": "2026-01-01T00:00:00"
    }
  ]
}
```

## Formato oficial de `TASK-XXX.md`

```md
# TASK-001 - Implementar endpoint health

- status: in_progress
- source: local
- depends_on: -
- created_at: 2026-01-01T00:00:00
- updated_at: 2026-01-01T00:05:00

## Descripción
Agregar /healthz y pruebas

## Historial
- 2026-01-01T00:01:00: Inicio de ejecución
- 2026-01-01T00:05:00: Fin de ejecución: done
```

## Modo de fuente (`task_mode`)

- `local` (default): usa sólo backlog local.
- `notion`: usa Notion.
- `hybrid`: combina local + Notion (prioridad para `/do_next` local).

## Flujo recomendado

1. Crear tareas con `/addtask` o `/addtasks`.
2. Ejecutar con `/do_next` o `/do_task TASK-XXX`.
3. Al terminar cada tarea, revisar confirmación del bot antes de continuar.
4. Consultar backlog con `/tasks` y detalle con `/task TASK-XXX`.
5. Opcional: importar con `/sync_notion_to_tasks` y exportar con `/export_tasks`.
