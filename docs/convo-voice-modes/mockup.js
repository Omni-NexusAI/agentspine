// Mockup frame only (not part of the design): each page writes its screen once in <template data-screen> and this script shows
// it twice, in Agent Zero's dark theme and in its light theme (`.light-mode`, the host's own class). Ids are suffixed per copy
// so labels, descriptions and controls stay paired. Controls: which theme(s) to show, and modal or phone width.
const ID_REFS = ['for', 'aria-labelledby', 'aria-describedby', 'aria-controls', 'aria-activedescendant'];

// Material-style icons as one inline sprite (the real cards use the host's <x-icon>). Always aria-hidden, always beside words.
const ICONS = {
    check: 'M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z',
    error: 'M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z',
    warning: 'M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z',
    help: 'M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 17h-2v-2h2v2zm2.07-7.75l-.9.92C13.45 12.9 13 13.5 13 15h-2v-.5c0-1.1.45-2.1 1.17-2.83l1.24-1.26c.37-.36.59-.86.59-1.41 0-1.1-.9-2-2-2s-2 .9-2 2H8c0-2.21 1.79-4 4-4s4 1.79 4 4c0 .88-.36 1.68-.93 2.25z',
    verified: 'M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm-2 16l-4-4 1.41-1.41L10 14.17l6.59-6.59L18 9l-8 8z',
    person: 'M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z',
    doc: 'M14 2H6c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z',
    draft: 'M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 000-1.41l-2.34-2.34a1 1 0 00-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z',
    block: 'M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zM4 12c0-4.42 3.58-8 8-8 1.85 0 3.55.63 4.9 1.69L5.69 16.9A7.9 7.9 0 014 12zm8 8c-1.85 0-3.55-.63-4.9-1.69L18.31 7.1A7.9 7.9 0 0120 12c0 4.42-3.58 8-8 8z',
    mic: 'M12 14c1.66 0 2.99-1.34 2.99-3L15 5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm5.3-3c0 3-2.54 5.1-5.3 5.1S6.7 14 6.7 11H5c0 3.41 2.72 6.23 6 6.72V21h2v-3.28c3.28-.48 6-3.3 6-6.72h-1.7z',
    speaker: 'M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z',
    decide: 'M14 4l2.29 2.29-2.88 2.88 1.42 1.42 2.88-2.88L20 10V4h-6zm-4 0H4v6l2.29-2.29 4.71 4.7V20h2v-8.41l-5.29-5.3L10 4z',
    lock: 'M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1 1.71 0 3.1 1.39 3.1 3.1v2z',
    auto: 'M19 9l1.25-2.75L23 5l-2.75-1.25L19 1l-1.25 2.75L15 5l2.75 1.25L19 9zm-7.5.5L9 4 6.5 9.5 1 12l5.5 2.5L9 20l2.5-5.5L17 12l-5.5-2.5zM19 15l-1.25 2.75L15 19l2.75 1.25L19 23l1.25-2.75L23 19l-2.75-1.25L19 15z',
    chevron: 'M10 6L8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z',
    search: 'M15.5 14h-.79l-.28-.27A6.47 6.47 0 0016 9.5 6.5 6.5 0 109.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z',
    tune: 'M3 17v2h6v-2H3zM3 5v2h10V5H3zm10 16v-2h8v-2h-8v-2h-2v6h2zM7 9v2H3v2h4v2h2V9H7zm14 4v-2H11v2h10zm-6-4h2V7h4V5h-4V3h-2v6z',
    info: 'M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z',
    wave: 'M7 18h2V6H7v12zm4 4h2V2h-2v20zm-8-8h2v-4H3v4zm12 4h2V6h-2v12zm4-8v4h2v-4h-2z',
};
const sprite = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
sprite.setAttribute('aria-hidden', 'true');
sprite.style.display = 'none';
sprite.innerHTML = Object.entries(ICONS).map(([name, path]) => `<symbol id="i-${name}" viewBox="0 0 24 24"><path d="${path}"/></symbol>`).join('');
document.body.prepend(sprite);

function copy(template, theme, index) {
    const fragment = template.content.cloneNode(true);
    const suffix = '-' + theme + '-' + index;      // unique per screen and theme, so radio groups and ids never collide
    fragment.querySelectorAll('[id]').forEach(node => { node.id += suffix; });
    fragment.querySelectorAll('*').forEach(node => {
        for (const name of ID_REFS) {
            const value = node.getAttribute(name);
            if (value) node.setAttribute(name, value.split(/\s+/).map(id => id + suffix).join(' '));
        }
        if (node.tagName === 'INPUT' && node.type === 'radio' && node.name) node.name += suffix;
        const href = node.getAttribute('href');
        if (node.tagName === 'A' && href && href.startsWith('#')) node.setAttribute('href', href + suffix);
    });
    return fragment;
}

function frame(template, theme, index) {
    const section = document.createElement('section');
    section.className = 'mock-frame' + (theme === 'light' ? ' light-mode' : ' theme-dark');
    section.dataset.themePane = theme;
    section.dataset.width = template.dataset.width || 'modal';
    section.setAttribute('aria-label', (template.dataset.title || 'Mockup') + ', ' + theme + ' theme');
    const label = document.createElement('div');
    label.className = 'mock-frame-label';
    label.innerHTML = '<span>' + (theme === 'light' ? 'Light theme' : 'Dark theme') + '</span><span>' + (template.dataset.where || 'Convo settings') + '</span>';
    const body = document.createElement('div');
    body.className = 'mock-frame-body' + (template.dataset.width === 'phone' ? ' convo-narrow' : '');
    body.appendChild(copy(template, theme, index));
    section.append(label, body);
    return section;
}

document.querySelectorAll('template[data-screen]').forEach((template, index) => {
    const pair = document.createElement('div');
    pair.className = 'mock-pair';
    pair.dataset.show = 'both';
    pair.append(frame(template, 'dark', index), frame(template, 'light', index));
    template.after(pair);
});

// Page controls: theme(s) shown and width.
const controls = document.querySelector('[data-mock-controls]');
if (controls) {
    controls.addEventListener('change', event => {
        const input = event.target;
        if (input.name === 'mock-show') document.querySelectorAll('.mock-pair').forEach(pair => { pair.dataset.show = input.value; });
        if (input.name === 'mock-width') document.querySelectorAll('.mock-frame').forEach(section => {
            const narrow = input.value === 'phone';
            section.dataset.width = narrow ? 'phone' : 'modal';
            section.querySelector('.mock-frame-body').classList.toggle('convo-narrow', narrow);
        });
    });
}
