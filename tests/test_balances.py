import sqlite3
import time

from emission_tracker.bot.balances import refresh_balances
from emission_tracker.config import PersonConfig
from emission_tracker.db import init_schema, sync_team
from emission_tracker.rate_limiter import TokenBucket
from emission_tracker.taostats_client import AccountInfo

CK_A = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA7"
CK_B = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA8"
HK_A = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"
HK_B = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2"


class _FakeTaoStats:
    def __init__(self):
        self.asked: list[str] = []
        self.subnets: list[int | None] = []

    def get_account(self, coldkey: str, subnet_id: int | None = None) -> AccountInfo:
        self.asked.append(coldkey)
        self.subnets.append(subnet_id)
        return AccountInfo(
            free_rao=1,
            staked_rao=2,
            total_rao=3,
            stake_alpha_rao=770_000_000_000,
            stake_alpha_as_tao_rao=12_400_000_000,
        )


class _FakeGradients:
    def __init__(self):
        self.asked: list[str] = []

    def get_tournament_balance(self, coldkey: str):
        self.asked.append(coldkey)
        return None  # 404 — no tournament account


def _seed(conn: sqlite3.Connection) -> None:
    init_schema(conn)
    sync_team(
        conn,
        [
            PersonConfig(
                name="Alice",
                hotkeys=[
                    {"hotkey": HK_A, "coldkey": CK_A},
                    {"hotkey": HK_B, "coldkey": CK_B},
                ],
            )
        ],
        subnet_id=56,
    )


def _run(conn, coldkeys=None):
    ts, gr = _FakeTaoStats(), _FakeGradients()
    result = refresh_balances(
        conn=conn,
        taostats=ts,
        gradients=gr,
        rate_limiter=TokenBucket(capacity=100, refill_per_second=100),
        request_interval_seconds=0,
        subnet_id=56,
        coldkeys=coldkeys,
    )
    return result, ts, gr


def test_default_run_covers_every_known_coldkey(memory_db):
    _seed(memory_db)
    result, ts, gr = _run(memory_db)
    assert sorted(ts.asked) == sorted([CK_A, CK_B])
    assert sorted(gr.asked) == sorted([CK_A, CK_B])
    assert result.coldkey_count == 2


def test_narrowing_to_one_coldkey_leaves_the_others_untouched(memory_db):
    """The per-card button must cost two API calls, not thirty."""
    _seed(memory_db)
    result, ts, gr = _run(memory_db, coldkeys=[CK_B])

    assert ts.asked == [CK_B]
    assert gr.asked == [CK_B]
    assert result.coldkey_count == 1

    rows = memory_db.execute(
        "SELECT DISTINCT coldkey_ss58 FROM coldkey_balances"
    ).fetchall()
    assert [r["coldkey_ss58"] for r in rows] == [CK_B]


def test_unknown_coldkeys_are_dropped_rather_than_fetched(memory_db):
    """A caller must not be able to point the operator's API key at an
    arbitrary account by passing an address the roster never mentioned."""
    _seed(memory_db)
    stranger = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA9"
    result, ts, gr = _run(memory_db, coldkeys=[CK_A, stranger])

    assert ts.asked == [CK_A]
    assert stranger not in gr.asked
    assert result.coldkey_count == 1


def test_empty_selection_fetches_nothing(memory_db):
    _seed(memory_db)
    result, ts, gr = _run(memory_db, coldkeys=[])
    assert ts.asked == [] and gr.asked == []
    assert result.coldkey_count == 0


def test_runner_never_widens_an_empty_selection_into_a_full_sweep(memory_db, tmp_path):
    """`start([])` means "nothing", not "everything". Truthiness would turn
    an empty list into a full 15-coldkey run against the live API."""
    import sqlite3 as _sqlite3

    from emission_tracker.bot.balances import BalanceRunner

    db = tmp_path / "t.db"
    conn = _sqlite3.connect(db)
    conn.row_factory = _sqlite3.Row
    _seed(conn)
    conn.commit()
    conn.close()

    def conn_factory():
        c = _sqlite3.connect(db)
        c.row_factory = _sqlite3.Row
        return c

    ts, gr = _FakeTaoStats(), _FakeGradients()
    runner = BalanceRunner(
        conn_factory=conn_factory,
        taostats=ts,
        gradients=gr,
        rate_limiter=TokenBucket(capacity=100, refill_per_second=100),
        request_interval_seconds=0,
        subnet_id=56,
    )
    assert runner.start([]) is True
    for _ in range(100):
        if not runner.is_running:
            break
        time.sleep(0.02)
    assert not runner.is_running, "refresh thread did not finish"
    assert ts.asked == [], f"empty selection fetched {ts.asked}"


def test_subnet_stake_is_stored_and_scoped_to_the_tracked_subnet(memory_db):
    """The card must show stake on our subnet, not balance_staked — several
    coldkeys hold alpha on other subnets, which would inflate the figure."""
    _seed(memory_db)
    _, ts, _ = _run(memory_db, coldkeys=[CK_A])

    assert ts.subnets == [56], "subnet_id not passed through to the client"

    row = memory_db.execute(
        "SELECT stake_alpha_rao, stake_alpha_as_tao_rao, balance_staked_rao "
        "FROM coldkey_balances WHERE coldkey_ss58 = ?",
        (CK_A,),
    ).fetchone()
    assert row["stake_alpha_rao"] == 770_000_000_000
    assert row["stake_alpha_as_tao_rao"] == 12_400_000_000
    # Kept separately from the all-subnet total, not conflated with it.
    assert row["balance_staked_rao"] == 2
