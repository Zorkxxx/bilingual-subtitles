#!/usr/bin/env python3
"""Locally transcribe media into unverified timestamped candidates; never edits records."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone


class AudioError(Exception):
    pass


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_audio_streams(source):
    try:
        import av
        with av.open(str(source), mode='r') as container:
            return [{'stream_index': stream.index, 'language': stream.metadata.get('language'),
                     'start_pts': stream.start_time,
                     'time_base': str(stream.time_base) if stream.time_base is not None else None,
                     'container_start_time_us': container.start_time}
                    for stream in container.streams.audio]
    except Exception as exc:
        raise AudioError('Could not inspect audio streams with PyAV: ' + str(exc)) from exc


def transcribe(source, model_dir, output, language=None, device='cpu', compute_type='int8', backend=None, media_probe=None):
    source, model_dir, output = Path(source).resolve(), Path(model_dir).resolve(), Path(output).absolute()
    if not source.is_file():
        raise AudioError('Input audio or video does not exist or is not a file.')
    original_stat = source.stat()
    if not model_dir.is_dir():
        raise AudioError('A local faster-whisper model directory is required; model IDs and automatic downloads are not supported.')
    # Without a local tokenizer some faster-whisper versions try to fetch one.
    # The optional preprocessor config falls back to local defaults.
    required = ('model.bin', 'config.json', 'tokenizer.json')
    missing = [name for name in required if not (model_dir / name).is_file()]
    if missing:
        raise AudioError('Local model directory is incomplete: ' + ', '.join(missing))
    if output.exists() or output.is_symlink():
        raise AudioError('Output already exists; choose a new path.')
    if not output.parent.is_dir():
        raise AudioError('Output parent directory does not exist.')
    if language is not None and (not language.isascii() or not language.isalpha() or not 2 <= len(language) <= 3):
        raise AudioError('Language must be a two- or three-letter language code, or omit it for detection.')
    tracks = (media_probe or inspect_audio_streams)(source)
    if len(tracks) != 1:
        raise AudioError(f'Expected exactly one audio stream, found {len(tracks)}. '
                         'For multi-track media, explicitly extract the intended track with ffmpeg and supply that file.')
    if backend is None:
        # No API token is read and downloads are disabled even if library defaults change.
        os.environ['HF_HUB_OFFLINE'] = '1'
        os.environ['TRANSFORMERS_OFFLINE'] = '1'
        try:
            import faster_whisper as backend
        except ImportError as exc:
            raise AudioError('faster-whisper is not installed in this Python environment.') from exc
    try:
        model = backend.WhisperModel(str(model_dir), device=device, compute_type=compute_type, local_files_only=True)
        segments, info = model.transcribe(str(source), language=language, task='transcribe', vad_filter=False)
        cues = []
        for segment in segments:  # inference is lazy; consume all before writing any result
            start, end = float(segment.start), float(segment.end)
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
                raise AudioError('The recognizer returned an invalid segment time.')
            text = str(segment.text).strip()
            if not text:
                continue
            cues.append({'id': f'asr-{len(cues) + 1:06d}', 'start_ms': round(start * 1000),
                         'end_ms': round(end * 1000), 'text': text,
                         'avg_logprob': getattr(segment, 'avg_logprob', None),
                         'no_speech_prob': getattr(segment, 'no_speech_prob', None),
                         'status': 'candidate_unverified'})
        duration = float(info.duration)
        if not math.isfinite(duration) or duration <= 0:
            raise AudioError('The decoded audio has no valid duration.')
        if any(cue['end_ms'] > math.ceil(duration * 1000) + 100 for cue in cues):
            raise AudioError('A recognized segment exceeds the decoded audio duration.')
        source_hash = sha256_file(source)
        final_stat = source.stat()
        if (original_stat.st_size, original_stat.st_mtime_ns) != (final_stat.st_size, final_stat.st_mtime_ns):
            raise AudioError('The source changed during recognition; repeat with a stable input.')
        report = {'schema_version': 1, 'kind': 'local_asr_candidates',
                  'created_utc': datetime.now(timezone.utc).isoformat(),
                  'source': {'path': str(source), 'size_bytes': final_stat.st_size, 'sha256': source_hash,
                             'audio_stream': tracks[0]},
                  'model': {'directory': str(model_dir), 'engine': 'faster-whisper',
                            'engine_version': getattr(backend, '__version__', 'unknown'),
                            'device_requested': device, 'compute_type_requested': compute_type,
                            'local_files_only': True},
                  'recognition': {'task': 'transcribe', 'language_requested': language,
                                  'language_detected': getattr(info, 'language', None),
                                  'language_probability': getattr(info, 'language_probability', None),
                                  'duration_seconds': duration, 'vad_filter': False,
                                  'timestamp_basis': 'decoded_audio_elapsed',
                                  'video_timeline_alignment': 'unverified'},
                  'segments': cues,
                  'review': {'status': 'pending', 'audio_heard_by_human': False,
                             'translation_verified': False,
                             'limitations': ['ASR output is an unverified candidate, not proof of human listening or translation accuracy.',
                                             'Timestamps are relative to decoded audio, not verified video time. Check offset and discontinuities against the source before using them.',
                                             'Silence, overlapping or quiet speech, chunk boundaries and screen text may contain missed content.',
                                             'Compare with actual media and independently review task coverage before any final delivery.']}}
        serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        # Create a separate temporary file in the destination directory, then
        # atomically link it without replacing a file created by another process.
        fd, tmp = tempfile.mkstemp(prefix='.local-audio-', suffix='.tmp', dir=output.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(tmp, output)
        finally:
            os.unlink(tmp)
    except AudioError:
        raise
    except Exception as exc:
        raise AudioError('Local recognition failed; no completed candidate report was produced: ' + str(exc)) from exc
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    cmd = commands.add_parser('transcribe', help='Decode local media and produce unverified speech candidates.')
    cmd.add_argument('source', type=Path)
    cmd.add_argument('--model', type=Path, required=True, help='Existing local faster-whisper model directory.')
    cmd.add_argument('--out', type=Path, required=True, help='New output JSON path; never overwrite.')
    cmd.add_argument('--language', default=None, help='Source language code, or omit for detection.')
    cmd.add_argument('--device', choices=('auto', 'cpu', 'cuda'), default='cpu')
    cmd.add_argument('--compute-type', choices=('auto', 'int8', 'float16', 'int8_float16'), default='int8')
    args = parser.parse_args(argv)
    try:
        report = transcribe(args.source, args.model, args.out, args.language, args.device, args.compute_type)
    except AudioError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({'output': str(args.out), 'segments': len(report['segments']),
                      'status': 'candidate_unverified'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
