"""Browser performance monitor: resource sampling and event recording.

Resources: Chrome's Performance.getMetrics snapshot, diffed per selected tab,
plus a tiny injected rAF/long-task probe for FPS and longest task.
Events: a recording session that combines CDP events (console/network/etc.)
with an injected page agent that records scroll, input handler invocations,
rAF callbacks and long tasks. All data stays in memory.
"""

import asyncio
import json
import time
from collections import deque

import core

AGENT_JS = r"""
(() => {
  if (window.__olMonitor) return "exists";
  const M = window.__olMonitor = {
    enabled: true, installed: false, events: [], agg: {}, cap: 1200, slowMs: 8,
    sessionStart: performance.now(), scrollAt: -1e9, seq: 0,
    // `at` is the entry's own startTime (performance timeline, same origin as
    // performance.now()). A PerformanceObserver delivers entries in batches
    // after the fact, so stamping at drain time collapses a burst onto one
    // instant and puts LoAF entries a frame late. Clamped at 0 for a frame that
    // was already running when recording started.
    push(e, at) {
      if (!this.enabled) return;
      const base = (at === undefined || at === null) ? performance.now() : at;
      this.seq += 1;
      e.id = 'e' + this.seq;
      e.rel_ms = Math.max(0, Math.round((base - this.sessionStart) * 10) / 10);
      e.wall_ms = Date.now();
      e.during_scroll = !!e.during_scroll || (base - this.scrollAt < 200);
      e.severity = e.severity || (Number(e.duration) > 50 ? 'warn' : 'info');
      e.tab = this.tab || null;
      this.events.push(e);
      if (this.events.length > this.cap) this.events.shift();
    },
    // Listener calls are counted here, in the page: a scroll spy fires dozens
    // of times per second at a fraction of a millisecond each, and shipping
    // every call over CDP drowns the timeline in events that carry no signal.
    // One row per (handler, event type, registration site); an individual event
    // is kept only when a call is actually slow.
    note(type, el, listener, site, dur, startedAt) {
      if (!this.enabled) return;
      const now = startedAt === undefined || startedAt === null ? performance.now() : startedAt;
      const name = listener.name || 'anonymous';
      const key = type + '|' + name + '|' + ((site && site[0]) || '');
      let row = this.agg[key];
      if (!row) {
        row = this.agg[key] = {
          kind: 'input', name: type, calls: 0, ms: 0, max_ms: 0, scroll_calls: 0,
          first_ms: 0, last_ms: 0, target: describe(el),
          handler: {name: name, site: site, source: String(listener).slice(0, 300)},
        };
      }
      if (!row.calls) row.first_ms = Math.round((now - this.sessionStart) * 10) / 10;
      row.calls += 1;
      row.ms += dur;
      row.max_ms = Math.max(row.max_ms, dur);
      if (now - this.scrollAt < 200) row.scroll_calls += 1;
      row.last_ms = Math.round((now - this.sessionStart) * 10) / 10;
      if (dur < this.slowMs) return;
      let stack = null;
      try { stack = String(new Error().stack).split('\n').slice(2, 6).map((s) => s.trim()); }
      catch (e) {}
      this.push({kind: 'input', name: type, duration: dur, target: describe(el),
                 handler: {name: name, source: String(listener).slice(0, 300),
                           stack: stack, site: site},
                 data: {}, during_scroll: now - this.scrollAt < 200}, now);
    },
    // A json() call that blocks is invisible in a CPU profile (parsing is
    // native) yet shows up in LoAF as work on the awaiting script. Recording it
    // names the endpoint, the payload size and the calling stack, which is what
    // turns "45 ms in Response.json.then" into a line of code.
    jsonNote(url, status, bytes, count, dur, stack, at) {
      if (!this.enabled) return;
      this.push({
        kind: 'json',
        name: url || 'response',
        duration: dur,
        target: status ? 'HTTP ' + status : 'response',
        handler: {name: 'json parse', source: '', stack: stack || [],
                  site: stack ? stack.slice(0, 2) : null},
        data: {bytes: bytes, count: count, status: status},
        severity: dur >= 16 ? 'warn' : 'info',
      }, at);
    },
    // A long task has no render phases; a long animation frame does. Splitting
    // it into work / render / style+layout is what says whether a frame was JS
    // or rendering - renderStart and styleAndLayoutStart exist only on LoAF.
    frameData(e) {
      const ms = (v) => Math.round((v || 0) * 10) / 10;
      const end = (e.startTime || 0) + (e.duration || 0);
      const renderStart = e.renderStart || 0;
      const slStart = e.styleAndLayoutStart || 0;
      const scripts = (e.scripts || []).slice(0, 12).map((s) => ({
        name: s.sourceFunctionName || s.invoker || 'anonymous',
        url: s.sourceURL || '',
        position: s.sourceCharPosition || 0,
        invoker: s.invoker || '',
        invokerType: s.invokerType || '',
        forced_ms: ms(s.forcedStyleAndLayoutDuration),
        duration: ms(s.duration),
      }));
      return {
        scripts: scripts,
        entryType: e.entryType || '',
        work_ms: renderStart ? ms(renderStart - (e.startTime || 0)) : ms(e.duration),
        render_ms: renderStart ? ms(end - renderStart) : 0,
        pre_layout_ms: (renderStart && slStart) ? ms(slStart - renderStart) : 0,
        style_layout_ms: slStart ? ms(end - slStart) : 0,
        blocking_ms: ms(e.blockingDuration),
        paint_ms: e.paintTime ? ms(e.paintTime - (e.startTime || 0)) : 0,
        first_ui_ms: e.firstUIEventTimestamp
          ? ms(e.firstUIEventTimestamp - (e.startTime || 0)) : 0,
      };
    },
    start() {
      this.enabled = true;
      this.sessionStart = performance.now();
      this.events = [];
      this.agg = {};
    },
    stop() { this.enabled = false; },
    drain() {
      const out = this.events;
      const handlers = Object.keys(this.agg).map((k) => this.agg[k]);
      this.events = [];
      this.agg = {};
      return {events: out, handlers: handlers};
    },
  };

  const describe = (el) => {
    if (el === window) return 'window';
    if (el === document) return 'document';
    if (!el || !el.tagName) return String(el);
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    else if (el.classList && el.classList.length) s += '.' + [...el.classList].slice(0, 2).join('.');
    return s;
  };

  try {
    new PerformanceObserver((list) => list.getEntries().forEach((e) => M.push({
      kind: 'longtask', name: e.name || e.entryType || 'longtask',
      duration: e.duration, target: 'main thread', handler: null,
      data: M.frameData(e), severity: e.duration > 50 ? 'warn' : 'info',
    }, e.startTime))).observe({entryTypes: ['longtask', 'long-animation-frame']});
  } catch (e) {}

  try {
    new PerformanceObserver((list) => list.getEntries().forEach((e) =>
      M.push({kind: 'input', name: e.name, duration: e.duration,
              target: 'event timing', handler: null,
              data: {interactionId: e.interactionId, processingStart: e.processingStart,
                     processingEnd: e.processingEnd,
                     delay_ms: Math.max(0, Math.round(((e.processingStart || 0) - (e.startTime || 0)) * 10) / 10)}},
             e.startTime)
    )).observe({entryTypes: ['event']});
  } catch (e) {}

  let scrollThrottle = 0;
  window.addEventListener('scroll', () => {
    const now = performance.now();
    if (now - scrollThrottle < 40) return;
    scrollThrottle = now;
    M.scrollAt = now;
    M.push({kind: 'scroll', name: 'scroll', duration: 0, target: 'document',
            handler: null, data: {scrollY: window.scrollY, scrollX: window.scrollX},
            during_scroll: true});
  }, {passive: true, capture: true});

  if (!M.installed) {
    M.installed = true;
    const origAdd = EventTarget.prototype.addEventListener;
    const origRemove = EventTarget.prototype.removeEventListener;
    // listener -> {type: wrapper}. One function can be registered for several
    // event types, so the wrapper has to remember the type it was made for.
    const wrapped = new WeakMap();
    const types = new Set(['scroll', 'wheel', 'pointerdown', 'pointermove', 'pointerup', 'keydown', 'click', 'resize']);

    EventTarget.prototype.addEventListener = function(type, listener, options) {
      if (!types.has(type) || typeof listener !== 'function') {
        return origAdd.call(this, type, listener, options);
      }
      let byType = wrapped.get(listener);
      if (!byType) {
        byType = {};
        wrapped.set(listener, byType);
      }
      let w = byType[type];
      if (!w) {
        // Captured once, at registration: the invocation stack says who fired
        // the handler, this says which code registered it - the only thing
        // that identifies an anonymous listener.
        let site = null;
        try {
          site = String(new Error().stack).split('\n').slice(2, 5).map((s) => s.trim()).filter(Boolean);
        } catch (e) {}
        w = function(ev) {
          if (!M.enabled) return listener.call(this, ev);
          const t0 = performance.now();
          try { return listener.call(this, ev); }
          finally { M.note(type, this, listener, site, performance.now() - t0, t0); }
        };
        w.__olOriginal = listener;
        byType[type] = w;
      }
      return origAdd.call(this, type, w, options);
    };

    EventTarget.prototype.removeEventListener = function(type, listener, options) {
      const byType = wrapped.get(listener);
      return origRemove.call(this, type, (byType && byType[type]) || listener, options);
    };

    const origRaf = window.requestAnimationFrame;
    window.requestAnimationFrame = function(cb) {
      return origRaf.call(window, function(t) {
        if (!M.enabled) return cb(t);
        const t0 = performance.now();
        try { return cb(t); }
        finally {
          const dur = performance.now() - t0;
          if (dur > 8) M.push({kind: 'raf', name: 'requestAnimationFrame', duration: dur,
                               target: 'frame', handler: null, data: {},
                               during_scroll: performance.now() - M.scrollAt < 200});
        }
      });
    };
    // Native JSON parsing blocks the main thread but never shows up in a CPU
    // profile, and LoAF only names "the awaiting script". Timing json() and
    // keeping the fetch call stack turns that into an endpoint, a payload size
    // and the function that asked for it.
    const origFetch = window.fetch;
    const origJson = Response.prototype.json;
    if (origFetch && origJson) {
      window.fetch = function(input, init) {
        let stack = null;
        if (M.enabled) {
          try { stack = String(new Error().stack).split('\n').slice(2, 6).map((s) => s.trim()); }
          catch (e) {}
        }
        const p = origFetch.apply(this, arguments);
        if (!stack) return p;
        return p.then((res) => {
          try { res.__olCaller = stack; } catch (e) {}
          return res;
        });
      };
      const origin = String((typeof location !== 'undefined' && location.origin) || '');
      Response.prototype.json = function() {
        const started = performance.now();
        const response = this;
        const p = origJson.apply(this, arguments);
        if (!M.enabled) return p;
        return p.then((data) => {
          try {
            const url = String((response && response.url) || '').replace(origin, '');
            // The monitor polls its own extension API every second; those
            // responses are ours and would only add noise.
            if (!url.includes('/api/ext/browser/perf/')) {
              let bytes = null;
              try { bytes = Number(response.headers.get('content-length')) || null; } catch (e) {}
              // The API wraps its payload: {success, data:{turns:…}}.
              const body = data && typeof data === 'object' && data.data !== undefined ? data.data : data;
              const count = body && Array.isArray(body.turns) ? body.turns.length
                : (Array.isArray(body) ? body.length : null);
              M.jsonNote(url, response && response.status, bytes, count,
                         performance.now() - started, response.__olCaller || null, started);
            }
          } catch (e) {}
          return data;
        });
      };
    }
    M.restore = () => {
      EventTarget.prototype.addEventListener = origAdd;
      EventTarget.prototype.removeEventListener = origRemove;
      window.requestAnimationFrame = origRaf;
      if (origFetch && origJson) {
        window.fetch = origFetch;
        Response.prototype.json = origJson;
      }
    };
  }
  return "installed";
})()
"""

AGENT_START_JS = "window.__olMonitor && window.__olMonitor.start(); 'ok'"

# The agent installed at document start is identical except that it idles until
# a recording session arms it: resource sampling is always on, so a page that is
# only being sampled must not accumulate events in memory. Installing at
# document start is the point of it - the addEventListener/rAF wrappers must
# exist before the page's own scripts register their handlers, otherwise
# listeners from the first page load never appear in top_handlers.
AGENT_DOC_JS = AGENT_JS.replace("enabled: true, installed: false",
                                "enabled: false, installed: false")
if AGENT_DOC_JS == AGENT_JS:
    # The variants are derived from one source. If that literal is ever
    # reformatted, the document-start copy silently turns into the always-on
    # agent (every page would queue events while merely being sampled), so
    # fail here instead of degrading quietly.
    raise RuntimeError(
        "perf.py: the 'enabled: true, installed: false' literal in AGENT_JS is "
        "gone, so AGENT_DOC_JS is not the idle variant")


def _agent_arm_js(elapsed_ms):
    """Arm the idle document-start agent, keeping the session clock continuous.

    A document created mid-recording (a navigation) starts its `sessionStart` at
    its own load time, so its events would report `rel_ms` near 0 and be placed
    at the beginning of the timeline. Shifting `sessionStart` by the difference
    pins them where the session actually is. An agent that is already armed is
    left alone, so repeated calls during one recording do not drift the clock.
    """
    return (
        "(() => { const M = window.__olMonitor;"
        " if (!M) return 'missing';"
        " if (M.enabled) return 'armed';"
        f" M.sessionStart -= Math.max(0, {int(elapsed_ms)} - (performance.now() - M.sessionStart));"
        " M.enabled = true; return 'armed'; })()"
    )


PROBE_JS = r"""
(() => {
  const now = performance.now();
  if (!window.__olPerf) {
    const s = {frames: 0, gapMax: 0, lastCb: now, long: 0, longTotal: 0,
               longest: 0, lastRead: now};
    window.__olPerf = s;
    try {
      new PerformanceObserver((list) => {
        for (const e of list.getEntries()) {
          s.long += 1;
          s.longTotal += 1;
          s.longest = Math.max(s.longest, e.duration || 0);
        }
      }).observe({entryTypes: ['longtask']});
    } catch (e) {}
    const tick = (t) => {
      s.frames += 1;
      const gap = t - s.lastCb;
      if (gap > s.gapMax) s.gapMax = gap;
      s.lastCb = t;
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }
  // Read and reset: the counters describe the window since the previous read,
  // so a 250 ms bucket can show a single 88 ms frame instead of an average.
  const st = window.__olPerf;
  const window_ms = now - st.lastRead;
  const out = {
    window_ms: Math.round(window_ms * 10) / 10,
    frames: st.frames,
    gap_max_ms: Math.round(st.gapMax * 10) / 10,
    long: st.long,
    long_total: st.longTotal,
    longest: Math.round(st.longest),
  };
  st.frames = 0;
  st.gapMax = 0;
  st.long = 0;
  st.lastRead = now;
  return JSON.stringify(out);
})()
"""


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_metrics(payload):
    return {item["name"]: _number(item.get("value"))
            for item in (payload or {}).get("metrics", []) or []
            if isinstance(item, dict) and item.get("name")}


def _rate(prev, cur, key, dt):
    if not prev or dt <= 0:
        return 0.0
    return max(_number(cur.get(key)) - _number(prev.get(key)), 0.0) / dt


def build_sample(prev, cur, dt, probe=None):
    probe = probe or {}
    window_ms = _number(probe.get("window_ms"))
    frames = _number(probe.get("frames"))
    fps = round(frames * 1000.0 / window_ms) if window_ms > 0 else 0
    # How many frames a steady 60 Hz display would have delivered in the window:
    # the difference is what the user feels as jitter. `gap_max_ms` is the sharp
    # one - a single 88 ms hitch shows up in exactly one bucket.
    expected = window_ms / 16.7 if window_ms > 0 else 0.0
    return {
        "t": round(time.time(), 3),
        "heap": int(_number(cur.get("JSHeapUsedSize"))),
        "heap_total": int(_number(cur.get("JSHeapTotalSize"))),
        "nodes": int(_number(cur.get("Nodes"))),
        "documents": int(_number(cur.get("Documents"))),
        "frames": int(_number(cur.get("Frames"))),
        "listeners": int(_number(cur.get("JSEventListeners"))),
        "layout_per_s": round(_rate(prev, cur, "LayoutCount", dt), 2),
        "style_per_s": round(_rate(prev, cur, "RecalcStyleCount", dt), 2),
        # Time actually spent, not just how often: a count of recalcs says
        # nothing about cost, these are main-thread ms per second.
        "layout_ms": round(1000 * _rate(prev, cur, "LayoutDuration", dt), 1),
        "style_ms": round(1000 * _rate(prev, cur, "RecalcStyleDuration", dt), 1),
        "script_pct": round(100 * _rate(prev, cur, "ScriptDuration", dt), 2),
        "task_pct": round(100 * _rate(prev, cur, "TaskDuration", dt), 2),
        "task_other_pct": round(100 * _rate(prev, cur, "TaskOtherDuration", dt), 2),
        "fps": int(max(0, min(fps, 240))),
        "gap_max_ms": round(_number(probe.get("gap_max_ms")), 1),
        "frames_lost": int(max(0, round(expected - frames))),
        "long_tasks": int(_number(probe.get("long"))),
        "long_tasks_total": int(_number(probe.get("long_total"))),
        "longest_task": int(_number(probe.get("longest"))),
    }


def _cdp_kind(method):
    if method in ("Runtime.consoleAPICalled", "Log.entryAdded"):
        return "console"
    if method in ("Runtime.exceptionThrown", "Network.loadingFailed"):
        return "error"
    if method in ("Network.requestWillBeSent", "Network.responseReceived", "Network.loadingFinished"):
        return "network"
    if method in ("Page.frameNavigated", "Target.targetCreated", "Target.targetDestroyed", "Target.targetCrashed"):
        return "navigation"
    return "input"


class PerfMonitor:
    def perf_init(self):
        old = getattr(self, "_perf_task", None)
        if old:
            old.cancel()
        self._perf_selected_target = None
        self._perf_samples = {}
        self._perf_last_metrics = {}
        self._perf_history = max(int(self.config.get("perf_history", 300) or 300), 30)
        self._perf_interval = max(int(self.config.get("perf_interval_ms", 1000) or 1000), 250) / 1000.0
        # Recording samples on their own cadence and their own series: the fine
        # grid is what lets the graph put a spike in one bucket, and keeping it
        # separate stops a 250 ms recording from shrinking the 5-minute
        # resource history to 75 seconds.
        self._perf_rec_interval = max(int(self.config.get("perf_rec_interval_ms", 250) or 250), 100) / 1000.0
        self._perf_rec_history = max(int(self.config.get("perf_rec_history", 4000) or 4000), 100)
        self._perf_rec_samples = {}
        self._perf_rec_last = {}
        self._perf_probe_acc = {}
        self._perf_recording = False
        self._perf_session_start = 0
        self._perf_session_elapsed = 0.0
        self._perf_event_log = deque(maxlen=max(int(self.config.get("perf_events_max", 5000) or 5000), 100))
        self._perf_event_seq = 0
        self._perf_handler_stats = {}
        self._perf_profile = []
        self._perf_profile_meta = {}
        self._perf_profile_active = False
        self._perf_profile_sid = None
        self._perf_doc_agents = {}
        self._perf_stop = asyncio.Event()
        self._perf_task = asyncio.create_task(self._perf_loop())

    async def perf_close(self):
        self._perf_stop.set()
        task = getattr(self, "_perf_task", None)
        self._perf_task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def perf_reset(self):
        self._perf_selected_target = None
        self._perf_recording = False
        self._perf_session_elapsed = 0.0
        self._perf_handler_stats = {}
        self._perf_rec_samples = {}
        self._perf_rec_last = {}
        self._perf_probe_acc = {}
        self._perf_profile = []
        self._perf_profile_meta = {}
        self._perf_profile_active = False
        self._perf_profile_sid = None
        self._perf_doc_agents = {}
        for name in ("_perf_samples", "_perf_last_metrics", "_perf_event_log"):
            bucket = getattr(self, name, None)
            if bucket is not None and hasattr(bucket, "clear"):
                bucket.clear()

    async def _perf_loop(self):
        while not self._perf_stop.is_set():
            try:
                await self._perf_tick()
                if self._perf_recording:
                    await self._perf_drain_events()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log("browser", f"perf monitor: {core_detail_error(e)}")
            interval = self._perf_rec_interval if self._perf_recording else self._perf_interval
            try:
                await asyncio.wait_for(self._perf_stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _perf_tick(self):
        target = self._perf_selected_target
        if not target or not self._is_connected():
            return
        if target not in self._attached:
            try:
                await self._attach_target(target)
            except Exception:
                self._perf_selected_target = None
                return
        sid = self._attached.get(target)
        if not sid:
            return
        raw = await self._cdp.cmd("Performance.getMetrics", {}, sid, timeout=2.0)
        cur = parse_metrics(raw)
        probe = None
        if self.config.get("perf_collect_fps", True):
            try:
                res = await self._cdp.cmd("Runtime.evaluate",
                                          {"expression": PROBE_JS, "returnByValue": True},
                                          sid, timeout=2.0)
                value = ((res or {}).get("result") or {}).get("value")
                probe = json.loads(value) if value else None
            except Exception:
                probe = None
        now = time.monotonic()
        # Recording series: one sample per fast tick, so a spike lands in its own
        # bucket instead of being averaged away inside a one-second one.
        if self._perf_recording:
            prev_rec = self._perf_rec_last.get(target)
            if prev_rec is not None and now > prev_rec[0]:
                bucket = self._perf_rec_samples.get(target)
                if bucket is None:
                    bucket = deque(maxlen=self._perf_rec_history)
                    self._perf_rec_samples[target] = bucket
                bucket.append(build_sample(prev_rec[1], cur, now - prev_rec[0], probe))
            self._perf_rec_last[target] = (now, cur)
        # The probe is read once per tick and reset, so its counters describe the
        # window since the previous read. Ticks that feed the always-on series
        # are spaced wider, so their probe figures are accumulated here to keep
        # that series honest at its own cadence.
        acc = self._perf_probe_acc.get(target)
        if probe:
            if acc is None:
                acc = self._perf_probe_acc[target] = {
                    "window_ms": 0.0, "frames": 0, "gap_max_ms": 0.0,
                    "long": 0, "long_total": 0, "longest": 0,
                }
            acc["window_ms"] += _number(probe.get("window_ms"))
            acc["frames"] += int(_number(probe.get("frames")))
            acc["gap_max_ms"] = max(acc["gap_max_ms"], _number(probe.get("gap_max_ms")))
            acc["long"] += int(_number(probe.get("long")))
            acc["long_total"] = int(_number(probe.get("long_total")))
            acc["longest"] = max(acc["longest"], _number(probe.get("longest")))
        prev = self._perf_last_metrics.get(target)
        if prev is None:
            self._perf_last_metrics[target] = (now, cur)
        elif now - prev[0] >= self._perf_interval * 0.9:
            bucket = self._perf_samples.get(target)
            if bucket is None:
                bucket = deque(maxlen=self._perf_history)
                self._perf_samples[target] = bucket
            bucket.append(build_sample(prev[1], cur, now - prev[0], acc))
            self._perf_last_metrics[target] = (now, cur)
            if acc is not None:
                acc.update({"window_ms": 0.0, "frames": 0, "gap_max_ms": 0.0,
                            "long": 0, "longest": 0})

    async def _perf_agent_start(self):
        target = self._perf_selected_target
        if not target or not self._is_connected():
            return
        if target not in self._attached:
            try:
                await self._attach_target(target)
            except Exception:
                return
        sid = self._attached.get(target)
        if not sid:
            return
        try:
            await self._cdp.cmd("Runtime.evaluate",
                                {"expression": AGENT_JS, "returnByValue": True},
                                sid, timeout=2.0)
            await self._cdp.cmd("Runtime.evaluate",
                                {"expression": AGENT_START_JS, "returnByValue": True},
                                sid, timeout=2.0)
        except Exception:
            pass

    async def _perf_install_doc_agent(self, target_id, session_id):
        """Register AGENT_DOC_JS to run at document start for one tab.

        Called from _attach_target, so it covers every tab the module attaches,
        not just the monitor's selection. Registration is per CDP session
        (session ids die with the connection), hence the id comparison to keep
        it idempotent. Tabs that were already attached before this ran keep the
        Runtime.evaluate path, which installs the agent in the live document.
        """
        if not target_id or not session_id:
            return
        installed = getattr(self, "_perf_doc_agents", None)
        if not isinstance(installed, dict):
            installed = self._perf_doc_agents = {}
        if installed.get(target_id) == session_id:
            return
        await self._cdp.cmd("Page.addScriptToEvaluateOnNewDocument",
                            {"source": AGENT_DOC_JS},
                            session_id, timeout=2.0)
        installed[target_id] = session_id

    async def _perf_profile_start(self):
        target = self._perf_selected_target
        if not target or not self._is_connected():
            return
        if target not in self._attached:
            try:
                await self._attach_target(target)
            except Exception:
                return
        sid = self._attached.get(target)
        if not sid:
            return
        try:
            await self._cdp.cmd("Profiler.enable", {}, sid, timeout=2.0)
            await self._cdp.cmd("Profiler.setSamplingInterval", {"interval": 1000}, sid, timeout=2.0)
            await self._cdp.cmd("Profiler.start", {}, sid, timeout=2.0)
            self._perf_profile_active = True
            self._perf_profile_sid = sid
        except Exception:
            self._perf_profile_active = False
            self._perf_profile_sid = None

    async def _perf_profile_stop(self):
        if not self._perf_profile_active:
            return
        sid = self._perf_profile_sid
        self._perf_profile_active = False
        self._perf_profile_sid = None
        if not sid:
            return
        try:
            res = await self._cdp.cmd("Profiler.stop", {}, sid, timeout=5.0)
            profile = (res or {}).get("profile") or {}
            self._perf_profile = self._perf_profile_parse(profile)
        except Exception:
            self._perf_profile = []
            self._perf_profile_meta = {}

    def _perf_profile_parse(self, profile):
        nodes = {n["id"]: n for n in (profile.get("nodes") or [])
                 if isinstance(n, dict) and n.get("id") is not None}
        samples = profile.get("samples") or []
        deltas = profile.get("timeDeltas") or []
        self_ms = {}
        counts = {}
        for i, node_id in enumerate(samples):
            dt = _number(deltas[i]) if i < len(deltas) else 0
            self_ms[node_id] = self_ms.get(node_id, 0.0) + max(dt, 0) / 1000.0
            counts[node_id] = counts.get(node_id, 0) + 1

        parent = {}
        for node_id, node in nodes.items():
            for child in node.get("children") or []:
                parent[child] = node_id

        total = dict(self_ms)
        for node_id, value in list(self_ms.items()):
            cur = node_id
            seen = set()
            while cur in parent and cur not in seen:
                seen.add(cur)
                cur = parent[cur]
                total[cur] = total.get(cur, 0.0) + value

        groups = {}
        # V8 names the two frames that are not code under test: `(idle)` is the
        # renderer waiting for work, `(program)` its own runtime. They dominate
        # by construction, so they are not functions to attribute - counted
        # here and reported separately as overhead instead of as rows.
        noise_ms = {"(idle)": 0.0, "(program)": 0.0}
        for node_id, node in nodes.items():
            frame = node.get("callFrame") or {}
            name = frame.get("functionName") or "(anonymous)"
            if name == "(root)":
                continue
            if name in noise_ms:
                noise_ms[name] += self_ms.get(node_id, 0.0)
                continue
            url = frame.get("url") or ""
            line = int(frame.get("lineNumber") or 0) + 1
            key = (name, url, line)
            row = groups.get(key)
            if row is None:
                row = {
                    "name": name,
                    "source": f"{url}:{line}" if url else "",
                    "url": url,
                    "line": line,
                    "self_ms": 0.0,
                    "total_ms": 0.0,
                    "calls": 0,
                }
                groups[key] = row
            row["self_ms"] += self_ms.get(node_id, 0.0)
            row["total_ms"] += total.get(node_id, 0.0)
            row["calls"] += counts.get(node_id, 0)

        rows = [r for r in groups.values() if r["self_ms"] > 0 or r["total_ms"] > 0]
        rows.sort(key=lambda r: (r["total_ms"], r["self_ms"]), reverse=True)
        result = []
        for row in rows[:30]:
            row["self_ms"] = round(row["self_ms"], 1)
            row["total_ms"] = round(row["total_ms"], 1)
            result.append(row)
        self._perf_profile_meta = {
            "idle_ms": round(noise_ms["(idle)"], 1),
            "program_ms": round(noise_ms["(program)"], 1),
            "functions": len(rows),
            "returned": len(result),
        }
        return result

    async def _perf_drain_events(self):
        target = self._perf_selected_target
        if not target or not self._is_connected():
            return
        if target not in self._attached:
            try:
                await self._attach_target(target)
            except Exception:
                return
        sid = self._attached.get(target)
        if not sid:
            return
        try:
            await self._cdp.cmd("Runtime.evaluate", {"expression": AGENT_JS, "returnByValue": True}, sid, timeout=2.0)
            if self._perf_recording:
                # A document created mid-recording (navigation) starts with the
                # idle document-start agent: arm it without dropping what it has
                # already queued, and keep its clock on the session's timeline.
                await self._cdp.cmd(
                    "Runtime.evaluate",
                    {"expression": _agent_arm_js(self._perf_session_seconds() * 1000),
                     "returnByValue": True},
                    sid, timeout=2.0)
            res = await self._cdp.cmd(
                "Runtime.evaluate",
                {"expression": "window.__olMonitor ? JSON.stringify(window.__olMonitor.drain()) : ''",
                 "returnByValue": True},
                sid, timeout=2.0,
            )
            value = ((res or {}).get("result") or {}).get("value")
            payload = json.loads(value) if value else {}
            if isinstance(payload, list):
                # A page still running the previous agent drains a bare list.
                payload = {"events": payload, "handlers": []}
            for item in payload.get("events") or []:
                self._perf_append_event(item)
            for row in payload.get("handlers") or []:
                self._perf_merge_handler(row)
        except Exception:
            pass

    def _perf_append_event(self, item):
        if not self._perf_recording:
            return
        self._perf_event_seq += 1
        item = dict(item or {})
        item.setdefault("id", f"e{self._perf_event_seq}")
        item["tab"] = self._perf_selected_target
        item["severity"] = item.get("severity") or (
            "warn" if _number(item.get("duration")) > 50 else "info")
        handler = item.get("handler")
        if not isinstance(handler, dict):
            handler = {"name": "—", "source": "", "stack": []}
        item["handler"] = handler
        item.setdefault("data", {})
        item.setdefault("during_scroll", False)
        item["rel_ms"] = _number(item.get("rel_ms"))
        item["wall_ms"] = int(item.get("wall_ms") or time.time() * 1000)
        self._perf_event_log.append(item)

    def _perf_merge_handler(self, row):
        """Fold one drained handler aggregate into the session totals.

        The page keeps the same (handler, event type, site) row for its whole
        drain window; sums are accumulated here so counts stay correct across
        drains without keeping an event per call.
        """
        if not self._perf_recording or not isinstance(row, dict):
            return
        handler = row.get("handler")
        if not isinstance(handler, dict):
            handler = {}
        name = handler.get("name") or "anonymous"
        site = handler.get("site")
        site = site if isinstance(site, list) else []
        key = (row.get("name") or "", name, site[0] if site else "")
        cur = self._perf_handler_stats.get(key)
        calls = int(_number(row.get("calls")))
        if cur is None:
            cur = self._perf_handler_stats[key] = {
                "type": row.get("name") or "",
                "name": name,
                "site": site,
                "source": handler.get("source") or "",
                "target": row.get("target") or "",
                "calls": 0,
                "ms": 0.0,
                "max_ms": 0.0,
                "scroll_calls": 0,
                "first_ms": None,
                "last_ms": None,
            }
        cur["calls"] += calls
        cur["ms"] += _number(row.get("ms"))
        cur["max_ms"] = max(cur["max_ms"], _number(row.get("max_ms")))
        cur["scroll_calls"] += int(_number(row.get("scroll_calls")))
        first = row.get("first_ms")
        last = row.get("last_ms")
        if first is not None:
            first = _number(first)
            cur["first_ms"] = first if cur["first_ms"] is None else min(cur["first_ms"], first)
        if last is not None:
            last = _number(last)
            cur["last_ms"] = last if cur["last_ms"] is None else max(cur["last_ms"], last)

    def _perf_record_event(self, method, sid, params):
        if not self._perf_recording or self._perf_session_start <= 0:
            return
        params = params or {}
        kind = _cdp_kind(method)
        text = ""
        target = self._target_of_session(sid) or ""
        data = {}
        if method == "Runtime.consoleAPICalled":
            text = " ".join(str(a.get("value", a.get("description", "")))
                            for a in params.get("args", []) or [])
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails", {}) or {}
            text = str(details.get("exception", {}).get("description")
                       or details.get("text", "exception"))
        elif method == "Log.entryAdded":
            entry = params.get("entry", {}) or {}
            text = entry.get("text", "")
            target = entry.get("url") or target
        elif method == "Network.requestWillBeSent":
            req = params.get("request", {}) or {}
            text = f"{req.get('method', '?')} {req.get('url', '')}"
            target = req.get("url", "")
        elif method == "Network.responseReceived":
            resp = params.get("response", {}) or {}
            text = f"{resp.get('status')} {resp.get('url', '')}"
            target = resp.get("url", "")
        elif method == "Network.loadingFailed":
            text = params.get("errorText", "request failed")
        elif method == "Page.frameNavigated":
            frame = params.get("frame", {}) or {}
            text = f"navigated to {frame.get('url', '')}"
            target = frame.get("url", "")
        elif method.startswith("Target."):
            info = params.get("targetInfo", {}) or {}
            text = f"{method.split('.')[-1]} {info.get('url', '')}".strip()
            target = info.get("url", "")
        self._perf_append_event({
            "kind": kind,
            "name": text or method,
            "duration": 0,
            "target": target or "document",
            "handler": {"name": "—", "source": "", "stack": []},
            "data": data,
            "severity": "error" if kind == "error" else "info",
            "rel_ms": max(0, time.time() * 1000 - self._perf_session_start),
            "wall_ms": int(time.time() * 1000),
        })

    async def _perf_targets(self):
        if not self._is_connected():
            return {"connected": False, "selected": self._perf_selected_target, "targets": []}
        from . import page
        targets = await page.list_page_targets(self._cdp)
        return {"connected": True, "selected": self._perf_selected_target,
                "targets": await self._tab_entries(targets)}

    async def _perf_select(self, target_id):
        target_id = str(target_id or "").strip()
        if not target_id:
            self._perf_selected_target = None
            return {"selected": None}
        if not self._is_connected():
            raise RuntimeError("Browser is not connected.")
        from . import page
        targets = await page.list_page_targets(self._cdp)
        if target_id not in {t.get("targetId") for t in targets}:
            raise RuntimeError("That tab is gone. Refresh the tab list.")
        await self._attach_target(target_id)
        self._perf_selected_target = target_id
        self._perf_last_metrics.pop(target_id, None)
        self._perf_rec_last.pop(target_id, None)
        self._perf_probe_acc.pop(target_id, None)
        self._perf_samples[target_id] = deque(maxlen=self._perf_history)
        if self._perf_recording:
            await self._perf_profile_stop()
            await self._perf_agent_start()
            await self._perf_profile_start()
        return {"selected": target_id}

    def _perf_session_seconds(self):
        if self._perf_session_start <= 0:
            return round(max(self._perf_session_elapsed, 0), 1)
        if not self._perf_recording:
            return round(max(self._perf_session_elapsed, 0), 1)
        return max(0, round((time.time() * 1000 - self._perf_session_start) / 1000, 1))

    async def _perf_summary(self, target_id=None, limit=180):
        target_id = target_id or self._perf_selected_target
        samples = list(self._perf_samples.get(target_id, ()))
        if limit:
            samples = samples[-max(int(limit), 1):]
        return {
            "connected": self._is_connected(),
            "selected": self._perf_selected_target,
            "target_id": target_id,
            "interval_ms": int(self._perf_interval * 1000),
            "history": self._perf_history,
            "samples": samples,
            "latest": samples[-1] if samples else None,
            "recording": self._perf_recording,
            "session_seconds": self._perf_session_seconds(),
            "event_count": len(self._perf_event_log),
        }

    async def _perf_clear(self, target_id=None):
        target_id = target_id or self._perf_selected_target
        if target_id:
            self._perf_samples.pop(target_id, None)
            self._perf_last_metrics.pop(target_id, None)
            self._perf_rec_samples.pop(target_id, None)
            self._perf_rec_last.pop(target_id, None)
            self._perf_probe_acc.pop(target_id, None)
        else:
            self._perf_samples.clear()
            self._perf_last_metrics.clear()
            self._perf_rec_samples.clear()
            self._perf_rec_last.clear()
            self._perf_probe_acc.clear()
        return {"cleared": target_id or "all"}

    async def _perf_set_recording(self, recording):
        if recording:
            self._perf_event_log.clear()
            self._perf_event_seq = 0
            self._perf_handler_stats = {}
            target = self._perf_selected_target
            self._perf_rec_samples.pop(target, None)
            self._perf_rec_last.pop(target, None)
            self._perf_probe_acc.pop(target, None)
            self._perf_profile = []
            self._perf_profile_meta = {}
            self._perf_session_elapsed = 0.0
            self._perf_session_start = time.time() * 1000
            self._perf_recording = True
            await self._perf_agent_start()
            await self._perf_profile_start()
        else:
            if self._perf_session_start > 0:
                self._perf_session_elapsed = max(
                    0.0, (time.time() * 1000 - self._perf_session_start) / 1000)
            await self._perf_drain_events()
            self._perf_recording = False
            await self._perf_profile_stop()
            try:
                target = self._perf_selected_target
                sid = self._attached.get(target) if target else None
                if sid:
                    await self._cdp.cmd("Runtime.evaluate",
                                        {"expression": "window.__olMonitor && window.__olMonitor.stop()",
                                         "returnByValue": True},
                                        sid, timeout=2.0)
            except Exception:
                pass
        return {"recording": self._perf_recording,
                "event_count": len(self._perf_event_log),
                "session_seconds": self._perf_session_seconds()}

    async def _perf_events(self, target_id=None, limit=300):
        events = list(self._perf_event_log)
        if target_id:
            events = [e for e in events if e.get("tab") == target_id]
        target = target_id or self._perf_selected_target
        rec = list(self._perf_rec_samples.get(target, ()))
        cap = 600
        return {
            "connected": self._is_connected(),
            "recording": self._perf_recording,
            "session_seconds": self._perf_session_seconds(),
            "event_count": len(self._perf_event_log),
            "functions": self._perf_profile,
            "profile_active": self._perf_profile_active,
            "events": events[-max(int(limit or 300), 1):],
            "handlers": self._perf_top_handlers(20),
            # The recording graph is plotted against session_start_ms, which is
            # the same wall clock the samples carry in `t`.
            "samples": rec[-cap:],
            "samples_truncated": max(0, len(rec) - cap),
            "sample_interval_ms": int(self._perf_rec_interval * 1000),
            "session_start_ms": int(self._perf_session_start) if self._perf_session_start > 0 else 0,
        }

    async def _perf_clear_events(self):
        self._perf_event_log.clear()
        self._perf_handler_stats = {}
        self._perf_rec_samples.clear()
        self._perf_rec_last.clear()
        self._perf_probe_acc.clear()
        self._perf_profile = []
        self._perf_profile_meta = {}
        self._perf_session_start = 0
        self._perf_session_elapsed = 0.0
        self._perf_recording = False
        return {"recording": False, "event_count": 0, "session_seconds": 0}

    # ------------------------------------------------------------------
    # AI tools: read what the monitor recorded
    # ------------------------------------------------------------------
    def _perf_top_handlers(self, limit=8):
        rows = []
        for stats in self._perf_handler_stats.values():
            calls = stats["calls"]
            name = stats["name"]
            if name == "anonymous" and stats["site"]:
                # Unnamed listeners are only distinguishable by where they were
                # registered, so label them by that site.
                frame = str(stats["site"][0])[:90]
                name = f"anonymous @ {frame}"
            rows.append({
                "name": name,
                "type": stats["type"],
                "calls": calls,
                "ms": round(stats["ms"], 1),
                "avg_ms": round(stats["ms"] / calls, 3) if calls else 0.0,
                "max_ms": round(stats["max_ms"], 2),
                "scroll_calls": stats["scroll_calls"],
                "first_ms": stats["first_ms"],
                "last_ms": stats["last_ms"],
            })
        rows.sort(key=lambda r: (r["ms"], r["calls"]), reverse=True)
        return rows[:limit]

    async def browser_monitor_summary(self):
        """Summarize the browser monitor's current state and last recording.

        Returns resource snapshot values, event counts by kind, the slowest
        events, top handlers by time, and the top functions from the last CPU
        profile. Use this first to decide what to inspect with
        browser_monitor_events or browser_monitor_functions.
        """
        events = list(self._perf_event_log)
        counts = {}
        for e in events:
            kind = e.get("kind", "?")
            counts[kind] = counts.get(kind, 0) + 1
        slowest = sorted(events, key=lambda e: _number(e.get("duration")), reverse=True)[:5]
        slowest_rows = [{
            "id": e.get("id"), "rel_ms": e.get("rel_ms"), "kind": e.get("kind"),
            "name": e.get("name"), "duration_ms": _number(e.get("duration")),
            "during_scroll": bool(e.get("during_scroll")),
        } for e in slowest]
        samples = list(self._perf_samples.get(self._perf_selected_target, ()))
        latest = samples[-1] if samples else None
        return self.result({
            "connected": self._is_connected(),
            "selected_tab": self._perf_selected_target,
            "recording": self._perf_recording,
            "session_seconds": self._perf_session_seconds(),
            "event_count": len(events),
            "event_counts": counts,
            "slowest_events": slowest_rows,
            "top_handlers": self._perf_top_handlers(),
            "profile_active": self._perf_profile_active,
            "top_functions": self._perf_profile[:10],
            "profile_noise": self._perf_profile_meta,
            "resource_latest": latest,
            "resource_samples": len(samples),
        }, success=True)

    async def browser_monitor_events(self, kinds: list = None, min_duration_ms: float = 0,
                                     during_scroll: bool = None, search: str = "",
                                     since_ms: float = None, until_ms: float = None,
                                     include_details: bool = False, limit: int = 50):
        """Read the recorded event timeline with optional filters.

        Args:
            kinds: only these event kinds (e.g. ["scroll", "longtask", "input", "error"]).
            min_duration_ms: only events at least this long.
            during_scroll: true/false to keep only events inside/outside scroll windows.
            search: case-insensitive text match over event, target and handler.
            since_ms / until_ms: session-relative window in milliseconds.
            include_details: include handler source/stack and event data.
            limit: maximum events to return (capped at 200).
        """
        wanted = set(kinds or [])
        term = str(search or "").lower()
        rows = []
        for e in self._perf_event_log:
            rel = _number(e.get("rel_ms"))
            if wanted and e.get("kind") not in wanted:
                continue
            if _number(e.get("duration")) < _number(min_duration_ms):
                continue
            if during_scroll is not None and bool(e.get("during_scroll")) != bool(during_scroll):
                continue
            if since_ms is not None and rel < _number(since_ms):
                continue
            if until_ms is not None and rel > _number(until_ms):
                continue
            if term:
                handler = e.get("handler") or {}
                hay = f"{e.get('kind', '')} {e.get('name', '')} {e.get('target', '')} " \
                      f"{handler.get('name', '') if isinstance(handler, dict) else ''}".lower()
                if term not in hay:
                    continue
            row = {
                "id": e.get("id"), "rel_ms": rel, "kind": e.get("kind"),
                "name": e.get("name"), "duration_ms": _number(e.get("duration")),
                "target": e.get("target"), "severity": e.get("severity"),
                "during_scroll": bool(e.get("during_scroll")),
                "handler_name": (e.get("handler") or {}).get("name")
                                if isinstance(e.get("handler"), dict) else "—",
            }
            if include_details:
                row["handler"] = e.get("handler")
                row["data"] = e.get("data")
            rows.append(row)
        rows.sort(key=lambda r: r["rel_ms"])
        cap = max(min(int(limit or 50), 200), 1)
        return self.result({
            "matched": len(rows), "returned": min(len(rows), cap),
            "recording": self._perf_recording,
            "session_seconds": self._perf_session_seconds(),
            "events": rows[-cap:],
        }, success=True)

    async def browser_monitor_functions(self, min_total_ms: float = 0, limit: int = 20):
        """Read the top functions from the last CPU profile.

        The profile is collected while recording and parsed when recording is
        paused. If the list is empty, record, interact, then pause first.
        `(idle)` and `(program)` frames are excluded from the rows and reported
        as `profile_noise` instead.

        Args:
            min_total_ms: only functions with at least this total time.
            limit: maximum functions to return.
        """
        rows = [f for f in self._perf_profile
                if _number(f.get("total_ms")) >= _number(min_total_ms)]
        rows = rows[:max(min(int(limit or 20), 100), 1)]
        return self.result({
            "profile_active": self._perf_profile_active,
            "recorded_functions": len(self._perf_profile),
            "profile_noise": self._perf_profile_meta,
            "functions": rows,
            "note": "The CPU profile is parsed on Pause; record + interact + pause first."
                    if not rows else "",
        }, success=True)

    async def browser_monitor_resources(self, target_id: str = None, limit: int = 60):
        """Read recent resource samples for the monitor's selected tab.

        Args:
            target_id: tab target id; defaults to the selected monitor tab.
            limit: number of recent samples to return.
        """
        target = target_id or self._perf_selected_target
        samples = list(self._perf_samples.get(target, ()))
        cap = max(min(int(limit or 60), 300), 1)
        return self.result({
            "target_id": target,
            "samples_returned": len(samples[-cap:]),
            "latest": samples[-1] if samples else None,
            "samples": samples[-cap:],
        }, success=True)

    @core.module.tool
    async def browser_monitor(self, view: str = "all", kinds: list = None,
                              min_duration_ms: float = 0, during_scroll: bool = None,
                              search: str = "", since_ms: float = None,
                              until_ms: float = None, include_details: bool = False,
                              limit: int = 50, min_total_ms: float = 0,
                              target_id: str = None):
        """Read the browser monitor's recording and resource data.

        Args:
            view: what to return: all, summary, events, functions, resources.
            kinds: event kinds filter for events.
            min_duration_ms: minimum event duration for events.
            during_scroll: only inside/outside scroll windows for events.
            search: text match over event, target and handler for events.
            since_ms / until_ms: session-relative window for events.
            include_details: include handler source/stack and event data.
            limit: maximum events (or resource samples) to return.
            min_total_ms: minimum total time for functions.
            target_id: resource tab; defaults to the monitor's selected tab.
        """
        def content(result):
            return result.get("content", result) if isinstance(result, dict) else result

        view = str(view or "all").lower()
        if view not in ("all", "summary", "events", "functions", "resources"):
            view = "all"
        data = {"view": view, "recording": self._perf_recording,
                "session_seconds": self._perf_session_seconds()}
        if view in ("all", "summary"):
            data["summary"] = content(await self.browser_monitor_summary())
        if view in ("all", "events"):
            data["events"] = content(await self.browser_monitor_events(
                kinds=kinds, min_duration_ms=min_duration_ms, during_scroll=during_scroll,
                search=search, since_ms=since_ms, until_ms=until_ms,
                include_details=include_details, limit=limit))
        if view in ("all", "functions"):
            data["functions"] = content(await self.browser_monitor_functions(
                min_total_ms=min_total_ms, limit=limit))
        if view in ("all", "resources"):
            data["resources"] = content(await self.browser_monitor_resources(
                target_id=target_id, limit=limit))
        return self.result(data, success=True)


def core_detail_error(exc):
    try:
        import core
        return core.detail_error(exc)
    except Exception:
        return str(exc)
