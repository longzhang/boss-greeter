"""贯穿整个流水线的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Job:
    """一个岗位。列表页先填前半部分，通过初筛后再进详情页补 jd。"""

    job_id: str
    title: str
    url: str
    company: str = ""
    salary_text: str = ""
    area_text: str = ""
    experience: str = ""
    education: str = ""
    company_size: str = ""
    company_stage: str = ""
    #: HR 在线状态。BOSS 早年的「X 日内活跃」文案已经从页面和接口里一起消失了，
    #: 现在能拿到的只有在线/离线。
    activity: str = ""
    #: 岗位技能标签，接口直给（DOM 上没有）。topic 过滤要用。
    skills: list[str] = field(default_factory=list)
    #: 猎头/代招岗位。接口的 proxyJob / goldHunter / proxyType 三个字段同时为 1，
    #: 对应卡片左上角那个「猎头」角标。
    is_headhunter: bool = False
    #: 接口的 jobType。5 = 在校/应届，0 = 普通社招。
    job_type: int = 0
    jd: str = ""
    keyword: str = ""  # 由哪个搜索关键词命中
    scraped_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def city(self) -> str:
        """area_text 形如「北京·海淀区·中关村」，取第一段。"""
        return self.area_text.split("·")[0].strip() if self.area_text else ""

    def __str__(self) -> str:
        return f"{self.title} @ {self.company} ({self.salary_text}, {self.area_text})"


@dataclass
class FilterResult:
    """过滤链的判定结果。rejected_by 用于 stats 统计各规则拦截量。"""

    passed: bool
    rejected_by: str = ""
    reason: str = ""

    @classmethod
    def ok(cls) -> FilterResult:
        return cls(passed=True)

    @classmethod
    def reject(cls, rule: str, reason: str) -> FilterResult:
        return cls(passed=False, rejected_by=rule, reason=reason)


@dataclass
class Greeting:
    """一条招呼语及其投递结果。"""

    job_id: str
    message: str
    model: str
    status: str  # sent | failed | dry_run | skipped
    reason: str = ""
    sent_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
