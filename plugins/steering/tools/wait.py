"""Agent Zero's wait, ending early when the user sends a message: waiting is not work, so nothing is lost by it."""
import asyncio
import time

from helpers.tool import Response
from tools import wait as core_wait

CHECK_SECONDS = 0.5


class WaitTool(core_wait.WaitTool):

    async def execute(self, **kwargs) -> Response:
        began = time.monotonic()
        waiting = asyncio.create_task(super().execute(**kwargs))
        try:
            while True:
                done, _ = await asyncio.wait({waiting}, timeout=CHECK_SECONDS)
                if done:
                    return waiting.result()
                if self._message_waits():
                    break
        finally:
            if not waiting.done():
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
        seconds = int(time.monotonic() - began)
        try:
            self.log.update(heading=self.get_heading("Ended early: the user sent a message", done=True))
        except Exception:
            pass
        return Response(message="The wait ended after %d seconds because the user sent a message. It follows; wait again afterwards if "
                                "that is still needed." % seconds, break_loop=False)

    def _message_waits(self) -> bool:
        try:
            from usr.plugins.steering.helpers import steering
            return steering.pending(self.agent)
        except Exception:
            return False
