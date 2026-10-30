"""One-time public-course smoke test (synthetic messages, no API key).

This script intentionally logs only HTTP/status and scoring metadata.
It uses three real Gemini replies and invalid requests for a WAF probe.
"""
from __future__ import annotations

import http.cookiejar
import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPCookieProcessor

BASE = 'https://granitsy-semen-chayka.vercel.app'
jar = http.cookiejar.CookieJar()
opener = build_opener(HTTPCookieProcessor(jar))
headers = {'Origin': BASE, 'Sec-Fetch-Site': 'same-origin', 'User-Agent': 'GranitsyReleaseAudit/1.0'}


def call(path, payload=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = Request(BASE + path, data=data,
                  headers={**headers, **({'Content-Type': 'application/json'} if data is not None else {})},
                  method='POST' if data is not None else 'GET')
    try:
        with opener.open(req, timeout=65) as resp:
            body = resp.read(300000)
            return resp.status, json.loads(body) if path != '/' else body.decode('utf-8', 'replace')
    except HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read(5000))
        except (ValueError, UnicodeError):
            return exc.code, {}
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f'Unable to reach public course: {type(exc).__name__}') from exc


def main():
    failures = []
    status_code, status = call('/api/status')
    print('STATUS', status_code, 'provider', status.get('provider'),
          'daily_limit', status.get('daily_limit'), 'remaining', status.get('remaining'))
    if status_code != 200 or status.get('provider') != 'gemini':
        failures.append('Published server does not report a ready Gemini connection')
    if status.get('daily_limit') != 18:
        failures.append('Production variable AI_DAILY_LIMIT is not 18: update Vercel and redeploy')
    initial_remaining = status.get('remaining')

    page_code, html = call('/')
    print('PAGE', page_code, 'bytes', len(html) if isinstance(html, str) else 0)
    if page_code != 200 or not isinstance(html, str):
        failures.append('Public homepage unavailable')
    elif not all(x in html for x in ['name="viewport"', '@media(max-width:360px)', 'aiRemaining', 'Индивидуальный разбор']):
        failures.append('Published frontend misses mobile or quota UI features')

    scenarios = json.loads(Path('scenarios.json').read_text('utf-8'))
    sid = scenarios[0]['id']
    messages = [
        'Сегодня я не смогу остаться после рабочего дня. Завтра смогу обсудить сроки.',
        'Я вижу, что задача срочная, но сегодня переработку не планирую.',
        'Моё решение остаётся прежним. Предлагаю согласовать, кто сможет помочь сегодня.',
    ]
    history = []
    completed = False
    for i, message in enumerate(messages, start=1):
        code, answer = call('/api/chat', {'scenario_id': sid, 'round': i,
                                          'message': message, 'history': history, 'consent': True})
        print('ROUND', i, 'HTTP', code, 'remaining', answer.get('daily_remaining'),
              'has_feedback', bool(answer.get('feedback')), 'has_reaction', bool(answer.get('reaction')))
        if code != 200:
            failures.append(f'AI round {i} failed (HTTP {code})')
            break
        if not all(isinstance(answer.get(key), str) and answer[key] for key in ('reaction', 'feedback', 'rewrite')):
            failures.append(f'AI round {i} missing feedback fields')
            break
        if any(type(v) is not int or v < 0 or v > 2 for v in answer.get('signals', {}).values()):
            failures.append(f'AI round {i} invalid scores')
            break
        if len(answer.get('signals', {})) != 3:
            failures.append(f'AI round {i} missing score dimensions')
            break
        if isinstance(initial_remaining, int) and answer.get('daily_remaining') != initial_remaining - i:
            failures.append(f'AI round {i} failed to decrement quota')
        history.append({'user': message, 'reply': answer['reaction'], 'signals': answer['signals']})
        if i == 3:
            completed = True
            summary, drill = answer.get('final_summary', ''), answer.get('next_practice', '')
            print('SUMMARY', 'present', bool(summary), 'length', len(summary),
                  'drill_present', bool(drill), 'drill_length', len(drill))
            if not summary or 'Динамика оценок' not in summary or not drill:
                failures.append('Three-round summary or adaptive drill missing')

    code2, after = call('/api/status')
    print('STATUS_AFTER', code2, 'remaining', after.get('remaining'))
    if completed and isinstance(initial_remaining, int) and after.get('remaining') != initial_remaining - 3:
        failures.append('Remaining count not persisted by signed cookie')

    # Invalid requests intentionally do not call Gemini; probe WAF by sending a small burst.
    waf_responses = {}
    for _ in range(14):
        code, _ = call('/api/chat', {'scenario_id': sid, 'round': 1, 'message': 'Тест', 'consent': False, 'history': []})
        waf_responses[code] = waf_responses.get(code, 0) + 1
        if code == 429:
            break
    print('WAF_PROBE_STATUS_COUNTS', waf_responses)
    # Vercel WAF "deny" returns HTTP 403; application daily quota returns HTTP 429.
    # Confirm the first invalid request was handled by Flask (400), then WAF blocked later.
    if not waf_responses.get(400) or not (waf_responses.get(403) or waf_responses.get(429)):
        failures.append('No published WAF rate-limit block observed after 14 invalid POST probes')

    print('AUDIT_RESULT', 'PASS' if not failures else 'FAIL', 'issues', len(failures))
    for issue in failures:
        print('ISSUE:', issue)
    if failures:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
