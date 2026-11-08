"""Every stored Convo setting is either editable in the shared settings interface or excluded on purpose.

The plugin Config modal and the canvas settings modal embed the same settings-controls.html shell, which composes one
x-component per card, so "every applicable setting is changeable from both" is a property of the files reachable from
those two entry points. A setting counts as editable only through a real binding (x-model, :value, :checked, @change or
@input); a mention in :disabled or x-show is not a control. This guard makes that structural: a new setting cannot be
added to the defaults without either a control or a written reason. A card whose control is still to come declares the
setting in its own file (`<!-- convo-settings-pending: path, path | reason -->`) and removes the declaration with the control.
"""
import re
import unittest
from pathlib import Path

from plugins._convo.helpers.convo_contract import DEFAULTS

WEBUI = Path(__file__).resolve().parents[1] / 'webui'
SETTINGS_ENTRY_POINTS = ('config.html', 'settings-modal.html')
ENTRY_POINTS = SETTINGS_ENTRY_POINTS + ('diagnostics.html',)     # the tunables live in the developer diagnostics panel
COMPONENT = re.compile(r'<x-component\s+path="/plugins/_convo/webui/([\w.-]+\.html)"')
BINDING = re.compile(r'\s(?:x-model(?:\.[\w.]+)?|:value|:checked|x-bind:value|x-bind:checked|@change|@input|x-on:change|x-on:input)="([^"]*)"')
ROLE = re.compile(r'''x-data="\{\s*role:\s*'(\w+)'\s*\}"''')
PENDING = re.compile(r'<!--\s*convo-settings-pending:\s*(.*?)\s*\|\s*(.*?)\s*-->', re.S)

# Stored on purpose but not edited in the interface, each with its reason.
EXCLUDED = {
    'compaction.trigger': 'Compaction thresholds are tuned together; the context window and response reserve are exposed instead.',
    'compaction.target': 'Compaction thresholds are tuned together; the context window and response reserve are exposed instead.',
    'compaction.recent_exchanges': 'A safety floor for what compaction never touches, not a preference.',
    'direct_timeout': 'Bounds a read-only tool call; changing it is a developer decision, not a setting.',
    'clarification_cooldown': 'Part of the clarification policy gate; the contract fixes its floor.',
    'roles.classifier.capabilities.cancellation': 'Declared by the provider adapter; the interface cannot verify it.',
    'decider.kind': 'The decision plugin backend id (one value today); the classifier normalizer selects it, nobody chooses it.',
    'decider.url': 'Derived by the classifier normalizer from roles.classifier.provider/api_base; a saved one is shown through the Classifier card.',
    'decider.model': 'Derived by the classifier normalizer from roles.classifier.model; a saved one is shown through the Classifier card.',
    'roles.tts.capabilities.audio_output': 'Always true for a speech output.',
    'roles.tts.capabilities.cloning': 'Declared by the managed audio adapter; clones are created in Voice Studio.',
}
# Stored only when set (not in the defaults), yet edited in the interface.
OPTIONAL = ('roles.tts.tuning_profile', 'roles.tts.instructions', 'roles.tts.capabilities.native_pcm',
            'roles.conversation.capabilities.audio_input', 'roles.classifier.capabilities.audio_input')
# Stored only when set and never edited directly, each with its reason.
NOT_EDITED = {
    'roles.tts.saved': 'The per-backend stash is managed by switching the backend.',
    'roles.tts.provider_saved': 'The per-provider stash is managed by switching the speech provider.',
    'roles.tts.capabilities.instruction_control': 'Says whether the speech engine honours delivery instructions; set from the provider, not by hand.',
}


def leaves(value, prefix=''):
    for key, item in value.items():
        path = prefix + key
        if isinstance(item, dict) and item:
            yield from leaves(item, path + '.')
        else:
            yield path


def settings_files(entries=ENTRY_POINTS):
    """Every file reachable from the entry points through x-component, entry points included, each once."""
    order, seen = [], set()
    def visit(name):
        if name in seen:
            return
        seen.add(name)
        order.append(name)
        for child in COMPONENT.findall((WEBUI / name).read_text(encoding='utf-8')):
            visit(child)
    for entry in entries:
        visit(entry)
    return order


def bindings(text):
    """The expressions of a file's real bindings, whitespace removed. In a card that fixes its role (x-data="{ role: '...' }")
    `roles[role]` is that role; in a loop over roles it stays `roles[role]` and stands for any role."""
    role = ROLE.search(text)
    found = [re.sub(r'\s+', '', expression) for expression in BINDING.findall(text)]
    return [expression.replace('roles[role].', 'roles.' + role.group(1) + '.') for expression in found] if role else found


def bound_expressions():
    return [expression for name in settings_files() for expression in bindings((WEBUI / name).read_text(encoding='utf-8'))]


def covered(path, expressions):
    candidates = {path}
    parts = path.split('.')
    if parts[0] == 'roles' and len(parts) > 2:
        candidates.add('roles[role].' + '.'.join(parts[2:]))              # a loop that renders every model role
    pattern = re.compile(r'config\.(?:' + '|'.join(re.escape(candidate) for candidate in candidates) + r')(?!\w)')
    return any(pattern.search(expression) for expression in expressions)


def pending():
    """path -> (file, reason) for every setting a card declares as awaiting its control."""
    found = {}
    for name in settings_files():
        for paths, reason in PENDING.findall((WEBUI / name).read_text(encoding='utf-8')):
            for path in re.split(r'[\s,]+', paths.strip()):
                if path:
                    if path in found:
                        raise AssertionError(path + ' is declared pending twice')
                    found[path] = (name, reason)
    return found


class Coverage(unittest.TestCase):
    def test_every_default_setting_has_a_control_or_a_written_reason(self):
        expressions, waiting = bound_expressions(), pending()
        missing = sorted(path for path in leaves(DEFAULTS) if not covered(path, expressions) and path not in EXCLUDED and path not in waiting)
        self.assertEqual(missing, [], 'Bind a control for these settings in a settings card, declare them pending in the card that will own '
                                      'the control, or list them in EXCLUDED with a reason.')

    def test_optional_stored_settings_outside_the_defaults_are_covered_too(self):
        expressions = bound_expressions()
        for path in OPTIONAL:
            self.assertTrue(covered(path, expressions), path)
        for path, reason in NOT_EDITED.items():
            self.assertFalse(covered(path, expressions), path + ' is bound to a control; remove it from NOT_EDITED')
            self.assertGreater(len(reason), 20, path)

    def test_exclusions_and_pending_declarations_are_real_and_not_secretly_covered(self):
        expressions, defaults = bound_expressions(), set(leaves(DEFAULTS))
        for path, reason in EXCLUDED.items():
            self.assertIn(path, defaults, path + ' is not a default setting any more; remove it from EXCLUDED')
            self.assertFalse(covered(path, expressions), path + ' now has a control; remove it from EXCLUDED')
            self.assertGreater(len(reason), 20, path)
        for path, (name, reason) in pending().items():
            self.assertIn(path, defaults, path + ' (pending in ' + name + ') is not a default setting')
            self.assertNotIn(path, EXCLUDED, path)
            self.assertFalse(covered(path, expressions), path + ' now has a control; remove its pending declaration from ' + name)
            self.assertGreater(len(reason), 20, path)

    def test_a_mention_outside_a_binding_is_not_a_control(self):
        self.assertFalse(covered('a.b', bindings('<textarea :disabled="!$store.convoSetup.config.a.b" x-show="$store.convoSetup.config.a.b"></textarea>')))
        self.assertTrue(covered('a.b', bindings('<input type="checkbox" x-model="$store.convoSetup.config.a.b">')))
        self.assertTrue(covered('a.b', bindings('<select :value="$store.convoSetup.config.a.b" @change="x()"></select>')))
        self.assertFalse(covered('a.b', bindings('<input x-model="$store.convoSetup.config.a.bc">')))          # a longer name is another setting
        card = '<fieldset x-data="{ role: \'tts\' }"><input x-model="$store.convoSetup.config.roles[role].url"></fieldset>'
        self.assertTrue(covered('roles.tts.url', bindings(card)))
        self.assertFalse(covered('roles.conversation.url', bindings(card)))                                # the card says which role it edits

    def test_the_two_entry_points_embed_the_one_shell_and_every_card_exists(self):
        for name in SETTINGS_ENTRY_POINTS:
            self.assertIn('/plugins/_convo/webui/settings-controls.html', (WEBUI / name).read_text(encoding='utf-8'), name)
        self.assertIn('/plugins/_convo/webui/tuning-controls.html', (WEBUI / 'diagnostics.html').read_text(encoding='utf-8'))
        files = settings_files()
        self.assertIn('settings-controls.html', files)
        self.assertGreaterEqual(len(files), len(ENTRY_POINTS) + 2)
        for name in files:
            self.assertTrue((WEBUI / name).is_file(), name)

    def test_the_stored_roles_the_interface_edits_match_the_defaults(self):
        markup = re.sub(r'\s+', '', '\n'.join((WEBUI / name).read_text(encoding='utf-8') for name in settings_files()))
        for role in DEFAULTS['roles']:
            self.assertIn(role, re.sub(r'\s+', '', (WEBUI / 'setup-store.js').read_text(encoding='utf-8')), role)
        for section in ('Connections', 'Conversation', 'Voice&Audio'):
            self.assertIn("section==='" + section + "'", markup)


if __name__ == '__main__':
    unittest.main()
