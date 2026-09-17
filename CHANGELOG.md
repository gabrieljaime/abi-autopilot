# Changelog

## v1.8.0

- `deployment_branch` separa la rama de integración de la rama que se despliega.
  Sin valor explícito se asume `base_branch`, así que un proyecto de una sola
  rama no cambia de comportamiento.
- Nuevo estado `agent:integrated`: mergeado en la rama de integración pero
  todavía no entregado. `agent:done` conserva el nombre y pasa a significar
  *entregado en la rama de despliegue*.
- `integrate`/`integrate-done` ya no cierran una issue por haber integrado: si
  la rama de despliegue es otra, la dejan `agent:integrated` y abierta.
- Gate de promoción por equivalencia de parche (`git cherry`) además de
  ancestría, para reconocer cherry-picks y rebases.
- `release-audit`: auditoría de consistencia (issues CLOSED fuera de la rama de
  despliegue), aviso de divergencia entre ramas y `--promote` para cerrar lo ya
  entregado.
- `release-status --issue N`: estado de entrega de una issue con su evidencia.
- `observe` informa RELEASED / PENDING PROMOTION y la divergencia.

## v1.7.0

- Integración batch en un único worktree, con validaciones baratas por issue y un gate final acumulado antes del push.
- Límites configurables de workers para Vitest, pytest y Playwright.
- Codex o Claude como implementador, con fallback opcional ante límites de cuota.
- `auto-ready` para preparar issues nuevas según la política configurada.
- Configuración de ejemplo, documentación y defaults neutrales para distribución pública.

## v1.6.1

- Corrige ejecución de wrappers Windows `.cmd`/`.bat` (por ejemplo `npm.cmd`) en `run_capture`; `doctor` ya no cae con `WinError 2`.
- `run_capture(..., check=False)` convierte errores de lanzamiento en `returncode=127` para diagnóstico en vez de propagar `FileNotFoundError`.
- Corrige el test de timeout en Windows: usa quoting nativo de `cmd.exe`, no `shlex.quote` POSIX.
- Mantiene timeout real y terminación del árbol de procesos para comandos silenciosos.
- `version` reporta `ABI Autopilot v1.6.1`.
- Upgrade crea `config.local.json.pre-v1.6.1.bak`.

## v1.6.0
- Nuevo `autopilot.py integrate --issue N`: integración manual en worktree temporal, con validación, push verificado y cierre live opcional.
- Nuevo `autopilot.py integrate --issue N --continue` para continuar un cherry-pick conflictivo después de resolverlo manualmente; jamás aborta/reset/stash/rebase automáticamente.
- Nuevo `integrate-done`: plan por defecto y ejecución secuencial sólo con `--execute`.
- Adopta de forma segura un único cherry-pick local ya hecho cuando coincide exactamente con `fix(issue-N):` y es el único commit ahead.
- Verifica que la branch del issue conserve el SHA aprobado por Autopilot antes de integrar.
- Exige evidencia estructurada de Reviewer/Targeted-Fast/Full PASS antes de integrar.
- Verificación live de la rama base y fetch live de la issue inmediatamente antes del cierre.
- Epics explícitamente excluidas del cierre automático del integrador.
- Descubrimiento de tests backend vecinos y subsets pytest con `--no-cov`; el gate global de coverage permanece en fast validation.
- Timeout de subprocess realmente wall-clock y terminación del árbol de procesos (incluyendo npm/Playwright en Windows).
- Cleanup pre-commit para evitar incluir `.coverage`, `coverage.xml`, `.next`, Playwright reports/results.
- Cleanup no elimina paths trackeados por Git aunque estén configurados como regenerables.
- `doctor` ampliado con versión, toolchain, disco, dependency-cache y warning de drift frente a CI (Python 3.11 / Node 20).
- `version` reporta `ABI Autopilot v1.6.0` (release base; v1.6.1 corrige Windows).
- Upgrade crea `config.local.json.pre-v1.6.bak` y conserva settings existentes.

## v1.5.0
- Cache npm compartido por SHA-256 de `package-lock.json` + `package.json`.
- Junction `worktree/node_modules` hacia una única instalación compartida por hash.
- Elimina instalaciones `frontend/node_modules` duplicadas al preparar un worktree.
- Limpieza automática de `.next`, `playwright-report` y `test-results` al completar.
- `PLAYWRIGHT_HTML_OPEN=never` gestionado por el runner.
- Preflight de espacio libre antes de construir un cache nuevo.
- Lock de cache con recuperación de locks stale después de reinicios.
- Upgrade de config v1.4 -> v1.5 sin pisar settings personalizados.

## v1.4.0
- Fix Windows `WinError 206`: prompts/diffs de Claude por stdin.
- Structured reviewer output vía `--json-schema`.
- Resume de stage persistido después de bloqueos humanos.
- `resume --stage review|fast|targeted|full`.
- Default reviewer diff 60k para ahorrar contexto.

## v1.3
- Reviewer Claude reanuda la misma sesión ante `error_max_turns` dentro de un presupuesto acotado.
- `max_turns` no consume fix/review loops.
- Evidencia Git determinística para el reviewer y logs separados por reanudación.

## v1.2
- Retry determinístico y baseline comparison para no gastar fix loops en fallos preexistentes/flaky verificables.

## v1.1
- Bootstrap de dependencias por worktree, espera por cuota, resume y dependencias nombradas.
