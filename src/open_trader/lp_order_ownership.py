"""Exact LP order provenance. Identity records never grant replay authority."""
import re
from dataclasses import dataclass, field
from decimal import Decimal

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak
from polymarket.models.clob.orders import SignedOrder

LOCAL_INSTANCE_SCHEMA = 'open_trader.lp.local_instance.v1'
LOCAL_INSTANCE_OWNERSHIP_POLICY = 'local_only_full_wallet_v1'


# Frozen CTF Exchange V2 protocol, not a signature or struct-hash identity.
ORDER_TYPE = ('Order(uint256 salt,address maker,address signer,uint256 tokenId,'
              'uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,'
              'uint256 timestamp,bytes32 metadata,bytes32 builder)')
ORDER_FIELDS = [dict(name=name, type=kind) for name, kind in (
    ('salt', 'uint256'), ('maker', 'address'), ('signer', 'address'),
    ('tokenId', 'uint256'), ('makerAmount', 'uint256'), ('takerAmount', 'uint256'),
    ('side', 'uint8'), ('signatureType', 'uint8'), ('timestamp', 'uint256'),
    ('metadata', 'bytes32'), ('builder', 'bytes32'))]
DOMAIN_FIELDS = [dict(name=name, type=kind) for name, kind in (
    ('name', 'string'), ('version', 'string'), ('chainId', 'uint256'),
    ('verifyingContract', 'address'))]


def _typed_order(fields, chain_id, exchange):
    if (set(fields) != {field['name'] for field in ORDER_FIELDS}
            or type(chain_id) is not int or chain_id not in (137, 80002)
            or fields['side'] not in (0, 1) or fields['signatureType'] not in (0, 1, 2, 3)):
        raise ValueError('lp_order_identity_fields_invalid')
    return dict(types=dict(EIP712Domain=DOMAIN_FIELDS, Order=ORDER_FIELDS),
        primaryType='Order', domain=dict(name='Polymarket CTF Exchange',
            version='2', chainId=chain_id, verifyingContract=exchange), message=dict(fields))


def canonical_order_id(fields, *, chain_id, exchange):
    """Return the complete EIP712 hash, independently pinned by protocol vectors."""
    encoded = encode_typed_data(full_message=_typed_order(fields, chain_id, exchange))
    return '0x' + keccak(b'\x19' + encoded.version + encoded.header + encoded.body).hex()


@dataclass(frozen=True)
class PreparedLPOrder:
    order_id: str
    account_id: str
    token_id: str
    side: str
    price: Decimal
    quantity: Decimal
    chain_id: int
    exchange: str
    signed_order: SignedOrder = field(repr=False)

    def provenance(self, *, instance_id, request_id, condition_id, venue='polymarket'):
        signed = self.signed_order
        fields = _signed_fields(signed)
        return dict(account_id=self.account_id, venue=venue, order_id=self.order_id,
            instance_id=validate_instance_id(instance_id), request_id=request_id,
            condition_id=condition_id, token_id=self.token_id, side=self.side,
            price=str(self.price), quantity=str(self.quantity), chain_id=self.chain_id,
            exchange=self.exchange, protocol_fields=fields, order_type=signed.order_type,
            expiration=signed.expiration, post_only=signed.post_only)


def _signed_fields(signed):
    return dict(salt=signed.salt, maker=signed.maker, signer=signed.signer,
        tokenId=int(signed.token_id, 16) if signed.token_id.startswith('0x') else int(signed.token_id),
        makerAmount=signed.maker_amount, takerAmount=signed.taker_amount,
        side=0 if signed.side == 'BUY' else 1, signatureType=signed.signature_type,
        timestamp=signed.timestamp, metadata=signed.metadata, builder=signed.builder)


def validate_provenance(row):
    required = {'account_id','venue','order_id','instance_id','request_id','condition_id',
        'token_id','side','price','quantity','chain_id','exchange','protocol_fields',
        'order_type','expiration','post_only'}
    if type(row) is not dict or set(row) != required:
        raise ValueError('lp_provenance_identity_invalid')
    validate_instance_id(row['instance_id'])
    if any(type(row[k]) is not str or not row[k] for k in (
            'account_id','order_id','request_id','condition_id','token_id','price','quantity')):
        raise ValueError('lp_provenance_identity_invalid')
    fields = row['protocol_fields']
    try:
        side = row['side']
        shares = Decimal(fields['takerAmount'] if side == 'BUY' else fields['makerAmount']) / 1000000
        amount = Decimal(fields['makerAmount'] if side == 'BUY' else fields['takerAmount']) / 1000000
        if (row['venue'] != 'polymarket' or side not in ('BUY','SELL')
                or fields['side'] != (0 if side == 'BUY' else 1)
                or fields['maker'].casefold() != row['account_id'].casefold()
                or fields['tokenId'] != (int(row['token_id'],16) if row['token_id'].startswith('0x') else int(row['token_id']))
                or shares <= 0 or Decimal(row['quantity']) != shares or Decimal(row['price']) * shares != amount
                or canonical_order_id(fields, chain_id=row['chain_id'], exchange=row['exchange']) != row['order_id']
                or row['order_type'] not in ('GTC','GTD','FOK')
                or type(row['expiration']) is not int or row['expiration'] < 0
                or type(row['post_only']) is not bool):
            raise ValueError
    except Exception:
        raise ValueError('lp_provenance_identity_invalid') from None
    return row


def verify_signed_order(signed, *, signer, wallet, environment, token_id, side,
                        quantity, price, protected=False):
    """Recover one actual signer/domain before deriving an LP order identity.

    The SDK drops its domain from SignedOrder. Accept only one verified domain
    from the actual environment. POLY_1271 includes an independently checked
    domain/contents/type trailer and its inner EOA signature.
    """
    try:
        if not isinstance(signed, SignedOrder):
            raise ValueError
        if (signed.maker.casefold() != wallet.casefold() or signed.token_id != token_id
                or signed.side != side or signed.signature_type not in (0, 1, 2, 3)
                or side not in ('BUY', 'SELL') or quantity <= 0 or price <= 0):
            raise ValueError
        expected_signer = wallet if signed.signature_type == 3 else signer
        if signed.signer.casefold() != expected_signer.casefold():
            raise ValueError
        shares = Decimal(signed.taker_amount if side == 'BUY' else signed.maker_amount) / 1000000
        cost = Decimal(signed.maker_amount if side == 'BUY' else signed.taker_amount) / 1000000
        if shares != quantity or (cost < quantity * price if protected else cost != quantity * price):
            raise ValueError
        if ((protected and (signed.order_type != 'FOK' or signed.post_only))
                or (not protected and (signed.order_type not in ('GTC', 'GTD') or not signed.post_only))):
            raise ValueError
        fields = _signed_fields(signed)
        signature = bytes.fromhex(signed.signature.removeprefix('0x'))
        matches = []
        for exchange in (environment.standard_exchange, environment.neg_risk_exchange):
            typed = _typed_order(fields, environment.chain_id, exchange)
            encoded = encode_typed_data(full_message=typed)
            inner_signature = signature
            if signed.signature_type == 3:
                trailer = encoded.header + encoded.body + ORDER_TYPE.encode() + len(ORDER_TYPE).to_bytes(2, 'big')
                if len(signature) != 65 + len(trailer) or signature[65:] != trailer:
                    continue
                inner_signature = signature[:65]
                typed['types'] = dict(typed['types'], TypedDataSign=[dict(name=n, type=t) for n,t in (
                    ('contents','Order'), ('name','string'), ('version','string'),
                    ('chainId','uint256'), ('verifyingContract','address'), ('salt','bytes32'))])
                typed['primaryType'] = 'TypedDataSign'
                typed['message'] = dict(contents=fields, name='DepositWallet', version='1',
                    chainId=environment.chain_id, verifyingContract=signed.signer, salt='0x'+'00'*32)
            if len(inner_signature) != 65:
                continue
            recovered = Account.recover_message(encode_typed_data(full_message=typed), signature=inner_signature)
            if recovered.casefold() == signer.casefold():
                matches.append((exchange, '0x'+keccak(b'\x19'+encoded.version+encoded.header+encoded.body).hex()))
        if len(matches) != 1:
            raise ValueError
        exchange, order_id = matches[0]
        return PreparedLPOrder(order_id, wallet.casefold(), token_id, side,
            cost/shares, shares, environment.chain_id, exchange, signed)
    except Exception:
        # No payload, key, signature or external response belongs in diagnostics.
        raise ValueError('lp_prepared_order_identity_invalid') from None


def validate_instance_id(value):
    if (type(value) is not str
            or re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', value) is None):
        raise ValueError('lp_instance_id must be a stable lowercase instance name')
    return value
