"""Stage 1: audio extraction with ffmpeg. The video stream is never decoded."""
import logging
import subprocess
import queue
import tempfile
import threading
import time
import wave
from pathlib import Path

import lecture_trimmer.config as config
from lecture_trimmer.processing.job_control import check_cancelled

logger = logging.getLogger("trimmer")


class AudioExtractionError(RuntimeError):
    """Public preparation error; detailed FFmpeg output stays in server logs."""
    public_message = ("Could not prepare this video's audio. This run stopped before speech recognition "
                      "and Gemini. The original video is unchanged. Try a fresh download or a different "
                      "copy of the video; the saved report contains technical details.")


def run_ffmpeg(args: list[str], *, progress=None, duration=None, stall_timeout=180) -> None:
    """Drain both pipes safely, report real media progress, and stop stalled encoders."""
    check_cancelled()
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror", "-nostdin", "-y",
           "-progress", "pipe:1", "-stats_period", "1", *args]
    messages = queue.Queue()
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as errors:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors, text=True,
                                encoding="utf-8", errors="replace")
        def read_progress():
            for line in proc.stdout:
                messages.put(line.strip())
        reader = threading.Thread(target=read_progress, daemon=True)
        reader.start()
        last_change, latest = time.monotonic(), -1.0
        try:
            while proc.poll() is None or not messages.empty():
                check_cancelled()
                try:
                    line = messages.get(timeout=0.25)
                    if line.startswith("out_time_us=") and line.split("=", 1)[1] != "N/A":
                        seconds = max(0, int(line.split("=", 1)[1]) / 1_000_000)
                        if seconds > latest:
                            latest, last_change = seconds, time.monotonic()
                        if progress and duration:
                            progress(min(seconds / duration, .999), f"{min(100, seconds/duration*100):.0f}% rendered")
                except queue.Empty:
                    pass
                if time.monotonic() - last_change > stall_timeout:
                    raise RuntimeError(f"FFmpeg made no progress for {stall_timeout}s. Completed sections are saved; retry the export.")
            if proc.wait() != 0:
                errors.seek(0)
                raise RuntimeError(f"FFmpeg failed: {errors.read()[-4000:]}")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            reader.join(timeout=2)
            proc.stdout.close()


def probe_duration(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(video)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return float(out.stdout.strip())


def extract_audio(video: Path, out_dir: Path = config.TEMP_DIR, *, progress=None) -> Path:
    """Prepare mono speech audio on the source timeline, including real gaps.

    AAC priming/remuxed recordings can contain overlapping initial timestamps.
    Explicit resampler synchronization fills gaps and trims timestamp overlaps;
    rebuilding timestamps from sample counts alone would shift later speech.
    Keep strict decoding errors enabled so damaged audio cannot silently pass.
    """
    out = out_dir / f"{video.stem}.wav"
    if out.resolve() == video.resolve():
        raise AudioExtractionError("Audio extraction must not overwrite the source file")
    logger.info("Extracting audio -> %s", out.name)
    try:
        duration = probe_duration(video)
        sync = f"aresample={config.AUDIO_SAMPLE_RATE}:async=1:first_pts=0:min_hard_comp=0.001"
        notify = (lambda fraction, _: progress(fraction, f"Preparing lecture audio — {fraction*100:.0f}%")) if progress else None
        run_ffmpeg(["-i", str(video), "-map", "0:a:0", "-vn", "-ac", "1", "-af", sync,
                    "-ar", str(config.AUDIO_SAMPLE_RATE), "-c:a", "pcm_s16le", str(out)],
                   progress=notify, duration=duration)
        with wave.open(str(out), "rb") as audio:
            if (audio.getnchannels() != 1 or audio.getframerate() != config.AUDIO_SAMPLE_RATE
                    or audio.getsampwidth() != 2 or audio.getnframes() <= 0):
                raise ValueError("Audio extraction produced empty or invalid PCM audio")
    except (RuntimeError, OSError, ValueError, wave.Error, EOFError, subprocess.SubprocessError) as exc:
        out.unlink(missing_ok=True)
        raise AudioExtractionError(f"Audio preparation failed: {exc}") from exc
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
