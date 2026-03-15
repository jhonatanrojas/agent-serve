# Agent Server

Agente autónomo que recibe instrucciones via Telegram, ejecuta tareas de desarrollo y reporta avances en tiempo real.

## Stack

- **LiteLLM** — soporte multi-proveedor (DeepSeek, Claude, GPT-4)
- **python-telegram-bot** — interfaz de control por Telegram
- **GitPython** — git pull / git push automático
- **Notion MCP** — integración con Notion via MCP
- **Serena MCP** — coding semántico (find_symbol, replace_content, etc.)
- **Memoria SQLite** — memoria persistente entre conversaciones (sin dependencias externas)
- **DuckDuckGo Search** — búsqueda web sin API key ni registro
- **APScheduler** — tareas programadas con cron desde Telegram
- **SQLite DB** — base de datos local para el agente
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
venv/bin/pip install "python-telegram-bot[webhooks]"
```

### 5. Instalar Notion MCP

```bash
npm install -g @notionhq/notion-mcp-server
```

### 6. Instalar uv (para Serena MCP)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Verificar que Serena funciona (descarga automática en primer uso):
```bash
~/.local/bin/uvx --from git+https://github.com/oraios/serena serena start-mcp-server --help
```

### 7. Configurar variables de entorno

```bash
cp .env.example .env
nano .env
```

Completar el archivo `.env`:

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
NOTION_API_KEY=ntn_...  # Notion Integration Token

# Repositorio que gestionará el agente
REPO_PATH=/ruta/al/repo
```

#### Obtener credenciales

**Telegram Bot Token:**
1. Hablar con [@BotFather](https://t.me/BotFather)
2. Enviar `/newbot` y seguir los pasos
3. Copiar el token

**Telegram Chat ID:**
1. Hablar con [@userinfobot](https://t.me/userinfobot)
2. Copiar el `Id`

**DeepSeek API Key:**
- [platform.deepseek.com](https://platform.deepseek.com) → API Keys

**Notion Integration Token:**
1. Ir a [notion.so/my-integrations](https://www.notion.so/my-integrations)
2. Crear nueva integración
3. Copiar el token `ntn_...`
4. Compartir las páginas/bases de datos con la integración

### 8. Instalar servicio systemd

```bash
cp agent-serve.service /etc/systemd/system/
# Editar WorkingDirectory y EnvironmentFile si el path es diferente a /root/agent-serve
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

Enviar mensajes al bot de Telegram. Ejemplos:

```
haz git pull y dime el estado del repo
crea una spec para agregar autenticación JWT
busca el símbolo UserService en el código
actualiza el archivo README con los nuevos endpoints
haz commit y push con el mensaje "feat: nueva funcionalidad"
crea una página en Notion con el resumen de los cambios
```

## Tools disponibles

| Tool | Descripción |
|------|-------------|
| `git_pull` | Sincroniza el repo con el remoto |
| `git_push` | Commit + push con mensaje |
| `create_spec` | Crea archivo `.md` en `specs/` |
| `read_file` | Lee un archivo |
| `write_file` | Escribe un archivo |
| `add_memory` | Guarda una memoria persistente |
| `search_memory` | Busca memorias relevantes |
| `get_all_memories` | Lista todas las memorias |
| `web_search` | Busca en internet con DuckDuckGo |
| `sql_query` | Ejecuta SQL en la DB local |
| `list_tables` | Lista tablas de la DB |
| `schedule_task` | Programa tarea con cron |
| `list_tasks` | Lista tareas programadas |
| `remove_task` | Elimina tarea programada |
| Notion (22) | CRUD páginas, bases de datos, bloques |
| Serena (27) | Búsqueda y edición semántica de código |

## Ejemplos de uso por componente

**Memoria:**
```
recuerda que el repo principal es mi-proyecto
¿qué recuerdas sobre el proyecto?
```

**Búsqueda web:**
```
busca en internet cómo implementar JWT en FastAPI
```

**Base de datos:**
```
crea una tabla de tareas con id, titulo y estado
lista todas las tareas pendientes
```

**Crons:**
```
programa una tarea "daily-pull" para hacer git pull todos los días a las 8am
lista las tareas programadas
elimina la tarea "daily-pull"
```
Formato cron: `"minuto hora día mes día_semana"` — ej: `"0 8 * * *"` = todos los días a las 8am

## Cambiar de proveedor LLM

Editar `.env`:

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

Reiniciar el servicio:
```bash
systemctl restart agent-serve
```

## Comandos útiles

```bash
# Ver logs en tiempo real
journalctl -u agent-serve -f

# Reiniciar
systemctl restart agent-serve

# Detener
systemctl stop agent-serve
```
