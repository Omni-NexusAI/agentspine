"""Convo's use of the Steering plugin (plugins/steering): a change to work that is going on joins the run at its next step.

The plugin is optional. `module()` is its rules when it is installed and switched on, else None; nothing of it is imported at load time,
and a plugin that cannot be read counts as absent. Without it a message to a running task is Agent Zero's own interruption (the step in
progress is lost), and Convo says so instead of calling it steering.
"""
from __future__ import annotations

import importlib
import logging
import time

logger = logging.getLogger(__name__)

PLUGIN = 'steering'
CHECK_SECONDS = 5.0             # how long an answer to "is it on" is used
_seen = {'at': None, 'module': None}


def module():
    """The Steering plugin's rules, or None. Never raises."""
    now = time.monotonic()
    if _seen['at'] is not None and now - _seen['at'] < CHECK_SECONDS:
        return _seen['module']
    found = None
    try:
        from helpers import plugins
        if PLUGIN in plugins.get_enabled_plugins(None):
            found = importlib.import_module('usr.plugins.steering.helpers.steering')
    except Exception:
        logger.debug('The Steering plugin is not available', exc_info=True)
        found = None
    _seen.update(at=now, module=found)
    return found


def available():
    return module() is not None


def busy(context):
    """Whether an agent is at work in this chat although Agent Zero does not call it running (a sub-agent, a scheduled task)."""
    rules = module()
    try:
        return bool(rules is not None and rules.busy(context))
    except Exception:
        return False


def steer(context, text):
    """Keep a change for the next step of the agent at work in this chat. Its id, or '' when it could not be kept that way."""
    rules = module()
    if rules is None:
        return ''
    return rules.steer(context, text, source='Convo', caller=True) or ''


def status(context, item):
    """waiting, delivered, undelivered, held, sent, or '' (unknown: Agent Zero restarted, or the plugin is off)."""
    rules = module()
    try:
        return rules.status(context, item) if rules is not None else ''
    except Exception:
        return ''


def forget(context, item):
    rules = module()
    try:
        if rules is not None:
            rules.forget(context, item)
    except Exception:
        logger.debug('A change could not be taken back from the Steering plugin', exc_info=True)
