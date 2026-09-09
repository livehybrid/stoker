/*
 * A smoke test for the built console.
 *
 * It builds the app into one IIFE, loads it into jsdom against a stubbed API,
 * walks every route, and fails on the first React error, unhandled rejection
 * or console.error. That is deliberately a build rather than the sources: the
 * failures worth catching here (a component imported from the wrong path, a
 * prop the library rejects, an undefined read while rendering a table) only
 * appear once it runs.
 *
 * It is a single-file build, not the shipped one. The shipped build is ES
 * modules with per-route code splitting, and jsdom has no module-script
 * support, so it cannot evaluate that entry at all. The trade-off is explicit:
 * this exercises the same code, not the same chunking, and will not catch a
 * broken dynamic import.
 *
 *   npm run smoke
 *
 * It is not a substitute for opening the page. It is the check that stops a
 * broken build reaching someone who then has to open the page to find out.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM, VirtualConsole } from 'jsdom';

const here = path.dirname(fileURLToPath(import.meta.url));
const dist = path.join(here, 'dist-smoke');
const bundlePath = path.join(dist, 'smoke-bundle.js');

if (!fs.existsSync(bundlePath)) {
    console.error('no dist-smoke/smoke-bundle.js: run `npm run build:smoke` first');
    process.exit(1);
}
const bundle = fs.readFileSync(bundlePath, 'utf8');
const html = '<!doctype html><html><head></head><body><div id="root"></div></body></html>';

/* ------------------------------------------------------------- fixtures */

const now = '2026-09-09T07:00:00Z';
const USER = { id: 1, username: 'will', role: 'admin', created_at: now };
const TARGET = {
    id: 1,
    name: 'lab-hec',
    hec_url: 'https://splunk.lab:8088/services/collector',
    env_tag: 'lab',
    health_state: 'green',
    default_index: 'main',
    verify_tls: true,
    max_concurrent_gb_day: 250,
    created_at: now,
};
const SPEC = {
    id: 3,
    name: 'steady-2k',
    engine: 'eventgen',
    fleet: 'docker',
    workers: 4,
    target_id: 1,
    pack_id: 5,
    rate_mode: 'eps',
    rate_value: 2000,
    duration_s: 600,
    overrides: {},
    created_at: now,
};
const PACK = {
    id: 5,
    name: 'apache-access',
    repo_id: 2,
    kind: 'eventgen',
    stanza_count: 4,
    description: 'Apache access logs',
    created_at: now,
    lint_ok: true,
};
const REPO = {
    id: 2,
    url: 'https://github.com/livehybrid/packs',
    head_sha: 'abcdef1234567890',
    last_synced_at: now,
    trusted_code: false,
    created_at: now,
};
const RUN = {
    id: 7,
    spec_id: 3,
    state: 'running',
    degraded: false,
    created_at: now,
    t0: now,
    ended_at: null,
    end_reason: null,
    resolved_sha: 'abcdef1',
    totals_json: {
        events_total: 1200000,
        bytes_total: 900000000,
        hec_2xx: 12000,
        hec_4xx: 2,
        hec_5xx: 0,
        hec_timeouts: 1,
        retries: 3,
    },
};
/*
 * Four metric samples a minute apart: enough for the charts to have something
 * to draw, which is what proves the Splunk chart library loaded, accepted the
 * dataSource and rendered.
 */
const SAMPLES = [0, 60, 120, 180].map((d, i) => ({
    id: i + 1,
    run_id: 7,
    slot: 0,
    ts: new Date(Date.parse(now) + d * 1000).toISOString(),
    eps: 1900 + d,
    bps: 1_400_000 + d * 100,
    lag_s: 2,
    queue_depth: 0,
    rss_mb: 180,
    hec_2xx: 1000 * (i + 1),
    hec_4xx: i,
    hec_5xx: 0,
    hec_timeouts: 0,
}));

const LEASE = {
    id: 11,
    run_id: 7,
    slot: 0,
    state: 'running',
    holder: 'worker-a',
    node: 'node-1',
    restarts: 1,
    assigned_json: { eps: 500 },
};

const ROUTES = {
    '/auth/status': { authenticated: true, setup_needed: false, sso_enabled: false, user: USER },
    '/auth/me': USER,
    '/targets': [TARGET],
    '/specs': [SPEC],
    '/packs': [PACK],
    '/repos': [REPO],
    '/runs': [RUN],
    '/users': [USER],
    '/fleets': [{ kind: 'docker', available: true, detail: 'local docker' }],
    '/runs/7': {
        ...RUN,
        leases: [LEASE],
        spec_snapshot_json: { engine: 'eventgen', overrides: { index: 'main' } },
    },
    '/runs/7/metrics': { samples: SAMPLES },
    '/runs/7/events': { events: [] },
};

function lookup(url) {
    const route = url.replace(/^\/api/, '').split('?')[0];
    if (route in ROUTES) {
        return ROUTES[route];
    }
    if (/^\/runs\/\d+$/.test(route)) {
        return ROUTES['/runs/7'];
    }
    if (route.endsWith('/leases')) {
        return [LEASE];
    }
    if (route.endsWith('/metrics') || route.endsWith('/samples')) {
        return { samples: SAMPLES };
    }
    if (route.endsWith('/events')) {
        return { events: [] };
    }
    return [];
}

/* -------------------------------------------------------------- harness */

const problems = [];

/*
 * jsdom implements no canvas and no layout engine. The chart library touches
 * both, and neither is a defect in the console: nothing here asserts on pixels,
 * and the accelerated canvas render path is off. Everything else still fails
 * the run, so this stays a short, named list rather than a blanket ignore.
 */
const EXPECTED_IN_JSDOM = [
    'Not implemented: HTMLCanvasElement.prototype.getContext',
    'Could not parse CSS stylesheet',
    'Not implemented: window.scrollTo',
];
const expected = (m) => EXPECTED_IN_JSDOM.some((known) => String(m).includes(known));

const virtualConsole = new VirtualConsole();
virtualConsole.on('error', (m) => {
    if (!expected(m)) {
        problems.push(`console.error: ${String(m).slice(0, 300)}`);
    }
});
virtualConsole.on('jsdomError', (e) => {
    if (!expected(e.message)) {
        problems.push(`jsdom: ${e.message.slice(0, 300)}`);
    }
});

const dom = new JSDOM(html, {
    url: 'http://localhost/',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
    virtualConsole,
});
const { window } = dom;

// Pieces of a browser jsdom does not implement but the libraries legitimately use.
window.SVGElement.prototype.getBBox =
    window.SVGElement.prototype.getBBox || (() => ({ x: 0, y: 0, width: 0, height: 0 }));
window.ResizeObserver = class {
    constructor(callback) {
        this.callback = callback;
    }

    observe(target) {
        this.callback([{ target, contentRect: { width: 640, height: 240 } }], this);
    }

    unobserve() {}

    disconnect() {}
};
Object.defineProperty(window.HTMLElement.prototype, 'clientWidth', {
    configurable: true,
    get: () => 640,
});
Object.defineProperty(window.HTMLElement.prototype, 'clientHeight', {
    configurable: true,
    get: () => 240,
});
window.HTMLElement.prototype.getBoundingClientRect = () => ({
    x: 0,
    y: 0,
    top: 0,
    left: 0,
    right: 640,
    bottom: 240,
    width: 640,
    height: 240,
});
window.matchMedia = (q) => ({
    matches: false,
    media: q,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
});
window.scrollTo = () => {};
window.HTMLElement.prototype.scrollIntoView = () => {};

window.fetch = async (url) => {
    const body = lookup(String(url));
    return {
        ok: true,
        status: 200,
        statusText: 'OK',
        headers: { get: () => 'application/json' },
        json: async () => body,
        text: async () => JSON.stringify(body),
    };
};

window.addEventListener('error', (e) => problems.push(`window error: ${e.message}`));
window.addEventListener('unhandledrejection', (e) =>
    problems.push(`unhandled rejection: ${e.reason}`)
);

window.eval(bundle);

const settle = (ms = 60) => new Promise((r) => setTimeout(r, ms));

/*
 * Wait for text rather than sleeping for a fixed time. Every page here fetches
 * before it can render anything, and the route chunks are loaded on demand, so
 * a fixed sleep is a race that passes on a quiet machine and fails on a busy
 * one, which is the worst kind of test.
 */
const check = async (label, needle, timeoutMs = 15000) => {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
        if ((window.document.body.textContent || '').includes(needle)) {
            process.stdout.write(`  ok  ${label}\n`);
            return;
        }
        if (Date.now() > deadline) {
            problems.push(`${label}: expected to find ${JSON.stringify(needle)} on the page`);
            return;
        }
        await settle(); // eslint-disable-line no-await-in-loop
    }
};

/** The same wait, for a condition that is not a piece of text. */
const waitFor = async (label, predicate, timeoutMs = 15000) => {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
        if (predicate()) {
            process.stdout.write(`  ok  ${label}\n`);
            return;
        }
        if (Date.now() > deadline) {
            problems.push(`${label}: the condition never became true`);
            return;
        }
        await settle(); // eslint-disable-line no-await-in-loop
    }
};

/** Navigate with the router's own history, as a link click would. */
const go = async (pathname) => {
    window.history.pushState({}, '', pathname);
    window.dispatchEvent(new window.PopStateEvent('popstate'));
    await settle(200);
};

(async () => {
    await settle(600);
    await check('shell', 'Stoker');
    await check('dashboard', 'Dashboard');

    await go('/runs');
    await check('runs', 'steady-2k');

    await go('/runs/7');
    await check('run detail: totals', 'Events');
    await check('run detail: lease roster', 'worker-a');
    await check('run detail: tabs', 'Spec snapshot');

    /*
     * The charts are Splunk Line components, and this is the assertion that
     * they actually drew rather than that a container exists: the series names
     * only appear once the chart library has loaded, taken the dataSource and
     * laid out a legend.
     */
    await waitFor(
        'run detail: charts drew',
        () => window.document.querySelectorAll('svg').length > 0,
        30000
    );
    const svgText = () =>
        Array.from(window.document.querySelectorAll('svg text')).map((t) =>
            (t.textContent || '').trim()
        );
    await waitFor('run detail: chart series', () => svgText().includes('Actual ev/s'));
    await waitFor('run detail: chart overlay axis', () => svgText().includes('Bytes/s'));

    await go('/specs');
    await check('specs', 'steady-2k');

    await go('/packs');
    await check('packs', 'apache-access');

    await go('/repos');
    await check('repos', 'livehybrid/packs');

    await go('/targets');
    await check('targets', 'lab-hec');

    await go('/users');
    await check('users', 'will');

    await settle(200);

    if (problems.length) {
        console.error('\nsmoke FAILED:');
        problems.forEach((p) => console.error(`  - ${p}`));
        process.exit(1);
    }
    console.log('\nsmoke passed');
    process.exit(0);
})();
