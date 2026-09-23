"""连续模式（-c）与小时级速率闸门。不起浏览器、不打网络、不真 sleep。"""

import pytest

from boss_greeter import runner
from boss_greeter.config import Config
from boss_greeter.pacing import Circuit, Pacer, QuotaReached


def _cfg(tmp_path, **pacing):
    resume = tmp_path / "r.md"
    resume.write_text("简历", encoding="utf-8")
    base = {"action_delay": [0, 0], "send_delay": [0, 0], "batch_rest": [0, 0],
            "batch_size": [1000, 1000], "round_rest": [0, 0]}
    base.update(pacing)
    return Config.model_validate({
        "search": {"keywords": ["golang"]},
        "greeting": {"provider": "openai", "model": "m", "resume_path": str(resume)},
        "pacing": base,
    })


# ---------------------------------------------------------------- 小时闸门


def test_quota_reached_is_a_circuit_subclass():
    """连续模式要靠类型区分「配额用完」和「撞风控」，前者正常收工、后者立刻停手。
    但对老的 except Circuit 调用方必须保持兼容。"""
    assert issubclass(QuotaReached, Circuit)


def test_hourly_limit_holds_after_the_quota_for_the_hour_is_spent(tmp_path, monkeypatch):
    """语义是「一小时内最多发 N 条」：发满第 N 条之后就卡住，等窗口滑出再放行。

    闸门放在 after_send 里，所以等待发生在「发满那条之后」而不是「第 N+1 条之前」，
    两者效果相同。这里测的是 N=3：前两条畅通，第 3 条发完开始等。
    """
    cfg = _cfg(tmp_path, hourly_limit=3)
    slept = []
    import boss_greeter.pacing as pacing_mod
    monkeypatch.setattr(pacing_mod.time, "sleep", lambda s: slept.append(s))

    pacer = Pacer(cfg.pacing)
    pacer.after_send()
    pacer.after_send()
    assert not [s for s in slept if s > 1], "没发满就不该等"

    pacer.after_send()
    waits = [s for s in slept if s > 1]
    assert waits, "发满 3 条后应该等窗口滑出"
    # 三条是瞬间发完的，所以最老那条几乎就是现在，得等接近整小时。
    # 真实场景里发送是摊开的，等待会短得多（只等到最老那条满一小时）。
    assert 3500 < waits[0] <= 3601, f"该等接近一小时，实际 {waits[0]:.0f}s"
    assert pacer.sent_today == 3, "闸门只拉长间隔，不该少发"


def test_hourly_limit_zero_disables_the_gate(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, hourly_limit=0)
    slept = []
    import boss_greeter.pacing as pacing_mod
    monkeypatch.setattr(pacing_mod.time, "sleep", lambda s: slept.append(s))
    pacer = Pacer(cfg.pacing)
    for _ in range(50):
        pacer.after_send()
    assert not [s for s in slept if s > 1]


# ---------------------------------------------------------------- 轮次循环


class _Page:
    url = "https://www.zhipin.com/web/geek/job"

    def goto(self, url, wait_until=None):
        pass


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """把浏览器、登录检查和单轮流程全换成桩，只留轮次循环本身。"""
    import contextlib

    monkeypatch.setattr(runner, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(runner, "browser_context",
                        lambda _cfg: contextlib.nullcontext(object()))
    monkeypatch.setattr(runner, "page_of", lambda ctx, attach=False: _Page())
    monkeypatch.setattr(runner, "is_logged_in", lambda page, sel: True)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)

    def script(per_round):
        """per_round: 每轮各发出多少条。"""
        seq = list(per_round)

        def fake_loop(ctx, page, cfg, sel, store, pacer, greeter, card_chain, jd_chain,
                      stats, run_id, dry_run, limit, allow_fallback=False,
                      default_greeting=False, screener=None):
            n = seq.pop(0) if seq else 0
            for _ in range(n):
                pacer.check_quota()
                stats.sent += 1
                pacer.after_send()
        monkeypatch.setattr(runner, "_loop", fake_loop)
    return script


def test_single_round_by_default(harness, tmp_path):
    harness([5, 5, 5])
    stats = runner.run(_cfg(tmp_path), None, default_greeting=True)
    assert stats.rounds == 1
    assert stats.sent == 5, "不加 -c 就只跑一轮"


def test_continuous_keeps_going_until_quota_is_used_up(harness, tmp_path):
    harness([4, 4, 4, 4, 4])
    cfg = _cfg(tmp_path, daily_limit=10, hourly_limit=0)
    stats = runner.run(cfg, None, default_greeting=True, continuous=True)
    assert stats.sent == 10, "配额用完就该停，不该超发"
    assert stats.rounds >= 3
    assert "上限" in stats.stop_reason and "收工" in stats.stop_reason


def test_continuous_stops_after_consecutive_empty_rounds(harness, tmp_path):
    """候选耗尽后再跑也是重扫同一批（全被去重挡掉），空转几轮就该收工。"""
    harness([3, 0, 0, 0, 0])
    cfg = _cfg(tmp_path, daily_limit=100, hourly_limit=0, max_empty_rounds=2)
    stats = runner.run(cfg, None, default_greeting=True, continuous=True)
    assert stats.sent == 3
    assert stats.rounds == 3, "第 1 轮发 3 条，第 2、3 轮空 → 收工"
    assert "没有新岗位" in stats.stop_reason


def test_a_productive_round_resets_the_empty_counter(harness, tmp_path):
    harness([2, 0, 2, 0, 0, 0])
    cfg = _cfg(tmp_path, daily_limit=100, hourly_limit=0, max_empty_rounds=2)
    stats = runner.run(cfg, None, default_greeting=True, continuous=True)
    assert stats.sent == 4, "中间那轮有产出，空轮计数该清零而不是提前收工"
    assert "没有新岗位" in stats.stop_reason


def test_risk_circuit_stops_continuous_immediately(harness, tmp_path, monkeypatch):
    """风控熔断跟配额用完不一样：必须立刻停，不能休息一下接着跑。"""
    def fake_loop(*a, **k):
        raise Circuit("页面出现风控提示：「操作过于频繁」")
    monkeypatch.setattr(runner, "_loop", fake_loop)
    stats = runner.run(_cfg(tmp_path), None, default_greeting=True, continuous=True)
    assert stats.rounds == 1
    assert "熔断" in stats.stop_reason and "操作过于频繁" in stats.stop_reason
