import inspect
import time

import pytest

import nsdav


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_first_call_does_not_sleep():
    c = FakeClock()
    rl = nsdav.RateLimiter(0.2, clock=c.now, sleep=c.sleep)
    rl.wait()
    assert c.slept == []


def test_second_call_sleeps_remaining_gap():
    """睡的是"还差的那部分"：已经过去 0.05 秒，就只补 0.15 秒。

    这里必须用 approx。假时钟走的是 `1000.0 + 0.05`，而浮点上
    `1000.05 - 1000.0` 不等于字面量 `0.05`（差约 4.5e-14），于是实现算出的
    "还差多少"和断言里手写的 `0.2 - 0.05` 也不是同一个数。拿被减出来的量做
    精确相等比较，工具就用错了——第一版就是这么写的，按计划自己的实现跑
    也过不去。
    """
    c = FakeClock()
    rl = nsdav.RateLimiter(0.2, clock=c.now, sleep=c.sleep)
    rl.wait()
    c.t += 0.05
    rl.wait()
    assert c.slept == pytest.approx([0.2 - 0.05])


def test_negative_gap_is_clamped_to_zero():
    """负的间隔按 0 处理，`min_gap` 这个公开属性也不该是负数。"""
    assert nsdav.RateLimiter(-1.0).min_gap == 0.0


def test_consecutive_waits_are_never_closer_than_min_gap():
    """核心不变量：相邻两次 wait() **返回时的钟值**至少差 min_gap。

    前面几条最多只走两次 wait()，而且没有一条在"上次没睡"之后再调一次，所以
    这条路径原来无人看管。把 `self._last = self._clock()` 顺手写进
    `if delta < self.min_gap:` 分支里——最自然的滑法——前面几条全绿，间隔却
    真的破了：返回时刻会是 [1000.0, 1000.2, 1000.7, 1000.71]，最后两跳只隔
    0.01 秒，而 min_gap 是 0.2。这个限流器的全部意义就是这个间隔。
    """
    c = FakeClock()
    rl = nsdav.RateLimiter(0.2, clock=c.now, sleep=c.sleep)
    stamps = []
    for step in (0.0, 0.05, 0.5, 0.01, 0.19):
        c.t += step
        rl.wait()
        stamps.append(c.t)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(gaps) == 4
    assert min(gaps) >= 0.2 - 1e-9


def test_defaults_are_the_production_clock_and_sleep():
    """默认值必须是真在生产里用的那对，因为假时钟的用例永远走不到它们。

    把 `time.monotonic` 换成 `time.time` 之类同样全绿，而生产上钟被回拨就会
    算错间隔。默认值不钉，这一层就没人看。
    """
    params = inspect.signature(nsdav.RateLimiter.__init__).parameters
    assert params["clock"].default is time.monotonic
    assert params["sleep"].default is time.sleep


def test_no_sleep_when_gap_already_elapsed():
    c = FakeClock()
    rl = nsdav.RateLimiter(0.2, clock=c.now, sleep=c.sleep)
    rl.wait()
    c.t += 5.0
    rl.wait()
    assert c.slept == []


def test_zero_gap_disables_throttling():
    c = FakeClock()
    rl = nsdav.RateLimiter(0.0, clock=c.now, sleep=c.sleep)
    rl.wait()
    rl.wait()
    assert c.slept == []
