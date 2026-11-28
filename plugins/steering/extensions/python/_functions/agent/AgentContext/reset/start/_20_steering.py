from helpers.extension import Extension


class SteeringReset(Extension):
    """The chat is reset: nothing of it waits any more."""

    def execute(self, data=None, **kwargs):
        try:
            from usr.plugins.steering.helpers import steering
            context = ((data or {}).get("args") or (None,))[0]
            if context is not None:
                steering.clear(context)
        except Exception:
            import logging
            logging.getLogger("steering").exception("Steering could not clear a reset chat")
