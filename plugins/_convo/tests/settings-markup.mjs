// The settings interface is one shell (settings-controls.html) composed of x-components, one per card. Markup checks read
// every file reachable from the entry points, so a card can move into its own component without a check losing sight of it.
// tests/test_settings_coverage.py walks the same tree.
import { readFile } from 'node:fs/promises';

const WEBUI = new URL('../webui/', import.meta.url);
export const ENTRY_POINTS = ['config.html', 'settings-modal.html'];
export const COMPONENT = /<x-component\s+path="\/plugins\/_convo\/webui\/([\w.-]+\.html)"/g;

export const readWebui = name => readFile(new URL(name, WEBUI), 'utf8');

// Component files in document order, depth first, each once (the entry points themselves are not included).
export async function settingsFiles(entries = ENTRY_POINTS) {
    const seen = new Set(entries), order = [];
    const visit = async name => {
        for (const [, child] of (await readWebui(name)).matchAll(COMPONENT)) {
            if (seen.has(child)) continue;
            seen.add(child); order.push(child);
            await visit(child);
        }
    };
    for (const entry of entries) await visit(entry);
    return order;
}

// The union of the settings markup, file by file in document order.
export async function settingsMarkup(entries = ENTRY_POINTS) {
    return (await Promise.all((await settingsFiles(entries)).map(readWebui))).join('\n');
}
