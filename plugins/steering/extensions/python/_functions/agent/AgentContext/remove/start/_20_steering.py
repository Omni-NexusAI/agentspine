from helpers.extension import Extension


class SteeringRemove(Extension):
    """The chat is removed: nothing of it waits any more."""

    def execute(self, data=None, **kwargs):
        try:
            from usr.plugins.steering.helpers import steering
            from agent import AgentContext
            args = (data or {}).get("args") or ()
            context = AgentContext.get(args[0] if args else ((data or {}).get("kwargs") or {}).get("id"))
            if context is not None:
                steering.clear(context, forget_mode=True)
        except Exception:
            import logging
            logging.getLogger("steering").exception("Steering could not clear a removed chat")
