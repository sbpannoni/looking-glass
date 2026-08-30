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
import base64
import hashlib
import json
import os
import re
import shlex
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Iterator
from urllib.parse import quote

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

    def _rack_by(key: str, label: str) -> dict:
        """One Prometheus result set, indexed by a label instead of taking [0].

        Taking series[0] is why MACHINES only ever showed one box's numbers:
        `node_load1` returns a series per host and everything after the first
        was thrown away.
        """
        out: dict = {}
        series = rack.get(key)
        if isinstance(series, list):
            for r in series:
                k = (r.get("labels") or {}).get(label)
                if k is not None:
                    out[str(k)] = r.get("value")
        return out

    host_load = _rack_by("fleet_load1", "host")
    host_mem = _rack_by("fleet_mem_used_ratio", "host")
    host_disk = _rack_by("fleet_disk_used_ratio", "host")
    host_uptime = _rack_by("fleet_uptime_seconds", "host")
    host_swap = _rack_by("fleet_swap_used_ratio", "host")
    guest_cpu = _rack_by("guest_cpu_ratio", "id")
    guest_mem_used = _rack_by("guest_mem_used_bytes", "id")
    guest_mem_total = _rack_by("guest_mem_total_bytes", "id")

    _virt_cfg = (CFG.get("ownership") or {}).get("virt") or {}

    def _guest_id(node_id: str) -> str | None:
        """topology node -> Proxmox guest id, reusing ownership.virt.

        ownership.virt already records CT112 / VM 100 for the HUD's QUICK
        ACCESS tiles; deriving from it keeps one source of truth instead of a
        second hand-maintained mapping that can drift.
        """
        ident = str((_virt_cfg.get(node_id) or {}).get("id") or "")
        m = re.match(r"\s*CT\s*(\d+)", ident, re.I)
        if m:
            return f"lxc/{m.group(1)}"
        m = re.match(r"\s*VM\s*(\d+)", ident, re.I)
        if m:
            return f"qemu/{m.group(1)}"
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

        nid = node["id"]

        # This host measures itself: psutil is more accurate than scraping our
        # own exporter, so it wins where available.
        if nid == "looking-glass" and psutil:
            info.update({
                "cpu": psutil.cpu_percent(interval=0.1),
                "mem": psutil.virtual_memory().percent,
                "disk": psutil.disk_usage(str(ROOT)).percent,
            })

        # node_exporter, for anything that runs one (snarf, beelink,
        # claude-control). Keyed by host label == topology id.
        if nid in host_load:
            info["load1"] = round(host_load[nid], 2)
        if "mem" not in info and nid in host_mem:
            info["mem"] = round(host_mem[nid] * 100, 1)
            info["mem_source"] = "node"      # MemAvailable-based: real pressure
        if "disk" not in info and nid in host_disk:
            info["disk"] = round(host_disk[nid] * 100, 1)
        if nid in host_uptime:
            info["uptime_s"] = int(host_uptime[nid])
        if nid in host_swap:
            info["swap"] = round(host_swap[nid] * 100, 1)

        # Proxmox guest series fill the gap for containers with no exporter of
        # their own -- hermes being the one that mattered.
        gid = _guest_id(nid)
        if gid:
            if "cpu" not in info and gid in guest_cpu:
                info["cpu"] = round(guest_cpu[gid] * 100, 1)
            if "mem" not in info and gid in guest_mem_used and guest_mem_total.get(gid):
                info["mem"] = round(guest_mem_used[gid] / guest_mem_total[gid] * 100, 1)
                # NOT comparable to the node_exporter figure. For a QEMU guest
                # with no balloon target this counts pages the guest has TOUCHED,
                # so Linux page cache drives it to the ceiling and it plateaus
                # there forever. home-assistant sat at a flat 93-94% for 14 days
                # with no upward trend -- normal steady state, not pressure.
                # Tagged so the UI does not red-flag it against a threshold
                # meant for MemAvailable.
                info["mem_source"] = "guest-allocated"

        if nid == "snarf":
            temp = _rack_value("snarf_gpu_temp_c")
            if temp is not None:
                info["gpu_temp"] = round(temp)

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
        "tool_calling": True,
        "params": "27B dense, hybrid linear/full attention, 98K ctx (served)",
        "status": "resident default",
        "good_for": "Current pipeline default. Cited by multiple community sources as "
                     "the highest verified SWE-bench Verified score among models that "
                     "run on consumer hardware. General coding + reasoning. STABILITY-TESTED 2026-08-21: a --repeats 2 pass (plus one extra confirmatory run) put its true rate at 0.704 (19/27) -- higher than the original single-run 0.6 baseline, since one task (collab-compute-rpkm) that originally failed passed on all 3 retest attempts. A legitimate rival to Devstral now, not a clear-cut incumbent-vs-challenger case.",
    },
    {
        "id": "qwen2.5-32b-awq", "label": "Qwen2.5 32B", "license": "Apache 2.0",
        "tool_calling": True,
        "params": "32B dense, 32K ctx",
        "status": "resident (rotation)",
        "good_for": "General-purpose, not code-specialized. Baseline the code-tuned "
                     "sibling (Qwen2.5-Coder-32B) gets compared against — same size "
                     "class, so the delta isolates what code-specific training buys.",
    },
    {
        "id": "llama3.1-70b-awq", "label": "Llama 3.1 70B", "license": "Llama 3.1 Community",
        "tool_calling": True,
        "params": "70B dense, TP=2 required",
        "status": "resident (rotation)",
        "good_for": "Largest resident model. General-purpose, not code-tuned. Tests "
                     "whether raw scale beats smaller code-specialized models on these "
                     "chunk-sized edits.",
    },
    {
        "id": "qwen3-14b-awq", "label": "Qwen3 14B", "license": "Apache 2.0",
        "tool_calling": True,
        "params": "14.8B dense, TP=1, 40K ctx",
        "status": "resident (rotation)",
        "good_for": "Smallest resident model — fits one GPU, fastest cold-swap. Speed/"
                     "quality floor reference for the rest of the roster.",
    },
    {
        "id": "devstral-small-2-24b-awq", "label": "Devstral Small 2 24B", "license": "Apache 2.0",
        "tool_calling": True,
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
        # Emits ```json blocks; no registered vLLM parser matches (deepseek_v3
        # expects <|tool_call_begin|>), so tool calls arrive as prose. Cannot be
        # assigned to a role. Verified on snarf 2026-08-26.
        "tool_calling": False,
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
        "tool_calling": True,
        "params": "30.5B total / 3.3B active MoE (128 experts, 8 routed)",
        "status": "tested 2026-08-21 — 0.5 pass rate, CONFIRMED STABLE, fast (avg 45s)",
        "good_for": "Coder-tuned MoE sibling of the resident default — fast inference "
                     "despite the total param count. Was flagged higher-risk (AWQ+MoE "
                     "instability reports elsewhere) but ran clean here: no crash-loop, "
                     "no expert-parallel flag even needed with the deployed config.",
    },
    {
        "id": "qwen2.5-coder-32b-instruct-awq", "label": "Qwen2.5-Coder 32B", "license": "Apache 2.0",
        "tool_calling": True,
        "params": "32B dense",
        "status": "tested 2026-08-21 — 0.5 pass rate, 1 disagreement (self-reported done, code was actually broken)",
        "good_for": "Official Qwen-org AWQ quant of the dedicated code model in the "
                     "same size class as the already-resident general Qwen2.5-32B — "
                     "direct code-specialized-vs-general ablation.",
    },
    {
        "id": "kimi-linear-48b-a3b-awq", "label": "Kimi-Linear 48B-A3B", "license": "MIT",
        "tool_calling": True,
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
        # Ships no chat_template.jinja; its tokenizer_config template ignores
        # `tools`, so the model never sees tool definitions and can't emit tool
        # calls. Cannot be assigned to a role. Verified on snarf 2026-08-26.
        "tool_calling": False,
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
        "tool_calling": True,
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
        "tool_calling": True,
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
        "tool_calling": True,
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
        "roster": [
            {"id": e["id"], "label": e["label"], "tool_calling": e.get("tool_calling", True)}
            for e in CODER_MODELS_ROSTER
        ],
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
    # A model that can't emit structured tool calls produces prose instead of
    # executable tool calls in any role -- the run looks like a refusal. Reject
    # here, not just in the UI, since this endpoint is callable directly.
    if not _CODER_ROSTER_BY_ID[model].get("tool_calling", True):
        return JSONResponse(
            {"ok": False, "error": f"model {model!r} cannot emit structured tool calls "
                                   f"and cannot be assigned to a role"},
            status_code=400,
        )

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


# --- Hermes kanban plugin API ---------------------------------------------
# Two ways to reach the board on CT111: exec `hermes kanban` over ssh (a
# process spawn and ~380ms per call), or the dashboard's own kanban plugin
# REST API at /api/plugins/kanban. The API wins on every axis -- one HTTP
# round trip, columns already ordered by the board's own status model, 42
# fields per card instead of the 9 `ls --json` keeps, and a real per-task
# endpoint -- so it is the primary path and ssh stays as the fallback for
# when the dashboard is down. Neither path is authoritative on its own: the
# board degrades to the CLI rather than breaking.
#
# Auth is the dashboard's interactive session-cookie flow. There is NO
# service-token path -- the bearer seam only guards routes that call
# register_token_route(), and only the drain plugin does that -- so the HUD
# logs in with the configured basic-auth credential and holds the cookies on
# a requests.Session. The access cookie lasts 12h and the refresh cookie 30d,
# and the dashboard rotates the access token transparently, so a re-login is
# an exception path (401), not something to schedule.
_KANBAN_API_LOCK = threading.Lock()
_KANBAN_API_SESSION: requests.Session | None = None


def _kanban_api_base() -> str:
    return (os.environ.get("HERMES_DASHBOARD_URL")
            or _kanban_cfg().get("dashboard_url") or "").rstrip("/")


def _kanban_api_login(session: requests.Session, base: str) -> None:
    user = os.environ.get("HERMES_DASHBOARD_USER", "")
    password = os.environ.get("HERMES_DASHBOARD_PASSWORD", "")
    if not user or not password:
        raise RuntimeError("HERMES_DASHBOARD_USER / HERMES_DASHBOARD_PASSWORD not set")
    r = session.post(f"{base}/auth/password-login", timeout=20,
                     json={"provider": "basic", "username": user, "password": password})
    if r.status_code != 200:
        raise RuntimeError(f"dashboard login failed: {r.status_code}")


def _kanban_api_call(method: str, path: str, **kwargs) -> requests.Response:
    """One authenticated call to the plugin API, re-logging in once on 401.

    Blocking on purpose -- callers go through asyncio.to_thread, same as the
    rest of this server's outbound HTTP."""
    global _KANBAN_API_SESSION
    base = _kanban_api_base()
    if not base:
        raise RuntimeError("kanban.dashboard_url is not configured")
    with _KANBAN_API_LOCK:
        session = _KANBAN_API_SESSION
        just_logged_in = session is None
        if session is None:
            session = requests.Session()
            _kanban_api_login(session, base)
            _KANBAN_API_SESSION = session
    r = session.request(method, f"{base}{path}", timeout=30, **kwargs)
    # A 401 on a session we did not just mint means the cookies lapsed (or the
    # dashboard restarted and rotated its signing secret). One re-login, one
    # retry; a 401 straight after a fresh login is a bad credential, not a
    # stale cookie, so it propagates instead of looping.
    if r.status_code == 401 and not just_logged_in:
        with _KANBAN_API_LOCK:
            _kanban_api_login(session, base)
        r = session.request(method, f"{base}{path}", timeout=30, **kwargs)
    r.raise_for_status()
    return r


async def _kanban_api_get(path: str) -> dict:
    r = await asyncio.to_thread(_kanban_api_call, "GET", path)
    return r.json()


# The CLI's --json carries the first ten; the rest exist only on the plugin
# API and are what let a card show its state instead of just a status word.
_KANBAN_TASK_FIELDS = (
    "id", "title", "status", "assignee", "created_by", "created_at",
    "started_at", "completed_at", "result", "session_id",
    "age", "priority", "progress", "comment_count", "link_counts",
    "latest_summary", "model_override", "block_kind", "current_run_id",
    "last_failure_error",
)


def _kanban_task_slim(task: dict) -> dict:
    return {k: task.get(k) for k in _KANBAN_TASK_FIELDS if k in task}


@app.get("/api/kanban")
async def kanban_board() -> JSONResponse:
    """The board.

    `tasks` keeps its original contract -- a flat list, newest first -- so
    every existing caller is unaffected. `columns` is additive: the plugin
    API already groups cards into the board's own ordered statuses, which is
    what a lane view needs and what the CLI path cannot produce."""
    try:
        board = await _kanban_api_get("/api/plugins/kanban/board")
        columns = [{"name": c.get("name"),
                    "tasks": [_kanban_task_slim(t) for t in c.get("tasks") or []]}
                   for c in board.get("columns") or []]
        tasks = [t for col in columns for t in col["tasks"]]
        tasks.sort(key=lambda t: t.get("created_at") or 0, reverse=True)
        return JSONResponse({"tasks": tasks, "columns": columns,
                             "assignees": board.get("assignees") or [],
                             "latest_event_id": board.get("latest_event_id"),
                             "source": "api"})
    except Exception as exc:
        api_error = str(exc)
    try:
        _, out = await _kanban_ssh("hermes kanban ls --json --sort created-desc")
    except Exception as exc:
        return JSONResponse({"tasks": [], "error": f"{api_error}; ssh fallback: {exc}"},
                            status_code=502)
    try:
        data = json.loads(out.strip() or "[]")
    except json.JSONDecodeError:
        return JSONResponse({"tasks": [], "error": "unparseable board output"}, status_code=502)
    rows = data if isinstance(data, list) else data.get("tasks", [])
    keep = ("id", "title", "status", "assignee", "created_by", "created_at",
            "started_at", "completed_at", "result", "session_id")
    return JSONResponse({"tasks": [{k: t.get(k) for k in keep} for t in rows],
                         "source": "ssh", "api_error": api_error})


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


@app.post("/api/kanban/reclaim")
async def kanban_reclaim(request: Request) -> JSONResponse:
    """Release the worker claim on a stuck `running` card via
    `hermes kanban reclaim <id>`.

    What that actually does (hermes_cli/kanban_db.py:reclaim_task, read
    2026-08-28): SIGTERM then SIGKILL the worker process if its claim lock is
    host-local, clear claim_lock/claim_expires/worker_pid, close the current
    run with outcome `reclaimed`, and set the card to `ready`.

    `ready` is DISPATCHABLE. The card does not park -- the gateway dispatcher
    picks it up on its next pass and starts a fresh run, at the cost of
    another model run. It does NOT touch consecutive_failures, so this can be
    done repeatedly without tripping the retry circuit breaker.

    This is the missing half of the board's manual controls. Blocked cards
    had Unblock and done cards had Archive, but a card whose worker died --
    the runtime cap fired, the model endpoint went away, the gateway was
    restarted mid-run -- stays `running` forever with the claim still held,
    and nothing in the HUD could move it. The dispatcher's own stale-claim
    sweep only runs on its tick and only past its own timeout; this is the
    user-driven path for the card that is already visibly wedged.

    A reason is recorded on the reclaimed event so the card's history says a
    human did this, rather than it looking like another silent requeue."""
    payload = await request.json()
    task_id = (payload.get("task_id") or "").strip()
    if not _TASK_ID_RE.match(task_id):
        return JSONResponse({"ok": False, "error": "bad task id"}, status_code=400)
    reason = "reclaimed from the Looking Glass board"
    try:
        rc, out = await _kanban_ssh(
            f"hermes kanban reclaim {shlex.quote(task_id)} "
            f"--reason {shlex.quote(reason)}")
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    if rc != 0:
        return JSONResponse({"ok": False, "error": out[-1000:]}, status_code=502)
    return JSONResponse({"ok": True})


# ------------------------------------------------------- pipeline pause
# `hermes pause` is Hermes's own global emergency stop, and it is exactly the
# right shape for "stop the pipeline but do not lose anything": the dispatcher
# checks it every tick BEFORE spawning, so it takes effect on the next pass
# with no restart, in-flight workers are never killed, and cards stay `ready`
# so `hermes resume` picks up precisely where it left off.
#
# It was only reachable from a shell on CT111, which meant the way to stop
# runaway work from the board was to reclaim cards one at a time -- killing
# their workers and losing whatever they had done. This exposes the correct
# tool where the runaway is actually visible.

@app.get("/api/kanban/pause")
async def kanban_pause_state() -> JSONResponse:
    """Whether the global stop is engaged, and why."""
    try:
        rc, out = await _kanban_ssh(
            "hermes status --json 2>/dev/null || hermes pause --help >/dev/null; "
            "test -f ~/.hermes/ESTOP && cat ~/.hermes/ESTOP || echo NOTPAUSED")
    except Exception as exc:
        return JSONResponse({"paused": None, "error": str(exc)}, status_code=502)
    text = (out or "").strip()
    if "NOTPAUSED" in text or not text:
        return JSONResponse({"paused": False})
    reason = ""
    try:
        reason = (json.loads(text) or {}).get("reason") or ""
    except Exception:
        reason = text[:200]
    return JSONResponse({"paused": True, "reason": reason})


@app.post("/api/kanban/pause")
async def kanban_pause(request: Request) -> JSONResponse:
    """Engage or release the global stop.

    Body: {"paused": true|false, "reason": "..."}. Deliberately NOT a toggle --
    a toggle read from a stale board would do the opposite of what was
    intended, and this is the control you reach for when something is already
    going wrong."""
    payload = await request.json()
    want = bool(payload.get("paused"))
    reason = (payload.get("reason") or "paused from the Looking Glass board").strip()
    cmd = (f"hermes pause --reason {shlex.quote(reason[:200])}" if want
           else "hermes resume")
    try:
        rc, out = await _kanban_ssh(cmd)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    if rc != 0:
        return JSONResponse({"ok": False, "error": out[-500:]}, status_code=502)
    return JSONResponse({"ok": True, "paused": want, "detail": out.strip()[-300:]})


# ------------------------------------------------------------- edit a card
# The whole diagnosis loop turns on amending a spec: a red test gate usually
# means the card was short, and the fix is to say the missing thing and retry.
# The worker can do that (`dispatch_to_engine(amended_description=...)`), but a
# HUMAN had no way to edit a card at all from the board -- not blocked, not
# ready, not any status. Telling someone "amend the card and unblock it" while
# giving them no field to type in is not a workflow.
#
# The dispatch-target block is deliberately NOT editable here. It is written by
# provisioning and describes machinery (worktree, branch, base) that a person
# editing a spec has no reason to retype and every reason to clobber by
# accident. It is stripped before editing and re-attached on save, so the
# textarea holds exactly the task text and nothing else.

@app.post("/api/kanban/{task_id}/edit")
async def kanban_edit(task_id: str, request: Request) -> JSONResponse:
    """Replace a card's task text, preserving its dispatch-target block."""
    if not _TASK_ID_RE.match(task_id):
        return JSONResponse({"ok": False, "error": "bad task id"}, status_code=400)
    payload = await request.json()
    new_text = (payload.get("body") or "").strip()
    if not new_text:
        return JSONResponse({"ok": False, "error": "body is empty"}, status_code=400)
    try:
        detail = await _kanban_task_detail(task_id)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"board unreadable: {exc}"},
                            status_code=502)
    old_body = (detail.get("task") or {}).get("body") or ""
    m = _DISPATCH_TARGET_RE.search(old_body)
    block = (m.group(0) + "\n\n") if m else ""
    try:
        await asyncio.to_thread(
            _kanban_api_call, "PATCH",
            f"/api/plugins/kanban/tasks/{quote(task_id)}",
            json={"body": block + new_text})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return JSONResponse({"ok": True, "dispatch_target_preserved": bool(block)})


@app.post("/api/kanban/{task_id}/comment")
async def kanban_comment(task_id: str, request: Request) -> JSONResponse:
    """Append a comment. Comments outlive the pane and a retrying worker reads
    them, so this is how a human hands a finding to the next attempt."""
    if not _TASK_ID_RE.match(task_id):
        return JSONResponse({"ok": False, "error": "bad task id"}, status_code=400)
    payload = await request.json()
    text = (payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "comment is empty"}, status_code=400)
    try:
        rc, out = await _kanban_ssh(
            f"hermes kanban comment {shlex.quote(task_id)} {shlex.quote(text[:4000])}")
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    if rc != 0:
        return JSONResponse({"ok": False, "error": out[-500:]}, status_code=502)
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

# Work that cannot be done by a dispatched worker at all: it needs a human
# driving a real Electron window against real outputs. TODO.md says so in
# its own words -- section 6 is literally titled "UI — needs a live Electron
# session against real outputs" -- but nothing read that, so six items no
# agent can execute sat in the picker looking submittable. Filing one costs
# a full dispatch that can only ever come back asking for a UI.
#
# Deliberately narrow phrases, matched against the section heading and the
# item's own text. A bare mention of "UI" is not enough: item 2's
# "genuinely ready, no known UI issues" is exactly the kind of line a loose
# /\bUI\b/ would misfile as unsubmittable.
_TODO_NEEDS_UI_RE = re.compile(
    r"(live|real|manual|interactive)\s+(electron|ui|gui)\s+(session|run)"
    r"|(electron|ui|gui)\s+session"
    r"|needs?\s+a\s+(live\s+)?(electron|ui|gui)"
    r"|requires?\s+a\s+(live\s+)?(electron|ui|gui)",
    re.IGNORECASE,
)


def _todo_needs_ui(section: str | None, text: str) -> bool:
    """True when this item can only be done in a live UI session.

    The section heading counts as much as the item text: TODO.md states the
    requirement once, at the top of the section, and then does not repeat it
    on each of the six items beneath it."""
    return bool(_TODO_NEEDS_UI_RE.search(section or "")
                or _TODO_NEEDS_UI_RE.search(text or ""))


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

    for line_no, line in enumerate(text.splitlines()):
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
                # Which line the checkbox itself is on. The write-back edits
                # that one line in place rather than re-serialising the file,
                # so everything this parser does not model -- prose between
                # sections, nested bullets, tables -- survives untouched.
                "line": line_no,
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
            "needs_ui": _todo_needs_ui(it["section"], cleaned),
            "line": it["line"],
            "text": cleaned,
            "title": cleaned.split("\n")[0][:140],
        })
    return result


# --------------------------------------------- DARKHELIX worktree isolation
# Before this, every kanban worker SSHed to snarf and edited the ONE shared
# checkout on master. Cards carried a `[dispatch-target] branch: wt/...` line
# that nothing ever read: no branch and no worktree was created, so five days
# of agent work (552 lines across four cards) sat uncommitted and
# indistinguishable in the main tree until it was salvaged by hand.
#
# Isolation is created HERE, at submit time, over the connection pool this
# server already holds -- not delegated to the agent. An agent that has to be
# told to isolate itself is exactly what failed.
#
# Branch and path derive from the TASK ID, never from the card body: the
# triage specifier rewrites title and body on promotion, so anything parsed
# back out of the body is untrustworthy by the time it matters.
# ONE ROOT FOR AGENT WORK, on the box the work happens on.
#
# It used to be spread over five places across two hosts: card worktrees under
# /home/sam, engine attempt trees under /ssdpool/coder-engine, patches and
# attachments under /root/.hermes on CT111, and — because nothing said
# otherwise — whatever an agent picked in /tmp. That last one is not
# hypothetical: one run left its finished rewrite at /tmp/synthetic_pcr_new.py
# and an earlier attempt left a whole worktree at /tmp/darkhelix_worktree.
# /tmp is cleared on reboot, so "where did the work go" had five answers and a
# deadline.
#
# Everything a card produces on snarf now lives under one per-task directory:
#
#     /ssdpool/agent-work/<task_id>/worktree/        the card's git worktree
#     /ssdpool/agent-work/<task_id>/attempts/<n>/    engine attempt + output
#
# One place to look, one to back up, one to point a cleanup job at. The
# directory is created by hand (sudo, owned by sam, setgid) because /ssdpool's
# root is root-owned; sam owns everything beneath it.
DARKHELIX_WORK_ROOT = "/ssdpool/agent-work"
DARKHELIX_BASE_BRANCH = "master"

# The repo is 1.5 TB on disk and 5.7 MB of tracked source -- the rest is
# databases and reference data git does not carry. A worktree gets the source
# in ~6 MB; these are shared back by symlink so a card can actually RUN.
# Outputs are deliberately absent from this list: DARKHELIX_output/ and
# testruns/ stay per-worktree so two cards cannot overwrite each other.
DARKHELIX_SHARED = ("database", "thirdParty", "testData", ".venv-dev")

# Artefacts the toolchain drops in the tree that must never reach a commit.
#
# Every engine attempt otherwise carried two of them, reproduced byte-for-byte
# across runs: an uninvited `.aider*` line appended to .gitignore, and an empty
# stringutils.py (the toy repo's filename). commit_node stages with `git add
# -A`, so both rode along into the patch.
#
# Excluding them is strictly better than narrowing what commit_node stages,
# which would also silently drop legitimately-added files -- a new test being
# the obvious case, and one the specs here explicitly contemplate. This is a
# named exclusion of two known artefacts; anything genuinely new still commits.
#
# `.aider*` also stops Aider writing to .gitignore in the first place rather
# than just hiding the result: aider/main.py's check_gitignore() appends the
# pattern only `if not repo.ignored(".aider")`, and that check is git
# check-ignore, which honours info/exclude. Ignored here, it returns early and
# never opens the file.
DARKHELIX_AGENT_ARTIFACTS = (".aider*", "stringutils.py")


def _dh_branch(task_id: str) -> str:
    return f"hermes/{task_id}"


def _dh_task_dir(task_id: str) -> str:
    """Everything this card produces on snarf, in one place."""
    return f"{DARKHELIX_WORK_ROOT}/{task_id}"


def _dh_worktree(task_id: str) -> str:
    return f"{_dh_task_dir(task_id)}/worktree"


# The dispatch-target block is the contract between this server (which
# provisions isolation) and the worker (which must use it and nothing else).
# `execution-engine-dispatch`'s SKILL.md step 1 refuses to work a card without
# a valid one, so "valid" has to mean the same thing on both sides.
_DISPATCH_TARGET_RE = re.compile(
    r"\[dispatch-target\](?P<inner>.*?)\[/dispatch-target\]", re.S)


# Everything this server writes onto a card lives INSIDE the block, prose
# included, so re-provisioning replaces the note wholesale. When only the
# bracketed header was replaceable, each re-provision stripped the header and
# left the previous prose behind, stacking another copy of "Work in the
# worktree above..." on every pass.
_DT_NOTES_SEP = "--- notes ---"


def _dispatch_target_fields(body: str) -> dict[str, str]:
    m = _DISPATCH_TARGET_RE.search(body or "")
    if not m:
        return {}
    fields: dict[str, str] = {}
    for line in m.group("inner").splitlines():
        # The human half is free prose and several of its lines contain a
        # colon ("IN YOUR TREE: ..."), so parsing must stop here or it
        # invents fields out of sentences.
        if line.strip() == _DT_NOTES_SEP:
            break
        if ":" in line:
            k, _, v = line.partition(":")
            fields[k.strip().lower()] = v.strip()
    return fields


def _dispatch_target_is_current(body: str, task_id: str) -> bool:
    """True only for a block this server's CURRENT provisioning wrote.

    Deliberately strict, because a stale block is worse than none: the first
    HUD create path wrote `[dispatch-target] branch: wt/<slug>-<epoch>` with
    no worktree line and created nothing, and the decomposer's LLM then copied
    that dead branch name into every child body. Cards carrying that older
    shape must be re-provisioned, not skipped as "already done", so the test
    is for the exact worktree + branch this server would create now."""
    fields = _dispatch_target_fields(body)
    return (fields.get("worktree") == _dh_worktree(task_id)
            and fields.get("branch") == _dh_branch(task_id))


async def _darkhelix_worktree_create(
        task_id: str, parent_task_ids: list[str] | None = None) -> dict:
    """Create the card's isolated worktree on snarf. Idempotent.

    Returns (ok, output, base) where `base` describes what the branch was cut
    from -- which is the whole point of `parent_task_ids`.

    ISOLATION IS NOT INDEPENDENCE. Every card branching from origin/master is
    right for unrelated work and wrong for work that builds on work: the
    second card of a pair opens a tree with no trace of the first, so it
    re-derives, re-invents, or re-implements what already exists on the
    sibling branch -- and then the two conflict at land time. When parent
    cards are named, this cuts from a PARENT'S branch instead, so the child
    starts from the parents' committed results.

    MULTIPLE PARENTS are the normal case, not an edge case: the decomposer
    emits a dependency graph, and its root card lists every leaf as a parent.
    The first existing parent branch becomes the base and the rest are merged
    in. A merge that conflicts is aborted, not forced -- the tree stays on the
    clean base and the unmerged parents are named in the output so the card
    can say so. Silently dropping a parent's work is the failure this whole
    function exists to prevent, so it must never be silent.

    A parent's branch is used only if it actually exists: a parent whose own
    isolation failed, or that predates this mechanism, is skipped rather than
    failing the child. Uncommitted work in a parent's worktree is NOT
    inherited -- a branch tip is what git can hand over -- which is the honest
    boundary, and why the card body states the base it actually got."""
    branch, wt = _dh_branch(task_id), _dh_worktree(task_id)
    parent_branches = [_dh_branch(p) for p in (parent_task_ids or [])]
    # `[ -e X ] ||` matters: `ln -sfn target testData` when testData is already
    # a real tracked directory in the checkout does NOT replace it, it creates
    # testData/testData inside it. Only link names the worktree doesn't have.
    links = " && ".join(
        f"([ -e {shlex.quote(d)} ] || ln -s "
        f"{shlex.quote(DARKHELIX_REPO_PATH + '/' + d)} {shlex.quote(d)})"
        for d in DARKHELIX_SHARED
    )
    # .gitignore ignores these as `database/`, `thirdParty/`, `.venv-dev/` --
    # patterns with a trailing slash, which match a DIRECTORY. In the primary
    # checkout they are directories, so they are ignored. In a worktree they
    # are SYMLINKS, which git sees as files, so the patterns miss and all
    # three show up as untracked -- one `git add -A` away from committing
    # absolute snarf paths into the repo. `info/exclude` lives in the common
    # git dir, so writing it once covers every worktree, and it is local: it
    # is never committed and never shows in a diff. Idempotent via grep.
    # A shared dir is excluded ONLY where it is actually a symlink here.
    #
    # testData is a TRACKED directory in this repo (7 tracked files), so the
    # `[ -e ]` guard above never symlinks it -- and excluding it anyway made
    # every NEW file under testData/ silently unaddable. That is precisely
    # where a worker puts a test fixture: t_610790d9's first engine attempt
    # created its tests there, `git add -A` skipped them, and the attempt
    # failed having committed nothing (2026-08-28).
    #
    # `[ -L ]` is the honest condition: the exclusion exists to stop a SYMLINK
    # being committed, so it should apply to exactly the names that are one.
    # The agent artefacts are unconditional -- they are never legitimate.
    link_excludes = "; ".join(
        f"([ -L {shlex.quote(d)} ] && {{ grep -qxF {shlex.quote(d)} \"$GITCOMMON/info/exclude\" 2>/dev/null || "
        f"echo {shlex.quote(d)} >> \"$GITCOMMON/info/exclude\"; }} || true)"
        for d in DARKHELIX_SHARED
    )
    artifact_excludes = "; ".join(
        f"grep -qxF {shlex.quote(d)} \"$GITCOMMON/info/exclude\" 2>/dev/null || "
        f"echo {shlex.quote(d)} >> \"$GITCOMMON/info/exclude\""
        for d in DARKHELIX_AGENT_ARTIFACTS
    )
    excludes = f"{link_excludes}; {artifact_excludes}"
    default_base = f"origin/{DARKHELIX_BASE_BRANCH}"
    # Resolve the base on snarf, not here: only the repo knows which parent
    # branches exist. It echoes the outcome back on BASE=/MERGED=/UNMERGED=
    # lines so the card can state what it was actually cut from rather than
    # what was asked for.
    quoted_parents = " ".join(shlex.quote(b) for b in parent_branches)
    pick_base = (
        f"BASE={shlex.quote(default_base)}; MERGED=''; UNMERGED=''; MISSING=''; "
        + (f"for PB in {quoted_parents}; do "
           f'  if ! git rev-parse --verify --quiet "$PB" >/dev/null; then '
           f'    MISSING="$MISSING $PB"; continue; fi; '
           f'  if [ "$BASE" = {shlex.quote(default_base)} ]; then BASE="$PB"; '
           f'  else MERGE_LIST="$MERGE_LIST $PB"; fi; '
           f"done; " if parent_branches else "")
        + 'echo "BASE=$BASE"; echo "MISSING=$MISSING"; '
    )
    # Merges happen inside the new worktree, after it exists. `|| true` on the
    # loop keeps `set -e` from killing the whole provisioning on a conflict --
    # a conflicted merge is a reportable outcome, not a failed provision.
    merge_extra = (
        'for PB in $MERGE_LIST; do '
        '  if git merge --no-edit -q "$PB" >/dev/null 2>&1; then MERGED="$MERGED $PB"; '
        '  else git merge --abort >/dev/null 2>&1 || true; UNMERGED="$UNMERGED $PB"; fi; '
        'done; echo "MERGED=$MERGED"; echo "UNMERGED=$UNMERGED"; '
    ) if parent_branches else 'echo "MERGED="; echo "UNMERGED="; '
    cmd = (
        f"if [ -d {shlex.quote(wt)} ]; then echo 'worktree already present'; "
        f'echo "BASE=$(cd {shlex.quote(wt)} && git rev-parse --abbrev-ref HEAD)"; '
        f'echo "MISSING="; echo "MERGED="; echo "UNMERGED="; exit 0; fi; '
        f"set -e; mkdir -p {shlex.quote(_dh_task_dir(task_id))}; "
        f"cd {shlex.quote(DARKHELIX_REPO_PATH)}; git fetch origin --quiet; "
        f"MERGE_LIST=''; MISSING=''; {pick_base}"
        f'git worktree add {shlex.quote(wt)} -b {shlex.quote(branch)} "$BASE"; '
        f"cd {shlex.quote(wt)}; {links}; "
        f'GITCOMMON="$(git rev-parse --git-common-dir)"; mkdir -p "$GITCOMMON/info"; '
        f"{excludes}; {merge_extra}"
    )
    try:
        rc, out = await _fleet_ssh("snarf", cmd + " 2>&1")
    except Exception as exc:
        return {"ok": False, "output": str(exc), "base": default_base,
                "merged": [], "unmerged": [], "missing": []}

    text = out.strip()

    def _line(prefix: str) -> str:
        return next((ln[len(prefix):].strip() for ln in text.splitlines()
                     if ln.startswith(prefix)), "")

    base = _line("BASE=") or default_base
    merged = _line("MERGED=").split()
    unmerged = _line("UNMERGED=").split()
    missing = _line("MISSING=").split()
    # Structured, not a packed display string. What lands on the card is a
    # claim about which parents' work is really in the tree, and a caller that
    # has to substring-match "CONFLICTED" out of a base ref will eventually
    # get that claim wrong.
    return {
        "ok": rc == 0,
        "output": text,
        "base": base,
        "merged": merged,
        "unmerged": unmerged,
        "missing": missing,
    }


# ------------------------------------------------- provisioning, one path
# Isolation used to be created in exactly one place: /api/kanban/create, the
# SUBMIT WORK button. Every other way a card reaches this board -- the
# auto-decomposer fanning a triage card into a dependency graph, `hermes
# kanban create` by hand, the dashboard, a swarm -- produced a card with no
# worktree and no dispatch-target, and the worker then edited the ONE shared
# /ssdpool/DARKHELIX checkout. That is not a decomposer bug; it is a bug in
# provisioning only one entry point out of several.
#
# So provisioning lives HERE, callable, and there are two callers: the create
# endpoint (at file time) and /api/kanban/provision, which the Hermes
# `kanban_task_claimed` plugin hook calls in the dispatcher immediately before
# the worker subprocess spawns. Claim time is the choke point EVERY card
# passes through no matter who created it, which is what makes this closed
# rather than one more special case.

_HUD_CREATOR = "looking-glass"


def _darkhelix_assignee() -> str:
    """Profile that DARKHELIX cards are assigned to.

    This filed cards with no --assignee, so they landed on `default` -- and
    `default` has no `execution-engine-dispatch` skill (it lives under
    profiles/coder/skills/) and no `darkhelix` toolset, so the worker got
    neither the dispatch tool nor the instruction to use its worktree. It then
    did what an unguided worker does: `cd /home/sam/code/projects/DARKHELIX`,
    the SHARED checkout, while a perfectly good isolated worktree sat unused
    (t_9116c28b, 2026-08-28).

    Provisioning a worktree and then handing the card to a profile that cannot
    be told about it is worse than not provisioning at all, because it looks
    correct from the board."""
    return ((CFG.get("darkhelix") or {}).get("assignee") or "coder").strip()


def _darkhelix_forced_assignees() -> set[str]:
    """Assignees whose cards are DARKHELIX work by declaration.

    The lineage walk below is deliberately conservative, and conservative
    means it misses things: a card filed straight onto the board by an agent
    or by hand has no link to a HUD-filed card, so it is skipped even when it
    is plainly DARKHELIX work (`t_d230ec7d`, "Scope the non-swiss-prot threats
    gap", was exactly this). That case fails safe rather than silently -- the
    worker's skill blocks a card with no dispatch-target -- but "fails safe"
    is not the same as "handled", so this is the explicit way to say so.

    Kept out of the inferred path on purpose: it is a declaration in
    server.yaml, not a guess about a title."""
    cfg = CFG.get("darkhelix") or {}
    return {a for a in (cfg.get("provision_assignees") or []) if a}


async def _kanban_task_detail(task_id: str) -> dict:
    return await _kanban_api_get(f"/api/plugins/kanban/tasks/{quote(task_id)}")


async def _darkhelix_lineage(task_id: str, detail: dict) -> tuple[bool, list[str]]:
    """(is DARKHELIX work, immediate parent ids) for one card.

    SUBMIT WORK files DARKHELIX TODO.md items and nothing else, so any card
    connected to one is DARKHELIX work. Connectivity is walked in BOTH
    directions because the decomposer inverts the direction you would expect:
    it keeps the original card alive and makes it DEPEND ON every leaf it
    produced, so from a decomposed child the HUD's own card is reached through
    `children`, not `parents`.

    A card whose component contains no HUD-filed card is left completely
    alone. This board is DARKHELIX's today, but provisioning a worktree in
    someone else's repo because a heuristic said "probably" is a worse failure
    than not provisioning at all -- and the worker-side guard (SKILL.md step 1
    blocks a card with no dispatch-target) means "left alone" fails loudly
    rather than silently corrupting a shared checkout."""
    task = detail.get("task") or {}
    parents = list((detail.get("links") or {}).get("parents") or [])
    if task.get("created_by") == _HUD_CREATOR:
        return True, parents
    if (task.get("assignee") or "") in _darkhelix_forced_assignees():
        return True, parents

    seen: set[str] = {task_id}
    frontier = list(parents) + list((detail.get("links") or {}).get("children") or [])
    # Bounded: a decomposition graph is tens of cards, but this must not be
    # able to walk a pathological board forever inside a dispatcher hook.
    for _ in range(200):
        if not frontier:
            break
        nxt = frontier.pop(0)
        if nxt in seen:
            continue
        seen.add(nxt)
        try:
            d = await _kanban_task_detail(nxt)
        except Exception:
            continue
        if (d.get("task") or {}).get("created_by") == _HUD_CREATOR:
            return True, parents
        links = d.get("links") or {}
        frontier.extend(links.get("parents") or [])
        frontier.extend(links.get("children") or [])
    return False, parents


# The decomposer writes card bodies with an LLM, and it copies machinery out
# of the parent it was given. That is how `wt/the-synthetic-pcr-gene-panel-is-
# fabricat-1787435482` -- a branch name from the first, superseded HUD create
# path, which nothing ever created -- ended up as an instruction in four child
# bodies, and then as the engine's actual `--branch-name` on 2026-08-28. The
# run failed and deleted the branch, so the work was lost.
#
# Branches and paths are provisioning's to decide, never the card text's. This
# neutralises that one provably-dead shape and nothing else: `wt/<slug>-<10-
# digit epoch>` is the exact convention the old path emitted, no live
# mechanism produces it, and a real sentence is not going to contain one. A
# broader "strip anything branch-shaped" rule would start editing Sam's own
# task text, which is a worse failure than the one it prevents.
_DEAD_WT_BRANCH_RE = re.compile(r"\bwt/[a-z0-9][a-z0-9-]*-\d{10}\b")


def _sanitize_card_body(body: str, task_id: str) -> tuple[str, int]:
    """Return (body, count) with dead branch instructions defused."""
    replacement = (f"(REMOVED: a dead branch name was here. The branch for this "
                   f"card is {_dh_branch(task_id)}, named in the dispatch-target "
                   f"above -- use that and nothing else.)")
    new_body, n = _DEAD_WT_BRANCH_RE.subn(replacement, body or "")
    return new_body, n


def _dispatch_target_note(task_id: str, wt: dict,
                          parent_task_ids: list[str]) -> str:
    """The block the worker reads.

    The lineage sentence is a factual claim about which parents' work is in
    the tree, so it is built from what git actually did, not from what was
    asked for. Naming a parent whose branch did not exist as "already in your
    tree" would be the same class of lie as the `wt/...` branch that started
    all of this -- a card asserting a state of the world nothing had created.
    """
    base = wt.get("base") or f"origin/{DARKHELIX_BASE_BRANCH}"
    merged = wt.get("merged") or []
    unmerged = wt.get("unmerged") or []
    missing = wt.get("missing") or []

    def _tasks_for(branches: list[str]) -> list[str]:
        prefix = _dh_branch("")
        return [b[len(prefix):] for b in branches if b.startswith(prefix)]

    # Whose work is genuinely present: the base branch's card, plus anything
    # merged in cleanly.
    present = _tasks_for([base] + merged) if base.startswith(_dh_branch("")) else _tasks_for(merged)
    absent_conflict = _tasks_for(unmerged)
    absent_nobranch = _tasks_for(missing)

    chained = ("builds-on: " + ", ".join(parent_task_ids) + "\n") if parent_task_ids else ""

    lineage = ""
    if present:
        lineage += (
            f"IN YOUR TREE: the committed work of {', '.join(present)} is already\n"
            f"here (this branch was cut from {base}"
            + (" and merged " + ", ".join(merged) if merged else "")
            + ").\nBuild on it; do not redo it.\n\n")
    if absent_nobranch:
        lineage += (
            f"NOT IN YOUR TREE: {', '.join(absent_nobranch)} — named as parents but\n"
            "they have no branch, so nothing of theirs was inherited. If you need\n"
            "their output, find it before assuming it is missing; do not rebuild\n"
            "it blindly.\n\n")
    if absent_conflict:
        lineage += (
            f"NOT IN YOUR TREE: {', '.join(absent_conflict)} — their branches\n"
            "CONFLICTED with this base and the merge was aborted. Reconcile them\n"
            "by hand; do not assume their work is present.\n\n")
    if parent_task_ids and not present:
        lineage += (
            f"This card was cut from {base}, not from any parent.\n\n")

    return (
        "[dispatch-target]\n"
        f"repo: {DARKHELIX_REPO_PATH}\n"
        f"worktree: {_dh_worktree(task_id)}\n"
        f"branch: {_dh_branch(task_id)}\n"
        f"base: {base}\n"
        f"{chained}"
        f"{_DT_NOTES_SEP}\n"
        "THIS WORKTREE IS ON SNARF (192.168.1.239), NOT on the host you are\n"
        "running on. You are a kanban worker on CT111, which has no /ssdpool at\n"
        "all — a local ls or grep of the path above will simply report that it\n"
        "does not exist. That is expected and is NOT a broken card.\n\n"
        "You do not need to reach it yourself: dispatch_to_engine operates on\n"
        "it for you, on snarf. Call that. Only if you need to READ something\n"
        "first, go over ssh with ~/.hermes/profiles/coder/snarf_key as sam.\n\n"
        "It is a real git worktree on its own branch off\n"
        f"{base}, with database/, thirdParty/, testData/ and .venv-dev/\n"
        "symlinked in. Do NOT edit /ssdpool/DARKHELIX — and note that on snarf\n"
        "/home/sam/code/projects/DARKHELIX is the SAME checkout by another\n"
        "path, so editing there corrupts every other card's view of the tree.\n\n"
        "ADDING REFERENCE DATA: the shared pool (database/, including\n"
        "collab_refs/) is mounted READ-ONLY in the engine container, so you\n"
        "cannot write to it and must not try to work around that. If this card\n"
        "genuinely needs to ADD a reference file, write it to\n"
        f"{_dh_staging_dir(task_id)}/\n"
        "instead (the engine mounts that read-write and exports it as\n"
        "$POOL_STAGING) and SAY SO in your summary, naming the files. A human\n"
        "promotes them into the pool as a separate reviewed step. That is the\n"
        "sanctioned route — do not edit the pool by hand outside the container.\n\n"
        f"{lineage}"
        "[/dispatch-target]\n\n"
    )


def _isolation_failed_note(task_id: str, detail: str) -> str:
    """Written when provisioning fails, so the card says so in the same place
    a working card says the opposite.

    The claim hook cannot veto a dispatch (kanban lifecycle hooks are
    observers; return values are ignored), so this marker IS the stop: the
    worker's skill refuses a card whose dispatch-target has no usable
    worktree. Leaving the block off entirely would be indistinguishable from
    a card nobody tried to provision."""
    return (
        "[dispatch-target]\n"
        f"repo: {DARKHELIX_REPO_PATH}\n"
        "worktree: NONE — ISOLATION FAILED\n"
        f"error: {detail[:300]}\n"
        f"{_DT_NOTES_SEP}\n"
        "This card has NO isolated worktree. Do not work it: anything written\n"
        "to the repo would land in the shared master checkout. Block it.\n"
        "[/dispatch-target]\n\n"
    )


async def _darkhelix_worktree_exists(task_id: str) -> bool:
    """Is the card's worktree actually on disk, right now?

    The card body is a CLAIM about the world, not the world. A body can name
    a worktree that has since been removed (cleaned up by hand, a rebuilt
    box, a `git worktree prune`), and trusting the claim would hand a worker
    a path that is not there -- which is how it ends up working in the shared
    checkout instead. Verified, not assumed."""
    wt = _dh_worktree(task_id)
    try:
        rc, _ = await _fleet_ssh("snarf", f"test -d {shlex.quote(wt)}/.git "
                                          f"-o -f {shlex.quote(wt)}/.git")
    except Exception:
        return False
    return rc == 0


async def _darkhelix_provision(task_id: str,
                               parent_task_ids: list[str] | None = None,
                               body: str | None = None,
                               dry_run: bool = False) -> dict:
    """Give one card a worktree and a dispatch-target. Idempotent.

    `parent_task_ids`/`body` let the create path pass what it already knows;
    otherwise both are read from the board.

    `dry_run` reports the decision -- in scope or not, parents, whether it is
    already provisioned -- and creates nothing. The scope rule is a graph walk
    over an LLM-generated dependency graph, so being able to ask "what would
    you do with this card" without side effects is how it stays auditable
    across the whole board."""
    if not _TASK_ID_RE.match(task_id):
        return {"ok": False, "error": "bad task id"}

    detail: dict = {}
    if body is None or parent_task_ids is None:
        try:
            detail = await _kanban_task_detail(task_id)
        except Exception as exc:
            return {"ok": False, "error": f"board unreadable: {exc}"}
        if body is None:
            body = (detail.get("task") or {}).get("body") or ""

    if parent_task_ids is None:
        in_scope, parents = await _darkhelix_lineage(task_id, detail)
        if not in_scope:
            return {"ok": True, "skipped": "not a DARKHELIX card",
                    **({"dry_run": True, "would_provision": False} if dry_run else {})}
        parent_task_ids = parents

    # Already provisioned by the current mechanism -- and only the current
    # one; the superseded `branch: wt/...` shape deliberately fails this.
    # Both halves must hold: the body has to name the current worktree AND
    # that worktree has to exist. Either alone is a card that lies.
    current = _dispatch_target_is_current(body, task_id)
    exists = await _darkhelix_worktree_exists(task_id)
    if current and exists:
        return {"ok": True, "already": True,
                "worktree": _dh_worktree(task_id), "branch": _dh_branch(task_id)}

    if dry_run:
        return {"ok": True, "dry_run": True, "would_provision": True,
                "parents": parent_task_ids,
                "body_claims_current": current, "worktree_exists": exists,
                "worktree": _dh_worktree(task_id), "branch": _dh_branch(task_id)}

    wt = await _darkhelix_worktree_create(task_id, parent_task_ids)
    isolated = bool(wt.get("ok"))
    iso_out = wt.get("output") or ""

    note = (_dispatch_target_note(task_id, wt, parent_task_ids) if isolated
            else _isolation_failed_note(task_id, iso_out))
    # Replace a stale block rather than stacking a second one on top of it:
    # two dispatch-targets in one body is precisely the ambiguity that had a
    # worker chasing a `wt/...` branch nothing had created.
    stripped = _DISPATCH_TARGET_RE.sub("", body or "").lstrip("\n")
    stripped, defused = _sanitize_card_body(stripped, task_id)
    try:
        await asyncio.to_thread(
            _kanban_api_call, "PATCH",
            f"/api/plugins/kanban/tasks/{quote(task_id)}",
            json={"body": note + stripped})
    except Exception as exc:
        return {"ok": False, "isolated": isolated,
                "error": f"worktree {'created' if isolated else 'failed'}; "
                         f"card body not updated: {exc}"}

    return {"ok": isolated, "isolated": isolated,
            "dead_branch_refs_defused": defused,
            "worktree": _dh_worktree(task_id) if isolated else None,
            "branch": _dh_branch(task_id) if isolated else None,
            "base": wt.get("base") if isolated else None,
            "merged": wt.get("merged") or [],
            "unmerged": wt.get("unmerged") or [],
            "missing": wt.get("missing") or [],
            "parents": parent_task_ids,
            "detail": iso_out[-400:]}


@app.post("/api/kanban/provision")
async def kanban_provision(request: Request) -> JSONResponse:
    """Provision isolation for a card this server did not file.

    Called by the `darkhelix-isolation` Hermes plugin from the
    `kanban_task_claimed` hook on CT111 -- in the dispatcher process, after
    the claim commits and before the worker subprocess spawns, so the worker
    reads a body that already names its worktree.

    Idempotent and safe to call on anything: a card outside DARKHELIX's
    lineage is skipped, and a card already carrying a current dispatch-target
    is a no-op."""
    payload = await request.json()
    task_id = (payload.get("task_id") or "").strip()
    result = await _darkhelix_provision(
        task_id, dry_run=bool(payload.get("dry_run")))
    status = 200 if result.get("ok") else 502
    if result.get("error") == "bad task id":
        status = 400
    return JSONResponse(result, status_code=status)


async def _kanban_block(task_id: str, reason: str, kind: str = "needs_input") -> str:
    """Put a card back on the board with the reason attached.

    Returns what actually happened, because two different things can:

      "blocked"   -- status is now blocked; it shows in the blocked lane.
      "commented" -- the reason is on the card but the status did not move.
                     `hermes kanban block` refuses some statuses (a card
                     still in triage, or already blocked) yet files the
                     reason as a comment regardless.
      "failed"    -- neither; nothing reached the card.

    Reporting a bare boolean here conflated "not blocked" with "the reason
    was lost", and losing the reason is exactly the failure this whole path
    exists to prevent."""
    try:
        await _kanban_ssh(
            f"hermes kanban block --kind {shlex.quote(kind)} "
            f"{shlex.quote(task_id)} {shlex.quote(reason[:900])}")
    except Exception:
        return "failed"
    # Judge by the card's own state, not the command's exit code.
    try:
        detail = await _kanban_api_get(f"/api/plugins/kanban/tasks/{quote(task_id)}")
    except Exception:
        return "failed"
    if (detail.get("task") or {}).get("status") == "blocked":
        return "blocked"
    marker = reason[:40]
    for c in detail.get("comments") or []:
        if marker and marker in (c.get("body") or ""):
            return "commented"
    return "failed"


# ------------------------------------------- DARKHELIX completion verification
# A worktree constrains WHERE a worker writes. Nothing constrained what it
# CLAIMS. On 2026-08-29 t_43886eea reported "Wrote collab_refs generator audit
# report (535 lines) to worktree", was marked done, and had written nothing at
# all: no branch, no commit, no file, no attachment. Its worker had failed to
# reach snarf (`ssh ... exit 255`) and reported success anyway.
#
# Completion was self-reported and never checked, so the board asserted work
# that did not exist -- and two child cards were cut from that non-existent
# work. This closes that: a card whose summary CLAIMS an artifact must be able
# to show one.
#
# WHY THIS LIVES HERE AND NOT IN A CT111 PLUGIN
# ---------------------------------------------
# The obvious design is the mirror of provisioning: a `kanban_task_completed`
# hook in `darkhelix-isolation`. That hook does exist and even carries the
# summary. It would not have fired.
#
# `kanban_task_claimed` fires in the DISPATCHER (root HERMES_HOME), where
# `darkhelix-isolation` is enabled. `kanban_task_completed` fires wherever
# `complete_task` is called -- and completion is `hermes kanban complete`,
# spawned by the WORKER, which runs with HERMES_HOME set to its own PROFILE.
# Plugins are enabled per profile: `hermes -p coder plugins list` shows only
# `darkhelix-engine`, and the `darkhelix` profile -- the assignee of the very
# card this check exists to catch -- has neither. A hook registered there
# would have silently never run, which is the failure mode this whole document
# keeps rediscovering.
#
# Cards on this board complete under coder, darkhelix, bioinformatics,
# researcher and default. Enabling one plugin across every profile, forever,
# including profiles nobody has created yet, is precisely the per-profile
# sprawl that left four identical stale memory files behind. The HUD already
# polls the board, so it is the one place every completed card passes through
# regardless of who ran it -- the same argument that moved provisioning to
# `kanban_task_claimed` in the first place, applied to the other end.
#
# Verification is detective either way: lifecycle hooks are observers whose
# return value is ignored, so even the plugin form could not have vetoed a
# `done`. Nothing is lost by checking a moment later.

# A summary only has to hold up if it CLAIMS something checkable. "Reviewed the
# fetch table and found no defect" asserts no artifact and is left alone; the
# words below are the ones that assert one. Deliberately a text heuristic: a
# structured result contract would be stricter, but it needs the worker to
# cooperate, and a worker that fabricates a summary is exactly the one that
# will not.
# The verb list started as the doc's (wrote|created|added|patch|commit|report)
# and was widened against the board: a dry-run sweep of all 21 done cards showed
# it missing real assertions. t_82a2d485 said "Documented in
# s2fast_inclusion_policy.md", t_d17fef80 said "Merged to master as 2bad11f",
# t_97cff6a5 said "gene_prediction.py now uses real contig IDs" -- every one an
# artifact claim, none of them matched. A check that does not fire is worth
# nothing, and widening is cheap here because the EVIDENCE side is generous:
# any one of three signals clears a card, and work that really happened almost
# always left a commit.
_DH_ARTIFACT_CLAIM_RE = re.compile(
    r"\b(wrote|created|added|patch|commit|committed|report|documented|"
    r"produced|merged|implemented|generated|updated|fixed|refactored)\b", re.I)

# A VERB is required, and merely naming a file is deliberately NOT a claim.
# Triggering on any summary that mentioned a real extension was tried and
# reverted: on this board it fired on three pure-analysis cards -- t_26383d0a,
# t_ca4d6f36 and t_e8465c45 all match no verb at all and simply DESCRIBE
# existing data ("Confirmed 234.fna and 29459.fna are byte-identical
# duplicates"). In a bioinformatics repo, naming a data file is how findings
# are stated; it is not an assertion that the card wrote it. Filenames still
# matter on the EVIDENCE side, which is where they belong.
# A summary that credits its children rather than itself. Only these may be
# cleared by a child's branch.
_DH_ROLLUP_CLAIM_RE = re.compile(
    r"\b(child|children|subtask|subtasks|sub-task|sub-tasks)\b", re.I)


def _dh_claims_artifact(summary: str) -> bool:
    """True when a summary asserts something that should be findable."""
    return bool(_DH_ARTIFACT_CLAIM_RE.search(summary or ""))

# Filenames named in a summary, e.g. "updated coord_liftover.py". Requires a
# real extension so prose ("3-of-9", "531/533") is not mistaken for a path.
_DH_SUMMARY_FILE_RE = re.compile(r"[\w./-]+\.[A-Za-z][A-Za-z0-9]{0,5}\b")

# Words that end a sentence, not files. `.md`/`.py` etc. are real; these are
# the false positives the extension rule alone still lets through.
_DH_FILE_STOPWORDS = {"e.g", "i.e", "etc", "vs", "no", "cf"}


def _dh_summary_files(summary: str) -> list[str]:
    """Filenames a summary names, most specific first."""
    out: list[str] = []
    for tok in _DH_SUMMARY_FILE_RE.findall(summary or ""):
        tok = tok.strip(".,;:)(").lstrip("`")
        if not tok or tok.lower() in _DH_FILE_STOPWORDS:
            continue
        # A bare number with a decimal point ("533.2") is not a file.
        if tok.replace(".", "").isdigit():
            continue
        if tok not in out:
            out.append(tok)
    return out[:8]


async def _dh_has_attachment(task_id: str) -> bool:
    """True when the card carries at least one attachment.

    Read over ssh rather than from the task detail's `events`: an attachment
    shows there as an `attached` event, but a long run buries those under
    dozens of heartbeats and the event list is not guaranteed complete.
    `hermes kanban attachments` is the authoritative answer and is one cheap
    call."""
    try:
        rc, out = await _kanban_ssh(f"hermes kanban attachments {shlex.quote(task_id)}")
    except Exception:
        return False
    if rc != 0:
        return False
    return "No attachments on" not in (out or "")


async def _darkhelix_verify_completion(task_id: str, dry_run: bool = False) -> dict:
    """Check that a card marked `done` can show the artifact it claims.

    Returns a verdict dict; `ok` is about the CHECK having run, not about the
    card passing. `verdict` is one of:

      "verified"    -- the claim is backed by at least one piece of evidence
      "no-claim"    -- the summary asserts no artifact; nothing to check
      "unverified"  -- it claims an artifact and none of the evidence holds

    Evidence is anything that proves work exists, in increasing cost:
      1. the card's branch has commits master does not have;
      2. the card has an attachment (the engine attaches its patch);
      3. a file the summary names by hand exists in the card's worktree.

    Any ONE is enough. The check is asymmetric on purpose -- it is trying to
    disprove "this card did nothing", not to audit the work's quality, which
    is what the test gate and human review are for."""
    if not _TASK_ID_RE.match(task_id or ""):
        return {"ok": False, "error": "bad task id"}

    try:
        detail = await _kanban_task_detail(task_id)
    except Exception as exc:
        return {"ok": False, "error": f"task lookup failed: {exc}"}

    task = detail.get("task") or {}
    if (task.get("status") or "") != "done":
        return {"ok": True, "verdict": "skipped",
                "why": f"status is {task.get('status')!r}, not done"}

    # Same scope rule as provisioning: judging a card in someone else's repo
    # by DARKHELIX's evidence would be worse than not judging it.
    try:
        in_scope, _parents = await _darkhelix_lineage(task_id, detail)
    except Exception as exc:
        return {"ok": False, "error": f"lineage walk failed: {exc}"}
    if not in_scope:
        return {"ok": True, "verdict": "skipped", "why": "not DARKHELIX work"}

    summary = (task.get("latest_summary") or task.get("result") or "").strip()
    if not summary:
        return {"ok": True, "verdict": "skipped", "why": "no summary to check"}
    if not _dh_claims_artifact(summary):
        return {"ok": True, "verdict": "no-claim", "summary": summary[:200]}

    fields = _dispatch_target_fields(task.get("body") or "")
    branch = fields.get("branch") or _dh_branch(task_id)
    worktree = fields.get("worktree") or _dh_worktree(task_id)
    repo = fields.get("repo") or DARKHELIX_REPO_PATH
    # A card whose provisioning failed carries "NONE -- ISOLATION FAILED".
    if not worktree.startswith("/"):
        worktree = ""

    evidence: list[str] = []
    checked: list[str] = []

    # 1. Commits on the card's branch that master does not already have.
    #    Counted against master rather than the card's base so that
    #    inheriting a parent's branch cannot itself look like new work.
    rc, out = await _fleet_ssh(
        "snarf",
        f"cd {shlex.quote(repo)} && "
        f"git rev-parse --verify --quiet {shlex.quote(branch)} >/dev/null && "
        f"git rev-list --count master..{shlex.quote(branch)} || echo MISSING")
    commits = (out or "").strip().splitlines()[-1] if out else ""
    if rc == 0 and commits.isdigit() and int(commits) > 0:
        evidence.append(f"branch {branch} has {commits} commit(s) not in master")
    checked.append(f"branch {branch}: "
                   + ("absent" if commits == "MISSING" else f"{commits or '?'} commits"))

    # 2. An attachment -- how dispatch_to_engine delivers its patch.
    if not evidence:
        if await _dh_has_attachment(task_id):
            evidence.append("card has an attachment")
        checked.append("attachments: none")

    # 3. A CHILD's branch. A rollup card legitimately claims work it did not
    #    commit itself: t_97cff6a5's summary is "All 4 child tasks completed
    #    successfully. The pyrodigal GFF seqid inconsistency has been fixed",
    #    and every one of those commits is on a child's branch. Judging a
    #    parent only by its own branch marks the decomposer's own bookkeeping
    #    card as a fabrication.
    # ...but ONLY for a summary that actually claims its children's work.
    # Without this gate the check clears the very card it was built to catch:
    # t_43886eea has a child that really did commit, so "any child has
    # commits" marked the fabricated "Wrote ... report (535 lines) to
    # worktree" as verified. A first-person claim about THIS card's worktree
    # is not substantiated by a different card's branch. A rollup says so in
    # words -- "All 4 child tasks completed successfully" -- and that is the
    # only shape allowed to borrow its children's evidence.
    children = list((detail.get("links") or {}).get("children") or [])
    if not evidence and children and not _DH_ROLLUP_CLAIM_RE.search(summary):
        checked.append(f"child branches ({len(children)}): not a rollup claim, "
                       "children cannot vouch for this summary")
        children = []
    if not evidence and children:
        for child in children[:8]:
            if not _TASK_ID_RE.match(child or ""):
                continue
            rc, out = await _fleet_ssh(
                "snarf",
                f"cd {shlex.quote(repo)} && "
                f"git rev-list --count master..{shlex.quote(_dh_branch(child))} "
                f"2>/dev/null || echo 0")
            n = (out or "").strip().splitlines()[-1] if out else "0"
            if rc == 0 and n.isdigit() and int(n) > 0:
                evidence.append(f"child {child} has {n} commit(s) not in master")
                break
        checked.append(f"child branches ({len(children)}): "
                       + ("none carry commits" if not evidence else "ok"))

    # 4. A file the summary names, present in the card's worktree. Last
    #    because it costs an ssh round trip and is the weakest signal: an
    #    uncommitted file proves work happened, not that it survived.
    named = _dh_summary_files(summary)
    if not evidence and named and worktree:
        quoted = " ".join(shlex.quote(f"{worktree}/{n}") for n in named)
        rc, out = await _fleet_ssh("snarf", f"ls -1d {quoted} 2>/dev/null | head -5")
        found = [ln for ln in (out or "").splitlines() if ln.strip()]
        if rc == 0 and found:
            evidence.append(f"summary names a file that exists: {found[0]}")
        checked.append(f"named files {named}: "
                       + (", ".join(found) if found else "none present"))
    elif not evidence:
        checked.append("named files: summary names none")

    if evidence:
        return {"ok": True, "verdict": "verified", "evidence": evidence,
                "checked": checked, "summary": summary[:200]}

    reason = (
        "Completion not verified. This card was marked done with a summary "
        "claiming an artifact, but none could be found.\n\n"
        f"Summary: {summary[:300]}\n\n"
        "Checked:\n  - " + "\n  - ".join(checked) + "\n\n"
        "No commit on the card's branch, no attachment, and no named file on "
        "disk. Either the work was not saved where the board can see it, or "
        "the summary is wrong. Re-open with the evidence, or correct the "
        "summary and complete again."
    )
    result = {"ok": True, "verdict": "unverified", "checked": checked,
              "reason": reason, "summary": summary[:200]}
    if dry_run:
        result["dry_run"] = True
        return result
    # Observers cannot veto a `done`, so the card is moved instead: it lands
    # in the blocked lane carrying why. Never crash the worker and never
    # silently pass -- the point is that the board stops lying.
    result["action"] = await _kanban_block(task_id, reason, kind="needs_input")
    return result


@app.post("/api/kanban/verify-completion")
async def kanban_verify_completion(request: Request) -> JSONResponse:
    """Check one completed card's claim against the tree.

    `dry_run: true` reports the verdict and changes nothing, which is how the
    whole board can be audited without moving a card."""
    payload = await request.json()
    task_id = (payload.get("task_id") or "").strip()
    result = await _darkhelix_verify_completion(
        task_id, dry_run=bool(payload.get("dry_run")))
    status = 200 if result.get("ok") else 502
    if result.get("error") == "bad task id":
        status = 400
    return JSONResponse(result, status_code=status)


# ---------------------------------------------- automatic completion sweep
# The check above is only worth anything if something RUNS it. A per-card
# endpoint means the board keeps lying until a human thinks to ask, which is
# the same shape as the failure it exists to catch.
#
# This is a server-side poller, NOT a side effect of GET /api/kanban. That
# endpoint is read by every open HUD tab several times a minute, so hanging
# the sweep off it would fire it concurrently once per tab, put ssh round
# trips in the path of a page render, and make how often a card gets judged
# depend on how many browsers happen to be open. One loop owned by the
# server is the "board poll" this was always meant to be, and it matches the
# activity-feed pollers registered beside it.
#
# SEEDING -- why the first pass deliberately checks nothing.
# The backlog of `done` cards was already swept by hand on 2026-08-30 (14
# verified, 4 no-claim, 3 flagged) and those flags were ADJUDICATED in
# docs/PIPELINE-VERIFICATION.md: t_97cff6a5's work really is in master,
# which `master..hermes/<id>` structurally cannot see. Blocking that card
# automatically would be wrong, and without a seed it would happen again on
# every single restart. So the first pass records the ids it finds and
# judges none of them.
#
# The seen-set is PERSISTED for that same reason, and persistence closes the
# opposite hole for free: a card completed while the HUD was down is absent
# from the file, so it is checked on the next boot instead of being seeded
# past. Losing the file fails in the safe direction -- it re-seeds, skipping
# cards rather than mass-blocking them.
VERIFY_STATE_PATH = ROOT / "logs" / "verify_completions.json"

# Comfortably longer than the board's `done` lane, bounded so the file cannot
# grow forever. Oldest ids drop first; anything aging out is long archived.
_DH_VERIFY_SEEN_MAX = 500

_DH_VERIFY_STATE: dict = {"seeded": False, "seen": []}
_DH_VERIFY_STATUS: dict = {
    "enabled": False, "seeded": False, "last_tick": None, "last_error": None,
    "note": None, "checked": 0, "recent": [],
}


def _dh_verify_cfg() -> dict:
    return CFG.get("darkhelix") or {}


def _dh_verify_poll_seconds() -> int:
    return int(_dh_verify_cfg().get("verify_poll_seconds") or 120)


def _dh_verify_load_state() -> None:
    global _DH_VERIFY_STATE
    try:
        data = json.loads(VERIFY_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    if isinstance(data, dict) and isinstance(data.get("seen"), list):
        _DH_VERIFY_STATE = {
            "seeded": bool(data.get("seeded")),
            "seen": [str(i) for i in data["seen"]][-_DH_VERIFY_SEEN_MAX:],
        }


def _dh_verify_save_state() -> None:
    try:
        VERIFY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        VERIFY_STATE_PATH.write_text(json.dumps(_DH_VERIFY_STATE), encoding="utf-8")
    except Exception:
        # A sweep that cannot persist still verifies correctly this run; it
        # would only re-seed after a restart. Not worth stopping for.
        pass


def _dh_verify_mark_seen(task_id: str) -> None:
    seen = _DH_VERIFY_STATE["seen"]
    if task_id in seen:
        return
    seen.append(task_id)
    del seen[:-_DH_VERIFY_SEEN_MAX]


def _dh_verify_record(entry: dict) -> None:
    entry["at"] = time.time()
    _DH_VERIFY_STATUS["recent"] = ([entry] + _DH_VERIFY_STATUS["recent"])[:20]


async def _dh_done_task_ids() -> list[str]:
    """Ids the board currently shows as `done`, most recently finished first.

    Read straight from the plugin API rather than through /api/kanban so the
    sweep does not depend on that endpoint's slimming, and newest-first so a
    burst of completions is judged in the order it happened."""
    board = await _kanban_api_get("/api/plugins/kanban/board")
    rows = [t for c in (board.get("columns") or []) for t in (c.get("tasks") or [])]
    rows.sort(key=lambda t: t.get("completed_at") or t.get("created_at") or 0,
              reverse=True)
    return [t["id"] for t in rows
            if t.get("id") and (t.get("status") or "") == "done"]


async def _verify_completions_tick() -> None:
    done = await _dh_done_task_ids()

    if not _DH_VERIFY_STATE["seeded"]:
        for task_id in done:
            _dh_verify_mark_seen(task_id)
        _DH_VERIFY_STATE["seeded"] = True
        _dh_verify_save_state()
        _DH_VERIFY_STATUS["seeded"] = True
        _DH_VERIFY_STATUS["note"] = (
            f"seeded {len(done)} already-done card(s) without judging them "
            "(that backlog was swept and adjudicated by hand); verification "
            "applies to completions from here on")
        return

    seen = set(_DH_VERIFY_STATE["seen"])
    fresh = [t for t in done if t not in seen]
    if not fresh:
        return

    # Bounded per tick: each card costs a board read plus up to four ssh round
    # trips to snarf, and a decomposer fan-out can land a dozen at once. The
    # rest are picked up on the following ticks -- nothing is dropped, because
    # a card is only marked seen once it has actually been judged.
    limit = int(_dh_verify_cfg().get("verify_max_per_tick") or 5)
    for task_id in fresh[:limit]:
        try:
            result = await _darkhelix_verify_completion(task_id)
        except Exception as exc:
            _dh_verify_record({"task_id": task_id, "verdict": "error",
                               "error": str(exc)[:300]})
            continue
        if not result.get("ok"):
            # snarf or the board was unreachable. That is an outage, not a
            # verdict -- leave the card unseen so the next tick retries it
            # instead of recording the outage as a pass.
            _dh_verify_record({"task_id": task_id, "verdict": "error",
                               "error": str(result.get("error"))[:300]})
            continue
        _dh_verify_mark_seen(task_id)
        _DH_VERIFY_STATUS["checked"] += 1
        entry = {"task_id": task_id, "verdict": result.get("verdict")}
        if result.get("verdict") == "unverified":
            # `action` is _kanban_block's own report of what reached the card
            # -- blocked, commented, or failed -- not an assumption that the
            # move landed.
            entry["action"] = result.get("action")
            entry["checked_for"] = result.get("checked")
        _dh_verify_record(entry)
    _dh_verify_save_state()


async def _verify_completions_forever() -> None:
    """Judge each newly-completed DARKHELIX card once, in the background.

    Off unless `darkhelix.verify_completions` is true: this moves cards on a
    shared board, so it is opt-in per deployment rather than on by default."""
    if not _dh_verify_cfg().get("verify_completions"):
        return
    _dh_verify_load_state()
    _DH_VERIFY_STATUS["enabled"] = True
    _DH_VERIFY_STATUS["seeded"] = _DH_VERIFY_STATE["seeded"]
    while True:
        try:
            await _verify_completions_tick()
            _DH_VERIFY_STATUS["last_error"] = None
        except Exception as exc:
            # The dashboard being down must not kill the loop -- same
            # swallow-and-retry contract as the pollers beside it.
            _DH_VERIFY_STATUS["last_error"] = str(exc)[:300]
        _DH_VERIFY_STATUS["last_tick"] = time.time()
        await asyncio.sleep(_dh_verify_poll_seconds())


@app.get("/api/kanban/verify-completion")
async def kanban_verify_completion_status() -> JSONResponse:
    """What the automatic sweep has done, and whether it is running at all.

    A detective control nobody can see the state of is one nobody trusts:
    this says whether the loop is enabled, whether it has seeded, when it
    last ticked, and the last 20 verdicts with what happened to each card."""
    return JSONResponse({
        **_DH_VERIFY_STATUS,
        "seen_count": len(_DH_VERIFY_STATE["seen"]),
        "poll_seconds": _dh_verify_poll_seconds(),
    })



# --------------------------------------------- shared-pool manifest logging
# Database policy option 3 from docs/PIPELINE-VERIFICATION.md.
#
# `database/` is gitignored and symlinked into every worktree from the one
# copy at /ssdpool/DARKHELIX. A worker mutating it leaves NO diff, no review
# and nothing to revert: when t_d17fef80 replaced 263.fna and deleted 234.fna,
# the only reason anyone found out was a human computing md5s days later.
# Code isolation is solved; data isolation is not, and copying cannot solve it.
#
# This is the detective half: hash the pool at every run boundary and attribute
# the delta to whoever was running. It is deliberately NOT preventive -- a
# read-only pool (option 1) without item C makes things worse, and we have
# direct evidence of what a worker does at a hard wall.
#
# WHY NOT THE WHOLE OF database/, AND WHY NOT A PLUGIN
# ----------------------------------------------------
# `database/` is 582G across ~154k files. Hashing that per run is not viable.
# `database/collab_refs/` is 184M across 158 files and md5s in 1.9s, and it is
# both what the doc names and what actually got damaged. Measured on snarf
# 2026-08-30. More paths can be added via config; the cost is linear and the
# guard below refuses an accidentally enormous one.
#
# The doc proposed putting this in `darkhelix-isolation`. It cannot live there
# whole: that plugin hooks `kanban_task_claimed`, which fires in the DISPATCHER
# and would give the BEFORE snapshot -- but the AFTER snapshot needs the run to
# end, and `kanban_task_completed` fires in the WORKER, under its own profile,
# where the plugin is not enabled. That is exactly the trap item A documented.
# Worse, a completion hook would miss every run that ends by blocking, crashing
# or being reclaimed, which is most of the interesting ones. A boundary poller
# on the HUD sees all of them.
POOL_MANIFEST_PATH = ROOT / "logs" / "pool_manifest.json"
POOL_DELTA_LOG = ROOT / "logs" / "pool_deltas.jsonl"

_DH_POOL_DEFAULT_PATHS = ("database/collab_refs",)

# A guard against a config typo pointing this at 582G. Refuse rather than
# spend an hour hashing: a manifest that never completes logs nothing.
_DH_POOL_MAX_FILES = 20000

# Enough to read; a delta of thousands of files is a catastrophe, not a diff,
# and the count still tells that story.
_DH_POOL_LIST_CAP = 40

_DH_POOL_MANIFEST: dict | None = None
_DH_POOL_STATUS: dict = {
    "enabled": False, "baseline": False, "last_tick": None, "last_error": None,
    "note": None, "snapshots": 0, "windows_clean": 0, "deltas": 0, "recent": [],
}


def _dh_pool_cfg() -> dict:
    return CFG.get("darkhelix") or {}


def _dh_pool_paths() -> list[str]:
    raw = _dh_pool_cfg().get("pool_manifest_paths") or _DH_POOL_DEFAULT_PATHS
    # Repo-relative only: an absolute path or a `..` escape would hash
    # something outside the pool this is supposed to be accounting for.
    return [p for p in (str(x).strip().strip("/") for x in raw)
            if p and not p.startswith("/") and ".." not in p.split("/")]


def _dh_pool_poll_seconds() -> int:
    return int(_dh_pool_cfg().get("pool_manifest_poll_seconds") or 120)


async def _dh_pool_snapshot() -> dict[str, str]:
    """md5 of every file under the watched paths, as {relative path: md5}.

    One ssh round trip. `sort -z` before md5sum keeps the command's output
    order stable so a diff of two snapshots is about content, not readdir
    order."""
    paths = _dh_pool_paths()
    if not paths:
        return {}
    quoted = " ".join(shlex.quote(p) for p in paths)
    rc, out = await _fleet_ssh(
        "snarf",
        f"cd {shlex.quote(DARKHELIX_REPO_PATH)} && "
        f"timeout 600 sh -c {shlex.quote(f'find {quoted} -type f -print0 | sort -z | xargs -0 -r md5sum')}")
    if rc != 0:
        raise RuntimeError(f"pool snapshot failed (rc {rc}): {(out or '')[-300:]}")
    files: dict[str, str] = {}
    for line in (out or "").splitlines():
        # md5sum prints "<32 hex>  <path>"; it prefixes the line with a
        # backslash when it had to escape the name. Those are skipped rather
        # than mis-parsed -- a filename with a newline in this pool would be
        # its own incident.
        if line.startswith("\\") or "  " not in line:
            continue
        digest, _, path = line.partition("  ")
        if len(digest) == 32:
            files[path] = digest
    if len(files) > _DH_POOL_MAX_FILES:
        raise RuntimeError(
            f"pool manifest refuses {len(files)} files (cap {_DH_POOL_MAX_FILES}); "
            f"check darkhelix.pool_manifest_paths")
    return files


def _dh_pool_diff(old: dict[str, str], new: dict[str, str]) -> dict:
    old_keys, new_keys = set(old), set(new)
    return {
        "added": sorted(new_keys - old_keys),
        "removed": sorted(old_keys - new_keys),
        "changed": sorted(p for p in (old_keys & new_keys) if old[p] != new[p]),
    }


def _dh_pool_delta_empty(delta: dict) -> bool:
    return not (delta["added"] or delta["removed"] or delta["changed"])


def _dh_pool_manifest_load() -> None:
    global _DH_POOL_MANIFEST
    try:
        data = json.loads(POOL_MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    if isinstance(data, dict) and isinstance(data.get("files"), dict):
        _DH_POOL_MANIFEST = data


def _dh_pool_manifest_save(files: dict[str, str], in_flight: list[str],
                           event_id) -> None:
    global _DH_POOL_MANIFEST
    _DH_POOL_MANIFEST = {"taken_at": time.time(), "files": files,
                         "in_flight": sorted(in_flight), "event_id": event_id}
    try:
        POOL_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        POOL_MANIFEST_PATH.write_text(json.dumps(_DH_POOL_MANIFEST), encoding="utf-8")
    except Exception:
        # Losing the file costs one re-baseline, which attributes nothing.
        # It must never take the loop down.
        pass


def _dh_pool_log_delta(record: dict) -> None:
    """Append to the durable ledger BEFORE trying to comment.

    The whole point is that a mutation stops depending on someone noticing.
    If CT111 is unreachable the comment fails, and the record still has to
    survive -- so the local log is written first and is the source of truth."""
    try:
        POOL_DELTA_LOG.parent.mkdir(parents=True, exist_ok=True)
        with POOL_DELTA_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _dh_pool_comment_body(record: dict) -> str:
    delta = record["delta"]
    candidates = record["candidates"]
    lines = [f"**Shared pool changed** while this card held the pipeline "
             f"({record['window_seconds']}s window, "
             f"{', '.join(_dh_pool_paths())}).", ""]
    for label, key in (("Added", "added"), ("Removed", "removed"),
                       ("Modified", "changed")):
        names = delta[key]
        if not names:
            continue
        shown = names[:_DH_POOL_LIST_CAP]
        lines.append(f"{label} ({len(names)}):")
        lines += [f"  - `{n}`" for n in shown]
        if len(names) > len(shown):
            lines.append(f"  - ...and {len(names) - len(shown)} more")
        lines.append("")
    if len(candidates) > 1:
        lines.append(
            f"**Attribution is ambiguous** — {len(candidates)} cards were in "
            f"flight during this window: {', '.join(candidates)}. The pool is "
            f"shared, so the change could be any of them.")
    else:
        lines.append(
            "This card was the only one running, so the change is attributed "
            "to it. `database/` is gitignored — there is no diff and no "
            "revert; this comment is the record.")
    return "\n".join(lines)


async def _dh_pool_report(record: dict) -> None:
    """File the delta on every card that could have caused it."""
    body = _dh_pool_comment_body(record)
    posted, failed = [], []
    for task_id in record["candidates"]:
        try:
            await asyncio.to_thread(
                _kanban_api_call, "POST",
                f"/api/plugins/kanban/tasks/{quote(task_id)}/comments",
                json={"author": "looking-glass", "body": body[:4000]})
            posted.append(task_id)
        except Exception as exc:
            failed.append(f"{task_id}: {exc}")
    record["commented"] = posted
    if failed:
        record["comment_errors"] = failed


async def _pool_manifest_tick() -> None:
    board = await _kanban_api_get("/api/plugins/kanban/board")
    rows = [t for c in (board.get("columns") or []) for t in (c.get("tasks") or [])]
    event_id = board.get("latest_event_id")
    running = sorted(t["id"] for t in rows
                     if t.get("id") and (t.get("status") or "") == "running")

    prev = _DH_POOL_MANIFEST
    if prev is None:
        files = await _dh_pool_snapshot()
        _dh_pool_manifest_save(files, running, event_id)
        _DH_POOL_STATUS["snapshots"] += 1
        _DH_POOL_STATUS["baseline"] = True
        _DH_POOL_STATUS["note"] = (
            f"baseline of {len(files)} file(s) taken; deltas are attributed "
            f"from here on")
        return

    # Re-hash only at a boundary. `latest_event_id` moves on ANY board
    # activity, which catches the case the running-set alone misses: a card
    # that starts and finishes entirely inside one poll interval, and a card
    # that exits by blocking (which sets no completed_at) rather than by
    # completing.
    if running == prev.get("in_flight") and event_id == prev.get("event_id"):
        return

    files = await _dh_pool_snapshot()
    _DH_POOL_STATUS["snapshots"] += 1
    delta = _dh_pool_diff(prev["files"], files)
    taken_at = prev.get("taken_at") or time.time()

    # Anyone who held the pipeline at any point in this window. `in_flight` is
    # who was running when the last manifest was taken; `completed_at` past
    # that mark catches a card that came and went inside one interval.
    candidates = set(prev.get("in_flight") or [])
    for t in rows:
        done_at = t.get("completed_at") or 0
        if t.get("id") and done_at and done_at > taken_at:
            candidates.add(t["id"])

    if _dh_pool_delta_empty(delta):
        _DH_POOL_STATUS["windows_clean"] += 1
    else:
        record = {
            "at": time.time(), "window_seconds": round(time.time() - taken_at),
            "paths": _dh_pool_paths(), "delta": delta,
            "counts": {k: len(v) for k, v in delta.items()},
            "candidates": sorted(candidates),
        }
        if not candidates:
            # No card held the pipeline, yet the shared pool moved. This is
            # the t_d17fef80 signature exactly -- a blocked worker doing the
            # work by hand at 23:59, outside the container and outside the
            # board. It is the single most interesting thing this can find,
            # so it is labelled rather than dropped for having nobody to
            # comment on.
            record["unattributed"] = True
        _dh_pool_log_delta(record)
        if candidates:
            await _dh_pool_report(record)
        _DH_POOL_STATUS["deltas"] += 1
        _DH_POOL_STATUS["recent"] = ([{
            "at": record["at"], "counts": record["counts"],
            "candidates": record["candidates"],
            "unattributed": record.get("unattributed", False),
            "commented": record.get("commented", []),
        }] + _DH_POOL_STATUS["recent"])[:20]

    _dh_pool_manifest_save(files, running, event_id)


async def _dh_pool_force_snapshot(reason: str, candidates: list[str]) -> dict:
    """Re-hash the pool NOW and attribute the delta to a named cause.

    The poller only hashes at run boundaries, which is right for workers but
    wrong for a change the HUD itself makes: a promotion happens outside any
    run, so the board may not move for hours and the delta would sit
    unrecorded until it did. An unrecorded pool change is the precise thing
    policy 3 exists to abolish, so the writer records its own."""
    files = await _dh_pool_snapshot()
    _DH_POOL_STATUS["snapshots"] += 1
    prev = _DH_POOL_MANIFEST
    if prev is None:
        _dh_pool_manifest_save(files, [], None)
        return {"baseline": True, "files": len(files)}
    delta = _dh_pool_diff(prev["files"], files)
    record = {
        "at": time.time(), "reason": reason,
        "window_seconds": round(time.time() - (prev.get("taken_at") or time.time())),
        "paths": _dh_pool_paths(), "delta": delta,
        "counts": {k: len(v) for k, v in delta.items()},
        "candidates": sorted(candidates), "forced": True,
    }
    if not _dh_pool_delta_empty(delta):
        _dh_pool_log_delta(record)
        _DH_POOL_STATUS["deltas"] += 1
        _DH_POOL_STATUS["recent"] = ([{
            "at": record["at"], "counts": record["counts"],
            "candidates": record["candidates"], "reason": reason, "forced": True,
        }] + _DH_POOL_STATUS["recent"])[:20]
    # Keep the boundary markers the poller reasons about; only the hashes and
    # the timestamp are newer.
    _dh_pool_manifest_save(files, prev.get("in_flight") or [], prev.get("event_id"))
    return {"counts": record["counts"], "delta": delta}


async def _poll_pool_manifest_forever() -> None:
    """Hash the shared pool at each run boundary and attribute what moved.

    Off unless `darkhelix.pool_manifest` is true. Detective only -- it never
    blocks a card and never touches the pool."""
    if not _dh_pool_cfg().get("pool_manifest"):
        return
    _dh_pool_manifest_load()
    _DH_POOL_STATUS["enabled"] = True
    _DH_POOL_STATUS["baseline"] = _DH_POOL_MANIFEST is not None
    while True:
        try:
            await _pool_manifest_tick()
            _DH_POOL_STATUS["last_error"] = None
        except Exception as exc:
            _DH_POOL_STATUS["last_error"] = str(exc)[:300]
        _DH_POOL_STATUS["last_tick"] = time.time()
        await asyncio.sleep(_dh_pool_poll_seconds())


@app.get("/api/darkhelix/pool-manifest")
async def darkhelix_pool_manifest(limit: int = 20) -> JSONResponse:
    """Sweep state plus the recent delta ledger.

    `windows_clean` is as informative as `deltas`: it is the count of run
    boundaries where the shared pool provably did not move."""
    ledger: list[dict] = []
    try:
        lines = POOL_DELTA_LOG.read_text(encoding="utf-8").splitlines()
        for line in lines[-max(1, min(limit, 200)):]:
            try:
                ledger.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        pass
    return JSONResponse({
        **_DH_POOL_STATUS,
        "paths": _dh_pool_paths(),
        "poll_seconds": _dh_pool_poll_seconds(),
        "manifest_files": len((_DH_POOL_MANIFEST or {}).get("files") or {}),
        "manifest_taken_at": (_DH_POOL_MANIFEST or {}).get("taken_at"),
        "ledger": ledger,
    })



# ------------------------------------------- the sanctioned write path
# The wall this exists to avoid building.
#
# Item E mounts the primary checkout READ-ONLY into the engine container
# (`-v {primary}:{primary}:ro`, dispatch_task.py:347). That is right for
# preventing an unreviewable mutation of the shared pool -- and it also means
# a card that legitimately needs to ADD a reference genome to
# database/collab_refs/ cannot do it from inside the container at all. Today
# the only route is the one t_d17fef80 took: block, then work by hand outside
# the container. Item C closes that exit. Closing it without opening another
# leaves a real, recurring operation with no path, and the doc is explicit
# about what a worker does at a hard wall.
#
# So: a narrow rw staging area OUTSIDE the repo. A card writes proposed
# reference files to /ssdpool/pool-staging/<task_id>/ and says so; promotion
# into database/collab_refs/ is this explicit, reviewed step.
#
# Staging sits outside /ssdpool/DARKHELIX on purpose. Inside the checkout it
# would show up in `git status`, and inside collab_refs it would register in
# the pool manifest as an `added` file -- conflating a PROPOSAL with an actual
# pool mutation. Out here, staging is invisible to both, and promotion is what
# the manifest records.
#
# THE MOUNT IS NOT IN PLACE YET. dispatch_task.py on snarf must mount
# POOL_STAGING_ROOT rw for a worker to be able to use this; until then these
# endpoints serve a human staging files by hand, and item C's enforcement must
# stay off.
POOL_STAGING_ROOT = "/ssdpool/pool-staging"

# Reference data only. This is a promotion path into a shared bioinformatics
# pool, not a general file-drop: an extension allowlist keeps a stray script
# or a .pyc out of collab_refs, and keeps the blast radius of the whole
# mechanism to the kind of file it exists to move.
_DH_PROMOTE_EXTS = {".fna", ".fa", ".fasta", ".gff", ".gff3", ".gbk", ".faa",
                    ".tsv", ".csv", ".txt", ".json", ".yaml", ".yml", ".md"}

# The pool path promotions land in, relative to DARKHELIX_REPO_PATH.
_DH_PROMOTE_DEST = "database/collab_refs"


def _dh_staging_dir(task_id: str) -> str:
    root = (_dh_pool_cfg().get("pool_staging_root") or POOL_STAGING_ROOT).rstrip("/")
    return f"{root}/{task_id}"


def _dh_promote_name_ok(name: str) -> bool:
    """A flat, plain filename with an allowed extension.

    Names come off a `find` on the staging dir, but the caller may also pass a
    subset, and either way they end up in a shell command and a destination
    path. Anything with a separator, a leading dot, or an unexpected extension
    is refused rather than sanitised -- there is no legitimate reference file
    that needs a path component."""
    if not name or "/" in name or name.startswith("."):
        return False
    if name != Path(name).name or name in (".", ".."):
        return False
    return Path(name).suffix.lower() in _DH_PROMOTE_EXTS


async def _dh_staged_files(task_id: str) -> list[dict]:
    """What is staged for one card: name, size and md5, flat, one round trip."""
    staging = _dh_staging_dir(task_id)
    rc, out = await _fleet_ssh(
        "snarf",
        f"test -d {shlex.quote(staging)} || {{ echo NODIR; exit 0; }}; "
        f"cd {shlex.quote(staging)} && "
        f"find . -maxdepth 1 -type f -printf '%f\\t%s\\n' 2>/dev/null | sort")
    if rc != 0:
        raise RuntimeError(f"staging listing failed (rc {rc}): {(out or '')[-200:]}")
    text = (out or "").strip()
    if not text or text == "NODIR":
        return []
    rows: list[dict] = []
    for line in text.splitlines():
        if "\t" not in line:
            continue
        name, _, size = line.partition("\t")
        rows.append({"name": name, "size": int(size) if size.isdigit() else None,
                     "eligible": _dh_promote_name_ok(name)})
    return rows


@app.get("/api/darkhelix/staged/{task_id}")
async def darkhelix_staged(task_id: str) -> JSONResponse:
    """What a card has proposed for the shared pool, and what is promotable."""
    if not _TASK_ID_RE.match(task_id):
        return JSONResponse({"ok": False, "error": "bad task id"}, status_code=400)
    try:
        files = await _dh_staged_files(task_id)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return JSONResponse({"ok": True, "task_id": task_id,
                         "staging": _dh_staging_dir(task_id),
                         "destination": f"{DARKHELIX_REPO_PATH}/{_DH_PROMOTE_DEST}",
                         "files": files,
                         "mount_note": ("dispatch_task.py must mount this rw for a "
                                        "worker to write here; not yet in place")})


@app.post("/api/darkhelix/promote-refs")
async def darkhelix_promote_refs(request: Request) -> JSONResponse:
    """Promote a card's staged reference files into the shared pool.

    The reviewed step that makes a legitimate data addition possible without
    a worker climbing the read-only wall.

    An existing file is NEVER overwritten unless `overwrite: true` is passed
    explicitly. Replacing a reference in place is precisely what t_d17fef80
    did to 263.fna, and it is the one operation here that can destroy
    something, so it does not happen by default and it does not happen by
    accident.

    Copies rather than moves, and leaves staging intact: if anything about
    the promotion turns out to be wrong, the proposal is still there. The
    copy is verified by md5 on the far side before it is reported as done.
    """
    payload = await request.json()
    task_id = (payload.get("task_id") or "").strip()
    if not _TASK_ID_RE.match(task_id):
        return JSONResponse({"ok": False, "error": "bad task id"}, status_code=400)
    dry_run = bool(payload.get("dry_run"))
    overwrite = bool(payload.get("overwrite"))

    try:
        staged = await _dh_staged_files(task_id)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    if not staged:
        return JSONResponse({"ok": False, "error": "nothing staged for this card",
                             "staging": _dh_staging_dir(task_id)}, status_code=404)

    available = {f["name"] for f in staged}
    requested = payload.get("files")
    if requested:
        # Validate against what is actually there rather than trusting the
        # request: a name that is not in the listing never reaches a command.
        unknown = [n for n in requested if n not in available]
        if unknown:
            return JSONResponse({"ok": False, "error": f"not staged: {unknown}"},
                                status_code=400)
        names = [n for n in requested]
    else:
        names = sorted(available)

    refused = [n for n in names if not _dh_promote_name_ok(n)]
    names = [n for n in names if _dh_promote_name_ok(n)]
    if not names:
        return JSONResponse({"ok": False, "error": "no eligible files",
                             "refused": refused,
                             "allowed_extensions": sorted(_DH_PROMOTE_EXTS)},
                            status_code=400)

    staging = _dh_staging_dir(task_id)
    dest_dir = f"{DARKHELIX_REPO_PATH}/{_DH_PROMOTE_DEST}"

    # What is already there, so a clobber is reported before it happens.
    rc, out = await _fleet_ssh(
        "snarf", "for n in " + " ".join(shlex.quote(n) for n in names) +
        f"; do if [ -e {shlex.quote(dest_dir)}/\"$n\" ]; then echo \"$n\"; fi; done")
    existing = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if existing and not overwrite:
        return JSONResponse({
            "ok": False, "error": "would overwrite existing pool files",
            "existing": existing, "refused": refused,
            "hint": "pass overwrite: true to replace them, deliberately",
        }, status_code=409)

    if dry_run:
        return JSONResponse({"ok": True, "dry_run": True, "task_id": task_id,
                             "would_promote": names, "would_overwrite": existing,
                             "refused": refused, "staging": staging,
                             "destination": dest_dir})

    # Copy, then verify by md5 on the far side. `cp` to a temp name in the
    # destination and `mv` into place keeps a reader from ever seeing a
    # half-written reference file.
    script_parts = [f"cd {shlex.quote(staging)} || exit 9",
                    f"mkdir -p {shlex.quote(dest_dir)} || exit 9"]
    for n in names:
        q = shlex.quote(n)
        d = shlex.quote(f"{dest_dir}/{n}")
        script_parts.append(
            f'cp -f -- {q} {d}.part && mv -f -- {d}.part {d} || echo "FAIL {n}"')
    script_parts.append("echo DONE")
    rc, out = await _fleet_ssh("snarf", "sh -c " + shlex.quote("; ".join(script_parts)))
    failures = [ln.split(" ", 1)[1] for ln in (out or "").splitlines()
                if ln.startswith("FAIL ")]
    if rc != 0 or "DONE" not in (out or ""):
        return JSONResponse({"ok": False, "error": f"promotion failed: {(out or '')[-400:]}"},
                            status_code=502)

    # Verify: the promoted file must hash the same on both sides.
    rc, out = await _fleet_ssh(
        "snarf",
        f"cd {shlex.quote(staging)} && md5sum " +
        " ".join(shlex.quote(n) for n in names) +
        f" 2>/dev/null; cd {shlex.quote(dest_dir)} && md5sum " +
        " ".join(shlex.quote(n) for n in names) + " 2>/dev/null")
    hashes: dict[str, list[str]] = {}
    for line in (out or "").splitlines():
        if "  " not in line:
            continue
        digest, _, name = line.partition("  ")
        hashes.setdefault(name.strip(), []).append(digest)
    verified = [n for n in names
                if len(hashes.get(n, [])) == 2 and len(set(hashes[n])) == 1]
    mismatched = [n for n in names if n not in verified]

    result = {"ok": not mismatched and not failures, "task_id": task_id,
              "promoted": verified, "failed": failures + mismatched,
              "refused": refused, "overwrote": existing,
              "staging": staging, "destination": dest_dir}

    # Record it in the pool ledger straight away. A promotion happens outside
    # any run boundary, so the manifest poller would not otherwise re-hash
    # until the next board event -- and an unrecorded pool change is the exact
    # thing policy 3 exists to abolish.
    if verified:
        try:
            result["ledger"] = await _dh_pool_force_snapshot(
                reason=f"promotion from {staging}", candidates=[task_id])
        except Exception as exc:
            result["ledger_error"] = str(exc)[:200]
        try:
            await asyncio.to_thread(
                _kanban_api_call, "POST",
                f"/api/plugins/kanban/tasks/{quote(task_id)}/comments",
                json={"author": "looking-glass", "body":
                      "**Promoted to the shared pool.**\n\n"
                      + "\n".join(f"  - `{_DH_PROMOTE_DEST}/{n}`" for n in verified)
                      + (f"\n\nReplaced existing: {', '.join(existing)}" if existing else "")
                      + "\n\nCopied from staging and md5-verified on the far side. "
                        "The staged copies were left in place."})
        except Exception as exc:
            result["comment_error"] = str(exc)[:200]

    return JSONResponse(result, status_code=200 if result["ok"] else 502)



# ------------------------------------------------ making "blocked" terminal
# Work item C. `t_d17fef80` blocked itself at 20:45 -- correctly -- and then
# went on working by hand until 23:59, mutating the shared pool. Blocking
# recorded a state and changed nothing about what the worker could still do.
#
# THE LEVER. `hermes kanban reclaim` looks like the tool for this and is not:
# `reclaim_task` (kanban_db.py:4600) welds two separable things together --
# `_terminate_reclaimed_worker(worker_pid, claim_lock)`, which SIGTERMs then
# SIGKILLs a host-local worker, and an `UPDATE ... SET status='ready'`. Only
# the kill is wanted. `ready` is DISPATCHABLE, so reclaiming a card the worker
# just blocked would immediately re-dispatch it.
#
# Hermes is vendor software and is configured, not patched -- but the kill
# half needs no patch, because both of its inputs are already on the plugin
# API's task detail: `worker_pid`, and `claim_lock` in the form
# "{hostname}:{pid}" (`_claimer_id()`, kanban_db.py:2858; the hostname on
# CT111 is `hermes`). So the HUD makes the same host-locality check Hermes
# makes and sends the same signals, and simply never touches the status.
#
# WHY LEAVING IT BLOCKED IS SAFE -- both checked in the Hermes source:
#   - a claim is only ever taken on `status='ready'` (kanban_db.py:4295), so
#     a blocked card is not dispatchable;
#   - a worker's own `kanban_block` emits a `blocked` EVENT, which makes
#     `_has_sticky_block()` true, and `recompute_ready` explicitly refuses to
#     auto-promote a sticky-blocked card (kanban_db.py:4177). Only an explicit
#     `unblock` exits. Stickiness comes from the event, not from `--kind`, so
#     any deliberate block qualifies while circuit-breaker blocks (which emit
#     `gave_up`, not `blocked`) keep their auto-recovery.
#
# SHIPPED OFF. `darkhelix.enforce_block` defaults false. Enforcement must not
# precede the sanctioned write path: the engine mounts the primary checkout
# read-only (dispatch_task.py:347), so a card that legitimately needs to add a
# reference genome CANNOT do it inside the container, and doing it by hand
# after blocking is currently the only route there is. Closing that exit
# before /ssdpool/pool-staging is mounted rw would leave no path at all --
# "fix the walls before punishing the climbing". Turn this on after the mount.
_DH_BLOCK_HANDLED_MAX = 500
_DH_BLOCK_STATE: dict = {"seeded": False, "handled": []}
_DH_BLOCK_STATUS: dict = {
    "enabled": False, "seeded": False, "last_tick": None, "last_error": None,
    "note": None, "killed": 0, "recent": [],
}
BLOCK_STATE_PATH = ROOT / "logs" / "enforce_blocks.json"


def _dh_block_cfg() -> dict:
    return CFG.get("darkhelix") or {}


def _dh_block_poll_seconds() -> int:
    return int(_dh_block_cfg().get("enforce_block_poll_seconds") or 60)


def _dh_block_state_load() -> None:
    global _DH_BLOCK_STATE
    try:
        data = json.loads(BLOCK_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    if isinstance(data, dict) and isinstance(data.get("handled"), list):
        _DH_BLOCK_STATE = {"seeded": bool(data.get("seeded")),
                           "handled": [str(i) for i in data["handled"]][-_DH_BLOCK_HANDLED_MAX:]}


def _dh_block_state_save() -> None:
    try:
        BLOCK_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BLOCK_STATE_PATH.write_text(json.dumps(_DH_BLOCK_STATE), encoding="utf-8")
    except Exception:
        pass


def _dh_block_mark(task_id: str) -> None:
    handled = _DH_BLOCK_STATE["handled"]
    if task_id in handled:
        return
    handled.append(task_id)
    del handled[:-_DH_BLOCK_HANDLED_MAX]


async def _dh_kanban_hostname() -> str:
    """The hostname CT111 stamps into `claim_lock`. Read, never assumed --
    a wrong guess here would make every claim look remote and silently
    disable enforcement."""
    rc, out = await _kanban_ssh("hostname")
    return (out or "").strip().splitlines()[-1].strip() if rc == 0 and out else ""


async def _dh_terminate_worker(task_id: str, pid: int) -> dict:
    """SIGTERM, wait, SIGKILL -- but only if the pid really is this card's
    Hermes worker.

    The pid comes off a board row that may be stale, and the OS recycles
    pids. Killing a recycled pid would kill an unrelated process, so the
    identity check and the signal happen in ONE ssh command: checking from
    here and signalling in a second round trip is a race with exactly that
    failure mode. /proc/<pid>/cmdline must name hermes AND this task."""
    script = (
        f'p={int(pid)}; '
        f'test -r /proc/$p/cmdline || {{ echo GONE; exit 0; }}; '
        f'cl=$(tr "\\0" " " < /proc/$p/cmdline); '
        f'case "$cl" in *hermes*) ;; *) echo NOTHERMES; exit 3;; esac; '
        f'case "$cl" in *{shlex.quote(task_id)}*) ;; *) echo WRONGTASK; exit 4;; esac; '
        f'kill -TERM $p 2>/dev/null || {{ echo GONE; exit 0; }}; '
        f'for i in $(seq 20); do kill -0 $p 2>/dev/null || {{ echo TERMED; exit 0; }}; sleep 0.5; done; '
        f'kill -KILL $p 2>/dev/null; sleep 1; '
        f'kill -0 $p 2>/dev/null && echo SURVIVED || echo KILLED'
    )
    try:
        rc, out = await _kanban_ssh(f"sh -c {shlex.quote(script)}")
    except Exception as exc:
        return {"ok": False, "outcome": "ssh-failed", "error": str(exc)[:200]}
    token = (out or "").strip().splitlines()[-1].strip() if out else ""
    return {"ok": token in ("TERMED", "KILLED", "GONE"), "outcome": token or f"rc{rc}"}


async def _dh_enforce_block(task_id: str, dry_run: bool = False) -> dict:
    """End the run behind one blocked card. The card's status is never touched."""
    if not _TASK_ID_RE.match(task_id or ""):
        return {"ok": False, "error": "bad task id"}
    try:
        detail = await _kanban_task_detail(task_id)
    except Exception as exc:
        return {"ok": False, "error": f"task lookup failed: {exc}"}
    task = detail.get("task") or {}
    if (task.get("status") or "") != "blocked":
        return {"ok": True, "outcome": "skipped",
                "why": f"status is {task.get('status')!r}, not blocked"}

    lock = task.get("claim_lock")
    pid = task.get("worker_pid")
    if not lock or not pid:
        # The normal case for a card blocked long ago: the claim is already
        # released, so there is no run left to end.
        return {"ok": True, "outcome": "no-live-claim",
                "claim_lock": lock, "worker_pid": pid}

    host = await _dh_kanban_hostname()
    if not host:
        return {"ok": False, "error": "could not read the kanban host's hostname"}
    if not str(lock).startswith(f"{host}:"):
        # Same guard Hermes applies. Signalling across hosts is not possible
        # from here and guessing would be worse than declining.
        return {"ok": True, "outcome": "remote-claim", "claim_lock": lock}

    if dry_run:
        return {"ok": True, "outcome": "would-terminate",
                "worker_pid": pid, "claim_lock": lock, "dry_run": True}

    result = await _dh_terminate_worker(task_id, int(pid))
    result.update({"worker_pid": pid, "claim_lock": lock, "task_id": task_id})
    if result.get("ok") and result.get("outcome") != "GONE":
        try:
            await asyncio.to_thread(
                _kanban_api_call, "POST",
                f"/api/plugins/kanban/tasks/{quote(task_id)}/comments",
                json={"author": "looking-glass", "body":
                      "**Run ended.** This card blocked itself while its worker "
                      f"was still running (pid {pid}); the worker was terminated "
                      "so that blocking actually stops work rather than only "
                      "recording a state.\n\nThe card's status was deliberately "
                      "left at `blocked` — it is not requeued, and `unblock` is "
                      "the only way out."})
        except Exception as exc:
            result["comment_error"] = str(exc)[:200]
    return result


async def _enforce_blocks_tick() -> None:
    board = await _kanban_api_get("/api/plugins/kanban/board")
    rows = [t for c in (board.get("columns") or []) for t in (c.get("tasks") or [])]
    blocked = [t["id"] for t in rows
               if t.get("id") and (t.get("status") or "") == "blocked"]

    if not _DH_BLOCK_STATE["seeded"]:
        # Same reasoning as the verification sweep: the cards already sitting
        # in the blocked lane had their runs end long ago. Their `worker_pid`
        # is stale, and a stale pid is the one thing that must never be
        # signalled. Seed, act on nothing.
        for task_id in blocked:
            _dh_block_mark(task_id)
        _DH_BLOCK_STATE["seeded"] = True
        _dh_block_state_save()
        _DH_BLOCK_STATUS["seeded"] = True
        _DH_BLOCK_STATUS["note"] = (
            f"seeded {len(blocked)} already-blocked card(s) without signalling "
            "anything; enforcement applies to blocks from here on")
        return

    handled = set(_DH_BLOCK_STATE["handled"])
    for task_id in [t for t in blocked if t not in handled]:
        result = await _dh_enforce_block(task_id)
        if not result.get("ok"):
            _DH_BLOCK_STATUS["recent"] = ([{
                "task_id": task_id, "outcome": "error",
                "error": result.get("error"), "at": time.time(),
            }] + _DH_BLOCK_STATUS["recent"])[:20]
            continue
        _dh_block_mark(task_id)
        outcome = result.get("outcome")
        if outcome in ("TERMED", "KILLED"):
            _DH_BLOCK_STATUS["killed"] += 1
        _DH_BLOCK_STATUS["recent"] = ([{
            "task_id": task_id, "outcome": outcome,
            "worker_pid": result.get("worker_pid"), "at": time.time(),
        }] + _DH_BLOCK_STATUS["recent"])[:20]
    _dh_block_state_save()


async def _poll_enforce_blocks_forever() -> None:
    """Make blocking terminal: end the run behind a card that blocked itself.

    OFF by default. See the module note above -- this must not be enabled
    before the sanctioned staging path exists, or a card needing to add a
    reference genome is left with no route at all."""
    if not _dh_block_cfg().get("enforce_block"):
        return
    _dh_block_state_load()
    _DH_BLOCK_STATUS["enabled"] = True
    _DH_BLOCK_STATUS["seeded"] = _DH_BLOCK_STATE["seeded"]
    while True:
        try:
            await _enforce_blocks_tick()
            _DH_BLOCK_STATUS["last_error"] = None
        except Exception as exc:
            _DH_BLOCK_STATUS["last_error"] = str(exc)[:300]
        _DH_BLOCK_STATUS["last_tick"] = time.time()
        await asyncio.sleep(_dh_block_poll_seconds())


@app.get("/api/kanban/enforce-block")
async def kanban_enforce_block_status() -> JSONResponse:
    return JSONResponse({**_DH_BLOCK_STATUS,
                         "handled_count": len(_DH_BLOCK_STATE["handled"]),
                         "poll_seconds": _dh_block_poll_seconds()})


@app.post("/api/kanban/enforce-block")
async def kanban_enforce_block(request: Request) -> JSONResponse:
    """End one blocked card's run by hand.

    Works whether or not the poller is enabled, and `dry_run` reports what
    would be signalled without sending anything -- which is how to check the
    lever against a live card before turning enforcement on."""
    payload = await request.json()
    task_id = (payload.get("task_id") or "").strip()
    result = await _dh_enforce_block(task_id, dry_run=bool(payload.get("dry_run")))
    status = 200 if result.get("ok") else 502
    if result.get("error") == "bad task id":
        status = 400
    return JSONResponse(result, status_code=status)



# ------------------------------------------------- DARKHELIX verification
# Checking whether a card's work actually holds almost always means running
# something on snarf: DARKHELIX lives at /ssdpool/DARKHELIX and no other box
# in the fleet can see into it. Doing that with an agent costs a full model
# run -- the coder card that blocked this board burned 2h04m and produced
# nothing checkable before its model endpoint died. The repo's own suite is
# 596 tests in ~44s with no model in the loop, so this exposes it directly
# over the SSH connection pool /api/darkhelix-todo already uses.
#
# Checks are defined HERE, never read from a card. A card body is written by
# an LLM -- the triage specifier rewrites title and body on promotion -- so
# treating it as a source of shell commands would be a command-injection path
# onto snarf as `sam`. The card may only name a check; this table decides
# what that name runs.
DARKHELIX_CHECKS: dict[str, dict[str, str]] = {
    "tests": {
        "label": "Test suite",
        "cmd": ".venv-dev/bin/pytest tests/ -q",
        "timeout": "900",
    },
    "imports": {
        "label": "Package imports",
        "cmd": ".venv-dev/bin/python -c 'import darkhelix; print(\"import ok\")'",
        "timeout": "120",
    },
    "tree": {
        "label": "Working tree + recent commits",
        "cmd": "git status --short --branch && echo '--- recent ---' && git log --oneline -5",
        "timeout": "60",
    },
}


@app.get("/api/darkhelix/checks")
async def darkhelix_check_list() -> JSONResponse:
    """The checks the HUD is allowed to run, for the Verify control."""
    return JSONResponse({"checks": [{"id": k, "label": v["label"]}
                                    for k, v in DARKHELIX_CHECKS.items()]})


@app.post("/api/darkhelix/verify")
async def darkhelix_verify(request: Request) -> JSONResponse:
    """Run one named check on snarf and return its exit code and output.

    Optionally posts the verdict back as a comment on `task_id`, so the
    result is durable on the card and a retrying worker can read why it
    failed instead of rediscovering it."""
    payload = await request.json()
    check = (payload.get("check") or "").strip()
    spec = DARKHELIX_CHECKS.get(check)
    if spec is None:
        return JSONResponse({"ok": False, "error": f"unknown check: {check!r}"},
                            status_code=400)
    cmd = (f"cd {shlex.quote(DARKHELIX_REPO_PATH)} && "
           f"timeout {spec['timeout']} {spec['cmd']} 2>&1")
    started = time.monotonic()
    try:
        rc, out = await _fleet_ssh("snarf", cmd)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    elapsed = round(time.monotonic() - started, 1)
    result = {"ok": rc == 0, "rc": rc, "check": check, "label": spec["label"],
              "elapsed": elapsed, "output": out[-8000:]}

    task_id = (payload.get("task_id") or "").strip()
    if task_id and payload.get("comment") and _TASK_ID_RE.match(task_id):
        verdict = "PASSED" if rc == 0 else f"FAILED (exit {rc})"
        tail = "\n".join(out.strip().splitlines()[-12:])
        note = (f"Verification `{check}` {verdict} in {elapsed}s "
                f"(run from the Looking Glass HUD on snarf).\n\n```\n{tail}\n```")
        try:
            await asyncio.to_thread(
                _kanban_api_call, "POST",
                f"/api/plugins/kanban/tasks/{quote(task_id)}/comments",
                json={"author": "looking-glass", "body": note})
            result["commented"] = True
        except Exception as exc:
            # The check itself succeeded or failed on its own merits; failing
            # to file the note must not change that verdict.
            result["comment_error"] = str(exc)
    return JSONResponse(result)


@app.post("/api/darkhelix/land")
async def darkhelix_land(request: Request) -> JSONResponse:
    """Take a card's finished worktree from "worker stopped" to "reviewable".

        verify -> commit -> push -> PR

    The branch is ALWAYS pushed, even when verification fails. Durability is
    the thing that actually went wrong here: 552 lines sat uncommitted in a
    shared checkout for five days because nothing ever pushed anything. A
    branch is cheap and silent; losing work is not.

    A pull request is opened only when verification PASSES. A card that
    crashed halfway leaves a branch and no review noise -- the coder card
    that ran 2h04m before its model endpoint died would otherwise have
    opened a PR too.

    Every failure reroutes the work back onto the board: the card is blocked
    with the reason (which `hermes kanban block` also files as a comment), so
    it reappears in the blocked lane with its cause attached instead of
    failing silently off-screen."""
    payload = await request.json()
    task_id = (payload.get("task_id") or "").strip()
    if not _TASK_ID_RE.match(task_id):
        return JSONResponse({"ok": False, "error": "bad task id"}, status_code=400)
    check = (payload.get("check") or "tests").strip()
    spec = DARKHELIX_CHECKS.get(check)
    if spec is None:
        return JSONResponse({"ok": False, "error": f"unknown check: {check!r}"},
                            status_code=400)
    open_pr = bool(payload.get("pr", True))
    branch, wt = _dh_branch(task_id), _dh_worktree(task_id)
    steps: list[dict] = []

    async def fail(stage: str, detail: str) -> JSONResponse:
        reason = f"land failed at {stage}: {detail[:400]}"
        rerouted = await _kanban_block(task_id, reason)
        if not steps or steps[-1].get("stage") != stage:
            steps.append({"stage": stage, "ok": False, "detail": detail[-1500:]})
        return JSONResponse({"ok": False, "stage": stage, "steps": steps,
                             "branch": branch, "rerouted_to_kanban": rerouted,
                             "error": detail[-1500:]}, status_code=200)

    async def run(stage: str, cmd: str, allow_fail: bool = False):
        """Run one stage in the card's worktree. ALWAYS records a step --
        an exception is a stage outcome too, and callers read steps[-1]."""
        try:
            rc, out = await _fleet_ssh("snarf", f"cd {shlex.quote(wt)} && {cmd} 2>&1")
        except Exception as exc:
            steps.append({"stage": stage, "ok": False, "rc": None, "detail": str(exc)})
            return False, str(exc)
        steps.append({"stage": stage, "ok": rc == 0 or allow_fail,
                      "rc": rc, "detail": out[-1500:]})
        return (rc == 0 or allow_fail), out

    try:
        _, present = await _fleet_ssh(
            "snarf", f"test -d {shlex.quote(wt)} && echo yes || echo no")
    except Exception as exc:
        return await fail("worktree", str(exc))
    if present.strip() != "yes":
        return await fail("worktree",
                          f"no worktree at {wt} — was this card filed before "
                          "isolation existed?")

    # 1. verify (does not gate the push, only the PR)
    ok_verify, verify_out = await run(
        "verify", f"timeout {spec['timeout']} {spec['cmd']}", allow_fail=True)
    verify_passed = steps[-1].get("rc") == 0

    # 2. commit whatever the worker left behind. This is what makes the work
    #    durable -- it is in git, on its own branch, the moment this succeeds.
    #    Pushing is visibility and offsite backup, not durability.
    ok, out = await run(
        "commit",
        "git add -A && (git diff --cached --quiet && echo 'nothing to commit' || "
        f"git commit -q -m {shlex.quote(f'{task_id}: work from the Looking Glass kanban')})")
    if not ok:
        return await fail("commit", out)

    result = {"ok": True, "branch": branch, "worktree": wt,
              "verify_passed": verify_passed, "steps": steps, "pr_url": None}

    # 3. A failed check stops here, BEFORE the push. DARKHELIX has a pre-push
    #    hook that runs the suite and refuses a broken push -- attempting it
    #    anyway would just run the same ~40s suite a second time to be told
    #    no. The commit above already holds the work; the card goes back to
    #    the board carrying the reason.
    if not verify_passed:
        reason = (f"verification `{check}` failed, so nothing was pushed and no PR "
                  f"was opened. The work is committed locally on {branch} in "
                  f"{wt}.\n\n{verify_out[-500:]}")
        result["ok"] = False
        result["stage"] = "verify"
        result["pushed"] = False
        result["pr_skipped"] = "verification failed"
        result["rerouted_to_kanban"] = await _kanban_block(task_id, reason)
        return JSONResponse(result)

    # 4. push -- the hook re-runs the suite here and should agree with step 1
    ok, out = await run("push", f"git push -u origin {shlex.quote(branch)}")
    if not ok:
        return await fail("push", out)
    result["pushed"] = True

    if not open_pr:
        result["pr_skipped"] = "pr not requested"
        return JSONResponse(result)

    title = f"[hermes] {task_id}: kanban work"
    body = (f"Filed from the Looking Glass HUD for kanban card `{task_id}`.\n\n"
            f"Verification `{check}` passed before this PR was opened.\n\n"
            f"```\n{chr(10).join(verify_out.strip().splitlines()[-12:])}\n```")
    ok, out = await run(
        "pr",
        f"gh pr create --base {shlex.quote(DARKHELIX_BASE_BRANCH)} "
        f"--head {shlex.quote(branch)} --title {shlex.quote(title)} "
        f"--body {shlex.quote(body)}")
    if not ok:
        # The work is pushed and safe; only the PR step failed.
        return await fail("pr", out)
    url = next((ln.strip() for ln in out.splitlines()
                if ln.strip().startswith("https://")), None)
    result["pr_url"] = url
    return JSONResponse(result)


def _submission_key(title: str, body: str) -> str:
    """The dedup identity of a submission: a hash of what was submitted.

    TODO.md item ids are positional (`todo-3` means "the 4th unchecked box in
    THIS parse"), so they identify nothing across an edit of the file. The
    content does.

    Filing (`/api/kanban/create`) and lookup (`/api/darkhelix-todo`) must
    derive this identically or the "already filed" badge silently stops
    matching, so both call here rather than each hashing for themselves."""
    return "lg-" + hashlib.sha256(f"{title}\n{body}".encode("utf-8")).hexdigest()[:32]


async def _submitted_keys() -> tuple[dict[str, dict], dict[str, dict]]:
    """({submission key -> card}, {card title -> card}) for the whole board.

    Archived cards are included on purpose: an item whose card was filed,
    finished and archived is still an item you should not be filing again.

    TWO indexes, because the key alone misses everything that matters. The
    idempotency key is set by /api/kanban/create, so only cards filed after it
    was introduced carry one -- and on 2026-08-28 that was six archived test
    cards and nothing else. Every card that did real work (t_9116c28b, the
    fabricated-panel fix; t_08aa9412, the taxor scheduler headers) predates it,
    so both items still showed as open in SUBMIT WORK long after their fix was
    merged to master. A tracker whose staleness check cannot see any of the
    real work is not a tracker.

    The title index closes most of that gap: SUBMIT WORK files a card whose
    title IS the TODO item's first line, so an exact match on it identifies the
    pairing with no key. It is weaker evidence than the key -- a hand-written
    card title will not match, which is why t_08aa9412 ("Fix uge-taxor.sh
    scheduler memory headers...") still cannot be paired with its item -- but
    it is exact, not fuzzy, so it does not invent pairings either."""
    board = await _kanban_api_get(
        "/api/plugins/kanban/board?include_archived=true")
    keys: dict[str, dict] = {}
    titles: dict[str, dict] = {}
    for column in board.get("columns") or []:
        for task in column.get("tasks") or []:
            card = {"id": task.get("id"), "status": task.get("status")}
            key = task.get("idempotency_key")
            if key:
                keys[key] = card
            title = (task.get("title") or "").strip()
            if title:
                titles.setdefault(title, card)
    return keys, titles


@app.get("/api/darkhelix-todo")
async def darkhelix_todo() -> JSONResponse:
    """Real, current TODO.md items from DARKHELIX (snarf), for the SUBMIT
    WORK panel's picker. Read live every request, no caching -- Sam edits
    this file directly and it's the single tracker ("if it isn't here, it
    isn't tracked").

    Each item is annotated with `filed_as` when a card for it already exists,
    because TODO.md and the board are separate trackers that nothing keeps in
    step: filing a card never ticks the box, and landing the work never ticks
    it either. That is how an item whose fix is already sitting in a merged
    or open PR stays sitting in this picker looking like open work."""
    try:
        rc, out = await _fleet_ssh("snarf", f"cat {shlex.quote(DARKHELIX_TODO_PATH)}")
    except Exception as exc:
        return JSONResponse({"items": [], "error": str(exc)}, status_code=502)
    if rc != 0:
        return JSONResponse({"items": [], "error": f"cat exited {rc}"}, status_code=502)
    items = _parse_darkhelix_todo(out)

    # A board that can't be read costs the badges, not the picker.
    board_error = None
    try:
        filed, filed_titles = await _submitted_keys()
    except Exception as exc:
        filed, filed_titles, board_error = {}, {}, str(exc)
    for item in items:
        # Key first: it matches a submission made with no extra instructions,
        # which is how the picker files by default. Adding notes makes a
        # different card on purpose -- different instructions are a different
        # request. Title second, for the cards that predate the key.
        item["filed_as"] = (filed.get(_submission_key(item["title"], item["text"]))
                            or filed_titles.get(item["title"].strip()))

    return JSONResponse({"items": items, "board_error": board_error})


# ------------------------------------------- TODO.md <- board write-back
# TODO.md and the kanban board were separate trackers with nothing flowing
# between them. Filing a card never ticked the box and landing the work never
# ticked it either, so an item whose fix was already merged went on looking
# like open work forever -- and the picker cheerfully offered to file it
# again. The "filed" badge was a read-only patch over that: it told you the
# state, in the HUD, until you closed the tab. Nothing reached the file.
#
# This writes the board's state back into TODO.md, in the file's own
# vocabulary (the conventions its header already documents, and that
# _parse_darkhelix_todo already reads):
#
#   card done             ->  - [x], and any **WIP** tag dropped
#   card running/ready/review -> - [ ] **WIP** (tag added if absent)
#
# What it will NOT do, on purpose:
#   * untick anything. Only ever ' ' -> 'x', never the reverse: the box is
#     Sam's to tick and a board hiccup must not erase a completion.
#   * touch an item already carrying **WIP** or **WAITING**. WAITING is a
#     human judgement about a missing database or binary; the board does not
#     get to overrule it.
#   * treat `archived` as done. Archive is reachable from any status, so it
#     is evidence a card was closed, not evidence the work happened.
_TODO_WIP_STATUSES = ("running", "ready", "review")


def _todo_apply_board_state(text: str, items: list[dict],
                            filed: dict[str, dict],
                            filed_titles: dict[str, dict] | None = None
                            ) -> tuple[str, list[dict]]:
    """Return (new_text, changes) with each item's line rewritten in place."""
    lines = text.splitlines(keepends=True)
    changes: list[dict] = []
    filed_titles = filed_titles or {}
    for item in items:
        card = (filed.get(_submission_key(item["title"], item["text"]))
                or filed_titles.get(item["title"].strip()))
        if not card:
            continue
        idx = item.get("line")
        if idx is None or idx >= len(lines):
            continue
        raw = lines[idx]
        eol = raw[len(raw.rstrip("\r\n")):]
        line = raw.rstrip("\r\n")
        m = _TODO_ITEM_RE.match(line)
        if not m:
            continue
        rest = m.group(2)
        status = card.get("status")
        if status == "done":
            # Drop the status tag as part of ticking: a **WIP** box that is
            # also checked contradicts itself.
            new_line = "- [x] " + _TODO_STATUS_TAG_RE.sub("", rest)
            kind = "completed"
        elif status in _TODO_WIP_STATUSES and not _TODO_STATUS_TAG_RE.match(rest):
            new_line = "- [ ] **WIP** " + rest
            kind = "marked WIP"
        else:
            continue
        if new_line == line:
            continue
        lines[idx] = new_line + (eol or "\n")
        changes.append({"title": item["title"][:120], "card": card.get("id"),
                        "status": status, "change": kind})
    return "".join(lines), changes


@app.post("/api/darkhelix-todo/sync")
async def darkhelix_todo_sync() -> JSONResponse:
    """Push the board's state back into TODO.md on snarf.

    Compare-and-swap, not a blind write: the replacement only lands if the
    file on snarf still hashes to what was just read. TODO.md is edited by
    hand as the single tracker, and clobbering an edit made in the seconds
    this took would be a far worse bug than the staleness being fixed. A
    `.bak` is kept alongside regardless."""
    try:
        rc, out = await _fleet_ssh("snarf", f"cat {shlex.quote(DARKHELIX_TODO_PATH)}")
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    if rc != 0:
        return JSONResponse({"ok": False, "error": f"cat exited {rc}"}, status_code=502)
    try:
        filed, filed_titles = await _submitted_keys()
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"board unreadable: {exc}"},
                            status_code=502)

    items = _parse_darkhelix_todo(out)
    new_text, changes = _todo_apply_board_state(out, items, filed, filed_titles)
    if not changes:
        return JSONResponse({"ok": True, "changes": [], "written": False})

    before = hashlib.sha256(out.encode("utf-8")).hexdigest()
    payload = base64.b64encode(new_text.encode("utf-8")).decode("ascii")
    path = shlex.quote(DARKHELIX_TODO_PATH)
    cmd = (
        f"set -e; cd \"$(dirname {path})\"; "
        f"test \"$(sha256sum {path} | cut -d' ' -f1)\" = {shlex.quote(before)} "
        f"|| {{ echo 'TODO.md changed on disk since it was read'; exit 3; }}; "
        f"printf '%s' {shlex.quote(payload)} | base64 -d > {path}.lg-new; "
        f"cp -p {path} {path}.bak; mv {path}.lg-new {path}"
    )
    try:
        rc, out2 = await _fleet_ssh("snarf", cmd + " 2>&1")
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc), "changes": changes},
                            status_code=502)
    if rc != 0:
        return JSONResponse({"ok": False, "error": out2.strip()[-500:] or f"exit {rc}",
                             "changes": changes}, status_code=409 if rc == 3 else 502)
    return JSONResponse({"ok": True, "changes": changes, "written": True})


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
    # "Builds on": the card this one continues. Two things follow from it,
    # and both are needed -- one without the other is still broken.
    #   --parent    makes the board hold this card in `todo` until the parent
    #               closes, so they run in order instead of racing.
    #   base branch makes the child's worktree start FROM the parent's result
    #               instead of from origin/master.
    # Ordering alone was never the problem; a card that runs second but sees
    # none of the first card's changes still rebuilds the same work.
    parent_task_id = (payload.get("parent_task_id") or "").strip()
    if parent_task_id and not _TASK_ID_RE.match(parent_task_id):
        return JSONResponse({"ok": False, "error": "bad parent task id"}, status_code=400)
    # Dedup key, derived from what was actually submitted. TODO.md item ids
    # are positional (`todo-3` means "the 4th unchecked box in this parse"),
    # so they identify nothing across an edit of the file -- the content
    # does. `hermes kanban create --idempotency-key` returns the EXISTING
    # non-archived card's id instead of creating a second one, which is what
    # stops the same item being filed twice with nothing noticing.
    #
    # Hash the body as RECEIVED, before the dispatch-target header below is
    # prepended: that header carries a timestamped branch name, so hashing
    # after it would make every key unique and quietly defeat the dedup.
    idempotency_key = _submission_key(title, body)
    # The dispatch-target block is filled in AFTER creation: the branch and
    # worktree are named from the task id, which does not exist yet.
    cmd = (
        "hermes kanban create "
        f"{shlex.quote(title[:200])} "
        f"--body {shlex.quote(body)} "
        "--workspace scratch "
        f"--idempotency-key {shlex.quote(idempotency_key)} "
        f"--assignee {shlex.quote(_darkhelix_assignee())} "
        + (f"--parent {shlex.quote(parent_task_id)} " if parent_task_id else "")
        + "--triage --created-by looking-glass --json"
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
    # On a dedup hit the CLI prints the pre-existing card rather than a new
    # one, and says nothing about which happened. Its age is the tell: a card
    # this request created is seconds old.
    created_at = data.get("created_at")
    duplicate = False
    try:
        if created_at is not None:
            duplicate = (time.time() - float(created_at)) > 10
    except (TypeError, ValueError):
        duplicate = False

    # Isolation, created here rather than asked for in the card text -- via
    # the same _darkhelix_provision() the claim-time hook uses, so a card
    # filed here and a card decomposed out of it are provisioned by one
    # implementation that cannot drift from itself.
    task_id = str(data.get("id") or data.get("task_id") or "")
    prov: dict = {"ok": False, "error": "no task id returned"}
    if _TASK_ID_RE.match(task_id):
        prov = await _darkhelix_provision(
            task_id,
            parent_task_ids=[parent_task_id] if parent_task_id else [],
            body=body,
        )

    return JSONResponse({"ok": True, "task": data, "duplicate": duplicate,
                         "idempotency_key": idempotency_key,
                         "isolated": bool(prov.get("isolated")),
                         "worktree": prov.get("worktree"),
                         "branch": prov.get("branch"),
                         "parent": parent_task_id or None,
                         "base": prov.get("base"),
                         "isolation_detail": prov.get("detail") or prov.get("error") or ""})


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


@app.get("/api/kanban/{task_id}")
async def kanban_task(task_id: str) -> JSONResponse:
    """One card, with its comments, runs, links and recent events.

    The task-log pane used to poll the WHOLE board every 3s just to read one
    card's status, because the ssh CLI had no per-task read. The plugin API
    does, so this is a single card's worth of traffic instead of the board's.
    No ssh fallback: the caller already has the board's copy of the card to
    fall back on, and a degraded detail view is better than a slow one."""
    if not _TASK_ID_RE.match(task_id):
        return JSONResponse({"error": "bad task id"}, status_code=400)
    try:
        detail = await _kanban_api_get(f"/api/plugins/kanban/tasks/{quote(task_id)}")
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    # `body` is deliberately absent from _KANBAN_TASK_FIELDS: the BOARD carries
    # every card and full bodies would bloat a 15s poll for text nothing on the
    # board renders. A single card is different -- the edit pane needs the spec
    # it is about to edit, and one body is one body.
    task = _kanban_task_slim(detail.get("task") or {})
    task["body"] = (detail.get("task") or {}).get("body") or ""
    return JSONResponse({
        "task": task,
        "comments": detail.get("comments") or [],
        "runs": detail.get("runs") or [],
        "links": detail.get("links") or [],
        "events": (detail.get("events") or [])[-40:],
    })


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
    # The shared mempalace lives on claude-control; these say who else reads it.
    # A connector rather than a use-hull on the flat map (see memory_edges in
    # server.yaml) so the map keeps three regions instead of four.
    memory_edges = [
        {"from": "claude-control", **e}
        for e in (cfg.get("memory_edges") or []) if e.get("to") in node_ids
    ]

    NETWORK_TOPOLOGY_STATE.update({
        "updated": time.time(),
        "nodes": nodes,
        "edges": {
            "physical": physical_edges, "general": general_edges,
            "hermes": hermes_edges, "claude": claude_edges,
            "memory": memory_edges,
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
    asyncio.get_running_loop().create_task(_verify_completions_forever())
    asyncio.get_running_loop().create_task(_poll_pool_manifest_forever())
    asyncio.get_running_loop().create_task(_poll_enforce_blocks_forever())


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
