// Browser monitor: resources + event recorder.
//
// Two surfaces share one store:
//   sidebars/browser_monitor.html -> selector / controls
//   chat/index.html               -> full chat-column cover
//
// The backend owns sampling and recording (webui_perf.py -> perf.py). This file
// only polls the extension routes and keeps the two views in sync.

// Session-graph geometry, one pass per sample set.
//
// The template asks for graphArea() and graphLine(), and graphArea() builds
// the line itself, so a redraw walked every sample twice; worse, graphY()
// asked for graphPeak() per point and graphPeak() reduced over all of them,
// i.e. O(n^2) reads for one polyline (a 464 s take is ~1860 samples, ~7 M).
// The geometry is therefore computed once per sample set and cached.
//
// The cache lives at module scope on purpose: on the store it would be wrapped
// by @vue/reactivity and every point read would go through a proxy trap, which
// is the cost being removed. It is keyed on exactly what the picture depends
// on, so a new sample, a metric switch, a cleared session, an opened take and
// the ticking session clock each invalidate it.
const GRAPH_GEOM = { key: '', points: [], domain: 1, peak: 1, line: '', area: '' };

const BROWSER_MONITOR = {
    mode: localStorage.getItem('bm.mode') || 'resources',
    targetId: localStorage.getItem('bm.target') || '',
    targets: [],
    samples: [],
    latestSample: null,
    events: [],
    handlers: [],
    connected: false,
    recording: false,
    eventCount: 0,
    profileFunctions: [],
    profileActive: false,
    profileWindow: '',
    sessionSecondsValue: 0,
    // Session graph: recording-only samples on a finer grid than the resource
    // history, plus the range the user drags to scope the event list.
    recSamples: [],
    sessionStartMs: 0,
    recIntervalMs: 250,
    samplesTruncated: 0,
    lastSessionId: '',
    recordings: [],
    reviewId: '',
    reviewInfoText: '',
    chartMetric: 'fps',
    hoverRel: null,
    rangeFrom: null,
    rangeTo: null,
    rangeAnchor: null,
    dragging: false,
    intervalMs: 1000,
    error: '',
    busy: false,
    filterKind: 'all',
    filterKinds: ['all', 'scroll', 'input', 'raf', 'longtask', 'fetch', 'json', 'console', 'network', 'navigation', 'error'],
    minDuration: 0,
    duringScrollOnly: false,
    eventSearch: '',
    selectedEvent: null,
    _timer: null,
    _targetsAt: 0,

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
        await this.refreshRecordings();
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
            // Keep the last list: a transient failure (the browser is still
            // starting) should not blank the panel.
            this.error = 'could not read tabs (' + this.errText(e) + ')';
        }
    },

    // The tab list used to be fetched once at mount, so a browser that
    // connected afterwards left the sidebar saying "no tabs" while samples were
    // flowing. Poll the list whenever it is empty or the summary names a tab
    // that is not in it, at most every few seconds.
    maybeRefreshTargets(selected) {
        if (!this.connected) return;
        const now = Date.now();
        const missing = selected && !this.targets.some((t) => t.id === selected);
        // New tabs (the browser tools open them) should show up without the
        // user hunting for the refresh button.
        const stale = !this._targetsAt || now - this._targetsAt > 10000;
        if (!missing && !stale) return;
        this._targetsAt = now;
        this.refreshTargets();
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
            this.maybeRefreshTargets(r.selected);
        } catch (e) {
            this.connected = false;
            this.error = this.errText(e);
        }
    },

    async pollEvents() {
        if (this.reviewId) return;   // a saved take is on screen; live data would overwrite it
        try {
            const r = await simpleApiFetch('/api/ext/browser/perf/events?limit=300');
            this.connected = !!r.connected;
            this.recording = !!r.recording;
            this.sessionSecondsValue = r.session_seconds || 0;
            this.eventCount = r.event_count || 0;
            if (!this.profileWindow) this.profileFunctions = r.functions || [];
            this.profileActive = !!r.profile_active;
            this.events = r.events || [];
            this.handlers = r.handlers || [];
            this.recSamples = r.samples || [];
            this.recIntervalMs = r.sample_interval_ms || 250;
            this.sessionStartMs = r.session_start_ms || 0;
            this.samplesTruncated = r.samples_truncated || 0;
            this.lastSessionId = r.last_session_id || this.lastSessionId;
            this.maybeRefreshTargets(this.targetId);
        } catch (e) {
            this.connected = false;
            this.error = this.errText(e);
        }
    },

    setMode(mode) {
        this.mode = mode === 'events' ? 'events' : 'resources';
        localStorage.setItem('bm.mode', this.mode);
        if (this.mode === 'events') this.refreshRecordings();
        this.poll();
    },

    // ---- saved takes ---------------------------------------------------
    async refreshRecordings() {
        try {
            const r = await simpleApiFetch('/api/ext/browser/perf/recordings');
            this.recordings = r.recordings || [];
        } catch (e) {
            this.error = 'could not list recordings (' + this.errText(e) + ')';
        }
    },

    async openRecording(id) {
        if (!id) return;
        try {
            const r = await simpleApiFetch('/api/ext/browser/perf/recording?id=' + encodeURIComponent(id));
            this.mode = 'events';
            localStorage.setItem('bm.mode', 'events');
            this.reviewId = r.id || id;
            this.reviewInfoText = [r.label, r.started_at_local, r.tab_title || r.tab_url,
                                   (r.session_seconds || 0) + 's'].filter(Boolean).join(' · ');
            this.recording = false;
            this.sessionSecondsValue = r.session_seconds || 0;
            this.eventCount = r.event_count || 0;
            this.events = r.events || [];
            this.handlers = r.handlers || [];
            this.profileFunctions = r.profile || [];
            this.profileActive = false;
            this.recSamples = r.samples || [];
            this.recIntervalMs = r.sample_interval_ms || 250;
            this.sessionStartMs = r.session_start_ms || 0;
            this.samplesTruncated = 0;
            this.lastSessionId = r.id || this.lastSessionId;
            this.selectedEvent = null;
            this.profileWindow = '';
            this.clearRange();
        } catch (e) {
            this.error = 'could not open ' + id + ' (' + this.errText(e) + ')';
        }
    },

    async deleteRecording(id) {
        if (!id) return;
        try {
            await simpleApiPost('/api/ext/browser/perf/recordings/delete', { id });
            if (this.reviewId === id) this.reviewId = '';
            await this.refreshRecordings();
        } catch (e) {
            this.error = 'could not delete ' + id + ' (' + this.errText(e) + ')';
        }
    },

    closeReview() {
        this.reviewId = '';
        this.reviewInfoText = '';
        this.profileWindow = '';
        this.selectedEvent = null;
        this.clearRange();
        this.poll();
    },

    // ---- profile a slice of the take -----------------------------------
    // The CPU profile keeps its raw samples with timestamps, so the range the
    // user dragged over a spike can be attributed on its own: same window for
    // events and functions.
    async profileRange() {
        const sel = this.graphSelection();
        if (!sel) return;
        const query = 'since_ms=' + Math.round(sel.from * 1000) +
                      '&until_ms=' + Math.round(sel.to * 1000) + '&limit=25';
        const url = this.reviewId
            ? '/api/ext/browser/perf/functions?id=' + encodeURIComponent(this.reviewId) + '&' + query
            : '/api/ext/browser/perf/functions?' + query;
        try {
            const r = await simpleApiFetch(url);
            this.profileFunctions = r.functions || [];
            this.profileWindow = sel.from.toFixed(2) + '–' + sel.to.toFixed(2) + ' s';
        } catch (e) {
            this.error = 'could not profile that range (' + this.errText(e) + ')';
        }
    },

    async profileWhole() {
        this.profileWindow = '';
        const url = this.reviewId
            ? '/api/ext/browser/perf/functions?id=' + encodeURIComponent(this.reviewId) + '&limit=25'
            : '/api/ext/browser/perf/functions?limit=25';
        try {
            const r = await simpleApiFetch(url);
            this.profileFunctions = r.functions || [];
        } catch (e) {
            this.error = 'could not read the profile (' + this.errText(e) + ')';
        }
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
            if (r.session_id) this.lastSessionId = r.session_id;
            if (this.recording) {
                this.reviewId = '';
                this.reviewInfoText = '';
                this.clearRange();
            } else {
                this.refreshRecordings();
            }
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
            this.handlers = [];
            this.eventCount = 0;
            this.profileFunctions = [];
            this.profileWindow = '';
            this.profileActive = false;
            this.sessionSecondsValue = 0;
            this.selectedEvent = null;
            this.recSamples = [];
            this.sessionStartMs = 0;
            this.samplesTruncated = 0;
            this.lastSessionId = '';
            this.clearRange();
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
        if (t) return t.title || t.id;
        // The summary can know the selected tab before the list arrives; say so
        // instead of claiming nothing is selected.
        return this.targetId ? 'tab ' + String(this.targetId).slice(-6) : 'no tab selected';
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
        if (l.long_tasks_total > 20) score -= 12;
        else if (l.long_tasks_total > 8) score -= 5;
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
        else if (key === 'long_tasks') { bad = (l.long_tasks_total || 0) > 20; warn = (l.long_tasks_total || 0) > 8; }
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

    // Listener calls are counted in the page (perf.py `M.note`) and folded into
    // session totals server-side, so the panel reads those rows instead of
    // rebuilding them from events that no longer contain one per call.
    topHandlers() {
        return (this.handlers || []).slice(0, 3);
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

    // ---- session graph -------------------------------------------------
    // The recording series is on its own fast grid (perf_rec_interval_ms), and
    // both the line and the long-frame markers are plotted against the session
    // clock (seconds since Record), so a spike lines up with the events that
    // could have caused it.
    graphMetrics() {
        return [
            { key: 'fps', label: 'FPS', unit: 'fps' },
            { key: 'gap_max_ms', label: 'Frame gap', unit: 'ms' },
            { key: 'frames_lost', label: 'Frames lost', unit: '' },
            { key: 'task_pct', label: 'Main thread', unit: '%' },
            { key: 'script_pct', label: 'Script', unit: '%' },
            { key: 'style_per_s', label: 'Style /s', unit: '/s' },
            { key: 'layout_per_s', label: 'Layout /s', unit: '/s' },
            { key: 'style_ms', label: 'Style cost', unit: 'ms' },
            { key: 'layout_ms', label: 'Layout cost', unit: 'ms' },
            { key: 'long_tasks', label: 'Long tasks', unit: '' },
            { key: 'heap', label: 'JS heap', unit: 'bytes' },
            { key: 'nodes', label: 'DOM nodes', unit: '' },
        ];
    },

    metricInfo() {
        return this.graphMetrics().find((m) => m.key === this.chartMetric) || this.graphMetrics()[0];
    },

    setMetric(key) {
        this.chartMetric = key;
    },

    // One pass over the samples per redraw, then cache hits (see GRAPH_GEOM).
    graphGeometry() {
        const n = this.recSamples.length;
        const seconds = this.sessionSeconds();
        const last = n ? Number(this.recSamples[n - 1].t) : 0;
        const key = `${this.sessionStartMs}|${this.chartMetric}|${n}|${last}|${seconds}`;
        if (GRAPH_GEOM.key === key) return GRAPH_GEOM;

        const W = 620;
        const H = 160;
        const points = [];
        let peak = 0;
        let maxRel = 0;
        if (this.sessionStartMs && n) {
            for (let i = 0; i < n; i++) {
                const s = this.recSamples[i];
                const rel = Math.max(0, (Number(s.t) * 1000 - this.sessionStartMs) / 1000);
                const value = Number(s[this.chartMetric]) || 0;
                points.push({ rel, value });
                if (value > peak) peak = value;
                if (rel > maxRel) maxRel = rel;
            }
        }

        const domain = Math.max(1, seconds, maxRel);
        const peakScale = peak > 0 ? peak * 1.06 : 1;
        const parts = [];
        for (let i = 0; i < points.length; i++) {
            const x = Math.round((points[i].rel / domain) * W);
            const y = Math.round(H - 4 - (points[i].value / peakScale) * (H - 12));
            parts.push(`${x},${y}`);
        }
        const line = parts.join(' ');

        GRAPH_GEOM.key = key;
        GRAPH_GEOM.points = points;
        GRAPH_GEOM.domain = domain;
        GRAPH_GEOM.peak = peakScale;
        GRAPH_GEOM.line = line;
        GRAPH_GEOM.area = line ? `0,160 ${line} 620,160` : '';
        return GRAPH_GEOM;
    },

    graphPoints() {
        return this.graphGeometry().points;
    },

    graphDomain() {
        return this.graphGeometry().domain;
    },

    graphPeak() {
        return this.graphGeometry().peak;
    },

    graphLine() {
        return this.graphGeometry().line;
    },

    graphArea() {
        return this.graphGeometry().area;
    },

    graphMarkers() {
        const domain = this.graphDomain();
        return this.events
            .filter((e) => e.kind === 'longtask')
            .map((e) => ({
                rel: Number(e.rel_ms) / 1000,
                pct: Math.min(100, (Number(e.rel_ms) / 1000 / domain) * 100),
                label: `${(Number(e.duration) || 0).toFixed(0)} ms at ${this.fmtRel(e.rel_ms)}`,
            }));
    },

    graphSelection() {
        if (this.rangeFrom === null || this.rangeTo === null) return null;
        const domain = this.graphDomain();
        const a = Math.min(this.rangeFrom, this.rangeTo);
        const b = Math.max(this.rangeFrom, this.rangeTo);
        return { left: (a / domain) * 100, width: Math.max(0.4, ((b - a) / domain) * 100), from: a, to: b };
    },

    graphRelFromEvent(ev) {
        const el = ev.currentTarget || ev.target;
        const rect = el.getBoundingClientRect();
        const frac = Math.min(1, Math.max(0, (ev.clientX - rect.left) / Math.max(1, rect.width)));
        return frac * this.graphDomain();
    },

    hoverGraph(ev) {
        this.hoverRel = this.graphRelFromEvent(ev);
        if (this.dragging && this.rangeAnchor !== null) this.rangeTo = this.hoverRel;
    },

    hoverOut() {
        if (!this.dragging) this.hoverRel = null;
    },

    beginRange(ev) {
        this.dragging = true;
        this.hoverRel = this.graphRelFromEvent(ev);
        this.rangeAnchor = this.hoverRel;
        this.rangeFrom = this.rangeAnchor;
        this.rangeTo = this.rangeAnchor;
    },

    endRange(ev) {
        if (!this.dragging) return;
        this.dragging = false;
        if (ev && ev.currentTarget && ev.currentTarget.getBoundingClientRect) {
            this.hoverRel = this.graphRelFromEvent(ev);
            this.rangeTo = this.hoverRel;
        }
        const a = Math.min(this.rangeFrom, this.rangeTo);
        const b = Math.max(this.rangeFrom, this.rangeTo);
        if (b - a < 0.15) {
            this.clearRange();
        } else {
            this.rangeFrom = a;
            this.rangeTo = b;
        }
        this.rangeAnchor = null;
    },

    clearRange() {
        this.rangeFrom = null;
        this.rangeTo = null;
        this.rangeAnchor = null;
        this.dragging = false;
    },

    rangeLabel() {
        const sel = this.graphSelection();
        if (!sel) return '';
        return `${sel.from.toFixed(2)}–${sel.to.toFixed(2)} s · ${this.filteredEvents().length} events`;
    },

    hoverSampleInfo() {
        if (this.hoverRel === null || !this.recSamples.length) return null;
        let best = null;
        for (const p of this.graphPoints()) {
            if (!best || Math.abs(p.rel - this.hoverRel) < Math.abs(best.rel - this.hoverRel)) best = p;
        }
        if (!best) return null;
        return { rel: best.rel, value: best.value, left: (best.rel / this.graphDomain()) * 100 };
    },

    fmtMetric(value) {
        const m = this.metricInfo();
        const v = Math.round(Number(value) * 10) / 10;
        if (m.unit === 'bytes') return this.fmtMB(value);
        if (m.unit === '%') return v + '%';
        if (m.unit === 'ms') return v + ' ms';
        return m.unit ? v + ' ' + m.unit : String(v);
    },

    axisLabel(seconds) {
        const s = Number(seconds) || 0;
        return s >= 60 ? `${(s / 60).toFixed(1)}m` : `${s.toFixed(1)}s`;
    },

    setFilter(kind) {
        this.filterKind = kind;
    },

    filterLabel(kind) {
        return {
            all: 'All', scroll: 'Scroll', input: 'Input', raf: 'RAF',
            longtask: 'Long tasks', fetch: 'Fetch', json: 'JSON parse (legacy)',
            console: 'Console', network: 'Network', navigation: 'Nav', error: 'Errors',
        }[kind] || kind;
    },

    filterCount(kind) {
        return this.events.filter((e) => kind === 'all' || e.kind === kind).length;
    },

    filteredEvents() {
        const term = String(this.eventSearch || '').trim().toLowerCase();
        const min = Number(this.minDuration || 0);
        const sel = this.graphSelection();
        const lo = sel ? sel.from * 1000 : null;
        const hi = sel ? sel.to * 1000 : null;
        return this.events.filter((e) => {
            if (this.filterKind !== 'all' && e.kind !== this.filterKind) return false;
            if ((Number(e.duration) || 0) < min) return false;
            if (lo !== null && ((Number(e.rel_ms) || 0) < lo || (Number(e.rel_ms) || 0) > hi)) return false;
            if (this.duringScrollOnly && !e.during_scroll) return false;
            if (term) {
                const hay = `${e.kind} ${e.name} ${e.target} ${e.handler?.name || ''}`.toLowerCase();
                if (!hay.includes(term)) return false;
            }
            return true;
        }).sort((a, b) => (Number(a.rel_ms) || 0) - (Number(b.rel_ms) || 0));
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

    // Bursts (scroll, pointer, network) are folded per window server-side; the
    // row says how many ran without turning into one row each.
    eventName(event) {
        const n = Number(event && event.data && event.data.count) || 1;
        return (event && event.name ? event.name : '') + (n > 1 ? ' ×' + n : '');
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
