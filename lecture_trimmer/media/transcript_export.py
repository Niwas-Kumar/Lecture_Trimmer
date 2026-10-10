"""Portable speech transcripts with timestamps relative to the original lecture."""
from pathlib import Path

from lecture_trimmer.models import Transcript


def timestamp(seconds, separator='.'):
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole, fraction = divmod(remainder, 1000)
    return f'{hours:02d}:{minutes:02d}:{whole:02d}{separator}{fraction:03d}'


def write_transcript_files(transcript: Transcript, directory: Path, *, video_name, backend, model=''):
    directory.mkdir(parents=True, exist_ok=True)
    header = [f'Lecture: {video_name}', f'Speech provider: {backend}',
              f'Model: {model or "not recorded"}', f'Detected/requested language: {transcript.language}',
              'Times refer to the ORIGINAL lecture, not the trimmed outputs.',
              'Automatic speech recognition; wording and timestamps are not human-verified.',
              'Speech is reproduced as returned by the provider, before study classification.', '']
    rows = [f'[{timestamp(s.start)} --> {timestamp(s.end)}] {s.text}' for s in transcript.segments]
    text_path = directory / 'transcript.txt'
    text_path.write_text('\n'.join(header + rows) + '\n', encoding='utf-8-sig')
    srt_path = directory / 'transcript.srt'
    srt_path.write_text('\n\n'.join(
        f'{index}\n{timestamp(s.start, ",")} --> {timestamp(s.end, ",")}\n{s.text}'
        for index, s in enumerate(transcript.segments, 1)) + '\n', encoding='utf-8-sig')
    json_path = directory / 'transcript.json'
    json_path.write_text(transcript.model_dump_json(indent=2), encoding='utf-8')
    paths = [text_path, srt_path, json_path]
    if transcript.words:
        word_path = directory / 'word_timestamps.txt'
        word_path.write_text('\n'.join(header + [
            f'[{timestamp(w.start)} --> {timestamp(w.end)}] {w.text}' for w in transcript.words
        ]) + '\n', encoding='utf-8-sig')
        paths.append(word_path)
    return [str(path.resolve()) for path in paths]
