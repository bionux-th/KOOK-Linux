import asyncio
import ctypes
import ctypes.util
import fractions
import json
import logging
import random
import ssl
import subprocess
import threading
import time
from typing import Optional

import websocket
from av import AudioFrame
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription, RTCConfiguration
from aiortc.mediastreams import MediaStreamError
from aiortc.rtcconfiguration import RTCIceServer

from kook_api import KookAPI

logger = logging.getLogger(__name__)
logging.basicConfig()
logging.getLogger("aiortc").setLevel(logging.WARNING)
logging.getLogger("aiortc.rtcrtpsender").setLevel(logging.WARNING)
logging.getLogger("aioice").setLevel(logging.ERROR)

# --- Direct Opus encoder via ctypes (bypasses PyAV's buggy resampler) ---
_libopus = ctypes.cdll.LoadLibrary(ctypes.util.find_library("opus"))

_libopus.opus_encoder_create.restype = ctypes.c_void_p
_libopus.opus_encoder_create.argtypes = [
    ctypes.c_int,   # Fs (sample rate)
    ctypes.c_int,   # channels
    ctypes.c_int,   # application
    ctypes.POINTER(ctypes.c_int),  # error
]
_libopus.opus_encode.restype = ctypes.c_int
_libopus.opus_encode.argtypes = [
    ctypes.c_void_p,     # encoder
    ctypes.POINTER(ctypes.c_int16),  # pcm
    ctypes.c_int,        # frame_size
    ctypes.POINTER(ctypes.c_ubyte),  # data
    ctypes.c_int,        # max_data_bytes
]
_libopus.opus_encoder_ctl.restype = ctypes.c_int
_libopus.opus_encoder_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
_libopus.opus_encoder_destroy.restype = None
_libopus.opus_encoder_destroy.argtypes = [ctypes.c_void_p]

OPUS_APPLICATION_VOIP = 2048
OPUS_SET_BITRATE = 4002

MAX_PACKET_SIZE = 4000  # more than enough for one Opus frame


def _create_opus_encoder() -> ctypes.c_void_p:
    err = ctypes.c_int(0)
    enc = _libopus.opus_encoder_create(48000, 2, OPUS_APPLICATION_VOIP, ctypes.byref(err))
    if err.value != 0 or not enc:
        raise RuntimeError(f"opus_encoder_create failed: error={err.value}")
    _libopus.opus_encoder_ctl(enc, OPUS_SET_BITRATE, 96000)
    return enc


class ReconnectNeeded(Exception):
    pass


class _DirectOpusEncoder:
    """Opus encoder backed by libopus via ctypes — no PyAV resampler involved."""

    def __init__(self):
        self._enc = _create_opus_encoder()
        self._first_pts: Optional[int] = None
        self._pts_offset = 0  # 48000 ticks per second

    def encode(self, frame, force_keyframe=False):
        # Frame is an av.AudioFrame with s16/stereo/48kHz/960 samples
        pcm_ptr = ctypes.cast(
            ctypes.c_char_p(bytes(frame.planes[0])),
            ctypes.POINTER(ctypes.c_int16),
        )
        out_buf = (ctypes.c_ubyte * MAX_PACKET_SIZE)()
        nbytes = _libopus.opus_encode(self._enc, pcm_ptr, 960, out_buf, MAX_PACKET_SIZE)
        if nbytes < 0:
            logger.warning(f"opus_encode returned {nbytes}")
            return [], None

        payload = bytes(out_buf[:nbytes])
        if self._first_pts is None:
            self._first_pts = frame.pts
            ts = 0
        else:
            ts = frame.pts - self._first_pts

        return [payload], ts

    def pack(self, packet):
        # Pre-encoded packets — not needed for our use case
        from aiortc.codecs.opus import OpusEncoder as _OpusEncoder
        return _OpusEncoder.pack(self, packet)

    def __del__(self):
        if hasattr(self, '_enc') and self._enc:
            _libopus.opus_encoder_destroy(self._enc)


# Replace aiortc's get_encoder to return our direct encoder
import aiortc.rtcrtpsender as _rtcrtpsender
_orig_get_encoder = _rtcrtpsender.get_encoder
def _patched_get_encoder(codec):
    mime = codec.mimeType.lower()
    if mime == "audio/opus":
        return _DirectOpusEncoder()
    return _orig_get_encoder(codec)
_rtcrtpsender.get_encoder = _patched_get_encoder



class _AudioTrack(MediaStreamTrack):
    kind = "audio"
    _TIME_BASE = fractions.Fraction(1, 48000)

    def __init__(self):
        super().__init__()
        self._queue = asyncio.Queue()
        self._pts = 0

    def push_pcm(self, data: bytes):
        self._queue.put_nowait(data)

    def stop(self):
        self._queue.put_nowait(b"")

    async def recv(self):
        frame = AudioFrame(format="s16", layout="stereo", samples=960)
        frame.sample_rate = 48000
        frame.time_base = self._TIME_BASE
        frame.pts = self._pts
        self._pts += 960
        try:
            data = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            for p in frame.planes:
                p.update(b'\x00' * p.buffer_size)
            return frame
        if not data:
            raise asyncio.CancelledError
        frame.planes[0].update(data)
        return frame


class _WsReader:
    def __init__(self, ws):
        self._ws = ws
        self._pending = {}  # id -> Event
        self._results = {}  # id -> dict
        self._lock = threading.Lock()
        self._notify_cb = None
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_notify_cb(self, cb):
        self._notify_cb = cb

    def stop(self):
        self._running = False

    def _run(self):
        while self._running:
            try:
                raw = self._ws.recv()
                if not raw:
                    continue
                data = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break
            msg_id = data.get("id")
            if msg_id is not None:
                with self._lock:
                    ev = self._pending.get(msg_id)
                    if ev:
                        self._results[msg_id] = data
                        ev.set()
                        continue
            cb = self._notify_cb
            if cb:
                try:
                    cb(data)
                except Exception:
                    pass

    def call(self, msg: dict, timeout: float = 10) -> dict:
        method = msg.get("method", "?")
        msg_id = random.randint(1000000, 9999999)
        msg["id"] = msg_id
        ev = threading.Event()
        with self._lock:
            try:
                self._ws.send(json.dumps(msg))
            except Exception as e:
                raise RuntimeError(f"Send failed [{method}]: {e}")
            self._pending[msg_id] = ev
        ev.wait(timeout)
        with self._lock:
            self._pending.pop(msg_id, None)
            result = self._results.pop(msg_id, None)
        if result is None:
            raise RuntimeError(f"Signal timeout [{method}]")
        if not result.get("ok", False):
            err = result.get("errorReason", result)
            print(f"[Voice] Signal error [{method}]: {err}", flush=True)
            raise RuntimeError(f"Signal error [{method}]: {err}")
        return result


class VoiceClient:
    def __init__(self, api: KookAPI):
        self.api = api
        self.channel_id: Optional[str] = None
        self._ws = None
        self._reader: Optional[_WsReader] = None
        self._running = False
        self._pc: Optional[RTCPeerConnection] = None
        self._loop = None
        self._track: Optional[_AudioTrack] = None
        self._ffmpeg_proc: Optional[subprocess.Popen] = None
        self._cand: Optional[dict] = None
        self._transport_id = None
        self._existing_producers: list = []

    def join(self, channel_id: str, password: Optional[str] = None) -> dict:
        self.channel_id = channel_id
        last_error = None
        for attempt in range(8):
            try:
                self._cleanup()
                if self._ws:
                    self._ws.close()
                print(f"[Voice] Connecting (attempt {attempt+1})...", flush=True)
                self._connect_ws(channel_id)
                transport_id, ice_p, cand, fp, all_fps = self._signaling()
                self._cand = cand
                self._running = True
                self._try_webrtc(transport_id, ice_p, cand, fp, all_fps)
                print(f"[Voice] Joined channel {channel_id}", flush=True)
                threading.Thread(target=self._ping_loop, daemon=True).start()
                return {"audio_ssrc": "1357", "audio_pt": "100", "bitrate": 128,
                        "ip": cand["ip"], "port": cand["port"]}
            except RuntimeError as e:
                last_error = e
                time.sleep(1)
        raise RuntimeError(f"Join failed after 8 attempts: {last_error}")

    def _get_gateway_url(self, channel_id: str) -> str:
        import requests
        session = requests.Session()
        for k, v in self.api.http.headers.items():
            session.headers[k] = v
        for c in self.api.http.cookies:
            session.cookies.set_cookie(c)
        resp = session.get(
            "https://www.kookapp.cn/api/v3/gateway/voice",
            params={"channel_id": channel_id},
            timeout=5,
        )
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"Gateway error: {body.get('message', body)}")
        return body["data"]["gateway_url"]

    def _connect_ws(self, channel_id: str):
        gw_url = self._get_gateway_url(channel_id)
        self._ws = websocket.create_connection(
            gw_url, timeout=10, sslopt={"cert_reqs": ssl.CERT_NONE}
        )
        self._ws.settimeout(5)
        self._reader = _WsReader(self._ws)
        self._reader.set_notify_cb(self._on_notification)

    def _on_notification(self, data):
        method = (data.get("method") or data.get("event") or data.get("type")
                  or data.get("action") or data.get("notification") or "?")
        data_str = json.dumps(data, ensure_ascii=False)[:800]
        print(f"[Voice] NOTIFY: method={repr(method)} keys={list(data.keys())} data={data_str}", flush=True)
        if method in ("producerAdded", "newPeer", "newConsumer"):
            print(f"[Voice] Spawning handle_producer for {method}", flush=True)
            threading.Thread(target=self._handle_producer, args=(data,), daemon=True).start()

    def _try_consume(self, producer_id: str, kind: str = "audio"):
        if not producer_id or not self._transport_id:
            return
        try:
            cons_req = {
                "request": True, "id": 0, "method": "consume",
                "data": {
                    "transportId": self._transport_id,
                    "producerId": producer_id,
                    "kind": kind,
                    "rtpCapabilities": {"codecs": [{
                        "channels": 2, "clockRate": 48000,
                        "mimeType": "audio/opus",
                        "parameters": {"sprop-stereo": 1},
                        "payloadType": 100,
                    }]},
                },
            }
            cons_resp = self._reader.call(cons_req)
            print(f"[Voice] consume {producer_id} ok", flush=True)
        except Exception as e:
            print(f"[Voice] consume {producer_id} failed: {e}", flush=True)

    def _handle_producer(self, data):
        peer_id = data.get("data", {}).get("peerId", "") or data.get("peerId", "")
        print(f"[Voice] handle_producer peer_id={repr(peer_id)} self._peer_id={repr(self._peer_id)}", flush=True)
        if peer_id and peer_id == self._peer_id:
            print("[Voice] skip own producer", flush=True)
            return
        producer_id = data.get("data", {}).get("producerId", "") or data.get("producerId", "") or data.get("consumerId", "")
        if not producer_id:
            producer_id = peer_id  # fallback: peerId may double as producerId
        kind = data.get("data", {}).get("kind", "audio")
        print(f"[Voice] handle_producer: producer_id={repr(producer_id)} kind={repr(kind)} transport_id={repr(self._transport_id)}", flush=True)
        if not self._transport_id:
            print(f"[Voice] consume skipped: no transport", flush=True)
            return
        self._try_consume(producer_id, kind)

    def _ws_call(self, msg: dict) -> dict:
        return self._reader.call(msg)

    def _signaling(self):
        rtc_resp = self._ws_call({"request": True, "id": 0,
                                  "method": "getRouterRtpCapabilities", "data": {}})
        rtp_caps = rtc_resp.get("data", {})
        print(f"[Voice] routerRtpCapabilities: {json.dumps(rtp_caps, ensure_ascii=False)[:500]}", flush=True)
        join_resp = self._ws_call({
            "request": True, "id": 0, "method": "join",
            "data": {"displayName": "", "device": "web",
                     "rtpCapabilities": rtp_caps},
        })
        self._peer_id = join_resp.get("data", {}).get("peerId", "")
        self._existing_producers = []
        peers = join_resp.get("data", {}).get("peers", [])
        print(f"[Voice] join peer_id={self._peer_id} peers={json.dumps(peers, ensure_ascii=False)}", flush=True)
        if isinstance(peers, list):
            for p in peers:
                for prod in (p.get("producers") or []):
                    pid = prod.get("producerId") or prod.get("id")
                    if pid and prod.get("kind") == "audio":
                        print(f"[Voice] existing producer: {pid}", flush=True)
                        self._existing_producers.append(pid)
                if not p.get("producers") and p.get("id") and p["id"] != self._peer_id:
                    print(f"[Voice] no producers for peer {p['id']}, will try peerId as producerId", flush=True)
                    self._existing_producers.append(p["id"])
        tr = self._ws_call({"request": True, "id": 0,
                            "method": "createWebRtcTransport",
                            "data": {"forceTcp": False, "producing": True,
                                     "consuming": True}})
        self._transport_id = tr["data"]["id"]
        fps = tr["data"]["dtlsParameters"]["fingerprints"]
        return (
            tr["data"]["id"],
            tr["data"]["iceParameters"],
            tr["data"]["iceCandidates"][0],
            fps[0],
            fps,
        )

    def _cleanup(self):
        if self._pc is not None:
            loop = getattr(self, '_loop', None)
            if loop is not None and loop.is_running():
                fut = asyncio.run_coroutine_threadsafe(
                    self._pc.close(), loop
                )
                fut.result(timeout=5)
            self._pc = None
        self._track = None
        self._transport_id = None

    def _try_webrtc(self, transport_id, ice_p, cand, fp, all_fps=None):
        fp_lines = ""
        for f in (all_fps or [fp]):
            algo = f.get("algorithm", "sha-256")
            val = f.get("value", "")
            fp_lines += f"a=fingerprint:{algo} {val}\r\n"

        self._pc = None
        self._track = None
        self._transport_id = transport_id

        connected_ev = threading.Event()
        error_ev = threading.Event()
        error_msg = []
        setup_ok = threading.Event()

        def _run():
            async def _setup():
                config = RTCConfiguration(
                    iceServers=[RTCIceServer(urls="stun:stun.l.google.com:19302")]
                )
                pc = RTCPeerConnection(config)
                self._pc = pc
                conn_async = asyncio.Event()

                @pc.on("connectionstatechange")
                def on_conn():
                    state = pc.connectionState
                    print(f"[Voice] Connection state: {state}", flush=True)
                    if state in ("connected", "failed"):
                        conn_async.set()

                track = _AudioTrack()
                self._track = track
                pc.addTrack(track)

                @pc.on("track")
                def on_track(remote_track):
                    print(f"[Voice] Incoming {remote_track.kind} track (id={remote_track.id})", flush=True)
                    async def _recv():
                        import subprocess
                        player = None
                        first = True
                        n = 0
                        retries = 0
                        last_log = 0
                        while self._running and retries < 50:
                            try:
                                frame = await asyncio.wait_for(remote_track.recv(), timeout=5)
                                if first:
                                    sr = getattr(frame, 'sample_rate', 48000)
                                    ch = getattr(frame, 'layout', 'stereo')
                                    ch_n = 2 if 'stereo' in str(ch) else 1
                                    print(f"[Voice] Audio: {sr}Hz {ch} channels={ch_n}", flush=True)
                                    player = subprocess.Popen(
                                        ["aplay", "-r", str(sr), "-f", "S16_LE", "-c", str(ch_n)],
                                        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    )
                                    first = False
                                data = frame.planes[0].to_bytes()
                                if player:
                                    player.stdin.write(data)
                                n += 1
                                retries = 0  # reset on success
                                now = time.monotonic()
                                if now - last_log >= 5:
                                    print(f"[Voice] _recv: frame {n} {len(data)} bytes", flush=True)
                                    last_log = now
                            except asyncio.TimeoutError:
                                if n == 0:
                                    print(f"[Voice] _recv: waiting for first frame... ({retries})", flush=True)
                                retries += 1
                                if retries > 60:
                                    print(f"[Voice] _recv: no audio for 5min, giving up", flush=True)
                                    break
                                continue
                            except MediaStreamError:
                                retries += 1
                                print(f"[Voice] _recv: track ended, will retry ({retries})", flush=True)
                                await asyncio.sleep(2)
                            except Exception as e:
                                print(f"[Voice] _recv error: {type(e).__name__}: {e}", flush=True)
                                break
                        print(f"[Voice] _recv: exiting after {n} frames", flush=True)
                        if player:
                            player.stdin.close()
                            player.wait()
                    asyncio.ensure_future(_recv())

                offer = await pc.createOffer()
                await pc.setLocalDescription(offer)
                local_sdp = pc.localDescription.sdp
                print(f"[Voice] signalingState={pc.signalingState} iceGatheringState={pc.iceGatheringState}", flush=True)
                try:
                    tcs = pc.getTransceivers()
                    sndrs = sum(1 for t in tcs if t.sender is not None)
                    for t in tcs:
                        codecs = getattr(t, '_codecs', None)
                        cd = getattr(t, 'currentDirection', None)
                        mid = getattr(t, 'mid', None)
                        print(f"[Voice]  transceiver: kind={t.kind} mid={mid} curDir={cd} codecs={'yes' if codecs else 'no'}", flush=True)
                    print(f"[Voice] transceivers={len(tcs)} senders={sndrs}", flush=True)
                except Exception as e:
                    print(f"[Voice] transceiver info: {e}", flush=True)
                print(f"[Voice] Local offer SDP:\n{local_sdp[:800]}", flush=True)

                local_ice_ufrag = local_ice_pwd = None
                local_fp_algo = local_fp_val = None
                local_ssrc = None
                local_pt = None
                for line in local_sdp.split("\r\n"):
                    if line.startswith("a=ice-ufrag:"):
                        local_ice_ufrag = line[12:]
                    elif line.startswith("a=ice-pwd:"):
                        local_ice_pwd = line[10:]
                    elif line.startswith("a=fingerprint:sha-256"):
                        local_fp_algo = "sha-256"
                        local_fp_val = line[18:]
                    elif line.startswith("a=ssrc:"):
                        ss = line[7:].split(None, 1)[0]
                        if ss.isdigit():
                            local_ssrc = int(ss)
                    elif line.startswith("a=rtpmap:") and "opus" in line:
                        pt_str = line[9:].split(None, 1)[0]
                        if pt_str.isdigit():
                            local_pt = int(pt_str)
                print(f"[Voice] local: ufrag={local_ice_ufrag} ssrC={local_ssrc} pt={local_pt} fp={local_fp_algo}:{local_fp_val[:20] if local_fp_val else 'none'}", flush=True)
                if not all([local_ice_ufrag, local_ice_pwd, local_fp_val]):
                    raise RuntimeError("Missing local ICE/DTLS params in SDP")

                # Build remote answer SDP from server params (opts PT from our offer)
                pt = local_pt or 100
                remote_sdp = (
                    "v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\n"
                    f"m=audio 9 UDP/TLS/RTP/SAVPF {pt}\r\nc=IN IP4 0.0.0.0\r\n"
                    f"a=ice-ufrag:{ice_p['usernameFragment']}\r\n"
                    f"a=ice-pwd:{ice_p['password']}\r\n"
                    f"{fp_lines}"
                    "a=setup:passive\r\na=mid:0\r\n"
                    f"a=candidate:1 1 UDP 1 {cand['ip']} {cand['port']} typ host\r\n"
                    f"a=rtpmap:{pt} opus/48000/2\r\na=rtcp-mux\r\na=sendrecv\r\n"
                )
                print(f"[Voice] Remote answer SDP:\n{remote_sdp[:600]}", flush=True)
                await pc.setRemoteDescription(
                    RTCSessionDescription(sdp=remote_sdp, type="answer")
                )

                self._ws_call({
                    "request": True, "id": 0,
                    "method": "connectWebRtcTransport",
                    "data": {
                        "transportId": transport_id,
                        "dtlsParameters": {
                            "fingerprints": [
                                {"algorithm": local_fp_algo, "value": local_fp_val}
                            ],
                            "role": "client",
                        },
                        "iceParameters": {
                            "usernameFragment": local_ice_ufrag,
                            "password": local_ice_pwd,
                        },
                    },
                })

                try:
                    await asyncio.wait_for(conn_async.wait(), timeout=15)
                except asyncio.TimeoutError:
                    raise RuntimeError("DTLS timeout (15s)")
                if pc.connectionState != "connected":
                    raise RuntimeError(f"DTLS {pc.connectionState}")
                connected_ev.set()

                # Wait a tiny bit for dtls to settle, then force-start sender
                await asyncio.sleep(0.1)
                try:
                    tcs = pc.getTransceivers()
                    for t in tcs:
                        if t.sender is not None and t.sender.transport is not None:
                            dtls_state = t.sender.transport.state
                            print(f"[Voice] dtls state={dtls_state} dir={t.direction}", flush=True)
                            if dtls_state == "connected":
                                ssrc = t.sender._ssrc
                                print(f"[Voice] sender _ssrc={ssrc} transport={t.sender.transport}", flush=True)
                                # Build send params the same way aiortc does internally
                                from aiortc.rtcrtpparameters import RTCRtpSendParameters
                                params = RTCRtpSendParameters(
                                    codecs=t._codecs,
                                    headerExtensions=t._headerExtensions,
                                    muxId=t.mid,
                                )
                                params.rtcp.cname = "opencode"
                                params.rtcp.ssrc = ssrc
                                params.rtcp.mux = True
                                for c in params.codecs:
                                    print(f"[Voice]  codec: mime={c.mimeType} pt={c.payloadType} clock={c.clockRate}", flush=True)
                                # Check if sender was already auto-started
                                rtp_task = getattr(t.sender, '_RTCRtpSender__rtp_task', None)
                                if rtp_task and rtp_task.done() and not rtp_task.cancelled():
                                    exc = rtp_task.exception()
                                    print(f"[Voice] prev rtp_task crashed: {type(exc).__name__}: {exc}", flush=True)
                                    # Replace encoder with safe version and restart
                                    codec = params.codecs[0]
                                    try:
                                        t.sender._RTCRtpSender__encoder = _SafeOpusEncoder()
                                    except NameError:
                                        pass
                                    asyncio.ensure_future(t.sender._run_rtp(codec))
                                else:
                                    print(f"[Voice] calling sender.send() with {len(params.codecs)} codecs", flush=True)
                                    await t.sender.send(params)
                            if t.receiver is not None and t.receiver.transport is not None:
                                dtls_state = t.receiver.transport.state
                                if dtls_state == "connected":
                                    from aiortc.rtcrtpparameters import RTCRtpReceiveParameters
                                    params = RTCRtpReceiveParameters(
                                        codecs=t._codecs,
                                        headerExtensions=t._headerExtensions,
                                        muxId=t.mid,
                                        rtcp=None,
                                    )
                                    # Disable 30s idle timeout so the track stays alive
                                    # waiting for late consumers
                                    t.receiver._timeout = 999999
                                    print(f"[Voice] calling receiver.receive()", flush=True)
                                    await t.receiver.receive(params)
                                    print(f"[Voice] receiver.receive() done", flush=True)
                except Exception as e:
                    import traceback
                    print(f"[Voice] force start error: {type(e).__name__}: {e}", flush=True)
                    traceback.print_exc()

                ssrc = local_ssrc or 1357
                print(f"[Voice] produce: ssrc={ssrc} pt={pt}", flush=True)
                prod_resp = self._ws_call({
                    "request": True, "id": 0, "method": "produce",
                    "data": {
                        "appData": {}, "kind": "audio", "peerId": "",
                        "rtpParameters": {
                            "codecs": [{
                                "channels": 2, "clockRate": 48000,
                                "mimeType": "audio/opus",
                                "parameters": {"sprop-stereo": 1},
                                "payloadType": pt,
                            }],
                            "encodings": [{"ssrc": ssrc}],
                        },
                        "transportId": transport_id,
                    },
                })
                print(f"[Voice] produce response: {json.dumps(prod_resp, ensure_ascii=False)[:500]}", flush=True)
                setup_ok.set()

                # Monitor connection health
                while self._running:
                    if pc.connectionState in ("failed", "closed"):
                        print(f"[Voice] Connection {pc.connectionState}, reconnecting...", flush=True)
                        raise ReconnectNeeded()
                    await asyncio.sleep(1)

            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                while self._running:
                    try:
                        loop.run_until_complete(_setup())
                        break
                    except ReconnectNeeded:
                        print("[Voice] Restarting WebRTC...", flush=True)
                        if self._pc is not None:
                            try:
                                loop.run_until_complete(self._pc.close())
                            except Exception:
                                pass
                            self._pc = None
                        self._track = None
                        self._transport_id = None
                        continue
                    break
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                logger.debug("webrtc error: %s", msg)
                print(f"[Voice] WebRTC error: {msg}", flush=True)
                error_msg.append(msg)
                error_ev.set()
            finally:
                if self._pc is not None:
                    try:
                        loop.run_until_complete(self._pc.close())
                    except Exception:
                        pass
                    self._pc = None
                for t in asyncio.all_tasks(loop):
                    t.cancel()
                if not loop.is_closed():
                    loop.run_until_complete(loop.shutdown_asyncgens())
                    loop.close()

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        for _ in range(40):
            if connected_ev.is_set():
                break
            if error_ev.is_set():
                raise RuntimeError(f"WebRTC error before connected: {error_msg[0] if error_msg else 'unknown'}")
            time.sleep(0.25)
        else:
            if error_ev.is_set():
                raise RuntimeError(f"WebRTC error before connected: {error_msg[0] if error_msg else 'unknown'}")
            raise RuntimeError("WebRTC connection timeout")

        for _ in range(40):
            if setup_ok.is_set():
                break
            if error_ev.is_set():
                raise RuntimeError(f"WebRTC error after connected: {error_msg[0] if error_msg else 'unknown'}")
            time.sleep(0.25)
        else:
            raise RuntimeError("WebRTC setup timeout")

    def _ping_loop(self):
        while self._running and self._ws:
            time.sleep(30)
            try:
                self._ws.ping()
            except Exception:
                break

    def push_file(self, audio_file: str):
        if not self._track:
            raise RuntimeError("Not in a voice channel. Call join() first.")
        if not self._loop:
            raise RuntimeError("WebRTC loop not started")

        cmd = [
            "ffmpeg", "-y",
            "-i", audio_file,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "48000",
            "-ac", "2",
            "-f", "s16le",
            "-",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._ffmpeg_proc = proc
        CHUNK = 960 * 2 * 2

        def _feed():
            loop = self._loop
            track = self._track
            while self._running and loop is not None:
                data = proc.stdout.read(CHUNK)
                if not data or len(data) < CHUNK:
                    break
                loop.call_soon_threadsafe(track.push_pcm, data)
            proc.wait()

        threading.Thread(target=_feed, daemon=True).start()
        print(f"[Voice] Pushing: {audio_file}", flush=True)
        return proc

    def push_mic(self):
        if not self._track:
            raise RuntimeError("Not in a voice channel. Call join() first.")
        if not self._loop:
            raise RuntimeError("WebRTC loop not started")

        import shutil, os
        if not shutil.which('ffmpeg'):
            raise RuntimeError("ffmpeg not found, required for mic capture")

        pulse_dir = f'/run/user/{os.getuid()}/pulse'
        use_pulse = os.path.isdir(pulse_dir) and any(f.startswith('native') for f in os.listdir(pulse_dir))
        if use_pulse:
            cmd = ["ffmpeg", "-f", "pulse", "-i", "default",
                   "-ac", "2", "-ar", "48000", "-f", "s16le", "-"]
        else:
            cmd = ["ffmpeg", "-f", "alsa", "-i", "default",
                   "-ac", "2", "-ar", "48000", "-f", "s16le", "-"]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._ffmpeg_proc = proc
        CHUNK = 960 * 2 * 2

        def _feed():
            loop = self._loop
            track = self._track
            n = 0
            while self._running and loop is not None:
                data = proc.stdout.read(CHUNK)
                if not data or len(data) < CHUNK:
                    print(f"[Voice] _feed(mic): break after {n} chunks", flush=True)
                    break
                loop.call_soon_threadsafe(track.push_pcm, data)
                n += 1
                if n <= 3 or n % 100 == 0:
                    print(f"[Voice] _feed(mic): pushed chunk {n} ({len(data)} bytes)", flush=True)
            proc.wait()

        threading.Thread(target=_feed, daemon=True).start()
        print("[Voice] Mic streaming started", flush=True)
        return proc

    def stop(self):
        self._running = False
        if self._reader:
            self._reader.stop()
            self._reader = None
        if self._ffmpeg_proc:
            self._ffmpeg_proc.terminate()
            try:
                self._ffmpeg_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._ffmpeg_proc.kill()
            self._ffmpeg_proc = None
        if self._track:
            async def _s():
                self._track.stop()
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(_s(), self._loop)
        if self._pc:
            async def _c():
                await self._pc.close()
            try:
                if self._loop and self._loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(_c(), self._loop)
                    future.result(timeout=5)
            except Exception:
                pass
            self._pc = None
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self.channel_id:
            try:
                self.api.leave_voice(self.channel_id)
                print(f"[Voice] Left channel {self.channel_id}", flush=True)
            except Exception as e:
                print(f"[Voice] Leave failed: {e}", flush=True)
        self.channel_id = None
