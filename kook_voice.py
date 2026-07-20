import asyncio
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
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription

from kook_api import KookAPI

logger = logging.getLogger(__name__)
OPUS_SDP = (
    "v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 100\r\nc=IN IP4 0.0.0.0\r\n"
    "a=ice-ufrag:{ufrag}\r\na=ice-pwd:{pwd}\r\n"
    "a=fingerprint:{algo} {fp}\r\na=setup:actpass\r\na=mid:0\r\n"
    "a=candidate:1 1 UDP 1 {ip} {port} typ host\r\n"
    "a=rtpmap:100 opus/48000/2\r\na=rtcp-mux\r\na=sendrecv\r\n"
)


class _AudioTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self):
        super().__init__()
        self._queue = asyncio.Queue()

    def push_pcm(self, data: bytes):
        self._queue.put_nowait(data)

    def stop(self):
        self._queue.put_nowait(b"")

    async def recv(self):
        data = await self._queue.get()
        if not data:
            raise asyncio.CancelledError
        frame = AudioFrame(format="s16", layout="stereo", samples=960)
        frame.planes[0] = data
        frame.sample_rate = 48000
        return frame


class VoiceClient:
    def __init__(self, api: KookAPI):
        self.api = api
        self.channel_id: Optional[str] = None
        self._ws = None
        self._ws_lock = threading.Lock()
        self._running = False
        self._pc: Optional[RTCPeerConnection] = None
        self._track: Optional[_AudioTrack] = None
        self._ffmpeg_proc: Optional[subprocess.Popen] = None
        self._cand: Optional[dict] = None

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
                transport_id, ice_p, cand, fp = self._signaling()
                self._cand = cand
                self._try_webrtc(transport_id, ice_p, cand, fp)
                print(f"[Voice] Joined channel {channel_id}", flush=True)
                self._running = True
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

    def _ws_send(self, msg: dict):
        self._ws.send(json.dumps(msg))

    def _ws_recv(self) -> dict:
        resp = self._ws.recv()
        return json.loads(resp) if isinstance(resp, str) else json.loads(resp.decode())

    def _ws_call(self, msg: dict) -> dict:
        method = msg.get("method", "?")
        msg_id = random.randint(1000000, 9999999)
        msg["id"] = msg_id
        with self._ws_lock:
            self._ws_send(msg)
            while True:
                data = self._ws_recv()
                if data.get("id") == msg_id:
                    break
        if not data.get("ok", False):
            err = data.get("errorReason", data)
            print(f"[Voice] Signal error [{method}]: {err}", flush=True)
            raise RuntimeError(f"Signal error [{method}]: {err}")
        return data

    def _signaling(self):
        self._ws_call({"request": True, "id": 0,
                       "method": "getRouterRtpCapabilities", "data": {}})
        self._ws_call({"data": {"displayName": ""}, "id": 0,
                       "method": "join", "request": True})
        tr = self._ws_call({"request": True, "id": 0,
                            "method": "createWebRtcTransport",
                            "data": {"forceTcp": False, "producing": True,
                                     "consuming": True}})
        return (
            tr["data"]["id"],
            tr["data"]["iceParameters"],
            tr["data"]["iceCandidates"][0],
            tr["data"]["dtlsParameters"]["fingerprints"][0],
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

    def _try_webrtc(self, transport_id, ice_p, cand, fp):
        sdp_offer = OPUS_SDP.format(
            ufrag=ice_p["usernameFragment"],
            pwd=ice_p["password"],
            algo=fp["algorithm"],
            fp=fp["value"],
            ip=cand["ip"],
            port=cand["port"],
        )

        self._pc = None
        self._track = None
        self._transport_id = transport_id

        connected_ev = threading.Event()
        error_ev = threading.Event()
        error_msg = []
        setup_ok = threading.Event()

        def _run():
            async def _setup():
                pc = RTCPeerConnection()
                self._pc = pc
                conn_async = asyncio.Event()

                @pc.on("connectionstatechange")
                def on_conn():
                    state = pc.connectionState
                    if state in ("connected", "failed"):
                        conn_async.set()

                track = _AudioTrack()
                self._track = track
                pc.addTrack(track)

                @pc.on("track")
                def on_track(remote_track):
                    print(f"[Voice] Incoming {remote_track.kind} track", flush=True)
                    async def _recv():
                        import subprocess
                        player = None
                        first = True
                        while self._running:
                            try:
                                frame = await remote_track.recv()
                                if first:
                                    sr = getattr(frame, 'sample_rate', 48000)
                                    ch = getattr(frame, 'layout', 'stereo')
                                    ch_n = 2 if 'stereo' in str(ch) else 1
                                    print(f"[Voice] Audio: {sr}Hz {ch}", flush=True)
                                    player = subprocess.Popen(
                                        ["aplay", "-r", str(sr), "-f", "S16_LE", "-c", str(ch_n)],
                                        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    )
                                    first = False
                                data = frame.planes[0].to_bytes()
                                if player:
                                    player.stdin.write(data)
                            except Exception:
                                break
                        if player:
                            player.stdin.close()
                            player.wait()
                    asyncio.ensure_future(_recv())

                await pc.setRemoteDescription(
                    RTCSessionDescription(sdp=sdp_offer, type="offer")
                )
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)

                local_sdp = pc.localDescription.sdp
                local_ice_ufrag = local_ice_pwd = None
                local_fp_algo = local_fp_val = None
                for line in local_sdp.split("\r\n"):
                    if line.startswith("a=ice-ufrag:"):
                        local_ice_ufrag = line[12:]
                    elif line.startswith("a=ice-pwd:"):
                        local_ice_pwd = line[10:]
                    elif line.startswith("a=fingerprint:sha-256"):
                        local_fp_algo = "sha-256"
                        local_fp_val = line[18:]
                if not all([local_ice_ufrag, local_ice_pwd, local_fp_val]):
                    raise RuntimeError("Missing local ICE/DTLS params in SDP")

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
                    await asyncio.wait_for(conn_async.wait(), timeout=5)
                except asyncio.TimeoutError:
                    raise RuntimeError("DTLS timeout")
                if pc.connectionState != "connected":
                    raise RuntimeError(f"DTLS {pc.connectionState}")
                connected_ev.set()

                silence = b'\x00' * (960 * 2 * 2)
                async def _silence():
                    while self._running:
                        track.push_pcm(silence)
                        await asyncio.sleep(0.02)
                asyncio.ensure_future(_silence())

                self._ws_call({
                    "request": True, "id": 0, "method": "produce",
                    "data": {
                        "appData": {}, "kind": "audio", "peerId": "",
                        "rtpParameters": {
                            "codecs": [{
                                "channels": 2, "clockRate": 48000,
                                "mimeType": "audio/opus",
                                "parameters": {"sprop-stereo": 1},
                                "payloadType": 100,
                            }],
                            "encodings": [{"ssrc": 1357}],
                        },
                        "transportId": transport_id,
                    },
                })
                setup_ok.set()

                while self._running:
                    await asyncio.sleep(1)

            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_setup())
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
            while self._running and loop is not None:
                data = proc.stdout.read(CHUNK)
                if not data or len(data) < CHUNK:
                    break
                loop.call_soon_threadsafe(track.push_pcm, data)
            proc.wait()

        threading.Thread(target=_feed, daemon=True).start()
        print("[Voice] Mic streaming started", flush=True)
        return proc

    def stop(self):
        self._running = False
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
            asyncio.run_coroutine_threadsafe(_s(), self._loop)
        if self._pc:
            async def _c():
                await self._pc.close()
            try:
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
