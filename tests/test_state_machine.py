import pytest

from app.constants import OrderStatus as S
from app.services.orders import ALLOWED, can_transition

HAPPY = [S.PENDING, S.OFFERED, S.ACCEPTED, S.DRIVER_ON_THE_WAY, S.ARRIVED_AT_PICKUP, S.IN_PROGRESS,
         S.ARRIVED_AT_DESTINATION, S.COMPLETED]


def test_happy_path():
    for a, b in zip(HAPPY, HAPPY[1:]):
        assert can_transition(a, b)


@pytest.mark.parametrize("a,b", [
    (S.ACCEPTED, S.IN_PROGRESS), (S.DRIVER_ON_THE_WAY, S.IN_PROGRESS), (S.ARRIVED_AT_PICKUP, S.ARRIVED_AT_DESTINATION),
    (S.ACCEPTED, S.COMPLETED), (S.IN_PROGRESS, S.COMPLETED), (S.COMPLETED, S.CANCELLED), (S.CANCELLED, S.OFFERED),
])
def test_forbidden(a, b):
    assert not can_transition(a, b)


def test_final_states_are_terminal():
    for s in S.FINAL:
        assert ALLOWED[s] == set()
