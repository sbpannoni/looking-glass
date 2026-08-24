#!/usr/bin/env python3
"""Hermes LAN voice pipeline server (v3 — sessions, stop, approvals, partials).

WebSocket protocol (client → server):
  {"type":"start", "sample_rate":16000, "format":"pcm_s16le", "channels":1,
   "conversation": "looking-glass-main"?}          begin a turn (mid-turn = barge-in)
  <binary int16 16 kHz mono PCM chunks>
  {"type":"stop"}                            end of speech, process turn
  {"type":"stop_run"}                        halt the running agent turn
  {"type":"approval_decision", "run_id":..., "approval_id":..., "decision":"allow"|"deny"}

Server → client JSON events:
  status, transcript, partial_transcript, agent_status{thinking|tool_use|speaking},
  run_started{run_id}, approval_request{...}, error, done{timing}
plus binary 16 kHz mono int16 PCM TTS audio.

Brain: Hermes Agent API server via the Sessions API (/api/sessions/{id}/chat/stream),
which provides persistent conversation memory, run ids (stoppable), tool events,
and approval events. Falls back to direct Anthropic ("basic mode") if unreachable.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Iterator

import asyncssh
import requests
import uvicorn
import yaml
import numpy as np
from anthropic import Anthropic
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from RealtimeSTT import AudioToTextRecorder

try:
    import psutil
except ImportError:  # machines panel degrades gracefully
    psutil = None

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "server.yaml"
LOG_PATH = ROOT / "logs" / "latency.jsonl"
STATE_PATH = ROOT / "logs" / "hermes_sessions.json"
USAGE_PATH = ROOT / "logs" / "usage_stats.json"
_USAGE_LOCK = threading.Lock()


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def record_usage(llm_in: int = 0, llm_out: int = 0, turns: int = 0, tts_chars: int = 0) -> None:
    """Accumulate token/character usage into logs/usage_stats.json (total + per-day)."""
    with _USAGE_LOCK:
        try:
            data = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {"total": {}, "days": {}}
        day = data["days"].setdefault(_today(), {})
        for bucket in (data["total"], day):
            bucket["llm_in"] = bucket.get("llm_in", 0) + llm_in
            bucket["llm_out"] = bucket.get("llm_out", 0) + llm_out
            bucket["turns"] = bucket.get("turns", 0) + turns
            bucket["tts_chars"] = bucket.get("tts_chars", 0) + tts_chars
        # keep last 60 days
        for k in sorted(data["days"])[:-60]:
            del data["days"][k]
        USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        USAGE_PATH.write_text(json.dumps(data), encoding="utf-8")


def read_usage() -> dict:
    with _USAGE_LOCK:
        try:
            data = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {"total": {}, "days": {}}
    return {"total": data.get("total", {}), "today": data.get("days", {}).get(_today(), {})}
ENV_PATHS = [Path.home() / ".hermes" / ".env", ROOT / ".env"]
SENTENCE_RE = re.compile(r"(.+?[.!?])(?=\s|$)", re.DOTALL)
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
CODEBLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
# Secret-shaped strings are never sent to cloud TTS (privacy filter):
SECRET_RES = [
    re.compile(r"\b(?:api[_-]?key|secret|password|passwd|token|bearer|authorization)\b\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"\b(?:sk|pk|key|tok|ghp|xox[abp])[-_][A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\b[A-Za-z0-9+/_\-]{36,}\b"),          # long opaque blobs (keys, JWT segments)
    re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.DOTALL),
]


def load_env() -> None:
    for path in ENV_PATHS:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


@dataclass
class TurnTiming:
    turn_id: int
    audio_start_monotonic: float | None = None
    end_of_speech_monotonic: float | None = None
    stt_start_monotonic: float | None = None
    stt_final_monotonic: float | None = None
    llm_start_monotonic: float | None = None
    llm_first_token_monotonic: float | None = None
    first_sentence_monotonic: float | None = None
    tts_request_start_monotonic: float | None = None
    first_tts_audio_byte_monotonic: float | None = None
    total_done_monotonic: float | None = None
    transcript: str = ""
    response_text: str = ""
    stt_model: str = ""
    llm_provider: str = ""
    llm_model: str = ""
    tts_model: str = ""
    voice_id: str = ""
    run_id: str = ""
    interrupted: bool = False
    tools_used: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        eos = self.end_of_speech_monotonic
        return {
            "turn_id": self.turn_id,
            "transcript": self.transcript,
            "response_text": self.response_text,
            "stt_model": self.stt_model,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "tts_model": self.tts_model,
            "voice_id": self.voice_id,
            "run_id": self.run_id,
            "interrupted": self.interrupted,
            "tools_used": self.tools_used,
            "stt_finalize_seconds": self._delta(self.stt_start_monotonic, self.stt_final_monotonic),
            "llm_time_to_first_token_seconds": self._delta(self.llm_start_monotonic, self.llm_first_token_monotonic),
            "time_to_first_tts_audio_byte_seconds": self._delta(self.tts_request_start_monotonic, self.first_tts_audio_byte_monotonic),
            "end_of_speech_to_first_audio_seconds": self._delta(eos, self.first_tts_audio_byte_monotonic),
            "total_turn_seconds": self._delta(eos, self.total_done_monotonic),
            "errors": self.errors,
        }

    @staticmethod
    def _delta(start: float | None, end: float | None) -> float | None:
        if start is None or end is None:
            return None
        return round(end - start, 4)


# ===================================================================== Hermes


class HermesAPI:
    """Thin client for the Hermes Agent API server (sessions, runs, approvals)."""

    def __init__(self, cfg: dict):
        self.cfg = cfg.get("hermes") or {}

    @property
    def base(self) -> str:
        return (self.cfg.get("base_url") or "http://127.0.0.1:8642").rstrip("/")

    def headers(self) -> dict:
        key = os.environ.get(self.cfg.get("api_key_env", "API_SERVER_KEY"), "")
        if not key:
            raise RuntimeError("Hermes API key not found in environment")
        h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        if self.cfg.get("session_key"):
            h["X-Hermes-Session-Key"] = self.cfg["session_key"]
        return h

    # ---- persistent named sessions ----
    def _load_state(self) -> dict:
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self, state: dict) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")

    def get_session_id(self, name: str, force_new: bool = False) -> str:
        state = self._load_state()
        sid = state.get(name)
        if sid and not force_new:
            return sid
        r = requests.post(f"{self.base}/api/sessions", headers=self.headers(),
                          json={"title": name}, timeout=15)
        r.raise_for_status()
        data = r.json()
        sid = (data.get("session") or data).get("id")
        state[name] = sid
        self._save_state(state)
        print(f"Created Hermes session '{name}' -> {sid}", flush=True)
        return sid

    def stop_run(self, run_id: str) -> dict:
        r = requests.post(f"{self.base}/v1/runs/{run_id}/stop", headers=self.headers(), timeout=15)
        return {"status_code": r.status_code, "body": r.text[:300]}

    def post_approval(self, run_id: str, body: dict) -> dict:
        r = requests.post(f"{self.base}/v1/runs/{run_id}/approval", headers=self.headers(),
                          json=body, timeout=15)
        return {"status_code": r.status_code, "body": r.text[:300]}

    def chat_stream_events(self, session_id: str, input_text: str, timeout: float) -> Iterator[tuple[str, str]]:
        """Yield ("run"|"text"|"tool"|"approval"|"final", value) from a session turn."""
        resp = requests.post(
            f"{self.base}/api/sessions/{session_id}/chat/stream",
            headers={**self.headers(), "Accept": "text/event-stream"},
            json={"input": input_text}, stream=True, timeout=(10, timeout),
        )
        if resp.status_code >= 400:
            resp.close()
            raise RuntimeError(f"Hermes session chat HTTP {resp.status_code}: {resp.text[:300]}")
        resp.encoding = "utf-8"  # SSE has no charset header; requests would assume latin-1 (mojibake)
        try:
            yield from self._parse_sse(resp)
        finally:
            resp.close()  # leaked FDs killed the server once (launchd limit is tiny)

    @staticmethod
    def _parse_sse(resp) -> Iterator[tuple[str, str]]:
        event_name = ""
        for raw in resp.iter_lines(decode_unicode=True):
            if raw is None:
                continue
            if raw.startswith("event: "):
                event_name = raw[7:].strip()
                continue
            if not raw.startswith("data: "):
                continue
            data_text = raw[6:].strip()
            try:
                data = json.loads(data_text)
            except json.JSONDecodeError:
                continue
            ev = event_name or data.get("event", "")
            if ev == "run.started":
                yield ("run", data.get("run_id") or "")
            elif ev == "assistant.delta":
                d = data.get("delta") or ""
                if d:
                    yield ("text", d)
            elif ev == "tool.started":
                name = data.get("tool_name") or "tool"
                if name.startswith("_"):
                    continue  # internal pseudo-tools like _thinking
                yield ("tool", json.dumps({"name": name, "preview": (data.get("preview") or "")[:200]}))
            elif "approval" in ev:
                yield ("approval", json.dumps(data)[:2000])
            elif ev == "assistant.completed":
                yield ("final", json.dumps({
                    "content": data.get("content") or "",
                    "interrupted": bool(data.get("interrupted")),
                }))
            elif ev in ("run.failed", "error"):
                raise RuntimeError(f"Hermes stream error: {data_text[:300]}")
            elif ev == "run.completed":
                usage = data.get("usage") or {}
                if usage:
                    record_usage(
                        llm_in=int(usage.get("input_tokens") or 0),
                        llm_out=int(usage.get("output_tokens") or 0),
                        turns=1,
                    )
            elif ev == "done":
                pass  # stream closes after this


# ==================================================================== Pipeline


def _resample_pcm16(pcm_bytes: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear-interpolation resample of 16-bit mono PCM. Adequate quality
    for speech; avoids adding scipy as a dependency for this one step."""
    if src_rate == dst_rate or not pcm_bytes:
        return pcm_bytes
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    duration = len(samples) / src_rate
    dst_n = int(round(duration * dst_rate))
    if dst_n <= 0:
        return b""
    src_idx = np.linspace(0, len(samples) - 1, num=dst_n)
    resampled = np.interp(src_idx, np.arange(len(samples)), samples)
    return resampled.astype(np.int16).tobytes()


_PIPER_VOICE = None
_PIPER_VOICE_MODEL = None


def _get_piper_voice():
    global _PIPER_VOICE, _PIPER_VOICE_MODEL
    voice_cfg = CFG.get("voice") or {}
    model_name = os.environ.get("LOOKING_GLASS_PIPER_MODEL") or voice_cfg.get("piper_model", "en_US-lessac-medium")
    if _PIPER_VOICE is None or _PIPER_VOICE_MODEL != model_name:
        from piper import PiperVoice
        if model_name.endswith(".onnx"):
            model_path = model_name
        else:
            model_path = str(ROOT / "models" / f"{model_name}.onnx")
        _PIPER_VOICE = PiperVoice.load(model_path)
        _PIPER_VOICE_MODEL = model_name
    return _PIPER_VOICE


def _piper_synthesis_config():
    """Build a piper.SynthesisConfig from voice.* knobs in server.yaml, so
    pacing/expressiveness are tunable without code changes. Defaults match
    Piper's own library defaults except length_scale, which defaults
    slightly slower (1.1) -- the bare 1.0 default reads as rushed on longer
    text with this fork's voices, per live listening feedback 2026-06-23."""
    from piper import SynthesisConfig
    voice_cfg = CFG.get("voice") or {}
    return SynthesisConfig(
        length_scale=voice_cfg.get("length_scale", 1.1),
        noise_scale=voice_cfg.get("noise_scale", 0.667),
        noise_w_scale=voice_cfg.get("noise_w_scale", 0.8),
        volume=voice_cfg.get("volume", 1.0),
        normalize_audio=voice_cfg.get("normalize_audio", True),
    )


def _tts_piper_chunks(text: str) -> Iterator[bytes]:
    """Local, free TTS via Piper, resampled to 16kHz mono to match the HUD's
    hardcoded AudioContext sample rate (see hud/index.html). piper-tts's
    synthesize() yields one AudioChunk per sentence (not raw per-callback
    bytes) -- audio_int16_bytes on each chunk is the actual PCM16 payload."""
    voice = _get_piper_voice()
    src_rate = voice.config.sample_rate
    syn_config = _piper_synthesis_config()
    for chunk in voice.synthesize(text, syn_config=syn_config):
        yield _resample_pcm16(chunk.audio_int16_bytes, src_rate, 16000)


class VoicePipelineServer:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.turn_counter = 0
        self.hermes = HermesAPI(cfg)
        self.stt_lock = asyncio.Lock()
        self.recorder = AudioToTextRecorder(
            model=cfg["stt"]["model"],
            use_microphone=False,
            spinner=False,
            device=cfg["stt"].get("device", "cpu"),
            compute_type=cfg["stt"].get("compute_type", "int8"),
            sample_rate=int(cfg["stt"].get("sample_rate", 16000)),
            language="en",
            beam_size=1,
            faster_whisper_vad_filter=False,
            no_log_file=True,
        )

    def next_turn_id(self) -> int:
        self.turn_counter += 1
        return self.turn_counter

    async def transcribe(self, audio: bytes, timing: TurnTiming | None = None) -> str:
        if timing:
            timing.stt_start_monotonic = time.perf_counter()
        # 1) GPU worker (if configured and reachable) — big model, ~0.3s
        remote = self.cfg["stt"].get("remote") or {}
        if remote.get("url"):
            text = await asyncio.to_thread(self._remote_stt, audio, remote)
            if text is not None:
                if timing:
                    timing.stt_model = f"remote:{remote.get('name', 'gpu')}"
                    timing.stt_final_monotonic = time.perf_counter()
                return text
        # 2) local Whisper fallback
        sample_rate = int(self.cfg["stt"].get("sample_rate", 16000))
        samples = (np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0).copy()
        try:
            async with self.stt_lock:
                self.recorder.feed_audio(samples, original_sample_rate=sample_rate)
                text = await asyncio.to_thread(self.recorder.perform_final_transcription, samples, True)
                self.recorder.clear_audio_queue()
        except Exception as exc:
            # near-silent audio can make whisper raise ("No clip timestamps found");
            # treat as empty transcript instead of failing the turn
            print(f"local STT error treated as empty transcript: {exc}", flush=True)
            text = ""
        if timing:
            timing.stt_final_monotonic = time.perf_counter()
        return (text or "").strip()

    def _remote_stt(self, audio: bytes, remote: dict) -> str | None:
        """POST raw PCM to the GPU STT worker. None = unavailable (use fallback)."""
        headers = {"Content-Type": "application/octet-stream"}
        token = os.environ.get(remote.get("token_env", "LOOKING_GLASS_HUD_TOKEN"), "")
        if token:
            headers["X-Looking-Glass-Token"] = token
        try:
            r = requests.post(remote["url"], data=audio, headers=headers,
                              timeout=float(remote.get("timeout", 6)))
            if r.ok:
                return (r.json().get("text") or "").strip()
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------ LLM

    def stream_llm_events_sync(
        self, transcript: str, timing: TurnTiming, conversation: str,
    ) -> Iterator[tuple[str, str]]:
        llm = self.cfg["llm"]
        provider = llm["provider"]
        timing.llm_start_monotonic = time.perf_counter()
        if provider == "hermes":
            try:
                h = self.cfg.get("hermes") or {}
                session_id = self.hermes.get_session_id(conversation)
                gen = self._hermes_turn(session_id, transcript, timing, h, conversation)
                first = next(gen)
            except StopIteration:
                return
            except Exception as exc:
                fb = (self.cfg.get("hermes") or {}).get("fallback_provider", "anthropic")
                print(f"Hermes unavailable ({type(exc).__name__}: {exc}); fallback={fb}", flush=True)
                timing.errors.append(f"hermes_fallback: {exc}")
                if not fb:
                    raise
                yield ("text", "Agent backend offline. Running in basic mode. ")
                provider = fb
            else:
                yield first
                yield from gen
                return
        timing.llm_provider = provider
        timing.llm_model = llm["model"]
        if provider == "anthropic":
            key = os.environ.get(llm.get("api_key_env", "ANTHROPIC_API_KEY"))
            if not key:
                raise RuntimeError("ANTHROPIC_API_KEY not found")
            client = Anthropic(api_key=key)
            with client.messages.stream(
                model=llm["model"],
                max_tokens=int(llm.get("max_tokens", 220)),
                temperature=float(llm.get("temperature", 0.3)),
                system=self.cfg["persona"]["system_prompt"],
                messages=[{"role": "user", "content": transcript}],
            ) as stream:
                for text in stream.text_stream:
                    if text and timing.llm_first_token_monotonic is None:
                        timing.llm_first_token_monotonic = time.perf_counter()
                    yield ("text", text)
        else:
            raise RuntimeError(f"Unsupported LLM provider: {provider}")

    def _hermes_turn(
        self, session_id: str, transcript: str, timing: TurnTiming, h: dict, conversation: str,
    ) -> Iterator[tuple[str, str]]:
        timing.llm_provider = "hermes"
        timing.llm_model = "hermes-agent"
        timeout = float(h.get("timeout", 240))
        try:
            it = self.hermes.chat_stream_events(session_id, transcript, timeout)
            for kind, value in it:
                if kind == "text" and timing.llm_first_token_monotonic is None:
                    timing.llm_first_token_monotonic = time.perf_counter()
                yield (kind, value)
        except RuntimeError as exc:
            # stale session id (e.g. Hermes DB reset) -> recreate once
            if "404" in str(exc):
                session_id = self.hermes.get_session_id(conversation, force_new=True)
                for kind, value in self.hermes.chat_stream_events(session_id, transcript, timeout):
                    if kind == "text" and timing.llm_first_token_monotonic is None:
                        timing.llm_first_token_monotonic = time.perf_counter()
                    yield (kind, value)
            else:
                raise

    # ------------------------------------------------------------------ TTS

    def tts_chunks_sync(self, text: str, timing: TurnTiming) -> Iterator[bytes]:
        voice = self.cfg["voice"]
        timing.tts_request_start_monotonic = timing.tts_request_start_monotonic or time.perf_counter()
        record_usage(tts_chars=len(text))
        provider = voice.get("provider", "elevenlabs")

        if provider == "piper":
            timing.tts_model = "piper"
            timing.voice_id = voice.get("piper_model", "local")
            for chunk in _tts_piper_chunks(text):
                if timing.first_tts_audio_byte_monotonic is None:
                    timing.first_tts_audio_byte_monotonic = time.perf_counter()
                yield chunk
            return

        key = os.environ.get("ELEVENLABS_API_KEY") or os.environ.get("ELEVEN_API_KEY") or os.environ.get("XI_API_KEY")
        if not key:
            raise RuntimeError("ElevenLabs API key not found")
        timing.tts_model = voice["model"]
        timing.voice_id = voice["voice_id"]
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice['voice_id']}/stream"
        params = {"output_format": voice.get("output_format", "pcm_16000")}
        payload = {
            "text": text,
            "model_id": voice["model"],
            "voice_settings": {
                "stability": 0.55, "similarity_boost": 0.70,
                "style": 0.10, "use_speaker_boost": True,
            },
        }
        response = requests.post(
            url, params=params,
            headers={"xi-api-key": key, "Accept": "application/octet-stream", "Content-Type": "application/json"},
            json=payload, stream=True, timeout=120,
        )
        if response.status_code >= 400:
            response.close()
            raise RuntimeError(f"ElevenLabs HTTP {response.status_code}: {response.text[:1000]}")
        try:
            for chunk in response.iter_content(chunk_size=4096):
                if not chunk:
                    continue
                if timing.first_tts_audio_byte_monotonic is None:
                    timing.first_tts_audio_byte_monotonic = time.perf_counter()
                yield chunk
        finally:
            response.close()  # barge-in cancels mid-stream; don't leak the connection

    # ------------------------------------------------------------- Turn flow

    async def stream_response_audio(
        self, ws: WebSocket, transcript: str, timing: TurnTiming, conn: "ConnState",
    ) -> None:
        pending = ""
        full_response: list[str] = []
        spoken = False
        await ws.send_json({"type": "agent_status", "state": "thinking"})

        q: asyncio.Queue = asyncio.Queue()

        async def forward() -> None:
            try:
                async for item in self._async_llm_events(transcript, timing, conn.conversation):
                    await q.put(item)
                await q.put(None)
            except Exception as exc:
                await q.put(exc)

        forward_task = asyncio.create_task(forward())
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                kind, value = item
                if kind == "run":
                    timing.run_id = value
                    conn.current_run_id = value
                    await ws.send_json({"type": "run_started", "run_id": value})
                    continue
                if kind == "tool":
                    info = json.loads(value)
                    timing.tools_used.append(info.get("name", "tool"))
                    await ws.send_json({"type": "agent_status", "state": "tool_use",
                                        "tool": info.get("name"), "preview": info.get("preview", "")})
                    matched = _match_topology_node(info.get("name"), info.get("preview"))
                    await _broadcast_network_activity(matched or "hermes", "hermes", "pulse")
                    continue
                if kind == "approval":
                    await ws.send_json({"type": "approval_request", "data": json.loads(value),
                                        "run_id": conn.current_run_id})
                    continue
                if kind == "final":
                    info = json.loads(value)
                    timing.interrupted = info.get("interrupted", False)
                    continue
                # kind == "text"
                full_response.append(value)
                pending += value
                sentences, pending = self._extract_complete_sentences(pending)
                for sentence in sentences:
                    clean = self._clean_for_tts(sentence)
                    if not clean:
                        continue
                    if timing.first_sentence_monotonic is None:
                        timing.first_sentence_monotonic = time.perf_counter()
                    if not spoken:
                        await ws.send_json({"type": "agent_status", "state": "speaking"})
                        spoken = True
                    conn.spoken_sentences.append(clean)
                    await self._send_tts_sentence(ws, clean, timing)
            tail = self._clean_for_tts(pending.strip())
            if tail:
                conn.spoken_sentences.append(tail)
                await self._send_tts_sentence(ws, tail, timing)
        finally:
            if not forward_task.done():
                forward_task.cancel()
        timing.response_text = "".join(full_response).strip()

    async def _async_llm_events(
        self, transcript: str, timing: TurnTiming, conversation: str,
    ) -> AsyncIterator[tuple[str, str]]:
        q: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def worker() -> None:
            try:
                for item in self.stream_llm_events_sync(transcript, timing, conversation):
                    loop.call_soon_threadsafe(q.put_nowait, item)
                loop.call_soon_threadsafe(q.put_nowait, None)
            except Exception as exc:
                loop.call_soon_threadsafe(q.put_nowait, exc)

        worker_task = asyncio.create_task(asyncio.to_thread(worker))
        while True:
            item = await q.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            yield item
        await worker_task

    async def _send_tts_sentence(self, ws: WebSocket, sentence: str, timing: TurnTiming) -> None:
        if not sentence:
            return
        q: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def worker() -> None:
            try:
                for chunk in self.tts_chunks_sync(sentence, timing):
                    loop.call_soon_threadsafe(q.put_nowait, chunk)
                loop.call_soon_threadsafe(q.put_nowait, None)
            except Exception as exc:
                loop.call_soon_threadsafe(q.put_nowait, exc)

        worker_task = asyncio.create_task(asyncio.to_thread(worker))
        while True:
            item = await q.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            await ws.send_bytes(item)
        await worker_task

    @staticmethod
    def _extract_complete_sentences(text: str) -> tuple[list[str], str]:
        sentences = []
        last_end = 0
        for match in SENTENCE_RE.finditer(text):
            sentences.append(match.group(1).strip())
            last_end = match.end()
        return sentences, text[last_end:]

    @staticmethod
    def _clean_for_tts(text: str) -> str:
        if not text:
            return ""
        text = THINK_RE.sub("", text)
        for pattern in SECRET_RES:                  # privacy: never speak secrets
            text = pattern.sub(" redacted ", text)
        text = CODEBLOCK_RE.sub(" code omitted. ", text)
        text = re.sub(r"`([^`]*)`", r"\1", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"^[\s>*#-]+", "", text)
        text = re.sub(r"[*_#]{1,3}([^*_#]+)[*_#]{1,3}", r"\1", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def log_turn(self, timing: TurnTiming) -> None:
        timing.total_done_monotonic = timing.total_done_monotonic or time.perf_counter()
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        summary = timing.summary()
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")
        print("TURN TIMING", json.dumps(summary, ensure_ascii=False), flush=True)


load_env()
CFG = load_config()
HERMES = HermesAPI(CFG)   # lightweight API client - independent of the STT pipeline
PIPELINE: VoicePipelineServer | None = None


_PIPELINE_LOCK = threading.Lock()


def get_pipeline() -> VoicePipelineServer:
    """Lock prevents the four uvicorn listeners' startup hooks from racing
    into concurrent recorder inits (which crashed three of the four lifespans
    and silently killed the TLS ports)."""
    global PIPELINE
    if PIPELINE is None:
        with _PIPELINE_LOCK:
            if PIPELINE is None:
                PIPELINE = VoicePipelineServer(CFG)
    return PIPELINE


app = FastAPI(title="Hermes Voice Pipeline")


@app.on_event("startup")
async def warm_pipeline() -> None:
    """Warm the local Whisper fallback in the BACKGROUND, exactly once (this
    hook fires once per uvicorn listener — there are four), and never let a
    warm failure take a listener down."""
    global _WARM_STARTED
    if _WARM_STARTED:
        return
    _WARM_STARTED = True

    async def warm() -> None:
        try:
            await asyncio.to_thread(get_pipeline)
            print("STT pipeline warmed.", flush=True)
        except Exception as exc:
            print(f"STT warm failed (remote STT still available): {exc}", flush=True)

    asyncio.get_running_loop().create_task(warm())


_WARM_STARTED = False


# ------------------------------------------------------------------ Auth

ALLOWED_ORIGIN_HOSTS = {"looking-glass.local", "looking-glass", "localhost", "127.0.0.1"}
ALLOWED_ORIGIN_HOSTS |= set((CFG.get("security") or {}).get("extra_origin_hosts") or [])


def hud_token() -> str | None:
    env_name = (CFG.get("security") or {}).get("hud_token_env", "LOOKING_GLASS_HUD_TOKEN")
    return os.environ.get(env_name) or None


def _request_authed(request: Request) -> bool:
    token = hud_token()
    if not token:
        return True
    supplied = request.headers.get("x-looking-glass-token") or request.cookies.get("looking_glass_token")
    return supplied == token


@app.middleware("http")
async def api_auth_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/") and not _request_authed(request):
        return Response(status_code=401, content="looking glass auth required")
    return await call_next(request)


@app.middleware("http")
async def hud_no_cache_middleware(request: Request, call_next):
    """Force revalidation of HUD assets.

    StaticFiles sends ETag + Last-Modified but no Cache-Control, so browsers
    fall back to *heuristic* freshness (roughly 10% of the file's age) and can
    keep serving a stale HUD for hours after a deploy — the HUD is edited in
    place and usually left open on a long-lived tab, so this bit us. "no-cache"
    means "revalidate before reuse", not "don't cache": unchanged files still
    answer with a cheap 304 off the ETag StaticFiles already provides.
    """
    response = await call_next(request)
    if request.url.path.startswith("/hud"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def _ws_allowed(ws: WebSocket) -> bool:
    """Browsers send Origin (+cookie); native clients (PTT, tests) send neither."""
    origin = ws.headers.get("origin")
    if not origin:
        return True  # non-browser client on the LAN (Python PTT, e2e tests)
    from urllib.parse import urlparse
    host = (urlparse(origin).hostname or "").lower()
    if host not in ALLOWED_ORIGIN_HOSTS:
        return False
    token = hud_token()
    if not token:
        return True
    return ws.cookies.get("looking_glass_token") == token or ws.query_params.get("token") == token


# ------------------------------------------------------------- Terminal panel

TERMINAL_HOSTS: dict[str, dict[str, str]] = {
    "snarf":          {"host": "192.168.1.239", "user": "sam"},
    "r720":           {"host": "192.168.1.61",  "user": "sam"},
    "octominer":      {"host": "192.168.1.50",  "user": "root"},
    "beelink":        {"host": "192.168.1.158", "user": "root"},
    "claude-control": {"host": "192.168.1.157", "user": "root"},
    "hermes":         {"host": "192.168.1.159", "user": "root"},
    "looking-glass":  {"host": "127.0.0.1",     "user": "root"},
}


def is_allowed_terminal_host(host: str) -> bool:
    return host in TERMINAL_HOSTS


# --------------------------------------------------------------- HUD + proxy

HUD_DIR = ROOT / "hud"
ALLOWED_GET_PATHS = {
    "/health", "/health/detailed", "/v1/capabilities",
    "/v1/skills", "/v1/toolsets", "/api/jobs", "/api/sessions",
}


def _proxy_allowed(method: str, path: str) -> bool:
    if method == "GET":
        return path in ALLOWED_GET_PATHS or (
            path.startswith("/api/sessions/") and path.endswith("/messages")
        )
    if method == "POST":
        return path == "/v1/responses"
    return False


@app.api_route("/api/hermes/{path:path}", methods=["GET", "POST"])
async def hermes_proxy(path: str, request: Request) -> Response:
    target = "/" + path
    if not _proxy_allowed(request.method, target):
        return Response(status_code=403, content="path not allowed")
    hermes = HERMES
    body = await request.body()
    params = dict(request.query_params)

    def do_request() -> requests.Response:
        return requests.request(
            request.method, hermes.base + target, params=params,
            headers=hermes.headers(), data=body if body else None, timeout=300,
        )

    resp = await asyncio.to_thread(do_request)
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("Content-Type", "application/json"))


@app.post("/api/chat")
async def hud_chat(request: Request) -> JSONResponse:
    """Typed chat from the HUD — same Hermes session as voice."""
    body = await request.json()
    text = (body.get("input") or "").strip()
    conversation = body.get("conversation") or (CFG.get("hermes") or {}).get("conversation", "looking-glass-main")
    if not text:
        return JSONResponse({"error": "empty input"}, status_code=400)
    out: dict = {"text": "", "tools": [], "run_id": None}

    def run_sync() -> None:
        timeout = float((CFG.get("hermes") or {}).get("timeout", 240))

        def consume(sid: str) -> list[str]:
            parts: list[str] = []
            for kind, value in HERMES.chat_stream_events(sid, text, timeout):
                if kind == "text":
                    parts.append(value)
                elif kind == "tool":
                    out["tools"].append(json.loads(value))
                elif kind == "run":
                    out["run_id"] = value
                elif kind == "final":
                    info = json.loads(value)
                    if info.get("content"):
                        parts = [info["content"]]
            return parts

        try:
            parts = consume(HERMES.get_session_id(conversation))
        except RuntimeError as exc:
            if "404" not in str(exc):
                raise
            # stale session id (e.g. profile switch / DB reset) -> recreate once
            out["tools"].clear()
            parts = consume(HERMES.get_session_id(conversation, force_new=True))
        out["text"] = "".join(parts).strip()

    try:
        await asyncio.to_thread(run_sync)
        return JSONResponse(out)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


_ELEVEN_CACHE: dict = {"ts": 0.0, "data": None}


@app.get("/api/usage")
async def usage() -> JSONResponse:
    """LLM token usage (local tally) + ElevenLabs subscription quota."""
    u = read_usage()
    cost_cfg = CFG.get("usage") or {}
    cin = float(cost_cfg.get("llm_cost_per_mtok_input", 0) or 0)
    cout = float(cost_cfg.get("llm_cost_per_mtok_output", 0) or 0)

    def est(b: dict) -> float | None:
        if not (cin or cout):
            return None
        return round(b.get("llm_in", 0) / 1e6 * cin + b.get("llm_out", 0) / 1e6 * cout, 4)

    out = {
        "llm": {
            "today": u["today"], "total": u["total"],
            "today_cost": est(u["today"]), "total_cost": est(u["total"]),
        },
        "elevenlabs": None,
    }
    # ElevenLabs subscription — NEVER blocks the response: serve the cache and
    # refresh it in the background when stale.
    now = time.time()
    if (_ELEVEN_CACHE["data"] is None or now - _ELEVEN_CACHE["ts"] > 300) and not _ELEVEN_CACHE.get("refreshing"):
        key = os.environ.get("ELEVENLABS_API_KEY") or os.environ.get("ELEVEN_API_KEY") or os.environ.get("XI_API_KEY")
        if key:
            _ELEVEN_CACHE["refreshing"] = True

            def fetch() -> dict | None:
                try:
                    r = requests.get("https://api.elevenlabs.io/v1/user/subscription",
                                     headers={"xi-api-key": key}, timeout=10)
                    if r.ok:
                        j = r.json()
                        return {
                            "used": j.get("character_count"),
                            "limit": j.get("character_limit"),
                            "remaining": (j.get("character_limit") or 0) - (j.get("character_count") or 0),
                            "tier": j.get("tier"),
                            "resets_unix": j.get("next_character_count_reset_unix"),
                        }
                except Exception:
                    pass
                return None

            async def refresh() -> None:
                try:
                    data = await asyncio.to_thread(fetch)
                    if data is not None:
                        _ELEVEN_CACHE.update(ts=time.time(), data=data)
                finally:
                    _ELEVEN_CACHE["refreshing"] = False

            asyncio.get_running_loop().create_task(refresh())
    out["elevenlabs"] = _ELEVEN_CACHE["data"]
    return JSONResponse(out)


WS_CLIENTS: set = set()
ACTIVE_TERMINALS: set[str] = set()   # hosts with a live /ws/terminal/{host} session right now


async def _broadcast_network_activity(node: str, source: str, state: str) -> None:
    """Pulse a node on the NETWORK MAP panel. source: "claude"|"hermes",
    state: "start"|"end"|"pulse". Ephemeral — not logged to ACTIVITY_LOG."""
    payload = {"type": "network_activity", "node": node, "source": source, "state": state}
    for client in list(WS_CLIENTS):
        try:
            await client.send_json(payload)
        except Exception:
            WS_CLIENTS.discard(client)


@app.post("/api/summon")
async def summon(request: Request) -> JSONResponse:
    """Broadcast a holographic media panel to all connected HUD clients.

    Body: {"media": "video"|"iframe"|"image", "src": "...", "title": "...",
           "position": "center"|"left"|"right"}  or  {"action": "dismiss"}
    Hermes can call this (curl with X-Looking-Glass-Token) to display media on the HUD.
    """
    body = await request.json()
    if body.get("action") == "dismiss":
        payload = {"type": "dismiss_panels"}
    else:
        payload = {"type": "summon_panel",
                   "media": body.get("media") or body.get("type") or "iframe",
                   "src": body.get("src", ""),
                   "title": body.get("title", "INCOMING FEED"),
                   "position": body.get("position", "center")}
    sent = 0
    for client in list(WS_CLIENTS):
        try:
            await client.send_json(payload)
            sent += 1
        except Exception:
            WS_CLIENTS.discard(client)
    return JSONResponse({"sent_to": sent})


_HA_NAME_RE = re.compile(r'^[a-z0-9_]+$')
_HA_ENTITY_RE = re.compile(r'^[a-z0-9_]+\.[a-z0-9_]+$')


@app.post("/api/ha/call_service")
async def ha_call_service(request: Request) -> JSONResponse:
    """Direct, deterministic Home Assistant control for dashboard UI clicks.
    Separate from Hermes's own native `homeassistant` toolset (used for
    chat/voice-driven control) — both call the same HA instance.
    Body: {"domain": "light", "service": "turn_on",
           "entity_id": "light.living_room", "data": {...}}
    """
    body = await request.json()
    domain = body.get("domain")
    service = body.get("service")
    if not domain or not service:
        return JSONResponse({"error": "domain and service are required"}, status_code=400)
    if not _HA_NAME_RE.match(domain) or not _HA_NAME_RE.match(service):
        return JSONResponse({"error": "domain and service must be lowercase alphanumeric/underscore"}, status_code=400)
    entity_id = body.get("entity_id")
    if entity_id and not _HA_ENTITY_RE.match(entity_id):
        return JSONResponse({"error": "entity_id must match 'domain.object_id'"}, status_code=400)

    ha_cfg = CFG.get("homeassistant") or {}
    base_url = ha_cfg.get("base_url")
    token = os.environ.get(ha_cfg.get("token_env", "HASS_TOKEN"))
    if not base_url or not token:
        return JSONResponse({"error": "Home Assistant not configured"}, status_code=503)

    payload = {"entity_id": body["entity_id"]} if body.get("entity_id") else {}
    payload.update(body.get("data") or {})

    try:
        response = await asyncio.to_thread(
            requests.post,
            f"{base_url}/api/services/{domain}/{service}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload, timeout=10,
        )
    except Exception as exc:
        return JSONResponse({"error": f"Home Assistant unreachable: {exc}"}, status_code=502)

    if response.status_code >= 400:
        return JSONResponse({"error": f"HA HTTP {response.status_code}"}, status_code=502)
    return JSONResponse({"ok": True, "result": response.json()})


_WORKER_CACHE: dict = {"ts": 0.0, "data": {}, "refreshing": False}


@app.get("/api/machines")
async def machines() -> JSONResponse:
    """The fleet, as actually probed.

    Sourced from NETWORK_TOPOLOGY_STATE rather than a second hand-maintained
    list: that poller already probes every node's ports concurrently every
    `network_topology.poll_seconds`, so this endpoint adds no I/O of its own and
    can never disagree with the network map about who is up.

    Enrichment, best-effort and never fatal:
      * this host        -> psutil (we are the looking-glass node)
      * `machines:` entry with a stats_url -> that worker's own stats agent
      * snarf            -> GPU temp / CPU load from the rack_health cache
    """
    topo_nodes = list(NETWORK_TOPOLOGY_STATE.get("nodes") or [])
    if not topo_nodes:
        return JSONResponse({"machines": [], "note": "topology not probed yet"})

    # Worker stats agents are keyed by host address so a config entry can enrich
    # whichever topology node it points at, regardless of what it is named.
    workers_by_addr = {
        str(w.get("host")): w for w in (CFG.get("machines") or []) if w.get("host")
    }

    def poll_worker(w: dict) -> dict:
        url = w.get("stats_url")
        if not url:
            return {}
        try:
            r = requests.get(url, timeout=2)
            if r.ok:
                return r.json()
        except Exception:
            pass
        return {}

    now = time.time()
    if workers_by_addr and now - _WORKER_CACHE["ts"] > 10 and not _WORKER_CACHE["refreshing"]:
        _WORKER_CACHE["refreshing"] = True

        async def refresh() -> None:
            try:
                data = {
                    addr: await asyncio.to_thread(poll_worker, w)
                    for addr, w in workers_by_addr.items()
                }
                _WORKER_CACHE.update(ts=time.time(), data=data)
            finally:
                _WORKER_CACHE["refreshing"] = False

        asyncio.get_running_loop().create_task(refresh())
    worker_stats = _WORKER_CACHE["data"] or {}

    # Warm the rack_health cache ourselves. It used to be read only if already
    # populated, but nothing in the HUD calls /api/rack_health, so "opportunistic"
    # meant "never" and snarf only ever showed bare "online". Refresh in the background so
    # the request still answers instantly from whatever is cached.
    if now - RACK_HEALTH_CACHE["ts"] > 30 and not RACK_HEALTH_CACHE.get("refreshing"):
        RACK_HEALTH_CACHE["refreshing"] = True

        async def warm_rack() -> None:
            try:
                rh_cfg = CFG.get("rack_health") or {}
                prom_url = rh_cfg.get("prometheus_url", "http://192.168.1.157:9090")
                queries = rh_cfg.get("queries") or {}

                def fetch() -> dict:
                    out: dict = {}
                    for name, promql in queries.items():
                        try:
                            out[name] = [
                                {"labels": r["metric"], "value": float(r["value"][1])}
                                for r in _prometheus_query(prom_url, promql)
                            ]
                        except Exception:
                            pass
                    return out

                RACK_HEALTH_CACHE.update(ts=time.time(), data=await asyncio.to_thread(fetch))
            finally:
                RACK_HEALTH_CACHE["refreshing"] = False

        asyncio.get_running_loop().create_task(warm_rack())
    rack = RACK_HEALTH_CACHE["data"] or {}

    def _rack_value(key: str) -> float | None:
        series = rack.get(key)
        if isinstance(series, list) and series:
            return series[0].get("value")
        return None

    # Physical hosts first, then containers, then services and out-of-band. The
    # OOB rows earn their place: a BMC that answers while its host is dark is
    # exactly how a mains cut is told apart from a machine that crashed.
    order = {"gpu-server": 0, "server": 1, "gpu-worker": 2, "proxmox": 3,
             "lxc": 4, "service": 5, "oob": 6}

    # Guests follow their host. A physical_group's id is also a node id (the
    # mini-PC itself), so the parent is the member whose id == the group id;
    # everything else in that group is a guest and gets indented under it.
    by_id = {n["id"]: n for n in topo_nodes}
    children: dict[str, list[dict]] = {}
    for node in topo_nodes:
        group = node.get("physical_group")
        if group and group != node["id"] and group in by_id:
            children.setdefault(group, []).append(node)

    nested = {n["id"] for kids in children.values() for n in kids}
    ordered: list[tuple[dict, int]] = []
    for node in sorted(topo_nodes, key=lambda n: (order.get(n.get("kind"), 9), n["id"])):
        if node["id"] in nested:
            continue                      # emitted beneath its host instead
        ordered.append((node, 0))
        for kid in sorted(children.get(node["id"], []),
                          key=lambda n: (order.get(n.get("kind"), 9), n["id"])):
            ordered.append((kid, 1))

    result: list[dict] = []
    for node, depth in ordered:
        info: dict = {
            "name": node["id"].upper().replace("-", " "),
            "kind": node.get("kind", "host"),
            "address": node.get("address"),
            "online": bool(node.get("up")),
            "depth": depth,
            "physical_group": node.get("physical_group"),
        }

        if node["id"] == "looking-glass" and psutil:
            info.update({
                "cpu": psutil.cpu_percent(interval=0.1),
                "mem": psutil.virtual_memory().percent,
                "disk": psutil.disk_usage(str(ROOT)).percent,
            })
        elif node["id"] == "snarf":
            temp = _rack_value("snarf_gpu_temp_c")
            load = _rack_value("snarf_cpu_load1")
            if temp is not None:
                info["gpu_temp"] = round(temp)
            if load is not None:
                info["load1"] = round(load, 2)
        elif node["id"] == "beelink":
            load = _rack_value("beelink_cpu_load1")
            if load is not None:
                info["load1"] = round(load, 2)

        stats = worker_stats.get(str(node.get("address")))
        if stats:
            info.update(stats)

        if not node.get("monitored", True):
            # Not a fault: nothing is asking. Reported separately from up/down so
            # a deliberately dark box never reads as an outage.
            info["monitored"] = False
            info["note"] = "not monitored"
        elif not info["online"]:
            info["note"] = "offline"
        result.append(info)

    return JSONResponse({"machines": result, "updated": NETWORK_TOPOLOGY_STATE.get("updated")})


RACK_HEALTH_CACHE: dict = {"ts": 0.0, "data": {}}


def _prometheus_query(prom_url: str, promql: str) -> list[dict]:
    response = requests.get(f"{prom_url}/api/v1/query", params={"query": promql}, timeout=5)
    response.raise_for_status()
    body = response.json()
    if body.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {body}")
    return body["data"]["result"]


@app.get("/api/rack_health")
async def rack_health() -> JSONResponse:
    """Custom glass-panel rack health, queried directly from Prometheus
    (not a Grafana iframe) so the HUD owns the visual presentation."""
    now = time.time()
    if now - RACK_HEALTH_CACHE["ts"] < 10:
        return JSONResponse(RACK_HEALTH_CACHE["data"])

    rh_cfg = CFG.get("rack_health") or {}
    prom_url = rh_cfg.get("prometheus_url", "http://192.168.1.157:9090")
    queries = rh_cfg.get("queries") or {}

    def fetch_all() -> dict:
        out: dict = {}
        for name, promql in queries.items():
            try:
                results = _prometheus_query(prom_url, promql)
                out[name] = [
                    {"labels": r["metric"], "value": float(r["value"][1])}
                    for r in results
                ]
            except Exception as exc:
                out[name] = {"error": str(exc)}
        return out

    data = await asyncio.to_thread(fetch_all)
    RACK_HEALTH_CACHE.update(ts=now, data=data)
    return JSONResponse(data)


BRAIN_CACHE: dict = {"ts": 0.0, "data": {}}


@app.get("/api/brain")
async def brain() -> JSONResponse:
    """What model Hermes is actually thinking with.

    The served model name exists only in vLLM's unit file on snarf; the Hermes
    dashboard just proxies the gateway and never sees it, which is why nothing
    in the stack could answer "which model is this?". Ask vLLM directly.
    """
    now = time.time()
    if now - BRAIN_CACHE["ts"] < 60:
        return JSONResponse(BRAIN_CACHE["data"])

    url = (CFG.get("brain") or {}).get("models_url", "http://192.168.1.239:8000/v1/models")

    def fetch() -> dict:
        try:
            r = requests.get(url, timeout=3)
            r.raise_for_status()
            entry = (r.json().get("data") or [{}])[0]
            return {
                "model": entry.get("id"),
                "max_model_len": entry.get("max_model_len"),
                "online": True,
            }
        except Exception as exc:
            return {"model": None, "online": False, "error": str(exc)}

    data = await asyncio.to_thread(fetch)
    BRAIN_CACHE.update(ts=now, data=data)
    return JSONResponse(data)


# `node`/`checkout` say where the code actually is, so the HUD can show which
# agent has native context for a project rather than just its issue counts. A
# project with no checkout anywhere in the fleet is tracked but unowned — that is
# a real state worth seeing, not a gap to paper over.
def _ownership_cfg() -> dict:
    return CFG.get("ownership") or {}


@app.get("/api/ownership")
async def ownership() -> JSONResponse:
    """Which node owns which domain, and which agent has native context.

    This exists because the answer was tribal knowledge: Hermes is configured on
    CT111 but has no agent installed there, the rack's power/monitoring history
    lives on CT110, and the HUD lives here — so work routinely got started on the
    wrong box and re-derived things another node had already written down.

    Live up/down is joined in from the topology poller so the diagram doubles as
    a status view rather than a static picture.
    """
    cfg = _ownership_cfg()
    up = {n["id"]: n.get("up") for n in (NETWORK_TOPOLOGY_STATE.get("nodes") or [])}
    virt = cfg.get("virt") or {}

    domains = []
    for d in cfg.get("domains") or []:
        node = d.get("node")
        domains.append({
            **d,
            "online": up.get(node),
            "virt": virt.get(node),
            # match on domain, not node: snarf hosts both DARKHELIX and the
            # vLLM unit, and the code belongs to only one of them
            "projects": [p["name"] for p in TRACKED_PROJECTS if p.get("domain") == d.get("id")],
        })

    return JSONResponse({
        "domains": domains,
        "virt": virt,
        "unowned": [p["name"] for p in TRACKED_PROJECTS if not p.get("node")],
    })


TRACKED_PROJECTS = [
    {"name": "my-website", "repo": "sbpannoni/my-website", "ref": "main", "path": "TODO.md", "parser": "checkbox",
     "node": None, "checkout": None},
    {"name": "DARKHELIX", "repo": "sbpannoni/DARKHELIX", "ref": "master", "path": "TODO.md", "parser": "checkbox",
     "node": "snarf", "checkout": "/ssdpool/DARKHELIX", "domain": "darkhelix"},
    {"name": "redqueen-website", "repo": "sbpannoni/redqueen-website", "ref": "main", "path": "TODO.md", "parser": "checkbox",
     "node": None, "checkout": None},
    {"name": "server", "repo": "sbpannoni/snarf", "ref": "main", "path": "STATUS.md", "parser": "status_table",
     "node": "claude-control", "checkout": None, "domain": "infra"},
    # No tracker file in these two. They still belong on the board — knowing where
    # a repo lives and who has native context is useful without a TODO count, and
    # omitting them made the list look like the whole fleet when it wasn't.
    {"name": "looking-glass", "repo": "sbpannoni/looking-glass", "ref": "main", "path": None, "parser": None,
     "node": "looking-glass", "checkout": "/opt/looking-glass", "domain": "hud"},
    {"name": "claude-config", "repo": "sbpannoni/claude-config", "ref": "main", "path": None, "parser": None,
     "node": "claude-control", "checkout": "/root/claude-config", "domain": "infra"},
]
_STATUS_EMOJI = {"✅": "done", "🔄": "in_progress", "🚧": "blocked", "📋": "todo"}
PROJECTS_CACHE: dict = {"ts": 0.0, "data": {}}


# ===================== CODER MODELS panel ============================
# The coder-engine (Hermes+LangGraph+Aider) pipeline's Phase 3 eval harness
# runs on snarf and appends one JSON record per (task, candidate model) run
# to pipeline/eval/results/eval_results.json. CT112 has no standing route to
# snarf's eval data, so a CT110 script (homelab/coder_models_refresh.py)
# pulls that file, aggregates it, and pushes the result here as a plain JSON
# file this endpoint reads from disk — same pull-then-push shape as the
# ownership panel's TRACKED_PROJECTS, just sourced from a different node.
# "Good for" text is static roster info (why this model was picked as a
# candidate), not derived from the eval data, since it doesn't change per run.
CODER_MODELS_METRICS_PATH = os.path.join(
    os.path.dirname(__file__), "config", "coder_model_metrics.json"
)
# Same pull-then-push shape as CODER_MODELS_METRICS_PATH, just for the
# reviewer role's catch-rate/false-positive-rate data (see coder_models_refresh.py
# on claude-control -- one script refreshes both files).
CODER_REVIEW_METRICS_PATH = os.path.join(
    os.path.dirname(__file__), "config", "coder_review_metrics.json"
)
# Static "what is this line" text for the transit map's non-editor lines.
# The reviewer line's per-model numbers come from CODER_REVIEW_METRICS_PATH;
# this only holds the roles/labels/mechanism text that doesn't change per run.
CODER_ORCH_METRICS_PATH = os.path.join(
    os.path.dirname(__file__), "config", "coder_orchestrator_metrics.json"
)
# Static framing text for the orchestrator line -- the mechanism itself
# (hermes kanban decompose/specify) is native and confirmed live; what CT110
# refreshes via CODER_ORCH_METRICS_PATH is which candidate model is actually
# good at driving it, benchmarked against real historically-grounded
# decompositions (see pipeline/eval/tasks/orchestrator/*.yaml).
CODER_TRANSIT_ORCHESTRATOR = {
    "status": "native mechanism (hermes kanban decompose/specify), now benchmarked",
    "note": (
        "hermes kanban decompose/specify already does LLM-driven task "
        "decomposition natively -- confirmed live, Revision 2 of the coder-engine "
        "plan. This local harness grades a candidate's proposed breakdown "
        "against the real, historically-grounded decomposition of an actual "
        "past DARKHELIX fix -- never against the live kanban board."
    ),
}
CODER_MODELS_ROSTER = [
    {
        "id": "qwen3.6-27b-awq", "label": "Qwen3.6 27B", "license": "Apache 2.0",
        "params": "27B dense, hybrid linear/full attention, 98K ctx (served)",
        "status": "resident default",
        "good_for": "Current pipeline default. Cited by multiple community sources as "
                     "the highest verified SWE-bench Verified score among models that "
                     "run on consumer hardware. General coding + reasoning. STABILITY-TESTED 2026-08-21: a --repeats 2 pass (plus one extra confirmatory run) put its true rate at 0.704 (19/27) -- higher than the original single-run 0.6 baseline, since one task (collab-compute-rpkm) that originally failed passed on all 3 retest attempts. A legitimate rival to Devstral now, not a clear-cut incumbent-vs-challenger case.",
    },
    {
        "id": "qwen2.5-32b-awq", "label": "Qwen2.5 32B", "license": "Apache 2.0",
        "params": "32B dense, 32K ctx",
        "status": "resident (rotation)",
        "good_for": "General-purpose, not code-specialized. Baseline the code-tuned "
                     "sibling (Qwen2.5-Coder-32B) gets compared against — same size "
                     "class, so the delta isolates what code-specific training buys.",
    },
    {
        "id": "llama3.1-70b-awq", "label": "Llama 3.1 70B", "license": "Llama 3.1 Community",
        "params": "70B dense, TP=2 required",
        "status": "resident (rotation)",
        "good_for": "Largest resident model. General-purpose, not code-tuned. Tests "
                     "whether raw scale beats smaller code-specialized models on these "
                     "chunk-sized edits.",
    },
    {
        "id": "qwen3-14b-awq", "label": "Qwen3 14B", "license": "Apache 2.0",
        "params": "14.8B dense, TP=1, 40K ctx",
        "status": "resident (rotation)",
        "good_for": "Smallest resident model — fits one GPU, fastest cold-swap. Speed/"
                     "quality floor reference for the rest of the roster.",
    },
    {
        "id": "devstral-small-2-24b-awq", "label": "Devstral Small 2 24B", "license": "Apache 2.0",
        "params": "24B dense",
        "status": "tested 2026-08-21 — 0.625 pass rate, CONFIRMED STABLE (best)",
        "good_for": "Purpose-built for agentic coding by Mistral + All Hands AI — "
                     "closest architectural match to what this pipeline actually does "
                     "(multi-step repo edits, test-gated, SWE-bench-style tasks). "
                     "CONFIRMED 2026-08-21: a --repeats 2 stability pass reproduced the "
                     "exact same outcome on every one of the 8 tasks across all 3 "
                     "attempts (24 total runs) — the only candidate that fully stable. "
                     "Also the fastest average time (60.3s) of the real contenders. "
                     "Clear leader pending Sam's final lock.",
    },
    {
        "id": "deepseek-r1-distill-qwen-32b-awq", "label": "DeepSeek-R1-Distill-Qwen 32B", "license": "MIT",
        "params": "32B dense, reasoning-distilled from DeepSeek-R1",
        "status": "tested 2026-08-21 — 0.583 pass rate across repeats, NOT stable, ~2.2x slower",
        "good_for": "Reasoning-distilled onto a Qwen2.5-32B base. Tests whether "
                     "reasoning-trace training helps on small, well-scoped edit tasks "
                     "vs. a plain instruct model of the same size. CORRECTION "
                     "2026-08-21: the original single-run 0.625 looked tied with "
                     "Devstral, but a --repeats 2 stability pass shows it is NOT "
                     "deterministic — collab-compute-rpkm flipped pass then fail/fail, "
                     "and validation-read-candidates flipped fail then pass/fail across "
                     "identical re-attempts. True stabilized rate across 24 runs is "
                     "0.583, not 0.625, and it runs ~2.2x slower than Devstral on "
                     "average (132.7s vs 60.3s) — reasoning-trace verbosity is the "
                     "likely source of both.",
    },
    {
        "id": "qwen3-coder-30b-a3b-awq", "label": "Qwen3-Coder 30B-A3B", "license": "Apache 2.0",
        "params": "30.5B total / 3.3B active MoE (128 experts, 8 routed)",
        "status": "tested 2026-08-21 — 0.5 pass rate, CONFIRMED STABLE, fast (avg 45s)",
        "good_for": "Coder-tuned MoE sibling of the resident default — fast inference "
                     "despite the total param count. Was flagged higher-risk (AWQ+MoE "
                     "instability reports elsewhere) but ran clean here: no crash-loop, "
                     "no expert-parallel flag even needed with the deployed config.",
    },
    {
        "id": "qwen2.5-coder-32b-instruct-awq", "label": "Qwen2.5-Coder 32B", "license": "Apache 2.0",
        "params": "32B dense",
        "status": "tested 2026-08-21 — 0.5 pass rate, 1 disagreement (self-reported done, code was actually broken)",
        "good_for": "Official Qwen-org AWQ quant of the dedicated code model in the "
                     "same size class as the already-resident general Qwen2.5-32B — "
                     "direct code-specialized-vs-general ablation.",
    },
    {
        "id": "kimi-linear-48b-a3b-awq", "label": "Kimi-Linear 48B-A3B", "license": "MIT",
        "params": "48B total / 3B active MoE, novel linear attention (KDA), 1M ctx",
        "status": "incompatible — hardware ceiling, not a config issue",
        "good_for": "Genuinely bleeding-edge: Moonshot AI's Kimi Delta Attention "
                     "(linear attention, not another transformer variant), same lab as "
                     "the currently-hyped Kimi K3. RULED OUT 2026-08-21: its hybrid "
                     "linear-attention kernel (KDA/short-conv) needs >64KB shared memory "
                     "per block; this rig's GPUs (2x Quadro RTX 6000, compute capability "
                     "7.5/Turing) cap at 65536 bytes/SM. vLLM crash-looped 31x before "
                     "being stopped — see eval_results.json's infra-startup record for "
                     "the trace. Not fixable via flags; would need a Turing-sized kernel "
                     "path upstream or newer GPUs.",
    },
    {
        "id": "codestral-22b-awq", "label": "Codestral 22B", "license": "Mistral AI Non-Production (MNPL) — research/internal use only",
        "params": "22B dense, code-specialized, 32K ctx",
        "status": "tested 2026-08-21 — 0.25 pass rate (worst), 3/8 hard timeouts",
        "good_for": "Mistral's dedicated code model, same family as the resident "
                     "Devstral (agentic-coding-tuned sibling) — this one is the "
                     "general code-completion/generation base it was built from. "
                     "Smallest dense candidate in the roster; tests whether a "
                     "purpose-built code model beats larger general models at this "
                     "size class. Non-commercial license -- internal eval only, not "
                     "a production pick regardless of eval score. Note: the AWQ repo "
                     "(TechxGenus) shipped with no chat_template — vLLM 0.23+ rejects "
                     "every request without one. Patched from the upstream Mistral "
                     "repo's tokenizer_config.json before this result was trusted; "
                     "the first eval attempt silently produced garbage (uniform ~9s "
                     "\"fails\" that were really instant 400 errors, not real attempts) "
                     "before that was caught.",
    },
    {
        "id": "gemma-4-31b-it-awq", "label": "Gemma 4 31B", "license": "Apache 2.0",
        "params": "30.7B dense, multimodal (vision+video), 256K ctx",
        "status": "incompatible — hardware ceiling, not a config issue",
        "good_for": "Google DeepMind's latest open dense model, ranked #3 on the Arena "
                     "AI text leaderboard among open models as of release. RULED OUT "
                     "2026-08-21: same Turing shared-memory ceiling as Kimi-Linear "
                     "(65536 byte/SM hardware limit, this kernel needs 98304) but from a "
                     "DIFFERENT cause -- standard attention, no exotic kernel like Kimi's "
                     "KDA. Correction to the 'standard architecture = safe' assumption "
                     "this entry was added under: the over-budget kernel here is "
                     "architecture-config-dependent (likely AWQ Marlin dequant or the "
                     "Triton attention kernel picking a large tile for the 5120 hidden "
                     "dim), so a normal-looking dense model can still hit this wall. "
                     "--enforce-eager didn't help (same kernel hit on first real request, "
                     "not just CUDA graph capture). See eval_results.json's "
                     "infra-startup record for the trace.",
    },
    {
        "id": "qwen3.8-27b-abliterated-awq", "label": "Qwen3.8 27B (abliterated AWQ)", "license": "Apache 2.0",
        "params": "27.8B dense, hybrid Gated DeltaNet + Gated Attention, AWQ W4A16, MTP preserved",
        "status": "tested 2026-08-22 -- comparable to the INT8 quant, faster, no boot flakiness",
        "good_for": "Same base model as qwen3.8-27b-int8-w8a16, different community quant "
                     "(twolven/Qwen3.8-27B-abliterated-AWQ-MTP) -- tested to see whether a proper "
                     "AWQ quant (this rig's proven format) sidesteps the INT8 quant's boot-flakiness "
                     "and runaway-reasoning issues. RESULT: editor 0.625 (same), avg 128s (faster than "
                     "248s), reviewer catch 2/3 (same), false-positive 1/3 (better than 2/3), "
                     "orchestrator coverage 7/9 (slightly below the INT8 quant's 9/9). Booted clean, "
                     "zero crash-loop restarts. Still hit the identical runaway-reasoning/empty-content "
                     "bug on complex prompts even with its own bundled 'medium' chat template -- "
                     "confirms the bug is a base-model trait, not specific to the INT8 quant or its "
                     "template. IMPORTANT: this checkpoint is abliterated (safety/refusal training "
                     "surgically removed) -- deployed only after Sam's explicit request and "
                     "re-authorization past an auto-mode classifier block; a real policy "
                     "consideration for a biodefense-adjacent pipeline, not just a performance "
                     "number, and worth Sam's own ongoing judgment before it becomes a default.",
    },
    {
        "id": "qwen3.8-27b-int8-w8a16", "label": "Qwen3.8 27B (INT8)", "license": "Apache 2.0",
        "params": "27.78B dense, hybrid Gated DeltaNet + Gated Attention, 262K ctx (1M w/ YaRN)",
        "status": "tested 2026-08-21 — 0.375 pass rate, slow (avg 209s), boot-flaky",
        "good_for": "Newer generation of the resident default's own architecture "
                     "family (qwen3.6-27b-awq is 'hybrid linear/full attention' too, "
                     "and has run clean all session) — lower kernel risk than a "
                     "first-of-its-kind exotic attention scheme. INT8, not the "
                     "official FP8 release: this rig's GPUs (Quadro RTX 6000, Turing) "
                     "have no FP8 tensor cores at all (documented hard constraint), "
                     "so the FP8 quant would run poorly or not at all. INT8 has real "
                     "Turing tensor-core support. Multimodal checkpoint, served "
                     "text-only. RESULT 2026-08-21: weakest of the real (non-ruled-out) "
                     "candidates — 4/8 tasks hit the full 300s aider timeout, and the "
                     "service itself needed --enforce-eager plus 6 systemd crash-loop "
                     "restarts (RuntimeError: cancelled on the KV-cache-spec RPC "
                     "handshake) before it would stay up.",
    },
]


@app.get("/api/coder-models")
async def coder_models() -> JSONResponse:
    """Static roster + live-ish eval metrics for the coder-engine pipeline's
    candidate models, for the "CODER MODELS" HUD panel. Metrics come from a
    file CT110 refreshes (see CODER_MODELS_METRICS_PATH docstring above); a
    model with no eval runs yet just shows "no runs yet", not an error."""
    metrics_blob: dict = {}
    try:
        with open(CODER_MODELS_METRICS_PATH) as f:
            metrics_blob = json.load(f)
    except FileNotFoundError:
        pass
    except Exception as exc:
        metrics_blob = {"_error": str(exc)}

    per_model_metrics = metrics_blob.get("metrics", {})
    models = [
        {**entry, "metrics": per_model_metrics.get(entry["id"])}
        for entry in CODER_MODELS_ROSTER
    ]
    return JSONResponse({
        "models": models,
        "metrics_generated_at": metrics_blob.get("generated_at"),
        "metrics_source": metrics_blob.get("source"),
    })


# Reviewer-role candidates share the same model catalog as the editor role
# (CODER_MODELS_ROSTER) -- reuse its id->label/license lookup rather than
# keeping a second roster in sync.
_CODER_ROSTER_BY_ID = {entry["id"]: entry for entry in CODER_MODELS_ROSTER}


@app.get("/api/coder-transit-map")
async def coder_transit_map() -> JSONResponse:
    """Combined editor + reviewer + orchestrator data for the coder-engine
    TRANSIT MAP panel -- one subway-style diagram of every role the pipeline
    needs a model for for, with each line's real metrics attached. Editor
    reuses the same roster/metrics as /api/coder-models; reviewer reads
    CODER_REVIEW_METRICS_PATH (refreshed by the same CT110 script); the
    orchestrator line is static text since it has no eval data yet."""
    editor_blob: dict = {}
    try:
        with open(CODER_MODELS_METRICS_PATH) as f:
            editor_blob = json.load(f)
    except FileNotFoundError:
        pass
    except Exception as exc:
        editor_blob = {"_error": str(exc)}

    review_blob: dict = {}
    try:
        with open(CODER_REVIEW_METRICS_PATH) as f:
            review_blob = json.load(f)
    except FileNotFoundError:
        pass
    except Exception as exc:
        review_blob = {"_error": str(exc)}

    editor_metrics = editor_blob.get("metrics", {})
    editor_models = sorted(
        (
            {
                "id": mid,
                "label": _CODER_ROSTER_BY_ID.get(mid, {}).get("label", mid),
                **m,
            }
            for mid, m in editor_metrics.items()
            if m.get("pass_rate") is not None
        ),
        key=lambda m: m["pass_rate"],
        reverse=True,
    )

    review_metrics = review_blob.get("metrics", {})
    review_models = sorted(
        (
            {
                "id": mid,
                "label": _CODER_ROSTER_BY_ID.get(mid, {}).get("label", mid),
                **m,
            }
            for mid, m in review_metrics.items()
        ),
        key=lambda m: (m.get("catch_rate") is None, -(m.get("catch_rate") or 0)),
    )

    orch_blob: dict = {}
    try:
        with open(CODER_ORCH_METRICS_PATH) as f:
            orch_blob = json.load(f)
    except FileNotFoundError:
        pass
    except Exception as exc:
        orch_blob = {"_error": str(exc)}

    orch_metrics = orch_blob.get("metrics", {})
    orch_models = sorted(
        (
            {
                "id": mid,
                "label": _CODER_ROSTER_BY_ID.get(mid, {}).get("label", mid),
                **m,
            }
            for mid, m in orch_metrics.items()
        ),
        key=lambda m: (m.get("coverage_rate") is None, -(m.get("coverage_rate") or 0)),
    )

    return JSONResponse({
        "editor": {
            "generated_at": editor_blob.get("generated_at"),
            "total_runs": editor_blob.get("total_records"),
            "models": editor_models,
        },
        "reviewer": {
            "generated_at": review_blob.get("generated_at"),
            "total_raw_records": review_blob.get("total_raw_records"),
            "total_graded_records": review_blob.get("total_graded_records"),
            "models": review_models,
        },
        "orchestrator": {
            **CODER_TRANSIT_ORCHESTRATOR,
            "generated_at": orch_blob.get("generated_at"),
            "total_raw_records": orch_blob.get("total_raw_records"),
            "total_graded_records": orch_blob.get("total_graded_records"),
            "models": orch_models,
        },
    })


MODEL_ROLE_ASSIGNMENTS_PATH = "/ssdpool/coder-engine/pipeline/model_role_assignments.json"
MODEL_ROLE_NAMES = ("editor", "reviewer", "orchestrator")
# "editor" is read live by dispatch_task.py on every real dispatch (see
# _role_default_model() there). "orchestrator" is wired to Hermes's own
# native decomposer/specifier (auxiliary.kanban_decomposer/triage_specifier
# in ~/.hermes/config.yaml, CT111) -- the actual live mechanism behind
# `hermes kanban decompose`/`specify`, confirmed live 2026-08-22 -- not a
# second, disconnected config. "reviewer" drives POST /api/review-file
# (dispatch_review_task.py on snarf), which runs a real review and files a
# --triage kanban card -- never a higher status, so it proposes but never
# acts unsupervised. See Phase 3.5 / snazzy-chasing-willow.md.
MODEL_ROLE_LIVE = {"editor", "orchestrator", "reviewer"}

HERMES_CONFIG_PATH = "/root/.hermes/config.yaml"
HERMES_VENV_PY = "/usr/local/lib/hermes-agent/venv/bin/python3"
_HERMES_AUX_SECTIONS = ("triage_specifier", "kanban_decomposer")


def _hermes_aux_section_model(config_text: str, section: str) -> str | None:
    """Pull `model: '...'` out of one auxiliary.<section> block. Bounded to
    that block only (up to the next 2-space-indented key) so this can't
    accidentally match a same-named key in a different section."""
    marker = f"  {section}:\n"
    if marker not in config_text:
        return None
    tail = config_text.split(marker, 1)[1]
    m = re.search(r"^  \S", tail, re.MULTILINE)
    block = tail[: m.start()] if m else tail
    mm = re.search(r"^    model:\s*'([^']*)'", block, re.MULTILINE)
    return mm.group(1) if mm else None


def _hermes_global_default_model(config_text: str) -> str | None:
    m = re.search(r"^model:\n(?:  .+\n)*?  default:\s*(\S+)", config_text, re.MULTILINE)
    return m.group(1) if m else None


async def _get_orchestrator_model() -> str | None:
    """Effective model `hermes kanban decompose`/`specify` will actually use
    right now: kanban_decomposer's own model if explicitly set, else the
    global default it inherits from (provider: auto behavior) -- never a
    blank string, since that's not what's really going to run."""
    rc, out = await _fleet_ssh("hermes", f"cat {shlex.quote(HERMES_CONFIG_PATH)}")
    if rc != 0:
        raise RuntimeError(f"cat exited {rc}: {out[-500:]}")
    explicit = _hermes_aux_section_model(out, "kanban_decomposer")
    if explicit:
        return explicit
    return _hermes_global_default_model(out)


async def _set_orchestrator_model(model: str) -> None:
    """Pin both triage_specifier and kanban_decomposer to `model` explicitly
    (provider: vllm, base_url matching the main model block's known-working
    endpoint) rather than relying on 'auto' inheritance semantics that
    aren't independently verifiable (Hermes is vendor software, no source
    checkout -- see CLAUDE.md). Surgical, idempotent anchored-block text
    replace, not a full YAML parse-and-dump: this is a large, hand-
    maintained vendor config file with folded multi-line scalars (the
    personality strings) a round-trip dump risks reformatting even when
    semantically a no-op. Idempotent means this works from ANY prior state
    of these two blocks (the pristine provider:auto/model:'' default, or a
    value a previous call already set) -- it replaces whatever the
    provider/model/base_url lines currently say, not a one-shot "auto ->
    explicit" converter (an earlier version of this function was exactly
    that and broke on the second call -- caught live 2026-08-22). Backs up
    first, validates the result actually parses (Hermes's own bundled
    PyYAML) before calling it done."""
    rc, out = await _fleet_ssh("hermes", f"cat {shlex.quote(HERMES_CONFIG_PATH)}")
    if rc != 0:
        raise RuntimeError(f"cat exited {rc}: {out[-500:]}")

    new_text = out
    for section in _HERMES_AUX_SECTIONS:
        marker = f"  {section}:\n"
        if marker not in new_text:
            raise RuntimeError(f"{section!r} block not found in config.yaml -- not touching it blind")
        before, tail = new_text.split(marker, 1)
        m = re.search(r"^  \S", tail, re.MULTILINE)
        block = tail[: m.start()] if m else tail
        rest = tail[m.start():] if m else ""

        for key in ("provider:", "model:", "base_url:"):
            if not re.search(rf"^    {re.escape(key)}", block, re.MULTILINE):
                raise RuntimeError(
                    f"{section!r} block is missing a {key!r} line -- "
                    f"config.yaml's shape has changed; not touching it blind"
                )

        new_block = re.sub(r"^    provider:.*$", "    provider: vllm", block, count=1, flags=re.MULTILINE)
        new_block = re.sub(r"^    model:.*$", f"    model: '{model}'", new_block, count=1, flags=re.MULTILINE)
        new_block = re.sub(
            r"^    base_url:.*$",
            "    base_url: 'http://192.168.1.239:8000/v1'",
            new_block, count=1, flags=re.MULTILINE,
        )
        new_text = before + marker + new_block + rest

    backup_cmd = (
        f"cp {shlex.quote(HERMES_CONFIG_PATH)} "
        f"{shlex.quote(HERMES_CONFIG_PATH)}.bak-model-role-assignments"
    )
    rc_b, out_b = await _fleet_ssh("hermes", backup_cmd)
    if rc_b != 0:
        raise RuntimeError(f"backup failed, aborting write: {out_b[-500:]}")

    write_cmd = f"printf %s {shlex.quote(new_text)} > {shlex.quote(HERMES_CONFIG_PATH)}"
    rc_w, out_w = await _fleet_ssh("hermes", write_cmd)
    if rc_w != 0:
        raise RuntimeError(f"write failed: {out_w[-500:]}")

    validate_cmd = (
        f"{HERMES_VENV_PY} -c "
        f"\"import yaml; yaml.safe_load(open({HERMES_CONFIG_PATH!r})); print('ok')\""
    )
    rc_v, out_v = await _fleet_ssh("hermes", validate_cmd)
    if rc_v != 0 or "ok" not in out_v:
        raise RuntimeError(
            f"post-write validation failed (file may be malformed -- backup "
            f"is at {HERMES_CONFIG_PATH}.bak-model-role-assignments): {out_v[-500:]}"
        )


@app.get("/api/model-role-assignments")
async def model_role_assignments() -> JSONResponse:
    """Current per-role model assignment. "editor"/"reviewer" come from
    snarf's model_role_assignments.json -- the exact file dispatch_task.py
    reads on every real dispatch for "editor", so a change from the TRANSIT
    MAP panel takes effect on the next dispatch, no redeploy. "orchestrator"
    is overlaid live from Hermes's own config (see _get_orchestrator_model)
    since that's the role's real live mechanism, not this file. Also
    returns the known model roster (id+label) so the HUD can populate each
    dropdown from one source of truth (CODER_MODELS_ROSTER)."""
    try:
        rc, out = await _fleet_ssh("snarf", f"cat {shlex.quote(MODEL_ROLE_ASSIGNMENTS_PATH)}")
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    if rc != 0:
        return JSONResponse({"error": f"cat exited {rc}: {out[-500:]}"}, status_code=502)
    try:
        assignments = json.loads(out)
    except json.JSONDecodeError:
        return JSONResponse({"error": f"unparseable assignments file: {out[-500:]}"}, status_code=502)

    try:
        orch_model = await _get_orchestrator_model()
        if orch_model:
            assignments["orchestrator"] = orch_model
    except Exception as exc:
        assignments["_orchestrator_error"] = str(exc)

    return JSONResponse({
        "assignments": assignments,
        "live_roles": sorted(MODEL_ROLE_LIVE),
        "roster": [{"id": e["id"], "label": e["label"]} for e in CODER_MODELS_ROSTER],
    })


@app.post("/api/model-role-assignments")
async def set_model_role_assignment(request: Request) -> JSONResponse:
    """Change one role's assigned model. Body: {"role": "editor"|"reviewer"|
    "orchestrator", "model": "<id from CODER_MODELS_ROSTER>"}. "editor"/
    "reviewer" update snarf's model_role_assignments.json directly.
    "orchestrator" instead updates Hermes's own live config on CT111 (see
    _set_orchestrator_model) -- the real mechanism behind `hermes kanban
    decompose`/`specify`, not a second file nothing reads. The response's
    "live_roles" tells the caller which change actually takes effect
    immediately vs. is just recorded for later (reviewer)."""
    payload = await request.json()
    role = (payload.get("role") or "").strip()
    model = (payload.get("model") or "").strip()
    if role not in MODEL_ROLE_NAMES:
        return JSONResponse(
            {"ok": False, "error": f"role must be one of {MODEL_ROLE_NAMES}"}, status_code=400
        )
    if model not in _CODER_ROSTER_BY_ID:
        return JSONResponse({"ok": False, "error": f"unknown model id {model!r}"}, status_code=400)

    if role == "orchestrator":
        try:
            await _set_orchestrator_model(model)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
        try:
            rc, out = await _fleet_ssh("snarf", f"cat {shlex.quote(MODEL_ROLE_ASSIGNMENTS_PATH)}")
            assignments = json.loads(out) if rc == 0 else {}
        except Exception:
            assignments = {}
        assignments["orchestrator"] = model
        return JSONResponse({"ok": True, "assignments": assignments, "live_roles": sorted(MODEL_ROLE_LIVE)})

    try:
        rc, out = await _fleet_ssh("snarf", f"cat {shlex.quote(MODEL_ROLE_ASSIGNMENTS_PATH)}")
        if rc != 0:
            raise RuntimeError(f"cat exited {rc}: {out[-500:]}")
        assignments = json.loads(out)
        assignments[role] = model
        new_content = json.dumps(assignments, indent=2) + "\n"
        write_cmd = f"printf %s {shlex.quote(new_content)} > {shlex.quote(MODEL_ROLE_ASSIGNMENTS_PATH)}"
        rc2, out2 = await _fleet_ssh("snarf", write_cmd)
        if rc2 != 0:
            raise RuntimeError(f"write exited {rc2}: {out2[-500:]}")
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return JSONResponse({"ok": True, "assignments": assignments, "live_roles": sorted(MODEL_ROLE_LIVE)})


CODER_ENGINE_VENV_PY = "/ssdpool/coder-engine/.venv/bin/python3"
DISPATCH_REVIEW_TASK_PY = "/ssdpool/coder-engine/pipeline/dispatch_review_task.py"


@app.post("/api/review-file")
async def review_file(request: Request) -> JSONResponse:
    """On-demand, human-triggered live review of one real DARKHELIX file --
    the reviewer role's actual live path (per Sam's explicit direction:
    "these steps are required even if they don't push a final choice" --
    reviewer needs a real, invocable pipeline even though it never acts
    unsupervised). Runs dispatch_review_task.py on snarf against whatever
    model model_role_assignments.json currently has for "reviewer" (so
    changing that dropdown changes what a review actually uses, same as
    editor), then files the result as a --triage kanban card -- never a
    higher status, same safety gate SUBMIT WORK already established above:
    a human decides what happens next, this only proposes. One call = one
    review, no schedule, no automatic trigger -- same "one dispatch = one
    attempt" discipline as dispatch_task.py.

    Body: {"target_file": "path/inside/DARKHELIX", "task_description":
    optional override of the default review prompt}."""
    payload = await request.json()
    target_file = (payload.get("target_file") or "").strip()
    if not target_file:
        return JSONResponse({"ok": False, "error": "target_file is required"}, status_code=400)
    task_description = (payload.get("task_description") or "").strip()

    try:
        rc0, out0 = await _fleet_ssh("snarf", f"cat {shlex.quote(MODEL_ROLE_ASSIGNMENTS_PATH)}")
        model = json.loads(out0).get("reviewer") if rc0 == 0 else None
    except Exception:
        model = None
    if not model:
        return JSONResponse(
            {"ok": False, "error": "could not resolve reviewer's assigned model"}, status_code=502
        )

    cmd = (
        f"{CODER_ENGINE_VENV_PY} {DISPATCH_REVIEW_TASK_PY} "
        f"--repo-path {shlex.quote(DARKHELIX_REPO_PATH)} "
        f"--target-file {shlex.quote(target_file)} "
        f"--model {shlex.quote(model)}"
    )
    if task_description:
        cmd += f" --task-description {shlex.quote(task_description)}"

    try:
        rc, out = await _fleet_ssh("snarf", cmd)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    brace = out.find("{")
    try:
        if brace < 0:
            raise ValueError("no JSON object in output")
        result = json.loads(out[brace:])
    except (ValueError, json.JSONDecodeError):
        return JSONResponse({"ok": False, "error": f"unparseable review output: {out[-1500:]}"}, status_code=502)

    if result.get("status") != "done":
        return JSONResponse(
            {"ok": False, "error": result.get("error") or "review failed", "model": model},
            status_code=502,
        )

    review_text = result.get("review_text") or ""
    title = f"[Review] {target_file}"
    body = (
        f"Automated review by {model} (reviewer role) -- a proposal, not a "
        f"verified finding. Read it and decide; nothing has been changed.\n\n"
        f"{review_text}"
    )
    kanban_cmd = (
        "hermes kanban create "
        f"{shlex.quote(title[:200])} "
        f"--body {shlex.quote(body)} "
        "--workspace scratch --triage --created-by looking-glass --json"
    )
    try:
        rc2, out2 = await _kanban_ssh(kanban_cmd)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc), "review_text": review_text}, status_code=502)
    if rc2 != 0:
        return JSONResponse({"ok": False, "error": out2[-2000:], "review_text": review_text}, status_code=502)
    try:
        task_data = json.loads(out2.strip())
    except json.JSONDecodeError:
        return JSONResponse(
            {"ok": False, "error": f"unparseable kanban output: {out2[-500:]}", "review_text": review_text},
            status_code=502,
        )

    return JSONResponse({"ok": True, "task": task_data, "model": model, "review_text": review_text})


def _parse_checkbox_md(text: str) -> dict:
    done = len(re.findall(r"^\s*-\s*\[[xX]\]", text, re.MULTILINE))
    open_ = len(re.findall(r"^\s*-\s*\[ \]", text, re.MULTILINE))
    return {"done": done, "open": open_, "total": done + open_}


def _parse_status_table_md(text: str) -> dict:
    counts = {"done": 0, "in_progress": 0, "blocked": 0, "todo": 0}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or all(set(c) <= set("-: ") for c in cells):
            continue  # separator row (e.g. |---|---|)
        for cell in cells:
            if cell in _STATUS_EMOJI:
                counts[_STATUS_EMOJI[cell]] += 1
                break
    total = sum(counts.values())
    return {**counts, "total": total, "done": counts["done"], "open": total - counts["done"]}


def _fetch_github_file(repo: str, ref: str, path: str) -> str | None:
    token = os.environ.get("GITHUB_TODO_TOKEN")
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw+json"}
    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={ref}"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return None
        return resp.text
    except requests.RequestException:
        return None


@app.get("/api/projects")
async def api_projects() -> JSONResponse:
    """Per-project TODO/status counts, fetched from each tracked repo's
    TODO.md (checkbox syntax) or, for the server repo, STATUS.md's own
    emoji-status table format."""
    now = time.time()
    if now - PROJECTS_CACHE["ts"] < 60:
        return JSONResponse(PROJECTS_CACHE["data"])

    def fetch_all() -> dict:
        results = []
        for proj in TRACKED_PROJECTS:
            # Location travels with the counts: "51 open" is only actionable once
            # you know which box to open a terminal on to work them.
            where = {"node": proj.get("node"), "checkout": proj.get("checkout"),
                     "repo": proj["repo"]}
            if not proj.get("path"):
                # Tracked for location only — no tracker file to count.
                results.append({"name": proj["name"], "untracked": True, **where})
                continue
            content = _fetch_github_file(proj["repo"], proj["ref"], proj["path"])
            if content is None:
                results.append({"name": proj["name"], "error": "unreachable", **where})
                continue
            parser = _parse_checkbox_md if proj["parser"] == "checkbox" else _parse_status_table_md
            results.append({"name": proj["name"], **parser(content), **where})
        return {"projects": results}

    data = await asyncio.to_thread(fetch_all)
    PROJECTS_CACHE.update(ts=now, data=data)
    return JSONResponse(data)


ACTIVITY_LOG: list[dict] = []
ACTIVITY_LOG_MAX = 200


async def _push_activity_event(event: dict) -> None:
    event = {**event, "ts": time.time()}
    ACTIVITY_LOG.append(event)
    del ACTIVITY_LOG[:-ACTIVITY_LOG_MAX]
    for client in list(WS_CLIENTS):
        try:
            await client.send_json({"type": "activity_event", "event": event})
        except Exception:
            WS_CLIENTS.discard(client)


# ---------------------------------------------------------------- kanban view
# The HUD's own chat session is NOT where agentic work happens — each kanban
# card runs in its own Hermes session and workspace. Managing that work means
# reading the board and the per-task run logs, which live on the hermes box.

def _kanban_cfg() -> dict:
    return CFG.get("kanban") or {"host": "hermes"}


# --- pooled SSH -----------------------------------------------------------
# Every API request used to open a fresh asyncssh connection, run one command
# and tear it down. With the HUD polling continuously that produced ~110k sshd
# lines/day on hermes (582MB journal, plus matching systemd-logind churn).
# Connections are now cached per host and reused; a dropped/stale connection is
# discarded and retried once, so behaviour on failure is unchanged.
_SSH_CONNS: dict = {}
_SSH_LOCKS: dict = {}


async def _ssh_connection(host_key: str):
    target = TERMINAL_HOSTS.get(host_key)
    if not target:
        raise RuntimeError(f"{host_key!r} is not a known terminal host")
    lock = _SSH_LOCKS.get(host_key)
    if lock is None:
        lock = _SSH_LOCKS[host_key] = asyncio.Lock()
    async with lock:
        conn = _SSH_CONNS.get(host_key)
        if conn is not None:
            return conn
        conn = await asyncssh.connect(
            target["host"], username=target["user"],
            client_keys=[TERMINAL_KEY_PATH],
            keepalive_interval=30, keepalive_count_max=3,
        )
        _SSH_CONNS[host_key] = conn
        return conn


async def _ssh_run(host_key: str, cmd: str) -> tuple[int, str]:
    last_exc = None
    for _attempt in (1, 2):
        try:
            conn = await _ssh_connection(host_key)
            result = await conn.run(cmd, check=False)
            return result.exit_status, (result.stdout or "") + (result.stderr or "")
        except Exception as exc:
            last_exc = exc
            stale = _SSH_CONNS.pop(host_key, None)
            if stale is not None:
                try:
                    stale.abort()
                except Exception:
                    pass
    raise last_exc


async def _kanban_ssh(cmd: str) -> tuple[int, str]:
    return await _ssh_run(_kanban_cfg().get("host", "hermes"), cmd)


@app.get("/api/kanban")
async def kanban_board() -> JSONResponse:
    try:
        _, out = await _kanban_ssh("hermes kanban ls --json --sort created-desc")
    except Exception as exc:
        return JSONResponse({"tasks": [], "error": str(exc)}, status_code=502)
    try:
        data = json.loads(out.strip() or "[]")
    except json.JSONDecodeError:
        return JSONResponse({"tasks": [], "error": "unparseable board output"}, status_code=502)
    rows = data if isinstance(data, list) else data.get("tasks", [])
    keep = ("id", "title", "status", "assignee", "created_by", "created_at",
            "started_at", "completed_at", "result", "session_id")
    return JSONResponse({"tasks": [{k: t.get(k) for k in keep} for t in rows]})


_TASK_ID_RE = re.compile(r"^t_[0-9a-f]{6,}$")


@app.post("/api/kanban/unblock")
async def kanban_unblock(request: Request) -> JSONResponse:
    """Returns a blocked card to ready (or todo, if its parents are still
    open) via `hermes kanban unblock <id>` -- same native-subcommand,
    previously-unexposed pattern as /api/kanban/archive below. No --reason
    prompt: single click, server-side is the only gate, matching the rest
    of this HUD's kanban actions."""
    payload = await request.json()
    task_id = (payload.get("task_id") or "").strip()
    if not _TASK_ID_RE.match(task_id):
        return JSONResponse({"ok": False, "error": "bad task id"}, status_code=400)
    try:
        rc, out = await _kanban_ssh(f"hermes kanban unblock {shlex.quote(task_id)}")
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    if rc != 0:
        return JSONResponse({"ok": False, "error": out[-1000:]}, status_code=502)
    return JSONResponse({"ok": True})


@app.post("/api/kanban/archive")
async def kanban_archive(request: Request) -> JSONResponse:
    """The review-then-deemphasize mechanism for a done card: archives it
    via `hermes kanban archive <id>` (native Hermes subcommand, previously
    unexposed by the HUD -- board reads only listed it, nothing could set
    it). Doesn't delete the task or its log; just moves it out of the
    active board so /api/kanban stops surfacing it as live work."""
    payload = await request.json()
    task_id = (payload.get("task_id") or "").strip()
    if not _TASK_ID_RE.match(task_id):
        return JSONResponse({"ok": False, "error": "bad task id"}, status_code=400)
    try:
        rc, out = await _kanban_ssh(f"hermes kanban archive {shlex.quote(task_id)}")
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    if rc != 0:
        return JSONResponse({"ok": False, "error": out[-1000:]}, status_code=502)
    return JSONResponse({"ok": True})


# ------------------------------------------------ SUBMIT WORK (DARKHELIX) --
# Lets Sam turn a real DARKHELIX TODO.md item into a real kanban card in one
# click, with optional freeform instructions appended. Added 2026-08-22.
#
# Reuses the exact same asyncssh-over-TERMINAL_HOSTS mechanism _kanban_ssh
# already proved out for /api/kanban -- CT112 already holds a fleet-wide
# terminal key, so this needed no new plumbing, just a second host (snarf,
# to read TODO.md) and a write instead of a read (hermes kanban create).
#
# Deliberate safety choice, reviewed with Sam before building: every card
# created here files to --triage, never straight to ready/running. Hermes's
# own specifier still has to flesh out and decompose it before anything
# executes -- single-click submission, not single-click unattended dispatch.
#
# No `hermes project` is registered for DARKHELIX yet (confirmed live,
# `hermes project list` -> "No projects yet"), so this anchors the task
# directly via --workspace worktree:<path> rather than --project.

DARKHELIX_TODO_PATH = "/ssdpool/DARKHELIX/TODO.md"
DARKHELIX_REPO_PATH = "/ssdpool/DARKHELIX"

_TODO_ITEM_RE = re.compile(r"^- \[([ xX])\]\s*(.*)$")
_TODO_HEADING_RE = re.compile(r"^#{1,3}\s+(.*)$")
_TODO_STATUS_TAG_RE = re.compile(r"^\*\*(WIP|WAITING)\*\*\s*")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


async def _fleet_ssh(host_key: str, cmd: str) -> tuple[int, str]:
    """Same mechanism as _kanban_ssh, parameterized by host key -- lets
    /api/darkhelix-todo reach snarf with the identical proven connection
    pattern _kanban_ssh already uses to reach hermes."""
    return await _ssh_run(host_key, cmd)


def _parse_darkhelix_todo(text: str) -> list[dict]:
    """Parse DARKHELIX's TODO.md checkbox items into submittable work items.
    Matches the file's own documented convention (see its own header):
    only "- [ ]"/"- [x]" lines are real items; a **WAITING**/**WIP** tag
    right after the checkbox is a status marker, not item text. Items
    commonly run to a few hundred words -- joined until the next item,
    heading, or blank line."""
    items: list[dict] = []
    section = None
    current: dict | None = None

    def flush():
        nonlocal current
        if current is not None:
            items.append(current)
            current = None

    for line in text.splitlines():
        h = _TODO_HEADING_RE.match(line)
        if h:
            flush()
            section = h.group(1).strip()
            continue
        m = _TODO_ITEM_RE.match(line)
        if m:
            flush()
            rest = m.group(2)
            tag_m = _TODO_STATUS_TAG_RE.match(rest)
            tag = tag_m.group(1) if tag_m else None
            if tag_m:
                rest = rest[tag_m.end():]
            current = {
                "section": section,
                "done": m.group(1).strip().lower() == "x",
                "status_tag": tag,
                "text": rest.strip(),
            }
            continue
        if current is not None:
            if not line.strip():
                flush()
            else:
                current["text"] += "\n" + line

    flush()

    result = []
    for i, it in enumerate(items):
        if it["done"]:
            continue
        cleaned = it["text"].strip()
        if not cleaned:
            continue
        result.append({
            "id": f"todo-{i}",
            "section": it["section"],
            "blocked": it["status_tag"] == "WAITING",
            "wip": it["status_tag"] == "WIP",
            "text": cleaned,
            "title": cleaned.split("\n")[0][:140],
        })
    return result


@app.get("/api/darkhelix-todo")
async def darkhelix_todo() -> JSONResponse:
    """Real, current TODO.md items from DARKHELIX (snarf), for the SUBMIT
    WORK panel's picker. Read live every request, no caching -- Sam edits
    this file directly and it's the single tracker ("if it isn't here, it
    isn't tracked")."""
    try:
        rc, out = await _fleet_ssh("snarf", f"cat {shlex.quote(DARKHELIX_TODO_PATH)}")
    except Exception as exc:
        return JSONResponse({"items": [], "error": str(exc)}, status_code=502)
    if rc != 0:
        return JSONResponse({"items": [], "error": f"cat exited {rc}"}, status_code=502)
    return JSONResponse({"items": _parse_darkhelix_todo(out)})


def _slugify(text: str, max_len: int = 40) -> str:
    s = _SLUG_RE.sub("-", text.lower()).strip("-")
    return (s[:max_len] or "task").strip("-")


@app.post("/api/kanban/create")
async def kanban_create(request: Request) -> JSONResponse:
    """Single-click work submission: files a REAL kanban card, filed
    --workspace scratch. Always files to --triage -- see the module note
    above for why this is a deliberate, reviewed choice, not a default
    left unconsidered.

    NOTE on workspace kind: this used to pass
    --workspace worktree:{DARKHELIX_REPO_PATH}, which tells Hermes to
    validate/manage a real git worktree at that path itself, from CT111.
    That's wrong -- DARKHELIX only exists on snarf, a different physical
    host CT111 can't see into, so Hermes always reported "not a git repo"
    the moment a worker actually tried to use the card (see
    snazzy-chasing-willow.md's 2026-08-22 handoff for the live repro).
    The execution-engine-dispatch skill is what actually manages the
    worktree, by SSHing to snarf itself -- so the repo path + branch name
    are embedded as plain text in the body instead, for the skill/agent
    to read directly rather than something Hermes tries to validate.

    Body: {"title": "...", "body": "..."} -- title is required (the
    picked TODO item's own first line, or freeform); body carries the
    item's full text plus any optional instructions appended."""
    payload = await request.json()
    title = (payload.get("title") or "").strip()
    if not title:
        return JSONResponse({"ok": False, "error": "title is required"}, status_code=400)
    body = payload.get("body") or ""
    branch = f"wt/{_slugify(title)}-{int(time.time())}"
    dispatch_target = (
        "[dispatch-target]\n"
        f"repo: {DARKHELIX_REPO_PATH}\n"
        f"branch: {branch}\n"
        "[/dispatch-target]\n\n"
    )
    body = dispatch_target + body
    cmd = (
        "hermes kanban create "
        f"{shlex.quote(title[:200])} "
        f"--body {shlex.quote(body)} "
        "--workspace scratch "
        "--triage --created-by looking-glass --json"
    )
    try:
        rc, out = await _kanban_ssh(cmd)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    if rc != 0:
        return JSONResponse({"ok": False, "error": out[-2000:]}, status_code=502)
    try:
        data = json.loads(out.strip())
    except json.JSONDecodeError:
        return JSONResponse(
            {"ok": False, "error": f"unparseable output: {out[-500:]}"}, status_code=502
        )
    return JSONResponse({"ok": True, "task": data})


@app.get("/api/kanban/{task_id}/log")
async def kanban_log(task_id: str, request: Request) -> JSONResponse:
    """Tail of a task's run log — this is the live view of Hermes working."""
    if not _TASK_ID_RE.match(task_id):
        return JSONResponse({"error": "bad task id"}, status_code=400)
    lines = min(int(request.query_params.get("lines", 200)), 1000)
    try:
        _, out = await _kanban_ssh(
            f"tail -n {lines} ~/.hermes/kanban/logs/{shlex.quote(task_id)}.log 2>/dev/null || true")
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"id": task_id, "log": out[-60000:]})


@app.get("/api/conversation")
async def conversation_history(request: Request) -> JSONResponse:
    """Message history for the HUD's shared Hermes session.

    The HUD only ever showed messages from the current browser session, so a
    reload (or opening the live pane) looked like an empty conversation even
    though the session on Hermes had full history. Resolves the conversation
    name to its session id the same way the voice path does, then reads
    /api/sessions/{id}/messages.
    """
    hermes_cfg = CFG.get("hermes") or {}
    conversation = request.query_params.get("conversation") or hermes_cfg.get(
        "conversation", "looking-glass-main")
    limit = min(int(request.query_params.get("limit", 60)), 200)
    token = os.environ.get(hermes_cfg.get("api_key_env", "API_SERVER_KEY"))
    base_url = hermes_cfg.get("base_url", "http://127.0.0.1:8642")
    if not token:
        return JSONResponse({"messages": [], "error": "no API key"}, status_code=503)
    try:
        session_id = await asyncio.to_thread(HERMES.get_session_id, conversation)
        response = await asyncio.to_thread(
            requests.get, f"{base_url}/api/sessions/{session_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={"limit": limit}, timeout=20,
        )
        response.raise_for_status()
        rows = response.json().get("data", [])
    except Exception as exc:
        return JSONResponse({"messages": [], "error": str(exc)}, status_code=502)

    messages = []
    for row in rows:
        role = row.get("role")
        content = (row.get("content") or "").strip()
        # tool/system rows carry no readable text for the transcript
        if role not in ("user", "assistant") or not content:
            continue
        messages.append({"role": role, "content": content[:4000]})
    return JSONResponse({"session_id": session_id, "conversation": conversation,
                         "messages": messages})


@app.get("/api/activity")
async def activity() -> JSONResponse:
    return JSONResponse({"events": ACTIVITY_LOG[-50:]})


def _host_reachable(address: str, port: int) -> bool:
    import socket
    try:
        with socket.create_connection((address, port), timeout=2):
            return True
    except Exception:
        return False


# ------------------------------------------------------- network topology map

def _topology_cfg() -> dict:
    return CFG.get("network_topology") or {}


NETWORK_TOPOLOGY_STATE: dict = {
    "updated": 0.0, "nodes": [],
    "edges": {"physical": [], "general": [], "hermes": [], "claude": []},
}


async def _probe_network_topology() -> None:
    cfg = _topology_cfg()
    nodes_cfg = cfg.get("nodes") or []
    physical_groups = cfg.get("physical_groups") or []

    async def probe_node(node: dict) -> dict:
        address = node["address"]
        ports = node.get("ports") or []
        open_flags = await asyncio.gather(
            *[asyncio.to_thread(_host_reachable, address, p) for p in ports]
        )
        group = next(
            (g["id"] for g in physical_groups if node["id"] in (g.get("members") or [])),
            None,
        )
        return {
            "id": node["id"], "address": address, "kind": node.get("kind", "host"),
            "up": any(open_flags), "physical_group": group,
            "monitored": node.get("monitored", True),
            "ports": [{"port": p, "open": o} for p, o in zip(ports, open_flags)],
        }

    nodes = list(await asyncio.gather(*[probe_node(n) for n in nodes_cfg]))
    node_ids = {n["id"] for n in nodes}

    physical_edges = [
        {"from": g["id"], "to": member}
        for g in physical_groups for member in (g.get("members") or [])
        if member in node_ids and member != g["id"]
    ]
    general_edges = [{"from": "lan", "to": n["id"]} for n in nodes]
    hermes_edges = [
        {"from": "hermes", **e} for e in (cfg.get("hermes_edges") or []) if e.get("to") in node_ids
    ]
    claude_edges = [
        {"from": "claude", **e} for e in (cfg.get("claude_edges") or []) if e.get("to") in node_ids
    ]

    NETWORK_TOPOLOGY_STATE.update({
        "updated": time.time(),
        "nodes": nodes,
        "edges": {
            "physical": physical_edges, "general": general_edges,
            "hermes": hermes_edges, "claude": claude_edges,
        },
    })


async def _poll_network_topology_forever() -> None:
    while True:
        try:
            await _probe_network_topology()
        except Exception:
            pass
        await asyncio.sleep(_topology_cfg().get("poll_seconds", 20))


@app.get("/api/network_topology")
async def network_topology() -> JSONResponse:
    return JSONResponse(NETWORK_TOPOLOGY_STATE)


def _match_topology_node(*texts: str) -> str | None:
    """Best-effort match of a Hermes tool name/preview to a configured node,
    for the network map's live-activity pulse. Simple substring heuristic —
    good enough to show *something* moving; a tighter signal (Hermes emitting
    structured host metadata per tool call) is the natural upgrade later."""
    haystack = " ".join(t for t in texts if t).lower()
    if not haystack:
        return None
    for node in (_topology_cfg().get("nodes") or []):
        if node["id"].lower() in haystack or node.get("address", "") in haystack:
            return node["id"]
    return None


_ACTIVITY_STARTED = False


@app.on_event("startup")
async def start_activity_feed() -> None:
    """Race-locked like warm_pipeline above — this hook also fires once per
    uvicorn listener (there are four)."""
    global _ACTIVITY_STARTED
    if _ACTIVITY_STARTED:
        return
    _ACTIVITY_STARTED = True
    asyncio.get_running_loop().create_task(_poll_rack_hosts_forever())
    asyncio.get_running_loop().create_task(_poll_hermes_sessions_forever())
    asyncio.get_running_loop().create_task(_poll_network_topology_forever())


async def _poll_rack_hosts_forever() -> None:
    af_cfg = CFG.get("activity_feed") or {}
    hosts_cfg = af_cfg.get("hosts") or []
    poll_seconds = af_cfg.get("poll_seconds", 30)
    while True:
        for host in hosts_cfg:
            reachable = await asyncio.to_thread(
                _host_reachable, host["address"], host.get("port", 22)
            )
            await _push_activity_event({
                "source": "infra",
                "host": host["name"],
                "status": "reachable" if reachable else "unreachable",
            })
        await asyncio.sleep(poll_seconds)


async def _poll_hermes_sessions_forever() -> None:
    hermes_cfg = CFG.get("hermes") or {}
    base_url = hermes_cfg.get("base_url", "http://127.0.0.1:8642")
    key_env = hermes_cfg.get("api_key_env", "API_SERVER_KEY")
    seen_ids: set[str] = set()
    while True:
        token = os.environ.get(key_env)
        if token:
            try:
                response = await asyncio.to_thread(
                    requests.get, f"{base_url}/api/sessions",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"limit": 10}, timeout=10,
                )
                response.raise_for_status()
                # Hermes's /api/sessions returns an OpenAI-style pagination
                # envelope: {"object": "list", "data": [...], "limit",
                # "offset", "has_more"} — NOT {"sessions": [...]} as the
                # original brief assumed. Confirmed live 2026-06-22.
                for session in response.json().get("data", []):
                    session_id = session.get("id")
                    if session_id and session_id not in seen_ids:
                        seen_ids.add(session_id)
                        await _push_activity_event({
                            "source": "hermes_session",
                            "session_id": session_id,
                            "title": session.get("title") or "(untitled session)",
                        })
            except Exception as exc:
                await _push_activity_event({"source": "hermes_session", "status": "error", "detail": str(exc)})
        await asyncio.sleep(15)


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/hud/")


if HUD_DIR.exists():
    app.mount("/hud", StaticFiles(directory=str(HUD_DIR), html=True), name="hud")


# ----------------------------------------------- Hermes dashboard TLS proxy
# The HUD (https) cannot iframe the plain-http dashboard (mixed content), so
# this second app reverse-proxies the entire dashboard over TLS, stripping
# frame-blocking headers. Served on its own port (see server.dashboard_proxy).

dash_app = FastAPI(title="Hermes Dashboard TLS Proxy")
_STRIP_HEADERS = {"x-frame-options", "content-security-policy", "content-length",
                  "transfer-encoding", "connection", "content-encoding"}


@dash_app.middleware("http")
async def dash_auth_middleware(request: Request, call_next):
    if not _request_authed(request):
        return Response(status_code=401, content="looking glass auth required")
    return await call_next(request)


def _dash_target() -> str:
    return ((CFG.get("server") or {}).get("dashboard_proxy") or {}).get(
        "target", "http://127.0.0.1:9119").rstrip("/")


@dash_app.websocket("/{path:path}")
async def dash_ws_proxy(ws: WebSocket, path: str) -> None:
    import websockets as wslib
    token = hud_token()
    if token and ws.cookies.get("looking_glass_token") != token:
        await ws.close(code=4401)
        return
    await ws.accept()
    target = _dash_target().replace("http://", "ws://").replace("https://", "wss://")
    uri = f"{target}/{path}" + (f"?{ws.url.query}" if ws.url.query else "")
    try:
        async with wslib.connect(uri, max_size=None) as backend:
            async def client_to_backend() -> None:
                while True:
                    m = await ws.receive()
                    if m.get("text") is not None:
                        await backend.send(m["text"])
                    elif m.get("bytes") is not None:
                        await backend.send(m["bytes"])
                    elif m.get("type") == "websocket.disconnect":
                        break

            async def backend_to_client() -> None:
                async for m in backend:
                    if isinstance(m, str):
                        await ws.send_text(m)
                    else:
                        await ws.send_bytes(m)

            done, pending_t = await asyncio.wait(
                [asyncio.create_task(client_to_backend()),
                 asyncio.create_task(backend_to_client())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending_t:
                t.cancel()
    except Exception:
        pass


# The dashboard's own sign-in, handled server-side so the HUD iframe never
# shows a login page. Its auth cannot simply be turned off — `hermes dashboard
# --insecure` became a no-op in the June 2026 hardening, and a non-loopback
# bind always requires an auth provider. It is a session-cookie flow (HTTP
# Basic is rejected), so the proxy signs in once, keeps the session cookie in
# this server-side Session, and re-signs-in when it expires. The dashboard
# credentials therefore stay on the server and never reach the browser —
# the same principle already used for the Hermes API key.
#
# This is gated on the HUD token like everything else on the proxy: anyone
# reaching it has already authenticated to Looking Glass.
_DASH_SESSION = requests.Session()
_DASH_LOGIN_LOCK = threading.Lock()


def _dash_auth_cfg() -> dict:
    return ((CFG.get("server") or {}).get("dashboard_proxy") or {}).get("auth") or {}


def _dash_credentials() -> tuple[str, str] | None:
    cfg = _dash_auth_cfg()
    user = os.environ.get(cfg.get("username_env", "HERMES_DASHBOARD_USER"), "")
    password = os.environ.get(cfg.get("password_env", "HERMES_DASHBOARD_PASSWORD"), "")
    return (user, password) if user and password else None


def _dash_needs_login(resp: requests.Response) -> bool:
    if resp.status_code == 401:
        return True
    location = resp.headers.get("location", "")
    return resp.is_redirect and "/login" in location


def _dash_login() -> bool:
    """Sign the proxy's session in. Returns True if a session cookie was set."""
    creds = _dash_credentials()
    if not creds:
        return False
    user, password = creds
    cfg = _dash_auth_cfg()
    login_path = cfg.get("login_path", "/auth/password-login")
    # `provider` is required — the dashboard's own sign-in JS sends it from the
    # form's data-provider attribute, and the endpoint 401s without it.
    payload = {"provider": cfg.get("provider", "basic"),
               "username": user, "password": password, "next": "/"}
    try:
        with _DASH_LOGIN_LOCK:
            resp = _DASH_SESSION.post(
                f"{_dash_target()}{login_path}", json=payload,
                timeout=30, allow_redirects=False,
            )
        ok = resp.status_code < 400 and bool(_DASH_SESSION.cookies)
        if not ok:
            print(f"[dash-proxy] sign-in failed: HTTP {resp.status_code}", flush=True)
        return ok
    except Exception as exc:
        print(f"[dash-proxy] sign-in error: {exc}", flush=True)
        return False


@dash_app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def dash_http_proxy(path: str, request: Request) -> Response:
    body = await request.body()
    # The browser's own Cookie header is dropped: the dashboard session lives
    # in _DASH_SESSION, and forwarding looking_glass_token upstream is noise.
    fwd_headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ("host", "accept-encoding", "connection", "cookie")}

    def do_request() -> requests.Response:
        return _DASH_SESSION.request(
            request.method, f"{_dash_target()}/{path}",
            params=dict(request.query_params), headers=fwd_headers,
            data=body if body else None, timeout=60, allow_redirects=False,
        )

    resp = await asyncio.to_thread(do_request)
    # Session expired (or we have never signed in): sign in and retry once.
    if _dash_needs_login(resp) and _dash_credentials():
        if await asyncio.to_thread(_dash_login):
            resp = await asyncio.to_thread(do_request)

    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _STRIP_HEADERS}
    # Never hand the dashboard's session cookie to the browser — it is the
    # proxy's, and the browser is already gated by the HUD token.
    out_headers.pop("set-cookie", None)
    out_headers.pop("Set-Cookie", None)
    return Response(content=resp.content, status_code=resp.status_code, headers=out_headers)


# ------------------------------------------------------------------ WebSocket


@dataclass
class ConnState:
    audio_chunks: list = field(default_factory=list)
    recording: bool = False
    timing: TurnTiming | None = None
    turn_task: asyncio.Task | None = None
    current_run_id: str | None = None
    conversation: str = "looking-glass-main"
    spoken_sentences: list = field(default_factory=list)
    interrupt_note: str | None = None
    partial_task: asyncio.Task | None = None
    last_partial_bytes: int = 0


async def _run_turn(ws: WebSocket, pipeline: VoicePipelineServer, conn: ConnState) -> None:
    timing = conn.timing
    assert timing is not None
    audio = b"".join(conn.audio_chunks)
    conn.audio_chunks = []
    try:
        transcript = await pipeline.transcribe(audio, timing)
        timing.transcript = transcript
        await ws.send_json({"type": "transcript", "text": transcript})
        if not transcript:
            await ws.send_json({"type": "error", "message": "No transcript detected."})
        else:
            if conn.interrupt_note:
                transcript_sent = (
                    f"[note: your previous spoken reply was cut off by the user after you said: "
                    f"\"{conn.interrupt_note}\"]\n{transcript}"
                )
                conn.interrupt_note = None
            else:
                transcript_sent = transcript
            conn.spoken_sentences = []
            await pipeline.stream_response_audio(ws, transcript_sent, timing, conn)
            timing.total_done_monotonic = time.perf_counter()
            await ws.send_json({"type": "done", "turn_id": timing.turn_id, "timing": timing.summary()})
    except asyncio.CancelledError:
        timing.errors.append("turn cancelled (barge-in or stop)")
        raise
    except Exception as exc:
        timing.errors.append(f"{type(exc).__name__}: {exc}")
        try:
            await ws.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass
    finally:
        timing.total_done_monotonic = timing.total_done_monotonic or time.perf_counter()
        pipeline.log_turn(timing)
        conn.timing = None
        conn.current_run_id = None


async def _cancel_active_turn(ws: WebSocket, pipeline: VoicePipelineServer, conn: ConnState,
                              stop_remote: bool = True) -> None:
    run_id = conn.current_run_id  # capture BEFORE cancel: turn cleanup clears it
    turn_was_active = conn.turn_task is not None and not conn.turn_task.done()
    if turn_was_active:
        if conn.spoken_sentences:
            conn.interrupt_note = conn.spoken_sentences[-1]
        conn.turn_task.cancel()
        try:
            await conn.turn_task
        except (asyncio.CancelledError, Exception):
            pass
    if stop_remote and run_id and turn_was_active:
        conn.current_run_id = None
        try:
            res = await asyncio.to_thread(pipeline.hermes.stop_run, run_id)
            # 404 = session runs not in the runs registry on this Hermes build;
            # dropping the SSE stream (above) still cuts the turn off.
            msg = "Run halted." if res["status_code"] in (200, 202, 404) else f"Stop returned {res['status_code']}."
            await ws.send_json({"type": "status", "message": msg})
        except Exception as exc:
            await ws.send_json({"type": "status", "message": f"Stop failed: {exc}"})


def _maybe_schedule_partial(ws: WebSocket, pipeline: VoicePipelineServer, conn: ConnState) -> None:
    stt_cfg = CFG.get("stt") or {}
    if not stt_cfg.get("partials", True) or not conn.recording:
        return
    if conn.partial_task and not conn.partial_task.done():
        return
    buf = b"".join(conn.audio_chunks)
    min_new = int(16000 * 2 * float(stt_cfg.get("partial_interval", 1.2)))
    if len(buf) < 16000 or len(buf) - conn.last_partial_bytes < min_new or len(buf) > 16000 * 2 * 30:
        return
    conn.last_partial_bytes = len(buf)

    async def run() -> None:
        try:
            text = await pipeline.transcribe(buf)
            if text and conn.recording:
                await ws.send_json({"type": "partial_transcript", "text": text})
        except Exception:
            pass

    conn.partial_task = asyncio.create_task(run())


TERMINAL_KEY_PATH = os.environ.get("LOOKING_GLASS_TERMINAL_KEY", "/etc/looking-glass/hud_terminal_key")


@app.websocket("/ws/terminal/{host}")
async def terminal_websocket(ws: WebSocket, host: str) -> None:
    if not _ws_allowed(ws):
        await ws.close(code=4401)
        return
    # This route grants real shell access (sam-level, sudo where the host
    # already grants it) to the whole fleet. _ws_allowed() only enforces the
    # token for callers that send an Origin header (browsers) and exempts
    # non-browser clients entirely - fine for the voice endpoint's PTT/test
    # clients, too permissive for something this sensitive. Require a
    # correctly-presented token unconditionally, regardless of Origin.
    token = hud_token()
    supplied = ws.cookies.get("looking_glass_token") or ws.query_params.get("token")
    if not token or supplied != token:
        await ws.close(code=4401)
        return
    if not is_allowed_terminal_host(host):
        await ws.close(code=4404)
        return
    # Optional ?tmux=<name>: wrap the shell in `tmux new-session -A -s <name>`
    # instead of a bare login shell, so the remote session outlives this
    # WebSocket -- a dropped connection, a closed tab, or a looking-glass
    # service restart no longer kills whatever was running (e.g. `claude`
    # mid-turn). Reopening the same host+name reattaches. Session name is
    # whitelisted to safe chars since it's interpolated into a shell command
    # sent over the SSH exec channel, not passed as an argv array.
    tmux_session = ws.query_params.get("tmux")
    if tmux_session and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", tmux_session):
        await ws.close(code=4400)
        return
    await ws.accept()
    target = TERMINAL_HOSTS[host]
    ACTIVE_TERMINALS.add(host)
    await _broadcast_network_activity(host, "claude", "start")

    try:
        async with asyncssh.connect(
            target["host"],
            username=target["user"],
            client_keys=[TERMINAL_KEY_PATH],
            # No known_hosts override: validates against the real ~/.ssh/known_hosts,
            # already populated via StrictHostKeyChecking=accept-new during key
            # deployment (see docs/superpowers/plans/... Task 8) - real host-key
            # verification, not disabled.
            #
            # keepalive: without this, an idle terminal (no keystrokes, no
            # output) sends zero SSH traffic and asyncssh has no way to tell
            # a genuinely dead connection apart from a quiet one. Any silent
            # drop along the way -- the target's sshd reaping an idle
            # session, a NAT/conntrack entry expiring on the LAN path --
            # then surfaces as this websocket suddenly closing client-side
            # ("terminal keeps disconnecting"), with no error to explain why.
            # Periodic keepalive packets give asyncssh something to fail on
            # quickly (surfacing a clear error instead of a silent hang) and,
            # as a side effect, keep any stateful NAT/firewall's idle timer
            # from expiring the connection in the first place.
            keepalive_interval=15,
            keepalive_count_max=3,
        ) as conn:
            # tmux keeps the client's terminal in the alternate-screen buffer
            # for its own rendering the entire time it's attached (that's
            # true even at a bare shell prompt inside tmux, not just full-
            # screen programs) -- so xterm.js's own scrollback essentially
            # never accumulates in a tmux session. The scrollback the user
            # actually wants live inside tmux itself, reachable only via
            # tmux's own copy-mode, which tmux enters automatically on mouse
            # wheel input ONLY once its `mouse` option is on (off by default).
            # `\; set-option -t <session> mouse on` chains onto the same tmux
            # invocation (`\;` is tmux's own command separator, escaped so
            # the shell passes a literal `;` through instead of ending the
            # command there) and re-applies every reconnect/reattach, so it
            # doesn't depend on a ~/.tmux.conf on whatever host is targeted.
            # Scoped with -t to this session only, not -g global, so it
            # doesn't change mouse behavior for the user's other tmux use on
            # the same host. Trade-off worth knowing: with mouse mode on,
            # click-drag selects inside tmux's copy-mode instead of the
            # browser's native text selection -- hold Shift while dragging
            # to bypass tmux's mouse reporting and get a normal selection.
            proc_cmd = (
                f"tmux new-session -A -s {tmux_session} "
                f"\\; set-option -t {tmux_session} mouse on"
                if tmux_session else None
            )
            async with conn.create_process(
                proc_cmd, term_type="xterm-256color"
            ) as process:

                async def pump_output() -> None:
                    try:
                        while True:
                            data = await process.stdout.read(4096)
                            if not data:
                                break
                            await ws.send_text(data)
                    except asyncssh.Error:
                        pass

                pump_task = asyncio.create_task(pump_output())
                try:
                    while True:
                        # Keystrokes arrive as text frames (written straight to
                        # stdin, unchanged). Resize notifications arrive as
                        # BINARY frames carrying {"cols":.., "rows":..} JSON -
                        # a distinct frame type so a resize message can never
                        # be mistaken for literal shell input. Without this,
                        # the remote pty stays locked at its initial size
                        # forever: tmux/the shell keep wrapping and redrawing
                        # for stale dimensions while xterm.js renders at the
                        # real (resized) one, producing garbled/overlapping
                        # text on long output.
                        message = await ws.receive()
                        if message["type"] == "websocket.disconnect":
                            break
                        data = message.get("bytes")
                        if data is not None:
                            try:
                                size = json.loads(data.decode())
                                process.change_terminal_size(
                                    int(size["cols"]), int(size["rows"])
                                )
                            except Exception:
                                pass
                            continue
                        text = message.get("text")
                        if text is not None:
                            process.stdin.write(text)
                except WebSocketDisconnect:
                    pass
                finally:
                    pump_task.cancel()
                    process.stdin.write_eof()
    except (asyncssh.Error, OSError) as e:
        try:
            await ws.send_text(f"\r\n[connection error: {e}]\r\n")
        except Exception:
            pass
        await ws.close(code=1011)
    finally:
        ACTIVE_TERMINALS.discard(host)
        await _broadcast_network_activity(host, "claude", "end")


# ------------------------------------------------------------- service control
# Start/stop/restart the backend services the HUD depends on (the vLLM GPU
# brain on snarf, the Hermes gateway and dashboard on the hermes LXC) over the
# terminal panel's existing SSH key and host allowlist — no new credentials.
#
# Two systemd scopes are supported, because they are genuinely both in play:
#   system -> `sudo systemctl ...`            (vllm, hermes-dashboard)
#   user   -> `systemctl --user ...` with XDG_RUNTIME_DIR set
# The Hermes gateway runs as a *user* unit under root's user manager
# (user@0.service/app.slice/hermes-gateway.service), which is why it never
# shows up in `systemctl list-units` and needs the runtime dir exported for
# non-interactive SSH to reach the right systemd instance.

_SERVICE_ACTIONS = {"start", "stop", "restart"}


def _service_defs() -> list[dict]:
    return CFG.get("service_control") or []


def _find_service(service_id: str) -> dict | None:
    return next((s for s in _service_defs() if s.get("id") == service_id), None)


def _systemctl(svc: dict, verb: str) -> str:
    """Build the systemctl command for one service, honouring its scope."""
    unit = shlex.quote(svc["unit"])
    if (svc.get("scope") or "system") == "user":
        runtime_dir = svc.get("runtime_dir", "/run/user/0")
        return f"XDG_RUNTIME_DIR={shlex.quote(runtime_dir)} systemctl --user {verb} {unit}"
    prefix = "" if verb == "is-active" else "sudo "
    return f"{prefix}systemctl {verb} {unit}"


async def _service_ssh_exec(svc: dict, cmd: str) -> tuple[int, str]:
    """One-shot SSH command against a service's host — same key/user as the
    terminal panel (TERMINAL_HOSTS), a single command instead of a shell."""
    host_key = svc.get("host", "")
    if host_key not in TERMINAL_HOSTS:
        raise RuntimeError(f"service host {svc.get('host')!r} is not a known terminal host")
    # pooled: the service panel polls every host's units on a timer, so a
    # connection per status check was the bulk of the sshd volume on hermes.
    rc, output = await _ssh_run(host_key, cmd)
    return rc, output.strip()


async def _service_state(svc: dict) -> str:
    try:
        _, output = await _service_ssh_exec(svc, _systemctl(svc, "is-active"))
    except Exception:
        return "unreachable"
    return output.splitlines()[0].strip() if output else "unknown"


@app.get("/api/services")
async def services_status() -> JSONResponse:
    defs = _service_defs()
    if not defs:
        return JSONResponse({"services": []})
    states = await asyncio.gather(*[_service_state(s) for s in defs])
    return JSONResponse({"services": [
        {"id": s["id"], "label": s.get("label", s["id"]), "host": s.get("host"),
         "unit": s.get("unit"), "scope": s.get("scope", "system"),
         "critical": bool(s.get("critical")), "active": state}
        for s, state in zip(defs, states)
    ]})


@app.post("/api/services/{service_id}/action")
async def service_action(service_id: str, request: Request) -> JSONResponse:
    svc = _find_service(service_id)
    if not svc:
        return JSONResponse({"error": f"unknown service {service_id!r}"}, status_code=404)
    body = await request.json()
    action = body.get("action")
    if action not in _SERVICE_ACTIONS:
        return JSONResponse({"error": f"action must be one of {sorted(_SERVICE_ACTIONS)}"}, status_code=400)
    try:
        exit_status, output = await _service_ssh_exec(svc, _systemctl(svc, action))
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return JSONResponse({"ok": exit_status == 0, "id": service_id, "action": action,
                         "output": output[-2000:]})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    if not _ws_allowed(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    WS_CLIENTS.add(ws)
    pipeline = get_pipeline()
    conn = ConnState(conversation=(CFG.get("hermes") or {}).get("conversation", "looking-glass-main"))
    await ws.send_json({"type": "status", "message": "Hermes voice server connected."})
    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                # Raw ws.receive() RETURNS this dict rather than raising
                # WebSocketDisconnect (only receive_text/json/bytes raise).
                # Without this the loop falls through both branches, calls
                # receive() again on a closed socket, and starlette raises
                # RuntimeError -> "Exception in ASGI application" on every
                # client disconnect. Re-raise so the existing handler below
                # still cancels an in-flight turn_task.
                raise WebSocketDisconnect(message.get("code", 1005))
            if "text" in message and message["text"] is not None:
                event = json.loads(message["text"])
                etype = event.get("type")
                if etype == "start":
                    await _cancel_active_turn(ws, pipeline, conn)  # barge-in
                    if event.get("conversation"):
                        conn.conversation = str(event["conversation"])
                    conn.audio_chunks = []
                    conn.last_partial_bytes = 0
                    conn.recording = True
                    conn.timing = TurnTiming(turn_id=pipeline.next_turn_id())
                    conn.timing.audio_start_monotonic = time.perf_counter()
                    conn.timing.stt_model = CFG["stt"]["model"]
                    await ws.send_json({"type": "status", "message": f"Turn {conn.timing.turn_id} recording started."})
                elif etype == "stop":
                    if conn.timing is None:
                        await ws.send_json({"type": "error", "message": "Received stop before start."})
                        continue
                    conn.recording = False
                    conn.timing.end_of_speech_monotonic = time.perf_counter()
                    conn.turn_task = asyncio.create_task(_run_turn(ws, pipeline, conn))
                elif etype == "stop_run":
                    await _cancel_active_turn(ws, pipeline, conn)
                    await ws.send_json({"type": "agent_status", "state": "stopped"})
                elif etype == "approval_decision":
                    run_id = event.get("run_id") or conn.current_run_id
                    if not run_id:
                        await ws.send_json({"type": "error", "message": "No run for approval."})
                        continue
                    decision = event.get("decision", "deny")
                    body = {
                        "decision": decision,
                        "approved": decision == "allow",
                        "approval_id": event.get("approval_id"),
                    }
                    res = await asyncio.to_thread(pipeline.hermes.post_approval, run_id, body)
                    await ws.send_json({"type": "status", "message": f"Approval sent ({res['status_code']})."})
                else:
                    await ws.send_json({"type": "error", "message": f"Unknown event type: {etype}"})
            elif "bytes" in message and message["bytes"] is not None:
                if conn.recording:
                    conn.audio_chunks.append(message["bytes"])
                    _maybe_schedule_partial(ws, pipeline, conn)
    except WebSocketDisconnect:
        if conn.turn_task and not conn.turn_task.done():
            conn.turn_task.cancel()
        print("Client disconnected", flush=True)
    finally:
        WS_CLIENTS.discard(ws)


def main() -> int:
    server = CFG["server"]
    host = server.get("host", "0.0.0.0")
    port = int(server.get("port", 8765))
    tls_ports = server.get("tls_ports") or ([server["tls_port"]] if server.get("tls_port") else [])
    cert = server.get("tls_cert")
    key = server.get("tls_key")
    print(f"Starting Hermes voice server on ws://{host}:{port}/ws", flush=True)
    if tls_ports and cert and key and (ROOT / cert).exists() and (ROOT / key).exists():
        servers = [uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))]
        for tp in tls_ports:
            print(f"HUD available on https://{host}:{tp}/hud/", flush=True)
            servers.append(uvicorn.Server(uvicorn.Config(
                app, host=host, port=int(tp), log_level="info",
                ssl_certfile=str(ROOT / cert), ssl_keyfile=str(ROOT / key),
            )))

        dp = server.get("dashboard_proxy") or {}
        if dp.get("port"):
            print(f"Dashboard proxy on https://{host}:{dp['port']}/", flush=True)
            servers.append(uvicorn.Server(uvicorn.Config(
                dash_app, host=host, port=int(dp["port"]), log_level="warning",
                ssl_certfile=str(ROOT / cert), ssl_keyfile=str(ROOT / key),
            )))

        async def serve_all() -> None:
            await asyncio.gather(*[s.serve() for s in servers])

        asyncio.run(serve_all())
    else:
        uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
