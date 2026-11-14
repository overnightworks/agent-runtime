# Agent kinds

Which kind of agent an app builds for an agent surface, and who owns each part of it. Read it when
an app adds an agent surface and you must decide between a helper and a persona, or must know
whether a part already has an owner before you build it.

## Who it covers

Every fleet app with an agent surface, an AI chat or any other place where an agent acts, follows
this document. An app adopts it with the item that adds or re-cuts an agent surface; a surface no
such item has touched stays as it is until then. Only the operator rules a deviation, and it is
recorded here.

## Helper or persona

Who acts, on whose budget, and whether the app must work without the runner decide the kind.

A **helper** is the app's own agent file, run in the app's process through this library, as the
signed-in person: their principal, their rights, their budget. An app ships a helper for every
agent surface it must offer while the runner is away.

A **persona** acts as itself: its own account and budget, a member of a place by the
[standard path](connector.md#the-standard-path) and the [calling control](calling.md). It never
acts in a person's name, and an app builds nothing for one persona. It may take over a role a
helper fills; the app keeps the helper for when the runner is away. No persona fills a role today.

## Who owns which part

Each part has one owner, as the code policy's rule "one owner, no parallel copies" asks.

- This library: [the turn](connector.md#the-turn), [the tool contract](connector.md#the-tool-contract),
  [the member place](connector.md#the-member-module) and
  [`StopOutcome`](connector.md#the-turn).
- design: the page parts, `AgentControl` (the calling control) and `HelperChat` (the helper chat).
- speech: sound.
- Each app: its agent file, tools, words (stop wording included), store and page layout.

A landing that gives a part an owner adds its line to this map.
