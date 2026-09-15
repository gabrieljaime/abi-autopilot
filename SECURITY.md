# Seguridad — ABI Autopilot v1.7.0

ABI Autopilot ejecuta herramientas con acceso al repositorio objetivo y debe correr únicamente en una máquina y sobre repositorios de confianza.

## Datos locales

- No guardes tokens, contraseñas ni claves API en `config.local.json`.
- La autenticación pertenece a GitHub CLI, Codex CLI y Claude Code.
- `config.local.json`, sus backups, `.env*` y `runtime/` están ignorados por Git.
- `runtime/` puede contener texto de issues, prompts, diffs, logs y respuestas de agentes. No lo publiques ni lo adjuntes completo a reportes.

## Controles operativos

Defaults deliberadamente conservadores:

- El daemon conserva `auto_integrate=false` y `auto_close=false`.
- `integrate` es una acción explícita del operador y sólo acepta issues `agent:done` con evidencia completa PASS.
- No force push.
- No `reset --hard`, stash, rebase, `git clean` ni abort automático de cherry-pick.
- La implementación usa un worktree por issue; la integración usa otro worktree temporal detached.
- Un conflicto se preserva en disco y se guarda en `runtime/integrations/issue-N/state.json`.
- `--continue` sólo opera cuando el usuario ya resolvió los archivos conflictivos.
- `.env` y `backend/data/` están protegidos como paths modificables.
- El cleanup sólo toca rutas explícitamente regenerables y rehúsa borrar rutas trackeadas por Git.
- El cleanup corre antes de `git add -A` para que artifacts de tests no entren al commit.
- Reviewer Claude continúa en `permission_mode=plan`.
- Quota/rate-limit y fallos de entorno no se confunden con fallos de código.
- La base remota se vuelve a comprobar antes del push; si cambió, se bloquea sin sobreescribir.
- El issue se vuelve a consultar inmediatamente antes del cierre y se verifica otra vez después.
- Las epics no se cierran desde el integrador.
- Los subprocess tienen timeout wall-clock y corte del árbol completo para evitar procesos huérfanos.

Recomendación: mantener los defaults de daemon sin auto-integración y usar `integrate`/`integrate-done --execute` sólo cuando se quiera avanzar deliberadamente la rama base.

## Reportar una vulnerabilidad

No publiques credenciales, logs de `runtime/` ni detalles explotables en una issue pública. Contactá en privado al mantenedor del repositorio y compartí únicamente la información mínima necesaria para reproducir el problema.
