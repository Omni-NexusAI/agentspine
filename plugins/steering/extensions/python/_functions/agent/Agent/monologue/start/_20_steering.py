import asyncio

from helpers.extension import Extension


class SteeringRunBegan(Extension):
    """An agent loop starts: steering knows which agents are at work, sub-agents and scheduled tasks included."""

    async def execute(self, data=None, **kwargs):
        try:
            from usr.plugins.steering.helpers import steering
            agent = self.agent or ((data or {}).get("args") or (None,))[0]
            if agent is not None:
                steering.run_began(agent, asyncio.current_task())
        except Exception:
            import logging
            logging.getLogger("steering").exception("Steering could not note the start of a run")
