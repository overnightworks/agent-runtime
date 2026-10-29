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
file, the Claude token file, the Codex login document and any API credential file are read and checked with it.

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

`[runs] max_files_bytes` (default 8,388,608) limits all files of one run together; a
value above 33,554,432, the wire's ceiling, stops startup with exit 2 naming
`runs.max_files_bytes`. Files over it end the run `failed: limit_exceeded`, and any other
refused entry of the run's output folder ends it `failed: provider_error`. `max_bytes`
counts a `succeeded` frame without its files. A run may end with files and empty words.

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

## CLI settings

A subscription CLI keeps its own settings. `cli_settings` holds them in that CLI's own shape,
and the service passes them through without a mapping; it governs CLI runs only, and the API
route ignores it. `cli_settings.openai` (Codex) and `cli_settings.anthropic` (Claude) are
accepted; any other key stops startup as an unknown field, and `openai` must be a table. The
Codex table takes the unquoted dotted keys of Codex's own `config.toml`; a quoted key such as
`"features.image_generation"` stays one literal key. Claude's table is described [below](#claude).

```toml
[defaults.cli_settings.openai]
web_search = "live"

[apps.notes.agents.illustrator.cli_settings.openai]
features.image_generation = true
```

A Codex picture reaches the run's files only when the agent's `cli_settings.openai` turns on both
`features.image_generation` and `features.code_mode_host`. `code_mode_host` lets the model run
code inside the run's sandbox; that exposure is accepted for the picture.

The levels apply in this order, each over the one before: the adapter's defaults, `[defaults]`,
`[apps.<app>]`, then `[apps.<app>.agents.<agent>]`. Tables merge per key, recursively; any other
value replaces, so a scalar at a higher level replaces a whole table below it and an array is
not appended to.

The service writes the result as the run's `config.toml` (mode 0600, in the run's own fresh home)
and no setting value appears in the program's arguments. Its Codex defaults are `sandbox_mode =
"read-only"`, `approval_policy = "never"`, `mcp_servers = {}`, `web_search = "disabled"` and
`features` with `shell_tool`, `unified_exec`, `browser_use`, `computer_use`, `multi_agent`,
`image_generation`, `plugins`, `hooks`, `code_mode_host`, `code_mode` and `code_mode_only` all
`false`; every other feature follows the pinned CLI. The configuration overrides each of them,
`sandbox_mode` included, and the service neither refuses nor warns about what it sets.

Codex also reads further layers, and the service owns three. The file above is the user layer.
A project's `.codex/config.toml` loads only when the effective `projects` table marks that
directory `trust_level = "trusted"`; the run's working directory is fresh and empty, so only the
operator's own `projects` setting can trust one, and a trusted layer ranks above the file. The
session flags the service keeps rank above both: the system text (`model_instructions_file`), the
model, `--ephemeral` and `--json`. The host's own Codex inputs (its system file, managed and
requirements files) are the host's; see [Codex's loader](https://github.com/openai/codex/tree/rust-v0.163.0/codex-rs/config/src/loader).

Approval, measured on `codex-cli 0.163.0`: a headless `exec` run sets `approval_policy = never`
itself, so `approval_policy` alone changes nothing in a run (its `turn_context` says `never`).
With `approvals_reviewer = "auto_review"` the configured `approval_policy` applies (measured:
`on-request` when the file sets it) and Codex's own reviewer decides each request. The service passes both keys through. A built-in tool the
configuration switches on runs inside the CLI without a per-call approval from the service.

Four top-level keys of `cli_settings.openai` stop startup at any level (exit 2, `--check` agrees),
because another setting already owns their fact: `model` and `model_provider` (set `model`),
`model_instructions_file` (the run request's system text) and `cli_auth_credentials_store`
(`[cli.openai] token_file`). A nested key such as `features.model` loads.

A typo is not caught at startup. The pinned CLI exits before its first line under
`--strict-config`, so every run of that agent ends `failed: provider_error`, and the log names
`CodexAnswerMissing`, not the key. `profile = "<name>"` ends the same way on this pin, while
`[profiles.<name>]` loads and is never selected.

A built-in tool's line yields no event of its own, so a long tool call counts against
`[runs] idle_timeout_s` (default 120 s) beside `max_duration_s`, and repeated progress lines of a
tool do not refresh it. Raise `idle_timeout_s` for long tools.

### Claude

`cli_settings.anthropic` has two fields, both optional at every level: `tools`, the built-in tool
names the agent may use, and `settings`, a table in Claude's own settings keys. Any other key stops
startup, and so does `tools` that is not a list of strings or `settings` that is not a table. The
levels merge in the order above, but a field a level does not set is absent from it and never
replaces a lower one; `tools = []` is set and switches a lower list off.

```toml
[apps.notes.agents.illustrator.cli_settings.anthropic]
tools = ["Bash", "WebSearch"]
settings.permissions.deny = ["Read(./secrets)"]
```

Each name reaches both `--tools` and `--allowedTools`, so a listed tool is available and runs
without a per-call approval; app tools keep both barriers. The service has no list of Claude's
tools or keys: an invented name loads, and the start check below ends the run when the CLI does not
announce it.

When tools are listed, Claude's sandbox is on by default: `sandbox.enabled = true`,
`sandbox.failIfUnavailable = true` and `sandbox.allowUnsandboxedCommands = false`. The last two
are what make it hold: without `failIfUnavailable` a sandbox that cannot start leaves a warning and
runs commands unsandboxed, and with `allowUnsandboxedCommands` at its default the model may leave
the sandbox for a command. The configuration overrides every key; `settings.sandbox.enabled =
false` switches it off, and the service neither refuses nor warns about that. The sandbox covers
`Bash` only. Claude's file and web tools are governed by permission rules, and a listed tool is
allowed; with a listed shell, file or code tool the exposure of the signing key, the logins and
a repeated token is accepted.

The sandbox needs bubblewrap and `socat` in the image and a container that lets it create its
namespaces. The service's reference image has neither, so a run of an agent with tools ends
`failed: provider_error` until they are installed or the operator switches the sandbox off; the log
does not say why, because the CLI's stderr is not read.

The start check accepts exactly the listed tools plus the relay's. A tool the CLI announces that
is neither ends the run, and so does a listed name it does not announce (an invented name, or one
the operator's own `permissions.deny` removes); the log names the missing tools from the
configured list, never from the CLI's text.

These stop startup at any level (exit 2, `--check` agrees), naming the key and its owner: a `tools`
entry starting `mcp__` (the app's tools come through the app's MCP origin), `settings.model` and
`settings.fallbackModel` (set `model`), `settings.agent` (the run request's system text and
`model`), `settings.apiKeyHelper` (`[cli.anthropic] token_file`), `settings.env` (the service
builds the run's environment) and `settings.permissions.allow` (set `tools`). A tool name that is
empty, holds a comma or whitespace or starts with `-` stops startup too, because the CLI splits
those options on commas and spaces. So does a settings value JSON cannot hold: a TOML date, time
or datetime, `inf`, `-inf` or `nan`.

Layers: with tools listed, the run sets `CLAUDE_CONFIG_DIR` to its own home, writes the sandbox
default as the `settings.json` there and passes `--setting-sources user`, so that file is Claude's
user layer; the operator's `settings` go to `cli-settings.json` (mode 0600) and `--settings`, the
layer above it, which wins per key. Project and local settings stay off, and without tools the
sources stay empty and no file but the operator's is written. The host's managed settings still
apply; Claude's [settings precedence](https://code.claude.com/docs/en/settings) orders them.
The run home is private to the run, so nothing of the host user's is read.

One invalid value in `cli-settings.json` (for example `sandbox.enabled = "yes"`) makes Claude drop
that whole file silently, and typos are not caught at startup. The sandbox default stays in force
in that case, because it lives in the other layer.

## The subscription login

The agent service holds the one login per provider for all apps. Only the
subscriber himself may use it, because the provider's terms allow no use on behalf
of others. The service keeps no user list; Keycloak decides, so the operator keeps
the subscription groups (`subscription_groups`) to the subscriber alone. Everyone
else reaches the API route only as a member of `api_groups`; a person in neither
group is refused `not_enabled` ([ARCHITECTURE.md](ARCHITECTURE.md)).
The file is named as `cli.<provider>.token_file`; the core library never creates, renews
or rewrites it, and the service's login command installs a new one. The service renews a
Codex login itself ("Renewal of a Codex login" below); a Claude token has no renewal and is the
operator's to keep fresh. Claude (`[cli.anthropic]`) and Codex
(`[cli.openai]`) have a table each, and every table needs its `token_file`; the service refuses a
table for another provider and a leftover `[profiles]` section.

```toml
[cli.anthropic]
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

**The OpenAI table.** `[cli.openai]` holds the Codex program and takes `binary`, `version`, `models` and `token_file`. The program must print exactly `codex-cli <version>` for
`--version`. The file is the Codex sign-in document: JSON with the sign-in's tokens, at a mode
that gives nothing to group or others, at most 16 KiB, a link followed and its target checked.
A document holding only an API key (`OPENAI_API_KEY`), a bare key line and a Claude setup
token are refused. The service hands the program only a copy without the renewal token. The
document comes from the pinned program's device-code sign-in made in a home of its own, never the
operator's terminal home, because the two would rotate each other's renewal token away; the
host's setup makes it.

**What a CLI run does with tools.** A Codex tool item in the stream does not fail the run; the
service drops it and no frame shows it. An item or event type the adapter does not know still ends
the run `failed: provider_error`. The service switches off by default the features its Codex adapter
names ("CLI settings"), and every other feature follows the pinned CLI. At every pin bump the
operator compares that list with `codex features list`, run with `HOME` and `CODEX_HOME` set to one
empty directory that holds the defaults file, so the comparison also shows the file is read. A name
that vanished or was renamed already stops runs (`--strict-config`); a new stable feature that is
on by default does not. A CLI output line over 4 MiB ends the run `failed: provider_error`, and a
tool's output travels in one line. Claude's `--max-turns` is `[runs] max_tool_calls` plus one and
counts every tool-using response, built-in tools included. Codex has no turn limit by name, so
only `[runs] max_duration_s` bounds it.

**Install or check it (both providers).** `agent-runtime-service-login --config PATH --provider PROVIDER` (`anthropic` or `openai`)
reads the new document on standard input, checks it with the service's reader for that provider and
swaps it in beside the old file; the next run reads it without a restart. It exits 0 when the
file is written, 1 when it is not written and the old file stays, 2 when it refused before
reading anything (the platform, the configuration, a provider without a table, or a
`.<file>.new` beside the login file, which may belong to a writer that still runs: stop the
service and any other login command, confirm that no process the service started still runs,
then remove it; one that appears while the command reads its input ends it with 1 and is left
alone), and 3 when it is not written and its own `.<file>.new`, which may hold the new
login, is not confirmed gone: it names the path. Exit 0 may say "written; durability not
confirmed": the new login is in place, and a crash before the directory is written could bring
back the old one, never a mix.
It needs what the service's own start reads except the logins, and write access to the login
file's directory, so run it as the service's own user: the new file is created 0600 and owned by
whoever runs the command. A failure before the swap leaves the old file untouched. `--check` runs the
service's reader on the file as it stands and reads no input; exit 0 means the reader accepts
the file, not that the provider does.

**Renewal of a Codex login.** The service renews the `[cli.openai]` login itself, from its own
lifespan: one round at start, then every 15 minutes, and one hour after a round in which an
attempt failed. An attempt checks the file and does nothing until the access token has less than
half its lifetime left, which is about every 120 hours. Then it runs one tool-free turn of the
pinned `codex` on a full copy in a private home under `[cli] working_root`, which costs one turn on
the subscription, and swaps the result in through the same writer as the login command, only over
the document it read. Runs keep their blanked copies and never renew.

- **Write access to the directory.** The service needs write access to the directory that holds
  `token_file`, not to the file alone: the swap renames a sibling `.<file>.new` over it, and a
  rename onto a single-file bind mount fails. With a read-only directory or a single-file bind,
  every attempt logs `sibling not created` at WARNING, once an hour, spends no turn, and nothing
  renews.
- **Stopping.** A stop waits for a renewal in flight: the 30 s turn deadline, 11.1 s settling and the
  read-back and write, about 45 s at most. Uvicorn first waits for open requests, with no timeout, so
  this bound comes after they have finished; give the supervisor's stop at least 60 s beyond that. A
  kill sooner, or a forced second SIGINT, can leave `.<file>.new` beside the login file.
- **What a log line asks of the operator.** Only WARNING and ERROR lines are printed, because the
  service configures no logging. `agent-runtime-service-login --check` shows today only whether the
  reader accepts the file, not whether a renewal is due or failed (the expiry is the proof).
  - `sibling not created`: the service cannot create the sibling, because the directory is
    read-only or the file is a single-file bind (a mount point). Bind the directory that holds the
    file, read-write.
  - `sibling held`: a login command or a renewal holds `.<file>.new`. If none runs, a stop or a
    crash left it: stop the service and any other login command, confirm that no process the
    service started still runs, then remove the file. If a renewal child had already rotated the
    tokens, its renewed login lived only in its home, which stays under `[cli] working_root` and
    still holds a secret; the stored login is dead and the operator signs in again, then removes
    that home.
  - `renewal interrupted with its end unconfirmed` (ERROR): the same recovery.
  - `refresh failed`, `written, not renewed` and the other failures: the next attempt is an hour
    later. If they repeat for days, sign in again.
  - Cost. A failure before the turn costs none (sibling held, sibling not created, pool full, a
    refused file), and a failed refresh costs none. A failure after a completed turn costs that
    turn; the hourly backoff caps it at 24 a day.
- **The login command meanwhile.** The service holds `.<file>.new` for milliseconds every 15
  minutes and up to about 45 s during a renewal. `agent-runtime-service-login` then exits 2
  with the held-sibling sentence; run it again after the renewal.

**When it fails.** A missing or refused file stops the service at startup, naming
`cli.<provider>.token_file` and the kind of fault, never the content, or fails a later run as
`provider_error` before any child starts. An expired token also fails the run as
`provider_error`. A login the CLI refuses names `CliLoginRefusedError` in
the log line; for Codex that is a `turn.failed` whose text carries `401 Unauthorized`, the only
signal codex 0.163.0 gives, so a version bump re-checks it.

An app that still calls the core directly names the same file in the provider's
block of its `ProviderRuntimeConfig`; that interim path reads Codex and Grok
login files under weaker rules than the service's.
