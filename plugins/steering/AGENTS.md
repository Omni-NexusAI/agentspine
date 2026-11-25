# steering

A message sent to a working agent joins the same run at its next step. A standalone Agent Zero plugin; Convo uses it when it is on.

## Contract

- Agent Zero's code is not edited and nothing of it is replaced. Everything goes through hooks (`extensions/python`), one tool override
  (`tools/wait.py`, a subclass) and the page's extension points. One instance attribute, `history.new_topic`, is shadowed for the length of
  one synchronous call (`_put`): a topic of its own would move the run's working history to the older topics, which are compressed first.
- **A failure never costs a message.** Every hook imports the rules inside a try and leaves Agent Zero's own behaviour when anything
  fails. A message that cannot be put into the run becomes `held`. A message is never in two places: every change of status is made
  under the chat's lock before Agent Zero is called.
- **Nothing waits.** `communicate` runs under other plugins' locks (Convo's dispatch lock) and from any thread: no network, no model call,
  no sleep in it. All chats share one agent event loop: hooks inside the run do no slow work.
- The lock is never held across a call into Agent Zero that may call back (communicate, history, log).
- Decided by meaning, never by words: the working agent reads the note beside the message (`AUTOMATIC_NOTE`, `STEER_NOTE`,
  `COPY_NOTE`). There is no phrase list. A judge by another model is only added if measurement shows the working agent deciding
  wrongly, and then beside the message, never before it.

## How it works (`helpers/steering.py`)

- `AgentContext.communicate` start hook -> `on_communicate`: with an agent at work in the chat the message is stored and
  `data['result']` set, so `agent.intervention` is never set. What comes back is the chat's own task, or for a chat whose agent works inside
  another run (a sub-agent, a scheduled task) a task that is already finished: never the superior's, since a caller that stops what it got
  would stop the superior's run. Left to Agent Zero: no run; a message marked `steering_mode = "interrupt"`; a paused agent with no tool
  running (the user stopped it to say this); more than `LIMIT` waiting.
- `live(context)`: the chat's own task, or an agent loop noted by the `monologue` hooks whose asyncio task is not done and which was not
  started by the chat's own task. `context.task.is_alive()` alone misses sub-agents and scheduled runs (a message there would start a
  second run); the noted loops alone would call a run alive that Stop has just cancelled.
- `message_loop_prompts_before/_95_steering.py` -> `deliver`: after `_90_organize_history_wait` (history compression is settled), before the
  history is read, inside the loop's own error handling. It goes through `hist_add_user_message(intervention=True)`, so other plugins'
  hooks see a native intervention. `extras_persistent['steering']` shows the messages with every request of the run: compression keeps only
  the first message of a topic.
- Final answer: no second loop inside the run (the messengers send only the last answer, `monologue_end` work would run twice, an error would
  discard a finished answer). What the run did not take up starts the next run.
- The end of a run: `_watch` adds a callback to the run's future after the message is stored (a run already over calls it at once).
  Stop and errors hold the messages immediately, inside the callback; `nudge` hooks set `keep`, so its new run takes them. The rest is
  settled `SETTLE_SECONDS` later (`_settle`), when the queue or another message may have started a new run whose first step takes them.
  Loops inside another run are settled by `run_over`: a sub-agent's leftovers go to its superior.
- Sub-agents: each one at work is told once about messages its superiors got after it began (`_copies`), for information only.
- State is a plain dict under `context.data['_steering']` (a lock and the runs, not saved with the chat); what must survive a restart is
  written to `data/<chat id>.json` at once (`_save`), because a message waits while a step is in progress. It comes back as held.
- A line in the chat: a sender that already logged one is recognised by id, else by the newest line's words within 15 s (`_bubble`); its id is
  used for the history, since Agent Zero's branching cuts by it. Otherwise the line is written when the message goes in.
- The page (`webui/steering-store.js`): `send_message_before` sends to `POST /plugins/steering/steering` instead of the queue, unless another
  hook has cancelled the send, the chat is idle or the chip says queue. The composer is emptied only when it holds that message, and gets it
  back when sending fails. Snapshots carry `_steering` (`output_data`, not saved).

## Verification

- `python -m unittest discover -s plugins/steering/tests -p "test_steering.py"` (stand-ins, no Agent Zero)
- `node --experimental-vm-modules --test plugins/steering/tests/page.test.mjs`
- In an isolated container with a stand-in model preset: `tests/in_container.py` (its docstring says how). It runs the real Agent Zero and
  compares with the plugin off. Never in a container with chats that matter: it creates and removes chats and toggles the plugin.
- Not proved by these: how well a given main model decides Automatic, native function calling through the Responses API, real messengers.
