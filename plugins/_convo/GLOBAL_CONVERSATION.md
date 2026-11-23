# Global conversations: design, not implemented (2026-11-09)

The user asked for one Convo conversation that can span chats: across the whole Agent Zero chat log, or across one project, while
per-chat conversations stay possible. Today every voice session belongs to exactly one chat (`target`, an Agent Zero context id).
This note is the plan for the next batch; nothing here is built.

## What a scope is
- `conversation_scope` (setting): `chat` (today, the default), `project` (every chat of the selected chat's project) or `global`.
- A scope key replaces the chat id as the conversation's identity: `chat:<context id>`, `project:<project name>`, `global`.
- Delegated work always goes to one chat: the selected chat (`target`), whatever the scope. The scope decides what Convo remembers
  and shows; the target decides where work runs and whose agent profile does it.

## Every place that assumes one chat today (and what changes)
| Place | Today | With a scope |
|---|---|---|
| Journal (`convo_store`: sessions, events, turns, memories, jobs) | rows carry `target`; history, context and compaction read by `target` | rows gain `scope`; history/context/compaction read by scope; `target` stays on jobs and on each turn (where it was said) |
| Model history (`Realtime._history`, `convo_history`) | the chat's last 30 turns | the scope's last 30 turns; a turn said in another chat is labelled with that chat's name when it is not the current target |
| Memory summary (`Compactor`, `memories` table) | one per chat | one per scope; a chat-scoped summary is not reused by a project scope |
| Persona (`Host.persona`, `voice_profile`) | the chat's agent prompt, or the pinned profile | unchanged: pinned profile, else the target chat's profile |
| Relevant context (`convo_context`, `Host.recent_chat`) | the target chat's recent messages | the target chat's recent messages, plus the scope's own journal |
| Delegation (`delegate_task`, `Store.submit`, `Dispatcher`) | the session's chat | the selected target chat; the job records both scope and target |
| Direct tools (`Host.direct`) | the target chat's agent (memory scope) | unchanged (memory belongs to the agent profile, not to Convo's scope) |
| Job notices (`_notice`) | spoken in the session that queued the job | spoken in any live session whose scope contains the job's target |
| Panel history (`history` action, `convo-store.js`) | one chat's events | the scope's events, each with its chat's name when different from the selected chat |
| Retarget (`convo.retarget`) | closes one chat's segment, opens another's | inside one scope: only the delegation target changes, history continues; across scopes: as today |
| Cancel/steer (`cancel_job`, `steer_job`) | checks the job's chat equals the request's | checks the job's target belongs to the session's scope |

## Open questions for the user
1. A global conversation that hears "delete that file": which chat runs it? Proposal: the selected chat, said aloud ("in Chat X").
2. Should switching chats in the Agent Zero sidebar retarget the global conversation automatically? Proposal: yes for the target,
   never for the scope.
3. Compaction across many chats: one summary per scope, refreshed like today's (`compaction.trigger`).

## Migration
Additive: new `scope` columns default to `chat:<target>`, so every existing conversation keeps reading exactly what it reads today;
rolling back ignores the column.
