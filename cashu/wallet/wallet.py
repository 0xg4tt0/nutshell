import copy
import time
from typing import Dict, List, Optional, Tuple, Union

import bolt11
from bip32 import BIP32
from loguru import logger

from ..core.base import (
    BlindedMessage,
    BlindedSignature,
    DLEQWallet,
    Invoice,
    Proof,
    SpentState,
    Unit,
    WalletKeyset,
)
from ..core.crypto import b_dhke
from ..core.crypto.secp import PrivateKey, PublicKey
from ..core.db import Database
from ..core.errors import KeysetNotFoundError
from ..core.helpers import amount_summary, calculate_number_of_blank_outputs, sum_proofs
from ..core.migrations import migrate_databases
from ..core.models import (
    PostCheckStateResponse,
    PostMeltQuoteResponse,
    PostMeltResponse,
)
from ..core.p2pk import Secret
from ..core.settings import settings
from ..core.split import amount_split
from ..wallet.crud import (
    bump_secret_derivation,
    get_keysets,
    get_proofs,
    invalidate_proof,
    secret_used,
    set_secret_derivation,
    store_keyset,
    store_lightning_invoice,
    store_proof,
    update_keyset,
    update_lightning_invoice,
    update_proof,
)
from . import migrations
from .htlc import WalletHTLC
from .mint_info import MintInfo
from .p2pk import WalletP2PK
from .proofs import WalletProofs
from .secrets import WalletSecrets
from .transactions import WalletTransactions
from .v1_api import LedgerAPI


class Wallet(
    LedgerAPI, WalletP2PK, WalletHTLC, WalletSecrets, WalletTransactions, WalletProofs
):
    """
    Nutshell wallet class.

    This class is the main interface to the Nutshell wallet. It is a subclass of the
    LedgerAPI class, which provides the API methods to interact with the mint.

    To use `Wallet`, initialize it with the mint URL and the path to the database directory.

    Initialize the wallet with `Wallet.with_db(url, db)`. This will load the private key and
     all keysets from the database.

    Use `load_proofs` to load all proofs of the selected mint and unit from the database.

    Use `load_mint` to load the public keys of the mint and fetch those that we don't have.
    This will also load the mint info.

    Use `mint_quote` to request a Lightning invoice for minting tokens.
    Use `mint` to mint tokens of a specific amount after an invoice has been paid.
    Use `melt_quote` to fetch a quote for paying a Lightning invoice.
    Use `melt` to pay a Lightning invoice.
    """

    keyset_id: str  # holds current keyset id
    keysets: Dict[str, WalletKeyset]  # holds keysets
    # mint_keyset_ids: List[str]  # holds active keyset ids of the mint
    unit: Unit
    mint_info: MintInfo  # holds info about mint
    mnemonic: str  # holds mnemonic of the wallet
    seed: bytes  # holds private key of the wallet generated from the mnemonic
    db: Database
    bip32: BIP32
    # private_key: Optional[PrivateKey] = None

    def __init__(self, url: str, db: str, name: str = "no_name", unit: str = "sat"):
        """A Cashu wallet.

        Args:
            url (str): URL of the mint.
            db (str): Path to the database directory.
            name (str, optional): Name of the wallet database file. Defaults to "no_name".
        """
        self.db = Database("wallet", db)
        self.proofs: List[Proof] = []
        self.name = name
        self.unit = Unit[unit]

        super().__init__(url=url, db=self.db)
        logger.debug("Wallet initialized")
        logger.debug(f"Mint URL: {url}")
        logger.debug(f"Database: {db}")
        logger.debug(f"Unit: {self.unit.name}")

    @classmethod
    async def with_db(
        cls,
        url: str,
        db: str,
        name: str = "no_name",
        skip_db_read: bool = False,
        unit: str = "sat",
    ):
        """Initializes a wallet with a database and initializes the private key.

        Args:
            url (str): URL of the mint.
            db (str): Path to the database.
            name (str, optional): Name of the wallet. Defaults to "no_name".
            skip_db_read (bool, optional): If true, values from db like private key and
                keysets are not loaded. Useful for running only migrations and returning.
                Defaults to False.

        Returns:
            Wallet: Initialized wallet.
        """
        logger.trace(f"Initializing wallet with database: {db}")
        self = cls(url=url, db=db, name=name, unit=unit)
        await self._migrate_database()
        if not skip_db_read:
            logger.trace("Mint init: loading private key and keysets from db.")
            await self._init_private_key()
            keysets_list = await get_keysets(mint_url=url, db=self.db)
            keysets_active_unit = [k for k in keysets_list if k.unit == self.unit]
            self.keysets = {k.id: k for k in keysets_active_unit}
            logger.debug(
                f"Loaded keysets: {' '.join([k.id + f' {k.unit}' for k in keysets_active_unit])}"
            )

        return self

    async def _migrate_database(self):
        try:
            await migrate_databases(self.db, migrations)
        except Exception as e:
            logger.error(f"Could not run migrations: {e}")
            raise e

    # ---------- API ----------

    async def load_mint_info(self) -> MintInfo:
        """Loads the mint info from the mint."""
        mint_info_resp = await self._get_info()
        self.mint_info = MintInfo(**mint_info_resp.dict())
        logger.debug(f"Mint info: {self.mint_info}")
        return self.mint_info

    async def load_mint_keysets(self):
        """Loads all keyset of the mint and makes sure we have them all in the database.

        Then loads all keysets from the database for the active mint and active unit into self.keysets.
        """
        logger.trace("Loading mint keysets.")
        mint_keysets_resp = await self._get_keysets()
        mint_keysets_dict = {k.id: k for k in mint_keysets_resp}

        # load all keysets of thisd mint from the db
        keysets_in_db = await get_keysets(mint_url=self.url, db=self.db)

        # db is empty, get all keys from the mint and store them
        if not keysets_in_db:
            all_keysets = await self._get_keys()
            for keyset in all_keysets:
                keyset.active = mint_keysets_dict[keyset.id].active
                keyset.input_fee_ppk = mint_keysets_dict[keyset.id].input_fee_ppk or 0
                await store_keyset(keyset=keyset, db=self.db)

        keysets_in_db = await get_keysets(mint_url=self.url, db=self.db)
        keysets_in_db_dict = {k.id: k for k in keysets_in_db}

        # get all new keysets that are not in memory yet and store them in the database
        for mint_keyset in mint_keysets_dict.values():
            if mint_keyset.id not in keysets_in_db_dict:
                logger.debug(
                    f"Storing new mint keyset: {mint_keyset.id} ({mint_keyset.unit})"
                )
                wallet_keyset = await self._get_keyset(mint_keyset.id)
                wallet_keyset.active = mint_keyset.active
                wallet_keyset.input_fee_ppk = mint_keyset.input_fee_ppk or 0
                await store_keyset(keyset=wallet_keyset, db=self.db)

        for mint_keyset in mint_keysets_dict.values():
            # if the active or the fee attributes have changed, update them in the database
            if mint_keyset.id in keysets_in_db_dict:
                changed = False
                if mint_keyset.active != keysets_in_db_dict[mint_keyset.id].active:
                    keysets_in_db_dict[mint_keyset.id].active = mint_keyset.active
                    changed = True
                if (
                    mint_keyset.input_fee_ppk
                    and mint_keyset.input_fee_ppk
                    != keysets_in_db_dict[mint_keyset.id].input_fee_ppk
                ):
                    keysets_in_db_dict[
                        mint_keyset.id
                    ].input_fee_ppk = mint_keyset.input_fee_ppk
                    changed = True
                if changed:
                    await update_keyset(
                        keyset=keysets_in_db_dict[mint_keyset.id], db=self.db
                    )

        await self.load_keysets_from_db()

    async def activate_keyset(self, keyset_id: Optional[str] = None) -> None:
        """Activates a keyset by setting self.keyset_id. Either activates a specific keyset
        of chooses one of the active keysets of the mint with the same unit as the wallet.
        """

        if keyset_id:
            if keyset_id not in self.keysets:
                await self.load_mint_keysets()

            if keyset_id not in self.keysets:
                raise KeysetNotFoundError(keyset_id)

            if self.keysets[keyset_id].unit != self.unit:
                raise Exception(
                    f"Keyset {keyset_id} has unit {self.keysets[keyset_id].unit.name},"
                    f" but wallet has unit {self.unit.name}."
                )

            if not self.keysets[keyset_id].active:
                raise Exception(f"Keyset {keyset_id} is not active.")

            self.keyset_id = keyset_id
        else:
            # if no keyset_id is given, choose an active keyset with the same unit as the wallet
            chosen_keyset = None
            for keyset in self.keysets.values():
                if keyset.unit == self.unit and keyset.active:
                    chosen_keyset = keyset
                    break

            if not chosen_keyset:
                raise Exception(f"No active keyset found for unit {self.unit.name}.")

            self.keyset_id = chosen_keyset.id

        logger.debug(f"Activated keyset {self.keyset_id}")

    async def load_mint(self, keyset_id: str = "") -> None:
        """
        Loads the public keys of the mint. Either gets the keys for the specified
        `keyset_id` or gets the keys of the active keyset from the mint.
        Gets the active keyset ids of the mint and stores in `self.mint_keyset_ids`.
        """
        logger.trace("Loading mint.")
        await self.load_mint_keysets()
        await self.activate_keyset(keyset_id)
        try:
            await self.load_mint_info()
        except Exception as e:
            logger.debug(f"Could not load mint info: {e}")
            pass

    async def load_proofs(self, reload: bool = False) -> None:
        """Load all proofs of the selected mint and unit (i.e. self.keysets) into memory."""

        if self.proofs and not reload:
            logger.debug("Proofs already loaded.")
            return

        self.proofs = []
        await self.load_keysets_from_db()
        async with self.db.connect() as conn:
            for keyset_id in self.keysets:
                proofs = await get_proofs(db=self.db, id=keyset_id, conn=conn)
                self.proofs.extend(proofs)
        logger.trace(
            f"Proofs loaded for keysets: {' '.join([k.id + f' ({k.unit})' for k in self.keysets.values()])}"
        )

    async def load_keysets_from_db(
        self, url: Union[str, None] = "", unit: Union[str, None] = ""
    ):
        """Load all keysets of the selected mint and unit from the database into self.keysets."""
        # so that the caller can set unit = None, otherwise use defaults
        if unit == "":
            unit = self.unit.name
        if url == "":
            url = self.url
        keysets = await get_keysets(mint_url=url, unit=unit, db=self.db)
        for keyset in keysets:
            self.keysets[keyset.id] = keyset
        logger.trace(
            f"Loaded keysets from db: {[(k.id, k.unit.name, k.input_fee_ppk) for k in self.keysets.values()]}"
        )

    async def _check_used_secrets(self, secrets):
        """Checks if any of the secrets have already been used"""
        logger.trace("Checking secrets.")
        for s in secrets:
            if await secret_used(s, db=self.db):
                raise Exception(f"secret already used: {s}")
        logger.trace("Secret check complete.")

    async def request_mint(self, amount: int) -> Invoice:
        """Request a Lightning invoice for minting tokens.

        Args:
            amount (int): Amount for Lightning invoice in satoshis

        Returns:
            PostMintQuoteResponse: Mint Quote Response
        """
        mint_quote_response = await super().mint_quote(amount, self.unit)
        decoded_invoice = bolt11.decode(mint_quote_response.request)
        invoice = Invoice(
            amount=amount,
            bolt11=mint_quote_response.request,
            payment_hash=decoded_invoice.payment_hash,
            id=mint_quote_response.quote,
            out=False,
            time_created=int(time.time()),
        )
        await store_lightning_invoice(db=self.db, invoice=invoice)
        return invoice

    def split_wallet_state(self, amount: int) -> List[int]:
        """This function produces an amount split for outputs based on the current state of the wallet.
        Its objective is to fill up the wallet so that it reaches `n_target` coins of each amount.

        Args:
            amount (int): Amount to split

        Returns:
            List[int]: List of amounts to mint
        """
        # read the target count for each amount from settings
        n_target = settings.wallet_target_amount_count
        amounts_we_have = [p.amount for p in self.proofs if p.reserved is not True]
        amounts_we_have.sort()
        # NOTE: Do not assume 2^n here
        all_possible_amounts: list[int] = [2**i for i in range(settings.max_order)]
        amounts_we_want_ll = [
            [a] * max(0, n_target - amounts_we_have.count(a))
            for a in all_possible_amounts
        ]
        # flatten list of lists to list
        amounts_we_want = [item for sublist in amounts_we_want_ll for item in sublist]
        # sort by increasing amount
        amounts_we_want.sort()

        logger.debug(
            f"Amounts we have: {[(a, amounts_we_have.count(a)) for a in set(amounts_we_have)]}"
        )
        amounts: list[int] = []
        while sum(amounts) < amount and amounts_we_want:
            if sum(amounts) + amounts_we_want[0] > amount:
                break
            amounts.append(amounts_we_want.pop(0))

        remaining_amount = amount - sum(amounts)
        if remaining_amount > 0:
            amounts += amount_split(remaining_amount)

        logger.debug(f"Amounts we want: {amounts}")
        if sum(amounts) != amount:
            raise Exception(f"Amounts do not sum to {amount}.")

        return amounts

    async def mint_quote(self, amount: int) -> Invoice:
        """Request a Lightning invoice for minting tokens.

        Args:
            amount (int): Amount for Lightning invoice in satoshis

        Returns:
            Invoice: Lightning invoice for minting tokens
        """
        mint_quote_response = await super().mint_quote(amount, self.unit)
        decoded_invoice = bolt11.decode(mint_quote_response.request)
        invoice = Invoice(
            amount=amount,
            bolt11=mint_quote_response.request,
            payment_hash=decoded_invoice.payment_hash,
            id=mint_quote_response.quote,
            out=False,
            time_created=int(time.time()),
        )
        await store_lightning_invoice(db=self.db, invoice=invoice)
        return invoice

    async def mint(
        self,
        amount: int,
        id: str,
        split: Optional[List[int]] = None,
    ) -> List[Proof]:
        """Mint tokens of a specific amount after an invoice has been paid.

        Args:
            amount (int): Total amount of tokens to be minted
            id (str): Id for looking up the paid Lightning invoice.
            split (Optional[List[str]], optional): List of desired amount splits to be minted. Total must sum to `amount`.

        Raises:
            Exception: Raises exception if `amounts` does not sum to `amount` or has unsupported value.
            Exception: Raises exception if no proofs have been provided

        Returns:
            List[Proof]: Newly minted proofs.
        """
        # specific split
        if split:
            logger.trace(f"Mint with split: {split}")
            assert sum(split) == amount, "split must sum to amount"
            allowed_amounts = [2**i for i in range(settings.max_order)]
            for a in split:
                if a not in allowed_amounts:
                    raise Exception(
                        f"Can only mint amounts with 2^n up to {2**settings.max_order}."
                    )

        # split based on our wallet state
        amounts = split or self.split_wallet_state(amount)
        # if no split was specified, we use the canonical split
        # amounts = split or amount_split(amount)

        # quirk: we skip bumping the secret counter in the database since we are
        # not sure if the minting will succeed. If it succeeds, we will bump it
        # in the next step.
        secrets, rs, derivation_paths = await self.generate_n_secrets(
            len(amounts), skip_bump=True
        )
        await self._check_used_secrets(secrets)
        outputs, rs = self._construct_outputs(amounts, secrets, rs)

        # will raise exception if mint is unsuccessful
        promises = await super().mint(outputs, id)

        promises_keyset_id = promises[0].id
        await bump_secret_derivation(
            db=self.db, keyset_id=promises_keyset_id, by=len(amounts)
        )
        proofs = await self._construct_proofs(promises, secrets, rs, derivation_paths)

        if id:
            await update_lightning_invoice(
                db=self.db, id=id, paid=True, time_paid=int(time.time())
            )
            # store the mint_id in proofs
            async with self.db.connect() as conn:
                for p in proofs:
                    p.mint_id = id
                    await update_proof(p, mint_id=id, conn=conn)
        return proofs

    async def redeem(
        self,
        proofs: List[Proof],
    ) -> Tuple[List[Proof], List[Proof]]:
        """Redeem proofs by sending them to yourself (by calling a split).)
        Calls `add_witnesses_to_proofs` which parses all proofs and checks whether their
        secrets corresponds to any locks that we have the unlock conditions for. If so,
        it adds the unlock conditions to the proofs.
        Args:
            proofs (List[Proof]): Proofs to be redeemed.
        """
        # verify DLEQ of incoming proofs
        self.verify_proofs_dleq(proofs)
        return await self.split(proofs=proofs, amount=0)

    def swap_send_and_keep_output_amounts(
        self, proofs: List[Proof], amount: int, fees: int = 0
    ) -> Tuple[List[int], List[int]]:
        """This function generates a suitable amount split for the outputs to keep and the outputs to send. It
        calculates the amount to keep based on the wallet state and the amount to send based on the amount
        provided.

        Args:
            proofs (List[Proof]): Proofs to be split.
            amount (int): Amount to be sent.

        Returns:
            Tuple[List[int], List[int]]: Two lists of amounts, one for keeping and one for sending.
        """
        # create a suitable amount split based on the proofs provided
        total = sum_proofs(proofs)
        keep_amt, send_amt = total - amount, amount
        logger.trace(f"Keep amount: {keep_amt}, send amount: {send_amt}")
        logger.trace(f"Total input: {sum_proofs(proofs)}")
        # generate splits for outputs
        send_outputs = amount_split(send_amt)

        # we subtract the fee for the entire transaction from the amount to keep
        keep_amt -= self.get_fees_for_proofs(proofs)
        logger.trace(f"Keep amount: {keep_amt}")

        # we determine the amounts to keep based on the wallet state
        keep_outputs = self.split_wallet_state(keep_amt)

        return keep_outputs, send_outputs

    async def split(
        self,
        proofs: List[Proof],
        amount: int,
        secret_lock: Optional[Secret] = None,
    ) -> Tuple[List[Proof], List[Proof]]:
        """Calls the swap API to split the proofs into two sets of proofs, one for keeping and one for sending.

        If secret_lock is None, random secrets will be generated for the tokens to keep (keep_outputs)
        and the promises to send (send_outputs). If secret_lock is provided, the wallet will create
        blinded secrets with those to attach a predefined spending condition to the tokens they want to send.

        Args:
            proofs (List[Proof]): Proofs to be split.
            amount (int): Amount to be sent.
            secret_lock (Optional[Secret], optional): Secret to lock the tokens to be sent. Defaults to None.

        Returns:
            Tuple[List[Proof], List[Proof]]: Two lists of proofs, one for keeping and one for sending.
        """
        assert len(proofs) > 0, "no proofs provided."
        assert sum_proofs(proofs) >= amount, "amount too large."
        assert amount >= 0, "amount can't be negative."
        # make sure we're operating on an independent copy of proofs
        proofs = copy.copy(proofs)

        # potentially add witnesses to unlock provided proofs (if they indicate one)
        proofs = await self.add_witnesses_to_proofs(proofs)

        input_fees = self.get_fees_for_proofs(proofs)
        logger.debug(f"Input fees: {input_fees}")
        # create a suitable amount lists to keep and send based on the proofs
        # provided and the state of the wallet
        keep_outputs, send_outputs = self.swap_send_and_keep_output_amounts(
            proofs, amount, input_fees
        )

        amounts = keep_outputs + send_outputs
        # generate secrets for new outputs
        if secret_lock is None:
            secrets, rs, derivation_paths = await self.generate_n_secrets(len(amounts))
        else:
            secrets, rs, derivation_paths = await self.generate_locked_secrets(
                send_outputs, keep_outputs, secret_lock
            )

        assert len(secrets) == len(
            amounts
        ), "number of secrets does not match number of outputs"
        # verify that we didn't accidentally reuse a secret
        await self._check_used_secrets(secrets)

        # construct outputs
        outputs, rs = self._construct_outputs(amounts, secrets, rs)

        # potentially add witnesses to outputs based on what requirement the proofs indicate
        outputs = await self.add_witnesses_to_outputs(proofs, outputs)

        # Call swap API
        promises = await super().split(proofs, outputs)

        # Construct proofs from returned promises (i.e., unblind the signatures)
        new_proofs = await self._construct_proofs(
            promises, secrets, rs, derivation_paths
        )

        await self.invalidate(proofs)

        keep_proofs = new_proofs[: len(keep_outputs)]
        send_proofs = new_proofs[len(keep_outputs) :]
        return keep_proofs, send_proofs

    async def melt_quote(
        self, invoice: str, amount: Optional[int] = None
    ) -> PostMeltQuoteResponse:
        """
        Fetches a melt quote from the mint and either uses the amount in the invoice or the amount provided.
        """
        if amount and not self.mint_info.supports_mpp("bolt11", self.unit):
            raise Exception("Mint does not support MPP, cannot specify amount.")
        melt_quote = await super().melt_quote(invoice, self.unit, amount)
        logger.debug(
            f"Mint wants {self.unit.str(melt_quote.fee_reserve)} as fee reserve."
        )
        return melt_quote

    async def melt(
        self, proofs: List[Proof], invoice: str, fee_reserve_sat: int, quote_id: str
    ) -> PostMeltResponse:
        """Pays a lightning invoice and returns the status of the payment.

        Args:
            proofs (List[Proof]): List of proofs to be spent.
            invoice (str): Lightning invoice to be paid.
            fee_reserve_sat (int): Amount of fees to be reserved for the payment.

        """
        # Make sure we're operating on an independent copy of proofs
        proofs = copy.copy(proofs)

        # Generate a number of blank outputs for any overpaid fees. As described in
        # NUT-08, the mint will imprint these outputs with a value depending on the
        # amount of fees we overpaid.
        n_change_outputs = calculate_number_of_blank_outputs(fee_reserve_sat)
        (
            change_secrets,
            change_rs,
            change_derivation_paths,
        ) = await self.generate_n_secrets(n_change_outputs)
        change_outputs, change_rs = self._construct_outputs(
            n_change_outputs * [1], change_secrets, change_rs
        )

        # store the melt_id in proofs db
        async with self.db.connect() as conn:
            for p in proofs:
                p.melt_id = quote_id
                await update_proof(p, melt_id=quote_id, conn=conn)

        # we store the invoice object in the database to later be able to check the invoice state

        decoded_invoice = bolt11.decode(invoice)
        invoice_obj = Invoice(
            amount=-sum_proofs(proofs),
            bolt11=invoice,
            payment_hash=decoded_invoice.payment_hash,
            # preimage=status.preimage,
            paid=False,
            time_paid=int(time.time()),
            id=quote_id,  # store the same ID in the invoice
            out=True,  # outgoing invoice
        )
        # store invoice in db as not paid yet
        await store_lightning_invoice(db=self.db, invoice=invoice_obj)

        status = await super().melt(quote_id, proofs, change_outputs)

        # if payment fails
        if not status.paid:
            # remove the melt_id in proofs
            for p in proofs:
                p.melt_id = None
                await update_proof(p, melt_id=None, db=self.db)
            raise Exception("could not pay invoice.")

        # invoice was paid successfully

        await self.invalidate(proofs)

        # update paid status in db
        logger.trace(f"Settings invoice {quote_id} to paid.")
        await update_lightning_invoice(
            db=self.db,
            id=quote_id,
            paid=True,
            time_paid=int(time.time()),
            preimage=status.payment_preimage,
        )

        # handle change and produce proofs
        if status.change:
            change_proofs = await self._construct_proofs(
                status.change,
                change_secrets[: len(status.change)],
                change_rs[: len(status.change)],
                change_derivation_paths[: len(status.change)],
            )
            logger.debug(f"Received change: {self.unit.str(sum_proofs(change_proofs))}")
        return status

    async def check_proof_state(self, proofs) -> PostCheckStateResponse:
        return await super().check_proof_state(proofs)

    # ---------- TOKEN MECHANICS ----------

    # ---------- DLEQ PROOFS ----------

    def verify_proofs_dleq(self, proofs: List[Proof]):
        """Verifies DLEQ proofs in proofs."""
        for proof in proofs:
            if not proof.dleq:
                logger.trace("No DLEQ proof in proof.")
                return
            logger.trace("Verifying DLEQ proof.")
            assert proof.id
            assert (
                proof.id in self.keysets
            ), f"Keyset {proof.id} not known, can not verify DLEQ."
            if not b_dhke.carol_verify_dleq(
                secret_msg=proof.secret,
                C=PublicKey(bytes.fromhex(proof.C), raw=True),
                r=PrivateKey(bytes.fromhex(proof.dleq.r), raw=True),
                e=PrivateKey(bytes.fromhex(proof.dleq.e), raw=True),
                s=PrivateKey(bytes.fromhex(proof.dleq.s), raw=True),
                A=self.keysets[proof.id].public_keys[proof.amount],
            ):
                raise Exception("DLEQ proof invalid.")
            else:
                logger.trace("DLEQ proof valid.")
        logger.debug("Verified incoming DLEQ proofs.")

    async def _construct_proofs(
        self,
        promises: List[BlindedSignature],
        secrets: List[str],
        rs: List[PrivateKey],
        derivation_paths: List[str],
    ) -> List[Proof]:
        """Constructs proofs from promises, secrets, rs and derivation paths.

        This method is called after the user has received blind signatures from
        the mint. The results are proofs that can be used as ecash.

        Args:
            promises (List[BlindedSignature]): blind signatures from mint
            secrets (List[str]): secrets that were previously used to create blind messages (that turned into promises)
            rs (List[PrivateKey]): blinding factors that were previously used to create blind messages (that turned into promises)
            derivation_paths (List[str]): derivation paths that were used to generate secrets and blinding factors

        Returns:
            List[Proof]: list of proofs that can be used as ecash
        """
        logger.trace("Constructing proofs.")
        proofs: List[Proof] = []
        for promise, secret, r, path in zip(promises, secrets, rs, derivation_paths):
            if promise.id not in self.keysets:
                logger.debug(f"Keyset {promise.id} not found in db. Loading from mint.")
                # we don't have the keyset for this promise, so we load all keysets from the mint
                await self.load_mint_keysets()
                assert promise.id in self.keysets, "Could not load keyset."
            C_ = PublicKey(bytes.fromhex(promise.C_), raw=True)
            C = b_dhke.step3_alice(
                C_, r, self.keysets[promise.id].public_keys[promise.amount]
            )

            if not settings.wallet_use_deprecated_h2c:
                B_, r = b_dhke.step1_alice(secret, r)  # recompute B_ for dleq proofs
            # BEGIN: BACKWARDS COMPATIBILITY < 0.15.1
            else:
                B_, r = b_dhke.step1_alice_deprecated(
                    secret, r
                )  # recompute B_ for dleq proofs
            # END: BACKWARDS COMPATIBILITY < 0.15.1

            proof = Proof(
                id=promise.id,
                amount=promise.amount,
                C=C.serialize().hex(),
                secret=secret,
                derivation_path=path,
            )

            # if the mint returned a dleq proof, we add it to the proof
            if promise.dleq:
                proof.dleq = DLEQWallet(
                    e=promise.dleq.e, s=promise.dleq.s, r=r.serialize()
                )

            proofs.append(proof)

            logger.trace(
                f"Created proof: {proof}, r: {r.serialize()} out of promise {promise}"
            )

        # DLEQ verify
        self.verify_proofs_dleq(proofs)

        logger.trace(f"Constructed {len(proofs)} proofs.")

        # add new proofs to wallet
        self.proofs += copy.copy(proofs)
        # store new proofs in database
        await self._store_proofs(proofs)

        return proofs

    def _construct_outputs(
        self,
        amounts: List[int],
        secrets: List[str],
        rs: List[PrivateKey] = [],
    ) -> Tuple[List[BlindedMessage], List[PrivateKey]]:
        """Takes a list of amounts and secrets and returns outputs.
        Outputs are blinded messages `outputs` and blinding factors `rs`

        Args:
            amounts (List[int]): list of amounts
            secrets (List[str]): list of secrets
            rs (List[PrivateKey], optional): list of blinding factors. If not given, `rs` are generated in step1_alice. Defaults to [].

        Returns:
            List[BlindedMessage]: list of blinded messages that can be sent to the mint
            List[PrivateKey]: list of blinding factors that can be used to construct proofs after receiving blind signatures from the mint

        Raises:
            AssertionError: if len(amounts) != len(secrets)
        """
        assert len(amounts) == len(
            secrets
        ), f"len(amounts)={len(amounts)} not equal to len(secrets)={len(secrets)}"
        outputs: List[BlindedMessage] = []

        rs_ = [None] * len(amounts) if not rs else rs
        rs_return: List[PrivateKey] = []
        for secret, amount, r in zip(secrets, amounts, rs_):
            if not settings.wallet_use_deprecated_h2c:
                B_, r = b_dhke.step1_alice(secret, r or None)
            # BEGIN: BACKWARDS COMPATIBILITY < 0.15.1
            else:
                B_, r = b_dhke.step1_alice_deprecated(secret, r or None)
            # END: BACKWARDS COMPATIBILITY < 0.15.1

            rs_return.append(r)
            output = BlindedMessage(
                amount=amount, B_=B_.serialize().hex(), id=self.keyset_id
            )
            outputs.append(output)
            logger.trace(f"Constructing output: {output}, r: {r.serialize()}")

        return outputs, rs_return

    async def construct_outputs(self, amounts: List[int]) -> List[BlindedMessage]:
        """Constructs outputs for a list of amounts.

        Args:
            amounts (List[int]): List of amounts to construct outputs for.

        Returns:
            List[BlindedMessage]: List of blinded messages that can be sent to the mint.
        """
        secrets, rs, _ = await self.generate_n_secrets(len(amounts))
        return self._construct_outputs(amounts, secrets, rs)[0]

    async def _store_proofs(self, proofs):
        try:
            async with self.db.connect() as conn:
                for proof in proofs:
                    await store_proof(proof, db=self.db, conn=conn)
        except Exception as e:
            logger.error(f"Could not store proofs in database: {e}")
            logger.error(proofs)
            raise e

    async def invalidate(
        self, proofs: List[Proof], check_spendable=False
    ) -> List[Proof]:
        """Invalidates all unspendable tokens supplied in proofs.

        Args:
            proofs (List[Proof]): Which proofs to delete
            check_spendable (bool, optional): Asks the mint to check whether proofs are already spent before deleting them. Defaults to False.

        Returns:
            List[Proof]: List of proofs that are still spendable.
        """
        invalidated_proofs: List[Proof] = []
        if check_spendable:
            proof_states = await self.check_proof_state(proofs)
            for i, state in enumerate(proof_states.states):
                if state.state == SpentState.spent:
                    invalidated_proofs.append(proofs[i])
        else:
            invalidated_proofs = proofs

        if invalidated_proofs:
            logger.trace(
                f"Invalidating {len(invalidated_proofs)} proofs worth"
                f" {self.unit.str(sum_proofs(invalidated_proofs))}."
            )

        async with self.db.connect() as conn:
            for p in invalidated_proofs:
                await invalidate_proof(p, db=self.db, conn=conn)

        invalidate_secrets = [p.secret for p in invalidated_proofs]
        self.proofs = list(
            filter(lambda p: p.secret not in invalidate_secrets, self.proofs)
        )
        return [p for p in proofs if p not in invalidated_proofs]

    # ---------- TRANSACTION HELPERS ----------

    async def select_to_send(
        self,
        proofs: List[Proof],
        amount: int,
        *,
        set_reserved: bool = False,
        offline: bool = False,
        include_fees: bool = True,
    ) -> Tuple[List[Proof], int]:
        """
        Selects proofs such that a desired `amount` can be sent. If the offline coin selection is unsuccessful,
        and `offline` is set to False (default), we split the available proofs with the mint to get the desired `amount`.

        If `set_reserved` is set to True, the proofs are marked as reserved so they aren't used in other transactions.

        If `include_fees` is set to False, the swap fees are not included in the amount to be selected.

        Args:
            proofs (List[Proof]): Proofs to split
            amount (int): Amount to split to
            set_reserved (bool, optional): If set, the proofs are marked as reserved.

        Returns:
            List[Proof]: Proofs to send
            int: Fees for the transaction
        """
        # select proofs that are not reserved and are in the active keysets of the mint
        proofs = self.active_proofs(proofs)
        if sum_proofs(proofs) < amount:
            raise Exception("balance too low.")

        # coin selection for potentially offline sending
        send_proofs = await self._select_proofs_to_send(
            proofs, amount, include_fees=include_fees
        )
        fees = self.get_fees_for_proofs(send_proofs)
        logger.trace(
            f"select_to_send: selected: {self.unit.str(sum_proofs(send_proofs))} (+ {self.unit.str(fees)} fees) – wanted: {self.unit.str(amount)}"
        )
        # offline coin selection unsuccessful, we need to swap proofs before we can send
        if not send_proofs or sum_proofs(send_proofs) > amount + fees:
            if not offline:
                logger.debug("Offline coin selection unsuccessful. Splitting proofs.")
                # we set the proofs as reserved later
                _, send_proofs = await self.split_to_send(
                    proofs, amount, set_reserved=False
                )
            else:
                raise Exception(
                    "Could not select proofs in offline mode. Available amounts:"
                    + amount_summary(proofs, self.unit)
                )
        if set_reserved:
            await self.set_reserved(send_proofs, reserved=True)
        return send_proofs, fees

    async def split_to_send(
        self,
        proofs: List[Proof],
        amount: int,
        *,
        secret_lock: Optional[Secret] = None,
        set_reserved: bool = False,
        include_fees: bool = True,
    ) -> Tuple[List[Proof], List[Proof]]:
        """
        Swaps a set of proofs with the mint to get a set that sums up to a desired amount that can be sent. The remaining
        proofs are returned to be kept. All newly created proofs will be stored in the database but if `set_reserved` is set
        to True, the proofs to be sent (which sum up to `amount`) will be marked as reserved so they aren't used in other
        transactions.

        Args:
            proofs (List[Proof]): Proofs to split
            amount (int): Amount to split to
            secret_lock (Optional[str], optional): If set, a custom secret is used to lock new outputs. Defaults to None.
            set_reserved (bool, optional): If set, the proofs are marked as reserved. Should be set to False if a payment attempt
            is made with the split that could fail (like a Lightning payment). Should be set to True if the token to be sent is
            displayed to the user to be then sent to someone else. Defaults to False.

        Returns:
            Tuple[List[Proof], List[Proof]]: Tuple of proofs to keep and proofs to send
        """
        # select proofs that are not reserved and are in the active keysets of the mint
        proofs = self.active_proofs(proofs)
        if sum_proofs(proofs) < amount:
            raise Exception("balance too low.")

        # coin selection for swapping
        # spendable_proofs, fees = await self._select_proofs_to_split(proofs, amount)
        swap_proofs = await self._select_proofs_to_send(
            proofs, amount, include_fees=True
        )
        # add proofs from inactive keysets to swap_proofs to get rid of them
        swap_proofs += [
            p
            for p in proofs
            if not self.keysets[p.id].active and not p.reserved and p not in swap_proofs
        ]

        fees = self.get_fees_for_proofs(swap_proofs)
        logger.debug(
            f"Amount to send: {self.unit.str(amount)} (+ {self.unit.str(fees)} fees)"
        )
        keep_proofs, send_proofs = await self.split(swap_proofs, amount, secret_lock)
        if set_reserved:
            await self.set_reserved(send_proofs, reserved=True)
        return keep_proofs, send_proofs

    # ---------- BALANCE CHECKS ----------

    @property
    def balance(self):
        return sum_proofs(self.proofs)

    @property
    def available_balance(self):
        return sum_proofs([p for p in self.proofs if not p.reserved])

    @property
    def proof_amounts(self):
        """Returns a sorted list of amounts of all proofs"""
        return [p.amount for p in sorted(self.proofs, key=lambda p: p.amount)]

    def active_proofs(self, proofs: List[Proof]):
        """Returns a list of proofs that
        - have an id that is in the current `self.keysets` which have the unit in `self.unit`
        - are not reserved
        """

        def is_active_proof(p: Proof) -> bool:
            return (
                p.id in self.keysets
                and self.keysets[p.id].unit == self.unit
                and not p.reserved
            )

        return [p for p in proofs if is_active_proof(p)]

    def balance_per_keyset(self) -> Dict[str, Dict[str, Union[int, str]]]:
        ret: Dict[str, Dict[str, Union[int, str]]] = {
            key: {
                "balance": sum_proofs(proofs),
                "available": sum_proofs([p for p in proofs if not p.reserved]),
            }
            for key, proofs in self._get_proofs_per_keyset(self.proofs).items()
        }
        for key in ret.keys():
            if key in self.keysets:
                ret[key]["unit"] = self.keysets[key].unit.name
        return ret

    def balance_per_unit(self) -> Dict[Unit, Dict[str, Union[int, str]]]:
        ret: Dict[Unit, Dict[str, Union[int, str]]] = {
            unit: {
                "balance": sum_proofs(proofs),
                "available": sum_proofs([p for p in proofs if not p.reserved]),
            }
            for unit, proofs in self._get_proofs_per_unit(self.proofs).items()
        }
        return ret

    async def balance_per_minturl(
        self, unit: Optional[Unit] = None
    ) -> Dict[str, Dict[str, Union[int, str]]]:
        balances = await self._get_proofs_per_minturl(self.proofs, unit=unit)
        balances_return: Dict[str, Dict[str, Union[int, str]]] = {
            key: {
                "balance": sum_proofs(proofs),
                "available": sum_proofs([p for p in proofs if not p.reserved]),
            }
            for key, proofs in balances.items()
        }
        for key in balances_return.keys():
            if unit:
                balances_return[key]["unit"] = unit.name
        return dict(sorted(balances_return.items(), key=lambda item: item[0]))  # type: ignore

    async def restore_tokens_for_keyset(
        self, keyset_id: str, to: int = 2, batch: int = 25
    ) -> None:
        """
        Restores tokens for a given keyset_id.

        Args:
            keyset_id (str): The keyset_id to restore tokens for.
            to (int, optional): The number of consecutive empty responses to stop restoring. Defaults to 2.
            batch (int, optional): The number of proofs to restore in one batch. Defaults to 25.
        """
        stop_counter = 0
        # we get the current secret counter and restore from there on
        spendable_proofs = []
        counter_before = await bump_secret_derivation(
            db=self.db, keyset_id=keyset_id, by=0
        )
        if counter_before != 0:
            print("Keyset has already been used. Restoring from it's last state.")
        i = counter_before
        n_last_restored_proofs = 0
        while stop_counter < to:
            print(f"Restoring token {i} to {i + batch}...")
            restored_proofs = await self.restore_promises_from_to(i, i + batch - 1)
            if len(restored_proofs) == 0:
                stop_counter += 1
            spendable_proofs = await self.invalidate(
                restored_proofs, check_spendable=True
            )
            if len(spendable_proofs):
                n_last_restored_proofs = len(spendable_proofs)
                print(f"Restored {sum_proofs(restored_proofs)} sat")
            i += batch

        # restore the secret counter to its previous value for the last round
        revert_counter_by = batch * to + n_last_restored_proofs
        logger.debug(f"Reverting secret counter by {revert_counter_by}")
        before = await bump_secret_derivation(
            db=self.db,
            keyset_id=keyset_id,
            by=-revert_counter_by,
        )
        logger.debug(
            f"Secret counter reverted from {before} to {before - revert_counter_by}"
        )
        if n_last_restored_proofs == 0:
            print("No tokens restored for keyset.")
            return

    async def restore_wallet_from_mnemonic(
        self, mnemonic: Optional[str], to: int = 2, batch: int = 25
    ) -> None:
        """
        Restores the wallet from a mnemonic.

        Args:
            mnemonic (Optional[str]): The mnemonic to restore the wallet from. If None, the mnemonic is loaded from the db.
            to (int, optional): The number of consecutive empty responses to stop restoring. Defaults to 2.
            batch (int, optional): The number of proofs to restore in one batch. Defaults to 25.
        """
        await self._init_private_key(mnemonic)
        await self.load_mint()
        print("Restoring tokens...")
        for keyset_id in self.keysets.keys():
            await self.restore_tokens_for_keyset(keyset_id, to, batch)

    async def restore_promises_from_to(
        self, from_counter: int, to_counter: int
    ) -> List[Proof]:
        """Restores promises from a given range of counters. This is for restoring a wallet from a mnemonic.

        Args:
            from_counter (int): Counter for the secret derivation to start from
            to_counter (int): Counter for the secret derivation to end at

        Returns:
            List[Proof]: List of restored proofs
        """
        # we regenerate the secrets and rs for the given range
        secrets, rs, derivation_paths = await self.generate_secrets_from_to(
            from_counter, to_counter
        )
        # we don't know the amount but luckily the mint will tell us so we use a dummy amount here
        amounts_dummy = [1] * len(secrets)
        # we generate outputs from deterministic secrets and rs
        regenerated_outputs, _ = self._construct_outputs(amounts_dummy, secrets, rs)
        # we ask the mint to reissue the promises
        proofs = await self.restore_promises(
            outputs=regenerated_outputs,
            secrets=secrets,
            rs=rs,
            derivation_paths=derivation_paths,
        )

        await set_secret_derivation(
            db=self.db, keyset_id=self.keyset_id, counter=to_counter + 1
        )
        return proofs

    async def restore_promises(
        self,
        outputs: List[BlindedMessage],
        secrets: List[str],
        rs: List[PrivateKey],
        derivation_paths: List[str],
    ) -> List[Proof]:
        """Restores proofs from a list of outputs, secrets, rs and derivation paths.

        Args:
            outputs (List[BlindedMessage]): Outputs for which we request promises
            secrets (List[str]): Secrets generated for the outputs
            rs (List[PrivateKey]): Random blinding factors generated for the outputs
            derivation_paths (List[str]): Derivation paths used for the secrets necessary to unblind the promises

        Returns:
            List[Proof]: List of restored proofs
        """
        # restored_outputs is there so we can match the promises to the secrets and rs
        restored_outputs, restored_promises = await super().restore_promises(outputs)
        # now we need to filter out the secrets and rs that had a match
        matching_indices = [
            idx
            for idx, val in enumerate(outputs)
            if val.B_ in [o.B_ for o in restored_outputs]
        ]
        secrets = [secrets[i] for i in matching_indices]
        rs = [rs[i] for i in matching_indices]
        # now we can construct the proofs with the secrets and rs
        proofs = await self._construct_proofs(
            restored_promises, secrets, rs, derivation_paths
        )
        logger.debug(f"Restored {len(restored_promises)} promises")
        return proofs
