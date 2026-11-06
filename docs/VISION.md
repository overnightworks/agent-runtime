# Vision

## Why it exists

An app that wants an agent should not have to run an agent platform. Every app reaches its
models directly through the LiteLLM gateway with its own key, budget and limit; the model
providers stay behind that gateway. What each app still needs, and should not write twice,
is small: a file format for its agents, a contract for its tools and their effects, a loop
that runs a turn against the gateway, and an MCP endpoint for its tools. This library is that
and nothing more.

## For whom

The apps that run agent turns in their own process, and the operator who ships them.
A host pins a release and owns everything deployment-specific: the gateway origin,
the key file, the tools and the person's identity.

## What it deliberately is not

- Not a service. No process sits between an app and its gateway, and no run outlives the
  app's own turn.
- Not a provider layer. It speaks one chat-completions dialect to one gateway and knows no
  provider, CLI or subscription login.
- Not a framework. It imports no web framework and no host; an app hands it ports and
  configuration.
- Not an authority on rights. The app decides who may run what, and the gateway enforces the
  budget.
- Not a place for host words. What belongs to one app stays in that app.
