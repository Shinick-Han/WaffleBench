"""Opt-in server-side Jev (TypeSafe System One) adapter for genuine inspection notes.

Shadow use only: judgements are audited against expert labels and never drive
equipment actions, numeric thresholds or campaign selection. Inputs are
genuine inspection note records restricted to a fixed field whitelist; arbitrary
dictionaries (for example simulator oracle records) are rejected, never
converted to text. The current synthetic simulator has no genuine notes, so the
integration reports itself unavailable there instead of producing mock output.

TYPESAFE_API_KEY is read from the process environment at request time and is
only placed in the Authorization header handed to the transport. It is never
stored on the client, in payloads, results, cache entries or error reasons.
Requests go only to the exact official ENDPOINT: any other endpoint is rejected
at construction, and the default transport refuses HTTP redirects so the
Authorization header is never forwarded to another location.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import statistics
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Mapping

MODEL = 'jev-1.13.0'
ENDPOINT = 'https://api.typesafe.ai/v1/systemone'
API_KEY_ENV = 'TYPESAFE_API_KEY'
NOTE_FIELDS = ('id', 'note', 'source', 'observed_at')
MAX_NOTE_CHARS = 4000
INSUFFICIENT = 'insufficient_information'
PROBABILITY_SUM_TOLERANCE = 0.01
ARGMAX_TOLERANCE = 1e-4
# Sources that would indicate generated or oracle-derived text, not a genuine note.
FORBIDDEN_SOURCE_TOKENS = ('synthetic', 'simulat', 'oracle', 'mock', 'generated', 'fabricated')
# Statuses after which further requests are pointless or harmful (bad key, throttling).
HALT_STATUSES = (401, 403, 429)
REDIRECT_REFUSED = 'redirect_refused'
CACHE_SCHEMA = 1

ROUTE_CRITERIA = {
    'sem_review': 'A valid optical image describes a visible particle, bridge, crack or missing pattern that needs high-resolution morphology review.',
    'electrical_review': 'The note explicitly describes an electrical failure or suspected buried defect without a visible surface anomaly.',
    'repeat_image': 'Charging, defocus, motion blur or acquisition contamination invalidates the current image. Reacquire before calling it a physical defect.',
    'no_further_review': 'A valid inspection is explicitly normal, reference-aligned, and has no other concern.',
    INSUFFICIENT: 'There is insufficient evidence to select any of the other routes.',
}
DEFAULT_QUESTIONS = {
    'route': {
        'type': 'choice',
        'instructions': 'Select the evidence-supported next inspection route using only `inspection_note`. Invalid acquisition requires repeat_image before physical interpretation. Missing evidence requires insufficient_information. Do not invent observations.',
        'criteria': ROUTE_CRITERIA,
    },
    'acquisition_invalid': {
        'type': 'noul',
        'instructions': 'Does `inspection_note` explicitly say the current acquisition is invalid due to an imaging artefact?',
        'criteria': {'true': 'The note explicitly identifies invalid acquisition or unreliable imaging.',
                     'false': 'No explicit acquisition-invalid statement is present.'},
    },
}

# transport(url, body, headers, timeout_s) -> (http_status, response_body_bytes)
Transport = Callable[[str, bytes, Mapping[str, str], float], 'tuple[int, bytes]']


class JevSchemaError(ValueError):
    """The response does not match the requested typed questions."""


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _sha256(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_probability(value) -> bool:
    return _is_number(value) and 0 <= value <= 1


# ---------------------------------------------------------------- note records

@dataclass(frozen=True)
class NoteRecord:
    id: str
    note: str
    source: str
    observed_at: str


def note_record(raw: Mapping) -> NoteRecord:
    """Validate a genuine inspection note record. Any non-whitelisted key is an error."""
    if isinstance(raw, NoteRecord):
        return raw
    if not isinstance(raw, Mapping):
        raise TypeError('note record must be a mapping')
    extra = set(raw) - set(NOTE_FIELDS)
    missing = set(NOTE_FIELDS) - set(raw)
    if extra or missing:
        raise ValueError(f'note record fields must be exactly {NOTE_FIELDS}; extra={sorted(map(str, extra))} missing={sorted(missing)}')
    for field in NOTE_FIELDS:
        if not isinstance(raw[field], str) or not raw[field].strip():
            raise ValueError(f'note record field {field!r} must be a non-empty string')
    if len(raw['note']) > MAX_NOTE_CHARS:
        raise ValueError(f'note exceeds {MAX_NOTE_CHARS} characters')
    source = raw['source'].lower()
    if any(token in source for token in FORBIDDEN_SOURCE_TOKENS):
        raise ValueError('note source indicates generated or oracle-derived text, not a genuine note')
    try:
        observed = datetime.fromisoformat(raw['observed_at'].replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('observed_at must be an ISO 8601 timestamp') from exc
    if observed.tzinfo is None:
        raise ValueError('observed_at must carry a timezone')
    return NoteRecord(**{field: raw[field] for field in NOTE_FIELDS})


def integration_status(notes: Iterable | None) -> dict:
    """Availability of the semantic integration for a data source. No notes means unavailable."""
    records = [] if notes is None else list(notes)
    if not records:
        return {'status': 'unavailable', 'reason': 'no_genuine_notes', 'notes': 0}
    validated = [note_record(r) for r in records]
    return {'status': 'available', 'reason': None, 'notes': len(validated)}


SIMULATOR_INTEGRATION = {'status': 'unavailable', 'reason': 'no_genuine_notes', 'notes': 0,
                         'detail': 'The synthetic inspection simulator produces no genuine inspection notes.'}


# ---------------------------------------------------------------- rubric

def validate_questions(questions: Mapping) -> dict:
    """Check the typed rubric: at least one Choice, every Choice has the insufficient-information option."""
    if not isinstance(questions, Mapping) or not questions:
        raise ValueError('questions must be a non-empty mapping')
    has_choice = False
    for qid, question in questions.items():
        if not isinstance(qid, str) or not qid:
            raise ValueError('question ids must be non-empty strings')
        qtype = question.get('type')
        if qtype not in ('choice', 'noul'):
            raise ValueError(f'question {qid!r}: only choice and noul are supported')
        if not question.get('instructions'):
            raise ValueError(f'question {qid!r}: instructions required')
        if qtype == 'choice':
            has_choice = True
            criteria = question.get('criteria')
            if not isinstance(criteria, Mapping) or len(criteria) < 2 or len(criteria) > 255:
                raise ValueError(f'question {qid!r}: choice needs 2-255 criteria')
            if INSUFFICIENT not in criteria:
                raise ValueError(f'question {qid!r}: choice must offer {INSUFFICIENT!r}')
        elif 'criteria' in question and set(question['criteria']) - {'true', 'false'}:
            raise ValueError(f'question {qid!r}: noul criteria keys are true/false')
    if not has_choice:
        raise ValueError('rubric needs at least one choice question')
    return json.loads(_canonical(questions))


def rubric_hash(questions: Mapping) -> str:
    return _sha256(validate_questions(questions))


def request_state(record: NoteRecord) -> dict:
    """The only content sent to the model: the note text. id/source/observed_at stay local provenance."""
    return {'inspection_note': record.note}


def cache_key(model: str, rubric_digest: str, state: Mapping) -> str:
    return _sha256({'model': model, 'rubric_hash': rubric_digest, 'state_hash': _sha256(state)})


# ---------------------------------------------------------------- validation

def validate_response(response, questions: Mapping, model: str = MODEL) -> dict:
    """Strict schema and probability checks. Returns the normalized answers and usage."""
    if not isinstance(response, Mapping):
        raise JevSchemaError('response must be an object')
    if response.get('model') != model:
        raise JevSchemaError('unexpected model id')
    answers = response.get('answers')
    if not isinstance(answers, Mapping) or set(answers) != set(questions):
        raise JevSchemaError('answers must match the requested question ids exactly')
    normalized = {}
    for qid, question in questions.items():
        answer = answers[qid]
        if not isinstance(answer, Mapping) or answer.get('type') != question['type']:
            raise JevSchemaError(f'{qid}: answer type mismatch')
        if question['type'] == 'choice':
            probs = answer.get('probabilities')
            if not isinstance(probs, Mapping) or set(probs) != set(question['criteria']):
                raise JevSchemaError(f'{qid}: probability keys must equal the criteria')
            if not all(_is_probability(v) for v in probs.values()):
                raise JevSchemaError(f'{qid}: probabilities must be finite and in [0, 1]')
            if abs(sum(probs.values()) - 1) > PROBABILITY_SUM_TOLERANCE:
                raise JevSchemaError(f'{qid}: probabilities do not sum to one')
            choice = answer.get('choice')
            if choice not in probs:
                raise JevSchemaError(f'{qid}: choice is not a requested option')
            if probs[choice] + ARGMAX_TOLERANCE < max(probs.values()):
                raise JevSchemaError(f'{qid}: choice is not the maximum-probability option')
            if not _is_probability(answer.get('confidence')):
                raise JevSchemaError(f'{qid}: confidence must be finite and in [0, 1]')
            normalized[qid] = {'type': 'choice', 'choice': choice, 'confidence': float(answer['confidence']),
                               'probabilities': {k: float(v) for k, v in probs.items()}}
        else:
            if not _is_probability(answer.get('noul')):
                raise JevSchemaError(f'{qid}: noul must be finite and in [0, 1]')
            normalized[qid] = {'type': 'noul', 'noul': float(answer['noul'])}
    usage = response.get('usage')
    if not isinstance(usage, Mapping) or not all(
            isinstance(usage.get(k), int) and not isinstance(usage.get(k), bool) and usage[k] >= 0
            for k in ('input_tokens', 'output_tokens')):
        raise JevSchemaError('usage token counts must be non-negative integers')
    return {'answers': normalized,
            'usage': {'input_tokens': usage['input_tokens'], 'output_tokens': usage['output_tokens']}}


# ---------------------------------------------------------------- transport

class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: no second request is built, so credentials are never re-sent.

    Returning None makes urllib surface the 3xx response as an HTTPError status.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Replaces the default HTTPRedirectHandler (build_opener drops a default when a subclass is given).
_OPENER = urllib.request.build_opener(_RefuseRedirects)


def _post(opener: urllib.request.OpenerDirector, url: str, body: bytes, headers: Mapping[str, str],
          timeout_s: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, body, headers=dict(headers), method='POST')
    try:
        with opener.open(request, timeout=timeout_s) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code, b''


def urllib_transport(url: str, body: bytes, headers: Mapping[str, str], timeout_s: float) -> tuple[int, bytes]:
    """Default stdlib HTTPS transport to the official endpoint only, with redirects refused.

    HTTP errors and refused 3xx redirects are returned as statuses, not raised.
    """
    if url != ENDPOINT:
        raise ValueError('default transport only posts to the official Jev endpoint')
    return _post(_OPENER, url, body, headers, timeout_s)


# ---------------------------------------------------------------- client

class JevClient:
    """Typed evidence-routing judgements over genuine notes, with explicit abstention.

    `enabled` must be True for any request to be made; otherwise every judgement is
    `unavailable`. Failures (timeout, transport, HTTP, malformed or schema-invalid
    responses) return `abstain` with a fixed reason and are never cached.
    """

    def __init__(self, *, enabled: bool = False, transport: Transport | None = None,
                 questions: Mapping | None = None, model: str = MODEL, endpoint: str = ENDPOINT,
                 timeout_s: float = 20.0, cache_dir: str | os.PathLike | None = None,
                 max_requests: int | None = None, environ: Mapping[str, str] | None = None,
                 clock: Callable[[], float] = time.perf_counter):
        if model != MODEL:
            raise ValueError(f'model is pinned to {MODEL}')
        if endpoint != ENDPOINT:
            raise ValueError(f'endpoint is pinned to the official {ENDPOINT}')
        if not (_is_number(timeout_s) and timeout_s > 0):
            raise ValueError('timeout_s must be positive')
        if max_requests is not None and (not isinstance(max_requests, int) or max_requests < 0):
            raise ValueError('max_requests must be a non-negative integer')
        self.enabled = bool(enabled)
        self.transport = transport or urllib_transport
        self.questions = validate_questions(questions or DEFAULT_QUESTIONS)
        self.model = model
        self.endpoint = endpoint
        self.timeout_s = float(timeout_s)
        self.rubric_hash = _sha256(self.questions)
        self.cache_dir = None
        if cache_dir is not None:
            self.cache_dir = Path(cache_dir)
            if not self.cache_dir.is_dir():
                raise ValueError('cache_dir must be an existing caller-owned directory')
        self.max_requests = max_requests
        env = os.environ if environ is None else environ
        # Read at request time; the client keeps no attribute holding the key itself.
        self._read_key = lambda: env.get(API_KEY_ENV)
        self._clock = clock
        self._memory: dict[str, dict] = {}
        self._halted: str | None = None
        self.requests_made = 0

    def __repr__(self) -> str:
        return f'JevClient(model={self.model!r}, enabled={self.enabled}, rubric_hash={self.rubric_hash[:12]!r})'

    def payload(self, record: NoteRecord) -> dict:
        return {'model': self.model, 'state': request_state(record), 'questions': self.questions}

    def judge(self, raw_record) -> dict:
        record = note_record(raw_record)
        state = request_state(record)
        key = cache_key(self.model, self.rubric_hash, state)
        base = {'note_id': record.id, 'source': record.source, 'observed_at': record.observed_at,
                'model': self.model, 'rubric_hash': self.rubric_hash, 'cache_key': key,
                'shadow': True, 'cache_hit': False, 'latency_ms': None}
        if not self.enabled:
            return self._result(base, 'unavailable', 'offline')
        cached = self._cache_get(key)
        if cached is not None:
            return self._result(base, 'ok', None, cached, cache_hit=True)
        api_key = self._read_key()
        if not api_key:
            return self._result(base, 'unavailable', 'missing_api_key')
        if self._halted:
            return self._result(base, 'abstain', self._halted)
        if self.max_requests is not None and self.requests_made >= self.max_requests:
            return self._result(base, 'abstain', 'request_budget_exhausted')
        body = _canonical(self.payload(record))
        headers = {'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json'}
        self.requests_made += 1
        tick = self._clock()
        try:
            status, raw = self.transport(self.endpoint, body, headers, self.timeout_s)
        except (TimeoutError, socket.timeout):
            return self._result(base, 'abstain', 'timeout', latency=self._clock() - tick)
        except urllib.error.URLError as exc:
            reason = 'timeout' if isinstance(exc.reason, (TimeoutError, socket.timeout)) else 'transport_error'
            return self._result(base, 'abstain', reason, latency=self._clock() - tick)
        except OSError:
            return self._result(base, 'abstain', 'transport_error', latency=self._clock() - tick)
        latency = self._clock() - tick
        if status != 200:
            reason = REDIRECT_REFUSED if 300 <= status < 400 else f'http_{status}'
            if status in HALT_STATUSES or reason == REDIRECT_REFUSED:
                self._halted = f'halted_after_{reason}'
            return self._result(base, 'abstain', reason, latency=latency)
        try:
            parsed = validate_response(json.loads(raw), self.questions, self.model)
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            reason = 'schema_error' if isinstance(exc, JevSchemaError) else 'malformed_response'
            return self._result(base, 'abstain', reason, latency=latency)
        self._cache_put(key, parsed)
        return self._result(base, 'ok', None, parsed, latency=latency)

    def judge_batch(self, records: Iterable) -> list[dict]:
        """One request per note carrying every typed question; identical notes share the cache."""
        return [self.judge(record) for record in records]

    # -- results

    def _result(self, base: dict, status: str, reason: str | None, parsed: dict | None = None,
                *, cache_hit: bool = False, latency: float | None = None) -> dict:
        result = dict(base, status=status, reason=reason, cache_hit=cache_hit,
                      latency_ms=None if latency is None else latency * 1000,
                      answers=None, usage=None, route=None, route_probabilities=None, route_confidence=None,
                      actionable=False)
        if parsed is not None:
            result['answers'] = parsed['answers']
            # Cache hits cost no new tokens; the original usage stays in `answers` provenance only.
            result['usage'] = {'input_tokens': 0, 'output_tokens': 0} if cache_hit else parsed['usage']
            route = next(a for a in parsed['answers'].values() if a['type'] == 'choice')
            result.update(route=route['choice'], route_probabilities=route['probabilities'],
                          route_confidence=route['confidence'], actionable=route['choice'] != INSUFFICIENT)
        return result

    # -- cache (validated successes only; never the key)

    def _cache_path(self, key: str) -> Path | None:
        return None if self.cache_dir is None else self.cache_dir / f'jev-{key}.json'

    def _cache_get(self, key: str) -> dict | None:
        if key in self._memory:
            return self._memory[key]
        path = self._cache_path(key)
        if path is None or not path.is_file():
            return None
        try:
            entry = json.loads(path.read_text(encoding='utf-8'))
            if (entry.get('schema') != CACHE_SCHEMA or entry.get('cache_key') != key
                    or entry.get('model') != self.model or entry.get('rubric_hash') != self.rubric_hash):
                return None
            parsed = validate_response({'model': self.model, **entry['response']}, self.questions, self.model)
        except (OSError, ValueError, TypeError, KeyError):
            return None  # Corrupt or foreign entry: treat as a miss.
        self._memory[key] = parsed
        return parsed

    def _cache_put(self, key: str, parsed: dict) -> None:
        self._memory[key] = parsed
        path = self._cache_path(key)
        if path is None:
            return
        entry = {'schema': CACHE_SCHEMA, 'cache_key': key, 'model': self.model, 'rubric_hash': self.rubric_hash,
                 'response': parsed}
        fd, tmp = tempfile.mkstemp(dir=self.cache_dir, prefix='.jev-', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True))
            os.replace(tmp, path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)


# ---------------------------------------------------------------- shadow audit

def _nearest_rank(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * q) - 1)]


def shadow_audit(judgements: Iterable[Mapping], expert_labels: Mapping[str, str],
                 options: Iterable[str] | None = None,
                 thresholds: Iterable[float] = (0.0, 0.5, 0.7, 0.9)) -> dict:
    """Compare shadow judgements against expert route labels. Nothing here feeds decisions.

    Coverage counts actionable `ok` judgements (not abstained, unavailable or
    insufficient-information) over labelled notes. Accuracy and Brier are on the
    covered/answered subset with explicit counts.
    """
    options = list(options or ROUTE_CRITERIA)
    for note_id, label in expert_labels.items():
        if label not in options:
            raise ValueError(f'expert label for {note_id!r} is not a rubric option')
    rows = list(judgements)
    labelled = [j for j in rows if j.get('note_id') in expert_labels]
    statuses: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for j in labelled:
        statuses[j['status']] = statuses.get(j['status'], 0) + 1
        if j.get('reason'):
            reasons[j['reason']] = reasons.get(j['reason'], 0) + 1
    answered = [j for j in labelled if j['status'] == 'ok']
    covered = [j for j in answered if j.get('actionable')]
    errors = [j for j in covered if j['route'] != expert_labels[j['note_id']]]
    confusion: dict[str, dict[str, int]] = {}
    for j in answered:
        row = confusion.setdefault(expert_labels[j['note_id']], {})
        row[j['route']] = row.get(j['route'], 0) + 1
    briers = [sum((j['route_probabilities'].get(o, 0.0) - (o == expert_labels[j['note_id']])) ** 2 for o in options)
              for j in answered]
    risk_coverage = []
    for t in thresholds:
        kept = [j for j in covered if j['route_confidence'] >= t]
        wrong = sum(j['route'] != expert_labels[j['note_id']] for j in kept)
        risk_coverage.append({'confidence_threshold': t, 'kept': len(kept),
                              'coverage': len(kept) / len(labelled) if labelled else None,
                              'risk': wrong / len(kept) if kept else None})
    latencies = [j['latency_ms'] for j in labelled if j.get('latency_ms') is not None and not j.get('cache_hit')]
    usage = [j['usage'] for j in labelled if j.get('usage')]
    if not labelled:
        status = 'unavailable'
    elif not answered:
        status = 'no_answers'
    else:
        status = 'complete'
    return {
        'kind': 'jev_shadow_audit', 'status': status, 'affects_decisions': False,
        'judgements': len(rows), 'labelled': len(labelled), 'unlabelled_judgements': len(rows) - len(labelled),
        'answered': len(answered), 'covered': len(covered),
        'coverage': len(covered) / len(labelled) if labelled else None,
        'errors': len(errors), 'error_rate_covered': len(errors) / len(covered) if covered else None,
        'accuracy_answered': (sum(j['route'] == expert_labels[j['note_id']] for j in answered) / len(answered)
                              if answered else None),
        'brier_answered': statistics.fmean(briers) if briers else None,
        'insufficient_information': sum(j['route'] == INSUFFICIENT for j in answered),
        'status_counts': statuses, 'abstention_reasons': reasons, 'confusion': confusion,
        'risk_coverage': risk_coverage,
        'cache_hits': sum(bool(j.get('cache_hit')) for j in labelled),
        'input_tokens': sum(u['input_tokens'] for u in usage),
        'output_tokens': sum(u['output_tokens'] for u in usage),
        'latency_ms_p50': statistics.median(latencies) if latencies else None,
        'latency_ms_p95_nearest_rank': _nearest_rank(latencies, 0.95),
        'limitations': ['Shadow judgements only; no equipment action or selection uses them.',
                        'Choice confidence is distribution concentration, not measured accuracy.'],
    }
