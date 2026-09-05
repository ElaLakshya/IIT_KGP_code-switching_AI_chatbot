"""
Code-switching TTS adapter.

Rime's own docs are explicit that a single Coda voice serves exactly one
language -- no Coda voice crosses between Hindi and English (see
PERMANENT_RULES.md / project notes). So for a genuinely code-switched
sentence like:

    "Bhai, kaise ho? Also, what's the cancellation charge?"

...one Rime call cannot correctly pronounce both halves. This module:

  1. Segments the LLM's text into clause-level (Hindi, English) spans via
     language_segmenter.segment_text().
  2. Synthesizes each span using the OFFICIAL `livekit-plugins-rime` TTS
     client -- one instance per language, since each Coda voice is
     language-locked -- rather than hand-rolling the Rime HTTP call
     ourselves. This still satisfies PERMANENT_RULES.md (Rime is the only
     TTS engine here); it just delegates the actual HTTP/retry/error
     handling to LiveKit's maintained plugin instead of reimplementing it.
  3. Crossfades adjacent spans at the splice point and normalizes pacing so
     the seam is as unobtrusive as possible.
  4. Writes the stitched PCM into the AgentSession via the 1.0
     `tts.ChunkedStream` / `AudioEmitter` contract.

This does NOT solve prosodic continuity the way a true polyglot model would
-- see the project notes on why stitching is a known, unsolved-in-general
problem, not a bug in this code. What it does do is make the seam quality
measurable: `last_stitch_report` exposes per-splice metadata (segment count,
languages, crossfade duration) so the evaluation harness can score
"naturalness at the switch" as an explicit, reported metric rather than an
unfalsifiable claim.

Mitigations applied here, per the tradeoffs discussed with the team:
  - Segmentation happens at clause boundaries, not mid-word.
  - Both voices are matched for similar age/energy in `DEFAULT_VOICE_PAIR`.
  - `speed_alpha` is held constant across both voices so pacing doesn't jump.
  - A short crossfade (default 60ms) replaces the hard cut at each splice.
"""

import asyncio
import logging
import os

import numpy as np
from livekit.agents import APIConnectOptions, tts, utils
from livekit.plugins import rime as rime_plugin

from language_segmenter import Lang, Segment, segment_text

logger = logging.getLogger("codeswitch-tts")

SAMPLE_RATE = 24000
NUM_CHANNELS = 1

# Voices picked for similar vocal character (young adult, warm/energetic)
# so the switch reads as "same person, different language" rather than a
# jump to a different speaker. Verified against the live catalog
# (https://users.rime.ai/data/voices/all-v2.json) on 2026-09-05 -- Coda's
# "hin" language currently ships 3 voices: "hin", "nadi", "taru". Re-check
# before a demo -- catalogs change.
DEFAULT_VOICE_PAIR = {
    Lang.ENGLISH: "lyra",   # Coda, eng, warm young American
    Lang.HINDI: "nadi",     # Coda, hin
}

# ISO 639-2-ish codes the Rime API / plugin expects for the `lang` param.
RIME_LANG_CODE = {
    Lang.ENGLISH: "eng",
    Lang.HINDI: "hin",
}

CROSSFADE_MS = 60


class CodeSwitchTTS(tts.TTS):
    """LiveKit-compatible TTS that stitches per-language Rime segments."""

    def __init__(
        self,
        voice_pair: dict[Lang, str] | None = None,
        speed_alpha: float = 1.0,
        model: str | None = None,
    ):
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
        )

        # Env vars override the hardcoded defaults so RIME_MODEL /
        # RIME_VOICE_ENGLISH / RIME_VOICE_HINDI in .env actually do
        # something.
        voice_pair = voice_pair or {
            Lang.ENGLISH: os.environ.get("RIME_VOICE_ENGLISH", DEFAULT_VOICE_PAIR[Lang.ENGLISH]),
            Lang.HINDI: os.environ.get("RIME_VOICE_HINDI", DEFAULT_VOICE_PAIR[Lang.HINDI]),
        }
        model = model or os.environ.get("RIME_MODEL", "coda")
        api_key = os.environ["RIME_API_KEY"]  # fail loudly if unset

        # One official rime.TTS client per language -- each Coda voice is
        # language-locked, so we can't share a single client across both.
        self._per_lang_tts: dict[Lang, rime_plugin.TTS] = {
            lang: rime_plugin.TTS(
                model=model,
                speaker=speaker,
                lang=RIME_LANG_CODE[lang],
                speed_alpha=speed_alpha,
                sample_rate=SAMPLE_RATE,
                api_key=api_key,
            )
            for lang, speaker in voice_pair.items()
        }
        self.last_stitch_report: dict | None = None

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions | None = None,
    ) -> "CodeSwitchChunkedStream":
        return CodeSwitchChunkedStream(
            tts=self,
            input_text=text,
            conn_options=conn_options or tts.DEFAULT_API_CONNECT_OPTIONS,
        )

    async def aclose(self) -> None:
        for t in self._per_lang_tts.values():
            await t.aclose()


class CodeSwitchChunkedStream(tts.ChunkedStream):
    @staticmethod
    def _crossfade(a: np.ndarray, b: np.ndarray, fade_samples: int) -> np.ndarray:
        """Linearly crossfade the tail of `a` into the head of `b`."""
        fade_samples = min(fade_samples, len(a), len(b))
        if fade_samples <= 0:
            return np.concatenate([a, b])

        fade_out = np.linspace(1.0, 0.0, fade_samples)
        fade_in = np.linspace(0.0, 1.0, fade_samples)

        a = a.astype(np.float32)
        b = b.astype(np.float32)

        blended = a[-fade_samples:] * fade_out + b[:fade_samples] * fade_in
        return np.concatenate([a[:-fade_samples], blended, b[fade_samples:]]).astype(np.int16)

    async def _synth_segment(self, owner: CodeSwitchTTS, seg) -> np.ndarray:
        """Run one segment through its per-language Rime client and collect
        the raw PCM bytes it emits."""
        per_lang_tts = owner._per_lang_tts[seg.lang]
        stream = per_lang_tts.synthesize(seg.text)
        chunks: list[bytes] = []
        async for event in stream:
            chunks.append(bytes(event.frame.data))
        return np.frombuffer(b"".join(chunks), dtype=np.int16)

    @staticmethod
    def _soften_non_final_terminal_punctuation(segments):
        """Each segment is synthesized as an independent Rime call, so Rime
        has no idea it's mid-sentence -- it reads whatever punctuation ends
        the fragment as if that fragment were the whole utterance. A period
        on a non-final segment triggers a full sentence-final falling
        contour in the middle of the sentence, which is what reads as
        "wrong stress" at the language switch. Downgrading a mid-utterance
        period to a comma keeps that segment reading as a pause, not a stop.
        Question marks and exclamation marks are left alone -- those
        usually reflect real intent (e.g. a genuine mid-sentence quoted
        question) and forcing them to a comma would lose real meaning."""
        softened = []
        last_idx = len(segments) - 1
        for i, seg in enumerate(segments):
            text = seg.text
            if i != last_idx and text.rstrip().endswith("."):
                text = text.rstrip()[:-1].rstrip() + ","
            softened.append(Segment(text=text, lang=seg.lang))
        return softened

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        owner: CodeSwitchTTS = self._tts  # type: ignore[assignment]
        segments = segment_text(self._input_text)
        if not segments:
            return
        segments = self._soften_non_final_terminal_punctuation(segments)

        # Independent Rime calls -- fetch concurrently, no reason to
        # serialize and add latency.
        arrays = await asyncio.gather(
            *[self._synth_segment(owner, seg) for seg in segments]
        )

        fade_samples = int(SAMPLE_RATE * CROSSFADE_MS / 1000)
        stitched = arrays[0]
        for arr in arrays[1:]:
            stitched = self._crossfade(stitched, arr, fade_samples)

        owner.last_stitch_report = {
            "segment_count": len(segments),
            "languages": [s.lang.value for s in segments],
            "crossfade_ms": CROSSFADE_MS,
            "total_duration_s": round(len(stitched) / SAMPLE_RATE, 2),
        }
        logger.info("Stitch report: %s", owner.last_stitch_report)

        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
            mime_type="audio/pcm",
        )
        output_emitter.push(stitched.tobytes())