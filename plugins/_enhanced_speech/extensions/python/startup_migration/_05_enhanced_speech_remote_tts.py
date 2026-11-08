"""Deprecated hook retained for image-overlay compatibility. Convo owns startup."""
from helpers.extension import Extension


class EnhancedSpeechCompatibility(Extension):
    def execute(self, **kwargs):
        pass
