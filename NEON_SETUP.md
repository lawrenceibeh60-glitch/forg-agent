# Neon services for Forge

Forge remains a Python/Flask application. `neon.ts` is Neon deployment configuration, not a replacement application entrypoint.

## Declared services

- **Postgres:** the project and `production` branch are recorded in `.neon.example`. Copy it to `.neon` locally; `.neon` itself is intentionally ignored by Git.
- **Auth:** Neon Managed Auth is enabled in the service configuration. Forge already uses GitHub-based access control; do not replace that identity flow without an explicit migration plan.
- **AI Gateway:** enabled in the service configuration. Forge's model routing remains configured through `XKIRO_BASE_URL`, `XKIRO_MODEL`, and `XKIRO_API_KEY`; enabling the gateway does not automatically route requests through it.
- **Functions:** `hello.ts` is a minimal deployment/smoke endpoint.
- **Realtime:** enabled for Neon preview branch configuration.

## Environment variables

Configure these as encrypted hosting-provider environment variables, never commit their values:

- `DATABASE_URL`: pooled Neon PostgreSQL URL used for app requests.
- `DATABASE_URL_POOLED`: explicit pooled connection alias.
- `DATABASE_URL_DIRECT`: direct connection reserved for migration/admin operations.
- `TASK_DB_BACKEND=postgres`: force PostgreSQL selection even when `TASK_DB_PATH` is present for local SQLite fallback compatibility.

`forge/tasks.py` and `forge/intelligence.py` already support `DATABASE_URL` through `psycopg`. `TASK_DB_BACKEND=sqlite` remains available for local development and tests.

## CLI operations

After copying `.neon.example` to `.neon` and authenticating the Neon CLI in an interactive terminal, run from the repository root:

```sh
npm run neon:plan
npm run neon:deploy
```

CLI authentication is separate from the database connection string. A database URL alone does not authorize creation/deployment of Neon-managed services. Verify the deployed branch and service status in Neon, then run Forge's Python tests and an application-level database smoke test.

## Build and test

Forge is Python-based and does not have an `npm run build` script. The npm manifest exists only for Neon configuration tooling; use the repository's Python CI workflow for application verification.
