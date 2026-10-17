"""ResultCsvCache（結果 CSV の台ごとのキャッシュ）の単体テスト（U7）。

NFR2.22（20 件・合計 256MB・1 件 32MB・300 秒）、performance-design.md 2（最も古く
使われたものから捨てる、1 つのロック）、BR2.5（鍵はアンケートの ID、削除で外す）。
時計は注入し、time.sleep には頼らない。
"""

import threading

import pytest

from src.managers.shared.result_csv_cache import (
    MAX_ENTRIES,
    MAX_ENTRY_BYTES,
    MAX_TOTAL_BYTES,
    TTL_SECONDS,
    ResultCsvCache,
    reset_result_cache,
    shared_result_cache,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def test_limits_are_the_designed_ones():
    assert MAX_ENTRIES == 20
    assert MAX_TOTAL_BYTES == 256 * 1024 * 1024
    assert MAX_ENTRY_BYTES == 32 * 1024 * 1024
    assert TTL_SECONDS == 300


def test_put_then_get_and_evict(clock):
    cache = ResultCsvCache(clock=clock)
    cache.put("s1", b"csv")
    assert cache.get("s1") == b"csv"
    cache.evict("s1")
    assert cache.get("s1") is None
    cache.evict("never-there")


def test_entries_expire_after_300_seconds(clock):
    cache = ResultCsvCache(clock=clock)
    cache.put("s1", b"csv")
    clock.now += TTL_SECONDS - 1
    assert cache.get("s1") == b"csv"
    clock.now += 1
    assert cache.get("s1") is None
    assert len(cache) == 0


def test_twenty_first_entry_drops_the_least_recently_used(clock):
    cache = ResultCsvCache(clock=clock)
    for i in range(MAX_ENTRIES):
        cache.put(f"s{i}", b"x")
    assert cache.get("s0") == b"x"  # s0 is now the most recently used
    cache.put("s-new", b"x")
    assert len(cache) == MAX_ENTRIES
    assert cache.get("s1") is None
    assert cache.get("s0") == b"x"
    assert cache.get("s-new") == b"x"


def test_total_size_is_bounded(clock):
    cache = ResultCsvCache(clock=clock, max_total_bytes=10, max_entry_bytes=6)
    cache.put("a", b"12345")
    cache.put("b", b"12345")
    cache.put("c", b"1234")
    assert cache.total_bytes <= 10
    assert cache.get("a") is None
    assert cache.get("b") == b"12345"
    assert cache.get("c") == b"1234"


def test_an_entry_over_the_single_limit_is_not_kept(clock):
    cache = ResultCsvCache(clock=clock, max_entry_bytes=4)
    cache.put("big", b"12345")
    assert cache.get("big") is None
    assert cache.total_bytes == 0


def test_putting_the_same_key_replaces_it(clock):
    cache = ResultCsvCache(clock=clock)
    cache.put("s1", b"old")
    cache.put("s1", b"newer")
    assert cache.get("s1") == b"newer"
    assert len(cache) == 1
    assert cache.total_bytes == len(b"newer")


def test_many_threads_never_exceed_the_limits(clock):
    cache = ResultCsvCache(
        clock=clock, max_entries=5, max_total_bytes=50, max_entry_bytes=20
    )
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(300):
                key = f"s{(n * 7 + i) % 13}"
                cache.put(key, b"x" * ((i % 15) + 1))
                cache.get(key)
                if i % 5 == 0:
                    cache.evict(f"s{i % 13}")
                assert len(cache) <= 5
                assert cache.total_bytes <= 50
        except BaseException as e:  # noqa: BLE001 - reported below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(cache) <= 5
    assert cache.total_bytes <= 50


def test_analysis_and_delete_share_the_one_instance():
    """Review: 削除の Manager からも消せるよう、2 つの Manager が同じ実体を使う。"""
    from unittest.mock import Mock

    from src.managers.survey_analysis_manager import SurveyAnalysisManager
    from src.managers.survey_execution_manager import SurveyExecutionManager

    reset_result_cache()
    try:
        analysis = SurveyAnalysisManager(
            database_service=Mock(),
            s3_service=Mock(),
            ai_service=Mock(),
            survey_batch_service=Mock(),
        )
        execution = SurveyExecutionManager(
            database_service=Mock(), survey_batch_service=Mock(), s3_service=Mock()
        )
        assert analysis.result_cache is shared_result_cache()
        assert execution.result_cache is shared_result_cache()
    finally:
        reset_result_cache()


def test_one_instance_per_process():
    reset_result_cache()
    try:
        first = shared_result_cache()
        assert shared_result_cache() is first
        reset_result_cache()
        assert shared_result_cache() is not first
    finally:
        reset_result_cache()
