// Browser monitor: resources + event recorder.
//
// Two surfaces share one store:
//   sidebars/browser_monitor.html -> selector / controls
//   chat/index.html               -> full chat-column cover
//
// The backend owns sampling and recording (webui_perf.py -> perf.py). This file
// only polls the extension routes and keeps the two views in sync.

const BROWSER_MONITOR = {
    mode: localStorage.getItem('bm.mode') || 'resources',
    targetId: localStorage.getItem('bm.target') || '',
    targets: [],
    samples: [],
    latestSample: null,
    events: [],
    connected: false,
    recording: false,
    eventCount: 0,
    sessionSecondsValue: 0,
    intervalMs: 1000,
    error: '',
    busy: false,
    filterKind: 'all',
    filterKinds: ['all', 'scroll', 'input', 'raf', 'longtask', 'console', 'network', 'navigation', 'error'],
    minDuration: 0,
    duringScrollOnly: false,
    eventSearch: '',
    selectedEvent: null,
    _timer: null,

    init() {
        if (!this._timer) {
            this._timer = setInterval(() => {
                if (!document.hidden) this.poll();
            }, 1000);
        }
        this.refreshAll();
    },

    async refreshAll() {
        this.error = '';
        await this.refreshTargets();
        await this.poll();
    },

    async refreshTargets() {
        try {
            const r = await simpleApiFetch('/api/ext/browser/perf/targets');
            this.connected = !!r.connected;
            const palette = ['#a78bfa', '#4aa3ff', '#2ecc71', '#e67e22', '#e05c5c', '#d6a84a'];
            this.targets = (r.targets || []).map((t, i) => ({
                ...t, color: t.color || palette[i % palette.length],
            }));
            if (!this.targetId || !this.targets.some((t) => t.id === this.targetId)) {
                const preferred = this.targets.find((t) => t.active) || this.targets[0];
                if (preferred) {
                    this.targetId = preferred.id;
                    localStorage.setItem('bm.target', this.targetId);
                    await this.selectTarget(this.targetId);
                } else {
                    this.targetId = '';
                    localStorage.removeItem('bm.target');
                }
            }
            if (r.selected && r.selected !== this.targetId) {
                this.targetId = r.selected;
                localStorage.setItem('bm.target', r.selected);
            }
        } catch (e) {
            this.error = 'could not read tabs (' + this.errText(e) + ')';
            this.connected = false;
            this.targets = [];
        }
    },

    async selectTarget(id) {
        this.targetId = id || '';
        if (id) localStorage.setItem('bm.target', id);
        else localStorage.removeItem('bm.target');
        this.samples = [];
        this.latestSample = null;
        this.error = '';
        try {
            await simpleApiPost('/api/ext/browser/perf/select', { target_id: id || null });
        } catch (e) {
            this.error = 'could not select tab (' + this.errText(e) + ')';
        }
    },

    async poll() {
        if (this.mode === 'events') return this.pollEvents();
        return this.pollSummary();
    },

    async pollSummary() {
        try {
            const q = this.targetId
                ? '?target_id=' + encodeURIComponent(this.targetId) + '&limit=180'
                : '?limit=180';
            const r = await simpleApiFetch('/api/ext/browser/perf/summary' + q);
            this.connected = !!r.connected;
            this.recording = !!r.recording;
            this.eventCount = r.event_count || 0;
            this.sessionSecondsValue = r.session_seconds || 0;
            this.intervalMs = r.interval_ms || 1000;
            this.samples = r.samples || [];
            this.latestSample = r.latest || null;
            if (r.selected && r.selected !== this.targetId) {
                this.targetId = r.selected;
                localStorage.setItem('bm.target', r.selected);
            }
        } catch (e) {
            this.connected = false;
            this.error = this.errText(e);
        }
    },

    async pollEvents() {
        try {
            const r = await simpleApiFetch('/api/ext/browser/perf/events?limit=300');
            this.connected = !!r.connected;
            this.recording = !!r.recording;
            this.sessionSecondsValue = r.session_seconds || 0;
            this.eventCount = r.event_count || 0;
            this.events = r.events || [];
        } catch (e) {
            this.connected = false;
            this.error = this.errText(e);
        }
    },

    setMode(mode) {
        this.mode = mode === 'events' ? 'events' : 'resources';
        localStorage.setItem('bm.mode', this.mode);
        this.poll();
    },

    async toggleRecording() {
        if (this.busy) return;
        this.busy = true;
        try {
            const r = await simpleApiPost('/api/ext/browser/perf/record', {
                recording: !this.recording,
            });
            this.recording = !!r.recording;
            this.eventCount = r.event_count || 0;
            this.sessionSecondsValue = r.session_seconds || 0;
            await this.pollEvents();
        } catch (e) {
            this.error = 'could not change recording (' + this.errText(e) + ')';
        } finally {
            this.busy = false;
        }
    },

    async clearSamples() {
        if (this.busy) return;
        this.busy = true;
        try {
            await simpleApiPost('/api/ext/browser/perf/clear', { target_id: this.targetId });
            this.samples = [];
            this.latestSample = null;
        } catch (e) {
            this.error = 'could not clear history (' + this.errText(e) + ')';
        } finally {
            this.busy = false;
        }
    },

    async clearEvents() {
        if (this.busy) return;
        this.busy = true;
        try {
            await simpleApiPost('/api/ext/browser/perf/events/clear', {});
            this.events = [];
            this.eventCount = 0;
            this.sessionSecondsValue = 0;
            this.selectedEvent = null;
        } catch (e) {
            this.error = 'could not clear events (' + this.errText(e) + ')';
        } finally {
            this.busy = false;
        }
    },

    closeOverlay() {
        try {
            const shell = window.Alpine
                ? Alpine.$data(document.querySelector('.sidebar-shell'))
                : null;
            if (shell && shell.setTab) shell.setTab('');
        } catch (e) { /* already closed */ }
        document.body.removeAttribute('data-bm');
    },

    currentTarget() {
        return this.targets.find((t) => t.id === this.targetId) || null;
    },

    targetLabel() {
        const t = this.currentTarget();
        return t ? (t.title || t.id) : 'no tab selected';
    },

    statusLine() {
        const t = this.currentTarget();
        return t ? this.tabLabel(t) : (this.connected ? 'no tab selected' : 'browser not connected');
    },

    tabLabel(t) {
        if (!t) return 'tab';
        const name = t.title || t.url || t.id;
        return name.length > 52 ? name.slice(0, 49) + '...' : name;
    },

    modeLabel() {
        return this.mode === 'events' ? 'Event manager' : 'Resource manager';
    },

    clock(seconds) {
        return new Date((Number(seconds) || 0) * 1000).toLocaleTimeString();
    },

    pageInitials() {
        const t = this.currentTarget();
        const match = String((t && t.title) || '?').match(/[A-Za-z0-9]/);
        return match ? match[0].toUpperCase() : '?';
    },

    fmtMB(bytes) {
        return (Number(bytes || 0) / 1048576).toFixed(1) + ' MB';
    },

    fmtNum(n) {
        return Number(n || 0).toLocaleString();
    },

    latest() {
        return this.latestSample;
    },

    health() {
        const l = this.latestSample;
        if (!l) return 0;
        let score = 100;
        if (l.fps < 30) score -= 38;
        else if (l.fps < 50) score -= 16;
        if (l.script_pct > 20) score -= 24;
        else if (l.script_pct > 10) score -= 9;
        if (l.heap > 150 * 1048576) score -= 18;
        else if (l.heap > 90 * 1048576) score -= 7;
        if (l.long_tasks > 20) score -= 12;
        else if (l.long_tasks > 8) score -= 5;
        if (l.nodes > 4000) score -= 10;
        return Math.max(0, Math.min(100, Math.round(score)));
    },

    healthClass() {
        const h = this.health();
        return h >= 80 ? 'good' : h >= 55 ? 'warn' : 'bad';
    },

    healthLabel() {
        const h = this.health();
        return h >= 80 ? 'smooth' : h >= 55 ? 'busy' : 'struggling';
    },

    kpiClass(key) {
        const l = this.latestSample;
        if (!l) return 'neutral';
        let bad = false, warn = false;
        if (key === 'fps') { bad = l.fps < 30; warn = l.fps < 50; }
        else if (key === 'heap') { bad = l.heap > 150 * 1048576; warn = l.heap > 90 * 1048576; }
        else if (key === 'nodes') { bad = l.nodes > 4000; warn = l.nodes > 2600; }
        else if (key === 'script_pct') { bad = l.script_pct > 20; warn = l.script_pct > 10; }
        else if (key === 'task_pct') { bad = l.task_pct > 28; warn = l.task_pct > 14; }
        else if (key === 'layout_per_s') { bad = l.layout_per_s > 20; warn = l.layout_per_s > 10; }
        else if (key === 'style_per_s') { bad = l.style_per_s > 20; warn = l.style_per_s > 10; }
        else if (key === 'long_tasks') { bad = l.long_tasks > 20; warn = l.long_tasks > 8; }
        else if (key === 'listeners') { bad = l.listeners > 220; warn = l.listeners > 150; }
        return bad ? 'bad' : warn ? 'warn' : 'good';
    },

    trend(key) {
        const n = this.samples.length;
        if (n < 2) return 'flat';
        const a = Number(this.samples[n - 2][key]) || 0;
        const b = Number(this.samples[n - 1][key]) || 0;
        if (Math.abs(b - a) < 0.01) return 'flat';
        return b > a ? 'up' : 'down';
    },

    trendGlyph(key) {
        const t = this.trend(key);
        return t === 'up' ? '▲' : t === 'down' ? '▼' : '·';
    },

    trendClass(key) {
        const t = this.trend(key);
        if (t === 'flat') return 'flat';
        if (key === 'fps') return t === 'up' ? 'up' : 'down';
        return t === 'up' ? 'down' : 'up';
    },

    chartPoints(key, w = 620, h = 150) {
        const vals = this.samples.map((s) => Number(s[key]) || 0).slice(-60);
        if (!vals.length) return '';
        const peak = Math.max(...vals, 1);
        if (vals.length === 1) return `0,${h - (vals[0] / peak) * h}`;
        return vals.map((v, i) => {
            const x = Math.round((i / (vals.length - 1)) * w);
            const y = Math.round(h - (v / peak) * (h - 8) - 4);
            return `${x},${y}`;
        }).join(' ');
    },

    chartArea(key, w = 620, h = 150) {
        const pts = this.chartPoints(key, w, h);
        return pts ? `0,${h} ${pts} ${w},${h}` : '';
    },

    sessionSeconds() {
        return this.sessionSecondsValue;
    },

    sessionTime() {
        const s = Math.floor(this.sessionSeconds());
        return s >= 60 ? `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s` : `${s}s`;
    },

    eventsPerSecond() {
        const s = this.sessionSeconds();
        return s > 0 ? (this.events.length / s).toFixed(1) : '0.0';
    },

    slowestEvent() {
        let best = null;
        for (const e of this.events) if (!best || e.duration > best.duration) best = e;
        return best;
    },

    blockedMs() {
        return Math.round(this.events.filter((e) => e.kind === 'longtask')
            .reduce((sum, e) => sum + (Number(e.duration) || 0), 0));
    },

    topHandlers() {
        const byName = new Map();
        for (const e of this.events) {
            if (!e.handler || e.handler.name === '—') continue;
            const row = byName.get(e.handler.name) || { name: e.handler.name, count: 0, ms: 0 };
            row.count += 1;
            row.ms += Number(e.duration) || 0;
            byName.set(e.handler.name, row);
        }
        return [...byName.values()].sort((a, b) => b.ms - a.ms).slice(0, 3);
    },

    density() {
        const buckets = Array(40).fill(0);
        const now = Date.now();
        for (const e of this.events) {
            const age = (now - (e.wall_ms || now)) / 1000;
            if (age >= 0 && age < 40) {
                const idx = 39 - Math.floor(age);
                if (idx >= 0) buckets[idx] += 1;
            }
        }
        const max = Math.max(...buckets, 1);
        return buckets.map((v) => Math.max(4, Math.round((v / max) * 100)));
    },

    setFilter(kind) {
        this.filterKind = kind;
    },

    filterLabel(kind) {
        return {
            all: 'All', scroll: 'Scroll', input: 'Input', raf: 'RAF',
            longtask: 'Long tasks', console: 'Console', network: 'Network',
            navigation: 'Nav', error: 'Errors',
        }[kind] || kind;
    },

    filterCount(kind) {
        return this.events.filter((e) => kind === 'all' || e.kind === kind).length;
    },

    filteredEvents() {
        const term = String(this.eventSearch || '').trim().toLowerCase();
        const min = Number(this.minDuration || 0);
        return this.events.filter((e) => {
            if (this.filterKind !== 'all' && e.kind !== this.filterKind) return false;
            if ((Number(e.duration) || 0) < min) return false;
            if (this.duringScrollOnly && !e.during_scroll) return false;
            if (term) {
                const hay = `${e.kind} ${e.name} ${e.target} ${e.handler?.name || ''}`.toLowerCase();
                if (!hay.includes(term)) return false;
            }
            return true;
        });
    },

    selectEvent(event) {
        this.selectedEvent = event;
    },

    closeEvent() {
        this.selectedEvent = null;
    },

    handlerName(event) {
        return event && event.handler && event.handler.name ? event.handler.name : '—';
    },

    fmtRel(ms) {
        const v = Number(ms) || 0;
        return v >= 1000 ? '+' + (v / 1000).toFixed(2) + 's' : '+' + v.toFixed(1) + 'ms';
    },

    errText(e) {
        return typeof e === 'string' ? e : ((e && e.message) || String(e));
    },
};

window.BROWSER_MONITOR = BROWSER_MONITOR;

// Never pass the same object to two x-data scopes: Alpine re-injects its
// magics and fails with "Cannot redefine property: $nextTick". Each surface
// gets a fresh proxy object over the shared store.
function monitorComponent() {
    const store = (window.Alpine && Alpine.store && Alpine.store('browserMonitor')) || BROWSER_MONITOR;
    const target = {};
    for (const [key, value] of Object.entries(store)) {
        if (typeof value === 'function') {
            target[key] = (...args) => store[key](...args);
        } else {
            Object.defineProperty(target, key, {
                get: () => store[key],
                set: (v) => { store[key] = v; },
                enumerable: true,
                configurable: true,
            });
        }
    }
    return target;
}

window.BROWSER_MONITOR_PANEL = monitorComponent;
window.BROWSER_MONITOR_VIEW = monitorComponent;

document.addEventListener('alpine:init', () => {
    Alpine.store('browserMonitor', BROWSER_MONITOR);
});

// Start polling from the mounted Alpine component (x-init on the sidebar
// panel), NOT from this raw object: writes through the raw object update data
// without notifying Alpine's effects, which leaves the monitor frozen.
