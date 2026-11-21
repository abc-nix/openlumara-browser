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

AGENT_JS = r"""
(() => {
  if (window.__olMonitor) return "exists";
  const M = window.__olMonitor = {
    enabled: true, installed: false, events: [], cap: 1200,
    sessionStart: performance.now(), scrollAt: 0, seq: 0,
    push(e) {
      if (!this.enabled) return;
      this.seq += 1;
      e.id = 'e' + this.seq;
      e.rel_ms = Math.round((performance.now() - this.sessionStart) * 10) / 10;
      e.wall_ms = Date.now();
      e.during_scroll = !!e.during_scroll || (performance.now() - this.scrollAt < 200);
      e.severity = e.severity || (Number(e.duration) > 50 ? 'warn' : 'info');
      e.tab = this.tab || null;
      this.events.push(e);
      if (this.events.length > this.cap) this.events.shift();
    },
    start() { this.enabled = true; this.sessionStart = performance.now(); this.events = []; },
    stop() { this.enabled = false; },
    drain() { const out = this.events; this.events = []; return out; },
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
    new PerformanceObserver((list) => list.getEntries().forEach((e) =>
      M.push({kind: 'longtask', name: e.name || 'longtask', duration: e.duration,
              target: 'main thread', handler: null, data: {},
              severity: e.duration > 50 ? 'warn' : 'info'})
    )).observe({entryTypes: ['longtask', 'long-animation-frame']});
  } catch (e) {}

  try {
    new PerformanceObserver((list) => list.getEntries().forEach((e) =>
      M.push({kind: 'input', name: e.name, duration: e.duration,
              target: 'event timing', handler: null,
              data: {interactionId: e.interactionId, processingStart: e.processingStart, processingEnd: e.processingEnd}})
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
    const wrapped = new WeakMap();
    const types = new Set(['scroll', 'wheel', 'pointerdown', 'pointermove', 'pointerup', 'keydown', 'click', 'resize']);

    EventTarget.prototype.addEventListener = function(type, listener, options) {
      if (!types.has(type) || typeof listener !== 'function') {
        return origAdd.call(this, type, listener, options);
      }
      let w = wrapped.get(listener);
      if (!w) {
        w = function(ev) {
          if (!M.enabled) return listener.call(this, ev);
          const t0 = performance.now();
          try { return listener.call(this, ev); }
          finally {
            const dur = performance.now() - t0;
            let stack = null;
            if (dur > 4) {
              try { stack = String(new Error().stack).split('\n').slice(2, 6).map((s) => s.trim()); }
              catch (e) {}
            }
            M.push({kind: 'input', name: type, duration: dur, target: describe(this),
                    handler: {name: listener.name || 'anonymous',
                              source: String(listener).slice(0, 300), stack: stack},
                    data: {}, during_scroll: performance.now() - M.scrollAt < 200});
          }
        };
        w.__olOriginal = listener;
        wrapped.set(listener, w);
      }
      return origAdd.call(this, type, w, options);
    };

    EventTarget.prototype.removeEventListener = function(type, listener, options) {
      return origRemove.call(this, type, wrapped.get(listener) || listener, options);
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
    M.restore = () => {
      EventTarget.prototype.addEventListener = origAdd;
      EventTarget.prototype.removeEventListener = origRemove;
      window.requestAnimationFrame = origRaf;
    };
  }
  return "installed";
})()
"""

AGENT_START_JS = "window.__olMonitor && window.__olMonitor.start(); 'ok'"

PROBE_JS = r"""
(() => {
  if (!window.__olPerf) {
    const s = {frames: 0, fps: 0, last: performance.now(), long: 0, longest: 0};
    window.__olPerf = s;
    try {
      new PerformanceObserver((list) => {
        for (const e of list.getEntries()) {
          s.long += 1;
          s.longest = Math.max(s.longest, e.duration || 0);
        }
      }).observe({entryTypes: ['longtask']});
    } catch (e) {}
    const tick = (t) => {
      s.frames += 1;
      if (t - s.last >= 1000) {
        s.fps = Math.round(s.frames * 1000 / (t - s.last));
        s.frames = 0; s.last = t;
      }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }
  return JSON.stringify({fps: window.__olPerf.fps, long: window.__olPerf.long,
                         longest: Math.round(window.__olPerf.longest)});
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
        "script_pct": round(100 * _rate(prev, cur, "ScriptDuration", dt), 2),
        "task_pct": round(100 * _rate(prev, cur, "TaskDuration", dt), 2),
        "task_other_pct": round(100 * _rate(prev, cur, "TaskOtherDuration", dt), 2),
        "fps": int(_number(probe.get("fps"))),
        "long_tasks": int(_number(probe.get("long"))),
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
        self._perf_recording = False
        self._perf_session_start = 0
        self._perf_event_log = deque(maxlen=max(int(self.config.get("perf_events_max", 5000) or 5000), 100))
        self._perf_event_seq = 0
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
            try:
                await asyncio.wait_for(self._perf_stop.wait(), timeout=self._perf_interval)
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
        prev = self._perf_last_metrics.get(target)
        if prev is not None:
            dt = now - prev[0]
            if dt > 0:
                sample = build_sample(prev[1], cur, dt, probe)
                bucket = self._perf_samples.get(target)
                if bucket is None:
                    bucket = deque(maxlen=self._perf_history)
                    self._perf_samples[target] = bucket
                bucket.append(sample)
        self._perf_last_metrics[target] = (now, cur)

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
            res = await self._cdp.cmd(
                "Runtime.evaluate",
                {"expression": "window.__olMonitor ? JSON.stringify(window.__olMonitor.drain()) : '[]'",
                 "returnByValue": True},
                sid, timeout=2.0,
            )
            value = ((res or {}).get("result") or {}).get("value")
            items = json.loads(value) if value else []
            for item in items:
                self._perf_append_event(item)
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
        self._perf_samples[target_id] = deque(maxlen=self._perf_history)
        if self._perf_recording:
            await self._perf_agent_start()
        return {"selected": target_id}

    def _perf_session_seconds(self):
        if self._perf_session_start <= 0:
            return 0
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
        else:
            self._perf_samples.clear()
            self._perf_last_metrics.clear()
        return {"cleared": target_id or "all"}

    async def _perf_set_recording(self, recording):
        if recording:
            self._perf_event_log.clear()
            self._perf_event_seq = 0
            self._perf_session_start = time.time() * 1000
            self._perf_recording = True
            await self._perf_agent_start()
        else:
            self._perf_recording = False
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
        return {
            "connected": self._is_connected(),
            "recording": self._perf_recording,
            "session_seconds": self._perf_session_seconds(),
            "event_count": len(self._perf_event_log),
            "events": events[-max(int(limit or 300), 1):],
        }

    async def _perf_clear_events(self):
        self._perf_event_log.clear()
        self._perf_session_start = 0
        self._perf_recording = False
        return {"recording": False, "event_count": 0, "session_seconds": 0}


def core_detail_error(exc):
    try:
        import core
        return core.detail_error(exc)
    except Exception:
        return str(exc)
