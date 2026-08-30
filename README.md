# Coder Manager

FastAPI and Celery foundation for managing Coder infrastructure. Business applications are owned
by an external system and represented here only by normalized identifiers on instances. Templates
form one global catalog and are assigned explicitly to individual instances; no template is
assigned by default. Argo CD Applications remain managed as part of the instance lifecycle.

## Stack

- FastAPI HTTP API
- PostgreSQL with SQLAlchemy 2 and Alembic
- Celery workers with Redis as broker and a result backend reserved for the worker healthcheck
- uv, Ruff, ty, and pytest for local development

## Run locally

Set `CODER_MANAGER_DATABASE_SCHEMA` and configure OIDC, or explicitly set
`CODER_MANAGER_ALLOW_UNAUTHENTICATED_API=true` for a local unauthenticated API. You can instead copy
`.env.example` to `.env` and complete those values before starting the stack:

```bash
docker compose up --build
```

The API is then available at <http://localhost:8000>, with interactive documentation at
<http://localhost:8000/docs>. Flower monitors the Celery worker at <http://127.0.0.1:5555>; its
unauthenticated interface is bound to localhost and is not exposed on external network interfaces.
Prometheus endpoints listen inside the Compose network at `http://api:9808/metrics`,
`http://worker:9808/metrics`, and `http://beat:9808/metrics`. Each component also exposes its process
liveness at `/health` on port `9808`. Port `9808` is not published on the host, and the API does not
expose `/metrics` or `/health` on its application port `8000`. The migration container applies
pending migrations before the API, worker, and Beat scheduler start.

The migration history is the single fresh-install baseline `e669afab6842`. It contains the complete
current schema and has no upgrade path from any earlier Coder Manager database. Recreate the
configured PostgreSQL schema or database before deploying this version; never use `alembic stamp`
to attach the new history to existing objects.

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
subset. `MIGRATE` uses the common database URL and schema, while `FLOWER` uses the common Celery broker
and `FLOWER_UNAUTHENTICATED_API=true`. This opens Flower's internal API without authentication only on
the localhost-bound interface. The worker publishes task events so Flower can display live task
activity.

Credentials embedded in `CODER_MANAGER_DATABASE_URL` must be URL-encoded. For example, the password
`*%?` is written as `%2A%25%3F` in the URL. Keep the standard single `%` characters in `.env`; the
Alembic migration process handles its internal interpolation escaping.

## OIDC authentication

The API fails to start when `CODER_MANAGER_OIDC_ISSUER_URL` is empty unless
`CODER_MANAGER_ALLOW_UNAUTHENTICATED_API=true` explicitly opts into an open API. The opt-in is false
by default and must only be used for an intentionally unauthenticated environment. When an issuer is
configured, every endpoint under `/api/v1` requires an `Authorization: Bearer <JWT>` header, even if
the unauthenticated opt-in is also present. The API validates the token's RS256 signature against the
provider's discovered JWKS and requires matching `iss` and `exp` claims. `/docs`, `/openapi.json`,
and `/docs/oauth2-redirect` remain public, as do the internal metrics and health endpoints on port
`9808`.

Configure the resource server and Swagger OAuth2 client with:

```dotenv
CODER_MANAGER_OIDC_ISSUER_URL=https://auth.example.com/realms/coder
CODER_MANAGER_OIDC_CLIENT_ID=coder-manager-swagger
CODER_MANAGER_OIDC_AUTHORIZATION_URL=https://auth.example.com/realms/coder/protocol/openid-connect/auth
CODER_MANAGER_OIDC_TOKEN_URL=https://auth.example.com/realms/coder/protocol/openid-connect/token
CODER_MANAGER_OIDC_SCOPES=openid,profile
```

For a deliberately open local API instead, leave the OIDC values empty and set:

```dotenv
CODER_MANAGER_ALLOW_UNAUTHENTICATED_API=true
```

Client ID, authorization URL, and token URL are required whenever the issuer is set. All OIDC URLs
must use HTTPS, and the scope list must contain `openid`. Authorization requires a top-level `roles`
claim containing a JSON array of strings with the `admin` role. Role matching is case-insensitive but
does not normalize whitespace: `admin` and `ADMIN` match, while ` admin ` does not. A missing or
malformed `roles` claim, or a claim without `admin`, returns HTTP `403` with `{"detail":"Access denied"}`.

Swagger uses the Authorization Code flow with PKCE. Register
`https://<api-host>/docs/oauth2-redirect` as an exact redirect URI in the identity provider, configure
the Swagger client as public, enable Authorization Code and PKCE S256, and do not assign it a client
secret. `CODER_MANAGER_OIDC_CLIENT_ID` configures Swagger's public OAuth2 client. Non-browser clients
can obtain a token independently and send it directly as a Bearer token.

At startup, the API retrieves `<issuer>/.well-known/openid-configuration`, requires an exact issuer
match, validates the HTTPS `jwks_uri`, and preloads at least one RS256 signing key. Startup fails if
the provider is unavailable or its metadata is invalid. Signing keys are cached and refreshed when a
token uses an unknown `kid`; cached keys continue to work during a later provider outage.

## Prometheus metrics

The API, worker, and Beat each expose unauthenticated `GET /metrics` and `GET /health` endpoints on
their internal port `9808`. The health endpoint returns `{"status":"ok"}` for process liveness;
every other path is rejected. Configure the listener bind address with
`CODER_MANAGER_METRICS_HOST` and its port with `CODER_MANAGER_METRICS_PORT`. Compose injects those
settings only into the three metric producers and declares the port with `expose` without publishing
it on the host.

The API reports request counts by method, normalized FastAPI route, and status; request durations by
method and normalized route; and in-progress requests by method. Unmatched paths use the fixed
`unmatched` route label, so request UUIDs and arbitrary paths do not create unbounded Prometheus
series. The API metrics listener is separate from Uvicorn on port `8000`, so scrapes do not
instrument themselves.

The worker reports completed tasks by task name and state, task durations, and tasks currently in
progress. Its prefork children write to `PROMETHEUS_MULTIPROC_DIR`, which the
`coder-manager-celery` launcher clears before Celery imports the Prometheus client. The parent worker
aggregates those files for each scrape. Beat reports successful scheduled task publications. Each
Celery process exposes only the series belonging to its own component.

To verify all three endpoints from the Compose network:

```bash
docker compose exec api python -c 'import urllib.request; print(urllib.request.urlopen("http://api:9808/metrics").status)'
docker compose exec api python -c 'import urllib.request; print(urllib.request.urlopen("http://worker:9808/metrics").status)'
docker compose exec api python -c 'import urllib.request; print(urllib.request.urlopen("http://beat:9808/metrics").status)'
```

The same checks can target `/health` on `api`, `worker`, and `beat`; each request returns HTTP `200`
with `{"status":"ok"}`.

## HTTP API

All endpoints are under `/api/v1`:

| Method | Path | Description |
| --- | --- | --- |
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
| `GET` | `/instances/{id}/templates?page=1&page_size=20` | List templates explicitly assigned to an instance |
| `PUT` | `/instances/{id}/templates/{template_id}` | Assign one template to an instance |
| `DELETE` | `/instances/{id}/templates/{template_id}` | Remove one template from an instance |
| `GET` | `/templates?page=1&page_size=20&display_name=Python` | Paginated template catalog |
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

Instances are identified by their application; they do not have their own name or environment.
The list endpoint accepts an optional `application` query parameter.

Creation payload:

```json
{
  "application": "MY-BUSINESS-APPLICATION"
}
```

A new instance starts with `state` set to `stopped`, `action` set to `creating`, and `status` set to
`pending`. `state` is an
observed value stored only by Coder Manager: `started` means that the Argo CD Application exists,
while `stopped` means that it is absent. It does not describe Argo health or pod readiness. Actions
include `starting` and `stopping`; statuses are limited to `pending`, `running`, `success`, and
`error`.

`application` is an externally managed free-form identifier. It is trimmed, converted to uppercase,
and limited to 255 characters. Coder Manager does not verify it against an internal catalog. The
application remains globally unique within one Coder Manager deployment.

Instance creation is split into three durable steps. The first opens a short-lived PostgreSQL
connection to the allocated database and executes `CREATE SCHEMA IF NOT EXISTS` with the schema
name passed as a quoted identifier. The second creates or attaches an Argo CD Application whose
`metadata.name` is `<CODER_MANAGER_ARGOCD_APPLICATION_PREFIX>-<instance slug>`. The
slug is required; there is no UUID fallback. Existing Applications are accepted only when their
`coder-manager/instance-id` label already matches the local instance UUID; an absent or different
owner is a conflict and is never adopted, overwritten, observed, or deleted. Attached Application
names are retained after their first successful reconciliation. Application metadata contains the
managed labels `coder-manager/instance-id=<instance UUID>`,
`region=<normalized CODER_MANAGER_ARGOCD_REGION>`, `domain=code-station`, and `tier=standard`.
Reconciliation removes the obsolete `environment` label, refreshes these managed labels, and
preserves labels owned by other actors. The Application uses a Helm chart from the configured Git
repository through the
`argocd-cyberark-plugin-helm` plugin. The third creates or recovers Coder's first administrator
account before the instance reaches success.
The plugin receives comma-separated `users` and `admins` values through `HELM_ARGS`, plus a
`cyberark` map containing `appId`, `certName`, `keyName`, `region`, and `safe` parameters.
`CODER_MANAGER_INSTANCE_BASE_DOMAIN` is shared by the API and worker and supplies the complete
hostname suffix used by every public instance URL. Argo CD labels and the CyberArk map use
`CODER_MANAGER_ARGOCD_REGION`'s normalized uppercase value. Commas in the two Helm scalar assignments are
backslash-escaped so Helm keeps each list as one value; the chart still receives the comma-separated
string.
Both the Argo CD destination and `HELM_ARGS` target the `app-code-instance` namespace.
`HELM_ARGS` does not load an environment-specific values file and does not inject an environment
Helm value.
At reconciliation time, `HELM_ARGS` sets `global.baseDomain` to the instance's complete public
hostname (the immutable slug followed by the configured base domain), without the `https://`
scheme. It also sets
`global.identifier` to the required immutable instance slug and supplies the allocated database's
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

Configure Argo CD with `CODER_MANAGER_ARGOCD_URL`,
`CODER_MANAGER_ARGOCD_TOKEN`, `CODER_MANAGER_ARGOCD_REPOSITORY_URL`,
`CODER_MANAGER_ARGOCD_REPOSITORY_PATH`, `CODER_MANAGER_ARGOCD_TARGET_REVISION`,
`CODER_MANAGER_ARGOCD_REGION`, `CODER_MANAGER_ARGOCD_APPLICATION_PREFIX`,
`CODER_MANAGER_ARGOCD_PROJECT_NAME`, and `CODER_MANAGER_ARGOCD_DESTINATION_NAME`. Configure the
single CyberArk plugin map with `CODER_MANAGER_CYBERARK_APP_ID`,
`CODER_MANAGER_CYBERARK_CERT_NAME`, `CODER_MANAGER_CYBERARK_KEY_NAME`, and
`CODER_MANAGER_CYBERARK_SAFE`. `.env.example` lists the complete configuration. TLS certificate
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
`Coder Admin`. Coder Manager generates a unique password, encrypts it with
`CODER_MANAGER_CRYPTO_KEY`, binds the ciphertext to the instance UUID, and commits it privately in
`instances.password_candidate_enc` before contacting Coder. A retry always reuses that same
candidate, including after Coder accepted the account but the worker crashed before recording the
success. Only after Coder confirms the bootstrap does the same transaction promote the ciphertext
to `instances.password_enc`, clear the candidate, and advance the job. Failed or running attempts
therefore leave the verified password unset and unavailable. `GET /api/v1/instances/{id}/admin`
returns the static username and email with the decrypted password only when `password_enc` is
present; it does not expose the candidate or depend on `job_executions`. A bootstrap job skips the
remote bootstrap when the instance already has a stored password. The response uses
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

The API generates and stores an immutable, globally unique, 12-character lowercase alphanumeric
slug for each new instance and exposes it as `slug`. It does not persist the public URL. Whenever
the API or worker needs that URL, it combines the slug with the required
`CODER_MANAGER_INSTANCE_BASE_DOMAIN`. The setting is a complete lowercase DNS hostname without a
scheme, path, port, or trailing dot. For example, slug `k7m4p2x9q3ab` and base domain
`emea.code-studio.echonet` resolve to
`https://k7m4p2x9q3ab.emea.code-studio.echonet`.

Changing the base domain changes the calculated URL for every existing instance after both the API
and worker have restarted; it does not update instance rows or timestamps. The corresponding Argo
CD `global.baseDomain` changes only when each Application is next reconciled. Provision the new DNS
route and a matching wildcard certificate, such as `*.emea.code-studio.echonet`, before restarting
the services.
Keep both the old and new DNS, TLS, and routing paths valid throughout this transition. Coder Manager
does not enqueue a global reconciliation automatically.

Deletion is asynchronous. It is accepted after a successful create, update, start, or stop, returns
HTTP 202, and changes the lifecycle to `deleting/pending`. A failed `instance.create` cannot be
reclassified through start, stop, or sync; DELETE instead abandons that provisioning attempt. When
no verified administrator credential exists, failed-create cleanup starts directly with Application
deletion rather than trying to contact Coder. Normal deletion first reserves workspace cleanup, then
requests foreground deletion of the Argo CD Application and remains on that durable step until a
subsequent Argo `GET` returns 404. Only this confirmed absence permits
`DROP SCHEMA IF EXISTS ... CASCADE`; a successful DELETE response alone never advances destructive
database cleanup. The final transaction removes the local workspaces, members, database allocation,
provider configuration, and instance. Local configuration is retained until final cleanup succeeds.

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

Templates form a catalog independent from applications and instances. Their technical `name` is
globally unique case-insensitively; `display_name` is a mutable human-readable label and does not
participate in uniqueness.

Creation payload:

```json
{
  "display_name": "Python Development",
  "name": "python-development",
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
  "git_url": "https://git.example.com/coder/managed-desktop.git",
  "source_path": ".",
  "branch": "main"
}
```

`display_name` is the mutable human-readable label. `name` is the immutable lowercase slug used
inside Coder. Git URLs accept HTTPS, `ssh://`, or
SCP-style SSH syntax. `source_path` is repository-relative and defaults to `.`, while `branch`
targets one exact `refs/heads/...` branch. On creation, modules default to an empty list; when
present, they must be ordered without duplicates. PUT replaces `display_name`, `git_url`,
`source_path`, `branch`, and `modules`; `name` remains immutable. Only module compatibility is
checked against existing workspaces. The removed CPU, RAM, and disk fields are rejected with HTTP
422. `GET /templates/{id}/modules` returns the module array directly. The list
supports only pagination and an optional case-insensitive literal `display_name` substring filter.

Template updates and synchronizations return HTTP 409 while an explicit assignment is `pending`,
`running`, or `error`, or while a workspace mutation using the template is retryable. Catalog
deletion is also rejected while the template is synchronizing or while any instance assignment
still references it; remove every assignment through the instance route first.

### Explicit instance assignments

Creating an instance or a catalog template does not deploy or select any template. Assignment is
always explicit and addresses exactly one instance/template pair:

- `GET /instances/{instance_id}/templates?page=1&page_size=20` returns an
  `InstanceTemplatePage`. Reads remain available when the instance is stopped or busy; an unknown
  instance returns HTTP 404.
- `PUT /instances/{instance_id}/templates/{template_id}` takes no request body. It returns HTTP 202
  with `JobResourceResponse<InstanceTemplateRead>` for a new or retryable asynchronous creation,
  and HTTP 200 when the pair is already `created/success`.
- `DELETE /instances/{instance_id}/templates/{template_id}` returns HTTP 202 with the same wrapper
  for a new or retryable removal. It returns an empty HTTP 204 when the pair is already absent,
  after still validating that both the instance and catalog template exist.

`InstanceTemplateRead` contains the assignment UUID, instance UUID, nested catalog template,
`action`, `status`, `job_id`, `step`, deployment status, target/applied commits, target/applied
system-parameter revisions, and timestamps. Assignment actions are `creating`, `created`, and
`deleting`; statuses are `pending`, `running`, `success`, and `error`. Repeated accepted requests
retain the same durable job so Beat can retry failures safely.

PUT and an existing-assignment DELETE require the instance to be strictly `started/success` and the
catalog synchronization status to be `success`; a `pending`, `running`, or `error` synchronization,
a conflicting assignment action, or an inconsistent durable job returns HTTP 409.
Deletion is additionally rejected while a workspace for that pair is `pending`, `running`, or
`error`. Successful workspaces do not block removal: the worker deletes every remote workspace built
from the assigned Coder template, deletes the remote template, then removes the local workspaces,
deployment, and assignment. A failed remote operation keeps the assignment and job retryable.

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

A system parameter has one write-only value:

```json
{
  "type": "system",
  "name": "registry_token",
  "display_name": "Registry token",
  "description": "",
  "value": "write-only-secret"
}
```

System values are stored in a private one-to-one table and encrypted with AES-256-GCM using the
parameter UUID as associated data. Reads expose only `value_configured`. Omitting `value` on PUT
retains the existing encrypted value; changing only display metadata does not advance the system
parameter revision. `type` and `name` cannot be changed. The removed `scope`, `values`, and
`values_configured` fields are rejected. Parameter mutations are rejected while that template is
synchronizing or an assignment transition for it remains retryable.

`POST /templates/{id}/sync` returns an empty HTTP 202 response after committing a durable
fire-and-forget job. The worker fetches the current branch HEAD once and synchronizes only explicit
`created/success` assignments whose instances are currently `started/success`; it never creates a
missing assignment and there is no automatic instance-bootstrap synchronization. System parameters
are decrypted once per assignment and sent to Coder as `user_variable_values`. The version name is
`git-<commit>-p<system_parameter_revision>`. A system value change immediately makes an existing
deployment outdated, but synchronization remains manual. CoderManager stores only the current
deployment state for each assignment and exposes it through the instance-template list; it keeps no
local template-version history.

The worker image contains Git and OpenSSH. Mount the SSH identity read-only for `appuser`. SSH uses
batch mode, disables host-key verification and `known_hosts`, uses identity-only authentication,
and disables agent forwarding. `CODER_MANAGER_TEMPLATE_SYNC_POLL_INTERVAL_SECONDS` controls Coder
import polling
(2 seconds by default), and `CODER_MANAGER_TEMPLATE_SYNC_TIMEOUT_SECONDS` bounds an individual
import (1800 seconds by default). Template archives use USTAR, exclude Terraform state and tfvars,
and must not exceed 1 MiB.

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

Workspace creation requires a ready owner from the selected instance, a `created/success` explicit
assignment for the selected template and instance with a known remote Coder template identifier,
and an image allowed by that template:

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
later change; omitting it from a later PUT preserves its existing value, including when it is
optional and has no default. Deleting a parameter definition preserves existing workspace snapshots
for history, while future Coder builds receive only parameters still defined on the template.

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
template synchronization, template-assignment creation/deletion, workspace create/update/delete,
and database synchronization.

The API creates a resource and its job in the same transaction. It attempts the first delivery only
after commit; a broker failure therefore leaves a recoverable `pending` job. Step completion is
fenced by `job_id`, step, and attempt, so a worker returning after a retry cannot overwrite the
newer attempt. Duplicate or stale deliveries are safe no-ops.

Business and system tasks ignore Celery results: their durable state lives in PostgreSQL, while
worker events continue to feed Flower and Prometheus metrics. This also prevents API publishers from
opening an unnecessary result-backend subscription before sending a task to the broker. Only
`coder_manager.healthcheck` retains a Celery result so it can verify worker and result-backend wiring;
`CODER_MANAGER_CELERY_RESULT_BACKEND` therefore remains worker-only.

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
process instances in parallel. Each task calculates the Coder URL from the current instance base
domain, reads the stored administrator credentials, lists all `running`,
`starting`, and `stopping` workspaces directly from Coder, and submits one
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

The Alembic history contains only baseline `e669afab6842`, whose `down_revision = None` and whose
downgrade removes the complete schema, including PostgreSQL enum types. Every database carrying an
earlier revision is intentionally incompatible and must be recreated before this image starts;
`alembic stamp` is not a supported deployment procedure. Wiping the Coder Manager database does not
remove remote Argo CD Applications, Coder resources, or managed PostgreSQL schemas. Decommission
those resources before recreating a deployment that contains real instances.
Deploy migrations with the same image as the API, worker, and Beat so every process uses the
matching task registry and database contract.

FastAPI and Alembic keep the asynchronous SQLAlchemy engine backed by `asyncpg`. Celery tasks use a
separate synchronous engine backed by `psycopg`; each worker process creates its own one-connection
pool after the process starts and disposes it during process shutdown. The worker derives the sync
driver from `CODER_MANAGER_DATABASE_URL`. The API, migrations, and worker also require
`CODER_MANAGER_DATABASE_SCHEMA`, which is passed directly to PostgreSQL as the `search_path`.
Coder Manager does not create or validate this externally managed schema.

Member changes are reconciled by a two-step `instance.update` workflow rather than individual
member tasks. `step_01_update_instance` claims pending members and reconciles the Argo CD access
policy. `step_02_cleanup_users` lists every Coder account, preserves active local members plus the
configured protected administrators, deletes every other account, and then finalizes local member
creations, role changes, and deletions. Member writes may coalesce during step 1 but return HTTP 409
during the cleanup snapshot; changes already queued by then create a new `instance.update` job.
Otherwise member, provider, and workspace mutations require a successful parent, so they cannot
overwrite a failed creation or deletion before Beat retries it.
