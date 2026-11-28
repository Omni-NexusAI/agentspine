from helpers.extension import Extension


class SteeringDeliver(Extension):
    """The waiting messages join the run: after history compression was waited for (_90), before the history is read."""

    async def execute(self, loop_data=None, **kwargs):
        if not self.agent:
            return
        try:
            from usr.plugins.steering.helpers import steering
            steering.deliver(self.agent, loop_data)
        except Exception:
            import logging
            logging.getLogger("steering").exception("Steering could not look for waiting messages")
