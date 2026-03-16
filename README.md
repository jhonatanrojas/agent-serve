# Agent Server

Agente autónomo operado por Telegram para tareas de ingeniería de software con ejecución en fases, guardrails, persistencia de runs, recuperación y observabilidad.

## Qué hay nuevo (resumen rápido)

- Runtime persistente con `run_id`, eventos, checkpoints, validaciones e intentos.
- Reanudación de corridas con `resume_run(run_id)`.
- Workspace por tarea (branch aislada) + gate de git por branch.
- RepoMap persistente para acelerar análisis contextual.
- RecoveryAgent para retry/pause por subtarea.
- Reviewer con contexto de diff + `required_fixes`.
- Validator incremental con lint/typecheck y tests relacionados.
- Seguridad operacional:
  - sandbox global de paths de archivos (`read_file`/`write_file`).
  - policy central de ejecución de tools (allowlist, timeout y truncado de output).
- Observabilidad operativa por Telegram: `/status`, `/plan`, `/resume`, `/logs`, `/diff`, `/stop`.
- Mensajes Telegram sin preview de links para evitar “imágenes” automáticas.

---

## Arquitectura

```text
run_agent()
  ├── run_supervisor()             [tareas complejas]
  │     ├── planner                → complejidad + spec
  │     ├── workspace_manager      → branch por run
  │     ├── analyst + repomap      → contexto del repo
  │     ├── coder                  → implementación por subtareas
  │     ├── recovery_agent         → retry/pause adaptativo
  │     ├── reviewer               → diff + criterios + required_fixes
  │     ├── validator              → syntax/lint/typecheck/tests relacionados
  │     └── run_state              → eventos/checkpoints/validaciones/intentos
  └── run_agent_loop()             [tareas simples]
        └── executor + shell_policy + loop_guard
```

---

## Workspace por tarea

El workspace **no es una carpeta separada** — es una branch de git aislada dentro del mismo repo.

Cuando el supervisor inicia una tarea compleja:
1. `WorkspaceManager.create_or_get_workspace(run_id, task)` crea una branch `task/<run_id_corto>-<slug-de-la-tarea>` desde la branch actual.
2. Hace checkout a esa branch automáticamente.
3. El agente trabaja ahí — todos los cambios quedan aislados en esa branch, sin afectar `master` ni otras ramas.
4. Si el repo tiene cambios sin commitear al momento de crear el workspace, se lanza `WorkspaceError` y la tarea no inicia.
5. Si el `run_id` ya tiene metadata guardada (reanudación), el workspace existente se reutiliza sin crear una nueva branch.

> Las tareas simples (`run_agent_loop`) no crean workspace — trabajan directo en la branch actual.

---

## Módulos clave

| Módulo | Responsabilidad |
|---|---|
| `src/agent.py` | Entrada principal, decide supervisor o loop simple |
| `src/supervisor.py` | Orquestación multi-fase, checkpoints/eventos, `resume_run` |
| `src/run_state.py` | Persistencia de corridas y helpers de consulta de runs |
| `src/run_dashboard.py` | Dashboard textual, plan y logs por `run_id` |
| `src/workspace_manager.py` | Workspace/branch aislado por corrida |
| `src/git_gate.py` | Reglas por branch para commit/push/aprobación |
| `src/repomap.py` | Mapa persistente del repositorio |
| `src/recovery_agent.py` | Clasificación de fallos y estrategia retry/pause |
| `src/reviewer.py` | Revisión con diff + `required_fixes` |
| `src/validator.py` | Validación incremental + tests relacionados |
| `src/path_sandbox.py` | Sandbox global de rutas dentro del repo |
| `src/shell_policy.py` | Policy central de tools (allowlist/timeout/output) |
| `src/executor.py` | Ejecución de tools aplicando policy + guardrails |
| `src/tools.py` | Registro de tools locales + MCP + git seguro |
| `main.py` | Bot Telegram, comandos operativos y ejecución async |

---

## Requisitos

- Ubuntu 22.04+
- Python 3.10+
- Node.js 18+
- Git

---

## Instalación rápida

```bash
git clone git@github.com:jhonatanrojas/agent-serve.git
cd agent-serve
python3 -m venv venv
venv/bin/pip install -r requirements.txt
npm install -g @notionhq/notion-mcp-server
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Crea `.env` (o adapta el existente):

```env
# Modelo
LLM_MODEL=deepseek/deepseek-chat
DEEPSEEK_API_KEY=...

# Telegram
TELEGRAM_TOKEN=...
TELEGRAM_ALLOWED_USER=...

# Integraciones
NOTION_API_KEY=...

# Paths
REPO_PATH=/ruta/al/repo
RUNSTATE_DB_PATH=/ruta/a/.agent.db
SQLITE_DB_PATH=/ruta/a/.agent.db

# Guardrails
AGENT_MAX_ITERATIONS=20
AGENT_MAX_SAME_TOOL_CALLS=3
AGENT_MAX_SAME_RESULT=2

# Shell policy (nueva)
AGENT_TOOL_TIMEOUT_SECONDS=45
AGENT_TOOL_OUTPUT_LIMIT=2000
AGENT_TOOL_ALLOWLIST=
AGENT_ALLOW_DYNAMIC_MCP_TOOLS=true
```

Instala servicio systemd:

```bash
cp agent-serve.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable agent-serve
systemctl start agent-serve
```

---

## Comandos Telegram (operación)

| Comando | Acción |
|---|---|
| `/stop` | Cancela la tarea en curso |
| `/status [run_id]` | Dashboard textual del run activo/último |
| `/plan [run_id]` | Vista de plan/subtareas del run |
| `/resume [run_id]` | Reanuda una corrida persistida |
| `/logs [run_id]` | Eventos recientes del run |
| `/diff` | Resumen del diff local actual |

> Si no pasas `run_id`, se usa el run activo o el más reciente.

---

## Seguridad operacional

### 1) Sandbox de rutas
- `read_file`/`write_file` solo operan dentro de `REPO_PATH`.
- Rutas fuera del repo son bloqueadas.

### 2) Policy central de tools
- Allowlist global de tools.
- Timeout por tool.
- Truncado de output para evitar respuestas gigantes.
- Soporte controlado para tools dinámicas MCP (`API-*`, `serena_*`, `notion_*`).

### 3) Git seguro por branch
- Gate de commit/push por branch.
- Push requiere aprobación explícita.
- Bloqueo de push a `main/master` según reglas del gate.

---

## Flujo de validación/revisión

- Reviewer usa snapshot de archivos + diff parcial.
- Reviewer devuelve `required_fixes` estructurado.
- Validator corre:
  - syntax check,
  - lint incremental,
  - typecheck incremental,
  - tests relacionados al área modificada.

---

## Cómo probar (checklist práctico)

### A. Smoke local de integridad

```bash
python -m compileall -q src main.py
python -c "import main; print('main_import_ok')"
```

### B. Probar dashboard y run-state

```bash
python - <<'PY'
from src.run_state import create_run_state, append_event, append_checkpoint
from src.run_dashboard import build_run_dashboard, build_run_logs, build_run_plan
rid = create_run_state('planning', 'demo')
append_event(rid,'planning_started','planning',{'message':'demo'})
append_checkpoint(rid,'planning_ready','planning',{})
print(build_run_dashboard(rid))
print(build_run_logs(rid))
print(build_run_plan(rid))
PY
```

### C. Probar sandbox de paths

```bash
python - <<'PY'
from src.path_sandbox import resolve_repo_path, PathSandboxError
try:
    resolve_repo_path('/tmp/outside.txt')
    print('ERROR: no bloqueó')
except PathSandboxError:
    print('OK: sandbox activo')
PY
```

### D. Probar shell policy

```bash
python - <<'PY'
from src.shell_policy import is_tool_allowed, truncate_output
print('git_status allowed =', is_tool_allowed('git_status'))
print('API-post-search allowed =', is_tool_allowed('API-post-search'))
print(truncate_output('x'*2500)[-35:])
PY
```

### E. Probar comandos Telegram
1. Arranca servicio: `systemctl restart agent-serve`
2. En Telegram ejecuta:
   - `/status`
   - `/plan`
   - `/logs`
   - `/diff`
   - `/resume <run_id>`
   - `/stop`

Logs:

```bash
journalctl -u agent-serve -f
```

---

## Notas operativas

- Si Telegram “muestra imagen”, normalmente es preview de URL en texto. El bot ya desactiva previews en respuestas.
- Si el proveedor LLM falla (403/timeout), revisa keys/model/provider en `.env`.
- Riesgos y backlog técnico: `docs/RISK_TASKS.md`.
