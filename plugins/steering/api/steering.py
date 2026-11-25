from __future__ import annotations

from helpers import extension
from helpers.api import ApiHandler, Request, Response


class Steering(ApiHandler):
    """The composer, the strip and the mode chip. Every action names its chat (`context`)."""

    async def process(self, input: dict, request: Request) -> dict | Response:
        from agent import AgentContext
        from usr.plugins.steering.helpers import steering

        action = str(input.get("action") or "").strip()
        context = AgentContext.use(str(input.get("context") or ""))
        if context is None:
            return Response(status=404, response="Chat not found")
        item_id = str(input.get("id") or "")
        try:
            if action == "view":
                return {"ok": True, "steering": steering.view(context), "running": steering.busy(context)}
            if action == "mode":
                return {"ok": True, "steering": steering.set_mode(context, str(input.get("mode") or ""))}
            if action == "known":                        # did a message with this id get here (its answer was lost)?
                return {"ok": True, "known": bool(item_id) and steering.known(context, item_id), "steering": steering.view(context)}
            if action == "send":
                data = {"message": str(input.get("text") or ""),
                        "attachment_paths": [steering.full_path(a) for a in input.get("attachments") or []]}
                # As Agent Zero's own message endpoint: other plugins may complete what the user sent.
                await extension.call_extensions_async("user_message_ui", agent=context.get_agent(), data=data)
                result = steering.send(context, data.get("message", ""), attachments=data.get("attachment_paths", []),
                                       how=str(input.get("how") or ""), item_id=item_id)
                return {"ok": True, **result, "steering": steering.view(context)}
            if action == "take":
                taken = steering.take_item(context, item_id)
                return {"ok": taken is not None, "item": taken, "steering": steering.view(context)}
            actions = {"interrupt": steering.interrupt_item, "queue": steering.queue_item, "release": steering.send_item,
                       "remove": lambda chat, item: steering.forget(chat, item) is not None}
            if action in actions:
                return {"ok": actions[action](context, item_id), "steering": steering.view(context)}
        except ValueError as error:
            return Response(status=400, response=str(error))
        return Response(status=400, response="Unknown action")
