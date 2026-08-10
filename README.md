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
<http://localhost:8000/docs>. Flower monitors the Celery worker at <http://127.0.0.1:5555>; its
unauthenticated interface is bound to localhost and is not exposed on external network interfaces.
The migration container applies pending migrations before the API, worker, and Beat scheduler start.

This release replaces the complete Alembic history with one fresh-install baseline. It has no
upgrade, backfill, reconciliation, or supported `alembic stamp` path for an existing database.
Destroy and recreate PostgreSQL before deploying this version.

To run Python tooling directly on the host:

```bash
uv sync --all-groups
uv run pytest
uv run ruff check .
uv run ty check src
```

Copy `.env.example` to `.env` before running the API, migrations, worker, Beat, or Flower directly on
the host. The example is organized into `COMMUN`, `API`, `WORKER`, `BEAT`, `MIGRATE`, and `FLOWER`
sections. `COMMUN` identifies values consumed by more than one service; it does not mean that every
value is injected into every container. Compose explicitly gives each service only its required
subset. `MIGRATE` uses only the common database URL, while `FLOWER` uses the common Celery broker and
`FLOWER_UNAUTHENTICATED_API=true`. This opens Flower's internal API without authentication only on
the localhost-bound interface. The worker publishes task events so Flower can display live task
activity.

## HTTP API

All endpoints are under `/api/v1`:

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | API liveness |
| `GET` | `/databases?page=1&page_size=20&name=primary` | Paginated database pool list |
| `GET` | `/databases/statistics` | Global and per-database usage |
| `POST` | `/databases/sync` | Request database synchronization |
| `GET` | `/databases/{id}` | Get one database pool entry |
| `GET` | `/databases/{id}/check` | Check the stored database connection |
| `POST` | `/databases` | Add a database to the pool |
| `PUT` | `/databases/{id}` | Replace database metadata or rotate its password |
| `DELETE` | `/databases/{id}` | Delete an unused database |
| `GET` | `/instances?page=1&page_size=20` | Paginated instance list |
| `GET` | `/instances/{id}` | Get one instance |
| `GET` | `/instances/{id}/admin` | Get the initialized Coder administrator credentials |
| `GET` | `/instances/{id}/status` | Get the live Argo CD status |
| `POST` | `/instances` | Request instance creation |
| `POST` | `/instances/{id}/start` | Start or resynchronize an instance |
| `POST` | `/instances/{id}/stop` | Stop remote workspaces and remove only the Argo CD Application |
| `POST` | `/instances/{id}/sync` | Force Argo CD reconciliation |
| `GET` | `/instances/{id}/provider` | Get the Kubernetes provider upload status |
| `GET` | `/instances/{id}/provider/configuration` | Download the decrypted kubeconfig |
| `POST` | `/instances/{id}/provider` | Upload the provider kubeconfig and update the instance |
| `DELETE` | `/instances/{id}` | Request instance deletion |
| `GET` | `/instances/{id}/members?page=1&page_size=20` | List instance members |
| `GET` | `/instances/{id}/members/{member_id}` | Get one instance member |
| `POST` | `/instances/{id}/members` | Request member creation |
| `PUT` | `/instances/{id}/members/{member_id}` | Request a member role change |
| `DELETE` | `/instances/{id}/members/{member_id}` | Request member deletion |
| `GET` | `/templates?page=1&page_size=20&scope=global` | Paginated template list |
| `GET` | `/templates/statistics` | Per-template deployment statistics |
| `GET` | `/templates/{id}` | Get one template |
| `GET` | `/templates/{id}/modules` | Get a template's module names |
| `POST` | `/templates` | Create a template |
| `PUT` | `/templates/{id}` | Replace a template's mutable fields |
| `POST` | `/templates/{id}/sync` | Queue current-branch synchronization |
| `DELETE` | `/templates/{id}` | Delete a template |
| `GET` | `/templates/{id}/images?page=1&page_size=20` | List allowed Docker images |
| `GET` | `/templates/{id}/images/{image_id}` | Get one allowed Docker image |
| `POST` | `/templates/{id}/images` | Allow an immutable Docker image |
| `DELETE` | `/templates/{id}/images/{image_id}` | Remove an unused Docker image |
| `GET` | `/templates/{id}/parameters?page=1&page_size=20` | List redacted template parameters |
| `GET` | `/templates/{id}/parameters/{parameter_id}` | Get one redacted template parameter |
| `POST` | `/templates/{id}/parameters` | Create a user or system parameter |
| `PUT` | `/templates/{id}/parameters/{parameter_id}` | Replace a parameter definition |
| `DELETE` | `/templates/{id}/parameters/{parameter_id}` | Delete a parameter definition |
| `GET` | `/workspaces?page=1&page_size=20` | Paginated and filtered workspace list |
| `GET` | `/workspaces/{id}` | Get one workspace |
| `POST` | `/workspaces` | Request workspace creation |
| `PUT` | `/workspaces/{id}` | Replace a workspace's mutable configuration |
| `DELETE` | `/workspaces/{id}` | Request workspace deletion |

## Database pool API

Every Coder instance reserves one logical PostgreSQL schema from the global database pool. Add a
pool entry with:

```json
{
  "name": "Primary",
  "instance_max": 20,
  "host": "postgres.internal",
  "port": 5432,
  "database_name": "coder",
  "username": "coder_admin",
  "password": "write-only-password"
}
```

Set `CODER_MANAGER_CRYPTO_KEY` to a base64-encoded 32-byte key, for example with
`openssl rand -base64 32`. Only the password is encrypted with AES-256-GCM in `password_enc`; it is
never returned by the API. `PUT` keeps the existing password when the field is omitted. Database
names are case-insensitively unique, and entries with active allocations cannot be deleted or
reduced below their current usage.

`GET /api/v1/databases/statistics` reports total capacity, allocations, available slots, and
utilization percentages globally and for every pool entry. These values are derived from allocation
rows rather than stored counters.

`GET /api/v1/databases/{id}/check` decrypts the stored password and opens a short-lived PostgreSQL
connection to validate the configured host, port, database, username, and password. Connection
errors are returned without exposing credentials. `POST /api/v1/databases/sync` accepts a global
synchronization request, persists a `database.sync` job, and enqueues
`coder_manager.database.sync.step_01_sync_database`. Its response contains the persisted `job`, so
the request remains observable and retryable even when the broker is temporarily unavailable.

## Instances API

Instances are identified by their application and environment; they do not have their own name.
The list endpoint accepts an optional `application` query parameter.

Creation payload:

```json
{
  "application": "MY-BUSINESS-APPLICATION",
  "environment": "development"
}
```

Supported environments are `development`, `staging`, and `production`. A new instance starts with
`state` set to `stopped`, `action` set to `creating`, and `status` set to `pending`. `state` is an
observed value stored only by Coder Manager: `started` means that the Argo CD Application exists,
while `stopped` means that it is absent. It does not describe Argo health or pod readiness. Actions
include `starting` and `stopping`; statuses are limited to `pending`, `running`, `success`, and
`error`.

`application` is an externally managed free-form identifier. It is trimmed, converted to uppercase,
and limited to 255 characters. Coder Manager does not verify it against an internal catalog. The
combination of application and environment remains unique.

Instance creation is split into three durable steps. The first opens a short-lived PostgreSQL
connection to the allocated database and executes `CREATE SCHEMA IF NOT EXISTS` with the schema
name passed as a quoted identifier. The second creates or attaches an Argo CD Application whose
`metadata.name` is `<CODER_MANAGER_ARGOCD_APPLICATION_PREFIX>-<instance slug>`. The slug is required;
there is no UUID fallback. Existing attached Application names are retained after their first
successful reconciliation. The Application uses a Helm chart from the configured Git repository
through the `argocd-cyberark-plugin-helm` plugin. The third creates or recovers Coder's first
administrator account before the instance reaches success.
The plugin receives comma-separated `users` and `admins` values through `HELM_ARGS`, plus a
`cyberark` map containing `appId`, `certName`, `keyName`, `region`, and `safe` parameters. The
`region` value comes from `CODER_MANAGER_ARGOCD_REGION` and is normalized to uppercase. Commas in
the two Helm scalar assignments are backslash-escaped so Helm keeps each list as one value; the
chart still receives the comma-separated string.
Both the Argo CD destination and `HELM_ARGS` target the `app-coder-system` namespace.
`HELM_ARGS` loads `values-dev.yaml`, `values-stg.yaml`, or `values-prd.yaml` for development,
staging, or production respectively.
`HELM_ARGS` sets `global.baseDomain` to the immutable instance URL's hostname without the
`https://` scheme, sets `global.identifier` to the required immutable instance slug, and supplies
the allocated managed database's
`server.config.postgres.host`, `database`, and `schema` values. The PostgreSQL username and password
use the CyberArk references `<secret:<name>#username>` and `<secret:<name>#password>`, where
`<name>` comes from the allocated managed database's `name` field, not its `database_name` field.
Neither credential value is included in the Argo CD Application payload.
When a Kubernetes provider is configured, it also supplies the uploaded file as a single-line
RFC 4648 Base64 value through `server.config.kube`.
The slug names the Argo CD Application metadata; Coder Manager does not add Helm
`--name-template`, `nameOverride`, or `fullnameOverride` arguments.
`CODER_MANAGER_DEFAULT_ADMINS` is a comma-separated list that is always included in both Helm
values without creating API member records. The static bootstrap username `admin` is always
included in the allowed-user and administrator values.

Configure Argo CD with `CODER_MANAGER_ARGOCD_URL`, `CODER_MANAGER_ARGOCD_TOKEN`,
`CODER_MANAGER_ARGOCD_PROJECT`, `CODER_MANAGER_ARGOCD_REPOSITORY_URL`,
`CODER_MANAGER_ARGOCD_REPOSITORY_PATH`, `CODER_MANAGER_ARGOCD_TARGET_REVISION`,
`CODER_MANAGER_ARGOCD_REGION`, and
one destination per environment with
`CODER_MANAGER_ARGOCD_<ENVIRONMENT>_DESTINATION_NAME`. Configure one CyberArk plugin map for each
environment. Variable names follow `CODER_MANAGER_CYBERARK_<ENVIRONMENT>_<FIELD>`, where
environments are `DEVELOPMENT`, `STAGING`, and `PRODUCTION`, and fields are `APP_ID`, `CERT_NAME`,
`KEY_NAME`, and `SAFE`. The region, all three destinations, and all 12 CyberArk values are required
for Argo CD reconciliation; `.env.example` lists the complete configuration. TLS certificate
verification is enabled by default; set
`CODER_MANAGER_ARGOCD_SKIP_SSL_VERIFY=true` only for an explicitly trusted test environment. The
worker requests synchronization but does not wait for Argo CD health convergence.

`POST /api/v1/instances/{id}/sync` creates an `instance.update` job for an idle successful or failed
instance. Pending, running, and deleting instances return HTTP 409. Only one job can own an instance
at a time; there is no parallel force mode.

`POST /api/v1/instances/{id}/start` creates an `instance.start` job and moves the lifecycle action
to `starting` without changing `state`. The worker requires the slug, managed PostgreSQL allocation,
and stored Coder administrator credentials. It performs the complete Argo reconciliation even when
the Application already exists, cleans up unreferenced Coder accounts, and sets `state=started`
only after Argo confirms creation or adoption. Missing data fails the job; no bootstrap or
credential fallback is attempted.

`POST /api/v1/instances/{id}/stop` creates an `instance.stop` job and moves the lifecycle action to
`stopping` without changing `state`. If the Application exists, the worker retrieves every remote
Coder workspace whose latest build is `running` or `starting`, including paginated results, submits
a stop build for each one, and waits until all submitted builds are `stopped`. It repeats the
workspace scan before continuing. Retries also wait for an already `stopping` latest build without
submitting a duplicate. Only then does it delete the Argo CD Application in cascade and set
`state=stopped`. If the Application is already absent, the workflow is already converged and
finishes idempotently. A Coder error or timeout preserves the Application and previous state.
`CODER_MANAGER_WORKSPACE_STOP_POLL_INTERVAL_SECONDS` controls polling (2 seconds by default), and
`CODER_MANAGER_WORKSPACE_STOP_TIMEOUT_SECONDS` sets the global deadline (1800 seconds by default).
Stop never deletes the local instance, database schema or allocation, members, workspace rows,
provider configuration, or secrets.

`DELETE /api/v1/instances/{id}` keeps its four durable deletion steps. The first step requires the
stored Coder administrator credentials and removes every non-deleted Coder workspace before any
instance resource is removed. If the Argo CD Application is absent, the worker first reconciles it
from the persisted instance configuration, records the observed instance as `started`, and waits
for Coder authentication. It then waits for any active workspace build, submits non-orphan
`delete` builds, waits for each build to reach `deleted`, and repeats the complete paginated scan
until Coder returns no workspaces. A retry observes an existing delete build instead of submitting
a duplicate. Only after the empty final scan does deletion remove the Application, drop the
PostgreSQL schema, and delete the local configuration. Any Coder failure or timeout keeps all
remaining instance resources available for retry.
`CODER_MANAGER_WORKSPACE_DELETE_POLL_INTERVAL_SECONDS` controls polling (2 seconds by default), and
`CODER_MANAGER_WORKSPACE_DELETE_TIMEOUT_SECONDS` sets the per-attempt global deadline (1800 seconds
by default) for Coder readiness, active builds, delete builds, and the final scan.

Both power routes return HTTP 202 with `{ "resource": ..., "job": ... }`, return 404 for an unknown
instance, and return 409 while another transition is active or deletion is in progress.

`GET /api/v1/instances/{id}/status` reads Argo CD directly and returns the Application name, sync
and health statuses, current operation phase, revision, and latest reconciliation timestamp.

The bootstrap account has the static username `admin`, email `admin@coder.local`, and display name
`Coder Admin`. Coder Manager generates a unique password and, only after Coder confirms a successful
bootstrap, encrypts it in
`instances.password_enc` with `CODER_MANAGER_CRYPTO_KEY`, and binds the ciphertext to the instance
UUID. `GET /api/v1/instances/{id}/admin` returns the static username and email with the decrypted
password whenever `password_enc` is present; it does not depend on `job_executions`. A bootstrap job
skips the remote bootstrap when the instance already has a stored password. Failed or running
bootstrap attempts leave the password unset and unavailable. The response uses
`Cache-Control: no-store`.

`POST /api/v1/instances/{id}/provider` is a create-only `multipart/form-data` upload whose required
file field is named `kubeconfig`. Coder Manager does not validate the filename, media type, size,
content, or whether the file is empty. The raw bytes are encrypted with AES-256-GCM in
`kubeconfig_enc` and bound to the instance UUID. The accepted upload moves the instance to
`updating/pending` and creates an `instance.update` job. A configured provider cannot be replaced,
and there is no provider `PUT` endpoint. `GET` returns `kubeconfig_configured` and timestamps
without exposing file or ciphertext material.
`GET /api/v1/instances/{id}/provider/configuration` decrypts and returns the original bytes as an
`application/octet-stream` attachment named `kubeconfig`. Successful and error responses use
`Cache-Control: no-store`; missing instances or providers return 404, while unavailable encryption
or an unauthenticatable envelope returns a redacted 503.

The API generates an immutable, globally unique, 12-character lowercase alphanumeric slug for each
new instance and exposes it as `slug`. The immutable HTTPS URL combines that slug with the
environment; for example, slug `k7m4p2x9q3ab` in `development` receives
`https://k7m4p2x9q3ab.code-studio.dev.echonet`. Environment DNS labels are `dev`, `staging`, and
`cib` for development, staging, and production respectively. The `code-studio` DNS label defaults
from `CODER_MANAGER_INSTANCE_DOMAIN` and can be changed for newly created instances.

Deletion is asynchronous. It is accepted after a successful create, update, start, or stop, returns
HTTP 202, and changes the lifecycle to `deleting/pending`. Its four steps reserve workspace cleanup,
remove the Argo CD Application idempotently, execute `DROP SCHEMA IF EXISTS ... CASCADE`, then
transactionally remove the local workspaces, members, database allocation, provider configuration,
and instance. Local configuration is retained until the fourth step succeeds.

Every endpoint that starts a resource job returns `{ "resource": ..., "job": ... }`; database
synchronization returns `{ "job": ... }`. `GET /api/v1/jobs/{job_id}` exposes the current step,
status, attempt, resource reference, and timestamps. Instance and workspace reads also expose their
latest `job_id` and active `step`; the step becomes null after successful completion.
Instance responses expose `slug`, `state`, `created_at`, and `updated_at`; the latter changes
whenever the instance lifecycle changes. They also expose the assigned `database_id` and
deterministic `schema_name`; no database password is returned.

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
be deleted while it still owns workspaces. Deletion first removes the username from the Argo CD
access policy. The dedicated `step_02_cleanup_users` then compares every Coder account with the
active instance members, deletes all unreferenced accounts, and only then removes locally deleting
members. Accounts missing from Coder are already converged; any other remote failure leaves the
member and job retryable. The bootstrap `admin` account and usernames configured through
`CODER_MANAGER_DEFAULT_ADMINS` are always referenced and cannot be removed through the members API.

## Templates API

Templates are either global or attached to one externally managed application identifier. Template
names are case-insensitively unique among global templates and separately within each application.

Creation payload:

```json
{
  "display_name": "Python Development",
  "name": "python-development",
  "scope": "application",
  "application": "MY-BUSINESS-APPLICATION",
  "git_url": "git@git.example.com:coder/python-template.git",
  "source_path": "templates/python",
  "branch": "main",
  "modules": ["code-server", "git-config"]
}
```

When creating a template without editable modules, `modules` can be omitted; the API persists and
returns an empty list:

```json
{
  "display_name": "Managed Desktop",
  "name": "managed-desktop",
  "scope": "global",
  "application": null,
  "git_url": "https://git.example.com/coder/managed-desktop.git",
  "source_path": ".",
  "branch": "main"
}
```

Set `scope` to `global` and `application` to `null` for a global template. Application identifiers
are normalized like instance identifiers and are not checked against an internal catalog.
`display_name` is the mutable human-readable label. `name` is the immutable lowercase slug used
inside Coder. Git URLs accept HTTPS, `ssh://`, or
SCP-style SSH syntax. `source_path` is repository-relative and defaults to `.`, while `branch`
targets one exact `refs/heads/...` branch. On creation, modules default to an empty list; when
present, they must be ordered without duplicates. PUT replaces `display_name`, `git_url`,
`source_path`, `branch`, and `modules`; scope, application, and `name` remain immutable. Only module
compatibility is checked against existing workspaces. The removed CPU, RAM, and disk fields are
rejected with HTTP 422. `GET /templates/{id}/modules` returns the module array directly.

### Template parameters

Parameters use an immutable lowercase snake_case `name`, an immutable `type`, mutable display
metadata, and timestamps. Names are unique within a template. A user parameter defines the values
accepted from workspace clients:

```json
{
  "type": "user",
  "name": "project_name",
  "display_name": "Project name",
  "description": "Name used by the workspace",
  "required": true,
  "mutable": false,
  "default_value": null
}
```

A global system parameter has one write-only value:

```json
{
  "type": "system",
  "name": "registry_token",
  "display_name": "Registry token",
  "description": "",
  "scope": "global",
  "value": "write-only-secret"
}
```

An environment-scoped system parameter requires exactly one value for every supported environment,
without fallback:

```json
{
  "type": "system",
  "name": "registry_url",
  "display_name": "Registry URL",
  "description": "",
  "scope": "environment",
  "values": {
    "development": "registry.dev.example.com",
    "staging": "registry.stg.example.com",
    "production": "registry.example.com"
  }
}
```

System values are encrypted with AES-256-GCM using the parameter UUID and concrete target as
associated data. Reads expose only `value_configured` or the three `values_configured` flags.
Omitting `value` or `values` on PUT retains the existing encrypted values; changing only display
metadata does not advance the system parameter revision. `type`, `name`, and system `scope` cannot
be changed. Parameter mutations are rejected while that template is synchronizing.

`POST /templates/{id}/sync` returns an empty HTTP 202 response after committing a durable
fire-and-forget job. The worker fetches the current branch HEAD once and synchronizes it to every
ready compatible instance. Global templates target all ready instances; application templates
target only matching normalized application identifiers. System parameters are resolved for each
instance environment and sent to Coder as `user_variable_values`. The version name is
`git-<commit>-p<system_parameter_revision>`. A system value change immediately makes existing
deployments outdated, but synchronization remains manual. CoderManager stores only the current
per-instance deployment state and exposes no local template-version history.

`GET /templates/statistics` returns one object per template with `updated`, `outdated`, and
`missing` ready-server counts. `updated` means the durable deployment state is successful and both
its applied commit and applied system parameter revision match their targets and the current
template revision. Any other existing deployment is `outdated`, while a compatible ready instance
without a deployment is `missing`. The endpoint reads only the local database and does not contact
Git or Coder.

The worker image contains Git and OpenSSH. Mount the SSH identity read-only for `appuser`. SSH uses
batch mode, disables host-key verification and `known_hosts`, uses identity-only authentication,
and disables agent forwarding. `CODER_MANAGER_TEMPLATE_SYNC_POLL_INTERVAL_SECONDS` controls Coder
import polling
(2 seconds by default), and `CODER_MANAGER_TEMPLATE_SYNC_TIMEOUT_SECONDS` bounds an individual
import (1800 seconds by default). Template archives use USTAR, exclude Terraform state and tfvars,
and must not exceed 1 MiB.

Filtering by `application` returns the global templates plus those attached to that application.
The optional `scope` filter narrows that result, and `display_name` performs a case-insensitive
literal substring search.

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
  "parameters": {
    "project_name": "demo"
  }
}
```

Workspace names follow Coder's contract: at most 32 alphanumeric or hyphen characters. PUT accepts
only `name`, `image_id`, `modules`, and `parameters`; instance, template, and owner remain immutable.
The removed `cpu`, `ram`, and `disk` fields are rejected with HTTP 422. Modules must be unique and
selected from the template; an empty module list is valid. An image change is limited to another
image from the same template.

User parameter defaults are resolved into the visible workspace snapshot. Unknown names and
missing required values are rejected. A `mutable: false` value can be assigned once but cannot
later change. Deleting a parameter definition preserves existing workspace snapshots for history,
while future Coder builds receive only parameters still defined on the template.

Creation starts in `creating/pending`; accepted updates and deletions return HTTP 202 and move to
`updating/pending` or `deleting/pending`. Reads remain available during processing. Instance-owned
mutations require a successful parent instance; workspaces in `error` can still be updated or
deleted after their parent is successful. The list supports `instance_id`, `template_id`,
`member_id`, `image_id`, `status`, and case-insensitive literal `name` filters.

The worker creates the remote workspace for the member username with `rich_parameter_values`,
adopts matching retries, and uses any known remote template even when the local deployment is
outdated. Creation returns HTTP 409 only when no remote template identifier is known. Renames are
propagated to Coder. A mutable parameter change starts and waits for a `start` build even when the
workspace was stopped. Deletion starts and waits for a `delete` build before deleting the local
row. Persisted remote workspace/build UUIDs and desired/applied revisions make retries idempotent.
`CODER_MANAGER_WORKSPACE_BUILD_POLL_INTERVAL_SECONDS` controls polling (2 seconds by default), and
`CODER_MANAGER_WORKSPACE_BUILD_TIMEOUT_SECONDS` bounds each build (1800 seconds by default).

## Celery

Every business operation is represented by a `job_executions` row and an explicitly named Celery
step. No Celery chain is used. A step locks and claims its job, increments its attempt, performs its
operation, persists the next step as `pending`, commits, and only then sends the next task. The
registry contains the exact allowlisted task names for instance create/update/start/stop/delete,
workspace create/update/delete, and database synchronization.

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

Beat also schedules `coder_manager.dispatch_daily_workspace_stops` every day at midnight in
`CODER_MANAGER_SCHEDULER_TIMEZONE` (`Europe/Paris` by default). The dispatcher reads every stored
instance without filtering its lifecycle state and sends one independent
`coder_manager.stop_instance_workspaces` task per instance, allowing the Celery worker pool to
process instances in parallel. Each task reads the stored Coder URL and administrator credentials,
lists all `running`, `starting`, and `stopping` workspaces directly from Coder, and submits one
`stop` build for every `running` or `starting` workspace. A workspace already `stopping` is left
untouched. The task returns immediately after all submissions: it does not call Argo CD, poll build
status, rescan Coder, retry failures, create a `JobExecution`, or update instance/workspace rows.
Submission failures are logged after the task has attempted the remaining workspaces for that
instance, and a failed instance does not prevent the independently dispatched tasks for the others.

Before mutating an existing Argo CD Application, instance reconciliation, start, stop, and deletion
read its current operation phase. An Application in `Running` or `Terminating` is left untouched:
the owned job and any members claimed by an update return to `pending` on the same step without an
exception, and Beat retries them on a later scan.

Beat also runs `coder_manager.check_instance_states` every hour. It observes only idle instances
that are not being deleted: an Argo `2xx` stores `started`, and a `404` stores `stopped`. Missing
configuration, transport failures, and any other response retain the previous state and are logged.
The result is committed only when the instance job, action, and status still match the snapshot
taken before the remote request. This scanner performs no remote mutation and creates no
`JobExecution`.

The single Alembic baseline creates the complete current schema directly and has
`down_revision = None`. Its downgrade removes the complete schema. It is intentionally fresh-only:
an existing PostgreSQL database must be destroyed and recreated, and `alembic stamp` is not a
supported deployment procedure. Deploy the schema with the same image as the API, worker, and Beat
so every process uses the matching task registry and database contract.

FastAPI and Alembic keep the asynchronous SQLAlchemy engine backed by `asyncpg`. Celery tasks use a
separate synchronous engine backed by `psycopg`; each worker process creates its own one-connection
pool after the process starts and disposes it during process shutdown. The worker derives the sync
driver from `CODER_MANAGER_DATABASE_URL`, so the API, migrations, and worker continue to share one
database URL setting.

Member changes are reconciled by a two-step `instance.update` workflow rather than individual
member tasks. `step_01_update_instance` claims pending members and reconciles the Argo CD access
policy. `step_02_cleanup_users` lists every Coder account, preserves active local members plus the
configured protected administrators, deletes every other account, and then finalizes local member
creations, role changes, and deletions. Member writes may coalesce during step 1 but return HTTP 409
during the cleanup snapshot; changes already queued by then create a new `instance.update` job.
Otherwise member, provider, and workspace mutations require a successful parent, so they cannot
overwrite a failed creation or deletion before Beat retries it.
