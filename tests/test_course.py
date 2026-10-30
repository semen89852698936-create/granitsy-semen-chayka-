"""Offline regression tests; no real Gemini API calls and no API secrets."""
import os
import unittest
from unittest.mock import patch

os.environ['GEMINI_API_KEY'] = 'test-secret-not-real'
os.environ['GEMINI_MODEL'] = 'gemini-3.5-flash-lite'

from course_quality import finalize, practice_for_weakness, neutralize_praise
import ai_server
import app as course_app


class TrainerTests(unittest.TestCase):
    def setUp(self):
        self.client = course_app.app.test_client()
        self.scenario = next(iter(ai_server.SCENARIOS.values()))
        self.sid = self.scenario['id']

    def payload(self, round_no=1, consent=True, history=None):
        return {'scenario_id': self.sid, 'round': round_no,
                'message': 'Сегодня я не останусь.', 'history': history or [],
                'consent': consent}

    def fake_reply(self, scenario, n, message, history):
        return {'reaction': 'Я понимаю. Возможно, обсудим завтра.',
                'feedback': 'Ты обозначил решение, но ещё можно уточнить срок.',
                'rewrite': 'Сегодня я не останусь. Вернёмся к вопросу завтра.',
                'signals': {'clarity': 2, 'respect': 2, 'boundaries': 2},
                'final_summary': 'В первом ответе было неясно. Во втором ты повысил тон. В третьем ты обозначил решение.',
                'next_practice': ''}

    def test_status(self):
        rv = self.client.get('/api/status')
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.json['provider'], 'gemini')

    def test_consent_required(self):
        rv = self.client.post('/api/chat', json=self.payload(consent=False))
        self.assertEqual(rv.status_code, 400)

    def test_cross_origin_blocked(self):
        rv = self.client.post('/api/chat', json=self.payload(), headers={'Origin': 'https://example.org'})
        self.assertEqual(rv.status_code, 403)

    def test_valid_chat_and_cookie_quota(self):
        with patch.object(course_app, 'gemini_reply', side_effect=self.fake_reply):
            rv = self.client.post('/api/chat', json=self.payload())
        self.assertEqual(rv.status_code, 200, rv.get_data(as_text=True))
        self.assertIn('granitsy_ai_daily=', rv.headers.get('Set-Cookie', ''))
        self.assertEqual(rv.json['signals']['clarity'], 2)

    def test_three_round_practice_with_weak_tone(self):
        history = [{'user': 'Я не уверен.', 'reply': 'Нужно сегодня.', 'signals': {'clarity': 1, 'respect': 2, 'boundaries': 1}},
                   {'user': 'Отстаньте!', 'reply': 'Давайте спокойнее.', 'signals': {'clarity': 1, 'respect': 0, 'boundaries': 2}}]
        out = self.fake_reply(self.scenario, 3, 'Нет, не останусь', history)
        out['final_summary'] = 'Третий ответ идеален. Второй был резким.'
        reviewed = finalize(out, self.scenario, 3, 'Нет, не останусь', history)
        self.assertNotIn('идеален', reviewed['final_summary'])
        self.assertIn('Динамика оценок', reviewed['final_summary'])
        self.assertIn('раунда 2', reviewed['next_practice'])
        self.assertIn('тона', reviewed['next_practice'])

    def test_no_overpraise(self):
        self.assertNotIn('идеален', neutralize_praise('Этот ответ идеален. Ты чётко сказал нет.'))
        self.assertIn('чётко', neutralize_praise('Этот ответ идеален. Ты чётко сказал нет.'))


    def test_full_three_round_dialogue_and_remaining_count(self):
        """Simulates all three POST requests with a fake Gemini provider."""
        sentences = [
            'Пока мне сложно решиться.',
            'Отстаньте, я всё сказал!',
            'Сегодня не смогу. Предлагаю вернуться к этому в понедельник.'
        ]
        history = []
        scores = [
            {'clarity': 1, 'respect': 2, 'boundaries': 1},
            {'clarity': 2, 'respect': 0, 'boundaries': 2},
            {'clarity': 2, 'respect': 2, 'boundaries': 2},
        ]

        def realistic_reply(scenario, n, message, past):
            response = self.fake_reply(scenario, n, message, past)
            response['signals'] = scores[n - 1]
            return response

        with patch.object(course_app, 'gemini_reply', side_effect=realistic_reply):
            for idx, sentence in enumerate(sentences, start=1):
                body = self.payload(round_no=idx, history=history)
                body['message'] = sentence
                response = self.client.post('/api/chat', json=body)
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                self.assertEqual(response.json['daily_limit'], course_app.DAILY_LIMIT)
                self.assertEqual(response.json['daily_remaining'], course_app.DAILY_LIMIT - idx)
                self.assertEqual(len(response.json['signals']), 3)
                history.append({'user': sentence, 'reply': response.json['reaction'],
                                'signals': response.json['signals']})
            result = response.json
            self.assertIn('Динамика оценок', result['final_summary'])
            self.assertTrue(result['next_practice'])
            self.assertIn('раунда', result['next_practice'])
        quota = self.client.get('/api/status')
        self.assertEqual(quota.status_code, 200)
        self.assertEqual(quota.json['remaining'], course_app.DAILY_LIMIT - 3)

    def test_six_complete_sessions_then_daily_limit(self):
        """18 successful turns, then 19th is blocked without invoking Gemini."""
        self.assertEqual(course_app.DAILY_LIMIT, 18)
        with patch.object(course_app, 'gemini_reply', side_effect=self.fake_reply) as model:
            for n in range(18):
                response = self.client.post('/api/chat', json=self.payload())
                self.assertEqual(response.status_code, 200, f'turn {n + 1}: {response.get_data(as_text=True)}')
                self.assertEqual(response.json['daily_remaining'], 17 - n)
            exhausted = self.client.post('/api/chat', json=self.payload())
            self.assertEqual(exhausted.status_code, 429)
            self.assertIn('На сегодня тренировка завершена', exhausted.json['error'])
            self.assertEqual(exhausted.json['remaining'], 0)
            self.assertEqual(model.call_count, 18)

    def test_invalid_input_does_not_use_daily_allowance(self):
        self.assertEqual(self.client.get('/api/status').json['remaining'], course_app.DAILY_LIMIT)
        denied = self.client.post('/api/chat', json=self.payload(consent=False))
        self.assertEqual(denied.status_code, 400)
        self.assertEqual(self.client.get('/api/status').json['remaining'], course_app.DAILY_LIMIT)


if __name__ == '__main__':
    unittest.main()
