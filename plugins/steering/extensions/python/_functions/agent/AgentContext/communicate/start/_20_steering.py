from helpers.extension import Extension


class SteeringCommunicate(Extension):
    """A message that arrives during a run is kept for its next step. Any failure here leaves Agent Zero's own behaviour."""

    def execute(self, data=None, **kwargs):
        try:
            from usr.plugins.steering.helpers import steering
            steering.on_communicate(data or {})
        except Exception:
            import logging
            logging.getLogger("steering").exception("Steering left a message to Agent Zero")
