"""Paid course entitlement, Prodamus webhook, passwordless email access.

PAYWALL_ENABLED is OFF by default; no charge or content restriction is activated
until a persistent DATABASE_URL, Prodamus keys, email sender and legal pages are
ready. Transactions are the source of truth; never trust the payment return URL.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from sqlalchemy import BigInteger, Column, Integer, String, create_engine, select
from sqlalchemy.orm import Session, declarative_base

PRICE = Decimal("399.00")
ACCESS_DAYS = 30
TOTAL_TURNS = 90
DAILY_TURNS = 18
COOKIE_NAME = "granitsy_paid_session"
SESSION_SECONDS = 30 * 86400
CODE_SECONDS = 10 * 60
MAX_CODE_ATTEMPTS = 5

Base = declarative_base()
_engine = None
_engine_url = None
_engine_lock = threading.Lock()


class Account(Base):
    __tablename__ = "course_accounts"
    email = Column(String(254), primary_key=True)
    paid_until = Column(BigInteger, nullable=False, default=0)
    remaining_turns = Column(Integer, nullable=False, default=0)
    daily_day = Column(String(10), nullable=False, default="")
    daily_count = Column(Integer, nullable=False, default=0)


class Order(Base):
    __tablename__ = "course_orders"
    order_ref = Column(String(64), primary_key=True)
    email = Column(String(254), nullable=False, index=True)
    status = Column(String(24), nullable=False, default="pending")
    price_kopecks = Column(Integer, nullable=False, default=39900)
    provider_order_id = Column(String(100), nullable=True)
    created_at = Column(BigInteger, nullable=False)
    paid_at = Column(BigInteger, nullable=True)


class RateBucket(Base):
    __tablename__ = "course_rate_buckets"
    bucket_key = Column(String(64), primary_key=True)
    window_start = Column(BigInteger, nullable=False)
    count = Column(Integer, nullable=False, default=0)


class LoginCode(Base):
    __tablename__ = "course_login_codes"
    email = Column(String(254), primary_key=True)
    digest = Column(String(64), nullable=False)
    expires_at = Column(BigInteger, nullable=False)
    issued_at = Column(BigInteger, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)


class AccessSession(Base):
    __tablename__ = "course_sessions"
    token_hash = Column(String(64), primary_key=True)
    email = Column(String(254), nullable=False, index=True)
    created_at = Column(BigInteger, nullable=False)
    expires_at = Column(BigInteger, nullable=False)


def enabled() -> bool:
    return os.getenv("PAYWALL_ENABLED", "").lower() in ("1", "true", "yes")


def configured() -> bool:
    basics = all(os.getenv(k, "").strip() for k in (
        "DATABASE_URL", "PAID_ACCESS_SECRET", "PRODAMUS_FORM_URL",
        "PRODAMUS_SECRET", "PRODAMUS_SYS", "MAIL_FROM",
        "LEGAL_OFFER_URL", "LEGAL_PRIVACY_URL", "SUPPORT_EMAIL"))
    email_ready = bool(os.getenv("RESEND_API_KEY", "").strip()) or all(
        os.getenv(k, "").strip() for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS"))
    return basics and email_ready and len(os.getenv("PAID_ACCESS_SECRET", "")) >= 32


def database():
    global _engine, _engine_url
    uri = os.environ.get("DATABASE_URL", "").strip()
    if not uri:
        raise RuntimeError("PAID_DATABASE_NOT_CONFIGURED")
    # Old postgres:// scheme is not supported by SQLAlchemy 2.
    if uri.startswith("postgres://"):
        uri = "postgresql+psycopg://" + uri[len("postgres://"):]
    elif uri.startswith("postgresql://"):
        uri = "postgresql+psycopg://" + uri[len("postgresql://"):]
    if _engine is None or _engine_url != uri:
        with _engine_lock:
            if _engine is None or _engine_url != uri:
                kw = {"pool_pre_ping": True, "pool_recycle": 300}
                if uri.startswith("sqlite") and ":memory:" in uri:
                    from sqlalchemy.pool import StaticPool
                    kw.update(connect_args={"check_same_thread": False}, poolclass=StaticPool)
                _engine = create_engine(uri, **kw)
                _engine_url = uri
                Base.metadata.create_all(_engine)
    return _engine


def normalize_email(raw) -> str:
    email = str(raw or "").strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[a-z0-9._%+\-]{1,64}@[a-z0-9.\-]{1,180}\.[a-z]{2,30}", email):
        raise ValueError("Укажи действующий адрес электронной почты.")
    return email


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def short_day(now=None) -> str:
    return datetime.fromtimestamp(now or time.time(), tz=timezone.utc).strftime("%Y-%m-%d")


def account_status(email: str, now=None):
    now = int(now or time.time())
    with Session(database()) as db:
        account = db.get(Account, email)
        if account is None or account.paid_until <= now:
            return {"active": False, "remaining": 0, "daily_limit": DAILY_TURNS}
        used = account.daily_count if account.daily_day == short_day(now) else 0
        return {"active": True, "remaining": max(0, min(DAILY_TURNS - used, account.remaining_turns)),
                "daily_limit": DAILY_TURNS, "total_remaining": account.remaining_turns,
                "paid_until": account.paid_until}


def session_email(raw_token: str) -> str | None:
    if not raw_token or len(raw_token) > 256:
        return None
    now = int(time.time())
    with Session(database()) as db:
        session = db.get(AccessSession, token_hash(raw_token))
        if session is None or session.expires_at <= now:
            return None
        account = db.get(Account, session.email)
        return session.email if account and account.paid_until > now else None


def issue_session(email: str) -> str:
    now = int(time.time())
    raw = secrets.token_urlsafe(40)
    with Session(database()) as db, db.begin():
        # Limit the number of devices/sessions to two.
        sessions = db.execute(select(AccessSession).where(
            AccessSession.email == email).order_by(AccessSession.created_at.desc())).scalars().all()
        for old in sessions[1:]:
            db.delete(old)
        db.add(AccessSession(token_hash=token_hash(raw), email=email, created_at=now, expires_at=now + SESSION_SECONDS))
    return raw


def revoke_session(raw_token: str) -> None:
    if not raw_token:
        return
    with Session(database()) as db, db.begin():
        value = db.get(AccessSession, token_hash(raw_token))
        if value is not None:
            db.delete(value)


def _secret() -> bytes:
    secret = os.environ.get("PAID_ACCESS_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("PAID_ACCESS_SECRET is missing or too short")
    return secret.encode("utf-8")


def rate_allowed(ip: str, action: str, limit: int, window: int = 600) -> bool:
    """Persistent per-IP abuse guard across serverless processes.

    IPs are not stored as plaintext; the hash is keyed with the application secret.
    """
    ip = (ip or "unknown").strip()[:128]
    bucket_key = hmac.new(_secret(), f"{action}|{ip}".encode("utf-8"),
                          hashlib.sha256).hexdigest()
    now = int(time.time())
    with Session(database()) as db, db.begin():
        bucket = db.get(RateBucket, bucket_key, with_for_update=True)
        if bucket is None:
            db.add(RateBucket(bucket_key=bucket_key, window_start=now, count=1))
            return True
        if now - bucket.window_start >= window:
            bucket.window_start = now
            bucket.count = 1
            return True
        if bucket.count >= limit:
            return False
        bucket.count += 1
        return True


def _code_digest(email: str, code: str) -> str:
    return hmac.new(_secret(), f"{email}\x00{code}".encode("utf-8"), hashlib.sha256).hexdigest()


def send_code(email: str) -> bool:
    """Rate-limit one code/email/minute. No account enumeration to the caller."""
    now = int(time.time())
    email = normalize_email(email)
    with Session(database()) as db, db.begin():
        account = db.get(Account, email)
        if account is None or account.paid_until <= now:
            return False
        existing = db.get(LoginCode, email)
        if existing is not None and now - existing.issued_at < 60:
            return False
        code = f"{secrets.randbelow(100_000_000):08d}"
        digest = _code_digest(email, code)
        if existing is None:
            db.add(LoginCode(email=email, digest=digest, issued_at=now, expires_at=now + CODE_SECONDS, attempts=0))
        else:
            existing.digest = digest
            existing.issued_at = now
            existing.expires_at = now + CODE_SECONDS
            existing.attempts = 0
    subject = "Код входа в курс «Личные границы»"
    body = ("Для входа в интерактивный курс Семёна Чайки используй код: " + code +
            "\nКод действителен 10 минут и подходит только для одного входа." +
            "\nЕсли ты не запрашивал код, проигнорируй это письмо.")
    try:
        send_email(email, subject, body)
    except Exception:
        # Codes are never logged, even on delivery failure.
        with Session(database()) as db, db.begin():
            row = db.get(LoginCode, email)
            if row is not None and row.digest == digest:
                db.delete(row)
        raise
    return True


def send_email(to: str, subject: str, body: str) -> None:
    origin = os.environ["MAIL_FROM"]
    if os.environ.get("RESEND_API_KEY", "").strip():
        key = os.environ["RESEND_API_KEY"]
        payload = json.dumps({"from": origin, "to": [to], "subject": subject, "text": body},
                             ensure_ascii=False).encode("utf-8")
        req = Request("https://api.resend.com/emails", data=payload, method="POST",
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with urlopen(req, timeout=12) as response:
            if response.status not in (200, 201):
                raise RuntimeError("MAIL_DELIVERY_FAILED")
        return
    # Existing domain mailboxes can use SMTP over TLS instead of Resend.
    import smtplib
    import ssl
    from email.message import EmailMessage
    mail = EmailMessage()
    mail["From"] = origin
    mail["To"] = to
    mail["Subject"] = subject
    mail.set_content(body)
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=12, context=ssl.create_default_context()) as connection:
            connection.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            connection.send_message(mail)
    elif port == 587:
        with smtplib.SMTP(host, port, timeout=12) as connection:
            connection.starttls(context=ssl.create_default_context())
            connection.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            connection.send_message(mail)
    else:
        raise RuntimeError("SMTP must use TLS port 465 or 587")


def verify_code(email: str, code: str) -> str | None:
    email = normalize_email(email)
    if not re.fullmatch(r"\d{8}", code or ""):
        return None
    now = int(time.time())
    with Session(database()) as db, db.begin():
        row = db.get(LoginCode, email, with_for_update=True)
        account = db.get(Account, email)
        if row is None or account is None or account.paid_until <= now or row.expires_at <= now:
            return None
        if row.attempts >= MAX_CODE_ATTEMPTS:
            return None
        row.attempts += 1
        matched = hmac.compare_digest(row.digest, _code_digest(email, code))
        if matched:
            db.delete(row)
    return issue_session(email) if matched else None


def reserve_turn(email: str) -> tuple[bool, str | None, int]:
    """Atomically reserve credits before invoking the paid external model."""
    now = int(time.time())
    day = short_day(now)
    with Session(database()) as db, db.begin():
        account = db.get(Account, email, with_for_update=True)
        if account is None or account.paid_until <= now:
            return False, None, 0
        if account.daily_day != day:
            account.daily_day = day
            account.daily_count = 0
        if account.daily_count >= DAILY_TURNS or account.remaining_turns <= 0:
            return False, None, max(0, min(DAILY_TURNS - account.daily_count, account.remaining_turns))
        account.daily_count += 1
        account.remaining_turns -= 1
        remaining = min(DAILY_TURNS - account.daily_count, account.remaining_turns)
    return True, day, remaining


def refund_turn(email: str, day: str) -> None:
    if not day:
        return
    with Session(database()) as db, db.begin():
        account = db.get(Account, email, with_for_update=True)
        if account is None:
            return
        account.remaining_turns += 1
        if account.daily_day == day and account.daily_count > 0:
            account.daily_count -= 1


def _sort_for_prodamus(value):
    """Match PHP recursive ksort + scalar to string + json_encode semantics."""
    if isinstance(value, dict):
        return {str(k): _sort_for_prodamus(v) for k, v in sorted(value.items(), key=lambda i: str(i[0]))}
    if isinstance(value, list):
        return [_sort_for_prodamus(v) for v in value]
    return str(value) if value is not None else ""


def prodamus_signature(data: dict, secret: str) -> str:
    sanitized = {k: v for k, v in data.items() if k != "signature"}
    canonical = json.dumps(_sort_for_prodamus(sanitized), ensure_ascii=False,
                           separators=(",", ":")).replace("/", "\\/")
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _nested_form(flat: dict) -> dict:
    """Rebuild PHP-style products[0][name] for Prodamus HMAC verification."""
    result = {}
    for key, value in flat.items():
        parts = re.findall(r"[^\[\]]+", key)
        if not parts or len(parts) > 6:
            raise ValueError("invalid webhook field")
        cursor = result
        for n, part in enumerate(parts):
            last = n == len(parts) - 1
            if isinstance(cursor, dict):
                if last:
                    cursor[part] = value
                else:
                    nxt = parts[n + 1]
                    cursor = cursor.setdefault(part, [] if nxt.isdigit() else {})
            elif isinstance(cursor, list):
                if not part.isdigit() or int(part) > 30:
                    raise ValueError("invalid nested index")
                idx = int(part)
                while len(cursor) <= idx:
                    cursor.append(None)
                if last:
                    cursor[idx] = value
                else:
                    if cursor[idx] is None:
                        cursor[idx] = [] if parts[n + 1].isdigit() else {}
                    cursor = cursor[idx]
            else:
                raise ValueError("invalid form structure")
    return result


def make_checkout(email: str) -> tuple[str, str]:
    """Create an unpaid order and signed HTTPS Prodamus payment URL."""
    email = normalize_email(email)
    raw_url = os.environ["PRODAMUS_FORM_URL"].strip()
    parsed = urlsplit(raw_url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".payform.ru"):
        raise RuntimeError("Invalid Prodamus form domain")
    secret = os.environ["PRODAMUS_SECRET"].strip()
    sys_code = os.environ["PRODAMUS_SYS"].strip()
    if not secret or not sys_code:
        raise RuntimeError("Missing Prodamus settings")
    order_ref = "chayka-" + secrets.token_hex(16)
    base = os.environ.get("COURSE_PUBLIC_URL", "https://granitsy-semen-chayka.vercel.app").rstrip("/")
    if not base.startswith("https://") or "/" in base[8:]:
        raise RuntimeError("Invalid public URL")
    now = int(time.time())
    with Session(database()) as db, db.begin():
        db.add(Order(order_ref=order_ref, email=email, price_kopecks=39900,
                     status="pending", created_at=now))
    data = {
        "do": "pay", "sys": sys_code, "order_id": order_ref,
        "customer_email": email, "npd_income_type": "FROM_INDIVIDUAL",
        "products": [{"name": "Личные границы. Интерактивный курс", "price": "399.00",
                      "quantity": "1", "type": "course"}],
        "urlReturn": base + "/payment/return",
        "urlSuccess": base + "/payment/return",
        "urlNotification": base + "/api/prodamus/webhook",
    }
    data["signature"] = prodamus_signature(data, secret)
    query = {
        k: v for k, v in data.items() if k != "products"
    }
    for k, v in data["products"][0].items():
        query[f"products[0][{k}]"] = v
    return raw_url.rstrip("/") + "/?" + urlencode(query), order_ref


def accept_prodamus_webhook(flat: dict, signature: str) -> bool:
    """Only signed, matching, successful, non-duplicate orders grant access."""
    secret = os.environ.get("PRODAMUS_SECRET", "").strip()
    if not secret or not signature or len(signature) != 64:
        return False
    nested = _nested_form(flat)
    expected = prodamus_signature(nested, secret)
    if not hmac.compare_digest(expected, signature.lower()):
        return False
    if flat.get("payment_status", "").lower() != "success":
        return True  # Legitimate non-success callback; do not grant access.
    order_ref = flat.get("order_num", "")
    domain = flat.get("domain", "").strip().lower()
    if domain != urlsplit(os.environ.get("PRODAMUS_FORM_URL", "")).hostname:
        return False
    if flat.get("sys", "") != os.environ.get("PRODAMUS_SYS", ""):
        return False
    try:
        received = Decimal(flat.get("sum", ""))
        if not received.is_finite() or received != PRICE:
            return False
        email = normalize_email(flat.get("customer_email", ""))
    except (InvalidOperation, ValueError):
        return False
    now = int(time.time())
    with Session(database()) as db, db.begin():
        order = db.get(Order, order_ref, with_for_update=True)
        if order is None or order.email != email or order.price_kopecks != 39900:
            return False
        if order.status == "paid":
            return True  # Duplicate webhook: idempotent.
        if order.status != "pending":
            return False
        order.status = "paid"
        order.paid_at = now
        order.provider_order_id = str(flat.get("order_id", ""))[:100]
        account = db.get(Account, email, with_for_update=True)
        if account is None:
            account = Account(email=email, paid_until=now + ACCESS_DAYS * 86400,
                              remaining_turns=TOTAL_TURNS, daily_day="", daily_count=0)
            db.add(account)
        else:
            account.paid_until = max(now, account.paid_until) + ACCESS_DAYS * 86400
            account.remaining_turns += TOTAL_TURNS
    return True
