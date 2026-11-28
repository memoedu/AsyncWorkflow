import json
import zipfile

import pytest

from async_workflow import conversation_asr, meeting_audio_transport, meeting_audio_worker
from async_workflow.core import Manifest, Settings, UnknownOutcome, atomic, file_hash


def setup_source(tmp_path, monkeypatch):
    cfg = Settings(root=tmp_path/'runtime', artifacts=tmp_path/'out', inbox=tmp_path/'in',
                   ffprobe=tmp_path/'ffprobe.exe', codex=tmp_path/'codex.exe',
                   conversation_channel_profiles={'Call.wav': {'0': 'Left', '1': 'Right'}})
    cfg.prepare()
    source = cfg.inbox/'Call.wav'
    source.write_bytes(b'sealed stereo fixture')
    store = Manifest(cfg, file_hash(source))
    atomic(cfg.root/'meeting-audio-worker.json', {'stereo_chunks': True})
    media = {'streams': [{'codec_type': 'audio', 'channels': 2}], 'format': {'duration': '300.250'}}
    monkeypatch.setattr(conversation_asr.subprocess, 'run',
                        lambda *a, **kw: type('Probe', (), {'stdout': json.dumps(media)})())
    return cfg, store, source


def test_original_recognizer_reuses_existing_part_and_keeps_299_second_offsets(tmp_path, monkeypatch):
    cfg, store, source = setup_source(tmp_path, monkeypatch)
    first = store.folder/'conversation-channels'/'0-000000'/'audio.mp3'
    first.parent.mkdir(parents=True)
    first.write_bytes(b'legacy audio')
    jobs = []

    def render(worker, path, identity, chunks, ffprobe, folder):
        jobs.extend(chunks)
        for _, _, _, output in chunks:
            output.parent.mkdir(parents=True)
            output.write_bytes(b'new audio')

    def receipt(config, audio, folder):
        return {'events': [{'output': {'sentence': {'sentence_id': 1, 'sentence_end': True,
            'text': 'fixture', 'begin_time': 11,
            'words': [{'text': 'fixture', 'begin_time': 11, 'end_time': 70}]}}}]}

    monkeypatch.setattr(meeting_audio_transport, 'render_channels', render)
    monkeypatch.setattr(conversation_asr, 'chunk_receipt', receipt)
    output = conversation_asr.recognize(cfg, store, source)
    assert [job[:3] for job in jobs] == [(0, 299, 1.25), (1, 0, 299), (1, 299, 1.25)]
    assert [(w['channel'], w['start_ms']) for w in output['words']] == [
        (0, 11), (0, 299011), (1, 11), (1, 299011)]
    assert first.read_bytes() == b'legacy audio'
    assert store.read()['channel_audio_preparation'] == {'backend': 'ssh', 'chunks': 3}
    assert conversation_asr.recognize(cfg, store, source) == output
    assert len(jobs) == 3


def test_unknown_missing_audio_is_not_regenerated_or_submitted(tmp_path, monkeypatch):
    cfg, store, source = setup_source(tmp_path, monkeypatch)
    first = store.folder/'conversation-channels'/'0-000000'
    atomic(first/'state.json', {'submission': 'unknown', 'audio_sha256': 'previous'})
    monkeypatch.setattr(meeting_audio_transport, 'render_channels', lambda *a: pytest.fail('unexpected SSH work'))
    monkeypatch.setattr(meeting_audio_worker, 'encode_channel', lambda *a: pytest.fail('unexpected work'))
    monkeypatch.setattr(conversation_asr, 'chunk_receipt', lambda *a: pytest.fail('unexpected submission'))
    with pytest.raises(UnknownOutcome, match='unknown_no_replay'):
        conversation_asr.recognize(cfg, store, source)
    assert not (first/'audio.mp3').exists()


def test_channel_receipt_cannot_install_a_wrong_part(tmp_path, monkeypatch):
    source, destination = tmp_path/'source', tmp_path/'output'/'audio.mp3'
    source.write_bytes(b'sealed stereo')

    def wrong(command, request, response):
        assert '--channels' in command[-1]
        with zipfile.ZipFile(request) as archive:
            manifest = json.loads(archive.read('channels.json'))
        with zipfile.ZipFile(response, 'w') as archive:
            archive.writestr('channels.json', json.dumps({**manifest, 'outputs': {'0-000000.mp3': 'wrong'}}))
            archive.writestr('0-000000.mp3', b'not the expected audio')

    monkeypatch.setattr(meeting_audio_transport, 'exchange', wrong)
    with pytest.raises(ValueError, match='fingerprint'):
        meeting_audio_transport.render_channels(
            {'host': 'mac', 'python': '/python', 'script': '/worker', 'ffmpeg': '/ffmpeg', 'scratch': '/scratch'},
            source, file_hash(source), [(0, 0, 299, destination)], 'ffprobe', tmp_path,
        )
    assert not destination.exists()
