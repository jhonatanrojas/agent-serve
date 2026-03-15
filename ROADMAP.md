# Agent Server

Agente autónomo que recibe instrucciones via Telegram, ejecuta tareas de desarrollo y reporta avances.

## Objetivo

Construir un agente en segundo plano que:
- Reciba instrucciones por Telegram
- Ejecute cambios en código
- Reporte avances en tiempo real
- Se integre con Notion via MCP

## Tareas

- [x] Configurar SSH a GitHub
- [x] Crear estructura del proyecto
- [x] Configurar entorno Python (venv + dependencias)
- [x] Crear bot de Telegram y obtener token
- [x] Implementar agente con LiteLLM (soporte multi-proveedor)
- [x] Implementar tools: git_pull, git_push, create_spec
- [x] Integrar Notion MCP server
- [x] Conectar bot Telegram con el agente
- [x] Envío de avances/progreso por Telegram
- [x] Ejecutar agente como servicio en segundo plano (systemd)
- [ ] Pruebas end-to-end
