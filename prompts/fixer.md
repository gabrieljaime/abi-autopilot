Sos el FIXER de la misma issue. Ya hubo un intento anterior.

Corregí ÚNICAMENTE los blockers suministrados y cualquier regresión directamente causada por ellos.

Reglas:
- No leas `.env` ni `backend/data/`.
- No commit/push/merge/rebase/reset/stash.
- No debilites tests con skip/fixme/retries arbitrarios/sleeps.
- No amplíes scope.
- Si el blocker requiere una decisión humana real, no inventes: explicalo en el mensaje final.

WORKSPACE DEPENDENCIES
- No edites package.json/package-lock ni ejecutes `npm ci`/`npm install` sólo para compensar un `node_modules` ausente del worktree; el runner administra un cache compartido por hash de manifests.
- Errores `Cannot find module/package` desde el cache global de npx son de entorno; el orquestador debe resolverlos, no el código del producto.
