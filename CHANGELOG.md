# Changelog

## Unreleased

- Guía para escribir issues en ambos READMEs: los agentes leen sólo título y
  cuerpo (no comentarios), el cuerpo se envía al proveedor de IA, tamaño y
  contenido recomendados, ejemplo bueno y malo. La plantilla Markdown ya no
  sugiere poner el riesgo en el texto (se lee del label) y suma *Context* y
  *Verification*. Nuevo formulario de GitHub `examples/autopilot-issue-form.yml`
  para copiar a `.github/ISSUE_TEMPLATE/` del proyecto.
- `init` interactivo con detección de stack: pregunta sólo lo que falta,
  deduce `owner/repo` y la rama desde `origin`, y arma `workspace_bootstrap` y
  `validation` desde Node.js (scripts de package.json; npm, pnpm o yarn),
  Python (pytest, ruff, mypy), Go y Rust en la raíz y en carpetas de primer
  nivel. `--non-interactive` para scripts; `--no-detect` conserva el ejemplo
  frontend/backend. `doctor` avisa si no hay ninguna validación configurada.
- Instalable con pip/pipx (`pyproject.toml`, comando `abi-autopilot`,
  `python -m abi_autopilot`). La config y `runtime/` viven en la carpeta
  *home*: el clon con `python autopilot.py`, o `$ABI_AUTOPILOT_HOME` / el
  directorio actual con el comando instalado. La CLI pasó a
  `abi_autopilot/cli.py` y los prompts a `abi_autopilot/prompts/`; una copia
  en `<home>/prompts/` los reemplaza.
- CI en GitHub Actions: tests en Windows, macOS y Linux con Python 3.11 y
  3.13, más instalación del paquete y prueba del comando.
- `autopilot.max_parallel` ahora funciona (antes se ignoraba): con un valor
  mayor que 1 el daemon corre hasta N issues a la vez, cada una en un proceso
  `run --issue N` con log en `runtime/daemon/`. Fetch, creación de worktrees,
  pushes y el baseline de cada commit base se serializan con locks entre
  procesos (`<worktree_root>/.locks/`). Con 1 (default) nada cambia.
- Mensajes de la CLI, comentarios en GitHub, descripciones de labels y prompts
  de los agentes traducidos al inglés. Los comentarios de finalización
  anteriores ("Autopilot completó #N" / "Base inicial") se siguen reconociendo,
  así que las issues ya implementadas pueden integrarse sin cambios.
- `optional` y `recommended` también marcan dependencias no bloqueantes.
- El aviso legal aclara que se necesitan cuentas propias de Claude y/o Codex.
- Preparación para código abierto: sin supuestos del proyecto original en la
  lógica. La detección de "cambió código" ya no exige `frontend/`/`backend/`
  (antes, en otro layout, la integración salteaba todas las validaciones);
  se configura con `integration.code_paths`.
- Nueva condición `"when": "changed:<ruta>"` para validaciones.
- Las referencias nombradas de dependencias (antes fijas `UX|COACH|MRAG`) se
  configuran en `dependency_ref_prefixes`. **Migración:** si las usabas,
  agregá tus prefijos a `config.local.json`.
- Los prompts listan las `protected_paths` configuradas en lugar de rutas fijas.
- `toolchain` ya no fija Python 3.11 / Node 20 por defecto; `doctor` detecta
  Node según los comandos configurados y avisa sobre `cwd` inexistentes.
- Cache probe por defecto `node_modules/.package-lock.json` (antes `jsdom`).
- Comentarios en GitHub con pie de versión, copyright, licencia MIT y descargo;
  la CLI muestra el mismo aviso en `version`, `--help`, `init`, `doctor` y `daemon`.
- Encabezados SPDX en el código fuente.
- README en inglés detallado para usuarios nuevos (instalación y login de
  `gh`, Codex CLI y Claude Code, referencia de configuración, comandos, códigos
  de salida, troubleshooting); el README en español pasa a `README.es.md`.
- Contacto de seguridad: Gabriel Jaime <gabrielsjaime@gmail.com>.
- `.gitignore` también ignora `*-autopilot-worktrees/` y `*-autopilot-deps/`.
- Fix: el backup de `upgrade-config` se llamaba siempre `.pre-v1.7.0.bak`
  aunque la CLI anunciaba el de la versión actual.
- `config.example.json` regenerado desde los defaults (faltaban
  `deployment_branch` y `release`).

- Dependency cache robusto en Windows: la publicación `.building-* → <key>` se
  reintenta con backoff acotado (`PUBLISH_RETRY_DELAYS`) ante `PermissionError`
  / WinError 5 / 32, y un `npm ci` exitoso ya no bloquea la issue por un rename
  transitorio (incidente #211).
- Validez de cache explícita: marker `.abi-autopilot-ready.json` con
  `schema_version`, `cache_key` y `kind` + probe. Un destino inválido se pone en
  cuarentena (`.<key>.corrupt-*`) en lugar de borrarse; uno válido publicado por
  otro proceso se reutiliza.
- `.building-*` sólo se limpia por antigüedad; un build completo cuyo publish
  falló se preserva y se reutiliza en el siguiente bootstrap sin reinstalar.
- `BootstrapResult.status` distingue `INSTALL_FAILED`, `CACHE_VALIDATE_FAILED`,
  `CACHE_PUBLISH_FAILED` (rc 8) y `CACHE_ATTACH_FAILED`; los rc previos no cambian.

## v1.8.1

- Cierra el bypass del gate de release en `finalize`: con `auto_integrate` y
  `auto_close`, un fast-forward a la rama de integración cerraba la issue sin
  mirar la rama de despliegue. Ahora queda `agent:integrated` y abierta.
- Etapa nueva `agent:implemented` (review y validación PASS, sin integrar).
  `agent:done` deja de significar "esperando integración" y queda sólo para
  *entregado*. `integrate`/`integrate-done` aceptan `agent:implemented` y, por
  compatibilidad, las `agent:done` abiertas.
- `release-audit` corre la auditoría de consistencia también con integración ==
  despliegue (antes devolvía OK sin revisar nada) y revisa hasta
  `release.audit_limit` issues cerradas (default 1000; antes 200 fijo).
- `observe` cuenta IMPLEMENTED / INTEGRATED / RELEASED / PENDING PROMOTION por
  separado; `PENDING PROMOTION` ya no mezcla issues que nunca se integraron.
- Tests: `tests/test_release_lifecycle.py` (13 casos).

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
