"""Low-overhead timing for outbound phone audio, without retaining audio data."""

import json
import time


class MonitoredWebSocket:
    """Measure when media packets are handed to the ASGI WebSocket transport.

    This does not measure when Exotel plays a packet on the phone. Gaps larger
    than 500 ms begin a new speech burst so caller turns do not count as jitter.
    """

    def __init__(self, websocket, sample_rate: int, clock=time.monotonic, report=None):
        self._websocket = websocket
        self._sample_rate = sample_rate
        self._clock = clock
        self._report = report
        self._last_report_at: float | None = None
        self._last_sent_at: float | None = None
        self._last_audio_seconds = 0.0
        self.media_packets = 0
        self.speech_bursts = 0
        self.late_packets = 0
        self.max_excess_gap_ms = 0.0
        self.max_send_ms = 0.0

    def __getattr__(self, name):
        return getattr(self._websocket, name)

    async def send_text(self, data: str) -> None:
        is_media = '"event": "media"' in data[:100]
        audio_seconds = 0.0
        if is_media:
            payload = json.loads(data).get("media", {}).get("payload", "")
            encoded_bytes = len(payload.rstrip("=")) * 3 // 4
            audio_seconds = encoded_bytes / (self._sample_rate * 2)
        started = self._clock()
        await self._websocket.send_text(data)
        finished = self._clock()
        if not is_media:
            return
        self.media_packets += 1
        self.max_send_ms = max(self.max_send_ms, (finished - started) * 1000)
        if self._last_sent_at is not None:
            gap = finished - self._last_sent_at
            if gap > 0.5:
                self.speech_bursts += 1
            else:
                excess_ms = max(0.0, (gap - self._last_audio_seconds) * 1000)
                self.max_excess_gap_ms = max(self.max_excess_gap_ms, excess_ms)
                if excess_ms > 80:
                    self.late_packets += 1
        else:
            self.speech_bursts = 1
        self._last_sent_at = finished
        self._last_audio_seconds = audio_seconds
        if self._last_report_at is None:
            self._last_report_at = finished
        elif self._report and finished - self._last_report_at >= 5:
            self._report(self.summary())
            self._last_report_at = finished

    def summary(self) -> dict[str, int]:
        return {
            "media_packets": self.media_packets,
            "speech_bursts": self.speech_bursts,
            "late_packets": self.late_packets,
            "max_excess_gap_ms": round(self.max_excess_gap_ms),
            "max_send_ms": round(self.max_send_ms),
        }
