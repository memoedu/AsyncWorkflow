"""Send a frozen batch to the configured SSH host, using the original WAV cache."""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import tempfile
import zipfile
from pathlib import Path

from .core import Attention, file_hash
from .meeting_audio_worker import unpack, wav_frames


def render_remote(worker, members, files, combined):
    host = worker['host']
    programs = [worker[k] for k in ('python', 'script', 'ffmpeg', 'scratch')]
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', host)
            or not all(p.startswith('/') for p in programs)):
        raise ValueError('invalid meeting audio worker configuration')
    python, script, ffmpeg, scratch = programs
    ssh = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
           '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
           host, shlex.join([python, '-I', '-B', script, ffmpeg, scratch])]
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
        with request.open('rb') as payload, response.open('wb') as received:
            subprocess.run(ssh, stdin=payload, stdout=received, stderr=subprocess.DEVNULL, check=True, timeout=1800)
        request.unlink()
        outputs = [*missing, combined]
        with zipfile.ZipFile(response) as archive:
            if (len(archive.infolist()) != len(outputs)
                    or set(archive.namelist()) != {p.name for p in outputs}
                    or any(info.compress_type != zipfile.ZIP_STORED or info.flag_bits & 1
                           for info in archive.infolist())):
                raise ValueError('unexpected meeting audio response')
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
