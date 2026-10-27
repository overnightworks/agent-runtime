# Operator guide

For the operator who runs the agent service: what it enforces at start, who may
run what, what the configuration owns, and how to provide and renew the
subscription login. The service's deployment lives in edge, not here.
`agent_runtime_service.config` documents the configuration file, and the decision record
[0001](decisions/0001-agent-service.md) owns the rulings behind it. The model of
the system is [ARCHITECTURE.md](ARCHITECTURE.md); what an app sends is
[wire.md](wire.md).

## Starting the service

`agent-runtime-service --config PATH` takes a TOML file whose required keys have no
default; startup names the field of any unknown or missing one and exits 2. The
service runs only on Linux, as an unprivileged effective user, and makes itself
non-dumpable at startup before it reads its configuration; it refuses to start
otherwise. The restriction names the service only. The image must run it as a
non-root user with `cap_drop: ALL`.

`[defaults] model` is required. It names a default model as `<provider>/<model>`;
`model` may also be set in `[apps.<app>]` and in `[apps.<app>.agents.<agent>]`. Startup exits
2 and names the field (`defaults.model`,
`apps.<app>.model` or `apps.<app>.agents.<agent>.model`) for a name outside the wire's grammar
or one that no `[cli.<provider>] models` lists. An installation without a `[cli]` table is
refused naming `cli`, because it has no list to check a model against; the paid route gets its own list with the
paid-route work (#463).

`agent-runtime-service --config PATH --check` runs every step of a start up to serving,
including the configuration with its credential files, the pinned program versions and the
CA bundles, and exits 0 without binding the port. Otherwise it prints the start's one
refusal line and exits 2. Run it with the new image and file before stopping the old service.

The configuration is read once; a restart applies a change. The signing key
file, the Claude token file and any API credential file are read and checked with it.

## Who may run what

A caller presents a workload token bound to one registered app and a user token
bound to one principal (`agent_runtime_service.identity`); an app's own run, for an
agent with `runs_as_app = true`, presents the workload token alone. The checks below run
in this order for a person's run:

1. The user must be a member of the app: one of their groups is `/apps/<app>` or
   a path below it (`/apps/notes-other` is not a member of `notes`). This holds
   for `POST /runs` and `GET /runs/{id}`; anyone else gets 403 `forbidden`,
   before the body is read.
2. Their role is `admin` in `/apps/<app>/admin` or below, else `user`; a
   request whose `roles` lacks it gets 403 `forbidden`.
3. An app without an MCP origin whose request names a tool gets 403
   `forbidden`.
4. `agent_runtime_service.routing.route` decides the route; the second picture in
   [ARCHITECTURE.md](ARCHITECTURE.md) owns the order. The keys that feed it: the
   subscription groups (`[identity]
   subscription_groups`), the programs (`[cli.<provider>]`), and the API groups (`api_groups`,
   set together with `[api]`). The app sends no model: admission takes the
   run's model from the configuration, the agent's `model` in
   `[apps.<app>.agents.<agent>]`, else the app's, else `[defaults] model`. A subscription-group member's
   run goes to the CLI route of the model's provider; every other caller with API rights goes to the paid
   API, under the model's full name. A run that names
   tools and whose provider has no tool relay yet (Codex) gets 403 `not_enabled`
   before any place, capability or program, never a silent drop and never another route.

An app's own run skips steps 1 and 2: its agent's table `[apps.<app>.agents.<agent>]` must say
`runs_as_app = true`, else 403 `forbidden`; its route comes from the groups of the app's Keycloak
service account, and which groups that account holds is a rights decision (decision record H2).
The list is an aid for a cooperative app and no boundary; the groups are the boundary.

At most `[runs] max_parallel_cli_runs` (default 5) CLI runs are admitted; one
beyond gets 429 `busy` with `Retry-After` and never falls back to the API
route. The CLI runner serves Claude and Codex and checks each pinned version at startup.
The optional API runner serves buffered text and granted app tools through the
configured gateway; it takes no subscription place. Both routes use the app's
MCP origin and trust configuration. The CLI and API runners are installed independently.

## Configure the API text route

Set `[identity] api_groups` and `[api]` together; either alone stops startup.
`[api]` takes `completion_url` and an absolute `credential_file`. The URL must be an ASCII
HTTP or HTTPS URL with a DNS, IPv4 or bracketed IPv6 host, optional port 1–65535,
and exactly `/v1/chat/completions`: no user information, query, fragment, escapes
or whitespace. HTTP is for the isolated gateway network; edge owns its isolation.
HTTPS requires an absolute `ca_file`, the only trusted CA bundle, and verifies
the hostname. HTTP forbids `ca_file`; there is no TLS-disable option.

The credential file is UTF-8 JSON with only `shared_key` and/or `app_keys`.
`app_keys` maps registered app names to credentials. Every credential is nonempty
Bearer-token text (`[A-Za-z0-9._~+/-]+=*`); duplicate fields, unknown apps and
invalid entries reject the entire file. The existing secret-file rules apply
(regular file, no group/other permissions, at most 16 KiB). Store only gateway
inference credentials here, never provider keys or a gateway administration key.
The gateway's authority and retention proof belong to
[H10](decisions/0001-agent-service.md#h10-gateway-proof).

Replace this file to rotate credentials: startup validates it, and each run
reads it afresh. The authenticated app's entry wins over `shared_key`; without
either, the run fails before HTTP. Replacing a shared key with keys for each
registered app needs no service or app code change. An invalid replacement
fails closed and does not fall back to the old or shared credential.

To disable paid entitlement entirely, remove `api_groups` and `[api]` together;
API-only callers then get `not_enabled`. Either one alone is a startup error
(exit 2, naming `api` or `identity.api_groups`).
The API route is installed beside the CLI program, whose `[cli.<provider>] models` the
configuration must list. Gateway activation and paid testing require
the operator's separate approval; this route's repository proof uses fakes.

## App tools

An app that offers tools has `[apps.<app>]` with `workload_client` and
`mcp_origin`, one canonical https origin that no other app shares. The service
mints each admitted run a capability ([wire.md](wire.md)), signed with the
service's one RSA-3072 key, `[signing] key_file`. The service trusts the CA
bundle `[mcp] ca_file` names for these origins alone. The service never makes
its key; generation and rotation are the A1a4 ruling in the decision record.

An app may also run its own agent on its workload token alone. Name each such agent in
`[apps.<app>.agents.<agent>]` with `runs_as_app = true`; an absent table or `false` means the
agent never runs as the app, and a table name that is no valid agent name, a non-boolean value or an
unknown key stops startup (exit 2, naming the field). Changing the list is a configuration change
and a restart; it revokes nothing already admitted.

## The subscription login

The agent service holds the one login per provider for all apps. Only the
subscriber himself may use it, because the provider's terms allow no use on behalf
of others. The service keeps no user list; Keycloak decides, so the operator keeps
the subscription groups (`subscription_groups`) to the subscriber alone. Everyone
else reaches the API route only as a member of `api_groups`; a person in neither
group is refused `not_enabled` ([ARCHITECTURE.md](ARCHITECTURE.md)).
The file is the operator's to keep
fresh, named as `cli.<provider>.token_file`; the core library never creates, renews
or rewrites it, and the service's login command installs a new one. Claude and Codex have a
`[cli.<provider>]` table, one each, and every table needs its `token_file`; the service refuses a
table for another provider (Grok) and a leftover `[profiles]` section.

```toml
[cli.claude]
binary = "/usr/local/bin/claude"
version = "2.1.302"
models = ["sonnet"]
token_file = "/run/secrets/claude-token"
```

**Provide it (Claude).** The file holds one line, the token `claude setup-token` prints,
at a mode that gives nothing to group or others (0600 or 0400), at most 16 KiB.
JSON (a credentials file) and API keys (`sk-ant-api...`) are refused; a link is
followed and its target checked. Run `claude setup-token` once, sign in with the
subscription, and install the printed token with the login command.

**The Codex table.** `[cli.codex]` takes `binary`, `version`, `models` and `token_file`. The program must print exactly `codex-cli <version>` for
`--version`. The file is the Codex sign-in document: JSON with the sign-in's tokens, at a mode
that gives nothing to group or others, at most 16 KiB, a link followed and its target checked.
A document holding only an API key (`OPENAI_API_KEY`), a bare key line and a Claude setup
token are refused. The service hands the program only a copy without the renewal token. The
document comes from the pinned program's device-code sign-in made in a home of its own, never the
operator's terminal home, because the two would rotate each other's renewal token away; the
host's setup makes it.

**Install or check it (both providers).** `agent-runtime-service-login --config PATH --provider NAME`
reads the new document on standard input, checks it with the service's reader for that provider and
swaps it in beside the old file; the next run reads it without a restart. It exits 0 when the
file is written, 1 when it is not written and the old file stays, 2 when it refused before
reading anything (the platform, the configuration, a provider without a table, or a leftover
`.<file>.new` beside the login file, which is removed by hand), and 3 when it is not written and
its own `.<file>.new`, which may hold the new login, is not confirmed gone: it names the path.
It needs what the service's own start reads except the logins, and write access to the login
file's directory, so run it as the service's own user: the new file is created 0600 and owned by
whoever runs the command. A failure before the swap leaves the old file untouched. `--check` runs the
service's reader on the file as it stands and reads no input; exit 0 means the reader accepts
the file, not that the provider does.

The service does not renew a Codex login yet. When runs fail with `CliLoginRefusedError` in the
log, install a new sign-in. A copy can fail before its access token's measured 240 hours are up
(decision record 0001, `#### A1b-C0b`, unavailable facts), so no lifetime is promised. The
Claude token lasts a year and renewing it is the operator's.

**When it fails.** A missing or refused file stops the service at startup, naming
`cli.<provider>.token_file` and the kind of fault, never the content, or fails a later run as
`provider_error` before any child starts. An expired token also fails the run as
`provider_error`. A login the CLI refuses names `CliLoginRefusedError` in
the log line; for Codex that is a `turn.failed` whose text carries `401 Unauthorized`, the only
signal codex 0.163.0 gives, so a version bump re-checks it.

An app that still calls the core directly names the same file in the provider's
block of its `ProviderRuntimeConfig`; that interim path reads Codex and Grok
login files under weaker rules than the service's.
