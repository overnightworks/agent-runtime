# Hearing an agent

The rules by which an app lets an agent hear the people at a place, the same in every app. Read
it when you build or change an app's way to let an agent hear, or write a test for it: a test
cites a rule by its ID, such as `HEARING-4`. The records the rules ride on (the place window, the
`spoken` entry) are in [connector.md](connector.md#hearing-and-speaking).

A **place** is where an app seats an agent as a member; a **speaker** is a person whose sound the
app captures at a place; a **member** is an agent that may hear. The library carries text only:
the app turns speech into text, and a finished utterance reaches the library as a record.

## The mental model

`MemberHearing` (`agents.mcp_server`) is a table in memory: for each place, speaker and sense, the
members switched on and the moment each was switched on. The app's rule decides who may hear whom
and tells the table with `allow`. When a speaker's utterance is finished, the app asks `heard`,
which answers the members that hear it and the one line to append. A member hears an utterance
only if it was switched on no later than the utterance began; any end deletes the entries, so
nothing that finishes after it reaches anyone.

Each rule is marked **L** when this library's tests prove it, naming the test, and **A** when each
app proves it.

## Who may hear

**HEARING-1 (A).** Only the app's own rule decides who may hear whom, and the app records every
decision with `allow(place, speaker, member, sense, allowed)`. A person's speech is entered only
while that person allows it under the app's own rule, so the app records `allowed` true for a
member of a person speaker only while that person allows it, and false when the person withdraws
it.

**HEARING-2 (L).** The record is keyed by place, the app's opaque speaker key, member and `Sense`;
only `Sense.HEARING` is defined. The speaker key is the app's own, not a `Principal`, so a seat
can end while the account goes on; the same key at two places names two speakers. Proved by
`test_one_speaker_key_at_two_places_is_two_speakers` and
`test_a_switch_on_for_one_speaker_is_not_one_for_another_speaker`.

**HEARING-3 (L).** Nobody is heard before an `allow` is recorded. Proved by
`test_nobody_hears_before_an_allow_is_recorded`.

## What is heard

**HEARING-4 (L).** The rule of the moment: a member hears an utterance only if its entry exists
when `heard` runs and its switch-on time is no later than `Utterance.began_at`. `allow` with
`allowed` true while on keeps the earlier time; `allowed` false deletes the entry. A switch-on
in the middle of an utterance therefore loses that whole utterance, and so does a switch-off and
on during it: the cost of never delivering words said before the member was switched on. Proved by
`test_a_member_hears_an_utterance_only_if_the_switch_on_is_no_later_than_its_start`,
`test_a_switch_off_before_heard_runs_loses_the_utterance`,
`test_a_switch_off_and_on_again_hides_only_what_began_before_the_second_switch_on`,
`test_a_second_allow_while_on_keeps_the_earlier_switch_on_time` and
`test_each_member_hears_by_their_own_switch_on_time`.

**HEARING-5 (A).** `began_at` is a lower bound of the sound's start on the clock the record
reads: the default is the running loop's time, read when `allow` runs, or the callable passed to
`MemberHearing(now=...)`, and both the record and the app use that one clock. The app maps it
from the speech service's position of the utterance's start in the audio against the moment its
server opened the sound session (which is no later than the session's first frame), never from
the moment an event arrives. When it has no position, it uses the previous utterance's mapped
end, or that session start.

**HEARING-6 (L+A).** Only finished utterances are heard: one spoken line per utterance, under the
speaker's shown name and the app's `kind`, and never provisional text as an entry; provisional text
reaches a member only through HEARING-21. `heard` builds the line as
`SpokenLine` (`spoken` true, `own` false) for every hearing member; the app appends only
finished utterances. Proved by `test_heard_builds_one_spoken_line_of_the_utterance_for_the_hearing_members`
and `test_the_line_is_spoken_and_not_the_members_own_and_becomes_a_place_entry_once_seq_is_added`.

## Where hearing ends

**HEARING-7 (L+A).** A member's end (`member_ended`), a speaker's end (`speaker_ended`) and a
place's end (`place_ended`) each end hearing at once, for an utterance that finishes after it
included. After a rejoin, only an utterance that began after the new `allow` is heard. The app
reports every exit path: leaving, removal, an ended lease, the speaker's leaving and the place's
ending. Proved by `test_an_end_ends_hearing_at_once_for_an_utterance_that_finishes_after_it`,
`test_after_a_rejoin_only_an_utterance_that_began_after_the_new_allow_is_heard` and
`test_an_end_reaches_only_its_member_speaker_or_place`.

**HEARING-8 (A).** The record does not know who has joined. The app calls `allow` only for a
member joined at that moment and never after it has reported that member's, that speaker's or
that place's end; a default recorded at a join is not replayed after a leave.

**HEARING-9 (A).** Pausing an agent does not end its hearing; an app that wants it to records
that with `allow`. A speaker's parked seat is not an end: the app ends that speaker's sound
session and keeps the record, and reports `speaker_ended` when the speaker leaves.

**HEARING-10 (L).** `allows` and `sensing` answer from the same entries as `heard`, so the app's
gate and its ear mark agree with what is delivered: `sensing` names every member switched on, one
switched on in the middle of a running utterance included, who does not yet hear it. Proved by
`test_allows_and_sensing_answer_as_heard_does_for_an_utterance_after_every_switch_on`,
`test_allows_and_sensing_answer_nobody_after_an_end` and
`test_sensing_names_a_member_whose_switch_on_is_later_than_a_running_utterance`.

## Where the line goes

**HEARING-11 (A).** The library returns lines and owns no window. The app gives each line its
`seq`, appends it to the window of each member in `Heard.members`, and signals the change with
`ResourceChanges.changed` once the append is visible to a read.

## The record's life

**HEARING-12 (A).** The record lives in one process. The app calls it on the front's event loop
or through `anyio.from_thread.run_sync`, as it does `ResourceChanges`; a call from another thread
is not safe.

**HEARING-13 (L).** The record is in memory only: nothing is persisted, an utterance's text and
name pass through `heard` and are kept nowhere, and no call logs. Proved by
`test_no_call_writes_a_log_record` and
`test_the_record_keeps_neither_an_utterance_nor_its_line_once_the_caller_lets_go`.

## An agent's spoken sentences

**HEARING-14 (A).** An agent's sentence at a place starts when the duration of its previous
sentence at that place has run out. The first sentence starts at once.

**HEARING-15 (A).** The app's `speak` answers at the start of the sentence, not before.

**HEARING-16 (A).** At its start the sentence enters every window at the place as a spoken chat
line.

**HEARING-17 (A).** The app's server keeps the queue of an agent's sentences. The library orders no
sentences.

**HEARING-20 (A).** An agent names the language of its sentence; the app never guesses it from the
text. Without one the app speaks the sentence in its page's language. A language the app's voice
does not speak is refused at once with `language_unavailable`, as `too_long` is.

## Being talked over

**HEARING-21 (A).** When a person starts speaking at a place and `allows` admits the agent to hear
that person at that moment, and a sentence of the agent plays then, the app sets the window's
`talked_over` record: its `id`, `playing` true and a `PartialSpeech(name, text)` for that person,
whose `text` may still be empty. The `id` is the app's, never repeats within the window's
`generation` and stays fixed from the record's appearance to its removal; a second person who
starts speaking meanwhile adds a partial under the same `id`, and `speech` keeps the order in which
they started. A runner takes at most one decision per `id`. The app updates the partials as the
words come and `playing` as the agent's sentences end, and signals each update per HEARING-11. A
person's finished line replaces that person's partial in the same window change, so one read never
shows both. The record goes when the person's turn ends, also a turn that heard no words, at a
switch-off (and does not return for that utterance) and when the speaker, the member or the place
ends. A partial can therefore go with no line after it, and a runner treats a partial as
provisional. A person's turn that began before the agent's sentence starts creates no record and
no duck.

**HEARING-22 (A).** At the moment the record appears, every seat lowers the agent's player at
once. A sentence that starts while the record stands also plays lowered. The volume returns when
the record goes, at the end of the person's turn, also a turn that heard no words. A sentence that
ends by itself while lowered brings nothing back.

**HEARING-23 (A).** `stop_speaking(place)` cuts the agent's playing sentence in every seat and
drops its whole queue there, and answers `StoppedSpeaking(cut)` with the text of the sentence cut.
Each dropped queued `speak` answers `stopped`, so no call hangs. With nothing playing it answers
`nothing_playing` and cut and dropped nothing. A stop is not a mute: the agent speaks again at its
next `speak`. The cut sentence's line stays as it was appended.

**HEARING-24 (A).** `speak` may carry a `style`, free text that the app passes on unchanged to its
speech service. The app neither bounds nor checks it: the speech service owns its length bound and
its check. Every refusal of the style by the voice (a voice that takes no style, a style too long,
or one the voice does not support) reaches the agent as `style_unavailable`, with no second code.

## What an app supplies

**HEARING-18 (A).** The library owns no sound, no speech client, no token, no switch and no mark. An
app supplies:

- its rule for who may hear whom, and every `allow` and end that follows from it;
- the finished utterance with its `began_at` (HEARING-5);
- the sound, the speech client, and the verified identity of the speaker;
- the switch and the ear mark, from the shared design kit, with the mark showing `sensing`;
- the window, its `seq`, its append and the signal (HEARING-11);
- the queue of an agent's own spoken sentences (HEARING-14 to HEARING-17);
- the talked-over record with the partials, the duck, the stop and the style (HEARING-21 to
  HEARING-24); and
- its privacy text, which states what is heard and its legal basis.

## Owners

**HEARING-19 (A).** Every app the standard covers follows these rules, so that who may hear whom
works the same in each. This file owns the rules; the shared design kit holds the switch and the
mark. The standard covers every fleet app that lets an agent with an account of its own hear
people at a place; an app that does adopts it with the release of this library's part. Seeing is
not defined.
