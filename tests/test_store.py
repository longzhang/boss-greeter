import pytest

from boss_greeter.models import Greeting, Job
from boss_greeter.store import Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "t.db") as s:
        yield s


def a_job(job_id="j1") -> Job:
    return Job(job_id=job_id, title="Golang 架构师", url="/job_detail/j1.html", company="某科技")


def test_dedup_only_counts_actually_sent(store):
    store.save_job(a_job())
    # 预演不应该挡住之后的真实发送
    store.save_greeting(Greeting("j1", "hi", "m", "dry_run"))
    assert not store.is_greeted("j1")
    # 失败的也应该允许重试
    store.save_greeting(Greeting("j1", "hi", "m", "failed", "超时"))
    assert not store.is_greeted("j1")
    # 只有真的发出去了才算
    store.save_greeting(Greeting("j1", "hi", "m", "sent"))
    assert store.is_greeted("j1")


def test_sent_today_counts_only_sent(store):
    store.save_greeting(Greeting("j1", "hi", "m", "sent"))
    store.save_greeting(Greeting("j2", "hi", "m", "dry_run"))
    store.save_greeting(Greeting("j3", "hi", "m", "failed"))
    assert store.sent_today() == 1


def test_save_job_upserts_jd(store):
    job = a_job()
    store.save_job(job)
    job.jd = "负责 AI 平台建设"
    store.save_job(job)
    row = store.conn.execute("SELECT jd, COUNT(*) OVER () AS n FROM jobs").fetchone()
    assert row["jd"] == "负责 AI 平台建设"
    assert row["n"] == 1  # upsert 而不是插入第二行


def test_rejection_stats(store):
    store.record_rejection(1, "j1", "salary", "低于期望")
    store.record_rejection(1, "j2", "salary", "低于期望")
    store.record_rejection(1, "j3", "activity", "不活跃")
    counts = {r["rule"]: r["n"] for r in store.rejection_counts(7)}
    assert counts == {"salary": 2, "activity": 1}


def test_run_lifecycle(store):
    run_id = store.start_run()
    store.finish_run(run_id, scanned=10, rejected=7, sent=3, failed=0,
                     dry_run=0, stop_reason="正常结束")
    run = store.recent_runs(1)[0]
    assert run["scanned"] == 10 and run["sent"] == 3
    assert run["stop_reason"] == "正常结束"


def test_already_chatted_blocks_retry(store):
    """详情页按钮已是「继续沟通」= 这个 HR 早聊过了。不挡住的话每轮都会
    重新进详情页、重新点一次，白跑一趟还各记一次失败。"""
    store.save_greeting(Greeting("j9", "", "default", "already", "之前已经沟通过"))
    assert store.is_greeted("j9")
    # 但不该算进今天的发送量——今天并没有真的发出去
    assert store.sent_today() == 0
