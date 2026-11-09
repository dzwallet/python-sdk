# dzwallet (Python)

Python SDK for the [DzWallet](https://dzwallet.app) payment API: accept **CIB** and **Edahabia** cards (through SATIM), create **payment links and QR codes**, check payment status and verify the signed payment notification (IPN). No dependencies (standard library only). Python 3.8+.

> Money is only received when the status is `success`. Never mark an order as paid because the customer reached your `success_url`: use the signed IPN, or call `payment_status()` from your server.
> The secret key is for servers only. Never put it in a browser or mobile app.

## Install

```bash
pip install dzwallet
```

## Configure

```python
from dzwallet import DzWallet

dz = DzWallet(
    mode="test",                 # test | live
    public_key="...",            # TEST key in test mode, LIVE key in live mode
    secret_key="...",            # server side only
)
```

Keys: merchant dashboard, **Setting > API Key**. Live keys exist after identity (KYC) verification. Register your domain in **Your website domains**: `ipn_url`, `success_url` and `cancel_url` must belong to it.

## Take a payment

```python
identifier = DzWallet.new_identifier("ORD")      # max 20 characters, save with your order first
redirect_url, _ = dz.initiate_payment(
    identifier=identifier, amount=2500, details="Order #1042", site_name="My shop",
    ipn_url="https://yourshop.dz/dzwallet/ipn",
    success_url="https://yourshop.dz/thanks",
    cancel_url="https://yourshop.dz/cart",
    customer=dict(first_name="Amine", last_name="B.", email="amine@example.dz", mobile="0555000000"),
)
# redirect the customer to redirect_url (test mode: simulated page. Live mode: SATIM card page)
```

## Receive the notification (IPN)

Every notification is signed on the **exact raw body** (header `X-DzWallet-Signature: t=<unix>,v1=<hex>`, `v1 = HMAC-SHA256(secret, t + "." + raw body)`), with a signed timestamp and a unique event id. Use the raw bytes, not a parsed form:

```python
from dzwallet import InvalidSignatureError

# Flask
@app.post("/dzwallet/ipn")
def ipn_view():
    try:
        ipn, event_id = dz.verify_notification(request.get_data(), request.headers)   # signature + 5 minute window
    except InvalidSignatureError:
        abort(403)
    if already_processed(event_id):                     # unique constraint on event id
        return "OK", 200
    order = find_order(ipn.identifier)                  # your own code
    if abs(float(ipn.amount) - float(order.total)) > 0.005:   # amount must match YOUR order
        abort(400)
    if ipn.is_success and not order.paid:               # fulfil once
        order.mark_paid(ipn.trx)
    record_event(event_id)
    return "OK", 200                                    # answer quickly with 2xx
```

- `verify_notification(raw_body, headers, tolerance=300, require_v2=False)` uses the v2 signature when DzWallet sends it and falls back to the older `signature` field (uppercase HMAC of `data.amount + identifier`) otherwise. Pass `require_v2=True` to refuse the older one. Django: `request.body` and `request.headers`.
- The event id is read from the signed body, so a replayed request cannot dodge the duplicate check by changing a header. It is the same on every retry and on manual redelivery.
- Lower level: `verify_webhook(raw_body, header)` returns `{"timestamp": ..., "event_id": ...}`; `parse_ipn_form(text)` and `verify_ipn(dict)` still work.
- Retries: DzWallet retries unsuccessful deliveries with increasing delays for about 12 hours. Failed deliveries can be resent from the merchant dashboard (Webhook Log).

## Status and payment links

```python
s = dz.payment_status(identifier)                # s.is_paid, s.status, s.trx
link = dz.create_payment_link(2500, reference="ORD1042", days=7, note="Room 12")   # live keys
dz.payment_link("ORD1042")
dz.reactivate_payment_link("ORD1042")
```

## Errors

`ConfigurationError` (key missing), `ValueError` (bad parameter, nothing sent), `ApiError` (`.errors`, `.http_status`, `.is_rate_limited`), `ConnectionFailed` (timeout: after `initiate_payment` the result is **unknown**, call `payment_status()` first), `InvalidSignatureError`. All except `ValueError` extend `DzWalletError`.

`initiate_payment` is never retried automatically; status and link reads retry on connection errors. Limit: 60 requests per minute per IP.

## Tests

```bash
python -m unittest discover -s tests
```

They use a fake transport and never call DzWallet.

MIT licence.
