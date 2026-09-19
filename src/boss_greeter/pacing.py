"""限速与熔断。让节奏像人，并在撞上风控时干净退出。"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from .config import PacingConfig


class Circuit(Exception):
    """熔断信号：调用方捕获后应立刻结束整轮，不再继续下一个岗位。"""


@dataclass
class Pacer:
    """随机延迟 + 分批长休息 + 日上限 + 连续失败熔断。"""

    cfg: PacingConfig
    sent_today: int = 0
    _since_rest: int = 0
    _consecutive_failures: int = 0
    _next_rest_at: int = field(init=False)

    def __post_init__(self) -> None:
        self._next_rest_at = random.randint(*self.cfg.batch_size)

    # ------------------------------------------------------------ 延迟

    def pause(self, scale: float = 1.0) -> None:
        """单步操作之间的停顿。用高斯分布而非均匀分布，更贴近真人节奏。"""
        lo, hi = self.cfg.action_delay
        mu, sigma = (lo + hi) / 2, (hi - lo) / 4
        delay = max(lo, min(hi, random.gauss(mu, sigma))) * scale
        time.sleep(delay)

    def after_send(self) -> None:
        """成功发出一条后调用：计数、拉开与下一条的间隔、必要时长休息。"""
        self.sent_today += 1
        self._since_rest += 1
        self._consecutive_failures = 0
        if self._since_rest >= self._next_rest_at:
            rest = random.uniform(*self.cfg.batch_rest)
            print(f"  ⏸  已连发 {self._since_rest} 条，休息 {rest / 60:.1f} 分钟…")
            time.sleep(rest)
            self._since_rest = 0
            self._next_rest_at = random.randint(*self.cfg.batch_size)
            return
        # 没到长休息，也要把两次发送拉开——真人不会每 10 秒打一个招呼。
        # 偶尔来一次「分心」式的长停顿，让间隔分布别太规整。
        lo, hi = self.cfg.send_delay
        gap = random.uniform(lo, hi)
        if random.random() < 0.15:
            gap *= random.uniform(1.8, 3.0)
        print(f"  ⏳ 等 {gap:.0f}s 再发下一条…")
        time.sleep(gap)

    # ------------------------------------------------------------ 闸门

    def check_quota(self) -> None:
        if self.sent_today >= self.cfg.daily_limit:
            raise Circuit(f"已达每日上限 {self.cfg.daily_limit} 条")

    def after_failure(self, detail: str = "") -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.cfg.max_consecutive_failures:
            raise Circuit(
                f"连续 {self._consecutive_failures} 次发送失败，疑似被风控拦截。{detail}".strip()
            )

    @property
    def remaining(self) -> int:
        return max(0, self.cfg.daily_limit - self.sent_today)
