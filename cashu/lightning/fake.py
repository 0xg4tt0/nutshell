import asyncio
import hashlib
import math
import random
from datetime import datetime
from os import urandom
from typing import AsyncGenerator, Dict, Optional, Set

from bolt11 import (
    Bolt11,
    MilliSatoshi,
    TagChar,
    Tags,
    decode,
    encode,
)

from ..core.base import Amount, MeltQuote, Unit
from ..core.helpers import fee_reserve
from ..core.models import PostMeltQuoteRequest
from ..core.settings import settings
from .base import (
    InvoiceResponse,
    LightningBackend,
    PaymentQuoteResponse,
    PaymentResponse,
    PaymentStatus,
    StatusResponse,
)


class FakeWallet(LightningBackend):
    fake_btc_price = 1e8 / 1337
    queue: asyncio.Queue[Bolt11] = asyncio.Queue(0)
    payment_secrets: Dict[str, str] = dict()
    paid_invoices: Set[str] = set()
    secret: str = "FAKEWALLET SECRET"
    privkey: str = hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode(),
        ("FakeWallet").encode(),
        2048,
        32,
    ).hex()

    supported_units = set([Unit.sat, Unit.msat, Unit.usd])
    unit = Unit.sat

    def __init__(self, unit: Unit = Unit.sat, **kwargs):
        self.assert_unit_supported(unit)
        self.unit = unit

    async def status(self) -> StatusResponse:
        return StatusResponse(error_message=None, balance=1337)

    async def create_invoice(
        self,
        amount: Amount,
        memo: Optional[str] = None,
        description_hash: Optional[bytes] = None,
        unhashed_description: Optional[bytes] = None,
        expiry: Optional[int] = None,
        payment_secret: Optional[bytes] = None,
    ) -> InvoiceResponse:
        self.assert_unit_supported(amount.unit)
        tags = Tags()

        if description_hash:
            tags.add(TagChar.description_hash, description_hash.hex())
        elif unhashed_description:
            tags.add(
                TagChar.description_hash,
                hashlib.sha256(unhashed_description).hexdigest(),
            )
        else:
            tags.add(TagChar.description, memo or "")

        tags.add(TagChar.expire_time, expiry or 3600)

        if payment_secret:
            secret = payment_secret.hex()
        else:
            secret = urandom(32).hex()
        tags.add(TagChar.payment_secret, secret)

        payment_hash = hashlib.sha256(secret.encode()).hexdigest()

        tags.add(TagChar.payment_hash, payment_hash)

        self.payment_secrets[payment_hash] = secret

        amount_msat = 0
        if self.unit == Unit.sat:
            amount_msat = MilliSatoshi(amount.to(Unit.msat, round="up").amount)
        elif self.unit == Unit.usd:
            amount_msat = MilliSatoshi(
                math.ceil(amount.amount / self.fake_btc_price * 1e9)
            )
        else:
            raise NotImplementedError()

        bolt11 = Bolt11(
            currency="bc",
            amount_msat=amount_msat,
            date=int(datetime.now().timestamp()),
            tags=tags,
        )

        payment_request = encode(bolt11, self.privkey)

        return InvoiceResponse(
            ok=True, checking_id=payment_hash, payment_request=payment_request
        )

    async def pay_invoice(self, quote: MeltQuote, fee_limit: int) -> PaymentResponse:
        invoice = decode(quote.request)

        if settings.fakewallet_delay_payment:
            await asyncio.sleep(5)

        if invoice.payment_hash in self.payment_secrets or settings.fakewallet_brr:
            await self.queue.put(invoice)
            self.paid_invoices.add(invoice.payment_hash)
            return PaymentResponse(
                ok=True,
                checking_id=invoice.payment_hash,
                fee=Amount(unit=self.unit, amount=1),
                preimage=self.payment_secrets.get(invoice.payment_hash) or "0" * 64,
            )
        else:
            return PaymentResponse(
                ok=False, error_message="Only internal invoices can be used!"
            )

    async def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        if settings.fakewallet_stochastic_invoice:
            paid = random.random() > 0.7
            return PaymentStatus(paid=paid)
        paid = checking_id in self.paid_invoices or settings.fakewallet_brr
        return PaymentStatus(paid=paid or None)

    async def get_payment_status(self, _: str) -> PaymentStatus:
        return PaymentStatus(paid=settings.fakewallet_payment_state)

    async def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        while True:
            value: Bolt11 = await self.queue.get()
            yield value.payment_hash

    # async def get_invoice_quote(self, bolt11: str) -> InvoiceQuoteResponse:
    #     invoice_obj = decode(bolt11)
    #     assert invoice_obj.amount_msat, "invoice has no amount."
    #     amount = invoice_obj.amount_msat
    #     return InvoiceQuoteResponse(checking_id="", amount=amount)

    async def get_payment_quote(
        self, melt_quote: PostMeltQuoteRequest
    ) -> PaymentQuoteResponse:
        invoice_obj = decode(melt_quote.request)
        assert invoice_obj.amount_msat, "invoice has no amount."

        if self.unit == Unit.sat:
            amount_msat = int(invoice_obj.amount_msat)
            fees_msat = fee_reserve(amount_msat)
            fees = Amount(unit=Unit.msat, amount=fees_msat)
            amount = Amount(unit=Unit.msat, amount=amount_msat)
        elif self.unit == Unit.usd:
            amount_usd = math.ceil(invoice_obj.amount_msat / 1e9 * self.fake_btc_price)
            amount = Amount(unit=Unit.usd, amount=amount_usd)
            fees = Amount(unit=Unit.usd, amount=2)
        else:
            raise NotImplementedError()

        return PaymentQuoteResponse(
            checking_id=invoice_obj.payment_hash,
            fee=fees.to(self.unit, round="up"),
            amount=amount.to(self.unit, round="up"),
        )
