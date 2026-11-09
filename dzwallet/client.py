import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from .errors import (ApiError, ConfigurationError, ConnectionFailed, InvalidSignatureError)


def to_form(obj, prefix=None, out=None):
    """Flattens nested dicts/lists into form fields: customer[first_name], gateway_methods[] ..."""
    out = [] if out is None else out
    for k, v in (obj or {}).items():
        if v is None:
            continue
        key = "%s[%s]" % (prefix, k) if prefix else str(k)
        if isinstance(v, (list, tuple)):
            for item in v:
                out.append(("%s[]" % key, str(item)))
        elif isinstance(v, dict):
            to_form(v, key, out)
        else:
            out.append((key, str(v)))
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def default_transport(method, url, headers, body, timeout):
    """Returns (status, text). Raises ConnectionFailed on network problems."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise ConnectionFailed("Could not reach DzWallet: %s" % e)


class PaymentStatus:
    def __init__(self, status, identifier, amount=None, currency=None, trx=None, raw=None):
        self.status, self.identifier, self.amount, self.currency, self.trx, self.raw = status, identifier, amount, currency, trx, raw or {}

    @property
    def is_paid(self):
        return self.status == "success"

    @property
    def is_final(self):
        return self.status in ("success", "rejected", "cancelled")


class PaymentLink:
    def __init__(self, d, raw, email_sent=None):
        self.trx = str(d.get("trx", ""))
        self.amount = str(d.get("amount", ""))
        self.currency = d.get("currency", "DZD")
        self.status = str(d.get("status", ""))
        self.url = str(d["url"])
        self.print_url = d.get("print_url")
        self.expires_at = d.get("expires_at")
        self.reference = d.get("reference")
        self.identifier = d.get("identifier")
        self.email_sent = email_sent
        self.raw = raw

    @property
    def is_open(self):
        return self.status == "pending"


class Ipn:
    def __init__(self, status, identifier, data, raw):
        self.status, self.identifier, self.data, self.raw = status, identifier, data, raw

    @property
    def is_success(self):
        return self.status == "success"

    @property
    def amount(self):
        return str(self.data.get("amount", ""))

    @property
    def trx(self):
        v = self.data.get("payment_trx")
        return str(v) if v else None


class DzWallet:
    def __init__(self, public_key="", secret_key="", mode="test", base_url="https://dzwallet.app",
                 timeout=30, status_retries=2, transport=None):
        self.mode = "live" if mode == "live" else "test"
        self.public_key = (public_key or "").strip()
        self.secret_key = (secret_key or "").strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.status_retries = status_retries
        self._transport = transport or default_transport

    def _need_public(self):
        if not self.public_key:
            raise ConfigurationError("public_key is not set.")
        return self.public_key

    def _need_secret(self):
        if not self.secret_key:
            raise ConfigurationError("secret_key is not set.")
        return self.secret_key

    def _call(self, method, path, headers=None, form=None, retryable=False):
        attempts = self.status_retries + 1 if retryable else 1
        h = {"Accept": "application/json"}
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        h.update(headers or {})
        last = None
        for i in range(attempts):
            try:
                status, text = self._transport(method, self.base_url + path, h, body, self.timeout)
            except ConnectionFailed as e:
                last = e
                if i + 1 < attempts:
                    time.sleep(0.3)
                continue
            return self._parse(status, text)
        raise last

    @staticmethod
    def _parse(status, text):
        try:
            j = json.loads(text)
            j = j if isinstance(j, dict) else {}
        except ValueError:
            j = {}
        if status < 200 or status >= 300 or j.get("status") == "error":
            errors = []
            m = j.get("message")
            if m is not None:
                errors = [str(x) for x in m] if isinstance(m, list) else [str(m)]
            if not errors and isinstance(j.get("errors"), dict):
                for lst in j["errors"].values():
                    errors.extend(str(x) for x in (lst if isinstance(lst, list) else [lst]))
            raise ApiError("; ".join(errors) if errors else "HTTP %d" % status, errors, status)
        return j

    # ------------------------------ card payment ------------------------------

    def initiate_payment(self, **params):
        """Starts a payment and returns (redirect_url, identifier). Send the customer to redirect_url.
        NEVER retried automatically: after a ConnectionFailed call payment_status() first."""
        p = {"currency": "DZD", "gateway_methods": ["Satim"]}
        p.update(params)
        p["currency"] = str(p["currency"]).upper()
        self._validate_initiate(p)
        p["public_key"] = self._need_public()
        path = "/test/payment/initiate" if self.mode == "test" else "/payment/initiate"
        j = self._call("POST", path, form=to_form(p))
        url = j.get("redirect_url")
        if not isinstance(url, str) or not url:
            raise ApiError("The API did not return a checkout address.")
        return url, str(p["identifier"])

    @staticmethod
    def _validate_initiate(p):
        errors = []
        for k in ("identifier", "details", "ipn_url", "success_url", "cancel_url", "site_name"):
            if not str(p.get(k, "")).strip():
                errors.append("%s is required" % k)
        if len(str(p.get("identifier", ""))) > 20:
            errors.append("identifier must be at most 20 characters")
        if len(str(p.get("details", ""))) > 100:
            errors.append("details must be at most 100 characters")
        try:
            if float(p.get("amount")) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("amount must be a number greater than 0")
        for k in ("ipn_url", "success_url", "cancel_url"):
            if p.get(k) and not re.match(r"^https?://", str(p[k]), re.I):
                errors.append("%s must start with http:// or https://" % k)
        c = p.get("customer")
        if not isinstance(c, dict):
            errors.append("customer is required")
        else:
            for k in ("first_name", "last_name", "email", "mobile"):
                if not str(c.get(k, "")).strip():
                    errors.append("customer.%s is required" % k)
        if errors:
            raise ValueError("; ".join(errors))

    def payment_status(self, identifier):
        """Only .is_paid proves the money was received. Safe to retry."""
        if not identifier:
            raise ValueError("identifier is required")
        j = self._call("POST", "/api/payment/status",
                       form=to_form({"public_key": self._need_public(), "identifier": identifier}), retryable=True)
        d = dict(j)
        if isinstance(j.get("data"), dict):
            d.update(j["data"])
        if not isinstance(d.get("payment_status"), str):
            raise ApiError("The API did not return a payment status.")
        return PaymentStatus(d["payment_status"], str(d.get("identifier") or identifier),
                             str(d["amount"]) if d.get("amount") is not None else None,
                             d.get("currency"), d.get("trx"), j)

    # --------------------------- payment links (live keys) ---------------------------

    def _link_headers(self):
        return {"X-Public-Key": self._need_public(), "X-Secret-Key": self._need_secret()}

    @staticmethod
    def _make_link(j):
        d = j.get("data")
        if not isinstance(d, dict) or not d.get("url"):
            raise ApiError("The API did not return a payment link.")
        sent = j.get("email_sent", d.get("email_sent"))
        return PaymentLink(d, j, None if sent is None else bool(sent))

    def create_payment_link(self, amount, note=None, days=None, reference=None, ipn_url=None, email=None):
        """The same reference with the same amount returns the same link."""
        try:
            if float(amount) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("amount must be a number greater than 0")
        if days is not None and not 1 <= int(days) <= 30:
            raise ValueError("days must be between 1 and 30")
        if note is not None and len(str(note)) > 100:
            raise ValueError("note must be at most 100 characters")
        if reference is not None and not re.match(r"^[A-Za-z0-9_-]{1,15}$", str(reference)):
            raise ValueError("reference: 1-15 characters, letters, digits, _ and - only")
        if email is not None and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", str(email)):
            raise ValueError("email is not valid")
        form = to_form({"amount": str(amount), "note": note, "days": days, "reference": reference, "ipn_url": ipn_url, "email": email})
        return self._make_link(self._call("POST", "/api/payment-links", headers=self._link_headers(), form=dict(form)))

    def payment_link(self, reference):
        return self._make_link(self._call("GET", "/api/payment-links/" + urllib.parse.quote(str(reference), safe=""),
                                          headers=self._link_headers(), retryable=True))

    def reactivate_payment_link(self, reference):
        return self._make_link(self._call("POST", "/api/payment-links/%s/reactivate" % urllib.parse.quote(str(reference), safe=""),
                                          headers=self._link_headers(), form={}))

    # ----------------------------------- IPN -----------------------------------

    def signature(self, amount, identifier):
        """UPPERCASE(HMAC-SHA256(data.amount + identifier, SECRET_KEY))"""
        return hmac.new(self._need_secret().encode(), (str(amount) + str(identifier)).encode(), hashlib.sha256).hexdigest().upper()

    def verify_webhook(self, raw_body, signature_header, tolerance=300, now=None, event_id_header=None):
        """Verify the v2 notification signature on the EXACT raw request body (bytes or str).
        Header X-DzWallet-Signature: t=<unix>,v1=<hex>; signed text is "<t>.<raw body>" with your secret key.
        Raises InvalidSignatureError on a bad signature or a timestamp further than `tolerance` seconds from now.
        The event id is read from the signed body (a header can be changed by anyone who captured the request).
        Returns {"timestamp": int, "event_id": str or None}."""
        raw = bytes(raw_body) if isinstance(raw_body, (bytes, bytearray)) else str(raw_body or "").encode("utf-8")
        timestamp = None
        sigs = []
        for part in str(signature_header or "").split(","):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "t" and v.isdigit():
                timestamp = int(v)
            elif k == "v1":
                sigs.append(v.lower())
        if timestamp is None or not sigs:
            raise InvalidSignatureError("The signature header is missing or malformed.")
        expected = hmac.new(self._need_secret().encode(), (str(timestamp) + ".").encode() + raw, hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(expected, sig) for sig in sigs):
            raise InvalidSignatureError("The notification signature is not valid.")
        if abs((time.time() if now is None else now) - timestamp) > tolerance:
            raise InvalidSignatureError("The notification timestamp is outside the allowed window.")
        event_id = dict(urllib.parse.parse_qsl(raw.decode("utf-8", "replace"))).get("event_id")
        if event_id_header and event_id and not hmac.compare_digest(event_id, str(event_id_header)):
            raise InvalidSignatureError("The event id header does not match the signed body.")
        return {"timestamp": timestamp, "event_id": event_id}

    def verify_notification(self, raw_body, headers=None, tolerance=300, require_v2=False):
        """Verify a notification request. Uses the v2 signature when X-DzWallet-Signature is present, otherwise the
        older signature field (refused when require_v2 is True). `headers` is any mapping (any letter case).
        Returns (ipn, event_id); event_id is None with the older signature."""
        h = {str(k).lower(): v for k, v in (headers or {}).items()}
        raw = bytes(raw_body) if isinstance(raw_body, (bytes, bytearray)) else str(raw_body or "").encode("utf-8")
        payload = parse_ipn_form(raw.decode("utf-8", "replace"))
        if h.get("x-dzwallet-signature"):
            meta = self.verify_webhook(raw, h["x-dzwallet-signature"], tolerance=tolerance, event_id_header=h.get("x-dzwallet-event-id"))
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            identifier = str(payload.get("identifier") or "")
            if not identifier or "amount" not in data:
                raise InvalidSignatureError("The notification is incomplete.")
            return Ipn(str(payload.get("status") or ""), identifier, data, payload), meta["event_id"]
        if require_v2:
            raise InvalidSignatureError("A signed v2 notification is required.")
        return self.verify_ipn(payload), None

    def verify_ipn(self, body, expected_identifier=None, expected_amount=None):
        """body: the notification as a dict, nested like {'data': {'amount': ...}}. Use parse_ipn_form() for a raw form string.
        Raises InvalidSignatureError. Pass YOUR order's identifier and amount, then check .is_success."""
        b = body or {}
        identifier = str(b.get("identifier") or "")
        signature = str(b.get("signature") or "")
        data = b.get("data") if isinstance(b.get("data"), dict) else {}
        if not identifier or not signature or "amount" not in data:
            raise InvalidSignatureError("The notification is incomplete.")
        if not hmac.compare_digest(self.signature(data["amount"], identifier), signature.upper()):
            raise InvalidSignatureError("The notification signature is not valid.")
        if expected_identifier is not None and not hmac.compare_digest(str(expected_identifier), identifier):
            raise InvalidSignatureError("The notification is for another order.")
        if expected_amount is not None and abs(float(expected_amount) - float(data["amount"])) > 0.005:
            raise InvalidSignatureError("The notification amount is not the expected amount.")
        return Ipn(str(b.get("status") or ""), identifier, data, b)

    @staticmethod
    def new_identifier(prefix="DZ"):
        return (re.sub(r"[^A-Za-z0-9]", "", str(prefix))[:6] + secrets.token_hex(8).upper())[:20]


def parse_ipn_form(raw):
    """Turns a raw form body (status=..&data[amount]=..) into the nested dict verify_ipn expects.
    Flask / Django: parse_ipn_form(request.get_data(as_text=True)) or parse_ipn_form(request.body.decode())."""
    out = {}
    for k, v in urllib.parse.parse_qsl(raw, keep_blank_values=True):
        m = re.match(r"^([^\[\]]+)\[([^\[\]]*)\]$", k)
        if m:
            out.setdefault(m.group(1), {})[m.group(2)] = v
        else:
            out[k] = v
    return out
