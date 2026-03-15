# Agent Server

Agente autónomo que recibe instrucciones via Telegram, ejecuta tareas de desarrollo y reporta avances en tiempo real.

## Arquitectura

```
run_agent()
  ├── run_supervisor()        [tareas complejas]
  │     ├── planner           → clasifica complejidad, genera spec
  │     ├── analyst           → escanea codebase, evalúa impacto
  │     ├── coder             → implementa subtareas (scope acotado)
  │     ├── reviewer          → verifica criterios de aceptación
  │     └── validator         → lint (ruff) + syntax check
  └── run_agent_loop()        [tareas simples]
        └── guardrails: loop guard + /stop + MAX_ITERATIONS
```

### Módulos

| Módulo | Responsabilidad |
|--------|----------------|
| `src/agent.py` | Punto de entrada, orquesta supervisor o loop directo |
| `src/supervisor.py` | Controla flujo entre subagentes, detecta loops |
| `src/planner.py` | Clasifica complejidad, genera spec estructurada |
| `src/analyst.py` | Escanea repo, identifica archivos relevantes, evalúa impacto |
| `src/coder.py` | Implementa subtareas con tools limitadas a escritura de código |
| `src/reviewer.py` | Verifica criterios de aceptación post-coder |
| `src/validator.py` | Lint (ruff) + syntax check sobre archivos modificados |
| `src/executor.py` | `execute_tool_call`, cancel/reset, MAX_ITERATIONS (compartido) |
| `src/loop_guard.py` | Detecta repetición de tool calls y resultados |
| `src/task_context.py` | Estado formal de tarea: status, historial, archivos modificados |
| `src/tools.py` | Definición y registro de todas las tools disponibles |
| `src/memory.py` | Memoria persistente entre conversaciones (SQLite) |
| `src/database.py` | Queries SQL sobre base de datos local |
| `src/search.py` | Búsqueda web con DuckDuckGo |
| `src/scheduler.py` | Tareas programadas con APScheduler |
| `src/notion.py` | Cliente Notion MCP (22 tools) |
| `src/serena.py` | Cliente Serena MCP (27 tools de coding semántico) |

---

## Stack

- **LiteLLM** — soporte multi-proveedor (DeepSeek, Claude, GPT-4)
- **python-telegram-bot** — interfaz de control por Telegram
- **GitPython** — git pull / git push automático
- **Notion MCP** — integración con Notion via MCP
- **Serena MCP** — coding semántico (find_symbol, replace_content, etc.)
- **Memoria SQLite** — memoria persistente entre conversaciones
- **DuckDuckGo Search** — búsqueda web sin API key
- **APScheduler** — tareas programadas con cron desde Telegram
- **SQLite DB** — base de datos local para el agente
- **ruff** — linter para validación técnica post-cambios
- **systemd** — servicio en segundo plano

---

## Requisitos

- Ubuntu 22.04+
- Python 3.10+
- Node.js 18+
- Git

---

## Instalación

### 1. Clonar el repositorio

```bash
git clone git@github.com:jhonatanrojas/agent-serve.git
cd agent-serve
```

### 2. Configurar SSH a GitHub

```bash
ssh-keygen -t ed25519 -C "github-agent" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
```

Agregar la clave en: **github.com → Settings → SSH and GPG keys → New SSH key**

Verificar:
```bash
ssh -T git@github.com
```

### 3. Configurar Git

```bash
git config --global user.name "tu nombre"
git config --global user.email "tu@email.com"
```

### 4. Entorno Python

```bash
apt install -y python3.10-venv
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### 5. Instalar Notion MCP

```bash
npm install -g @notionhq/notion-mcp-server
```

### 6. Instalar uv (para Serena MCP)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 7. Configurar variables de entorno

```bash
cp .env.example .env
nano .env
```

```env
# LLM — elegir uno
LLM_MODEL=deepseek/deepseek-chat
DEEPSEEK_API_KEY=sk-...

# LLM_MODEL=anthropic/claude-3-5-sonnet-20241022
# ANTHROPIC_API_KEY=sk-ant-...

# LLM_MODEL=gpt-4o
# OPENAI_API_KEY=sk-...

# Telegram
TELEGRAM_TOKEN=         # Token de @BotFather
TELEGRAM_ALLOWED_USER=  # Tu chat ID (obtener con @userinfobot)

# Notion
NOTION_API_KEY=ntn_...

# Repositorio que gestionará el agente
REPO_PATH=/ruta/al/repo

# Guardrails (opcional)
AGENT_MAX_ITERATIONS=20
AGENT_MAX_SAME_TOOL_CALLS=3
AGENT_MAX_SAME_RESULT=2
```

### 8. Instalar servicio systemd

```bash
cp agent-serve.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable agent-serve
systemctl start agent-serve
```

Verificar:
```bash
systemctl status agent-serve
journalctl -u agent-serve -f
```

---

## Uso

Enviar mensajes al bot de Telegram:

```
haz git pull y dime el estado del repo
refactoriza el módulo de autenticación          ← tarea compleja: activa supervisor
busca en internet las últimas versiones de FastAPI
recuerda que prefiero commits en inglés
programa un git pull diario a las 8am
crea una tabla de tareas en la base de datos
```

### Comando especial

| Comando | Acción |
|---------|--------|
| `/stop` | Cancela la tarea en curso inmediatamente |

---

## Tools disponibles (63 total)

| Categoría | Tools |
|-----------|-------|
| Git | `git_pull`, `git_push` |
| Archivos | `read_file`, `write_file`, `create_spec` |
| Memoria | `add_memory`, `search_memory`, `get_all_memories` |
| Búsqueda | `web_search` |
| Base de datos | `sql_query`, `list_tables` |
| Crons | `schedule_task`, `list_tasks`, `remove_task` |
| Notion | 22 tools (CRUD páginas, bases de datos, bloques) |
| Serena | 27 tools (búsqueda y edición semántica de código) |

---

## Guardrails

| Guardrail | Comportamiento |
|-----------|---------------|
| `MAX_ITERATIONS` | Corta el loop tras N iteraciones (default: 20) |
| Loop de tool calls | Detecta misma tool+args repetida >3 veces |
| Loop de resultados | Detecta mismo resultado repetido >2 veces |
| Loop de subagentes | Detecta mismo subagente invocado >2 veces sin progreso |
| `/stop` | Cancela inmediatamente via `threading.Event` |

---

## Flujo para tareas complejas

Cuando el agente detecta palabras clave como `refactor`, `arquitectura`, `integrar`, `migrar`, etc., activa el supervisor:

1. **Planner** — genera spec con objetivo, subtareas y criterios de aceptación → guardada en `specs/`
2. **Analyst** — escanea el repo, identifica archivos relevantes, evalúa impacto (low/medium/high)
3. **Coder** — ejecuta cada subtarea con tools limitadas a escritura de código
4. **Reviewer** — verifica que los cambios cumplen los criterios de aceptación
5. **Validator** — ejecuta ruff lint + syntax check sobre archivos modificados

Todo el progreso se reporta en tiempo real por Telegram.

---

## Cambiar de proveedor LLM

```env
# DeepSeek (más económico)
LLM_MODEL=deepseek/deepseek-chat
DEEPSEEK_API_KEY=sk-...

# Claude (mejor para código)
LLM_MODEL=anthropic/claude-3-5-sonnet-20241022
ANTHROPIC_API_KEY=sk-ant-...

# GPT-4
LLM_MODEL=gpt-4o
OPENAI_API_KEY=sk-...
```

```bash
systemctl restart agent-serve
```

## Comandos útiles

```bash
# Ver logs en tiempo real
journalctl -u agent-serve -f

# Reiniciar / detener
systemctl restart agent-serve
systemctl stop agent-serve
```
