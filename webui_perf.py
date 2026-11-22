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

    @webui.route("perf/recordings")
    async def _route_perf_recordings(self, body=None, query=None):
        return {"recordings": self._perf_list_recordings(30)}

    @webui.route("perf/recording")
    async def _route_perf_recording(self, body=None, query=None):
        session_id = (query or {}).get("id")
        return self._perf_recording_for_ui(session_id)

    @webui.route("perf/functions")
    async def _route_perf_functions(self, body=None, query=None):
        q = query or {}

        def number(key):
            try:
                return float(q.get(key))
            except (TypeError, ValueError):
                return None

        recording_id = q.get("id")
        if recording_id:
            return self._perf_session_functions(
                self._perf_load_session(recording_id),
                min_total_ms=number("min_total_ms") or 0,
                limit=int(number("limit") or 25),
                since_ms=number("since_ms"), until_ms=number("until_ms"))
        return await self.browser_monitor_functions(
            min_total_ms=number("min_total_ms") or 0,
            limit=int(number("limit") or 25),
            since_ms=number("since_ms"), until_ms=number("until_ms"))

    @webui.route("perf/recordings/delete", method="POST")
    async def _route_perf_recordings_delete(self, body=None, query=None):
        return self._perf_delete_recording((body or {}).get("id"))
