"""Stage 1: audio extraction with ffmpeg. The video stream is never decoded."""
import logging
import subprocess
from pathlib import Path

import config

logger = logging.getLogger("trimmer")


def run_ffmpeg(args: list[str]) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{' '.join(cmd)}\n{result.stderr}")


def probe_duration(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(video)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def extract_audio(video: Path, out_dir: Path = config.TEMP_DIR) -> Path:
    """Mono 16 kHz WAV - what every ASR model wants."""
    out = out_dir / f"{video.stem}.wav"
    logger.info("Extracting audio -> %s", out.name)
    run_ffmpeg(["-i", str(video), "-vn", "-ac", "1",
                "-ar", str(config.AUDIO_SAMPLE_RATE), "-c:a", "pcm_s16le", str(out)])
    return out


def chunk_audio(wav: Path, chunk_seconds: int, out_dir: Path = config.TEMP_DIR) -> list[tuple[float, Path]]:
    """Split WAV into fixed-length FLAC chunks for parallel cloud upload.

    Returns [(offset_seconds, chunk_path), ...]. FLAC keeps the chunks small
    (Groq has a 25 MB upload limit) without losing quality.
    """
    duration = probe_duration(wav)
    chunks = []
    offset = 0.0
    i = 0
    while offset < duration:
        p = out_dir / f"{wav.stem}_chunk{i:03d}.flac"
        run_ffmpeg(["-ss", str(offset), "-t", str(chunk_seconds), "-i", str(wav),
                    "-c:a", "flac", str(p)])
        chunks.append((offset, p))
        offset += chunk_seconds
        i += 1
    logger.info("Split audio into %d chunks of %ds", len(chunks), chunk_seconds)
    return chunks
