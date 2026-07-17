"""Optional Google ViSQOL command-line adapter."""

from __future__ import annotations

import csv
import os
import subprocess
import tempfile
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse

from voxbench.media import resample_pcm16_mono
from voxbench.verification.full_reference import FullReferenceCandidate
from voxbench.verification.scoring import (
    FullReferenceMeasurement,
    FullReferenceScorerContract,
    FullReferenceScorerReadiness,
)

VisqolMode = Literal["speech", "audio"]
ProcessRunner = Callable[..., subprocess.CompletedProcess[Any]]

VISQOL_CONTRACT = FullReferenceScorerContract(
    scorer="visqol",
    metric_name="visqol_moslqo",
    minimum_score=1.0,
    maximum_score=5.0,
)

_MODE_SAMPLE_RATES: dict[VisqolMode, int] = {
    "speech": 16_000,
    "audio": 48_000,
}


@dataclass(frozen=True)
class VisqolCliScorer:
    """Run an explicitly installed ViSQOL binary without making it a core dependency."""

    binary: Path
    mode: VisqolMode = "speech"
    timeout_seconds: float = 120.0
    process_runner: ProcessRunner = subprocess.run
    contract: FullReferenceScorerContract = VISQOL_CONTRACT

    def __post_init__(self) -> None:
        if self.mode not in _MODE_SAMPLE_RATES:
            raise ValueError("mode must be speech or audio")
        if not 0 < self.timeout_seconds <= 600:
            raise ValueError("timeout_seconds must be between 0 and 600")

    def readiness(self) -> FullReferenceScorerReadiness:
        available = self.binary.is_file() and os.access(self.binary, os.X_OK)
        return FullReferenceScorerReadiness(
            available=available,
            reason_alias=None if available else "visqol-binary-unavailable",
        )

    def score(self, candidate: FullReferenceCandidate) -> FullReferenceMeasurement:
        input_rate = _candidate_sample_rate(candidate)
        target_rate = _MODE_SAMPLE_RATES[self.mode]
        reference_pcm = _read_pcm16_mono_wav(candidate.reference_uri, expected_rate=input_rate)
        degraded_pcm = _read_pcm16_mono_wav(candidate.degraded_uri, expected_rate=input_rate)
        transformations = (f"visqol-mode:{self.mode}",)
        if input_rate != target_rate:
            reference_pcm = resample_pcm16_mono(
                reference_pcm,
                input_rate=input_rate,
                output_rate=target_rate,
            )
            degraded_pcm = resample_pcm16_mono(
                degraded_pcm,
                input_rate=input_rate,
                output_rate=target_rate,
            )
            transformations += (f"resample:{input_rate}->{target_rate}",)

        with tempfile.TemporaryDirectory(prefix="voxbench-visqol-") as temporary_dir:
            root = Path(temporary_dir)
            reference_path = root / "reference.wav"
            degraded_path = root / "degraded.wav"
            results_path = root / "results.csv"
            _write_pcm16_mono_wav(reference_path, reference_pcm, sample_rate=target_rate)
            _write_pcm16_mono_wav(degraded_path, degraded_pcm, sample_rate=target_rate)
            command = [
                str(self.binary.resolve()),
                "--reference_file",
                str(reference_path),
                "--degraded_file",
                str(degraded_path),
                "--results_csv",
                str(results_path),
            ]
            if self.mode == "speech":
                command.append("--use_speech_mode")
            completed = self.process_runner(
                command,
                check=False,
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                timeout=self.timeout_seconds,
            )
            if completed.returncode != 0:
                raise RuntimeError("visqol process failed")
            score = _read_moslqo(results_path)
        return FullReferenceMeasurement(score=score, transformations=transformations)


def _candidate_sample_rate(candidate: FullReferenceCandidate) -> int:
    comparison_format = candidate.comparison_format
    if any(
        comparison_format.get(key) != candidate.degraded_format.get(key)
        for key in ("encoding", "rate", "channels")
    ):
        raise ValueError("reference and degraded formats must match")
    if comparison_format.get("encoding") != "pcm16":
        raise ValueError("ViSQOL input must be decoded PCM16")
    if comparison_format.get("channels") != 1:
        raise ValueError("ViSQOL input must be mono")
    sample_rate = comparison_format.get("rate")
    if not isinstance(sample_rate, int) or isinstance(sample_rate, bool) or sample_rate <= 0:
        raise ValueError("ViSQOL input sample rate must be a positive integer")
    return sample_rate


def _read_pcm16_mono_wav(uri: str, *, expected_rate: int) -> bytes:
    path = _local_file_uri_path(uri)
    with wave.open(str(path), "rb") as wav:
        if wav.getcomptype() != "NONE":
            raise ValueError("compressed WAV input is unsupported")
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("WAV input must be mono PCM16")
        if wav.getframerate() != expected_rate:
            raise ValueError("WAV sample rate does not match candidate metadata")
        return wav.readframes(wav.getnframes())


def _local_file_uri_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        raise ValueError("scorer input must be a local file URI")
    if parsed.query or parsed.fragment:
        raise ValueError("scorer input URI must not contain query or fragment")
    return Path(unquote(parsed.path))


def _write_pcm16_mono_wav(path: Path, pcm: bytes, *, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)


def _read_moslqo(results_path: Path) -> float:
    with results_path.open(newline="", encoding="utf-8") as handle:
        rows = csv.DictReader(handle)
        row = next(rows, None)
    if row is None or "moslqo" not in row:
        raise ValueError("ViSQOL results did not contain MOS-LQO")
    return float(row["moslqo"])
