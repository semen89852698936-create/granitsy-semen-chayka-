"""Vercel Flask entry point for the boundaries-course trainer.

The student's text is processed by this backend, never by a client-side API key.
This deployment is intended for a small free non-commercial pilot.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

from flask import Flask, Response, request, send_file

import ai_server as course

HERE = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=None)
app.config['MAX_CONTENT_LENGTH'] = course.MAX_BODY
course.MODEL = os.environ.get('OPENAI_MODEL', 'gpt-4.1-mini')


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
    enabled = bool(os.environ.get('OPENAI_API_KEY', '').strip())
    return json_response({'available': enabled, 'model': course.MODEL if enabled else None})


@app.post('/api/chat')
def chat():
    origin = request.headers.get('Origin', '')
    if origin and urlparse(origin).netloc != request.host:
        return json_response({'error': 'Недопустимый источник запроса.'}, 403)
    if not os.environ.get('OPENAI_API_KEY', '').strip():
        return json_response({'error': 'ИИ ещё не подключён: добавь OPENAI_API_KEY на сервере.'}, 503)
    if not request.is_json:
        return json_response({'error': 'Нужен JSON.'}, 415)
    if request.content_length is None or request.content_length > course.MAX_BODY:
        return json_response({'error': 'Недопустимый размер запроса.'}, 413)
    try:
        data = request.get_json(silent=False)
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
        output = course.openai_reply(scenario, round_no, message, history)
    except HTTPError as error:
        if error.code == 429:
            return json_response({'error': 'Лимит API временно исчерпан. Попробуй позже.'}, 503)
        return json_response({'error': 'Ошибка сервиса ИИ. Проверь API-ключ и доступ к модели.'}, 502)
    except (URLError, TimeoutError, OSError, ValueError, RuntimeError):
        return json_response({'error': 'Не удалось получить ответ ИИ. Попробуй ещё раз.'}, 502)
    return json_response(output)


@app.errorhandler(413)
def too_large(_error):
    return json_response({'error': 'Слишком большой запрос.'}, 413)


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('PORT', '8000')))
