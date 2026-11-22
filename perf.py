"""Browser performance monitor: resource sampling and event recording.

Resources: Chrome's Performance.getMetrics snapshot, diffed per selected tab,
plus a tiny injected rAF/long-task probe for FPS and longest task.
Events: a recording session that combines CDP events (console/network/etc.)
with an injected page agent that records scroll, input handler invocations,
rAF callbacks and long tasks. All data stays in memory.
"""

import asyncio
import base64
import json
import os
import secrets
import time
from array import array
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
    // fetch() resolves when the headers arrive, so this duration is round-trip
    // latency, not parse time. What matters is the body size and who asked:
    // that is what names the request behind a long frame. Wrapping fetch (and
    // not json) keeps LoAF free to attribute the reaction to the real script.
    fetchNote(url, status, bytes, dur, stack, at) {
      if (!this.enabled) return;
      this.push({
        kind: 'fetch',
        name: url || 'request',
        duration: dur,
        target: status ? 'HTTP ' + status : 'response',
        handler: {name: 'fetch', source: '', stack: stack || [],
                  site: stack ? stack.slice(0, 2) : null},
        data: {bytes: bytes, status: status},
        severity: (bytes && bytes >= 200000) || dur >= 150 ? 'warn' : 'info',
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
    // fetch() resolves when the headers arrive, so what this records is the
    // request, its body size and the code that asked for it - not parse time.
    // Wrapping fetch (and not json) leaves LoAF free to attribute the reaction
    // to the real script, which is what a wrapped json() took away.
    const origFetch = window.fetch;
    if (origFetch) {
      const origin = String((typeof location !== 'undefined' && location.origin) || '');
      window.fetch = function(input, init) {
        const started = performance.now();
        let stack = null;
        if (M.enabled) {
          try { stack = String(new Error().stack).split('\n').slice(2, 6).map((s) => s.trim()); }
          catch (e) {}
        }
        const p = origFetch.apply(this, arguments);
        if (!stack) return p;
        return p.then((res) => {
          try {
            const url = String((res && res.url) || '').replace(origin, '');
            // The monitor polls its own extension API every second; those
            // responses are ours and would only add noise.
            if (!url.includes('/api/ext/browser/perf/')) {
              let bytes = null;
              try { bytes = Number(res.headers.get('content-length')) || null; } catch (e) {}
              M.fetchNote(url, res.status, bytes, performance.now() - started, stack, started);
            }
          } catch (e) {}
          return res;
        });
      };
    }
    M.restore = () => {
      EventTarget.prototype.addEventListener = origAdd;
      EventTarget.prototype.removeEventListener = origRemove;
      window.requestAnimationFrame = origRaf;
      if (origFetch) window.fetch = origFetch;
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
               longest: 0, longestWindow: 0, lastRead: now};
    window.__olPerf = s;
    try {
      new PerformanceObserver((list) => {
        for (const e of list.getEntries()) {
          s.long += 1;
          s.longTotal += 1;
          s.longest = Math.max(s.longest, e.duration || 0);
          s.longestWindow = Math.max(s.longestWindow, e.duration || 0);
        }
      // 'longtask' misses most jank: a long animation frame is a different
      // entry type, so the per-window count would read 0 while frames run long.
      }).observe({entryTypes: ['longtask', 'long-animation-frame']});
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
    longest_window: Math.round(st.longestWindow),
  };
  st.frames = 0;
  st.gapMax = 0;
  st.long = 0;
  st.longestWindow = 0;
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
        # longest_task is the page-lifetime maximum, longest_ms is this window's:
        # a KPI that only ever grows says nothing about the take in front of you.
        "longest_task": int(_number(probe.get("longest"))),
        "longest_ms": int(_number(probe.get("longest_window"))),
    }


# Kinds that arrive in bursts and are folded per window (see _perf_append_event).
_PERF_MERGE_KINDS = ("scroll", "input", "network")

# Recorded network events are limited to these CDP resource types unless
# perf_record_static_network is on: images, fonts, styles and scripts are noise
# in a take, requests are the story.
_PERF_NET_KEEP = ("Document", "XHR", "Fetch", "WebSocket", "EventSource")

# Payload-level chatter (one entry per websocket frame, per chunk) says nothing
# about what the page did; the request and its response do.
_PERF_NET_METHODS = ("Network.requestWillBeSent", "Network.responseReceived",
                     "Network.loadingFailed", "Network.webSocketCreated",
                     "Network.webSocketClosed")


def _cdp_kind(method):
    if method in ("Runtime.consoleAPICalled", "Log.entryAdded"):
        return "console"
    if method in ("Runtime.exceptionThrown", "Network.loadingFailed"):
        return "error"
    # Every Network.* method used to fall through to "input" here, so websocket
    # frames and dataReceived showed up as input events and inflated that count.
    if method.startswith("Network."):
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
        self._perf_merged = {}
        self._perf_merge_window_ms = max(self._perf_rec_interval * 1000, 250)
        self._perf_recording = False
        self._perf_session_start = 0
        self._perf_session_elapsed = 0.0
        self._perf_event_log = deque(maxlen=max(int(self.config.get("perf_events_max", 12000) or 12000), 100))
        # The log is a deque, so a long take silently loses its oldest rows when
        # the cap is reached (a 464 s take did exactly that). Count them, and
        # remember how far the surviving events reach, so a take can say what it
        # is missing instead of looking like it started late.
        self._perf_events_dropped = 0
        self._perf_events_dropped_before_ms = 0.0
        self._perf_event_seq = 0
        self._perf_handler_stats = {}
        self._perf_profile = []
        self._perf_profile_meta = {}
        self._perf_profile_active = False
        self._perf_profile_sid = None
        self._perf_profile_wall = 0.0        # wall clock at Profiler.start
        self._perf_profile_graph = None      # {"nodes": …, "parent": …} for windows
        self._perf_profile_raw = None        # compact samples for windowed rows
        # ~12 bytes per sample in memory: 600k covers ~10 minutes at the ~1 ms
        # sampling interval, which is the length of the takes this is used on.
        # What still falls off is reported (see _perf_profile_coverage), never
        # silently dropped.
        self._perf_raw_cap = 600000
        self._perf_doc_agents = {}
        self._perf_stop_task = None      # scheduled auto-stop for AI-started takes
        self._perf_stop_at = 0.0
        self._perf_last_stop = {}
        self._perf_session_tab = {}      # tab id/url/title captured at Record
        self._perf_session_label = ""    # optional name given to a take
        self._perf_net_extra = False     # Network enabled for this take only
        self._perf_stop = asyncio.Event()
        self._perf_task = asyncio.create_task(self._perf_loop())

    async def perf_close(self):
        self._perf_cancel_stop()
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
        self._perf_cancel_stop()
        self._perf_last_stop = {}
        self._perf_session_tab = {}
        self._perf_session_label = ""
        self._perf_net_extra = False
        self._perf_selected_target = None
        self._perf_recording = False
        self._perf_session_elapsed = 0.0
        self._perf_handler_stats = {}
        self._perf_rec_samples = {}
        self._perf_rec_last = {}
        self._perf_probe_acc = {}
        self._perf_merged = {}
        self._perf_profile = []
        self._perf_profile_meta = {}
        self._perf_profile_active = False
        self._perf_profile_sid = None
        self._perf_profile_wall = 0.0        # wall clock at Profiler.start
        self._perf_profile_graph = None      # {"nodes": …, "parent": …} for windows
        self._perf_profile_raw = None        # compact samples for windowed rows
        self._perf_raw_cap = 600000          # ~10 min at the ~1 ms sampling interval
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
                    "long": 0, "long_total": 0, "longest": 0, "longest_window": 0,
                }
            acc["window_ms"] += _number(probe.get("window_ms"))
            acc["frames"] += int(_number(probe.get("frames")))
            acc["gap_max_ms"] = max(acc["gap_max_ms"], _number(probe.get("gap_max_ms")))
            acc["long"] += int(_number(probe.get("long")))
            acc["long_total"] = int(_number(probe.get("long_total")))
            acc["longest"] = max(acc["longest"], _number(probe.get("longest")))
            acc["longest_window"] = max(acc["longest_window"], _number(probe.get("longest_window")))
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
                            "long": 0, "longest": 0, "longest_window": 0})

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
            # Wall clock at the start lets a session-relative window (the graph's
            # selection) be mapped onto profile sample offsets later.
            self._perf_profile_wall = time.time() * 1000
            self._perf_profile_raw = None
            self._perf_profile_graph = None
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
        """Turn a raw V8 profile into rows, and keep the samples for windows.

        The rows answer "what dominated the take". The samples (node id + delta,
        ~12 bytes each in compact arrays) let a later query answer "what ran
        between 3.1 and 3.4 s" from the same single profiling run.
        """
        nodes = {n["id"]: n for n in (profile.get("nodes") or [])
                 if isinstance(n, dict) and n.get("id") is not None}
        parent = {}
        for node_id, node in nodes.items():
            for child in node.get("children") or []:
                parent[child] = node_id
        samples = profile.get("samples") or []
        deltas = profile.get("timeDeltas") or []
        self_ms, counts = self._perf_accumulate(samples, deltas)
        self._perf_profile_graph = {"nodes": nodes, "parent": parent}
        self._store_raw_samples(samples, deltas)
        rows, noise_ms, functions = self._perf_rows_from(nodes, parent, self_ms, counts)
        self._perf_profile_meta = {
            "idle_ms": round(noise_ms["(idle)"], 1),
            "program_ms": round(noise_ms["(program)"], 1),
            "functions": functions,
            "returned": len(rows),
            "samples": len(self._perf_profile_raw["ids"]) if self._perf_profile_raw else 0,
        }
        return rows

    @staticmethod
    def _perf_accumulate(samples, deltas, start_us=0.0, since_us=None, until_us=None):
        self_ms = {}
        counts = {}
        t = start_us
        for i, node_id in enumerate(samples):
            dt = max(_number(deltas[i]) if i < len(deltas) else 0.0, 0.0)
            t += dt
            if since_us is not None and t < since_us:
                continue
            if until_us is not None and t > until_us:
                continue
            self_ms[node_id] = self_ms.get(node_id, 0.0) + dt / 1000.0
            counts[node_id] = counts.get(node_id, 0) + 1
        return self_ms, counts

    @staticmethod
    def _perf_rows_from(nodes, parent, self_ms, counts, top=30):
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
        # by construction, so they are counted and reported as overhead instead
        # of as rows.
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
        for row in rows[:top]:
            row["self_ms"] = round(row["self_ms"], 1)
            row["total_ms"] = round(row["total_ms"], 1)
            result.append(row)
        return result, noise_ms, len(rows)

    def _perf_profile_coverage(self):
        """How much of the take the raw CPU samples actually cover.

        The profiler stream is ring-buffered, so a long take loses its oldest
        samples and the profile silently starts mid-session. Report it instead:
        `covered_s` is what survived, `dropped_s` what the cap removed, `cap`
        the sample count it is allowed to keep.
        """
        raw = getattr(self, "_perf_profile_raw", None)
        cap = int(getattr(self, "_perf_raw_cap", 0) or 0)
        if not raw:
            return {"samples": 0, "covered_s": 0.0, "dropped_s": 0.0, "cap": cap}
        return {
            "samples": len(raw.get("ids") or ()),
            "covered_s": round((raw.get("covered_us") or 0.0) / 1e6, 1),
            "dropped_s": round((raw.get("dropped_us") or 0.0) / 1e6, 1),
            "cap": cap,
        }

    def _perf_profile_raw_payload(self):
        """Compact, JSON-safe copy of the raw samples for a saved take.

        base64 of the native arrays: ~12 bytes/sample instead of ~12 characters
        per number in JSON, so a 5 minute take carries a few MB, not tens.
        """
        raw = getattr(self, "_perf_profile_raw", None)
        graph = getattr(self, "_perf_profile_graph", None)
        if not raw or not graph:
            return None
        return {
            "ids": base64.b64encode(raw["ids"].tobytes()).decode("ascii"),
            "deltas": base64.b64encode(raw["deltas"].tobytes()).decode("ascii"),
            "dropped_us": raw["dropped_us"],
            "wall_ms": int(self._perf_profile_wall or 0),
        }

    def _perf_profile_graph_payload(self):
        """Node names and parent links, JSON-safe (ids as strings/pairs)."""
        graph = getattr(self, "_perf_profile_graph", None)
        if not graph:
            return None
        nodes = {}
        for node_id, node in graph.get("nodes", {}).items():
            frame = node.get("callFrame") or {}
            nodes[str(node_id)] = {
                "functionName": frame.get("functionName") or "",
                "url": frame.get("url") or "",
                "lineNumber": int(frame.get("lineNumber") or 0),
            }
        pairs = [[child, par] for child, par in (graph.get("parent") or {}).items()]
        return {"nodes": nodes, "parent": pairs}

    def _store_raw_samples(self, samples, deltas):
        if not samples:
            self._perf_profile_raw = None
            return
        cap = int(getattr(self, "_perf_raw_cap", 600000) or 600000)
        keep = samples[-cap:]
        keep_deltas = [max(_number(d), 0.0) for d in deltas[-cap:]]
        # One pass over the whole stream: `dropped_us` is the wall time of the
        # samples the cap threw away (the start of the take), so coverage is
        # total minus dropped. Without this a long take's profile silently
        # starts in the middle, which is exactly what happened to a 430 s take
        # whose first 103.8 s were missing with nothing in the payload saying so.
        total = sum(max(_number(d), 0.0) for d in deltas)
        dropped = total - sum(keep_deltas)
        self._perf_profile_raw = {
            "ids": array("i", keep),
            "deltas": array("d", keep_deltas),
            "dropped_us": dropped,
            "covered_us": total - dropped,
            "total_us": total,
        }

    def _perf_profile_rows(self, since_ms=None, until_ms=None):
        """Rows for the whole take, or only for a session-relative window.

        `since_ms`/`until_ms` use the same clock as events and the graph, so the
        range the user dragged over a spike is exactly the range queried here.
        """
        if since_ms is None and until_ms is None:
            return self._perf_profile
        raw = self._perf_profile_raw
        graph = self._perf_profile_graph
        if not raw or not graph:
            return []
        base = ((self._perf_session_start - self._perf_profile_wall)
                if self._perf_profile_wall else 0.0)
        lo = ((since_ms if since_ms is not None else -1e9) + base) * 1000.0
        hi = ((until_ms if until_ms is not None else 1e12) + base) * 1000.0
        ids = raw["ids"]
        deltas = raw["deltas"]
        self_ms = {}
        counts = {}
        taken = 0
        t = raw["dropped_us"]
        for i in range(len(ids)):
            dt = deltas[i]
            if dt < 0:
                dt = 0.0
            t += dt
            if t < lo or t > hi:
                continue
            node_id = ids[i]
            self_ms[node_id] = self_ms.get(node_id, 0.0) + dt / 1000.0
            counts[node_id] = counts.get(node_id, 0) + 1
            taken += 1
        rows, _noise, total = self._perf_rows_from(graph["nodes"], graph["parent"],
                                                   self_ms, counts)
        self._perf_window_samples = taken
        self._perf_window_functions = total
        return rows

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

        # Scrolling, pointer movement and network traffic arrive in bursts. One
        # row per (kind, name, window) keeps how many happened and the worst
        # duration in that window, instead of one timeline row every 40 ms that
        # buries everything else. The row keeps the first event's id and time.
        bucket = None
        if item.get("kind") in _PERF_MERGE_KINDS:
            bucket = int(item["rel_ms"] // self._perf_merge_window_ms)
        if bucket is not None:
            key = (item.get("kind"), item.get("name"), bucket)
            merged = self._perf_merged.get(key)
            if merged is not None:
                data = merged.setdefault("data", {})
                data["count"] = int(data.get("count") or 1) + 1
                data["ms_total"] = round(
                    _number(data.get("ms_total")) + _number(item.get("duration")), 2)
                if _number(item.get("duration")) > _number(merged.get("duration")):
                    merged["duration"] = item["duration"]
                    merged["severity"] = item["severity"]
                # A request and its response fold into one row: keep the method
                # from the first and the status from the second.
                for extra_key, extra_value in (item.get("data") or {}).items():
                    if extra_key not in ("count", "ms_total") and extra_value not in (None, ""):
                        data.setdefault(extra_key, extra_value)
                merged["during_scroll"] = (bool(merged.get("during_scroll"))
                                           or bool(item.get("during_scroll")))
                return
            item["data"]["count"] = 1
            item["data"]["ms_total"] = round(_number(item.get("duration")), 2)
            if len(self._perf_merged) > 2000:
                # Their rows have long left the log; only the map would grow.
                self._perf_merged.clear()
            self._perf_merged[key] = item
        log = self._perf_event_log
        if log.maxlen is not None and len(log) >= log.maxlen and log:
            # appending evicts the oldest row; keep count of what the take lost
            self._perf_events_dropped += 1
            self._perf_events_dropped_before_ms = max(
                self._perf_events_dropped_before_ms, _number(log[0].get("rel_ms")))
        log.append(item)

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
        if (method.startswith("Network.")
                and not self.config.get("perf_record_static_network", False)):
            rtype = params.get("type") or ""
            if rtype and rtype not in _PERF_NET_KEEP:
                return
        kind = _cdp_kind(method)
        if method.startswith("Network.") and method not in _PERF_NET_METHODS:
            return
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
            # Named by url so the response for the same request folds into this
            # row; the method and status ride along in data.
            text = req.get("url", "")
            target = text
            data["method"] = req.get("method", "?")
        elif method == "Network.responseReceived":
            resp = params.get("response", {}) or {}
            text = resp.get("url", "")
            target = text
            data["status"] = resp.get("status")
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

    async def _perf_network(self, enable):
        """Turn Network on for the recorded tab while a take is running.

        Network events are what make a recording tell the whole story, but the
        module keeps Network off for ordinary browsing. This covers only the
        take and puts it back when the take ends; when the setting is already on
        for every tab there is nothing to restore.
        """
        target = self._perf_selected_target
        sid = self._attached.get(target) if target else None
        if not sid or self.config.get("enable_network_monitoring", False):
            self._perf_net_extra = False
            return
        if not enable and not self._perf_net_extra:
            return
        try:
            await self._cdp.cmd("Network.enable" if enable else "Network.disable",
                                {}, sid, timeout=2.0)
            self._perf_net_extra = bool(enable)
        except Exception:
            self._perf_net_extra = False

    async def _perf_capture_tab(self):
        """id/url/title of the tab being recorded, so a saved take says where it came from.

        Captured when Record is pressed: the tab can navigate or be closed during
        the take, and the interesting value is what it was showing when the user
        hit record.
        """
        target = self._perf_selected_target
        info = {"target_id": target, "url": "", "title": ""}
        if not target:
            return info
        try:
            listing = await self._perf_targets()
            for entry in listing.get("targets") or []:
                if entry.get("id") == target:
                    info["url"] = entry.get("url") or ""
                    info["title"] = entry.get("title") or ""
                    break
        except Exception:
            pass
        if not info["url"]:
            info["url"] = getattr(self, "_target_url", {}).get(target, "") or ""
        return info

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
            await self._perf_network(True)
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
            "events_dropped": self._perf_events_dropped,
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

    async def _perf_set_recording(self, recording, label=None):
        # Any explicit state change supersedes a timer: the AI's auto-stop must
        # never fire into a take the user started afterwards.
        self._perf_cancel_stop()
        was_recording = self._perf_recording
        session_id = None
        if recording:
            self._perf_event_log.clear()
            self._perf_event_seq = 0
            self._perf_events_dropped = 0
            self._perf_events_dropped_before_ms = 0.0
            self._perf_handler_stats = {}
            self._perf_merged = {}
            target = self._perf_selected_target
            self._perf_rec_samples.pop(target, None)
            self._perf_rec_last.pop(target, None)
            self._perf_probe_acc.pop(target, None)
            self._perf_profile = []
            self._perf_profile_meta = {}
            self._perf_session_elapsed = 0.0
            self._perf_session_start = time.time() * 1000
            self._perf_session_tab = await self._perf_capture_tab()
            self._perf_session_label = str(label or "").strip()[:60]
            self._perf_recording = True
            await self._perf_agent_start()
            await self._perf_profile_start()
            await self._perf_network(True)
        else:
            if self._perf_session_start > 0:
                self._perf_session_elapsed = max(
                    0.0, (time.time() * 1000 - self._perf_session_start) / 1000)
            await self._perf_drain_events()
            self._perf_recording = False
            await self._perf_profile_stop()
            await self._perf_network(False)
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
        if was_recording and not self._perf_recording:
            session_id = self._perf_save_session()
            self._perf_last_stop = {
                "id": session_id,
                "by": "manual",
                "at": int(time.time() * 1000),
            } if session_id else {}
        return {"recording": self._perf_recording,
                "event_count": len(self._perf_event_log),
                "session_seconds": self._perf_session_seconds(),
                "session_id": session_id}

    @staticmethod
    def _perf_decimate(rows, cap):
        """At most `cap` evenly spaced rows, spanning the whole range.

        Sending the newest `cap` rows (the old behaviour) hid the start of a
        long take: a 463 s take at the 250 ms grid has 1606 rows, so the panel
        was given 150 s of it and the graph looked like it began minutes late.
        A stride keeps the shape of the whole session and always ends on the
        newest row. A live client appends exact rows on top of this base (see
        `sample_mode` in _perf_events).
        """
        rows = list(rows or ())
        n = len(rows)
        cap = max(int(cap or 1), 1)
        if n <= cap:
            return rows
        step = n / float(cap)
        picked = [rows[int(i * step)] for i in range(cap)]
        picked[-1] = rows[-1]
        return picked

    async def _perf_events(self, target_id=None, limit=300, since_t=None, since_start=None):
        events = list(self._perf_event_log)
        if target_id:
            events = [e for e in events if e.get("tab") == target_id]
        target = target_id or self._perf_selected_target
        rec = list(self._perf_rec_samples.get(target, ()))
        # Exact up to ~16 min at the 250 ms grid; only a base larger than this is
        # decimated, because the base is sent once per client (join, reconnect,
        # review) while every poll after it carries just the new rows.
        cap = 4000
        session_start = int(self._perf_session_start) if self._perf_session_start > 0 else 0
        # Samples are append-only inside one take, so a client that already has
        # the history only needs what arrived since. A client with nothing (first
        # poll, reconnect, target switch, new take) gets a decimated base of the
        # whole take. `since_start` is the client's idea of the session start: if
        # it does not match, its history belongs to another take and appending
        # would splice two sessions together, so it gets a base instead.
        if since_t is not None and session_start and int(since_start or 0) == session_start:
            samples = [row for row in rec if _number(row.get("t")) > _number(since_t)]
            sample_mode = "append"
        else:
            samples = self._perf_decimate(rec, cap)
            sample_mode = "base"
        return {
            "connected": self._is_connected(),
            "recording": self._perf_recording,
            "session_seconds": self._perf_session_seconds(),
            "event_count": len(self._perf_event_log),
            "events_dropped": self._perf_events_dropped,
            "events_dropped_before_ms": round(self._perf_events_dropped_before_ms, 1),
            "functions": self._perf_profile,
            "profile_active": self._perf_profile_active,
            "profile_coverage": self._perf_profile_coverage(),
            "events": events[-max(int(limit or 300), 1):],
            "handlers": self._perf_top_handlers(20),
            # The recording graph is plotted against session_start_ms, which is
            # the same wall clock the samples carry in `t`.
            "samples": samples,
            "samples_total": len(rec),
            "sample_mode": sample_mode,
            "sample_interval_ms": int(self._perf_rec_interval * 1000),
            "session_start_ms": session_start,
            "last_session_id": (self._perf_last_stop or {}).get("id"),
        }

    async def _perf_clear_events(self):
        self._perf_cancel_stop()
        self._perf_last_stop = {}
        await self._perf_network(False)
        self._perf_event_log.clear()
        self._perf_event_seq = 0
        self._perf_events_dropped = 0
        self._perf_events_dropped_before_ms = 0.0
        self._perf_handler_stats = {}
        self._perf_merged = {}
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
    # Saved recordings
    # ------------------------------------------------------------------
    def _perf_recordings_dir(self):
        path = core.get_data_path("browser/recordings")
        os.makedirs(path, exist_ok=True)
        return path

    def _perf_save_session(self):
        """Write the finished take to data/browser/recordings/<id>.json.

        Pausing is the natural save point: the events, the fine samples, the
        folded handler totals and the CPU profile are all in memory by then, and
        a recording is only worth keeping once it has stopped. A small sidecar
        .meta.json keeps listing cheap without reading multi-megabyte files.
        """
        target = self._perf_selected_target
        events = list(self._perf_event_log)
        samples = list(self._perf_rec_samples.get(target, ()))
        if not events and not samples:
            return None
        rid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        long_frames = sum(1 for e in events if e.get("kind") == "longtask")
        saved_at = int(time.time() * 1000)
        started_at = int(self._perf_session_start) if self._perf_session_start > 0 else saved_at
        tab_info = self._perf_session_tab or {}
        tab_url = tab_info.get("url") or getattr(self, "_target_url", {}).get(target, "") or ""
        payload = {
            "version": 1,
            "id": rid,
            "started_at": started_at,
            "started_at_local": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started_at / 1000)),
            "saved_at": saved_at,
            "saved_at_local": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(saved_at / 1000)),
            "tab": target,
            "tab_url": tab_url,
            "tab_title": tab_info.get("title") or "",
            "label": self._perf_session_label or "",
            "session_seconds": self._perf_session_seconds(),
            "session_start_ms": int(self._perf_session_start) if self._perf_session_start > 0 else 0,
            "sample_interval_ms": int(self._perf_rec_interval * 1000),
            "event_count": len(events),
            "events_dropped": self._perf_events_dropped,
            "events_dropped_before_ms": round(self._perf_events_dropped_before_ms, 1),
            "events": events,
            "samples": samples,
            "handlers": self._perf_top_handlers(50),
            "profile": self._perf_profile,
            "profile_meta": self._perf_profile_meta,
            "profile_coverage": self._perf_profile_coverage(),
            "profile_raw": self._perf_profile_raw_payload(),
            "profile_graph": self._perf_profile_graph_payload(),
        }
        meta = {
            "id": rid,
            "started_at": started_at,
            "started_at_local": payload["started_at_local"],
            "saved_at": saved_at,
            "saved_at_local": payload["saved_at_local"],
            "tab": target,
            "tab_url": tab_url,
            "tab_title": payload["tab_title"],
            "label": payload["label"],
            "session_seconds": payload["session_seconds"],
            "event_count": len(events),
            "events_dropped": self._perf_events_dropped,
            "samples": len(samples),
            "long_frames": long_frames,
        }
        try:
            folder = self._perf_recordings_dir()
            with open(os.path.join(folder, rid + ".json"), "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            with open(os.path.join(folder, rid + ".meta.json"), "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False)
            self._perf_prune_recordings(50)
            return rid
        except Exception as e:
            self.log("browser", f"perf monitor: could not save the recording: {core_detail_error(e)}")
            return None

    def _perf_remove_recording(self, folder, name):
        for path in (os.path.join(folder, name),
                     os.path.join(folder, name[:-5] + ".meta.json")):
            try:
                os.remove(path)
            except OSError:
                pass

    def _perf_prune_recordings(self, keep=50, max_mb=200):
        """Keep the newest `keep` takes and the most recent `max_mb` of them.

        A take carries every event and sample, so fifty of them can reach
        hundreds of megabytes; the size ceiling deletes the oldest first.
        """
        try:
            folder = self._perf_recordings_dir()

            def take_names():
                return sorted((n for n in os.listdir(folder) if n.endswith(".json")
                               and not n.endswith(".meta.json")), reverse=True)

            names = take_names()
            for name in names[max(int(keep), 1):]:
                self._perf_remove_recording(folder, name)

            names = take_names()
            budget = max(int(max_mb), 1) * 1024 * 1024
            total = 0
            cut = None
            for index, name in enumerate(names):
                try:
                    total += os.path.getsize(os.path.join(folder, name))
                except OSError:
                    pass
                if total > budget:
                    cut = index
                    break
            if cut is not None:
                for name in names[cut:]:
                    self._perf_remove_recording(folder, name)
        except Exception:
            pass

    def _perf_list_recordings(self, limit=20):
        rows = []
        try:
            folder = self._perf_recordings_dir()
            for name in sorted((n for n in os.listdir(folder) if n.endswith(".meta.json")),
                               reverse=True)[:max(int(limit or 20), 1)]:
                try:
                    with open(os.path.join(folder, name), encoding="utf-8") as fh:
                        rows.append(json.load(fh))
                except Exception:
                    continue
        except Exception as e:
            self.log("browser", f"perf monitor: could not list recordings: {core_detail_error(e)}")
        return rows

    def _perf_load_session(self, session_id):
        name = os.path.basename(str(session_id or "").strip())
        if not name:
            raise RuntimeError("No recording id given.")
        if not name.endswith(".json"):
            name += ".json"
        path = os.path.join(self._perf_recordings_dir(), name)
        if not os.path.isfile(path):
            raise RuntimeError(f"No saved recording named '{session_id}'. "
                               "Use view=\"recordings\" to list them.")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def _perf_session_summary(self, data):
        events = data.get("events") or []
        counts = {}
        for e in events:
            kind = e.get("kind", "?")
            counts[kind] = counts.get(kind, 0) + 1
        slowest = sorted(events, key=lambda e: _number(e.get("duration")), reverse=True)[:5]
        samples = data.get("samples") or []
        # Count the recorded long frames, exactly like the live summary: the
        # per-window probe total can miss a frame that started before the first
        # window and says nothing about the take as a whole.
        long_session = sum(1 for e in events if e.get("kind") == "longtask")
        return {
            "recording_id": data.get("id"),
            "label": data.get("label") or "",
            "started_at": data.get("started_at"),
            "started_at_local": data.get("started_at_local"),
            "saved_at": data.get("saved_at"),
            "saved_at_local": data.get("saved_at_local"),
            "tab": data.get("tab"),
            "tab_url": data.get("tab_url"),
            "tab_title": data.get("tab_title"),
            "recording": False,
            "session_seconds": data.get("session_seconds"),
            "event_count": len(events),
            "event_counts": counts,
            "slowest_events": [{
                "id": e.get("id"), "rel_ms": e.get("rel_ms"), "kind": e.get("kind"),
                "name": e.get("name"), "duration_ms": _number(e.get("duration")),
                "during_scroll": bool(e.get("during_scroll")),
            } for e in slowest],
            "top_handlers": data.get("handlers") or [],
            "profile_active": False,
            "top_functions": (data.get("profile") or [])[:10],
            "profile_noise": data.get("profile_meta") or {},
            "long_tasks_session": long_session,
            "sample_interval_ms": data.get("sample_interval_ms"),
            "resource_latest": samples[-1] if samples else None,
            "resource_samples": len(samples),
        }

    def _perf_recording_for_ui(self, session_id):
        """One saved take, trimmed for the review view.

        The file can be megabytes; the panel only needs enough to redraw the
        graph and the first few hundred events, so both are capped here rather
        than shipping the whole take to the browser.
        """
        data = self._perf_load_session(session_id)
        events = data.get("events") or []
        samples = data.get("samples") or []
        return {
            "id": data.get("id"),
            "label": data.get("label") or "",
            "started_at_local": data.get("started_at_local"),
            "saved_at_local": data.get("saved_at_local"),
            "tab_url": data.get("tab_url"),
            "tab_title": data.get("tab_title"),
            "session_seconds": data.get("session_seconds"),
            "session_start_ms": data.get("session_start_ms"),
            "sample_interval_ms": data.get("sample_interval_ms"),
            "event_count": len(events),
            "events_dropped": data.get("events_dropped") or 0,
            "events_dropped_before_ms": data.get("events_dropped_before_ms") or 0,
            "events": events[-400:],
            # Whole take: a review must show the beginning too, and the file is
            # read once here. Only huge takes get decimated (see _perf_events).
            "samples": self._perf_decimate(samples, 4000),
            "samples_total": len(samples),
            "handlers": data.get("handlers") or [],
            "profile": data.get("profile") or [],
            "profile_meta": data.get("profile_meta") or {},
            "profile_coverage": data.get("profile_coverage") or {},
        }

    def _perf_rename_recording(self, session_id, label):
        """Set the label of a saved take, in the take and its sidecar.

        The label lives in both places on purpose: the sidecar is what the list
        reads (cheaply), the take itself is what review mode and the AI tool
        read, so a rename has to reach both or the two disagree. Rewriting a
        multi-megabyte take costs a few tens of ms, which is fine for a manual
        action.
        """
        name = os.path.basename(str(session_id or "").strip())
        if not name:
            raise RuntimeError("No recording id given.")
        if not name.endswith(".json"):
            name += ".json"
        folder = self._perf_recordings_dir()
        path = os.path.join(folder, name)
        if not os.path.exists(path):
            raise RuntimeError(f"No recording '{session_id}'.")
        label = str(label or "").strip()[:60]
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["label"] = label
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)

        meta_path = os.path.join(folder, name[:-5] + ".meta.json")
        try:
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
        except Exception:
            meta = {"id": data.get("id") or name[:-5]}
        meta["label"] = label
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False)
        return {"id": meta.get("id") or name[:-5], "label": label}

    def _perf_delete_recording(self, session_id):
        name = os.path.basename(str(session_id or "").strip())
        if not name:
            raise RuntimeError("No recording id given.")
        if not name.endswith(".json"):
            name += ".json"
        folder = self._perf_recordings_dir()
        removed = False
        for path in (os.path.join(folder, name),
                     os.path.join(folder, name[:-5] + ".meta.json")):
            try:
                os.remove(path)
                removed = True
            except OSError:
                pass
        return {"deleted": name, "ok": removed}

    def _perf_session_profile_rows(self, data, since_ms=None, until_ms=None):
        """Rows for a saved take, optionally restricted to a time window."""
        if since_ms is None and until_ms is None:
            return data.get("profile") or []
        raw = data.get("profile_raw")
        graph = data.get("profile_graph")
        if not raw or not graph:
            return []
        ids = array("i")
        ids.frombytes(base64.b64decode(raw.get("ids") or ""))
        deltas = array("d")
        deltas.frombytes(base64.b64decode(raw.get("deltas") or ""))
        base = _number(data.get("session_start_ms")) - _number(raw.get("wall_ms"))
        lo = ((since_ms if since_ms is not None else -1e9) + base) * 1000.0
        hi = ((until_ms if until_ms is not None else 1e12) + base) * 1000.0
        t = _number(raw.get("dropped_us"))
        self_ms = {}
        counts = {}
        taken = 0
        for i in range(len(ids)):
            dt = deltas[i]
            if dt < 0:
                dt = 0.0
            t += dt
            if t < lo or t > hi:
                continue
            node_id = ids[i]
            self_ms[node_id] = self_ms.get(node_id, 0.0) + dt / 1000.0
            counts[node_id] = counts.get(node_id, 0) + 1
            taken += 1
        nodes = {int(nid): {"id": int(nid), "callFrame": frame}
                 for nid, frame in (graph.get("nodes") or {}).items()}
        parent = {int(child): int(par) for child, par in (graph.get("parent") or [])}
        rows, _noise, total = self._perf_rows_from(nodes, parent, self_ms, counts)
        self._perf_window_samples = taken
        self._perf_window_functions = total
        return rows

    def _perf_session_functions(self, data, min_total_ms=0, limit=20,
                                since_ms=None, until_ms=None):
        windowed = since_ms is not None or until_ms is not None
        profile = self._perf_session_profile_rows(data, since_ms, until_ms)
        rows = [f for f in profile if _number(f.get("total_ms")) >= _number(min_total_ms)]
        cap = max(min(int(limit or 20), 100), 1)
        return {
            "recording_id": data.get("id"),
            "profile_active": False,
            "recorded_functions": (getattr(self, "_perf_window_functions", 0)
                                   if windowed else
                                   (data.get("profile_meta") or {}).get(
                                       "functions", len(data.get("profile") or []))),
            "profile_noise": data.get("profile_meta") or {},
            "profile_coverage": data.get("profile_coverage") or {},
            "window": ({"since_ms": since_ms, "until_ms": until_ms,
                        "samples": getattr(self, "_perf_window_samples", 0)}
                       if windowed else None),
            "functions": rows[:cap],
        }

    def _perf_session_resources(self, data, limit=60):
        samples = data.get("samples") or []
        cap = max(min(int(limit or 60), 300), 1)
        return {
            "recording_id": data.get("id"),
            "sample_interval_ms": data.get("sample_interval_ms"),
            "samples_returned": len(samples[-cap:]),
            "latest": samples[-1] if samples else None,
            "samples": samples[-cap:],
        }

    def _perf_event_rows(self, events, kinds=None, min_duration_ms=0, during_scroll=None,
                         search="", since_ms=None, until_ms=None, include_details=False,
                         limit=50):
        wanted = set(kinds or [])
        term = str(search or "").lower()
        rows = []
        for e in events:
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
            data = e.get("data") or {}
            row = {
                "id": e.get("id"), "rel_ms": rel, "kind": e.get("kind"),
                "name": e.get("name"), "duration_ms": _number(e.get("duration")),
                "target": e.get("target"), "severity": e.get("severity"),
                "during_scroll": bool(e.get("during_scroll")),
                # Bursts are folded per window; `count` says how many ran and
                # `ms_total` what they cost together.
                "count": int(_number(data.get("count")) or 1),
                "ms_total": round(_number(data.get("ms_total")
                                          if "ms_total" in data else e.get("duration")), 2),
                "handler_name": (e.get("handler") or {}).get("name")
                                if isinstance(e.get("handler"), dict) else "—",
            }
            if include_details:
                row["handler"] = e.get("handler")
                row["data"] = e.get("data")
            rows.append(row)
        rows.sort(key=lambda r: r["rel_ms"])
        cap = max(min(int(limit or 50), 200), 1)
        return {
            "matched": len(rows),
            "returned": min(len(rows), cap),
            "events": rows[-cap:],
        }

    # ------------------------------------------------------------------
    # Auto-stop for AI-started recordings
    # ------------------------------------------------------------------
    def _perf_cancel_stop(self):
        task = getattr(self, "_perf_stop_task", None)
        self._perf_stop_task = None
        self._perf_stop_at = 0.0
        if task is not None and not task.done():
            task.cancel()

    def _perf_schedule_stop(self, seconds):
        self._perf_cancel_stop()
        seconds = max(1, min(int(seconds or 15), 120))
        self._perf_stop_at = time.time() + seconds
        self._perf_stop_task = asyncio.create_task(self._perf_auto_stop(seconds))
        return seconds

    async def _perf_auto_stop(self, seconds):
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        self._perf_stop_task = None
        self._perf_stop_at = 0.0
        if not self._perf_recording:
            return
        await self._perf_set_recording(False)
        stop = dict(self._perf_last_stop or {})
        stop["by"] = "timer"
        self._perf_last_stop = stop
        self.log("browser", f"perf monitor: recording stopped after {seconds}s")

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

    @core.module.tool
    async def browser_monitor_record(self, action: str = "start", seconds: int = 15,
                                     restart: bool = False, label: str = ""):
        """Start, stop or inspect a recording session.

        `start` returns immediately and schedules the stop on the server, so the
        page can be driven (scroll, click) during the window instead of waiting
        inside the call. Any explicit change - the UI's Pause or Record button, a
        later start, this tool - cancels that timer, so the user keeps control.
        Stopping saves the take to data/browser/recordings and returns its id.

        Args:
            action: start, stop or status.
            seconds: how long to record, 1-120 (default 15).
            restart: start a fresh take even if one is already running; this
                clears the current session (it is saved first).
            label: a short name for the take, shown in the saved list and in
                the summary (e.g. "scroll repro").
        """
        action = str(action or "start").strip().lower()
        now = time.time()

        def state():
            out = {
                "recording": self._perf_recording,
                "label": self._perf_session_label or "",
                "session_seconds": self._perf_session_seconds(),
                "event_count": len(self._perf_event_log),
                "sample_interval_ms": int(self._perf_rec_interval * 1000),
                "last_session": dict(self._perf_last_stop or {}),
            }
            if self._perf_stop_at > 0:
                out["remaining_s"] = max(0, round(self._perf_stop_at - now, 1))
            return out

        if action == "status":
            return self.result(state(), success=True)

        if action == "start":
            if not self._is_connected() or not self._perf_selected_target:
                return self.result(
                    {"error": "No monitored tab: connect the browser and select a tab in the "
                              "Browser Monitor panel before recording."},
                    success=False)
            if self._perf_recording and not restart:
                out = state()
                out["hint"] = ("A recording is already running (the user may have started "
                               "it); it was left untouched. Act now, then call stop.")
                return self.result(out, success=True)
            had_session = bool(self._perf_event_log) or bool(
                self._perf_rec_samples.get(self._perf_selected_target))
            if restart and self._perf_recording:
                await self._perf_set_recording(False)
            await self._perf_set_recording(True)
            self._perf_session_label = str(label or "").strip()[:60]
            window = self._perf_schedule_stop(seconds)
            out = state()
            out.update({"seconds": window, "remaining_s": window,
                        "cleared_previous": had_session,
                        "hint": "Interact with the page now; recording stops by itself."})
            return self.result(out, success=True)

        if action == "stop":
            was_recording = self._perf_recording
            stopped = await self._perf_set_recording(False) if was_recording else {}
            out = state()
            out["recording_id"] = (stopped or {}).get("session_id")
            out["hint"] = ("Read it later with browser_monitor(recording_id=...); "
                           "view=\"recordings\" lists saved takes."
                           if out["recording_id"] else "Nothing was recording.")
            return self.result(out, success=True)

        return self.result({"error": "action must be start, stop or status"}, success=False)

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
        # `long_tasks_total` is cumulative since the page loaded, which says
        # nothing about this take. The session's count is the recorded long
        # frames/tasks, which is also what the timeline shows.
        long_session = sum(1 for e in events if e.get("kind") == "longtask")
        return self.result({
            "connected": self._is_connected(),
            "selected_tab": self._perf_selected_target,
            "recording": self._perf_recording,
            "last_session_id": (self._perf_last_stop or {}).get("id"),
            "session_seconds": self._perf_session_seconds(),
            "event_count": len(events),
            "event_counts": counts,
            "slowest_events": slowest_rows,
            "top_handlers": self._perf_top_handlers(),
            "profile_active": self._perf_profile_active,
            "top_functions": self._perf_profile[:10],
            "profile_noise": self._perf_profile_meta,
            "long_tasks_session": long_session,
            "resource_latest": latest,
            "resource_samples": len(samples),
        }, success=True)

    async def browser_monitor_events(self, kinds: list = None, min_duration_ms: float = 0,
                                     during_scroll: bool = None, search: str = "",
                                     since_ms: float = None, until_ms: float = None,
                                     include_details: bool = False, limit: int = 50):
        """Read the recorded event timeline with optional filters.

        Args:
            kinds: only these event kinds (e.g. ["scroll", "longtask", "input", "fetch", "error"]).
            min_duration_ms: only events at least this long.
            during_scroll: true/false to keep only events inside/outside scroll windows.
            search: case-insensitive text match over event, target and handler.
            since_ms / until_ms: session-relative window in milliseconds.
            include_details: include handler source/stack and event data.
            limit: maximum events to return (capped at 200).
        """
        rows = self._perf_event_rows(
            list(self._perf_event_log), kinds=kinds, min_duration_ms=min_duration_ms,
            during_scroll=during_scroll, search=search, since_ms=since_ms,
            until_ms=until_ms, include_details=include_details, limit=limit)
        return self.result({
            "connected": self._is_connected(),
            "recording": self._perf_recording,
            "session_seconds": self._perf_session_seconds(),
            "matched": rows["matched"],
            "returned": rows["returned"],
            "events": rows["events"],
        }, success=True)

    async def browser_monitor_functions(self, min_total_ms: float = 0, limit: int = 20,
                                        since_ms: float = None, until_ms: float = None):
        """Read the top functions from the last CPU profile.

        The profile is collected while recording and parsed when recording is
        paused. If the list is empty, record, interact, then pause first.
        `(idle)` and `(program)` frames are excluded from the rows and reported
        as `profile_noise` instead.

        Args:
            min_total_ms: only functions with at least this total time.
            limit: maximum functions to return.
            since_ms / until_ms: restrict the profile to a session-relative
                window (the same clock as events and the graph), e.g. the range
                dragged over a CPU spike.
        """
        windowed = since_ms is not None or until_ms is not None
        source = self._perf_profile_rows(since_ms, until_ms) if windowed else self._perf_profile
        rows = [f for f in source
                if _number(f.get("total_ms")) >= _number(min_total_ms)]
        rows = rows[:max(min(int(limit or 20), 100), 1)]
        note = ""
        if not rows:
            note = ("No samples in that window (or none above min_total_ms). "
                    "Widen it, or record + pause first."
                    if windowed else
                    "The CPU profile is parsed on Pause; record + interact + pause first.")
        return self.result({
            "profile_active": self._perf_profile_active,
            "recorded_functions": (getattr(self, "_perf_window_functions", 0)
                                   if windowed else
                                   self._perf_profile_meta.get("functions",
                                                               len(self._perf_profile))),
            "profile_noise": self._perf_profile_meta,
            "profile_coverage": self._perf_profile_coverage(),
            "window": ({"since_ms": since_ms, "until_ms": until_ms,
                        "samples": getattr(self, "_perf_window_samples", 0),
                        "buffer_samples": len(self._perf_profile_raw["ids"])
                                          if self._perf_profile_raw else 0}
                       if windowed else None),
            "functions": rows,
            "note": note,
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
    @core.module.tool
    async def browser_monitor(self, view: str = "all", kinds: list = None,
                              min_duration_ms: float = 0, during_scroll: bool = None,
                              search: str = "", since_ms: float = None,
                              until_ms: float = None, include_details: bool = False,
                              limit: int = 50, min_total_ms: float = 0,
                              target_id: str = None, recording_id: str = None):
        """Read the browser monitor's recording and resource data.

        Readings are live by default. Recordings are written to
        data/browser/recordings/<id>.json every time a recording stops (the UI's
        Pause or browser_monitor_record); pass `recording_id` to read one from
        disk later, and view="recordings" to list the saved ones.

        Args:
            view: all, summary, events, functions, resources, or recordings.
            kinds: event kinds filter for events.
            min_duration_ms: minimum event duration for events.
            during_scroll: only inside/outside scroll windows for events.
            search: text match over event, target and handler for events.
            since_ms / until_ms: session-relative window for events.
            include_details: include handler source/stack and event data.
            limit: maximum events (or resource samples) to return.
            min_total_ms: minimum total time for functions.
            target_id: resource tab; defaults to the monitor's selected tab.
            recording_id: read a saved recording instead of the live session.
        """
        def content(result):
            return result.get("content", result) if isinstance(result, dict) else result

        view = str(view or "all").lower()
        if view == "recordings":
            return self.result({"recordings": self._perf_list_recordings(20)}, success=True)
        if view not in ("all", "summary", "events", "functions", "resources"):
            view = "all"

        if recording_id:
            saved = self._perf_load_session(recording_id)
            data = {"view": view, "recording_id": saved.get("id"),
                    "session_seconds": saved.get("session_seconds")}
            if view in ("all", "summary"):
                data["summary"] = self._perf_session_summary(saved)
            if view in ("all", "events"):
                rows = self._perf_event_rows(
                    saved.get("events") or [], kinds=kinds,
                    min_duration_ms=min_duration_ms, during_scroll=during_scroll,
                    search=search, since_ms=since_ms, until_ms=until_ms,
                    include_details=include_details, limit=limit)
                data["events"] = {"matched": rows["matched"],
                                  "returned": rows["returned"], "events": rows["events"]}
            if view in ("all", "functions"):
                data["functions"] = self._perf_session_functions(
                    saved, min_total_ms=min_total_ms, limit=limit,
                    since_ms=since_ms, until_ms=until_ms)
            if view in ("all", "resources"):
                data["resources"] = self._perf_session_resources(saved, limit=limit)
            return self.result(data, success=True)

        data = {"view": view, "recording": self._perf_recording,
                "session_seconds": self._perf_session_seconds(),
                "last_session_id": (self._perf_last_stop or {}).get("id")}
        if view in ("all", "summary"):
            data["summary"] = content(await self.browser_monitor_summary())
        if view in ("all", "events"):
            data["events"] = content(await self.browser_monitor_events(
                kinds=kinds, min_duration_ms=min_duration_ms, during_scroll=during_scroll,
                search=search, since_ms=since_ms, until_ms=until_ms,
                include_details=include_details, limit=limit))
        if view in ("all", "functions"):
            data["functions"] = content(await self.browser_monitor_functions(
                min_total_ms=min_total_ms, limit=limit,
                since_ms=since_ms, until_ms=until_ms))
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
