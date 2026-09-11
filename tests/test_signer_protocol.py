import pytest

from emission_tracker.signer.protocol import (
    OP_PAY,
    OP_UNSTAKE,
    ProtocolError,
    SignRequest,
    SignResult,
)

CK = "5FnhiibtJkvCDSnfrp1iUQiZZaYJv31h114Pv7wtoztt9FP9"


def test_request_round_trips():
    req = SignRequest(op=OP_PAY, coldkey=CK, types=("text", "env"))
    assert SignRequest.from_line(req.to_line()) == req


def test_request_line_is_newline_terminated():
    """The socket framing is one JSON object per line."""
    line = SignRequest(op=OP_UNSTAKE, coldkey=CK).to_line()
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1


def test_unknown_op_is_refused():
    with pytest.raises(ProtocolError):
        SignRequest.from_line(b'{"op": "drain_everything", "coldkey": "5F"}\n')


def test_unknown_tournament_type_is_refused():
    """Types index a fee table; an unrecognised one must not reach it."""
    with pytest.raises(ProtocolError):
        SignRequest.from_line(
            b'{"op": "pay_tournament", "coldkey": "5F", "types": ["gold"]}\n'
        )


def test_duplicate_types_are_refused():
    """Otherwise ["text","text"] silently doubles the fee."""
    with pytest.raises(ProtocolError):
        SignRequest.from_line(
            b'{"op": "pay_tournament", "coldkey": "5F", "types": ["text", "text"]}\n'
        )


def test_payment_with_no_types_is_refused():
    with pytest.raises(ProtocolError):
        SignRequest.from_line(b'{"op": "pay_tournament", "coldkey": "5F"}\n')


def test_amount_is_never_accepted_from_the_wire():
    """The signer computes amounts. A caller-supplied one must be ignored,
    not honoured, or the security boundary is decorative."""
    req = SignRequest.from_line(
        b'{"op": "pay_tournament", "coldkey": "5F", "types": ["text"],'
        b' "amount_tao": 999}\n'
    )
    assert not hasattr(req, "amount_tao")


def test_result_round_trips():
    res = SignResult(ok=True, op=OP_PAY, coldkey=CK, amount_rao=700_000_000,
                     tx_hash="0xabc")
    assert SignResult.from_line(res.to_line()) == res


def test_malformed_json_raises_protocol_error():
    with pytest.raises(ProtocolError):
        SignRequest.from_line(b"not json\n")
