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


def test_backoff_for_429_also_starts_at_two_seconds():
    """429 的起跳值要和 503 一样是 2 秒。

    只断言 `429 in RETRYABLE_STATUS` 抓不住"重试它，但按 0.5 秒起跳"这种
    实现——那等于没把 429 当成降速信号。必须断言它的**基值**。
    """
    assert nsdav.backoff_delay(1, 429, rand=_no_jitter) == 2.0


def test_backoff_is_capped():
    assert nsdav.backoff_delay(20, 503, rand=_no_jitter) == 60.0


def test_backoff_never_exceeds_the_cap():
    """封顶是**实际等待时间**的上界，抖动加完也不能越过。

    先封顶再加抖动的话，rand() 取 1.0 时实际会等到 60*1.25 = 75 秒，而规格
    写的是封顶 60 秒——用户读到的是一个代码不兑现的承诺。
    """
    assert nsdav.backoff_delay(20, 503, rand=lambda: 1.0) == 60.0
    assert nsdav.backoff_delay(20, None, rand=lambda: 1.0) == 60.0


def test_jitter_stays_within_bounds():
    lo = nsdav.backoff_delay(3, 503, rand=lambda: 0.0)
    hi = nsdav.backoff_delay(3, 503, rand=lambda: 1.0)
    assert lo == 8.0 * 0.75
    assert hi == 8.0 * 1.25
