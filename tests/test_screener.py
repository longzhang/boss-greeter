"""AI 判定的纯逻辑单测。不打网络——只测标签解析和边界处理。"""

from boss_greeter.models import Job
from boss_greeter.screener import Screener, Verdict


def parse(raw: str) -> Verdict:
    return Screener._parse(raw)


def test_plain_labels():
    assert parse("OK").ok
    assert not parse("OUTSOURCE").ok
    assert parse("OUTSOURCE").label == "OUTSOURCE"
    assert parse("SALES").label == "SALES"


def test_tolerates_punctuation_and_whitespace():
    """模型偶尔会带标点或换行。"""
    assert parse("  OK。\n").ok
    assert parse("OUTSOURCE.").label == "OUTSOURCE"
    assert parse("**SALES**").label == "SALES"


def test_lowercase():
    assert parse("outsource").label == "OUTSOURCE"


def test_takes_the_first_label_mentioned():
    """「OK，不是外包」这种多余解释，不能被后面的 OUTSOURCE 带偏。"""
    assert parse("OK，这不是 OUTSOURCE 岗位").ok
    # 反过来也要对
    assert parse("OUTSOURCE —— 明显是外包，不 OK").label == "OUTSOURCE"


def test_unparseable_is_treated_as_error_and_passes():
    """判不出来时放行，但标记 errored 让调用方计数——宁可多发，不可漏判还不报警。"""
    v = parse("我觉得这个岗位还行吧")
    assert v.ok and v.errored


def test_empty_is_error():
    assert parse("").errored
    assert parse("   ").errored


def test_no_jd_passes_without_calling_model():
    """没 JD 就没得判，且不该算模型出错。"""
    sc = Screener.__new__(Screener)       # 不走 __init__，避免建客户端
    sc.init_error = None
    v = sc.check(Job(job_id="1", title="Go 后端", url="", jd=""))
    assert v.ok and not v.errored
