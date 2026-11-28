"""Send a frozen batch to the configured SSH host, using the original WAV cache."""
from __future__ import annotations

import hashlib
import json
import math
import re
import shlex
import subprocess
import tempfile
import zipfile
from pathlib import Path

from .core import Attention, file_hash
from .meeting_audio_worker import channel_name, unpack, wav_frames


def worker_command(worker, *options):
    host = worker['host']
    programs = [worker[k] for k in ('python', 'script', 'ffmpeg', 'scratch')]
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', host)
            or not all(p.startswith('/') for p in programs)):
        raise ValueError('invalid meeting audio worker configuration')
    python, script, ffmpeg, scratch = programs
    return ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
            host, shlex.join([python, '-I', '-B', script, ffmpeg, scratch, *options])]


def exchange(command, request, response):
    with request.open('rb') as payload, response.open('wb') as received:
        subprocess.run(command, stdin=payload, stdout=received, stderr=subprocess.DEVNULL, check=True, timeout=1800)
    request.unlink()


def require_members(archive, names):
    if (len(archive.infolist()) != len(names) or set(archive.namelist()) != names
            or any(info.compress_type != zipfile.ZIP_STORED or info.flag_bits & 1 for info in archive.infolist())):
        raise ValueError('unexpected meeting audio response')


def render_remote(worker, members, files, combined):
    ssh = worker_command(worker)
    missing = [file for file in files if not file.is_file()]
    with tempfile.TemporaryDirectory(prefix='meeting-worker-', dir=combined.parent) as temporary:
        folder = Path(temporary)
        request, response = folder / 'request.zip', folder / 'response.zip'
        with zipfile.ZipFile(request, 'w') as archive:
            archive.writestr('order.json', json.dumps([m['id'] for m in members]))
            for member, file in zip(members, files, strict=True):
                if file in missing:
                    archive.write(member['path'], member['id'] + '.source')
                else:
                    archive.write(file, file.name)
        exchange(ssh, request, response)
        outputs = [*missing, combined]
        with zipfile.ZipFile(response) as archive:
            require_members(archive, {p.name for p in outputs})
            for file in outputs:
                unpack(archive, file.name, folder / file.name)
        response.unlink()
        expected, received = hashlib.sha256(), hashlib.sha256()
        frames = sum(wav_frames(folder / file.name if file in missing else file, expected) for file in files)
        if (wav_frames(folder / combined.name, received) != frames or expected.digest() != received.digest()):
            raise ValueError('combined PCM does not match the ordered members')
        for member in members:
            if file_hash(Path(member['path'])) != member['id']:
                raise Attention('meeting_source_changed')
        for file in outputs:
            (folder / file.name).replace(file)


def verify_channel(ffprobe, path, length):
    probe = subprocess.run([str(ffprobe), '-v', 'error', '-show_entries',
                            'stream=codec_name,sample_rate,channels:format=duration', '-of', 'json', str(path)],
                           capture_output=True, check=True)
    media = json.loads(probe.stdout)
    duration = float(media['format']['duration'])
    if (media['streams'] != [{'codec_name': 'mp3', 'sample_rate': '16000', 'channels': 1}]
            or not math.isfinite(duration) or not duration > 0 or abs(duration - length) > 0.12):
        raise ValueError('channel output format or duration differs')


def render_channels(worker, source, identity, chunks, ffprobe, folder):
    """Return only validated MP3s, using the original local chunk destinations."""
    by_name = {channel_name(channel, offset): (length, target)
               for channel, offset, length, target in chunks}
    manifest = {'source_sha256': identity,
                'jobs': [[channel, offset, length] for channel, offset, length, _ in chunks]}
    with tempfile.TemporaryDirectory(prefix='channel-worker-', dir=folder) as temporary:
        staging = Path(temporary)
        request, response = staging / 'request.zip', staging / 'response.zip'
        with zipfile.ZipFile(request, 'w') as archive:
            archive.writestr('channels.json', json.dumps(manifest))
            archive.write(source, 'recording.source')
        exchange(worker_command(worker, '--channels'), request, response)
        with zipfile.ZipFile(response) as archive:
            require_members(archive, {'channels.json', *by_name})
            if archive.getinfo('channels.json').file_size > 512 * 1024:
                raise ValueError('oversized channel receipt')
            returned = json.loads(archive.read('channels.json'))
            hashes = returned.pop('outputs')
            if returned != manifest or set(hashes) != set(by_name):
                raise ValueError('channel receipt does not match request')
            for name, (length, _) in by_name.items():
                if not 0 < archive.getinfo(name).file_size <= length * 8000 + 16384:
                    raise ValueError('unexpected channel MP3 size')
                path = staging / name
                unpack(archive, name, path)
                if file_hash(path) != hashes[name]:
                    raise ValueError('channel output fingerprint differs')
                verify_channel(ffprobe, path, length)
        if file_hash(source) != identity:
            raise Attention('conversation_source_changed')
        for name, (_, target) in by_name.items():
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                (staging / name).replace(target)
