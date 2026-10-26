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

The configuration is read once; a restart applies a change. The signing key
file, the Claude token file and any API credential file are read and checked with it.

## Who may run what

A caller presents a workload token bound to one registered app and either a user token
bound to one principal or, for a background job, a job ticket the app got earlier for that
principal (`agent_runtime_service.identity`). The checks run in this order:

1. The user must be a member of the app: one of their groups is `/apps/<app>` or
   a path below it (`/apps/notes-other` is not a member of `notes`). This holds
   for `POST /runs`, `GET /runs/{id}` and `GET /offer`; anyone else gets 403 `forbidden`,
   before the body is read. For a ticket the groups are the ones bound at mint, not the
   person's live groups.
2. Their role is `admin` in `/apps/<app>/admin` or below, else `user`; a
   request whose `roles` lacks it gets 403 `forbidden`.
3. An app without an MCP origin whose request names a tool gets 403
   `forbidden`.
4. `agent_runtime_service.routing.route` decides the route; the second picture in
   [ARCHITECTURE.md](ARCHITECTURE.md) owns the order. The keys that feed it: the
   subscription groups (`[identity]
   subscription_groups`), the profiles (`[profiles.<name>]`), the names the
   subscription lists (`[cli.<provider>] models`), and the API groups (`api_groups`,
   set together with `[route] api_alias`). A model name the subscription does not
   list sends that agent to the paid API for every caller with API rights and
   gives subscription-only callers `not_enabled`. The app chooses the model per
   agent and sends it on each run; the service stores nothing of the choice. A run that
   names no model goes to the profile marked `default = true`; with one profile that is
   the profile, marked or not, and with two exactly one must be marked. A run that names
   tools and whose profile's provider has no tool relay yet (Codex) gets 403 `not_enabled`
   before any place, capability or program, never a silent drop and never another route.
   A ticket run reads only the profiles with `background = true`; without one that serves
   it (a model-less run needs the default profile among them), it goes on over the API when
   the ticket's groups carry API rights, else `not_enabled`.

At most `[runs] max_parallel_cli_runs` (default 5) CLI runs are admitted; one
beyond gets 429 `busy` with `Retry-After` and never falls back to the API
route. The CLI runner serves Claude and Codex and checks each pinned version at startup.
The optional API runner serves buffered text and granted app tools through the
configured gateway; it takes no subscription place. Both routes use the app's
MCP origin and trust configuration. The CLI and API runners are installed independently.

## Configure the API text route

Set `[identity] api_groups` and `[route] api_alias` together, then add `[api]`
with `completion_url` and an absolute `credential_file`. The URL must be an ASCII
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

Removing `[api]` leaves group/alias routing enabled but makes admitted API runs
fail `provider_unavailable`. To disable paid entitlement entirely, remove
`api_groups`, `[route]` and `[api]` together; API-only callers then get
`not_enabled`. A contradictory partial configuration is a startup error.
Without CLI model configuration, `/offer` lists no models even with API installed;
apps may still submit explicit models. Gateway activation and paid testing require
the operator's separate approval; this route's repository proof uses fakes.

## App tools

An app that offers tools has `[apps.<app>]` with `workload_client` and
`mcp_origin`, one canonical https origin that no other app shares. The service
mints each admitted run a capability ([wire.md](wire.md)), signed with the
service's one RSA-3072 key, `[signing] key_file`. The service trusts the CA
bundle `[mcp] ca_file` names for these origins alone. The service never makes
its key; generation and rotation are the A1a4 ruling in the decision record.

## The subscription login

The agent service holds the one login per provider for all apps. Only the
subscriber himself may use it, because the provider's terms allow no use on behalf
of others. The service keeps no user list; Keycloak decides, so the operator keeps
the subscription groups (`subscription_groups`) to the subscriber alone. Everyone
else reaches the API route only as a member of `api_groups`; a person in neither
group is refused `not_enabled` ([ARCHITECTURE.md](ARCHITECTURE.md)).
The file is the operator's to make and to keep
fresh, named as `profiles.<name>.token_file`; the library never creates, renews
or rewrites it. Claude and Codex have a profile, one each; the service refuses a
profile for another provider (Grok) and a second one for the same provider.

```toml
[profiles.subscription]
provider = "claude"
token_file = "/run/secrets/claude-token"
background = false
default = true
```

`default` serves a run that names no model. A lone profile is the default, marked or not;
with two profiles exactly one is marked `default = true`, else the service stops at startup
(exit 2, naming `profiles.<name>.default`).

`background` is required, `true` or `false`; a profile without it, or with another
value, stops the service at startup (exit 2, naming `profiles.<name>.background`). `true`
lets the profile serve runs started by a job ticket; `false` keeps it to live runs. `true`
also needs the `[signing]` section, because a ticket is signed with its key.

**Provide it.** The file holds one line, the token `claude setup-token` prints,
at a mode that gives nothing to group or others (0600 or 0400), at most 16 KiB.
JSON (a credentials file) and API keys (`sk-ant-api...`) are refused; a link is
followed and its target checked. Run `claude setup-token` once, sign in with the
subscription, and write the printed token into the file.

**A Codex profile.** `provider = "codex"` needs `token_file` and a `[cli.codex]` table
(`binary`, `version`, `models`). The program must print exactly `codex-cli <version>` for
`--version`. The file is the Codex sign-in document: JSON with the sign-in's tokens, at a mode
that gives nothing to group or others, at most 16 KiB, a link followed and its target checked.
An API key (`OPENAI_API_KEY`), a bare key line and a Claude setup token are refused. The
service hands the program only a copy without the renewal token. Only the file's content is
named here; how the dedicated login is made is not.

**Renew it.** The token lasts a year and renewing it is the operator's. Run
`uv run python scripts/replace_token_file.py PATH` from a checkout: it reads
the new token on standard input, checks it by the service's rules and swaps it
in atomically; a refused token leaves the old file untouched. The next run
reads the file again, so no restart is needed.

**When it fails.** A missing or refused file stops the service at startup, naming
`profiles.<name>.token_file` and the kind of fault, never the content, or fails a later run as
`provider_error` before any child starts. An expired token also fails the run as
`provider_error`.

An app that still calls the core directly names the same file in the provider's
block of its `ProviderRuntimeConfig`; that interim path reads Codex and Grok
login files under weaker rules than the service's.
