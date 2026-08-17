"""QAMcast transmitter.

    python tx.py                                   open the UI, configure there
    python tx.py --source live.mp3 --bitrate 128k  start immediately
    python tx.py --list-rates                      print the rate card

Reads any source ffmpeg can open -- Icecast, Shoutcast, HLS, a local file --
encodes it, and puts the modulated passband audio out of a sound card.

Intended for feeding a broadcast FM transmitter, with the receiver working
from an SDR at the other end. The output is ordinary mono audio, so it goes
wherever station audio goes.

The UI is at http://127.0.0.1:8731. Only the profile has to match at the far
end; modulation, coding and codec all travel in the frame header.
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
import wave

import numpy as np

from qamcore import (channel as CH, codec, framing, icy, linkkey, modulator,
                     ofdm, profiles, scope, streams, transport, webui)

# Config and PAD are retransmitted this often. They cannot be sent once: a
# receiver joining mid-broadcast has missed anything at the start, and so has
# the interleaver's fill region, which discards the first several seconds of
# the stream by design.
REPEAT_SECONDS = 1.0
SCOPE_HZ = 20             # UI refresh, independent of the frame rate

# Ceiling on encoded audio waiting for the channel, in packets. At 20 ms per
# packet this is about six seconds.
#
# It has to be bounded, and the overflow has to be dropped from the *front*.
# An Icecast server bursts several seconds of buffer the moment you connect,
# and any mismatch between encoder and channel accumulates forever after. An
# unbounded queue turns that into unbounded latency: the modem keeps
# transmitting, further and further behind live, with no way to catch up.
# Dropping the oldest keeps the broadcast current, which for live radio is the
# thing that matters.
MAX_PENDING = 300


def parse_rate(text: str) -> int:
    t = str(text).strip().lower().rstrip("bps").rstrip()
    mult = 1000 if t.endswith("k") else 1
    return int(float(t.rstrip("k")) * mult)


def _shutdown(*objs) -> None:
    """Close or stop each of these, whatever it takes, without raising.

    Used by the worker on its way out and again by stop(). Running it twice is
    harmless; running it only in the worker is not enough, because the worker
    cannot close a device it is blocked writing to.
    """
    for obj in objs:
        if obj is None:
            continue
        for name in ("close", "stop"):
            fn = getattr(obj, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
                break


class Transmitter:
    def __init__(self, ffmpeg: str | None = None):
        self.ffmpeg = ffmpeg
        self.telemetry = webui.Telemetry()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pad = transport.Pad("QAM RADIO", "", "")
        self._chan_cfg = CH.PRESETS["clean"]
        self._state: dict = {"running": False}
        self._feed = None
        self._meta: dict = {}
        self._live: dict = {}
        self._last_seq = -1
        self._probe: dict | None = None
        # The encoder and the output device, held here as well as in the worker
        # so that stop() can shut them down without the worker's cooperation.
        self._enc = None
        self._sink = None
        self._icy: icy.Poller | None = None
        self._meta_swap = False
        self._nowplaying: dict | None = None
        self._source = ""
        self._drive_db = 0.0
        self._drive = 1.0
        self.error: str | None = None

    # -- control ---------------------------------------------------------

    def start(self, cfg: dict) -> dict:
        if self._thread and self._thread.is_alive():
            return {"error": "already running"}
        source = (cfg.get("source") or "").strip()
        if not source:
            return {"error": "no source given"}
        # Passthrough copies the source's own packets instead of re-encoding,
        # so the source decides the bitrate and the codec, not the settings.
        # Probing here rather than trusting what the page sent means the rate
        # the capacity check runs against is the rate that will actually
        # arrive -- a stream that turns out to be 128k must not be started on
        # a 96k link because a dropdown said 64k.
        passthrough = None
        if cfg.get("passthrough"):
            try:
                info = codec.probe(source, ffmpeg=self.ffmpeg)
            except codec.CodecError as exc:
                return {"error": f"cannot pass through: {exc}"}
            ok = info["passthrough"]
            if not ok["ok"]:
                return {"error": f"cannot pass through: {ok['reason']}"}
            passthrough = ok["codec_id"]
            cfg = {**cfg, "bitrate": info["bitrate"] or cfg.get("bitrate")}
            self._probe = info

        try:
            profile = build_profile(cfg)
            bitrate = int(cfg.get("bitrate") or 128000)
            # An explicit MODCOD wins. Without one, pick the cheapest rung that
            # carries the requested bitrate -- the old behaviour, kept so the
            # simple path stays simple.
            modcod = pick_modcod(cfg, profile, bitrate)
        except (ValueError, KeyError) as exc:
            return {"error": str(exc)}

        # The channel has to carry the audio *and* the PAD, the codec config
        # and a length prefix on every packet. Starting with no headroom does
        # not degrade gracefully -- the encoder simply outruns the channel and
        # the queue grows until something gives, which reads as "packets
        # overflowing" rather than as the rate mistake it is.
        spare = profile.net_bitrate(modcod) - bitrate
        if spare < profiles.PAD_RESERVE_BPS:
            fix = ("Pick a lower-rate feed from this station, or turn "
                   "passthrough off and re-encode." if passthrough is not None
                   else "Lower the bitrate, or raise the symbol rate or MODCOD.")
            return {"error": (
                f"{profile.name} at {modcod} carries "
                f"{profile.net_bitrate(modcod)/1000:.1f} kbps, and "
                f"{bitrate/1000:.0f} kbps of audio leaves {spare/1000:.1f} kbps "
                f"for metadata and packet headers -- at least "
                f"{profiles.PAD_RESERVE_BPS/1000:.1f} kbps is needed. {fix}")}
        self._pad = transport.Pad(cfg.get("station", ""), cfg.get("title", ""),
                                  cfg.get("artist", ""))
        self._start_metadata(source, cfg)
        self.error = None
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(source, cfg.get("codec", "opus"), bitrate, profile, modcod,
                  cfg.get("device", ""), passthrough),
            daemon=True)
        self._thread.start()
        return {"ok": True,
                "modcod": modcod.label_for(profile.constellation_family),
                "passthrough": passthrough is not None,
                "detected": self._probe.get("label") if self._probe else None}

    def stop(self) -> dict:
        """Stop, and mean it.

        The worker shuts down its own encoder and output on the way out, which
        is enough when it is running and not enough when it is stuck -- an
        output stream whose device has gone away blocks in write() and never
        sees the stop flag. Joining with a timeout and returning ok then left
        the sound card open and ffmpeg running, and said nothing about it.

        The follow is governed by its own checkbox, not by whether anything is
        on air -- so stopping the transmitter leaves the panel still showing
        what the station is playing.
        """
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        stuck = bool(self._thread and self._thread.is_alive())
        _shutdown(self._enc, self._sink)
        self._enc = self._sink = None
        if stuck and self._thread:
            self._thread.join(timeout=3)
            stuck = self._thread.is_alive()
        if stuck:
            return {"ok": True, "note": "encoder and output closed; the worker "
                                        "had to be abandoned rather than joined"}
        return {"ok": True}

    def set_pad(self, cfg: dict) -> dict:
        with self._lock:
            self._pad = transport.Pad(cfg.get("station", ""), cfg.get("title", ""),
                                      cfg.get("artist", ""))
        return {"ok": True}

    # -- now playing -----------------------------------------------------

    def _start_metadata(self, source: str, cfg: dict) -> None:
        """Follow the station's own song metadata, if it has any.

        Runs whether or not anything is being transmitted, so the panel is
        already current when Start is pressed rather than stale until the
        first poll after it.

        Only for HTTP sources: ICY metadata is an HTTP-layer thing, and a
        local file has nothing to poll. The station name stays whatever the
        operator typed unless they left it blank -- a broadcaster relaying a
        stream is not necessarily happy to be renamed by it.
        """
        source = str(source or "")
        if source:
            self._source = source
        if not cfg.get("auto_metadata"):
            self._stop_metadata()
            return
        if not source.lower().startswith(("http://", "https://")):
            self._stop_metadata()
            self._nowplaying = None
            return
        self._meta_swap = bool(cfg.get("swap_artist", self._meta_swap))
        # Already following this stream: keep it rather than throwing away the
        # song in hand and waiting a whole poll for the next one. Start()
        # calls through here too, so going on air does not interrupt a follow
        # that was already running.
        if self._icy is not None and self._icy.url == source:
            self._icy.set_swap(self._meta_swap)
            return
        self._stop_metadata()
        self._icy = icy.Poller(source, self._on_metadata,
                               swap=self._meta_swap)
        self._icy.start()

    def _stop_metadata(self) -> None:
        if self._icy is not None:
            self._icy.stop()
            self._icy = None

    def _on_metadata(self, np) -> None:
        with self._lock:
            station = self._pad.station or np.station
            self._pad = transport.Pad(station, np.title, np.artist)
            self._nowplaying = np.as_dict()
        # On air, the 20 Hz publisher carries this out with everything else.
        # Idle, nothing else is publishing at all, so a song change would not
        # reach the page until something else happened to push a state.
        if not (self._thread and self._thread.is_alive()):
            self._state = {**self._state, "running": False,
                           "nowplaying": self._nowplaying,
                           "pad_station": self._pad.station,
                           "pad_title": self._pad.title,
                           "pad_artist": self._pad.artist,
                           "meta_error": self._icy.error if self._icy else None}
            self.telemetry.publish(self._state)

    def set_metadata(self, cfg: dict) -> dict:
        """Turn stream metadata on or off, or flip the artist/title order."""
        if "swap_artist" in cfg:
            self._meta_swap = bool(cfg["swap_artist"])
            if self._icy is not None:
                self._icy.set_swap(self._meta_swap)
        # Turning it on has to *start* the poller, not merely record the
        # intention. Without this, ticking the box after Start filled the
        # fields once from a one-shot read and then nothing ever polled again,
        # so every later song had to be pushed out by hand.
        if "auto_metadata" in cfg:
            self._start_metadata(
                cfg.get("source") or self._source,
                {"auto_metadata": cfg["auto_metadata"],
                 "swap_artist": self._meta_swap})
        return {"ok": True, "nowplaying": self._nowplaying,
                "swap_artist": self._meta_swap,
                "following": self._icy is not None}

    def nowplaying(self, msg: dict) -> dict:
        """One-shot read, so the page can show a song before going on air."""
        src = str(msg.get("source") or "").strip()
        if not src:
            return {"error": "no source"}
        try:
            np = icy.fetch(src, swap=bool(msg.get("swap_artist")))
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        return np.as_dict()

    # Range on the drive. Down is for a transmitter input that clips before
    # full scale; up is bounded because the modulator already leaves 10 dB of
    # headroom for the peak-to-average ratio, and spending all of it is how
    # 256QAM gets folded across the constellation at once.
    DRIVE_MIN_DB = -30.0
    DRIVE_MAX_DB = 10.0

    def set_drive(self, cfg: dict) -> dict:
        """Output level, in dB relative to the modulator's own -15 dBFS."""
        if "drive_db" in cfg:
            try:
                db = float(cfg["drive_db"])
            except (TypeError, ValueError):
                return {"error": f"bad drive {cfg['drive_db']!r}"}
            db = min(self.DRIVE_MAX_DB, max(self.DRIVE_MIN_DB, db))
            with self._lock:
                self._drive_db = db
                self._drive = 10.0 ** (db / 20.0)
        return {"ok": True, "drive_db": self._drive_db,
                "rms_dbfs": modulator.DEFAULT_LEVEL_DBFS + self._drive_db,
                "min_db": self.DRIVE_MIN_DB, "max_db": self.DRIVE_MAX_DB}

    def set_channel(self, cfg: dict) -> dict:
        base = CH.PRESETS.get(cfg.get("preset", "clean"), CH.PRESETS["clean"])
        snr = str(cfg.get("snr", "")).strip()
        new = CH.ChannelConfig(**{**base.__dict__})
        if snr:
            try:
                new.snr_db = float(snr)
            except ValueError:
                return {"error": f"bad SNR {snr!r}"}
        with self._lock:
            self._chan_cfg = new
        return {"ok": True, "summary": new.summary()}

    def control(self, msg: dict) -> dict:
        cmd = msg.get("cmd")
        if cmd == "start":
            return self.start(msg)
        if cmd == "stop":
            return self.stop()
        if cmd == "pad":
            return self.set_pad(msg)
        if cmd == "metadata":
            return self.set_metadata(msg)
        if cmd == "nowplaying":
            return self.nowplaying(msg)
        if cmd == "channel":
            return self.set_channel(msg)
        if cmd == "drive":
            return self.set_drive(msg)
        if cmd == "profiles":
            return {"profiles": webui.profile_list(),
                    "modcods": webui.modcod_list(),
                    "sample_rates": webui.sample_rates(),
                    "carrier_choices": list(profiles.OFDM_CARRIER_CHOICES),
                    "default_profile": webui.default_profile(),
                    "default_profiles": webui.default_profiles(),
                    "symbol_rates": {str(r): webui.symbol_rates(r)
                                     for r in webui.sample_rates()}}
        if cmd == "solve":
            return solve(msg)
        if cmd == "streams":
            return {"dir": streams.DEFAULT_DIR,
                    "stations": streams.as_dicts(msg.get("dir") or None)}
        if cmd == "probe":
            src = str(msg.get("source") or "").strip()
            if not src:
                return {"error": "nothing to probe"}
            try:
                return codec.probe(src, ffmpeg=self.ffmpeg)
            except codec.CodecError as exc:
                return {"error": str(exc)}
        if cmd == "devices":
            return {"devices": list_devices(output=True,
                                            every_api=bool(msg.get("all")))}
        return {"error": f"unknown command {cmd!r}"}

    # -- worker ----------------------------------------------------------

    def _run(self, source, codec_name, bitrate, profile, modcod, device,
             passthrough=None) -> None:
        enc = sink = None
        try:
            enc = self._enc = codec.Encoder(source, codec_name, bitrate,
                                ffmpeg=self.ffmpeg, loop=True,
                                passthrough=passthrough)
            enc.start()
            sink = self._sink = open_output(device, profile.sample_rate)

            tx = transport.TransmitChain(profile, modcod)
            # One line decides the physical layer. Everything after it --
            # the transport chain, the FEC, the framing, the scope, the drive
            # -- is identical, which is the point of giving the OFDM path the
            # same interface as the single-carrier one.
            mod = (ofdm.CodedModulator(profile) if profile.is_ofdm
                   else modulator.Modulator(profile))
            cap = profile.capacity(modcod)
            chan = CH.Channel(profile, self._chan_cfg)
            chan_key = self._chan_cfg.summary()

            frames = 0
            started = time.time()
            last_repeat = 0.0
            pending: list[bytes] = []
            spare = cap.net_bitrate - bitrate

            scope_symbols = scope.symbol_budget(modcod.bits_per_symbol)
            feed = scope.ScopeFeed(profile.sample_rate)
            self._feed = feed
            self._meta = {
                "profile": profile, "modcod": modcod, "cap": cap,
                "bitrate": bitrate, "spare": spare, "started": started,
                # The encoder's own choice, not the one the page predicted --
                # so what is displayed is what is actually being encoded.
                "choice": enc.choice,
                "passthrough": passthrough is not None,
                "ps_undetermined": bool(
                    (self._probe or {}).get("passthrough", {})
                    .get("ps_undetermined")),
            }
            publisher = scope.Publisher(self.telemetry, self._build_state, SCOPE_HZ)
            publisher.start()

            dropped = 0
            last_drop = 0.0
            audio_packets = 0
            last_audio = time.time()
            while not self._stop.is_set():
                fresh = enc.packets()
                if fresh:
                    audio_packets += len(fresh)
                    last_audio = time.time()
                pending.extend(fresh)
                if len(pending) > MAX_PENDING:
                    dropped += len(pending) - MAX_PENDING
                    del pending[:len(pending) - MAX_PENDING]
                    last_drop = time.time()

                now = time.time()
                if now - last_repeat >= REPEAT_SECONDS:
                    last_repeat = now
                    if enc.config:
                        tx.push_config(enc.config)
                    with self._lock:
                        tx.push_pad(self._pad)

                # Fill the channel from the encoder, stuffing if it is behind.
                while pending and tx.backlog < cap.frame_bytes * 3:
                    tx.push_audio(pending.pop(0))

                payload, il, rsp = tx.next_frame()
                hdr = framing.Header(modcod.index, enc.choice.codec_id, il, rsp,
                                     frames % framing.FRAME_COUNT_MOD)
                # Single carrier builds symbols explicitly rather than going
                # straight to audio: the constellation display needs them, and
                # rebuilding them afterwards would modulate the frame twice.
                # OFDM has no such symbol vector -- its frame is subcarriers,
                # not a line of symbols -- so it goes straight to audio and
                # keeps the payload symbols for the display itself.
                if profile.is_ofdm:
                    samples = mod.modulate_frame(modcod, hdr, payload)
                else:
                    symbols = framing.build_frame(profile, modcod, hdr, payload)
                    samples = mod.modulate_symbols(symbols)

                with self._lock:
                    if self._chan_cfg.summary() != chan_key:
                        chan = CH.Channel(profile, self._chan_cfg)
                        chan_key = self._chan_cfg.summary()
                out = chan.process(samples)

                # Drive is the last thing applied, so it sets what leaves the
                # card without touching the modulation or the simulator's SNR
                # -- both of those are ratios, and scaling afterwards leaves
                # them alone. Read under the lock so the slider can move it
                # while transmitting.
                with self._lock:
                    drive = self._drive
                if drive != 1.0:
                    out = out * drive

                # Into the scope before the blocking write, so the waterfall
                # is never waiting on the sound card.
                feed.push_audio(out)
                shown = (mod.last_symbols if profile.is_ofdm
                         else symbols[framing.data_slots(profile)])
                feed.push_symbols(shown[::max(
                    1, profile.data_symbols // scope_symbols)])
                sink.write(out)

                frames += 1
                self._live = {
                    "frames": frames, "pending": len(pending),
                    "dropped": dropped, "audio_packets": audio_packets,
                    "silent_for": time.time() - last_audio,
                    "since_drop": time.time() - last_drop if last_drop else 1e9,
                    "peak": float(np.max(np.abs(out))) if len(out) else 0.0,
                    "clipping": modulator.clipping_fraction(out),
                }
                if enc.error and not self.error:
                    self.error = enc.error
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                publisher.stop()
            except NameError:
                pass
            self._feed = None
            self._enc = self._sink = None
            _shutdown(enc, sink)
            self._state = {**self._state, "running": False, "error": self.error}
            self.telemetry.publish(self._state)

    def _build_state(self) -> dict | None:
        """Snapshot for the UI. Called by the publisher thread, not the loop."""
        feed = self._feed
        meta = self._meta
        if feed is None or not meta:
            return None
        profile = meta["profile"]
        modcod = meta["modcod"]
        cap = meta["cap"]
        choice = meta.get("choice")
        live = dict(self._live)

        shot = feed.snapshot(self._last_seq)
        self._last_seq = shot.get("seq", self._last_seq)

        lo, hi = profile.band
        nyq = profile.sample_rate / 2.0
        peak = live.get("peak", 0.0)
        pending = live.get("pending", 0)
        state = {
            "running": True,
            "profile": profile.name,
            # An OFDM profile has no symbol rate to quote -- the one on the
            # profile is the single-carrier one it borrowed its band from, and
            # printing that would describe a signal nobody is transmitting.
            "footprint": f"{modcod.label_for(profile.constellation_family)} \u00b7 "
                         + (f"{profile.geometry.carriers} carriers"
                            if profile.is_ofdm
                            else f"{profile.symbol_rate} Bd")
                         + f" \u00b7 {lo/1000:.1f}-{hi/1000:.1f} kHz",
            "modcod": modcod.label_for(profile.constellation_family),
            # What this rung needs to be received. Quoted as Es/N0 but
            # measured as EVM, which is the same number on a link whose only
            # impairment is noise and a pessimistic one otherwise -- and it is
            # the only thing the far end can actually measure.
            "required_evm_db": modcod.required_evm_db,
            "net_rate": cap.net_bitrate,
            "audio_rate": meta["bitrate"],
            "codec": choice.name if choice else None,
            "passthrough": bool(meta.get("passthrough")),
            # Whether the PS flag is asserted or merely probable; see
            # codec.passthrough_choice.
            "ps_undetermined": bool(meta.get("ps_undetermined")),
            "sbr": bool(choice and choice.sbr),
            "ps": bool(choice and choice.ps),
            "spare": meta["spare"],
            "frames": live.get("frames", 0),
            "uptime": time.time() - meta["started"],
            "peak_dbfs": 20 * np.log10(peak) if peak > 0 else -99.0,
            "drive_db": self._drive_db,
            "clipping": live.get("clipping", 0.0),
            "backlog_frac": min(1.0, pending / MAX_PENDING),
            "backlog_note": _backlog_note(pending, live.get("dropped", 0),
                                          meta["spare"],
                                          live.get("audio_packets", 0),
                                          live.get("silent_for", 0.0),
                                          live.get("since_drop", 1e9)),
            "audio_packets": live.get("audio_packets", 0),
            "dropped": live.get("dropped", 0),
            # What is going out on the PAD channel right now, and where it
            # came from -- typed in, or followed from the station.
            "pad_station": self._pad.station,
            "pad_title": self._pad.title,
            "pad_artist": self._pad.artist,
            "nowplaying": self._nowplaying,
            "meta_error": self._icy.error if self._icy else None,
            "meta_polls": self._icy.polls if self._icy else 0,
            "band": [lo / nyq, hi / nyq],
            "error": self.error,
        }
        state.update(shot)
        # Keep the last snapshot where callers that are not a browser can see
        # it -- the CLI and tools/selftest.py both read _state, and when this
        # moved onto a publisher thread they silently started reading the
        # initial "not running" stub instead.
        self._state = state
        return state


def _backlog_note(pending: int, dropped: int, spare: float,
                  audio_packets: int, silent_for: float,
                  since_drop: float = 1e9) -> str:
    """Say what the queue is doing, and why if it is unhappy.

    An empty queue has two completely different causes -- the channel keeping
    up, and the encoder producing nothing at all -- and they were reported
    identically. A dead source therefore read as "in step" while the modem
    transmitted metadata into the void, which is the least useful thing this
    field could have said.
    """
    if audio_packets == 0:
        return ("no audio from the source yet -- check the URL or path. "
                "The link is up but carrying only metadata.")
    if silent_for > 3.0:
        return (f"nothing from the encoder for {silent_for:.0f} s -- the "
                f"source has stopped or dropped out.")
    # Only complain while drops are actually happening. An Icecast server
    # bursts its buffer the moment you connect, so trimming a few hundred
    # packets at startup is normal and self-correcting; leaving the alarm up
    # afterwards trains you to ignore it.
    if dropped and since_drop < 5.0:
        return (f"{pending} queued, dropping -- the encoder is outrunning the "
                f"channel. Spare capacity is {spare/1000:.1f} kbps.")
    if dropped:
        return (f"{pending} queued, in step. {dropped} trimmed at startup to "
                f"stay live with the source.")
    if pending > MAX_PENDING * 0.6:
        return f"{pending} queued and climbing -- the channel is close to full"
    if pending > 5:
        return f"{pending} queued, absorbing the stream's burst"
    return f"{pending} queued, in step"


def build_profile(cfg: dict) -> profiles.Profile:
    """A link key, a named preset, or one built from the explicit fields."""
    # A key wins outright when one is supplied. It describes the whole physical
    # layer, and rebuilding from it directly is what makes copying one across
    # exact rather than approximate -- the dials it fills in cannot express a
    # carrier frequency or a pilot spacing, so a round trip through them would
    # quietly drop both.
    key = str(cfg.get("link_key") or "").strip()
    if key:
        return linkkey.to_profile(linkkey.decode(key))
    name = str(cfg.get("profile") or "WIDE").upper()
    if name != "CUSTOM":
        profile = profiles.get_profile(name)
        # An OFDM profile's carrier count is a front-panel setting in exactly
        # the way the profile itself is: it changes the frame geometry, none
        # of it travels in the header, and both ends must be set the same.
        if profile.is_ofdm and cfg.get("carriers"):
            profile = profiles.with_carriers(profile, int(cfg["carriers"]))
        return profile
    return profiles.make_profile(
        sample_rate=int(cfg.get("sample_rate") or 48000),
        symbol_rate=int(cfg.get("symbol_rate") or 16000),
        rolloff=float(cfg.get("rolloff") or 0.25),
        carrier=float(cfg["carrier"]) if cfg.get("carrier") else None,
        pilot_spacing=int(cfg.get("pilot_spacing") or 64),
        mode="apsk" if str(cfg.get("mode") or "") == "apsk" else "sc",
    )


# Both spellings, so "16APSK" from an APSK page and "16QAM" from a QAM one
# both name the same rung. The constellation a MODCOD's bits land on is the
# profile's business; the rung itself is the same either way.
BITS = profiles.BITS_BY_MODULATION


def pick_modcod(cfg: dict, profile: profiles.Profile, bitrate: int) -> profiles.Modcod:
    """Explicit MODCOD if given, otherwise the cheapest rung that fits."""
    idx = cfg.get("modcod")
    if idx not in (None, "", "auto"):
        return profiles.MODCOD_BY_INDEX[int(idx)]
    if cfg.get("modulation") and cfg.get("code_rate"):
        num, den = (int(v) for v in str(cfg["code_rate"]).split("/"))
        return profiles.find_modcod(BITS[cfg["modulation"]], num, den)
    return profile.modcod_for_bitrate(bitrate)


# --------------------------------------------------------------------------
# The rate solver
# --------------------------------------------------------------------------
#
# Symbol rate, MODCOD and audio bitrate are three views of one quantity:
#
#     audio bitrate <= symbol_rate * data_fraction * bits_per_symbol
#                      * conv_rate * rs_rate - PAD reserve
#
# Two of the three determine the third, so the UI cannot simply "edit a field".
# Something else has to move, and only the user knows which. Hence locks: a
# locked field is one the solver may not touch, so the user pins what they care
# about and the rest follows.
#
# Without that the page ratchets. Set 64QAM, get a bitrate ceiling, pick a
# bitrate -- and the solver narrows the symbol rate to the slowest that carries
# it, which drops the ceiling to just above what you picked and takes every
# higher option away. You can walk down the list but never back up.

LOCKABLE = ("symbol_rate", "bitrate", "modcod")

# Which field to move for each thing the user touched, in order of preference.
# The field they just edited is never in its own list -- moving it would undo
# the edit, which is the one outcome guaranteed to be wrong.
ADJUST_ORDER = {
    "bitrate": ("symbol_rate", "modcod"),
    "symbol_rate": ("bitrate", "modcod"),
    "modcod": ("bitrate", "symbol_rate"),
    "sample_rate": ("bitrate", "symbol_rate", "modcod"),
    "rolloff": ("bitrate", "symbol_rate", "modcod"),
    # A preset *is* its symbol rate, so choosing one holds that field the same
    # way editing it would. Otherwise picking WIDE48 could immediately solve
    # its way off WIDE48, and the preset name would stop describing the link.
    "preset": ("bitrate", "modcod"),
    # Toggling a lock, or switching codec, edits nothing about the link, so
    # one that already fits stays put -- bitrate leads the list and only ever
    # clamps downwards.
    "locks": ("bitrate", "symbol_rate", "modcod"),
    "codec": ("bitrate", "symbol_rate", "modcod"),
}

FIELD_LABELS = {"symbol_rate": "symbol rate", "bitrate": "bitrate",
                "modcod": "modulation"}

# Hard bounds on what the encoder is ever asked for, since the bitrate is
# typed rather than picked from a list.
BITRATE_FLOOR = 8000
BITRATE_CEILING = 320000

# The range the codec ladder and the MODCOD thresholds were built and measured
# for. Outside it things still work, but nothing has been checked -- so this
# earns a note, not a clamp.
DESIGN_RANGE = (16000, 192000)


def _ceiling(sample_rate: int, symbol_rate: int, modcod: profiles.Modcod,
             rolloff: float, pilot: int) -> float:
    """Audio bits per second left after coding, framing and the PAD reserve."""
    try:
        net = profiles.bitrate_for_symbol_rate(
            sample_rate, symbol_rate, modcod, rolloff, pilot)
    except ValueError:
        return 0.0
    return max(0.0, net - profiles.PAD_RESERVE_BPS)


def _tightest_modcod(sample_rate: int, symbol_rate: int, bitrate: int,
                     rolloff: float, pilot: int) -> profiles.Modcod | None:
    """Most rugged rung that still carries ``bitrate`` at this symbol rate.

    Most rugged rather than fastest: every rung up the ladder costs SNR, so
    when the constellation is what is free to move, moving it down is free
    robustness.
    """
    for m in profiles.MODCODS:
        if _ceiling(sample_rate, symbol_rate, m, rolloff, pilot) >= bitrate:
            return m
    return None


def _solve_ofdm(profile: profiles.Profile, msg: dict) -> dict:
    """What the UI shows for an OFDM profile."""
    try:
        profile = profiles.with_carriers(profile, msg.get("carriers") or None)
        modcod = pick_modcod(msg, profile, 0)
    except (KeyError, ValueError) as exc:
        return {"error": f"bad settings: {exc}"}
    geo = profile.geometry
    cap = profile.capacity(modcod)
    ceiling = max(0.0, cap.net_bitrate - profiles.PAD_RESERVE_BPS)
    bitrate = int(msg.get("bitrate") or 64000)
    moved = []
    if bitrate > ceiling:
        bitrate = int(ceiling // 1000) * 1000
        moved.append("bitrate")
    lo, hi = geo.band
    return {
        "mode": "ofdm",
        "sample_rate": profile.sample_rate,
        "symbol_rate": int(round(geo.symbol_rate)),
        "symbol_rates": [int(round(geo.symbol_rate))],
        # The geometry cannot travel in the header -- the preamble is built
        # from it -- so it travels as a key the operator copies across.
        "link_key": linkkey.encode(profile),
        "sps": geo.fft,
        "rolloff": profile.rolloff,
        "carrier": (lo + hi) / 2,
        "band": [round(lo), round(hi)],
        "bandwidth": round(geo.bandwidth),
        "nyquist": profile.sample_rate / 2,
        "fits": hi < profile.sample_rate / 2,
        "frame_symbols": geo.frame_symbols,
        "frame_ms": round(geo.frame_duration * 1000),
        "pilot_spacing": profile.pilot_spacing,
        "modcod": modcod.index,
        "modcod_name": modcod.label_for(profile.constellation_family),
        "modulation": modcod.modulation_for(profile.constellation_family),
        "code_rate": f"{modcod.conv_num}/{modcod.conv_den}",
        "rs": f"{profiles.RS_N},{modcod.rs_k}",
        "required_evm_db": modcod.required_evm_db,
        "net_bitrate": round(cap.net_bitrate),
        "max_audio_bitrate": round(ceiling),
        "interleaver_seconds": round(profiles.interleaver_delay(profile, modcod), 1),
        "bitrate": max(0, bitrate),
        "carries": bitrate <= ceiling,
        "locks": [], "moved": moved, "notes": [],
        "conflict": "" if bitrate <= ceiling else
                    f"{bitrate/1000:.0f} kbps is more than this profile carries "
                    f"at {modcod}; the geometry is fixed, so lower the bitrate "
                    f"or raise the MODCOD.",
        "ofdm": {
            "fft": geo.fft, "cp": geo.cp, "carriers": geo.carriers,
            "carrier_choices": list(profiles.OFDM_CARRIER_CHOICES),
            "spacing": round(geo.bin_spacing, 1),
            "delay_spread_ms": round(geo.max_delay_spread * 1000, 2),
            "max_offset_hz": round(geo.max_freq_offset),
            "pilot_every": geo.pilot_every,
            "describe": geo.describe(),
            # The two halves of the trade, in the terms an operator has to
            # choose between: an echo longer than the first, or a transmitter
            # further off frequency than the second, is what breaks the link.
            "trade": (f"Absorbs echoes up to "
                      f"{geo.max_delay_spread * 1000:.2f} ms and carrier "
                      f"offsets up to {geo.max_freq_offset:.0f} Hz. Fewer "
                      f"carriers trade echo tolerance for offset tolerance; "
                      f"more trade it back."),
        },
    }


def solve(msg: dict) -> dict:
    """Make the three linked fields agree, moving only what is not locked.

    ``driver`` is the field the user just touched, which is held for this solve
    whether or not it is locked. ``locks`` is the list the user has pinned.
    Everything else is fair game, tried in ``ADJUST_ORDER``. If nothing is free
    the mismatch is reported rather than papered over by quietly overriding a
    lock -- a lock that gives way without saying so is worse than no lock.
    """
    # A link key describes the physical layer outright, so when one is in force
    # it replaces the page's dials rather than being reconciled with them. The
    # dials cannot express a carrier frequency, a pilot spacing or a frame
    # length at all, so solving from them would draw a panel describing a
    # different link from the one Start is about to build.
    key = str(msg.get("link_key") or "").strip()
    if key:
        try:
            keyed = linkkey.to_profile(linkkey.decode(key))
        except linkkey.LinkKeyError as exc:
            return {"error": str(exc)}
        if keyed.is_ofdm:
            return _solve_ofdm(keyed, {**msg, "carriers": keyed.ofdm_carriers})
        # Including the mode. The key carries the constellation family, and it
        # is the source of truth while it is in force -- reading that one field
        # off the page's selector instead would let a stale dropdown describe
        # the link as QAM while the key, and Start, build APSK.
        msg = {**msg, "profile": "CUSTOM", "sample_rate": keyed.sample_rate,
               "symbol_rate": keyed.symbol_rate, "rolloff": keyed.rolloff,
               "carrier": keyed.carrier, "pilot_spacing": keyed.pilot_spacing,
               "frame_symbols": keyed.frame_symbols, "mode": keyed.mode}

    # An OFDM profile is not a symbol rate and a roll-off, so none of the
    # solving below applies to it: the geometry is fixed by the profile and
    # the only free choice left is the MODCOD. Report it from the geometry
    # rather than describing it as a shaped single carrier, which would put
    # the wrong band and the wrong capacity on the page.
    name = str(msg.get("profile") or "").upper()
    try:
        preset_for_mode = profiles.get_profile(name) if name and name != "CUSTOM" else None
    except (KeyError, ValueError):
        preset_for_mode = None
    if preset_for_mode is not None and preset_for_mode.is_ofdm:
        return _solve_ofdm(preset_for_mode, msg)

    try:
        sample_rate = int(msg.get("sample_rate") or 48000)
        rolloff = float(msg.get("rolloff") or 0.25)
        # Pilot spacing and carrier are not on the page, so take them from the
        # same place ``build_profile`` will: the named preset. Planning against
        # a different spacing or a different carrier than Start uses is how the
        # panels come to describe a channel the encoder is not being given.
        pilot = int(msg.get("pilot_spacing") or 0)
        carrier = float(msg["carrier"]) if msg.get("carrier") else None
        preset = None
        name = str(msg.get("profile") or "").upper()
        if name and name != "CUSTOM":
            try:
                preset = profiles.get_profile(name)
            except (KeyError, ValueError):
                preset = None
        # Pinned by a link key when one is in force; otherwise the preset's, or
        # the automatic choice.
        frame_symbols = int(msg["frame_symbols"]) if msg.get("frame_symbols") else None
        # QAM or APSK. The preset settles it when there is one, because a
        # preset *is* a mode; otherwise the page's own selector does.
        sc_mode = str(msg.get("mode") or "sc")
        if sc_mode not in ("sc", "apsk"):
            sc_mode = "sc"
        if preset is not None:
            sc_mode = preset.mode if preset.mode in ("sc", "apsk") else sc_mode
            pilot = pilot or preset.pilot_spacing
            # Only while the page still describes that preset -- once the
            # symbol rate is edited the hand-placed carrier no longer centres
            # anything, and the automatic one is the honest answer.
            #
            # The frame length rides along for the same reason and is just as
            # load-bearing: five of the seven presets set one that the
            # automatic choice would not pick, so planning without it described
            # a frame half the length of the one Start goes on to transmit.
            if (carrier is None
                    and preset.sample_rate == sample_rate
                    and preset.symbol_rate == int(msg.get("symbol_rate") or 0)):
                carrier = preset.carrier
                frame_symbols = preset.frame_symbols
        pilot = pilot or 64
        modcod = pick_modcod(msg, profiles.get_profile("WIDE48"), 0)
    except (KeyError, ValueError) as exc:
        return {"error": f"bad settings: {exc}"}

    rates = profiles.valid_symbol_rates(sample_rate)
    if not rates:
        return {"error": f"no usable symbol rate at {sample_rate} Hz"}

    driver = str(msg.get("driver") or "symbol_rate")
    locks = {f for f in msg.get("locks") or () if f in LOCKABLE}
    bitrate = int(msg.get("bitrate") or 64000)
    symbol_rate = int(msg.get("symbol_rate") or rates[0])
    moved: list[str] = []
    notes: list[str] = []

    bounded = min(BITRATE_CEILING, max(BITRATE_FLOOR, bitrate))
    if bounded != bitrate:
        notes.append(f"Bitrate held to {bounded/1000:.0f} kbps; "
                     f"{BITRATE_FLOOR//1000}-{BITRATE_CEILING//1000} kbps is "
                     f"the range the encoder is asked for.")
        bitrate = bounded
        moved.append("bitrate")

    # A symbol rate has to divide the sample rate to an integer samples/symbol,
    # so an unlisted one is not a preference the solver is overriding -- it is
    # not achievable on this card at all. Snap it before anything else.
    if symbol_rate not in rates:
        snapped = min(rates, key=lambda r: abs(r - symbol_rate))
        notes.append(f"{symbol_rate} Bd needs a non-integer samples/symbol at "
                     f"{sample_rate/1000:.1f} kHz; using {snapped} Bd.")
        symbol_rate = snapped
        moved.append("symbol_rate")

    free = [f for f in ADJUST_ORDER.get(driver, ADJUST_ORDER["preset"])
            if f not in locks]
    conflict = ""
    fitted = False

    for field in free:
        if _ceiling(sample_rate, symbol_rate, modcod, rolloff, pilot) >= bitrate \
                and field == "bitrate":
            fitted = True
            break               # already fits; bitrate only ever clamps down
        if field == "symbol_rate":
            found = profiles.symbol_rate_for_bitrate(
                sample_rate, bitrate, modcod, rolloff, pilot)
            if found is None:
                continue        # no symbol rate carries it on *this* MODCOD
            if found != symbol_rate:
                symbol_rate = found
                moved.append("symbol_rate")
            fitted = True
            break
        if field == "modcod":
            found = _tightest_modcod(sample_rate, symbol_rate, bitrate,
                                     rolloff, pilot)
            if found is None:
                continue        # nothing fits at *this* symbol rate
            if found.index != modcod.index:
                modcod = found
                moved.append("modcod")
            fitted = True
            break
        if field == "bitrate":
            # Down to a whole kbps, not to the exact ceiling. An encoder asked
            # for 58631 bps is an encoder sitting on the limit, and the tidy
            # number is what the user would have typed anyway.
            ceiling = _ceiling(sample_rate, symbol_rate, modcod, rolloff, pilot)
            capped = int(ceiling // 1000) * 1000
            if capped < bitrate:
                bitrate = capped
                moved.append("bitrate")
            fitted = True
            break

    # Neither field could do it alone, which does not mean it cannot be done.
    # Asking for 160 kbps at 19200 Bd on 64QAM 3/4 needs the symbol rate *and*
    # the MODCOD to move together: no symbol rate carries it on 64QAM 3/4, and
    # no MODCOD carries it at 19200 Bd, yet 32000 Bd on 256QAM 3/4 carries it
    # comfortably. Without this the page would have said "beyond anything this
    # card can carry" about a card with 35 kbps to spare -- which is the same
    # ratchet in a new place: whatever narrowed the symbol rate earlier would
    # have capped every later edit.
    if not fitted and {"symbol_rate", "modcod"} <= set(free):
        for rate in sorted(rates):          # narrowest first; bandwidth is cost
            found = _tightest_modcod(sample_rate, rate, bitrate, rolloff, pilot)
            if found is None:
                continue
            if rate != symbol_rate:
                symbol_rate = rate
                moved.append("symbol_rate")
            if found.index != modcod.index:
                modcod = found
                moved.append("modcod")
            break

    ceiling = _ceiling(sample_rate, symbol_rate, modcod, rolloff, pilot)
    if bitrate > ceiling:
        # Quote the best the *free* fields could reach, not the ceiling where
        # things happen to be sitting. With the symbol rate free at 4000 Bd,
        # "lower the bitrate to 16.1 kbps" is advice against a limit that does
        # not exist -- widening to 16000 Bd carries 70.
        reachable = ceiling
        for rate in (rates if "symbol_rate" in free else [symbol_rate]):
            for m in (profiles.MODCODS if "modcod" in free else [modcod]):
                reachable = max(reachable,
                                _ceiling(sample_rate, rate, m, rolloff, pilot))
        held = sorted(locks & set(ADJUST_ORDER.get(driver, ())))
        if held:
            names = " and ".join(FIELD_LABELS[f] for f in held)
            conflict = (
                f"{bitrate/1000:.0f} kbps does not fit, and {names} "
                f"{'are' if len(held) > 1 else 'is'} locked. "
                f"{'Unlock one' if len(held) > 1 else 'Unlock it'}, or drop to "
                f"{int(reachable) // 1000} kbps.")
        else:
            conflict = (f"{bitrate/1000:.0f} kbps is beyond anything this card "
                        f"can carry at {sample_rate/1000:.1f} kHz; "
                        f"{int(reachable) // 1000} kbps is the most it will do.")

    # Only keep the preset's hand-placed carrier and frame length if the solver
    # left the symbol rate where the preset put it; if it moved, the page has
    # deviated to Custom and Start will build the automatic ones.
    if carrier is not None and symbol_rate != int(msg.get("symbol_rate") or 0):
        carrier = None
        if not msg.get("frame_symbols"):
            frame_symbols = None
    out = profiles.plan(sample_rate, symbol_rate, modcod, rolloff, pilot,
                        carrier, frame_symbols, mode=sc_mode)
    out["bitrate"] = max(0, bitrate)
    out["symbol_rates"] = rates
    # Encoded from the profile the plan actually describes, not from the three
    # fields on the page. The carrier, the pilot spacing and the frame length
    # are settled here and are every bit as load-bearing as the symbol rate; a
    # key without them names a link that only sometimes exists.
    out["link_key"] = linkkey.encode(
        profiles.make_profile(sample_rate, symbol_rate, rolloff,
                              carrier=carrier, pilot_spacing=pilot,
                              frame_symbols=frame_symbols, mode=sc_mode))
    out["mode"] = sc_mode
    out["carries"] = bitrate <= ceiling
    out["locks"] = sorted(locks)
    out["moved"] = sorted(set(moved))
    # Which codec profile this bitrate actually selects. HE-AAC steps
    # v2 -> v1 -> LC as the rate rises and the switches are invisible
    # otherwise: nothing on the page changes when you cross 48k, yet a
    # different encoder profile goes out.
    try:
        out["codec_ladder"] = codec.ladder(str(msg.get("codec") or "opus"),
                                           bitrate)
    except codec.CodecError as exc:
        out["codec_ladder"] = []
        notes.append(str(exc))
    out["conflict"] = conflict
    # Only worth saying about a bitrate that is actually going to be used --
    # repeating the number in a second sentence right after saying it does not
    # fit is noise, and pushes the sentence that matters off the line.
    if not conflict and not (DESIGN_RANGE[0] <= bitrate <= DESIGN_RANGE[1]):
        notes.append(f"{bitrate/1000:.0f} kbps is outside the 16-192 kbps the "
                     f"codec ladder was tuned for.")
    out["notes"] = notes
    return out




# --------------------------------------------------------------------------
# Audio output
# --------------------------------------------------------------------------

class WavSink:
    def __init__(self, path: str, rate: int):
        self.path = path
        self._w = wave.open(path, "wb")
        self._w.setnchannels(1)
        self._w.setsampwidth(2)
        self._w.setframerate(rate)
        self._t = time.time()
        self._rate = rate
        self._written = 0

    def write(self, x: np.ndarray) -> None:
        self._w.writeframes(modulator.to_int16(x).tobytes())
        self._written += len(x)
        # Pace to real time so the UI and the encoder behave as they would on
        # a sound card rather than sprinting through the file.
        target = self._t + self._written / self._rate
        delay = target - time.time()
        if delay > 0:
            time.sleep(min(delay, 0.5))

    def close(self) -> None:
        self._w.close()


class DeviceSink:
    def __init__(self, index: int | None, rate: int):
        import sounddevice as sd
        self._sd = sd
        self.stream = sd.OutputStream(samplerate=rate, channels=1,
                                      dtype="float32", device=index,
                                      blocksize=0, latency="high")
        self.stream.start()

    def write(self, x: np.ndarray) -> None:
        self.stream.write(np.clip(x, -1.0, 1.0).astype(np.float32))

    def close(self) -> None:
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass


def open_output(device: str, rate: int):
    if not device or device == "wav":
        return WavSink("tx.wav", rate)
    return DeviceSink(int(device), rate)


# Windows enumerates every card once per host API, so an unfiltered list is
# fifty entries for a dozen devices. MME lists each device exactly once and
# accepts any sample rate -- by resampling, which costs a little quality, so
# the other APIs stay available behind `every_api`.
PREFERRED_API = "MME"


def list_devices(output: bool, every_api: bool = False) -> list[dict]:
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        default = sd.default.device[1 if output else 0]
    except Exception:
        return []
    key = "max_output_channels" if output else "max_input_channels"
    out = []
    for i, d in enumerate(devs):
        if d[key] < 1:
            continue
        try:
            api = sd.query_hostapis(d["hostapi"])["name"]
        except Exception:
            api = "?"
        if not every_api and PREFERRED_API not in api:
            continue
        label = d["name"] if not every_api else f"{d['name']} [{api}]"
        out.append({"index": i, "name": label, "api": api,
                    "default": i == default})
    return out


def _install_shutdown(app) -> None:
    """Make sure the devices come down however this process ends.

    Ctrl+C is already handled where the main loop sits, but that is only one
    of the ways out. A closed console window, a `kill`, or an unhandled
    exception all skip it -- and skipping it leaves a sound card open with
    PortAudio's callback still running on a native thread that does not get
    killed with Python's, which is exactly the "I closed it and it kept
    playing" case. atexit covers the ordinary exits and the signals cover the
    rest. Nothing covers Task Manager's End Task; the OS reclaims the device
    itself there.
    """
    import atexit
    import signal

    done = threading.Event()

    def once(*_args) -> None:
        if done.is_set():
            return
        done.set()
        try:
            app.stop()
        except Exception:
            pass

    atexit.register(once)
    for name in ("SIGTERM", "SIGBREAK", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, lambda *_a: (once(), sys.exit(0)))
        except (ValueError, OSError):
            pass          # not the main thread, or not supported here


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", help="stream URL or file")
    ap.add_argument("--profile", default=profiles.DEFAULT_PROFILE,
                    help="preset name, or CUSTOM with --symbol-rate etc")
    ap.add_argument("--sample-rate", type=int, default=48000,
                    help="card rate, used with --profile CUSTOM")
    ap.add_argument("--symbol-rate", type=int, default=None, help="baud")
    ap.add_argument("--rolloff", type=float, default=0.25)
    ap.add_argument("--carrier", type=float, default=None)
    ap.add_argument("--carriers", type=int, default=None,
                    choices=list(profiles.OFDM_CARRIER_CHOICES),
                    help="OFDM subcarriers; fewer rides out frequency drift, "
                         "more rides out echoes. Must match at both ends.")
    ap.add_argument("--pilot-spacing", type=int, default=64, choices=[32, 64, 128])
    ap.add_argument("--mode", default="sc", choices=["sc", "apsk"],
                    help="constellation family for --profile CUSTOM: square "
                         "QAM, or APSK rings for a compressed amplifier. The "
                         "named presets carry their own.")
    ap.add_argument("--modcod", default=None,
                    help="MODCOD index; omit to choose from the bitrate")
    ap.add_argument("--modulation", default=None,
                    choices=sorted(profiles.BITS_BY_MODULATION))
    ap.add_argument("--code-rate", default=None, help="e.g. 3/4")
    ap.add_argument("--codec", default="opus", choices=["opus", "aac"])
    ap.add_argument("--bitrate", default="128k")
    ap.add_argument("--passthrough", action="store_true",
                    help="send the source's own packets without re-encoding; "
                         "it sets the bitrate, and must already be AAC or Opus")
    ap.add_argument("--probe", metavar="SOURCE",
                    help="report what codec and rate a source carries, and "
                         "whether it can be passed through")
    ap.add_argument("--list-stations", action="store_true",
                    help=f"stations found in {streams.DEFAULT_DIR}")
    ap.add_argument("--station-dir", default=None,
                    help="folder of .pls/.m3u playlists to read")
    ap.add_argument("--device", default="", help="output device index, or 'wav'")
    ap.add_argument("--station", default="QAM RADIO")
    ap.add_argument("--port", type=int, default=8731)
    ap.add_argument("--ffmpeg", default=None)
    ap.add_argument("--no-ui", action="store_true")
    ap.add_argument("--open", action="store_true",
                    help="open the UI in a browser once it is listening")
    ap.add_argument("--list-rates", action="store_true")
    ap.add_argument("--list-devices", action="store_true")
    a = ap.parse_args()

    if a.list_stations:
        found = streams.load(a.station_dir)
        if not found:
            print(f"no playlists in {a.station_dir or streams.DEFAULT_DIR}")
            return 1
        for st in found:
            print(st.name)
            if st.description:
                print(f"    {st.description}")
            for fd in sorted(st.feeds, key=lambda f: f.bitrate or 1 << 30):
                print(f"    {fd.label:<11} {fd.url}")
        return 0

    if a.probe:
        try:
            info = codec.probe(a.probe, ffmpeg=a.ffmpeg)
        except codec.CodecError as exc:
            print(exc)
            return 1
        pt = info["passthrough"]
        print(f"{a.probe}\n  {info['label']}")
        print(f"  passthrough: {'yes' if pt['ok'] else 'no'} -- {pt['reason']}")
        return 0

    if a.list_rates:
        if a.profile.upper() == "CUSTOM":
            print("--list-rates needs a preset name, not CUSTOM")
            return 1
        print(profiles.describe(profiles.get_profile(a.profile)))
        return 0
    if a.list_devices:
        for d in list_devices(output=True):
            print(f"{d['index']:>3}  {d['name']}{'  (default)' if d['default'] else ''}")
        return 0

    tx = Transmitter(ffmpeg=a.ffmpeg)
    _install_shutdown(tx)
    if not a.no_ui:
        webui.serve("tx.html", tx.telemetry, tx.control, port=a.port)
        print(f"transmit UI at http://127.0.0.1:{a.port}")
        # Opened here rather than from the launcher: the server is already
        # listening by this point, so the browser cannot beat it to the port.
        if a.open:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{a.port}")
        # Publish an idle snapshot so a browser that connects before anything
        # is running shows "connected" rather than sitting on "connecting".
        tx.telemetry.publish(tx._state)

    if a.source:
        bitrate = parse_rate(a.bitrate)
        spec = {"profile": a.profile, "sample_rate": a.sample_rate,
                "symbol_rate": a.symbol_rate, "rolloff": a.rolloff,
                "carrier": a.carrier, "carriers": a.carriers,
                "pilot_spacing": a.pilot_spacing, "mode": a.mode,
                "modcod": a.modcod, "modulation": a.modulation,
                "code_rate": a.code_rate}
        try:
            profile = build_profile(spec)
            # With passthrough the source dictates the bitrate, so there is
            # nothing to pick a MODCOD from until it has been probed. Doing it
            # here anyway would reject the run over a --bitrate default that
            # is about to be discarded.
            modcod = (None if a.passthrough
                      else pick_modcod(spec, profile, bitrate))
        except (ValueError, KeyError) as exc:
            print(exc, file=sys.stderr)
            return 1
        if a.passthrough:
            # ASCII only: this goes to a Windows console, where cp1252 turns an
            # ellipsis into a replacement character.
            print(f"{profile.name} / probing {a.source} ...")
        else:
            print(f"{profile.name} / {modcod} / {a.codec} at "
                  f"{bitrate/1000:.0f} kbps")
        res = tx.start({**spec, "source": a.source, "codec": a.codec,
                        "passthrough": a.passthrough,
                        "bitrate": bitrate, "device": a.device,
                        "station": a.station})
        if res.get("error"):
            print(res["error"], file=sys.stderr)
            return 1
        if res.get("passthrough"):
            print(f"{profile.name} / {res['modcod']} / passing through "
                  f"{res.get('detected') or 'the source'}")
    elif a.no_ui:
        print("nothing to do: give --source or drop --no-ui", file=sys.stderr)
        return 1

    try:
        while True:
            time.sleep(0.5)
            if tx.error:
                print(f"error: {tx.error}", file=sys.stderr)
                tx.error = None
    except KeyboardInterrupt:
        tx.stop()
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
