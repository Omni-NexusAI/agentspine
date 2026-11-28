"""Registers the decision backend with Convo when an agent starts (the registry lives in the host process and starts empty)."""
from helpers.extension import Extension


class ConvoD1RegisterAtAgentInit(Extension):
    def execute(self, **kwargs):
        try:
            from usr.plugins.convo_d1_decider.helpers.provisioner import ensure_registered
            ensure_registered(only_with_convo=True)
        except Exception:
            # Convo reports "not registered" in its own readiness; an agent must never fail to start because of this plugin.
            pass
