"""FFmpeg operations shared by the local meeting batch and its SSH worker."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import wave
import zipfile
from pathlib import Path


CHANNEL_SECONDS = 299


def encode_channel(ffmpeg, source, output, channel, offset, length):
    subprocess.run([str(ffmpeg), '-v', 'error', '-nostdin', '-y', '-ss', str(offset),
                    '-protocol_whitelist', 'file', '-i', str(source), '-t', str(length),
                    '-af', f'pan=mono|c0=c{channel}', '-ar', '16000', '-b:a', '64k', str(output)], check=True)


def channel_name(channel, offset):
    return f'{channel}-{offset:06d}.mp3'


def sha256(path):
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            checksum.update(chunk)
    return checksum.hexdigest()


def run_channel_worker(ffmpeg, folder, stdin, stdout):
    request = folder / 'request.zip'
    source = folder / 'recording.source'
    with request.open('wb') as stream:
        shutil.copyfileobj(stdin, stream, length=1024 * 1024)
    with zipfile.ZipFile(request) as archive:
        if (len(archive.infolist()) != 2 or set(archive.namelist()) != {'channels.json', source.name}
                or archive.getinfo('channels.json').file_size > 256 * 1024):
            raise ValueError('unexpected channel input')
        manifest = json.loads(archive.read('channels.json'))
        identity, jobs = manifest['source_sha256'], manifest['jobs']
        if (not re.fullmatch(r'[0-9a-f]{64}', identity) or not jobs
                or any(type(channel) is not int or channel not in (0, 1)
                       or type(offset) is not int or offset < 0 or offset % CHANNEL_SECONDS
                       or not math.isfinite(length) or not 0 < length <= CHANNEL_SECONDS
                       for channel, offset, length in jobs)
                or len({(channel, offset) for channel, offset, _ in jobs}) != len(jobs)):
            raise ValueError('invalid channel request')
        unpack(archive, source.name, source)
    request.unlink()
    if sha256(source) != identity:
        raise ValueError('channel source fingerprint differs')
    outputs = {}
    for channel, offset, length in jobs:
        output = folder / channel_name(channel, offset)
        encode_channel(ffmpeg, source, output, channel, offset, length)
        outputs[output.name] = sha256(output)
    with zipfile.ZipFile(stdout, 'w') as response:
        response.writestr('channels.json', json.dumps({**manifest, 'outputs': outputs}))
        for name in outputs:
            response.write(folder / name, name)


def normalize(ffmpeg, source, output):
    subprocess.run([str(ffmpeg), '-v', 'error', '-nostdin', '-y', '-protocol_whitelist', 'file',
                    '-i', str(source), '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(output)], check=True)


def combine(ffmpeg, files, output):
    listing = output.parent / 'concat.txt'
    with listing.open('w', encoding='ascii', newline='\n') as stream:
        stream.write('\n'.join(f"file '{p.name}'" for p in files))
    subprocess.run([str(ffmpeg), '-v', 'error', '-nostdin', '-y', '-f', 'concat', '-safe', '1',
                    '-i', str(listing), '-c', 'copy', str(output)], check=True)


def wav_frames(path, checksum=None):
    with wave.open(str(path), 'rb') as stream:
        if (stream.getnchannels(), stream.getframerate(), stream.getsampwidth(), stream.getcomptype()) != (1, 16000, 2, 'NONE'):
            raise ValueError('meeting audio must be mono 16 kHz PCM16')
        frames = stream.getnframes()
        if not frames:
            raise ValueError('empty meeting audio')
        if checksum is not None:
            remaining = frames * 2
            while chunk := stream.readframes(65536):
                checksum.update(chunk)
                remaining -= len(chunk)
            if remaining:
                raise ValueError('truncated PCM')
        return frames


def unpack(archive, name, target):
    with archive.open(name) as source, target.open('wb') as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)


def run_worker(ffmpeg, folder, stdin, stdout):
    request = folder / 'request.zip'
    with request.open('wb') as stream:
        shutil.copyfileobj(stdin, stream, length=1024 * 1024)
    with zipfile.ZipFile(request) as archive:
        order = json.loads(archive.read('order.json'))
        if (not isinstance(order, list) or not order
                or not all(isinstance(i, str) and re.fullmatch(r'[0-9a-f]{64}', i) for i in order)
                or len(set(order)) != len(order)):
            raise ValueError('invalid meeting source order')
        names = set(archive.namelist())
        ordered_names = [identity + ('.wav' if identity + '.wav' in names else '.source') for identity in order]
        if len(archive.infolist()) != len(order) + 1 or names != {'order.json', *ordered_names}:
            raise ValueError('unexpected meeting audio input')
        files, generated = [], []
        for identity, name in zip(order, ordered_names):
            incoming = folder / name
            unpack(archive, name, incoming)
            audio = folder / (identity + '.wav')
            if name.endswith('.source'):
                if sha256(incoming) != identity:
                    raise ValueError('meeting source fingerprint differs')
                normalize(ffmpeg, incoming, audio)
                incoming.unlink()
                generated.append(audio)
            wav_frames(audio)
            files.append(audio)
    request.unlink()
    combined = folder / 'combined.wav'
    combine(ffmpeg, files, combined)
    # Stream the response, including ZIP64 for long meetings.
    with zipfile.ZipFile(stdout, 'w') as response:
        for file in [*generated, combined]:
            response.write(file, file.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ffmpeg', type=Path)
    parser.add_argument('scratch', type=Path)
    parser.add_argument('--channels', action='store_true')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='meeting-', dir=args.scratch) as temporary:
        worker = run_channel_worker if args.channels else run_worker
        worker(args.ffmpeg, Path(temporary), sys.stdin.buffer, sys.stdout.buffer)


if __name__ == '__main__':
    main()
