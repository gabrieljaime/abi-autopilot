# ABI Autopilot v1.7.0

ABI Autopilot es un runner local para implementar, revisar, validar e integrar issues de GitHub con agentes de código. Cada issue se trabaja en un Git worktree aislado y sólo avanza cuando supera las validaciones configuradas.

El proyecto está pensado para ejecutarse desde una máquina de confianza. No es un servicio web ni necesita guardar tokens: usa las sesiones ya autenticadas de GitHub CLI, Codex CLI y Claude Code.

## Características

- Un worktree y una rama por issue.
- Codex o Claude como implementador configurable.
- Claude como reviewer estructurado.
- Validaciones dirigidas, rápidas y completas definidas en JSON.
- Reintentos y espera ante límites de cuota o fallos transitorios.
- Integración manual individual o por lotes.
- Cache compartido opcional de dependencias npm.
- Estado y logs locales para observar o reanudar ejecuciones.
- Defaults conservadores: el daemon no integra ni cierra issues automáticamente.

## Requisitos

- Python 3.11 o compatible.
- Git.
- GitHub CLI autenticado con `gh auth login`.
- Codex CLI si se usa Codex.
- Claude Code CLI si se usa Claude como reviewer, implementador o fallback.
- Node.js y npm sólo si las validaciones del repositorio objetivo los requieren.

ABI Autopilot no instala ni configura estas herramientas por su cuenta.

## Inicio rápido

Cloná este repositorio y entrá en su carpeta. Luego creá la configuración local indicando explícitamente el repositorio objetivo:

```powershell
python autopilot.py init `
  --repo "C:\path\to\your-repository" `
  --repo-slug "owner/repository" `
  --base-branch "main"
```

En macOS o Linux se usan los mismos argumentos con una ruta POSIX y continuaciones `\`.

El comando crea `config.local.json`; ese archivo y sus backups están ignorados por Git. Revisalo antes de continuar y ejecutá:

```powershell
python autopilot.py doctor
python -m unittest discover -s tests -v
python autopilot.py bootstrap-labels
python autopilot.py observe
```

`doctor --probe-agents` también hace una llamada mínima a cada agente y puede consumir cuota.

## Configuración

[`config.example.json`](config.example.json) documenta la configuración completa. `init` genera un `config.local.json` con los mismos defaults y las rutas derivadas del repositorio objetivo.

Las secciones principales son:

| Sección | Controla |
| --- | --- |
| `repo_path`, `repo_slug`, `base_branch` | Repositorio local, repositorio GitHub y rama de integración |
| `autopilot` | Polling, paralelismo, loops, auto-triage e implementador |
| `codex`, `claude` | Comandos, modelos, permisos y límites de cada agente |
| `availability` | Espera y backoff ante cuota o fallos transitorios |
| `workspace_bootstrap` | Preparación de cada worktree |
| `validation` | Comandos targeted, fast, full y batch |
| `validation_workers` | Paralelismo de Vitest, pytest y Playwright |
| `integration` | Política de validación, push, comentarios y cierre |
| `dependency_cache` | Cache compartido y artefactos regenerables |
| `protected_paths` | Rutas que los agentes no pueden modificar |
| `process_env` | Variables de entorno inyectadas en subprocesses |

Los comandos de validación del ejemplo asumen un repositorio con `frontend/` y `backend/`. Adaptalos al stack del repositorio objetivo; no hace falta modificar el código de Autopilot.

Para elegir implementador o fallback:

```json
{
  "autopilot": {
    "implementer": "codex",
    "implementer_fallback": "claude"
  }
}
```

Usá `null` en `implementer_fallback` para desactivar el fallback. Los valores admitidos son `codex` y `claude`.

Para actualizar una configuración creada por una versión anterior:

```powershell
python autopilot.py upgrade-config
```

El upgrade conserva las opciones existentes, agrega defaults nuevos y crea un backup local ignorado por Git.

## Flujo de una issue

Primero marcá la issue como lista y comprobá el plan:

```powershell
python autopilot.py mark-ready 123 --risk medium
python autopilot.py run --issue 123 --dry-run
```

Después ejecutala:

```powershell
python autopilot.py run --issue 123
```

Si una corrida quedó interrumpida o bloqueada, se puede reanudar sin descartar el trabajo:

```powershell
python autopilot.py resume --issue 123
```

El daemon procesa la cola continuamente; `--once` ejecuta un único ciclo:

```powershell
python autopilot.py daemon --once
python autopilot.py daemon
```

Con `autopilot.auto_ready=true`, cada ciclo puede etiquetar automáticamente issues nuevas sin ningún label `agent:*`. Omite epics, dependencias abiertas y issues con `needs:human` o `needs:product`.

## Integración

Integrar una issue terminada:

```powershell
python autopilot.py integrate --issue 123
```

La integración recupera el commit aprobado, lo valida en un worktree temporal, comprueba que la rama base remota no haya cambiado y recién entonces hace push y, según la configuración, cierra la issue.

Si hay un conflicto, el worktree queda preservado. Después de resolverlo manualmente:

```powershell
python autopilot.py integrate --issue 123 --continue
```

Planificar o ejecutar varias issues `agent:done`:

```powershell
python autopilot.py integrate-done --issues 123 124 125
python autopilot.py integrate-done --issues 123 124 125 --execute
```

Sin `--execute` sólo se muestra el plan. En modo batch, los cambios se validan juntos y no se publica nada hasta que el gate final queda verde.

## Seguridad

- `auto_integrate` y `auto_close` vienen desactivados.
- No se usa force push, `reset --hard`, stash, rebase ni `git clean`.
- Los conflictos se conservan para inspección manual.
- El cleanup se limita a artefactos configurados y nunca borra rutas trackeadas.
- `.env`, `backend/data/` y cualquier ruta agregada a `protected_paths` quedan protegidos.
- Las credenciales deben permanecer en los mecanismos de autenticación de cada CLI, nunca en `config.local.json`.
- `runtime/` puede contener prompts, diffs, salidas de agentes y metadatos de issues; es privado y está ignorado por Git.

Consultá [`SECURITY.md`](SECURITY.md) para el modelo operativo completo.

## Comandos

Todos los comandos se ejecutan como `python autopilot.py <comando>`. Esta tabla resume qué hace cada uno y qué estado puede modificar:

| Comando | Qué hace | Efectos |
| --- | --- | --- |
| `version` | Muestra la versión instalada de ABI Autopilot. | Sólo lectura. |
| `init` | Crea la configuración para un repositorio objetivo. | Escribe `config.local.json` y crea `runtime/`. No reemplaza una configuración existente salvo que se use `--force`. |
| `upgrade-config` | Fusiona una configuración anterior con los defaults de la versión actual. | Crea un backup y actualiza `config.local.json` sin reemplazar valores personalizados. |
| `doctor` | Comprueba configuración, repositorio, rama remota, autenticación de GitHub, herramientas, versiones y espacio disponible. | No modifica el repositorio objetivo; puede crear la carpeta configurada para el cache. |
| `bootstrap-labels` | Crea o actualiza en GitHub los labels que usa el flujo `agent:*` y `risk:*`. | Modifica labels del repositorio GitHub. |
| `observe` | Muestra las issues disponibles, en ejecución, bloqueadas y terminadas. | Normalmente sólo lectura. Si `autopilot.auto_ready=true`, también puede etiquetar issues nuevas. |
| `mark-ready` | Asigna el riesgo y deja una issue lista para ejecutar. | Cambia sus labels a `agent:ready` y `risk:<nivel>`. |
| `auto-ready` | Revisa todas las issues abiertas sin estado `agent:*` y prepara las elegibles. | Agrega `agent:ready` y el riesgo por defecto. Omite epics, dependencias abiertas y decisiones humanas pendientes. |
| `run` | Ejecuta el pipeline de una issue: worktree, agente implementador, validaciones, review, correcciones y commit. | Modifica el worktree y los labels/comentarios de la issue; no integra por sí solo a la rama base. |
| `resume` | Continúa una ejecución conservando el worktree y el estado persistido. | Reactiva la issue y continúa desde el stage guardado o indicado. |
| `daemon` | Observa la cola y procesa issues elegibles continuamente. | Puede ejecutar el pipeline y modificar GitHub. Con `--once` realiza un solo ciclo. |
| `cleanup` | Elimina el worktree de una issue terminada. | Sólo lo elimina si está limpio; conserva la rama local para trazabilidad. |
| `integrate` | Verifica e integra una issue `agent:done` en la rama base. | Ejecuta validaciones, puede hacer push y puede cerrar la issue. Los conflictos se preservan. |
| `integrate-done` | Ordena y planifica la integración de varias issues terminadas. | Sin `--execute` sólo muestra el plan; con `--execute` valida, integra, publica y opcionalmente cierra. |
| `release-audit` | Verifica que las issues cerradas estén realmente en la rama de despliegue y muestra la divergencia integración↔despliegue. | Sólo lee, salvo con `--promote`, que cierra como entregadas las issues ya promovidas. |
| `release-status` | Estado de entrega de una issue puntual, con la evidencia que lo respalda. | Sólo lectura. |

### Integración y despliegue no son lo mismo

Autopilot distingue dos ramas:

```yaml
base_branch:       integration/phase5-refactors   # donde se integra cada issue
deployment_branch: main                           # la que realmente se despliega
```

Si se omite `deployment_branch`, se asume `base_branch` y el flujo es el de
siempre: integrar y cerrar en un paso.

Cuando son distintas, el ciclo de vida tiene tres etapas y `agent:done` pasa a
significar **entregado**, no “mergeado en algún lado”:

```text
implementado   agent:ready → agent:running → agent:review
integrado      agent:integrated   merge en la rama de integración; la issue queda ABIERTA
entregado      agent:done         alcanzable desde la rama de despliegue; recién acá se cierra
```

La promoción se confirma con tres señales, de más fuerte a más débil, porque
comparar SHAs no alcanza —un cherry-pick o un rebase cambian el SHA sin cambiar
el trabajo:

| Evidencia | Significado |
|---|---|
| `ancestor` | El commit es alcanzable desde la rama de despliegue. |
| `patch-equivalent` | `git cherry` lo marca `-`: el parche ya existe upstream con otro SHA. |
| `message-match` | La rama de despliegue tiene commits con el prefijo de la issue, pero el parche difiere. Pasa cuando la promoción resolvió conflictos; conviene revisar el diff. |

Una issue no se cierra mientras `git cherry` deje trabajo propio en `+`.

#### Política de release

```text
implementación → integración → validación → lote de promoción → rama de despliegue → cierre
```

La rama de integración no debe divergir indefinidamente. `release-audit` avisa
en cuanto ambas ramas acumulan trabajo exclusivo:

```text
RELEASE DRIFT
  main unique commits: 30
  integration/phase5-refactors unique commits: 30

  WARNING: integración y rama de despliegue divergieron.
  No cerrar nuevas issues como released hasta reconciliar.
```

Con `release.block_close_on_drift: true` la divergencia además bloquea
`--promote`. El umbral del aviso se ajusta con `release.drift_warn_commits`.

### Configuración inicial

```powershell
python autopilot.py init `
  --repo "C:\path\to\your-repository" `
  --repo-slug "owner/repository" `
  --base-branch "main"
```

- `--repo`: ruta local del repositorio que será modificado.
- `--repo-slug`: nombre de GitHub en formato `owner/repository`.
- `--base-branch`: rama remota donde se integrarán los cambios aprobados.
- `--force`: permite reemplazar `config.local.json`. Usalo sólo si querés regenerar la configuración desde cero.

### Diagnóstico y observación

```powershell
python autopilot.py doctor
python autopilot.py doctor --probe-agents
python autopilot.py observe
```

`--probe-agents` comprueba que Codex y Claude puedan responder realmente. Hace una interacción mínima con cada proveedor y puede consumir cuota.

### Preparar y ejecutar issues

```powershell
python autopilot.py mark-ready 123 --risk medium
python autopilot.py run --issue 123 --dry-run
python autopilot.py run --issue 123
```

- `--risk low|medium|high` determina qué validaciones y políticas adicionales se aplican.
- `--dry-run` verifica elegibilidad y crea o reutiliza el worktree, pero no llama agentes ni cambia código o labels.
- `--force` permite ejecutar una issue aunque no tenga `agent:ready` o falle una condición de elegibilidad. Es una vía de escape para uso deliberado.
- `--resume-existing` hace que `run` reutilice el trabajo existente; normalmente es más claro usar `resume`.

Para continuar una ejecución interrumpida:

```powershell
python autopilot.py resume --issue 123
python autopilot.py resume --issue 123 --stage review
```

`--stage` permite retomar explícitamente desde `targeted`, `fast`, `review` o `full`. Requiere que el worktree ya tenga cambios y debería usarse sólo cuando el estado automático no representa la etapa correcta.

### Procesamiento continuo

```powershell
python autopilot.py daemon --once
python autopilot.py daemon
```

`--once` es útil para tareas programadas: consulta y procesa como máximo un ciclo, y después termina. Sin esa opción, espera el intervalo `autopilot.poll_seconds` y continúa hasta que se interrumpe el proceso.

### Integración y cierre

```powershell
python autopilot.py integrate --issue 123
python autopilot.py integrate --issue 123 --no-close
python autopilot.py integrate --issue 123 --continue
```

- `--no-close` publica la integración pero mantiene abierta la issue.
- `--continue` continúa un cherry-pick cuyo conflicto ya fue resuelto manualmente en el worktree preservado.

Para varias issues:

```powershell
python autopilot.py integrate-done --issues 123 124 125
python autopilot.py integrate-done --issues 123 124 125 --execute
python autopilot.py integrate-done --execute --no-close
```

- `--issues` fija el orden exacto. Si se omite, Autopilot obtiene las issues `agent:done` y prioriza cambios pequeños y de menor riesgo.
- Sin `--execute`, el comando es un plan de sólo lectura.
- `--execute` habilita la integración efectiva y se detiene ante el primer conflicto o fallo.
- `--no-close` mantiene abiertas las issues después de un push exitoso.

Usá `python autopilot.py <comando> --help` para consultar la sintaxis vigente.

## Estructura

```text
autopilot.py          CLI principal
abi_autopilot/        implementación
prompts/              instrucciones para implementador y reviewer
scripts/              helpers de PowerShell
tests/                suite de unittest
config.example.json   referencia completa de configuración
runtime/              estado y logs locales (ignorado)
```

## Desarrollo

La suite no necesita servicios externos:

```powershell
python -m unittest discover -s tests -v
```

Antes de publicar cambios, verificá también:

```powershell
python autopilot.py version
python -m compileall -q autopilot.py abi_autopilot
```

Los cambios de versión se resumen en [`CHANGELOG.md`](CHANGELOG.md).
