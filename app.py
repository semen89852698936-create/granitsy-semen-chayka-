"""Vercel Flask entry point for the boundaries-course trainer.

The student's text is processed by this backend, never by a client-side API key.
This deployment is intended for a small free non-commercial pilot.
"""
from __future__ import annotations

import json
import os
import time
import hmac
import base64
import hashlib
import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, quote
from urllib.request import Request, urlopen

from flask import Flask, Response, request, send_file
from course_quality import finalize

import ai_server as course

HERE = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=None)
app.config['MAX_CONTENT_LENGTH'] = course.MAX_BODY
course.MODEL = os.environ.get('OPENAI_MODEL', 'gpt-4.1-mini')
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-3.5-flash-lite')
# Browser-level pilot allowance. For public traffic ALSO enable Vercel WAF rate limiting:
# cookies can be removed and are not a substitute for network-level enforcement.
DAILY_LIMIT = max(3, min(60, int(os.environ.get('AI_DAILY_LIMIT', '18'))))
COOKIE = 'granitsy_ai_daily'
COOKIE_MAX_AGE = 86400


def _sign_token(value: str) -> str:
    # API key material never travels to the browser. Signature invalidated on rotation.
    secret = os.environ.get('COURSE_COOKIE_SECRET') or os.environ.get('GEMINI_API_KEY') or os.environ.get('OPENAI_API_KEY', '')
    return hmac.new(secret.encode('utf-8'), value.encode('ascii'), hashlib.sha256).hexdigest()


def _quota_usage() -> int:
    today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d')
    raw = request.cookies.get(COOKIE, '')
    try:
        date, amount, sig = raw.split('.')
        payload = date + '.' + amount
        if date == today and hmac.compare_digest(_sign_token(payload), sig):
            return max(0, min(DAILY_LIMIT, int(amount)))
    except (ValueError, TypeError, OverflowError):
        pass
    return 0


def _set_quota(response, count: int):
    today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d')
    payload = f'{today}.{count}'
    response.set_cookie(COOKIE, payload + '.' + _sign_token(payload),
                        max_age=COOKIE_MAX_AGE, secure=True, httponly=True,
                        samesite='Lax', path='/api/')
    return response


def active_provider():
    # A Gemini key takes priority, so the previous OpenAI key is not used.
    if os.environ.get('GEMINI_API_KEY', '').strip():
        return 'gemini', GEMINI_MODEL
    if os.environ.get('OPENAI_API_KEY', '').strip():
        return 'openai', course.MODEL
    return None, None


def gemini_reply(scenario, round_no, message, history):
    key = os.environ.get('GEMINI_API_KEY', '').strip()
    if not key:
        raise RuntimeError('GEMINI_API_KEY_MISSING')
    model = GEMINI_MODEL
    endpoint = 'https://generativelanguage.googleapis.com/v1beta/models/' + quote(model, safe='') + ':generateContent'
    payload = {
        'systemInstruction': {'parts': [{'text': course.SYSTEM}]},
        'contents': [{'role': 'user', 'parts': [{'text': course.make_prompt(scenario, round_no, message, history)}]}],
        'generationConfig': {
            'responseMimeType': 'application/json',
            # responseJsonSchema accepts JSON Schema keywords such as additionalProperties.
            # Legacy responseSchema expects an OpenAPI subset and can reject them with HTTP 400.
            'responseJsonSchema': course.RESPONSE_SCHEMA,
            'maxOutputTokens': 2048,
            'temperature': 0.6,
        },
    }
    req = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'x-goog-api-key': key, 'Content-Type': 'application/json', 'Accept': 'application/json'},
        method='POST'
    )
    with urlopen(req, timeout=35) as response:
        data = json.load(response)
    candidates = data.get('candidates') or []
    if not candidates:
        raise ValueError('Gemini returned no candidates')
    parts = candidates[0].get('content', {}).get('parts', [])
    raw = ''.join(p.get('text', '') for p in parts if isinstance(p, dict))
    return course.clean_result(raw, round_no)


def json_response(obj: dict, status: int = 200):
    body = json.dumps(obj, ensure_ascii=False)
    result = Response(body, status=status, content_type='application/json; charset=utf-8')
    result.headers['Cache-Control'] = 'no-store'
    result.headers['X-Content-Type-Options'] = 'nosniff'
    return result


@app.get('/')
@app.get('/index.html')
def home():
    response = send_file(HERE / 'index.html', mimetype='text/html')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.get('/api/status')
def status():
    provider, model = active_provider()
    return json_response({'available': bool(provider), 'model': model, 'provider': provider,
                          'daily_limit': DAILY_LIMIT, 'remaining': max(0, DAILY_LIMIT - _quota_usage())})


@app.post('/api/chat')
def chat():
    origin = request.headers.get('Origin', '')
    if origin and urlparse(origin).netloc != request.host:
        return json_response({'error': 'Недопустимый источник запроса.'}, 403)
    provider, _model = active_provider()
    if not provider:
        return json_response({'error': 'ИИ ещё не подключён: добавь GEMINI_API_KEY в настройки Vercel.'}, 503)
    if request.headers.get('Sec-Fetch-Site', 'same-origin') not in ('same-origin', 'none'):
        return json_response({'error': 'Недопустимый источник запроса.'}, 403)
    if not request.is_json:
        return json_response({'error': 'Нужен JSON.'}, 415)
    used = _quota_usage()
    if used >= DAILY_LIMIT:
        return json_response({'error': 'На сегодня тренировка завершена! Ты использовал все 18 ответов ИИ. Новые диалоги будут доступны завтра. Пока можешь повторить упражнения и прочитать рекомендации.', 'remaining': 0, 'daily_limit': DAILY_LIMIT}, 429)
    if request.content_length is None or request.content_length > course.MAX_BODY:
        return json_response({'error': 'Недопустимый размер запроса.'}, 413)
    try:
        data = request.get_json(silent=False)
        if not isinstance(data, dict) or data.get('consent') is not True:
            return json_response({'error': 'Перед ИИ-разговором требуется согласие на передачу учебного текста провайдеру.'}, 400)
        scenario, round_no, message, history = course.validate_request(data)
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        return json_response({'error': str(error)[:160]}, 400)

    # Best-effort anti-abuse only: Vercel functions may run on separate instances.
    # Before broad public release, add durable rate limits and bot protection.
    ip = request.headers.get('x-forwarded-for', request.remote_addr or 'unknown').split(',')[0].strip()
    now = time.monotonic()
    with course.mutex:
        bucket = course.hits[ip]
        while bucket and now - bucket[0] > course.WINDOW:
            bucket.popleft()
        if len(bucket) >= course.RATE_LIMIT:
            return json_response({'error': 'Лимит обращений к ИИ. Попробуй позже.'}, 429)
        bucket.append(now)
    try:
        output = gemini_reply(scenario, round_no, message, history) if provider == 'gemini' else course.openai_reply(scenario, round_no, message, history)
    except HTTPError as error:
        # Keep diagnostics on the server, without exposing keys or student messages.
        import logging
        provider_name, _ = active_provider()
        try:
            info = json.loads(error.read(4096).decode('utf-8', errors='replace'))
            detail = info.get('error', {}) if isinstance(info, dict) else {}
            upstream_status = str(detail.get('status', 'UNKNOWN'))[:60] if isinstance(detail, dict) else 'UNKNOWN'
        except (ValueError, OSError, UnicodeError):
            upstream_status = 'UNKNOWN'
        logging.warning('AI upstream HTTP %s provider=%s status=%s', error.code, provider_name, upstream_status)
        if error.code == 400:
            return json_response({'error': 'Модель отклонила формат запроса. Проверь журналы Vercel.'}, 502)
        if error.code == 429:
            return json_response({'error': 'Достигнут временный лимит запросов к модели. Попробуй позже.'}, 503)
        if error.code in (401, 403):
            return json_response({'error': 'Доступ к модели отклонён. Проверь ключ и квоты проекта в AI Studio.'}, 502)
        if error.code == 404:
            return json_response({'error': 'Модель недоступна для проекта. Проверь GEMINI_MODEL в Vercel.'}, 502)
        return json_response({'error': 'Сервис ИИ вернул ошибку. Проверь настройки модели и доступ.'}, 502)
    except (URLError, TimeoutError, OSError, ValueError, RuntimeError):
        return json_response({'error': 'Не удалось получить ответ ИИ. Попробуй ещё раз.'}, 502)
    output = finalize(output, scenario, round_no, message, history)
    output['daily_remaining'] = max(0, DAILY_LIMIT - used - 1)
    output['daily_limit'] = DAILY_LIMIT
    return _set_quota(json_response(output), used + 1)


@app.errorhandler(413)
def too_large(_error):
    return json_response({'error': 'Слишком большой запрос.'}, 413)


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('PORT', '8000')))
