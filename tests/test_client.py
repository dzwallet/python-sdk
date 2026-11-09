import hashlib
import hmac
import json
import unittest
import urllib.parse

from dzwallet import (ApiError, ConfigurationError, ConnectionFailed, DzWallet, InvalidSignatureError,
                      parse_ipn_form, to_form)


class Fake:
    def __init__(self, *queue):
        self.queue, self.calls = list(queue), []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": urllib.parse.unquote_plus(body.decode()) if body else ""})
        r = self.queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


GOOD = dict(identifier="ORD1", amount=1000, details="x", ipn_url="https://a.dz/i", success_url="https://a.dz/s",
            cancel_url="https://a.dz/c", site_name="S",
            customer=dict(first_name="A", last_name="B", email="a@b.dz", mobile="0555"))


def mk(*queue, **kw):
    f = Fake(*queue)
    return DzWallet(public_key="test_pub", secret_key="test_sec", transport=f, status_retries=kw.pop("retries", 2), **kw), f


def sig(amount, ident, secret="test_sec"):
    return hmac.new(secret.encode(), (amount + ident).encode(), hashlib.sha256).hexdigest().upper()


def ipn(**over):
    d = {"status": "success", "signature": sig("1000.00000000", "ORD1"), "identifier": "ORD1",
         "data": {"payment_trx": "T1", "amount": "1000.00000000", "currency": "DZD"}}
    d.update(over)
    return d


class T(unittest.TestCase):
    def test_to_form(self):
        s = urllib.parse.unquote(urllib.parse.urlencode(to_form({"a": 1, "customer": {"email": "x@y.dz"}, "g": ["Satim"], "skip": None})))
        self.assertEqual(s, "a=1&customer[email]=x@y.dz&g[]=Satim")

    def test_initiate_test_endpoint_and_defaults(self):
        dz, f = mk((200, json.dumps({"status": "success", "redirect_url": "https://dzwallet.app/test/payment/checkout?x"})))
        url, ident = dz.initiate_payment(**GOOD)
        self.assertEqual(url, "https://dzwallet.app/test/payment/checkout?x")
        self.assertEqual(ident, "ORD1")
        self.assertEqual(f.calls[0]["url"], "https://dzwallet.app/test/payment/initiate")
        b = f.calls[0]["body"]
        for part in ("public_key=test_pub", "currency=DZD", "gateway_methods[]=Satim", "customer[first_name]=A"):
            self.assertIn(part, b)
        self.assertNotIn("test_sec", b)

    def test_live_endpoint(self):
        dz, f = mk((200, json.dumps({"redirect_url": "https://x"})), mode="live")
        dz.initiate_payment(**GOOD)
        self.assertEqual(f.calls[0]["url"], "https://dzwallet.app/payment/initiate")

    def test_validation(self):
        dz, f = mk()
        bad = dict(GOOD, customer=dict(GOOD["customer"], email=""))
        with self.assertRaisesRegex(ValueError, r"customer\.email is required"):
            dz.initiate_payment(**bad)
        with self.assertRaises(ValueError):
            dz.initiate_payment(**dict(GOOD, amount=0))
        with self.assertRaises(ValueError):
            dz.initiate_payment(**dict(GOOD, identifier="A" * 21))
        self.assertEqual(f.calls, [])

    def test_missing_public_key(self):
        with self.assertRaises(ConfigurationError):
            DzWallet(transport=Fake()).initiate_payment(**GOOD)

    def test_api_error(self):
        dz, _ = mk((401, json.dumps({"status": "error", "message": ["Invalid api key"]})))
        with self.assertRaises(ApiError) as c:
            dz.initiate_payment(**GOOD)
        self.assertEqual((str(c.exception), c.exception.http_status), ("Invalid api key", 401))
        dz, _ = mk((429, "{}"), retries=0)
        with self.assertRaises(ApiError) as c:
            dz.payment_status("X")
        self.assertTrue(c.exception.is_rate_limited)

    def test_initiate_not_retried(self):
        dz, f = mk(ConnectionFailed("timeout"), (200, json.dumps({"redirect_url": "https://x"})))
        with self.assertRaises(ConnectionFailed):
            dz.initiate_payment(**GOOD)
        self.assertEqual(len(f.calls), 1)

    def test_status_retried(self):
        dz, f = mk(ConnectionFailed("1"), ConnectionFailed("2"),
                   (200, json.dumps({"status": "success", "payment_status": "success", "identifier": "ORD1", "amount": "1000.00000000", "trx": "T1"})))
        s = dz.payment_status("ORD1")
        self.assertTrue(s.is_paid and s.is_final)
        self.assertEqual((s.trx, len(f.calls)), ("T1", 3))
        dz2, _ = mk((200, json.dumps({"payment_status": "pending"})))
        self.assertFalse(dz2.payment_status("Z").is_paid)

    def test_links(self):
        link = {"status": "success", "data": {"trx": "H3K9", "amount": 2500, "currency": "DZD", "status": "pending",
                "url": "https://dzwallet.app/qr/H3K9/abc", "print_url": "https://dzwallet.app/qr/H3K9/abc/code",
                "reference": "ORD1042", "identifier": "QRA03ORD1042"}}
        dz, f = mk((200, json.dumps(link)), (200, json.dumps(link)), (200, json.dumps(link)))
        l = dz.create_payment_link(2500, reference="ORD1042", days=3)
        self.assertTrue(l.is_open)
        self.assertEqual(l.identifier, "QRA03ORD1042")
        self.assertEqual(f.calls[0]["headers"]["X-Secret-Key"], "test_sec")
        self.assertIn("amount=2500", f.calls[0]["body"])
        dz.payment_link("ORD 1")
        self.assertEqual((f.calls[1]["url"], f.calls[1]["method"]), ("https://dzwallet.app/api/payment-links/ORD%201", "GET"))
        dz.reactivate_payment_link("ORD1042")
        self.assertTrue(f.calls[2]["url"].endswith("/ORD1042/reactivate"))
        with self.assertRaises(ValueError):
            dz.create_payment_link(100, reference="bad ref!")
        with self.assertRaises(ValueError):
            dz.create_payment_link(100, days=31)

    def test_ipn(self):
        dz, _ = mk()
        r = dz.verify_ipn(ipn(), "ORD1", 1000)
        self.assertTrue(r.is_success)
        self.assertEqual((r.trx, r.amount), ("T1", "1000.00000000"))
        self.assertEqual(dz.verify_ipn(ipn(signature=sig("1000.00000000", "ORD1").lower())).identifier, "ORD1")
        for bad in (ipn(signature="AAAA"), ipn(signature=sig("1000.00000000", "ORD1", "wrong"))):
            with self.assertRaises(InvalidSignatureError):
                dz.verify_ipn(bad)
        with self.assertRaisesRegex(InvalidSignatureError, "another order"):
            dz.verify_ipn(ipn(), "OTHER", 1000)
        with self.assertRaisesRegex(InvalidSignatureError, "amount"):
            dz.verify_ipn(ipn(), "ORD1", 999)
        with self.assertRaisesRegex(InvalidSignatureError, "incomplete"):
            dz.verify_ipn({"status": "success"})
        t = ipn()
        t["data"]["amount"] = "1.00000000"
        with self.assertRaises(InvalidSignatureError):
            dz.verify_ipn(t)
        self.assertFalse(dz.verify_ipn(ipn(status="rejected")).is_success)

    def test_parse_ipn_form(self):
        raw = "status=success&signature=%s&identifier=ORD1&data%%5Bamount%%5D=1000.00000000&data%%5Bpayment_trx%%5D=T1" % sig("1000.00000000", "ORD1")
        dz, _ = mk()
        self.assertTrue(dz.verify_ipn(parse_ipn_form(raw), "ORD1", 1000).is_success)

    def test_new_identifier(self):
        i = DzWallet.new_identifier("ORD")
        self.assertTrue(len(i) <= 20 and i.startswith("ORD"))
        self.assertTrue(len(DzWallet.new_identifier("A-B_C!!!!!!")) <= 20)

    def test_v2_webhook(self):
        dz = DzWallet(public_key="pk", secret_key="test_sec", mode="test")
        raw = "event=payment.succeeded&event_id=evt_abc&status=success&identifier=ORD1&data%5Bamount%5D=100.00&data%5Bpayment_trx%5D=T1&timestamp=1700000000"

        def hdr(t, body, secret="test_sec"):
            return "t=%d,v1=%s" % (t, hmac.new(secret.encode(), ("%d." % t).encode() + body.encode(), hashlib.sha256).hexdigest())

        m = dz.verify_webhook(raw, hdr(1700000000, raw), now=1700000100)
        self.assertEqual(m, {"timestamp": 1700000000, "event_id": "evt_abc"})
        self.assertEqual(dz.verify_webhook(raw.encode(), hdr(1700000000, raw), now=1700000100)["event_id"], "evt_abc")
        for bad in (
            lambda: dz.verify_webhook(raw + "x", hdr(1700000000, raw), now=1700000100),
            lambda: dz.verify_webhook(raw, hdr(1700000000, raw), now=1700001000),
            lambda: dz.verify_webhook(raw, hdr(1700000000, raw), now=1699999000),
            lambda: dz.verify_webhook(raw, "garbage", now=1700000100),
            lambda: dz.verify_webhook(raw, hdr(1700000000, raw, "other"), now=1700000100),
            lambda: dz.verify_webhook(raw, hdr(1700000000, raw), now=1700000100, event_id_header="evt_other"),
        ):
            with self.assertRaises(InvalidSignatureError):
                bad()
        import time as _t
        t = int(_t.time())
        raw2 = "event_id=evt_zz&status=success&identifier=ORD9&data%%5Bamount%%5D=50.00&data%%5Bpayment_trx%%5D=T9&timestamp=%d" % t
        ipn_obj, eid = dz.verify_notification(raw2, {"X-DzWallet-Signature": hdr(t, raw2), "X-DzWallet-Event-Id": "evt_zz"})
        self.assertEqual((eid, ipn_obj.identifier, ipn_obj.trx, ipn_obj.is_success), ("evt_zz", "ORD9", "T9", True))
        legacy = "status=success&identifier=ORD1&signature=%s&data%%5Bamount%%5D=100.00" % hmac.new(b"test_sec", b"100.00ORD1", hashlib.sha256).hexdigest().upper()
        self.assertIsNone(dz.verify_notification(legacy, {})[1])
        with self.assertRaises(InvalidSignatureError):
            dz.verify_notification(legacy, {}, require_v2=True)


if __name__ == "__main__":
    unittest.main()
