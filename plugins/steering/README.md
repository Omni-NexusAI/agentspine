# Steering

A message you send while Agent Zero works goes into the same run at its next step, instead of stopping what it is doing.

Without this plugin Agent Zero either queues the message until the run is done, or interrupts: the reply being written is thrown away
and a tool that has just finished loses its result.

## What happens to a message

| When it arrives | What happens |
|---|---|
| The agent is writing, or a tool is running | That step finishes and keeps its results. The message then goes into the run, before the next model call. |
| The agent is giving its final answer | The answer is delivered whole; the message starts the next run at once. |
| The agent is only waiting (its wait tool) | The wait ends early and the message goes in. |
| You paused the agent | The message goes in where it stopped. A finished tool's result is kept; a half-written reply is cut. |
| You press Stop, or the run fails | The message stays above the composer: send it, put it back into the composer, or remove it. |
| Agent Zero restarts | The same. |

Sub-agents hear it at their own next step, as information for their part; new work stays with the main agent. A message typed into a
sub-agent's own chat steers that sub-agent; one it could not take up any more goes to the agent that waits for it (or stays above the
composer when that one has ended or was stopped, too). The message stays in view for the rest of the run.

A message that could not be sent at all (the page lost its connection) goes back into the composer, or stays above the composer of its chat.

A tool that takes long is not cut, up to its own limit. "Interrupt now" beside the waiting message does that on purpose.

## Choosing

- The chip beside Pause and Nudge, while the agent works, for this chat; the default is in the plugin's settings.
  - **Automatic**: the working agent decides by what you say: take it in now, finish first and then do it, or stop.
  - **Steer**: take it in now.
  - **Queue**: Agent Zero's own queue, after the run.
- `/steer <message>`, `/later <message>` (queue) and `/interrupt <message>` (Agent Zero's own interruption). Commands carry no attachments.
- Ctrl+Enter steers and Alt+Enter queues, whatever the chip says.
- "Send now" in Agent Zero's queue and `/steer` in the messengers steer, too.

## Limits

- Running code may take up to 4 minutes, waiting for more output of a running command 10, parallel sub-agents 5: a message waits that long
  for a step to end. These are Agent Zero's own limits.
- Automatic is decided by your main model. A model that follows instructions poorly may drop a separate request; `/later` queues it for sure.
- Attachments of a held message must be added again when it goes back to the composer.

## For other plugins

```python
from usr.plugins.steering.helpers import steering
item = steering.steer(context, text, source="my_plugin", caller=True)   # '' when no agent is at work
steering.status(context, item)     # waiting, held, delivered, sent, undelivered or ''
steering.busy(context)             # also true for sub-agents and scheduled tasks
```

With `caller=True` a message the run could not take up any more is left to you (`undelivered`): deal with it and call `forget`.
A `UserMessage` with the attribute `steering_mode = "interrupt"` is Agent Zero's own interruption.

See `AGENTS.md` for how it works inside.
