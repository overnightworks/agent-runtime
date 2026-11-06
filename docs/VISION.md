# Vision

## What it is

The one interface through which every app runs its agents. Every app writes its agents as
files in the subagent format of Claude Code, declares its tools in code with an effect class,
read-only or side-effecting, and runs a turn that comes back as the same typed events and ends
short of its answer with the same stop codes. The turn reaches the app's LiteLLM gateway with
the app's own key, in the gateway's OpenAI-compatible dialect: chat completions for text,
image generations for pictures. The same interface serves the app's tools to a signed-in
person's agent over MCP.

## Why

Without it each app writes its own agent loop, its own tool contract and its own MCP endpoint,
and each gets the hard parts wrong in its own way: when a turn may be retried, what a tool may
change, how one person keeps one budget. One interface gives every app the same answers, so a
fix lands once and a developer who knows one app's agents can read every other's. Following
the standards the gateway and MCP already define keeps the interface small: it adds only what
they leave open.

## Who uses it today

Apps that run agent turns in their own Python process. Each pins a release and owns everything
deployment-specific: the gateway origin, the key file, the tools and the person's identity.

## What it deliberately is not

- Not a service. No process sits between an app and its gateway, and no run outlives the
  app's own turn.
- Not a provider layer. It speaks one dialect to one gateway and knows no provider, CLI or
  subscription login.
- Not a framework. It imports no web framework and no host; an app hands it ports and
  configuration.
- Not an authority on rights. The app decides who may run what, and the gateway enforces the
  budget.
- Not a place for host words. What belongs to one app stays in that app.
