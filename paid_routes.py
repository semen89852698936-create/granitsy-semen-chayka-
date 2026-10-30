"""Flask endpoints for paid Prodamus access, bound only while paywall is enabled."""
from __future__ import annotations

import os

from flask import Blueprint, jsonify, request, redirect, make_response

import paid_access as paid

bp = Blueprint("paid", __name__)


def _reply(body: dict, status: int = 200):
    resp = jsonify(body)
    resp.status_code = status
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def _same_origin():
    from urllib.parse import urlsplit
    origin = request.headers.get("Origin", "")
    site = request.headers.get("Sec-Fetch-Site", "same-origin")
    if site not in ("same-origin", "none"):
        return False
    return not origin or urlsplit(origin).netloc == request.host


def _active():
    return paid.enabled() and paid.configured()


def _email_body():
    if not request.is_json or (request.content_length is not None and request.content_length > 2048):
        raise ValueError("Нужен короткий JSON-запрос.")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("Некорректные данные.")
    return data


@bp.get("/api/access/status")
def access_status():
    if not paid.enabled():
        return _reply({"paywall_enabled": False, "active": False, "ready": False})
    if not paid.configured():
        return _reply({"paywall_enabled": True, "ready": False, "active": False}, 503)
    token = request.cookies.get(paid.COOKIE_NAME, "")
    email = paid.session_email(token)
    status = paid.account_status(email) if email else {"active": False, "remaining": 0}
    return _reply({"paywall_enabled": True, "ready": True, **status,
                   "email": (email[:2] + "***@" + email.split("@")[1]) if email else None})


@bp.post("/api/checkout")
def checkout():
    if not _active():
        return _reply({"error": "Оплата ещё не подключена. Деньги не списываются."}, 503)
    if not _same_origin():
        return _reply({"error": "Недопустимый источник запроса."}, 403)
    try:
        data = _email_body()
        if data.get("agreed") is not True:
            return _reply({"error": "Перед оплатой необходимо принять условия продажи и обработки данных."}, 400)
        url, reference = paid.make_checkout(data.get("email"))
        return _reply({"url": url, "order_ref": reference})
    except (ValueError, TypeError):
        return _reply({"error": "Проверь электронную почту и повтори попытку."}, 400)
    except Exception:
        # No secret information or provider details are sent to the browser.
        return _reply({"error": "Оформление временно недоступно. Оплата не начата."}, 503)


@bp.post("/api/access/request-code")
def request_code():
    if not _active():
        return _reply({"error": "Сервис входа ещё не подключён."}, 503)
    if not _same_origin():
        return _reply({"error": "Недопустимый источник запроса."}, 403)
    try:
        data = _email_body()
        email = paid.normalize_email(data.get("email"))
        paid.send_code(email)
    except ValueError:
        return _reply({"error": "Проверь адрес электронной почты."}, 400)
    except Exception:
        return _reply({"error": "Письмо пока не удалось отправить. Повтори позже."}, 503)
    # Do not disclose whether an email has paid access.
    return _reply({"message": "Если на эту почту оформлен доступ, код отправлен. Проверь папку «Спам»."})


@bp.post("/api/access/verify-code")
def verify_code():
    if not _active():
        return _reply({"error": "Сервис входа ещё не подключён."}, 503)
    if not _same_origin():
        return _reply({"error": "Недопустимый источник запроса."}, 403)
    try:
        data = _email_body()
        email = paid.normalize_email(data.get("email"))
        code = str(data.get("code", "")).strip()
        token = paid.verify_code(email, code)
    except ValueError:
        return _reply({"error": "Проверь адрес и код."}, 400)
    except Exception:
        return _reply({"error": "Вход временно недоступен."}, 503)
    if not token:
        return _reply({"error": "Код неверный, истёк или доступ не оплачен."}, 401)
    response = _reply({"active": True, "redirect": "/"})
    response.set_cookie(paid.COOKIE_NAME, token, max_age=paid.SESSION_SECONDS,
                        secure=True, httponly=True, samesite="Lax", path="/")
    return response


@bp.post("/api/access/logout")
def logout():
    if not _same_origin():
        return _reply({"error": "Недопустимый источник запроса."}, 403)
    try:
        if paid.enabled() and paid.configured():
            paid.revoke_session(request.cookies.get(paid.COOKIE_NAME, ""))
    except Exception:
        pass
    response = _reply({"active": False})
    response.delete_cookie(paid.COOKIE_NAME, path="/")
    return response


@bp.post("/api/prodamus/webhook")
def webhook():
    if not _active():
        return _reply({"error": "Integration not configured"}, 503)
    if request.content_length is None or request.content_length > 25000:
        return _reply({"error": "Invalid request size"}, 413)
    if not request.mimetype in ("multipart/form-data", "application/x-www-form-urlencoded"):
        return _reply({"error": "Invalid form format"}, 415)
    # A Prodamus signature is in the "Sign" HTTP header, NOT a query token.
    signature = request.headers.get("Sign", "").strip()
    if not signature:
        return _reply({"error": "Signature missing"}, 403)
    items = list(request.form.items(multi=True))
    if not items or len(items) > 80 or len(set(k for k, _ in items)) != len(items):
        return _reply({"error": "Invalid form"}, 400)
    flat = dict(items)
    try:
        valid = paid.accept_prodamus_webhook(flat, signature)
    except Exception:
        # A temporary database error should trigger Prodamus's webhook retries.
        return _reply({"error": "Unable to process webhook"}, 503)
    if not valid:
        return _reply({"error": "Signature or payment data invalid"}, 403)
    return make_response("success", 200, {"Cache-Control": "no-store"})


@bp.get("/payment/return")
def payment_return():
    # Never treat redirects/query parameters as proof of payment.
    return redirect("/?payment=return", code=303)
