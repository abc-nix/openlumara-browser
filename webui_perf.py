"""WebUI routes for the browser monitor tab."""

import channels.webui.api as webui

from .perf import PerfMonitor


class WebuiPerfRoutes(PerfMonitor):

    @webui.route("perf/targets")
    async def _route_perf_targets(self, body=None, query=None):
        return await self._perf_targets()

    @webui.route("perf/select", method="POST")
    async def _route_perf_select(self, body=None, query=None):
        return await self._perf_select((body or {}).get("target_id"))

    @webui.route("perf/summary")
    async def _route_perf_summary(self, body=None, query=None):
        q = query or {}
        return await self._perf_summary(q.get("target_id"), q.get("limit", 180))

    @webui.route("perf/clear", method="POST")
    async def _route_perf_clear(self, body=None, query=None):
        return await self._perf_clear((body or {}).get("target_id"))

    @webui.route("perf/record", method="POST")
    async def _route_perf_record(self, body=None, query=None):
        return await self._perf_set_recording((body or {}).get("recording"))

    @webui.route("perf/events")
    async def _route_perf_events(self, body=None, query=None):
        q = query or {}
        return await self._perf_events(q.get("target_id"), q.get("limit", 300))

    @webui.route("perf/events/clear", method="POST")
    async def _route_perf_events_clear(self, body=None, query=None):
        return await self._perf_clear_events()
