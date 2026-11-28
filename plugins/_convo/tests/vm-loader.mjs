// Shared VM module loader for the browser-state tests. Plugin-local specifiers resolve from disk;
// host specifiers (/js/..., /components/..., other plugins) must be supplied explicitly, so a new
// import in a store can never silently load nothing or reach a real host module.
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const root = new URL('..', import.meta.url);
const HOST_PREFIXES = ['/js/', '/components/', '/vendor/'];

export function pluginPath(specifier, from) {
    if (specifier.startsWith('/plugins/_convo/')) return specifier.slice('/plugins/_convo/'.length);
    if (specifier.startsWith('./') || specifier.startsWith('../')) {
        const base = from.split('/').slice(0, -1);
        for (const part of specifier.split('/')) {
            if (part === '.' || part === '') continue;
            if (part === '..') base.pop(); else base.push(part);
        }
        return base.join('/');
    }
    return null;
}

// Alpine hands components a deep reactive Proxy, not the object that was stored. Proxies are not structured-cloneable,
// so code that structuredClone()s store state works on plain test objects and throws in the browser. `reactive`
// reproduces that: every object read through the store is a Proxy over the original.
export function reactive(target, seen = new WeakMap()) {
    if (target === null || typeof target !== 'object') return target;
    if (seen.has(target)) return seen.get(target);
    const proxy = new Proxy(target, { get: (object, key, receiver) => reactive(Reflect.get(object, key, receiver), seen) });
    seen.set(target, proxy);
    return proxy;
}

export const hostStubs = (extra = {}, { reactive: deep = false } = {}) => ({
    '/js/AlpineStore.js': { createStore: (_name, state) => (deep ? reactive(state) : state) },
    '/js/api.js': { callJsonApi: async () => ({ ok: true }), fetchApi: async () => ({ ok: true }) },
    '/js/shortcuts.js': { getCurrentContextId: () => 'chat', getCurrentContext: () => ({ id: 'chat' }), openModal: async () => {} },
    ...extra,
});

export async function loadModule(file, { bindings = {}, globals = {}, evaluate = true } = {}) {
    const context = vm.createContext({ console, setTimeout, clearTimeout, structuredClone, crypto: globalThis.crypto, ...globals });
    const cache = new Map();         // key -> Promise<module>: concurrent requests for one file share one instance
    const unknown = specifier => {
        const hint = HOST_PREFIXES.some(prefix => specifier.startsWith(prefix)) || specifier.startsWith('/plugins/')
            ? 'host or foreign-plugin module; pass it in `bindings`' : 'bare specifier; not supported';
        return new Error(`Unexpected import "${specifier}" (${hint})`);
    };
    // Static imports come back unlinked: `link` walks the whole graph (each request resolved against the module that makes
    // it) and the entry's evaluation then runs every dependency once, in order. Evaluating inside the linker breaks a diamond
    // (a module evaluated while a dependency it shares with a sibling is still linking).
    function resolve(specifier, referrer) {
        const local = pluginPath(specifier, referrer);
        const key = specifier in bindings ? specifier : (local ?? specifier);
        if (!cache.has(key)) cache.set(key, create(specifier, local));
        return cache.get(key);
    }
    async function create(specifier, local) {
        if (specifier in bindings) {
            const exports = bindings[specifier];
            const module = new vm.SyntheticModule(Object.keys(exports), function () { for (const [k, v] of Object.entries(exports)) this.setExport(k, v); }, { context });
            await module.link(() => { throw unknown(specifier); });
            await module.evaluate();
            return module;
        }
        if (!local) throw unknown(specifier);
        return new vm.SourceTextModule(await readFile(new URL(local, root), 'utf8'), { context, identifier: local,
            importModuleDynamically: name => complete(name, local) });
    }
    const link = (specifier, referencing) => resolve(specifier, referencing.identifier);
    // A dynamic import() needs its module linked and evaluated before the namespace is handed over.
    async function complete(specifier, referrer) {
        const module = await resolve(specifier, referrer);
        if (module.status === 'unlinked') await module.link(link);
        if (module.status === 'linked') await module.evaluate();
        return module;
    }
    const entry = new vm.SourceTextModule(await readFile(new URL(file, root), 'utf8'), { context, identifier: file,
        importModuleDynamically: name => complete(name, file) });
    await entry.link(link);
    if (evaluate) await entry.evaluate();
    return { namespace: entry.namespace, context };
}
