import pytest


@pytest.mark.benchmark
def test_sum() -> None:
    assert sum(range(1_000)) == 499_500


@pytest.mark.benchmark
@pytest.mark.slow
def test_sum_slow() -> None:
    assert sum(range(100_000)) == 4_999_950_000


def test_not_a_benchmark() -> None:
    assert True
