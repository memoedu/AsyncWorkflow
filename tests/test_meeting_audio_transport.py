import zipfile
import wave
from pathlib import Path

import pytest

from async_workflow import meeting_audio, meeting_audio_transport
from async_workflow.core import Manifest, Settings, atomic, file_hash


def write_wave(path, samples):
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(samples)


def test_rejects_pcm_returned_in_wrong_order_without_installing(tmp_path, monkeypatch):
    files, members, samples = [], [], [b'\x01\x00'*32, b'\x02\x00'*48]
    for index, data in enumerate(samples):
        source = tmp_path / str(index)
        write_wave(source, data)
        identity = file_hash(source)
        members.append({'id': identity, 'path': str(source)})
        files.append(tmp_path / (identity + '.wav'))
    combined = tmp_path / 'combined.wav'

    def wrong_order(command, *, stdin, stdout, **kwargs):
        assert 'StrictHostKeyChecking=yes' in command
        with zipfile.ZipFile(stdin) as sent:
            assert set(sent.namelist()) == {'order.json', *(m['id']+'.source' for m in members)}
        wrong = tmp_path / 'wrong.wav'
        write_wave(wrong, b''.join(reversed(samples)))
        with zipfile.ZipFile(stdout, 'w') as zip_response:
            for member, file in zip(members, files, strict=True):
                zip_response.write(member['path'], file.name)
            zip_response.write(wrong, 'combined.wav')

    monkeypatch.setattr(meeting_audio_transport.subprocess, 'run', wrong_order)
    with pytest.raises(ValueError, match='ordered members'):
        meeting_audio_transport.render_remote(
            {'host': 'mac', 'python': '/python', 'script': '/script', 'ffmpeg': '/ffmpeg', 'scratch': '/scratch'},
            members, files, combined,
        )
    assert all(not p.exists() for p in [*files, combined])


def test_cached_audio_does_not_restart_ssh_or_change_old_offsets(tmp_path, monkeypatch):
    cfg = Settings(root=tmp_path / 'runtime', artifacts=tmp_path / 'artifacts', inbox=tmp_path / 'inbox',
                   ffprobe=Path('ffprobe.exe'), codex=Path('codex.exe'))
    cfg.prepare()
    member = cfg.inbox / 'source.wav'
    write_wave(member, b'\x0a\x00'*32)
    identity = file_hash(member)
    store = Manifest(cfg, identity)
    store.update(audio_preparation='local-fallback', existing_field=True)
    cached = store.folder / (identity + '.wav')
    combined = store.folder / 'combined.wav'
    write_wave(cached, b'\x0a\x00'*32)
    write_wave(combined, b'\x0a\x00'*32)
    atomic(cfg.root/'meeting-audio-worker.json', {'host': 'mac'})
    monkeypatch.setattr(meeting_audio_transport, 'render_remote', lambda *a: pytest.fail('completed PCM repeated'))
    assert meeting_audio.prepare_audio(cfg, store, [{'id': identity, 'path': str(member)}]) == (
        combined, [{'id': identity, 'start_ms': 0, 'end_ms': 2}],
    )
    assert store.read()['audio_preparation'] == 'local-fallback'
    assert store.read()['existing_field'] is True
