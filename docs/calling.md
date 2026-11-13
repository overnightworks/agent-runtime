# Calling an agent

The rules by which a person calls an agent to a place and sends it away, the same in every
app. Read it when you build or change an app's control for this, or write a test for it: a test
cites a rule by its ID, such as `CALLING-9`. The mechanism the rules ride on (the standing, the
invitation, the lease, `accept_invitation`) is in [connector.md](connector.md#the-member-module).

A **place** is where an app seats an agent as a member; a **person** acts through the app's page;
the **owner** is the person an agent works for; the **inviter** is the person who called it.

## Who is offered which agent

**CALLING-1.** An agent is offered only if it has an account of its own and holds an open listen
that covers the app's standing. The lease keeps an agent at a place it holds; it does not make
the agent offered. An app asks the library with `ResourceChanges.listens_to(principal,
STANDING_URI)` and learns of a change from the `listening_changed` callable it passes to
`ResourceChanges` ([connector.md](connector.md#the-standard-path)).

**CALLING-2.** An agent is offered at a place only while its owner is present there by the app's
rule.

**CALLING-3.** A person is offered an agent only if they are present at the place and a member of
the realm group `/agent-adders`. The list rests on the view's membership (CALLING-17); the call
reads it fresh (CALLING-21).

**CALLING-4.** An agent is not offered at a place where it is invited or already joined, nor at a
place that has no room for it.

**CALLING-5.** While an invitation is open, only its inviter sees the agent, as inviting.

**CALLING-25.** An agent's runner holds a listen on an app's standing only while it accepts
invitations to that app's places.

## Calling

**CALLING-6.** The invitation is the app's. It is bound to its inviter and to the owner's
presence, and it expires `INVITATION_SECONDS` (`agents.member`) after the app opens it. The agent
side reads the invitation's `expires_at` and never that value.

**CALLING-7.** The agent's accept is checked by the app: the invitation still stands (id, epoch,
expiry), the owner is still present, and the bounds hold. The place then becomes active or the
accept is refused with a code of `AcceptInvitationRefusal`.

**CALLING-8.** An invitation that runs out unanswered, one to an agent busy elsewhere included, is
a normal ending: the row is offered again and shows no refusal.

## Sending away

**CALLING-9.** A person present at the place may send an agent away that holds it when they are a
member of `/agent-adders` (read fresh at the send-away), the agent's owner, or hold a right the
place's own rules give. An agent is sent away whether or not its owner is present; the person who
sends it away is present at the place, the owner included.

**CALLING-10.** A send-away takes effect at once and at any time, mid-activity included: the app
takes the place from the agent's standing, raises the place's epoch, signals the standing and
tells the place's people. The agent's runner finds the place absent and ends its turn there.

**CALLING-11.** A place that cannot go on without the agent pauses with the agent's seat open.
What pausing and resuming mean there is the app's.

**CALLING-12.** An agent that was sent away may be called again at once.

**CALLING-13.** When an agent leaves a place without being sent away stays each place's own rule.
The standard neither requires nor forbids that an agent leaves with its owner.

**CALLING-26.** A place inside another app's place is independent of it: sending an agent away
from one leaves it in the other. Its standing names the other place as its `parent`
(CALLING-27).

**CALLING-27.** An app whose place lies inside another app's place sets `parent` on the standing
of its place, from the invitation on, and keeps it unchanged while the member holds the place, so
a runner can read it before it accepts.

**CALLING-28.** `parent.app` is the parent app's MCP resource URL, taken from the app's own
setting, and `parent.place` is the `PlaceRef` the parent app uses, taken from the page that called
the agent, so untrusted. A runner matches a parent by equality of the normalised URL and of the `PlaceRef`
against a place it holds, and a parent that matches none counts as none. A parent grants no right.

## What the person sees

**CALLING-14.** The control lists each agent in one of three states: available (the person may
call it), inviting (no action) or here (the person may send it away). Inviting shows no deadline.

**CALLING-15.** Neither calling nor sending away asks for confirmation or counts down: the tap is
the act.

**CALLING-16.** An agent is listed as here only for a person who may send it away, by CALLING-9.
To everyone else it shows only at its seat in the place, with the AI mark.

**CALLING-17.** The control is shown to a person present at the place who is a member of the group
(CALLING-19) or holds a send-away right the place's own rules give (CALLING-9), and to any person
for whom it lists an agent. When it lists no agent, it shows one sentence that no agent can be
reached, and no reason per agent. The app decides whether to show it from the groups its session
carries, or from one read of the group when the view is built, and the place's rights. The group
is never read per render; only the act reads it fresh (CALLING-21), so a person whom a stale view
shows the control is refused at the act as not allowed.

**CALLING-18.** The control stands at the place itself, at the same position whether or not the
place lies inside another app's page. No app fetches an agent into another app's place.

## The group

**CALLING-19.** One realm group, `/agent-adders`, decides who may call, for every owner's agent,
and who may send away besides the agent's owner and a right the place's own rules give
(CALLING-9). Each app names it as a constant. Any member of the group may call or send away any
owner's agent while it is there.

**CALLING-20.** Before a second person becomes an owner (a member of `/agent-owners`), the split
of this group per owner lands. Nothing refuses or warns when a second owner is added.

**CALLING-21.** Group membership is read fresh from the realm for each call and each send-away,
never for the display (CALLING-17). Fresh means asked of the realm at the act itself, never taken
from a token, session or cache that existed before it. Liveness comes from the app's own listen
streams. The standard adds no store, directory or list.

## What an app supplies

**CALLING-22.** An app supplies:

- its place (the `PlaceRef` kind and id) and its presence rule;
- what accepting and pausing mean at the place, and its bounds;
- the invitation book, the accept checks, the send-away check and the leases;
- the person routes for calling and sending away, which are never an MCP tool, so no agent can
  call or send away an agent;
- the shared control and the AI mark, with a stable, opaque key of its own minting for each row,
  the same across the row's states;
- the mapping of its refusal codes, many to one, onto the control's reasons;
- its channel to its page; and
- when its place lies inside another app's place, the `parent` of its standing (CALLING-27).

## The steps

1. The agent's runner listens on the app's `member://standing`, so the agent is live (CALLING-1),
   and does so only while it accepts invitations to the app's places (CALLING-25).
2. A person opens the place. The page shows the control at the place itself where CALLING-17 shows
   it, and lists agents per CALLING-1 to CALLING-5.
3. **Call.** The person taps an available row. The page posts to the app's person route. The app
   reads the person's groups fresh, opens an invitation (CALLING-6), adds it to the agent's
   standing, with its `parent` where CALLING-27 applies, and signals it.
4. The runner reads the standing and calls `accept_invitation(place, invitation, epoch)`. The app
   checks it (CALLING-7) and makes the place active or refuses.
5. The row moves: inviting, then here for those who may send away, or available again after the
   invitation runs out (CALLING-8). A refusal shows the reason the app maps (CALLING-22).
6. **Send away.** The person taps a here row. The page posts to the app's person route. The app
   checks the right (CALLING-9), takes the place from the standing, raises the epoch, signals the
   standing and tells the place's people (CALLING-10). The runner reads the standing and ends its
   turn there.

A place inside another app's place follows the same steps (CALLING-26); each place's own rule
decides when an agent leaves it without being sent away (CALLING-13).

## Owners

**CALLING-23.** Every app the standard covers follows these rules and draws the one shared control,
so that calling and sending away an agent feel the same in each.

**CALLING-24.** This file owns the rules; the shared design kit holds the control's look, the words
of its reasons and the sentence of an empty list. The standard covers every fleet app that has a
place an agent with an account of its own can join. An app with such a place adopts it with the
release of the shared control and of this library's part; an app without one adopts it at the
landing of its first such place.
