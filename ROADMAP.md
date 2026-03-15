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
- [ ] Configurar entorno Python (venv + dependencias)
- [ ] Crear bot de Telegram y obtener token
- [ ] Implementar agente con LiteLLM (soporte multi-proveedor)
- [ ] Implementar tools: git_pull, git_push, create_spec
- [ ] Integrar Notion MCP server
- [ ] Conectar bot Telegram con el agente
- [ ] Envío de avances/progreso por Telegram
- [ ] Ejecutar agente como servicio en segundo plano (systemd)
- [ ] Pruebas end-to-end
