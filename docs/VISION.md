# Vision

## What it is

The one interface through which every app runs its agents. An app describes its agents and its
tools once, in the same way as every other app. It runs their turns, for text, for pictures and
for a conversation by voice, against its own model gateway, and it can hand the same tools to a
signed-in person's own agent. An app that hears and speaks offers both to an agent the same way:
heard text with its speaker in the place's window, spoken text through one standard tool. The
sound stays with the app and the speech service; this library carries text, and the one still
picture an app's tool hands an agent to see.

## What it does

It gives every app the same answers to the hard questions of running an agent:

- when a turn may be retried, and why one that cannot finish stopped;
- what a tool may change, and which tools only read;
- how one person keeps one budget across text and pictures.

## Why

Without one interface each app writes its own agent loop and its own tool contract, and each
gets the hard parts wrong in its own way. With it, a fix lands once for every app, and someone
who knows one app's agents can read every other's. The interface follows open standards where
they exist and adds only what they leave open, so it stays small.

## Who uses it today

Apps that run their agents in their own process. Each one owns its deployment facts: where its
gateway is, which key it uses, which tools it offers and who the signed-in person is.

## What it deliberately is not

- Not a service. Nothing runs between an app and its gateway, and no run outlives the app's turn.
- Not a provider layer. It talks to the app's gateway and knows no model provider behind it.
- Not a framework. An app keeps its own web stack and hands in what the interface needs.
- Not an authority on rights. The app decides who may run what; the gateway holds the budget.
- Not a place for one app's words. What belongs to one app stays in that app.
