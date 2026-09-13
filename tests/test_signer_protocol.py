import json

import pytest

from emission_tracker.signer.protocol import (
    OP_PAY,
    OP_UNSTAKE,
    ProtocolError,
    SignRequest,
    SignResult,
)

CK = "5FnhiibtJkvCDSnfrp1iUQiZZaYJv31h114Pv7wtoztt9FP9"

# Obvious dummy. The wallet unlock value travels with each request now, so
# a request without one is malformed.
UNLOCK = "dummy-unlock-value"


def test_request_round_trips():
    req = SignRequest(op=OP_PAY, coldkey=CK, types=("text", "env"), secret=UNLOCK)
    assert SignRequest.from_line(req.to_line()) == req


def test_a_request_without_an_unlock_value_is_refused():
    with pytest.raises(ProtocolError):
        SignRequest.from_line(
            b'{"op": "unstake_all", "coldkey": "5F"}\n'
        )


def test_an_empty_unlock_value_is_refused():
    """Empty must not read as "no unlock needed" — btcli would then prompt
    and --no-prompt would turn that into a confusing non-zero exit."""
    with pytest.raises(ProtocolError):
        SignRequest.from_line(
            b'{"op": "unstake_all", "coldkey": "5F", "secret": ""}\n'
        )


def test_the_unlock_value_is_not_printed_by_repr():
    """These objects get logged and appear in tracebacks."""
    req = SignRequest(op=OP_UNSTAKE, coldkey=CK, secret=UNLOCK)
    assert UNLOCK not in repr(req)


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
        b' "secret": "dummy-unlock-value", "amount_tao": 999}\n'
    )
    assert not hasattr(req, "amount_tao")


def test_result_round_trips():
    res = SignResult(ok=True, op=OP_PAY, coldkey=CK, amount_rao=700_000_000,
                     tx_hash="0xabc")
    assert SignResult.from_line(res.to_line()) == res


def test_result_from_non_dict_json_raises_protocol_error():
    """Valid JSON that is not an object must still be a ProtocolError, or it
    escapes the socket client, which only catches OSError and ProtocolError."""
    with pytest.raises(ProtocolError):
        SignResult.from_line(b"[1, 2]\n")


def test_malformed_json_raises_protocol_error():
    with pytest.raises(ProtocolError):
        SignRequest.from_line(b"not json\n")


def test_a_whitespace_only_unlock_value_is_refused():
    """The signer has to be defensible on its own, not rely on the web tier
    having stripped first: whitespace reaches btcli as an unset variable."""
    with pytest.raises(ProtocolError):
        SignRequest.from_line(
            b'{"op": "unstake_all", "coldkey": "5F", "secret": "   "}\n'
        )


class TestTreasuryRequests:
    """Sweep and distribute are the first operations where the caller
    names a destination and an amount. Everything the caller can name is
    something an attacker who reaches the web tier can name, so each
    field is checked here before it can reach a wallet."""

    DEST = "5HERhLCKSpmTiRD6EpnsY7DUnVqUThaANhYgXYAWqZZ28fLB"

    def test_a_sweep_names_only_the_wallet_to_empty(self):
        req = SignRequest.from_line(
            json.dumps({"op": "sweep", "coldkey": CK, "secret": "s"}).encode()
        )
        assert req.op == "sweep"
        assert req.amount_rao == 0
        assert req.destination == ""

    def test_a_sweep_may_not_carry_an_amount(self):
        """How much to sweep is read from the chain by the signer. A
        caller-supplied amount would be a stale figure at best and a
        chosen one at worst."""
        line = json.dumps(
            {"op": "sweep", "coldkey": CK, "amount_rao": 1, "secret": "s"}
        ).encode()
        with pytest.raises(ProtocolError, match="amount"):
            SignRequest.from_line(line)

    def test_a_sweep_may_not_carry_a_destination(self):
        """It has exactly one: the treasury wallet in the signer's own
        config."""
        line = json.dumps(
            {"op": "sweep", "coldkey": CK, "destination": "5Evil", "secret": "s"}
        ).encode()
        with pytest.raises(ProtocolError, match="destination"):
            SignRequest.from_line(line)

    def test_a_distribution_carries_a_destination_and_an_amount(self):
        req = SignRequest.from_line(
            json.dumps({
                "op": "distribute", "coldkey": CK, "destination": self.DEST,
                "amount_rao": 2_500_000_000, "secret": "s",
            }).encode()
        )
        assert req.destination == self.DEST
        assert req.amount_rao == 2_500_000_000

    @pytest.mark.parametrize("amount", [0, -1, "1.5", None, 1.5])
    def test_a_distribution_without_a_positive_whole_amount_is_refused(
        self, amount
    ):
        line = json.dumps({
            "op": "distribute", "coldkey": CK, "destination": self.DEST,
            "amount_rao": amount, "secret": "s",
        }).encode()
        with pytest.raises(ProtocolError):
            SignRequest.from_line(line)

    def test_a_distribution_without_a_destination_is_refused(self):
        line = json.dumps({
            "op": "distribute", "coldkey": CK, "amount_rao": 1, "secret": "s",
        }).encode()
        with pytest.raises(ProtocolError, match="destination"):
            SignRequest.from_line(line)

    def test_the_amount_and_destination_survive_a_round_trip(self):
        sent = SignRequest(
            "distribute", CK, destination=self.DEST, amount_rao=7, secret="s"
        )
        assert SignRequest.from_line(sent.to_line()) == sent

    def test_the_secret_is_still_not_printable(self):
        sent = SignRequest(
            "distribute", CK, destination=self.DEST, amount_rao=7,
            secret="sangat-rahasia",
        )
        assert "sangat-rahasia" not in repr(sent)


class TestTheBalanceRead:
    """Filling the treasury dialogs needs fresh figures, and only the
    signer can reach the wallets. That read must stay a different kind
    of thing from signing: no wallet, no amount, no unlock value."""

    def test_it_names_no_wallet_and_carries_no_secret(self):
        req = SignRequest.from_line(json.dumps({"op": "balances"}).encode())
        assert req.coldkey == ""
        assert req.secret == ""

    def test_a_secret_sent_with_it_is_refused(self):
        """Nothing on this path can use one, so a request carrying one
        is either a mistake or someone probing for a path that leaks it."""
        line = json.dumps({"op": "balances", "secret": "s"}).encode()
        with pytest.raises(ProtocolError, match="secret"):
            SignRequest.from_line(line)

    def test_the_result_carries_the_figures(self):
        sent = SignResult(True, "balances", "", balances={CK: 1_000_000_000})
        assert SignResult.from_line(sent.to_line()).balances == {CK: 1_000_000_000}

    def test_an_unreadable_wallet_stays_unreadable_across_the_wire(self):
        """None means "we could not read it" and 0 means "empty". JSON
        null must not come back as a zero."""
        sent = SignResult(True, "balances", "", balances={CK: None})
        assert SignResult.from_line(sent.to_line()).balances == {CK: None}
