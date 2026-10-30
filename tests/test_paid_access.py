"""Paid course smoke and security tests. No external payment or mail APIs called."""
import os
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import paid_access as paid
import app as app_module


class PaidCourseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vars = patch.dict(os.environ, {
            'PAYWALL_ENABLED': '1',
            'DATABASE_URL': 'sqlite:///' + str(Path(self.temp.name) / 'paid.sqlite'),
            'PAID_ACCESS_SECRET': 'a' * 64,
            'PRODAMUS_FORM_URL': 'https://testmerchant.payform.ru/',
            'PRODAMUS_SECRET': 'local-unit-test-secret',
            'PRODAMUS_SYS': 'chayka-course',
            'RESEND_API_KEY': 'unit-test-no-live-api',
            'MAIL_FROM': 'test@example.com',
            'LEGAL_OFFER_URL': 'https://example.com/offer',
            'LEGAL_PRIVACY_URL': 'https://example.com/privacy',
            'SUPPORT_EMAIL': 'support@example.com',
            'GEMINI_API_KEY': 'unit-test-fake-gemini'
        })
        self.vars.start()
        paid._engine = None
        paid._engine_url = None
        self.client = app_module.app.test_client()

    def tearDown(self):
        if paid._engine is not None:
            paid._engine.dispose()
        paid._engine = None
        paid._engine_url = None
        self.vars.stop()
        self.temp.cleanup()

    def _create_order(self, email='student@example.com'):
        reply = self.client.post('/api/checkout', json={'email': email, 'agreed': True})
        self.assertEqual(reply.status_code, 200, reply.get_data(as_text=True))
        params = parse_qs(urlsplit(reply.json['url']).query)
        self.assertEqual(params['products[0][price]'], ['399.00'])
        self.assertEqual(params['customer_email'], [email])
        self.assertEqual(params['order_id'], [reply.json['order_ref']])
        self.assertEqual(params['sys'], ['chayka-course'])
        return reply.json['order_ref']

    def _webhook_form(self, order, *, sum='399.00', email='student@example.com', status='success'):
        return {'order_num': order, 'order_id': 'provider-payment-id-123',
                'domain': 'testmerchant.payform.ru', 'sys': 'chayka-course',
                'customer_email': email, 'payment_status': status, 'sum': sum,
                'products[0][name]': 'Личные границы. Интерактивный курс',
                'products[0][price]': '399.00', 'products[0][quantity]': '1'}

    def _post_signed(self, flat):
        signature = paid.prodamus_signature(paid._nested_form(flat), 'local-unit-test-secret')
        return self.client.post('/api/prodamus/webhook', data=flat, headers={'Sign': signature})

    def _pay(self):
        order = self._create_order()
        reply = self._post_signed(self._webhook_form(order))
        self.assertEqual(reply.status_code, 200, reply.get_data(as_text=True))
        return order

    def _login(self):
        messages = []
        with patch.object(paid, 'send_email', side_effect=lambda to, sub, msg: messages.append((to, msg))):
            r = self.client.post('/api/access/request-code', json={'email': 'student@example.com'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(messages), 1)
        code = re.search(r'код:\s*(\d{8})', messages[0][1]).group(1)
        invalid = self.client.post('/api/access/verify-code', json={'email': 'student@example.com', 'code': '00000000' if code != '00000000' else '11111111'})
        self.assertEqual(invalid.status_code, 401)
        r = self.client.post('/api/access/verify-code', json={'email': 'student@example.com', 'code': code})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return code

    @staticmethod
    def _gemini(scenario, n, message, history):
        return {'reaction': 'Понял, обсудим другой срок.', 'feedback': 'Ты озвучил решение.',
                'rewrite': 'Я сегодня не смогу.', 'signals': {'clarity': 2, 'respect': 2, 'boundaries': 2},
                'final_summary': 'В первом раунде отказ был мягким. Во втором чётким. В третьем появился вариант.',
                'next_practice': 'Попробуй короткую формулировку.'}

    def test_unpaid_user_cannot_fetch_course_or_use_gemini(self):
        homepage = self.client.get('/')
        self.assertEqual(homepage.status_code, 200)
        self.assertIn('399 ₽', homepage.get_data(as_text=True))
        self.assertNotIn('Отчёт после работы', homepage.get_data(as_text=True))
        self.assertEqual(self.client.get('/index.html').status_code, 200)
        self.assertEqual(self.client.get('/api/status').status_code, 401)
        self.assertEqual(self.client.post('/api/chat', json={}).status_code, 401)

    def test_unconfigured_paid_site_fails_closed(self):
        os.environ.pop('PRODAMUS_SECRET')
        self.assertEqual(self.client.get('/').status_code, 503)
        self.assertEqual(self.client.post('/api/checkout', json={'email': 'student@example.com','agreed':True}).status_code, 503)
        self.assertEqual(self.client.post('/api/chat', json={}).status_code, 503)

    def test_success_redirect_never_grants_access(self):
        self._create_order()
        self.assertEqual(self.client.get('/payment/return').status_code, 303)
        self.assertEqual(self.client.get('/api/status').status_code, 401)

    def test_signed_webhook_amount_email_and_duplicates(self):
        order = self._create_order()
        form = self._webhook_form(order)
        self.assertEqual(self.client.post('/api/prodamus/webhook', data=form).status_code, 403)
        self.assertEqual(self._post_signed(self._webhook_form(order, sum='1.00')).status_code, 403)
        self.assertEqual(self._post_signed(self._webhook_form(order, email='other@example.com')).status_code, 403)
        self.assertEqual(self.client.get('/api/status').status_code, 401)
        self.assertEqual(self._post_signed(form).status_code, 200)
        self.assertEqual(self._post_signed(form).status_code, 200)
        self.assertEqual(paid.account_status('student@example.com')['total_remaining'], 90)

    def test_full_checkout_code_login_and_paid_three_rounds(self):
        self._pay()
        self._login()
        homepage = self.client.get('/')
        self.assertEqual(homepage.status_code, 200)
        self.assertIn('Отчёт после работы', homepage.get_data(as_text=True))
        status = self.client.get('/api/status').json
        self.assertEqual(status['remaining'], 18)
        self.assertEqual(status['total_remaining'], 90)
        history = []
        with patch.object(app_module, 'gemini_reply', side_effect=self._gemini):
            for n in range(1, 4):
                message = f'Я откажусь от переработки, вариант {n}.'
                reply = self.client.post('/api/chat', json={'scenario_id': 1, 'round': n, 'message': message,
                                                             'history': history, 'consent': True})
                self.assertEqual(reply.status_code, 200, reply.get_data(as_text=True))
                self.assertEqual(reply.json['daily_remaining'], 18-n)
                self.assertEqual(reply.json['total_remaining'], 90-n)
                history.append({'user': message, 'reply': reply.json['reaction'], 'signals': reply.json['signals']})
        self.assertIn('Динамика оценок', reply.json['final_summary'])
        self.assertTrue(reply.json['next_practice'])
        self.assertEqual(self.client.post('/api/access/logout').status_code, 200)
        self.assertEqual(self.client.get('/api/status').status_code, 401)
        self.assertNotIn('Отчёт после работы', self.client.get('/').get_data(as_text=True))

    def test_failed_gemini_refunds_paid_turn(self):
        self._pay()
        self._login()
        before = self.client.get('/api/status').json
        with patch.object(app_module, 'gemini_reply', side_effect=ValueError('model error')):
            reply = self.client.post('/api/chat', json={'scenario_id': 1, 'round': 1,
                                      'message': 'Сегодня я не останусь.', 'history': [], 'consent': True})
        self.assertEqual(reply.status_code, 502)
        after = self.client.get('/api/status').json
        self.assertEqual(after['remaining'], before['remaining'])
        self.assertEqual(after['total_remaining'], before['total_remaining'])

    def test_email_enumeration_not_allowed(self):
        for email in ('unpaid@example.com', 'unknown@example.com'):
            r = self.client.post('/api/access/request-code', json={'email': email})
            self.assertEqual(r.status_code, 200)
            self.assertIn('Если', r.json['message'])

    def test_cannot_checkout_without_consent(self):
        result = self.client.post('/api/checkout', json={'email': 'student@example.com', 'agreed': False})
        self.assertEqual(result.status_code, 400)


if __name__ == '__main__':
    unittest.main()
