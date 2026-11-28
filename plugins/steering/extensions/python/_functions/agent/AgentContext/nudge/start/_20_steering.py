from helpers.extension import Extension


class SteeringNudgeStart(Extension):
    """A nudge stops the run and starts it again: its waiting messages belong to the new one."""

    def execute(self, data=None, **kwargs):
        try:
            from usr.plugins.steering.helpers import steering
            context = ((data or {}).get("args") or (None,))[0]
            st = steering.state(context, create=False) if context is not None else None
            if st is not None:
                with st["lock"]:
                    st["keep"] = True
        except Exception:
            import logging
            logging.getLogger("steering").exception("Steering could not follow a nudge")
