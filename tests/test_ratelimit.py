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
