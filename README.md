# Coder Manager

FastAPI and Celery foundation for managing Coder infrastructure. Business applications are owned
by an external system and represented here only by normalized identifiers on instances and scoped
templates. Argo CD Applications remain managed as part of the instance lifecycle.

## Stack

- FastAPI HTTP API
- PostgreSQL with SQLAlchemy 2 and Alembic
- Celery workers with Redis as broker and result backend
- uv, Ruff, ty, and pytest for local development

## Run locally

The complete stack starts with one command:

```bash
docker compose up --build
```

The API is then available at <http://localhost:8000>, with interactive documentation at
<http://localhost:8000/docs>. The migration container applies pending migrations before the API,
worker, and Beat scheduler start.

To run Python tooling directly on the host:

```bash
uv sync --all-groups
uv run pytest
uv run ruff check .
uv run ty check src
```

Copy `.env.example` to `.env` before running the API, migrations, worker, or Beat directly on the
host.

## HTTP API

All endpoints are under `/api/v1`:

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | API liveness |
| `GET` | `/databases?page=1&page_size=20&region=emea` | Paginated database pool list |
| `GET` | `/databases/statistics` | Global, regional, and per-database usage |
| `POST` | `/databases/sync` | Request database synchronization |
| `GET` | `/databases/{id}` | Get one database pool entry |
| `GET` | `/databases/{id}/check` | Check the stored database connection |
| `POST` | `/databases` | Add a database to the pool |
| `PUT` | `/databases/{id}` | Replace database metadata or rotate its password |
| `DELETE` | `/databases/{id}` | Delete an unused database |
| `GET` | `/instances?page=1&page_size=20` | Paginated instance list |
| `GET` | `/instances/{id}` | Get one instance |
| `GET` | `/instances/{id}/status` | Get the live Argo CD status |
| `POST` | `/instances` | Request instance creation |
| `POST` | `/instances/{id}/sync` | Force Argo CD reconciliation |
| `GET` | `/instances/{id}/provider` | Get the Kubernetes provider configuration |
| `POST` | `/instances/{id}/provider` | Create the provider and update the instance |
| `PUT` | `/instances/{id}/provider` | Update the provider CA or token |
| `DELETE` | `/instances/{id}` | Request instance deletion |
| `GET` | `/instances/{id}/members?page=1&page_size=20` | List instance members |
| `GET` | `/instances/{id}/members/{member_id}` | Get one instance member |
| `POST` | `/instances/{id}/members` | Request member creation |
| `PUT` | `/instances/{id}/members/{member_id}` | Request a member role change |
| `DELETE` | `/instances/{id}/members/{member_id}` | Request member deletion |
| `GET` | `/templates?page=1&page_size=20&scope=global` | Paginated template list |
| `GET` | `/templates/{id}` | Get one template |
| `GET` | `/templates/{id}/modules` | Get a template's module names |
| `POST` | `/templates` | Create a template |
| `PUT` | `/templates/{id}` | Replace a template's mutable fields |
| `DELETE` | `/templates/{id}` | Delete a template |
| `GET` | `/templates/{id}/images?page=1&page_size=20` | List allowed Docker images |
| `GET` | `/templates/{id}/images/{image_id}` | Get one allowed Docker image |
| `POST` | `/templates/{id}/images` | Allow an immutable Docker image |
| `DELETE` | `/templates/{id}/images/{image_id}` | Remove an unused Docker image |
| `GET` | `/workspaces?page=1&page_size=20` | Paginated and filtered workspace list |
| `GET` | `/workspaces/{id}` | Get one workspace |
| `POST` | `/workspaces` | Request workspace creation |
| `PUT` | `/workspaces/{id}` | Replace a workspace's mutable configuration |
| `DELETE` | `/workspaces/{id}` | Request workspace deletion |

## Database pool API

Every Coder instance reserves one logical PostgreSQL schema from a database in its region. Add a
pool entry with:

```json
{
  "name": "EMEA primary",
  "region": "emea",
  "instance_max": 20,
  "host": "postgres-emea.internal",
  "port": 5432,
  "database_name": "coder",
  "username": "coder_admin",
  "password": "write-only-password"
}
```

Set `CODER_MANAGER_CRYPTO_KEY` to a base64-encoded 32-byte key, for example with
`openssl rand -base64 32`. Only the password is encrypted with AES-256-GCM in `password_enc`; it is
never returned by the API. `PUT` keeps the existing password when the field is omitted. Database
names are case-insensitively unique, and entries with active allocations cannot be deleted, moved
to another region, or reduced below their current usage.

`GET /api/v1/databases/statistics` reports total capacity, allocations, available slots, and
utilization percentages globally, by region, and for every pool entry. These values are derived
from allocation rows rather than stored counters.

`GET /api/v1/databases/{id}/check` decrypts the stored password and opens a short-lived PostgreSQL
connection to validate the configured host, port, database, username, and password. Connection
errors are returned without exposing credentials. `POST /api/v1/databases/sync` accepts a global
synchronization request, persists a `database.sync` job, and enqueues
`coder_manager.database.sync.step_01_sync_database`. Its response contains the persisted `job`, so
the request remains observable and retryable even when the broker is temporarily unavailable.

## Instances API

Instances are identified by their application, region, and environment; they do not have their own
name. The list endpoint accepts an optional `application` query parameter.

Creation payload:

```json
{
  "application": "MY-BUSINESS-APPLICATION",
  "region": "emea",
  "environment": "development"
}
```

Supported regions are `emea`, `apac`, and `amer`. Supported environments are `development`,
`staging`, and `production`. A new instance starts with `action` set to `creating` and `status` set
to `pending`. Actions are otherwise free-form strings for future provisioning workflows, while
statuses are limited to `pending`, `running`, `success`, and `error`.

`application` is an externally managed free-form identifier. It is trimmed, converted to uppercase,
and limited to 255 characters. Coder Manager does not verify it against an internal catalog. The
combination of application, region, and environment remains unique.

Instance creation is split into two durable steps. The first opens a short-lived PostgreSQL
connection to the allocated database and executes `CREATE SCHEMA IF NOT EXISTS` with the schema
name passed as a quoted identifier. The second creates or attaches an Argo CD Application named
`<CODER_MANAGER_ARGOCD_APPLICATION_PREFIX>-<instance UUID without dashes>`. The Application uses a
Helm chart from the configured Git repository through the `argocd-cyberark-plugin-helm` plugin.
The plugin receives comma-separated `users` and `admins` values through `HELM_ARGS`, plus a
`cyberark` map containing `appId`, `certName`, `keyName`, `region`, and `safe` parameters.
`HELM_ARGS` loads `values-global.yaml` first, then `values-dev.yaml`, `values-stg.yaml`, or
`values-prd.yaml` according to the instance environment so environment-specific values take
precedence.
`CODER_MANAGER_DEFAULT_ADMINS` is a comma-separated list that is always included in both Helm
values without creating API member records.

Configure Argo CD with `CODER_MANAGER_ARGOCD_URL`, `CODER_MANAGER_ARGOCD_TOKEN`,
`CODER_MANAGER_ARGOCD_PROJECT`, `CODER_MANAGER_ARGOCD_REPOSITORY_URL`,
`CODER_MANAGER_ARGOCD_REPOSITORY_PATH`, `CODER_MANAGER_ARGOCD_TARGET_REVISION`, and
`CODER_MANAGER_ARGOCD_DESTINATION_NAME`. Configure one CyberArk plugin map for each of the nine
region/environment combinations. Variable names follow
`CODER_MANAGER_CYBERARK_<REGION>_<ENVIRONMENT>_<FIELD>`, where regions are `EMEA`, `APAC`, and
`AMER`, environments are `DEVELOPMENT`, `STAGING`, and `PRODUCTION`, and fields are `APP_ID`,
`CERT_NAME`, `KEY_NAME`, and `SAFE`. All 36 values are required for Argo CD reconciliation. The
plugin `region` parameter comes directly from the instance region in uppercase; `.env.example`
lists the complete matrix. TLS certificate verification is enabled by default; set
`CODER_MANAGER_ARGOCD_SKIP_SSL_VERIFY=true` only for an explicitly trusted test environment. The
worker requests synchronization but does not wait for Argo CD health convergence.

`POST /api/v1/instances/{id}/sync` creates an `instance.update` job for an idle successful or failed
instance. Pending, running, and deleting instances return HTTP 409. Only one job can own an instance
at a time; there is no parallel force mode.

`GET /api/v1/instances/{id}/status` reads Argo CD directly and returns the Application name, sync
and health statuses, current operation phase, revision, and latest reconciliation timestamp.

`POST /api/v1/instances/{id}/provider` creates the Kubernetes provider with `host`, `namespace`,
`ca`, and the write-only `token`. `PUT` updates `ca` and optionally rotates `token`; `host` and
`namespace` are immutable, whether omitted or repeated unchanged in the update payload. The token
is encrypted with AES-256-GCM in `token_enc` and bound to the instance UUID. Every accepted change
moves the instance to `updating/pending` and creates an `instance.update` job.
`GET` returns `token_configured` and never token material.

The API generates an immutable HTTPS URL from the application identifier, region, and environment. For
example, application `My App` in `emea` and `development` receives
`https://my-app.emea.code-studio.dev.echonet`. Environment DNS labels are `dev`, `staging`, and
`cib` for development, staging, and production respectively. The `code-studio` DNS label defaults
from `CODER_MANAGER_INSTANCE_DOMAIN` and can be changed for newly created instances.

Deletion is asynchronous. It is accepted after `creating/success` or `updating/success`, returns
HTTP 202, and changes the state to `deleting/pending`. Its four steps reserve workspace cleanup,
remove the Argo CD Application idempotently, execute `DROP SCHEMA IF EXISTS ... CASCADE`, then
transactionally remove the local workspaces, members, database allocation, provider configuration,
and instance. Local configuration is retained until the fourth step succeeds.

Every endpoint that starts a resource job returns `{ "resource": ..., "job": ... }`; database
synchronization returns `{ "job": ... }`. `GET /api/v1/jobs/{job_id}` exposes the current step,
status, attempt, resource reference, and timestamps. Instance and workspace reads also expose their
latest `job_id` and active `step`; the step becomes null after successful completion.
Instance responses expose both `created_at` and `updated_at`; the latter changes whenever the
instance action or status changes. They also expose the assigned `database_id` and deterministic
`schema_name`; no database password is returned.

## Instance members API

Members belong to exactly one instance and are addressed by their generated UUID. To add a member,
send a username and one of the supported roles:

```json
{
  "username": "Alice.Example",
  "role": "user"
}
```

Usernames are trimmed, converted to lowercase, limited to 255 characters, and unique within an
instance. Supported roles are `user` and `admin`. A new member starts in `creating/pending`. Role
changes use `updating/pending`, and deletion requests use `deleting/pending`; deleted members remain
available for a future worker. Member statuses are `pending`, `running`, `success`, and `error`.

Member creation, role changes, and deletion return HTTP 409 while the parent instance is `pending`
or `running`; member reads remain available. A member can only be changed after its previous action
has succeeded. Repeating a successful member's current role with PUT is an idempotent HTTP 200
response and does not change `updated_at`; accepted role changes return HTTP 202. A member cannot
be deleted while it still owns workspaces.

## Templates API

Templates are either global or attached to one externally managed application identifier. Template
names are case-insensitively unique among global templates and separately within each application.

Creation payload:

```json
{
  "name": "Python Development",
  "scope": "application",
  "application": "MY-BUSINESS-APPLICATION",
  "git_url": "https://git.example.com/coder/python-template.git",
  "modules": ["code-server", "git-config"],
  "version": "v1.0.0",
  "min_cpu_count": 1,
  "max_cpu_count": 8,
  "min_ram_gb": 2,
  "max_ram_gb": 32,
  "min_disk_gb": 10,
  "max_disk_gb": 100
}
```

Set `scope` to `global` and `application` to `null` for a global template. Application identifiers
are normalized like instance identifiers and are not checked against an internal catalog. Git URLs must use
HTTPS. Versions are free-form Git references, and modules must be a non-empty ordered list without
duplicates. CPU counts, RAM GB, and disk GB are positive integers with inclusive minimum and
maximum bounds. PUT replaces these limits together with `name`, `git_url`, `modules`, and `version`;
scope and application remain immutable. A change that would invalidate an existing workspace
returns HTTP 409. `GET /templates/{id}/modules` returns the module array directly.

Filtering by `application` returns the global templates plus those attached to that application.
The optional `scope` filter narrows that result, and `name` performs a case-insensitive literal
substring search.

## Template Docker images API

Each template owns an allowlist of immutable Docker image references. To add one:

```json
{
  "registry": "registry.example.com",
  "name": "company/python",
  "version": "3.13"
}
```

Registry and image names are trimmed and normalized to lowercase. The tuple `registry`, `name`, and
`version` is unique within a template. Updating an image in place is intentionally unsupported;
create a new entry for a new version. Images referenced by workspaces cannot be deleted.

## Workspaces API

Workspace creation requires a ready owner from the selected instance, an available global or
application-scoped template, and an image allowed by that template:

```json
{
  "name": "alice-development",
  "instance_id": "c0d8d7a7-b54c-4f89-b344-06d28bd3f685",
  "template_id": "7f4cfd54-456f-4195-894d-f709d147fa7c",
  "member_id": "043a736a-1bfd-431f-9382-1402c91a6b02",
  "image_id": "d7555af5-d499-4368-9f39-d6e0bfdaf69c",
  "modules": ["code-server"],
  "cpu": 2,
  "ram": 8,
  "disk": 20
}
```

CPU, RAM, and disk are checked inclusively against the template limits during creation. PUT accepts
only `name`, `image_id`, `modules`, `cpu`, and `ram`; instance, template, owner, and disk are
immutable. Every PUT revalidates the complete candidate configuration, including the stored disk,
inside the same transaction as the update. Modules must be unique and selected from the template;
an empty module list is valid. An image change is limited to another image from the same template.

Creation starts in `creating/pending`; accepted updates and deletions return HTTP 202 and move to
`updating/pending` or `deleting/pending`. Reads remain available during processing. Instance-owned
mutations require a successful parent instance; workspaces in `error` can still be updated or
deleted after their parent is successful. The list supports `instance_id`, `template_id`,
`member_id`, `image_id`, `status`, and case-insensitive literal `name` filters.

## Celery

Every business operation is represented by a `job_executions` row and an explicitly named Celery
step. No Celery chain is used. A step locks and claims its job, increments its attempt, performs its
operation, persists the next step as `pending`, commits, and only then sends the next task. The
registry contains the exact allowlisted task names for instance create/update/delete, workspace
create/update/delete, and database synchronization.

The API creates a resource and its job in the same transaction. It attempts the first delivery only
after commit; a broker failure therefore leaves a recoverable `pending` job. Step completion is
fenced by `job_id`, step, and attempt, so a worker returning after a retry cannot overwrite the
newer attempt. Duplicate or stale deliveries are safe no-ops.

The dedicated `beat` service schedules `coder_manager.retry_job_executions` every 60 seconds by
default. Configure the scan interval with `CODER_MANAGER_JOB_RETRY_INTERVAL_SECONDS` and the stale
running threshold with `CODER_MANAGER_JOB_STALE_AFTER_SECONDS` (300 seconds by default). The scanner
redelivers the exact allowlisted step for `pending` and `error` jobs and first returns expired
`running` jobs to `pending`. Unknown task names are logged and ignored. The healthcheck and scanner
are intentionally not tracked as jobs.

The initial Alembic baseline creates job rows and lifecycle columns directly. Deploy schema changes
with the same image as the API, worker, and Beat so every process uses the matching task registry
and database contract.

FastAPI and Alembic keep the asynchronous SQLAlchemy engine backed by `asyncpg`. Celery tasks use a
separate synchronous engine backed by `psycopg`; each worker process creates its own one-connection
pool after the process starts and disposes it during process shutdown. The worker derives the sync
driver from `CODER_MANAGER_DATABASE_URL`, so the API, migrations, and worker continue to share one
database URL setting.

Member changes are reconciled by `coder_manager.instance.update.step_01_update_instance` rather
than individual member tasks. One pass claims the currently pending members, finalizes member
creations and role changes, deletes members marked for deletion, and creates a new `instance.update`
job when changes arrived while it was running. Otherwise member, provider, and workspace mutations
require a successful parent, so they cannot overwrite a failed creation or deletion before Beat
retries it.
