from helpers.extension import Extension


class SteeringRunOver(Extension):
    """An agent loop ended."""

    async def execute(self, data=None, **kwargs):
        try:
            from usr.plugins.steering.helpers import steering
            agent = self.agent or ((data or {}).get("args") or (None,))[0]
            if agent is not None:
                steering.run_over(agent, failed=isinstance((data or {}).get("exception"), BaseException))
        except Exception:
            import logging
            logging.getLogger("steering").exception("Steering could not note the end of a run")
