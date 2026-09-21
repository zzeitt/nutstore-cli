import nsdav


def test_retryable_status_set():
    assert 503 in nsdav.RETRYABLE_STATUS
    assert 429 in nsdav.RETRYABLE_STATUS
    assert 500 in nsdav.RETRYABLE_STATUS
    # 这几个绝不能重试
    assert 401 not in nsdav.RETRYABLE_STATUS
    assert 403 not in nsdav.RETRYABLE_STATUS
    assert 404 not in nsdav.RETRYABLE_STATUS
    assert 410 not in nsdav.RETRYABLE_STATUS


def _no_jitter():
    return 0.5


def test_backoff_grows_exponentially_for_503():
    d = [nsdav.backoff_delay(i, 503, rand=_no_jitter) for i in range(1, 6)]
    assert d == [2.0, 4.0, 8.0, 16.0, 32.0]


def test_backoff_for_connection_error_starts_lower():
    assert nsdav.backoff_delay(1, None, rand=_no_jitter) == 0.5


def test_backoff_is_capped():
    assert nsdav.backoff_delay(20, 503, rand=_no_jitter) == 60.0


def test_jitter_stays_within_bounds():
    lo = nsdav.backoff_delay(3, 503, rand=lambda: 0.0)
    hi = nsdav.backoff_delay(3, 503, rand=lambda: 1.0)
    assert lo == 8.0 * 0.75
    assert hi == 8.0 * 1.25


def test_backoff_for_429_also_starts_at_two_seconds():
    # brief 正文写明 429 与 503 同为硬档（起跳 2 秒），但上面 5 条里只有
    # 503 钉了起跳值 —— 把 429 归到软档（0.5s）的实现在那 5 条下全绿。
    # 这条补上另一半：429 与 503 共用同一个 base。
    assert nsdav.backoff_delay(1, 429, rand=_no_jitter) == 2.0
    assert nsdav.backoff_delay(2, 429, rand=_no_jitter) == 4.0
