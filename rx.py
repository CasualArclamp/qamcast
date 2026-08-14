"""QAMcast receiver.

    python rx.py                     open the UI, pick a device there
    python rx.py --profile WIDE      start listening immediately
    python rx.py --input tx.wav      decode a captured file

Listens on a sound card -- an SDR's audio output, for an off-air signal --
demodulates, decodes, and plays the audio.

Only the **profile** has to match the transmitter. Modulation, coding rate and
codec all travel in the frame header, so this configures itself from the first
frame it locks to -- including when that frame is forty minutes into a
broadcast that was already running.

The UI is at http://127.0.0.1:8732.
"""

from __future__ import annotations

import argparse
import collections
import sys
import threading
import time
import wave

import numpy as np

from qamcore import (codec, demodulator, framing, profiles, scope,
                     transport, webui)

# Before anything is decoded the constellation is unknown, so start at the
# floor; scope.symbol_budget raises it once the MODCOD arrives.
SCOPE_SYMBOLS = scope.SYMBOLS_MIN
SCOPE_HZ = 20             # UI refresh, independent of the block or frame rate
# Smaller blocks than the 4096 this used to read. The capture read blocks for
# a whole block, and that latency lands on everything downstream -- lock
# reporting, the audio hand-off, and how quickly a change on the wire shows up
# on the page. 1024 samples is ~21 ms at 48 kHz.
BLOCK = 1024
PROBE_MARGIN_DB = 2.0


class RateMeter:
    """Bits per second over a sliding window.

    The receiver is never told the bitrate -- nothing in the frame carries it,
    and there would be no reason to trust it if something did. What it can do
    is weigh the audio packets it delivers, which is the rate that actually
    arrived rather than the one the encoder was asked for. The two differ
    whenever the encoder is running VBR, dropping, or catching up.

    The window is a compromise: too short and Opus's per-packet variation
    swamps it, too long and it lags a MODCOD change by more than the change
    takes.
    """

    def __init__(self, window: float = 4.0):
        self.window = window
        self._events: collections.deque = collections.deque()
        self._bytes = 0
        self._first: float | None = None

    def add(self, nbytes: int, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if self._first is None:
            self._first = now
        self._events.append((now, nbytes))
        self._bytes += nbytes
        self._trim(now)

    def _trim(self, now: float) -> None:
        while self._events and now - self._events[0][0] > self.window:
            self._bytes -= self._events.popleft()[1]

    def rate(self, now: float | None = None) -> float | None:
        """Bits per second over the last ``window``, or None while warming up.

        Everything that arrived in the window, divided by the window -- not by
        the span from the oldest surviving packet to now. That span is shorter
        than the window by however long ago the oldest packet landed, and
        dividing by it reads high.

        It is not a small effect here, because packets do not trickle in one
        at a time: a frame's worth is delivered at once every 256 ms, so a 4 s
        window holds about 16 bursts spanning only 15 gaps. Measured against a
        known 32 kbps stream, the span version settled at 34.0 kbps -- a
        steady +6.2%, and above the channel's own net rate, which is how the
        error announced itself.
        """
        now = time.time() if now is None else now
        self._trim(now)
        # Below a full window the answer would be an extrapolation, and the
        # interleaver takes longer to fill than this anyway.
        if self._first is None or now - self._first < self.window:
            return None
        return self._bytes * 8.0 / self.window


class Receiver:
    def __init__(self, ffmpeg: str | None = None):
        self.ffmpeg = ffmpeg
        self.telemetry = webui.Telemetry()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state: dict = {"running": False}
        self._feed = None
        self._meta: dict = {}
        self._live: dict = {}
        self._last_seq = -1
        self._player = None
        self._volume = 1.0
        self._paused = False
        self.error: str | None = None

    # -- control ---------------------------------------------------------

    def start(self, cfg: dict) -> dict:
        if self._thread and self._thread.is_alive():
            return {"error": "already running"}
        try:
            # Same builder the transmitter uses, so a hand-set link is
            # described identically at both ends -- which it has to be, since
            # none of these travel in the header.
            from tx import build_profile
            profile = build_profile(cfg)
        except (ValueError, KeyError) as exc:
            return {"error": str(exc)}
        self.error = None
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(profile, cfg.get("device", ""), cfg.get("outdev", ""),
                  cfg.get("record") or None),
            daemon=True)
        self._thread.start()
        return {"ok": True}

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        return {"ok": True}

    def control(self, msg: dict) -> dict:
        cmd = msg.get("cmd")
        if cmd == "start":
            return self.start(msg)
        if cmd == "stop":
            return self.stop()
        if cmd == "profiles":
            return {"profiles": webui.profile_list(),
                    "modcods": webui.modcod_list(),
                    "sample_rates": webui.sample_rates(),
                    "symbol_rates": {str(r): webui.symbol_rates(r)
                                     for r in webui.sample_rates()}}
        if cmd == "solve":
            from tx import solve
            return solve(msg)
        if cmd == "devices":
            from tx import list_devices
            every = bool(msg.get("all"))
            return {"inputs": list_devices(output=False, every_api=every),
                    "outputs": list_devices(output=True, every_api=every)}
        if cmd in ("volume", "pause"):
            return self.set_monitoring(msg)
        return {"error": f"unknown command {cmd!r}"}

    def set_monitoring(self, msg: dict) -> dict:
        """Speaker volume and pause. Held here as well as pushed at the
        player, so a device opened later starts where the listener left it."""
        if "volume" in msg:
            try:
                self._volume = min(1.0, max(0.0, float(msg["volume"])))
            except (TypeError, ValueError):
                return {"error": f"bad volume {msg['volume']!r}"}
        if "paused" in msg:
            self._paused = bool(msg["paused"])
        player = self._player
        if player is not None:
            player.set_volume(self._volume)
            player.set_paused(self._paused)
        return {"ok": True, "volume": self._volume, "paused": self._paused}

    # -- worker ----------------------------------------------------------

    def _run(self, profile, device, outdev, record=None) -> None:
        src = player = dec = None
        try:
            src = open_input(device, profile.sample_rate)
            dem = demodulator.Demodulator(profile)
            player = open_player(outdev, codec.SAMPLE_RATE, record)
            # Carry the listener's settings across a restart rather than
            # springing full volume on them when the device reopens.
            player.set_volume(self._volume)
            player.set_paused(self._paused)
            self._player = player

            chain: transport.ReceiveChain | None = None
            modcod = None
            codec_id = None
            config: bytes | None = None
            pad = transport.Pad()
            frames = locked_frames = 0
            last_result = None
            audio_seconds = 0.0
            meter = RateMeter()

            feed = scope.ScopeFeed(profile.sample_rate)
            self._feed = feed
            self._meta = {"profile": profile, "dem": dem}
            self._live = {}
            publisher = scope.Publisher(self.telemetry, self._build_state, SCOPE_HZ)
            publisher.start()

            while not self._stop.is_set():
                block = src.read(BLOCK)
                if block is None:
                    break
                if not len(block):
                    time.sleep(0.01)
                    continue
                feed.push_audio(block)
                dem.feed(block)

                for r in dem.frames():
                    frames += 1
                    last_result = r
                    if len(r.symbols):
                        # Budget follows the constellation the transmitter is
                        # using, which is only known once a frame decodes --
                        # so this tracks a MODCOD change rather than being
                        # fixed when the receiver started.
                        want = (scope.symbol_budget(r.modcod.bits_per_symbol)
                                if r.modcod else SCOPE_SYMBOLS)
                        feed.push_symbols(r.symbols[::max(
                            1, len(r.symbols) // want)])
                    if not r.locked or r.modcod is None or r.payload is None:
                        continue
                    locked_frames += 1

                    # Reconfigure whenever the transmitter's MODCOD changes.
                    # It restarts the outer chain, which costs an interleaver
                    # depth -- but MODCOD is an operator setting that changes
                    # rarely, so paying for it here is cheaper than carrying
                    # machinery to avoid it.
                    if modcod is None or r.modcod.index != modcod.index:
                        modcod = r.modcod
                        chain = transport.ReceiveChain(profile, modcod)

                    for ptype, payload in chain.push_frame(
                            r.payload, r.header.il_phase, r.header.rs_phase,
                            r.header.frame_count):
                        if ptype == transport.PKT_CONFIG:
                            if config != payload:
                                config = payload
                                dec = restart_decoder(dec, r.header.codec, config,
                                                      self.ffmpeg)
                        elif ptype == transport.PKT_PAD:
                            got = transport.Pad.decode(payload)
                            if got:
                                pad = got
                        elif ptype == transport.PKT_AUDIO:
                            if dec is None and r.header.codec != framing.CODEC_OPUS:
                                # AAC needs no out-of-band config: its ADTS
                                # headers already describe the stream.
                                dec = restart_decoder(None, r.header.codec, None,
                                                      self.ffmpeg)
                            if dec is not None:
                                dec.feed([payload])
                            codec_id = r.header.codec
                            meter.add(len(payload))

                if dec is not None:
                    pcm = dec.pcm()
                    if pcm:
                        audio_seconds += len(pcm) / 2 / codec.CHANNELS / codec.SAMPLE_RATE
                        player.write(pcm)

                self._live = {
                    "chain": chain, "result": last_result, "modcod": modcod,
                    "codec_id": codec_id, "pad": pad, "frames": frames,
                    "buffered": player.buffered, "audio_rate": meter.rate(),
                }
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                publisher.stop()
            except NameError:
                pass
            self._feed = None
            self._player = None
            for obj in (src, dec, player):
                try:
                    if obj:
                        obj.close() if hasattr(obj, "close") else obj.stop()
                except Exception:
                    pass
            self._state = {**self._state, "running": False, "locked": False,
                           "error": self.error}
            self.telemetry.publish(self._state)

    def _build_state(self) -> dict | None:
        """Snapshot for the UI. Called by the publisher thread, not the loop."""
        feed = self._feed
        meta = self._meta
        if feed is None or not meta:
            return None
        profile = meta["profile"]
        dem = meta["dem"]
        live = dict(self._live)
        r = live.get("result")
        chain = live.get("chain")
        modcod = live.get("modcod")
        pad = live.get("pad") or transport.Pad()

        shot = feed.snapshot(self._last_seq)
        self._last_seq = shot.get("seq", self._last_seq)

        lo, hi = profile.band
        nyq = profile.sample_rate / 2.0
        locked = bool(r and r.locked)
        state = {
            "running": True,
            "profile": profile.name,
            "footprint": f"{profile.symbol_rate} Bd \u00b7 "
                         f"{lo/1000:.1f}-{hi/1000:.1f} kHz",
            "locked": locked,
            "corr": r.corr_peak if r else None,
            "modcod": str(modcod) if modcod else None,
            "codec": framing.CODEC_NAMES.get(live.get("codec_id"))
                     if live.get("codec_id") is not None else None,
            # Measured off the packets that arrived, not read from the frame:
            # nothing on the wire states the bitrate, so this is the only
            # honest answer the receiver has.
            "audio_rate": live.get("audio_rate"),
            **(codec.features(live["codec_id"])
               if live.get("codec_id") is not None else {"sbr": False, "ps": False}),
            "evm": r.evm_db if locked else None,
            "freq": r.freq_offset_hz if locked else None,
            "ppm": r.timing_ppm if locked else None,
            "frames": live.get("frames", 0),
            "header_errors": dem.stats.headers_failed,
            "rs_corrected": chain.stats.rs_corrected if chain else 0,
            "rs_failed": chain.stats.rs_failed if chain else 0,
            "resyncs": dem.stats.resyncs,
            "bridged": chain.stats.bridged_frames if chain else 0,
            "chain_resyncs": chain.stats.resyncs if chain else 0,
            "audio_buffer": live.get("buffered"),
            "volume": self._volume,
            "paused": self._paused,
            "pad_station": pad.station,
            "pad_title": pad.title,
            "pad_artist": pad.artist,
            "band": [lo / nyq, hi / nyq],
            "error": self.error,
        }
        state.update(shot)

        if locked and r.evm_db:
            best = profile.modcod_for_evm(r.evm_db, PROBE_MARGIN_DB)
            state["probe_snr"] = r.evm_db
            if best is not None:
                state["probe_modcod"] = str(best)
                state["probe_rate"] = profile.net_bitrate(best)
                state["probe_margin"] = r.evm_db - best.required_evm_db
            else:
                state["probe_modcod"] = "none - below the lowest rung"

        if chain is not None:
            state["fill"] = chain.fill_fraction
            state["fill_note"] = (f"{chain.fill_seconds:.1f} s of diversity delay; "
                                  f"audio starts once filled")
        # Keep the last snapshot where callers that are not a browser can see
        # it -- the CLI and tools/selftest.py both read _state, and when this
        # moved onto a publisher thread they silently started reading the
        # initial "not running" stub instead.
        self._state = state
        return state


def restart_decoder(old, codec_id: int, config: bytes | None, ffmpeg):
    if old is not None:
        old.stop()
    try:
        dec = codec.Decoder(codec_id, config, ffmpeg=ffmpeg)
        dec.start()
        return dec
    except codec.CodecError:
        return None


# --------------------------------------------------------------------------
# Audio in and out
# --------------------------------------------------------------------------

class WavSource:
    """Reads a captured file, paced to real time so the UI behaves normally."""

    def __init__(self, path: str, rate: int):
        self._w = wave.open(path, "rb")
        if self._w.getframerate() != rate:
            raise ValueError(
                f"{path} is {self._w.getframerate()} Hz but the profile needs "
                f"{rate} Hz -- the profile must match how it was recorded")
        self._ch = self._w.getnchannels()
        self._t = time.time()
        self._read = 0
        self._rate = rate

    def read(self, n: int):
        raw = self._w.readframes(n)
        if not raw:
            return None
        x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
        if self._ch > 1:
            x = x[::self._ch]
        self._read += len(x)
        delay = self._t + self._read / self._rate - time.time()
        if delay > 0:
            time.sleep(min(delay, 0.5))
        return x

    def close(self) -> None:
        self._w.close()


class DeviceSource:
    def __init__(self, index: int | None, rate: int):
        import sounddevice as sd
        self.stream = sd.InputStream(samplerate=rate, channels=1,
                                     dtype="float32", device=index,
                                     blocksize=0, latency="high")
        self.stream.start()

    def read(self, n: int):
        data, _overflow = self.stream.read(n)
        return data[:, 0].astype(np.float64)

    def close(self) -> None:
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass


class Monitoring:
    """Volume and pause, for the thing actually making sound.

    Deliberately not applied to a recording. What is written to a file is the
    audio the link recovered, and quietening it because someone turned the
    monitoring down would make the file evidence of the wrong thing.
    """

    volume = 1.0
    paused = False

    def set_volume(self, value: float) -> None:
        self.volume = min(1.0, max(0.0, float(value)))

    def set_paused(self, value: bool) -> None:
        self.paused = bool(value)


class NullPlayer(Monitoring):
    buffered = 0.0

    def write(self, pcm: bytes) -> None:
        pass

    def close(self) -> None:
        pass


class WavPlayer(Monitoring):
    """Writes recovered audio to a file instead of (or as well as) playing it.

    The point of the modem is the audio at this end, so being able to keep it
    is how you check the link did its job rather than merely reporting that it
    did.
    """

    def __init__(self, path: str, rate: int, also=None):
        self._w = wave.open(path, "wb")
        self._w.setnchannels(codec.CHANNELS)
        self._w.setsampwidth(2)
        self._w.setframerate(rate)
        self._also = also
        self._rate = rate
        self._n = 0

    def write(self, pcm: bytes) -> None:
        self._w.writeframes(pcm)
        self._n += len(pcm)
        if self._also:
            self._also.write(pcm)

    @property
    def buffered(self) -> float:
        return self._also.buffered if self._also else self._n / 4 / self._rate

    # The file keeps the audio as recovered; only the speaker follows these.
    def set_volume(self, value: float) -> None:
        if self._also:
            self._also.set_volume(value)

    def set_paused(self, value: bool) -> None:
        if self._also:
            self._also.set_paused(value)

    def close(self) -> None:
        self._w.close()
        if self._also:
            self._also.close()


class DevicePlayer(Monitoring):
    """Plays decoded PCM, with a small jitter buffer.

    The modem delivers audio in frame-sized bursts, not smoothly, so writing
    straight to the device would underrun constantly.
    """

    def __init__(self, index: int | None, rate: int):
        import sounddevice as sd
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._rate = rate

        def callback(outdata, frames, _time, _status):
            need = frames * 2 * 2
            with self._lock:
                take = self._buf[:need]
                del self._buf[:need]
                gain, paused = self.volume, self.paused
            if len(take) < need:
                take = bytes(take) + bytes(need - len(take))
            block = np.frombuffer(bytes(take), dtype="<i2").reshape(-1, 2)
            # Paused still consumes the buffer. This is a live broadcast with
            # no way to ask for the missing seconds back, so holding the audio
            # would only grow the queue and put the listener further behind
            # with every second paused. Silence now, live again on resume.
            if paused:
                block = np.zeros_like(block)
            elif gain < 0.999:
                block = (block.astype(np.int32) * int(gain * 4096) >> 12
                         ).clip(-32768, 32767).astype(np.int16)
            outdata[:] = block

        self.stream = sd.OutputStream(samplerate=rate, channels=2, dtype="int16",
                                      device=index, callback=callback,
                                      blocksize=1024, latency="high")
        self.stream.start()

    def write(self, pcm: bytes) -> None:
        with self._lock:
            self._buf.extend(pcm)

    @property
    def buffered(self) -> float:
        with self._lock:
            return len(self._buf) / 4 / self._rate

    def close(self) -> None:
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass


def open_input(device: str, rate: int):
    if not device or device == "wav":
        return WavSource("tx.wav", rate)
    return DeviceSource(int(device), rate)


def open_player(device: str, rate: int, record: str | None = None):
    live: object = NullPlayer()
    if device and device != "none":
        try:
            live = DevicePlayer(int(device), rate)
        except Exception:
            live = NullPlayer()
    if record:
        return WavPlayer(record, rate, also=live if not isinstance(live, NullPlayer) else None)
    return live


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default=None,
                    help="preset name, or CUSTOM with --symbol-rate etc")
    ap.add_argument("--sample-rate", type=int, default=48000,
                    help="card rate, used with --profile CUSTOM")
    ap.add_argument("--symbol-rate", type=int, default=None, help="baud")
    ap.add_argument("--rolloff", type=float, default=0.25)
    ap.add_argument("--carrier", type=float, default=None)
    ap.add_argument("--pilot-spacing", type=int, default=64, choices=[32, 64, 128])
    ap.add_argument("--device", default="", help="input device index, or 'wav'")
    ap.add_argument("--output", default="", help="output device index, or 'none'")
    ap.add_argument("--input", dest="infile", default=None, help="decode a wav file")
    ap.add_argument("--record", default=None, help="write recovered audio to a wav")
    ap.add_argument("--port", type=int, default=8732)
    ap.add_argument("--ffmpeg", default=None)
    ap.add_argument("--no-ui", action="store_true")
    ap.add_argument("--open", action="store_true",
                    help="open the UI in a browser once it is listening")
    ap.add_argument("--list-devices", action="store_true")
    a = ap.parse_args()

    if a.list_devices:
        from tx import list_devices
        print("inputs:")
        for d in list_devices(output=False):
            print(f"{d['index']:>3}  {d['name']}{'  (default)' if d['default'] else ''}")
        print("outputs:")
        for d in list_devices(output=True):
            print(f"{d['index']:>3}  {d['name']}{'  (default)' if d['default'] else ''}")
        return 0

    rx = Receiver(ffmpeg=a.ffmpeg)
    if not a.no_ui:
        webui.serve("rx.html", rx.telemetry, rx.control, port=a.port)
        print(f"receive UI at http://127.0.0.1:{a.port}")
        # Opened here rather than from the launcher: the server is already
        # listening by this point, so the browser cannot beat it to the port.
        if a.open:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{a.port}")
        # Publish an idle snapshot so a browser that connects before anything
        # is running shows "connected" rather than sitting on "connecting".
        rx.telemetry.publish(rx._state)

    if a.profile:
        device = "wav" if a.infile else a.device
        res = rx.start({"profile": a.profile, "device": device,
                        "outdev": a.output, "record": a.record,
                        "sample_rate": a.sample_rate, "symbol_rate": a.symbol_rate,
                        "rolloff": a.rolloff, "carrier": a.carrier,
                        "pilot_spacing": a.pilot_spacing})
        if res.get("error"):
            print(res["error"], file=sys.stderr)
            return 1
        print(f"listening on {a.profile}")
    elif a.no_ui:
        print("nothing to do: give --profile or drop --no-ui", file=sys.stderr)
        return 1

    try:
        while True:
            time.sleep(0.5)
            if rx.error:
                print(f"error: {rx.error}", file=sys.stderr)
                rx.error = None
    except KeyboardInterrupt:
        rx.stop()
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
