// The rules every model connection card shares (conversation model, classifier, speech): which addresses are this machine or your
// network, and what a credential environment reference may hold. Browser copies of convo_contract.local_host and
// convo_contract.credential_reference_problem, for advice only: the server decides, and the tests hold both to one table.

export const LOCAL_NAMES = ['localhost', 'host.docker.internal', 'convo-runtime', 'convo-audio', 'kokoro-worker'];
// Name endings reserved for a private network: mDNS, home routers, RFC 8375/6761, ICANN's .internal and Tailscale's MagicDNS.
export const LOCAL_SUFFIXES = ['.local', '.lan', '.home.arpa', '.internal', '.localhost', '.ts.net'];
const LABEL = /^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$/;              // a one-label machine name; an all-digit one is an address
const V4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/;
const ENV_REFERENCE = /^[A-Za-z0-9_]{1,128}$/;

// This machine, Convo's containers or your network: loopback, private (RFC 1918, IPv6 unique local) and Tailscale (100.64.0.0/10)
// addresses; Convo's names, a one-label machine name and the private-use endings (a *.ts.net name is taken to be in your
// tailnet). Judged as written; nothing is looked up.
export function isLocalHost(hostname) {
    const name = String(hostname || '').toLowerCase().replace(/^\[|\]$/g, '').replace(/\.$/, '');
    const v4 = name.match(V4);
    if (v4) {
        const [a, b, c, d] = v4.slice(1).map(Number);
        if ([a, b, c, d].some(part => part > 255)) return false;
        return a === 10 || a === 127 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 100 && b >= 64 && b <= 127);
    }
    if (name.includes(':')) return name === '::1' || /^f[cd][0-9a-f]{0,2}:/.test(name);      // IPv6 loopback or unique local
    return LOCAL_NAMES.includes(name) || LABEL.test(name) || LOCAL_SUFFIXES.some(end => name.endsWith(end) && name.length > end.length);
}

// How to reach a server on another computer: the hint every server-address field shows.
export const NETWORK_HINT = 'On another computer, use its LAN or Tailscale address, for example http://192.168.1.50:8080/v1 or http://my-desktop.tail1234.ts.net:8080/v1; no consent is needed for your own network.';

// Why a credential environment reference would be refused ('' when it would not): it names an environment variable. The value is
// never repeated, so a key pasted here does not show up again.
export function credentialReferenceProblem(name) {
    if (name === '' || name === undefined || name === null) return '';
    if (typeof name === 'string' && ENV_REFERENCE.test(name)) return '';
    const text = typeof name === 'string' ? name.trim() : '';
    const hint = text.includes('://') || /^(https?:|www\.)/i.test(text) ? ' This looks like an address: addresses go in API base URL.'
        : text.length >= 16 && !text.includes(' ') ? ' This looks like a key itself: enter keys in the API key field, where they are stored masked.' : '';
    return 'Credential environment reference must be the name of an environment variable, such as MY_API_KEY (letters, digits and underscores).' + hint;
}
