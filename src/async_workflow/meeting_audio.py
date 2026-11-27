"""Mixed-room audio uses native FUN-ASR diarization, never channel identity."""
from __future__ import annotations
import json
import logging
import subprocess
import wave
import zipfile
from pathlib import Path
from pydantic import BaseModel, ConfigDict
from . import asr, meeting_audio_transport, meeting_audio_worker, routing
from .core import Attention, Manifest, atomic, digest, file_hash, read_json

REVISION = 'fun-asr-room-1'
LOG = logging.getLogger(__name__)


class SpeakerName(BaseModel):
    model_config = ConfigDict(extra='forbid')
    speaker_id: str
    name: str
    evidence: str


class Names(BaseModel):
    model_config = ConfigDict(extra='forbid')
    speakers: list[SpeakerName]


def resolve_names(cfg, store, turns, profile):
    """Resolve anonymous acoustic IDs from dialogue evidence, not arrival order."""
    path = store.folder / 'speaker-names.json'
    saved = read_json(path)
    if saved:
        return saved['mapping']
    ids = {t['speaker_id'] for t in turns}
    candidates = [profile['self_name'], profile['other_name']]
    text = '\n'.join(t['text'] for t in turns)
    def validate(value):
        if len(value.speakers) != len(ids) or {s.speaker_id for s in value.speakers} != ids:
            raise ValueError('必须完整列出输入 speaker_id，每个一次')
        for speaker in value.speakers:
            if speaker.name not in [*candidates, '说话人'+speaker.speaker_id]:
                raise ValueError('名称只能是给定姓名，确实不明时保留说话人编号')
            if speaker.name in candidates and (not speaker.evidence or speaker.evidence not in text):
                raise ValueError('姓名对应必须附一段输入原文中的连续证据')
        return value
    result = routing.ask(cfg, Names,
        '输入是现场面对面录音，FUN-ASR 已按真实声音给出 speaker_id。这不是声道编号。'
        '仅根据完整对话中的称呼、自我介绍、问答指代和人物经历语境，将声学编号对应到给定姓名。'
        '被称呼者通常是另一位，不能把称呼误认成自称。禁止按编号、发言顺序、左右声道猜名字。'
        '不同编号可能是同一个人的误分段；背景插话和确实无法确定的编号保留为“说话人”加编号。'
        'evidence 使用能支持归属的原文连续片段；不改写转写，不审核会议内容。',
        {'participants': candidates, 'turns': turns}, validate)
    mapping = {s.speaker_id: s.name for s in result.speakers}
    atomic(path, {'mapping': mapping, 'evidence': result.model_dump()})
    return mapping


def prepare_audio(cfg, store, members):
    """Build or reuse the original batch audio before entering the ASR stage."""
    ffmpeg = cfg.ffprobe.with_name('ffmpeg.exe')
    files = []
    for member in members:
        if file_hash(Path(member['path'])) != member['id']:
            raise Attention('meeting_source_changed')
        files.append(store.folder / (member['id']+'.wav'))
    combined = store.folder / 'combined.wav'
    with store.lock:
        backend = 'cached'
        if not combined.exists():
            backend = 'local'
            try:
                worker = read_json(cfg.root / 'meeting-audio-worker.json')
                if worker:
                    meeting_audio_transport.render_remote(worker, members, files, combined)
                    backend = 'ssh'
            except (OSError, ValueError, TypeError, KeyError, EOFError, wave.Error, zipfile.BadZipFile,
                    subprocess.SubprocessError):
                LOG.warning('Meeting audio SSH worker unavailable, using local FFmpeg')
                backend = 'local-fallback'
        for member, audio in zip(members, files, strict=True):
            if not audio.exists():
                temporary = audio.with_suffix('.preparing.wav')
                source = Path(member['path'])
                meeting_audio_worker.normalize(ffmpeg, source, temporary)
                if file_hash(source) != member['id']:
                    raise Attention('meeting_source_changed')
                temporary.replace(audio)
        if not combined.exists():
            temporary = combined.with_suffix('.preparing.wav')
            meeting_audio_worker.combine(ffmpeg, files, temporary)
            temporary.replace(combined)
        boundaries, offset = [], 0
        for member, audio in zip(members, files, strict=True):
            # Duration after resampling is exact; original containers may include padding.
            length = round(meeting_audio_worker.wav_frames(audio)*1000/16000)
            boundaries.append({'id': member['id'], 'start_ms': offset, 'end_ms': offset+length})
            offset += length
        if backend != 'cached' or not store.read().get('audio_preparation'):
            # Pre-existing combined files remain authoritative on retry.
            store.update(audio_preparation=backend)
    return combined, boundaries


def prepare(cfg, members, profile):
    """One continuous mono input gives consistent acoustic IDs across file breaks."""
    identity = digest(json.dumps([REVISION, [m['id'] for m in members], profile['self_name'],
                                  profile['other_name']], ensure_ascii=False))
    store = Manifest(cfg, identity)
    path = store.folder / 'room-transcript.json'
    saved = read_json(path)
    if saved:
        return saved
    store.update(recording_type='meeting_asr_batch', sources=[m['path'] for m in members])
    combined, boundaries = prepare_audio(cfg, store, members)
    asr.recognize_filetrans(cfg, store, combined, model='fun-asr',
                           parameters={'diarization_enabled': True, 'speaker_count': 2})
    result = read_json(store.folder/'asr.json')
    turns = []
    for sentence in result['sentences']:
        if 'speaker_id' not in sentence or sentence['start_ms'] is None or sentence['end_ms'] is None:
            raise Attention('fun_asr_diarization_missing')
        turns.append({**sentence, 'speaker_id': str(sentence['speaker_id']), 'id': len(turns)})
    mapping = resolve_names(cfg, store, turns, profile)
    for turn in turns:
        turn['speaker'] = mapping[turn['speaker_id']]
    saved = {'turns': turns, 'speakers': mapping, 'boundaries': boundaries,
             'batch_id': identity, 'source_ids': [m['id'] for m in members],
             'text': '\n'.join(t['speaker']+'：'+t['text'] for t in turns),
             'identity_basis': 'native-acoustic-diarization-and-dialogue-context'}
    atomic(path, saved)
    atomic(store.folder/'named-transcript.txt', saved['text'], text=True)
    return saved
