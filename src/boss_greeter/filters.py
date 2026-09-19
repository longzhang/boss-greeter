"""岗位过滤链。全是纯函数/纯逻辑，不碰浏览器，可完整单测。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Protocol

from .config import Config
from .models import FilterResult, Job

# ---------------------------------------------------------------- 薪资解析

# 学历从低到高。岗位要求的学历若高于配置上限则拒绝。
EDUCATION_ORDER = [
    "不限", "初中及以下", "中专/中技", "中专", "中技", "高中",
    "大专", "本科", "硕士", "博士",
]

_NEGOTIABLE_WORDS = ("面议", "面谈")
_UNIT_SCALE = {"K": 1.0, "k": 1.0, "千": 1.0, "万": 10.0}
_WORKDAYS_PER_MONTH = 22


@dataclass(frozen=True)
class Salary:
    """归一化后的薪资，单位统一为「K / 月」。"""

    min_k: float
    max_k: float
    months: int = 12
    negotiable: bool = False
    raw: str = ""


def _to_k(value: float, unit: str | None, *, unitless_is_yuan: bool = False) -> float:
    """把带单位的数值换算成 K。

    无单位时的含义取决于语境：按天计薪（'300-500元/天'）里裸数字就是「元」，
    月薪（'20-40'）里裸数字是「K」——除非大到 >=1000，那也只能是「元」。
    """
    if unit:
        return value * _UNIT_SCALE[unit]
    if unitless_is_yuan or value >= 1000:
        return value / 1000
    return value


def parse_salary(text: str) -> Salary | None:
    """解析 BOSS 的薪资文案。无法识别返回 None，「面议」返回 negotiable=True。

    覆盖：'20-40K'、'20-40K·15薪'、'8千-1.2万'、'1.5-2万'、'300-500元/天'、
         '20K以上'、'面议'。
    """
    if not text:
        return None
    raw = text.strip()
    s = raw.replace(" ", "").replace("－", "-").replace("—", "-").replace("–", "-")

    if any(w in s for w in _NEGOTIABLE_WORDS):
        return Salary(0.0, 0.0, negotiable=True, raw=raw)

    months = 12
    if m := re.search(r"[·•・*](\d+)薪", s):
        months = int(m.group(1))
        s = s[: m.start()]

    per_day = bool(re.search(r"元?/[天日]", s))
    s = re.sub(r"元?/[天日]", "", s)
    s = s.replace("元", "")

    num = r"(\d+(?:\.\d+)?)"
    unit = r"(K|k|千|万)?"

    if m := re.match(rf"^{num}{unit}-{num}{unit}", s):
        lo_v, lo_u, hi_v, hi_u = m.groups()
        # '1.5-2万' 这种只在尾部标单位，前半段沿用后半段的单位
        lo_u = lo_u or hi_u
        lo = _to_k(float(lo_v), lo_u, unitless_is_yuan=per_day)
        hi = _to_k(float(hi_v), hi_u, unitless_is_yuan=per_day)
    elif m := re.match(rf"^{num}{unit}", s):
        lo = hi = _to_k(float(m.group(1)), m.group(2), unitless_is_yuan=per_day)
    else:
        return None

    if per_day:
        lo, hi = lo * _WORKDAYS_PER_MONTH, hi * _WORKDAYS_PER_MONTH

    return Salary(min_k=lo, max_k=hi, months=months, raw=raw)


def education_rank(text: str) -> int:
    """学历 → 序号。识别不了返回 -1（调用方按「不限」放行）。"""
    if not text:
        return -1
    t = text.strip()
    # 长的先匹配，避免「中专/中技」被「中专」截断
    for i, name in sorted(enumerate(EDUCATION_ORDER), key=lambda p: -len(p[1])):
        if name in t:
            return i
    return -1


# ---------------------------------------------------------------- 过滤器


class Filter(Protocol):
    name: str

    def check(self, job: Job) -> FilterResult: ...


@dataclass
class DedupFilter:
    """已经打过招呼的岗位不再重复打扰。"""

    is_seen: Callable[[str], bool]
    name: str = "dedup"

    def check(self, job: Job) -> FilterResult:
        if self.is_seen(job.job_id):
            return FilterResult.reject(self.name, "已打过招呼")
        return FilterResult.ok()


def topic_hit(text: str, term: str) -> bool:
    """方向词是否出现在 text 里。

    纯 ASCII 的词（ai / go / llm）按「独立单词」匹配，否则 "ai" 会被 chain、training、
    domain 里的 ai 误伤，"go" 会被 Google、MongoDB、Django 误伤。含中文的词直接包含匹配
    ——中文没有词边界，而且「大模型」这类词本身就不会嵌在别的词里。
    """
    if term.isascii():
        return re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text, re.I) is not None
    return term in text


@dataclass
class TopicFilter:
    """只要指定方向的岗位。

    BOSS 的搜索结果很散：搜「Golang 架构师」会顺带塞进来「分布式文件存储-架构师」
    「Golang游戏开发」这类沾边不沾心的岗位。这一条在岗位名和技能标签上做白名单匹配，
    把方向不对的挡在详情页之外——既省 LLM 的钱，也省每天有限的打招呼配额。
    """

    allow: list[str]
    name: str = "topic"

    def check(self, job: Job) -> FilterResult:
        if not self.allow:
            return FilterResult.ok()
        hay = f"{job.title} {' '.join(job.skills)}"
        if any(topic_hit(hay, w) for w in self.allow):
            return FilterResult.ok()
        return FilterResult.reject(self.name, f"「{job.title}」方向不符")


#: 一眼就不是研发岗的title词。这些岗位的 JD 里常常堆满 AI / Python，
#: 光靠 topic 白名单拦不住——「AI产品经理」「大模型解决方案顾问」都会命中。
NON_RND_TITLES = [
    "产品经理", "产品专家", "产品负责人", "项目经理", "解决方案", "售前", "售后",
    "销售", "商务", "market", "运营", "客服", "人力", "招聘", "行政", "财务",
    "法务", "设计师", "UI", "UX", "编辑", "写作", "讲师", "教研", "培训",
    "分析师", "咨询", "顾问", "测试", "实施", "交付", "标注",
]


@dataclass
class JobSourceFilter:
    """挡掉猎头/代招岗位和应届实习岗。

    猎头岗（卡片左上角那个「猎头」角标）由接口的 proxyJob/goldHunter 标出来，
    跟公司自招混在同一批搜索结果里，只看标题分不出来。
    """

    exclude_headhunter: bool
    exclude_campus: bool
    name: str = "job_source"

    def check(self, job: Job) -> FilterResult:
        if self.exclude_headhunter and job.is_headhunter:
            return FilterResult.reject(self.name, "猎头/代招岗位")
        if self.exclude_campus and job.job_type == 5:
            return FilterResult.reject(self.name, "在校/应届岗位")
        return FilterResult.ok()


@dataclass
class RndOnlyFilter:
    """只要研发岗。命中 NON_RND_TITLES 里的词就拒。

    只看岗位名，不看 JD——JD 里出现「配合产品经理」是家常便饭，拿它匹配会误杀一片。
    """

    deny: list[str]
    name: str = "rnd_only"

    def check(self, job: Job) -> FilterResult:
        for word in self.deny:
            if topic_hit(job.title, word):
                return FilterResult.reject(self.name, f"「{job.title}」不是研发岗（命中「{word}」）")
        return FilterResult.ok()


@dataclass
class ActivityFilter:
    allow: list[str]
    name: str = "activity"

    def check(self, job: Job) -> FilterResult:
        if not job.activity:
            return FilterResult.reject(self.name, "未获取到活跃度")
        if any(a in job.activity for a in self.allow):
            return FilterResult.ok()
        return FilterResult.reject(self.name, f"活跃度「{job.activity}」不在白名单")


@dataclass
class SalaryFilter:
    min_k: int
    allow_negotiable: bool
    name: str = "salary"

    def check(self, job: Job) -> FilterResult:
        sal = parse_salary(job.salary_text)
        if sal is None:
            return FilterResult.reject(self.name, f"薪资无法解析：{job.salary_text!r}")
        if sal.negotiable:
            return (
                FilterResult.ok()
                if self.allow_negotiable
                else FilterResult.reject(self.name, "薪资面议")
            )
        # 用上限比较：只要岗位开得到我的下限就值得聊
        if sal.max_k < self.min_k:
            return FilterResult.reject(
                self.name, f"薪资上限 {sal.max_k:g}K < 期望 {self.min_k}K"
            )
        return FilterResult.ok()


@dataclass
class CityFilter:
    allow: list[str]
    name: str = "city"

    def check(self, job: Job) -> FilterResult:
        if not self.allow:
            return FilterResult.ok()
        if any(c in job.area_text for c in self.allow):
            return FilterResult.ok()
        return FilterResult.reject(self.name, f"城市「{job.area_text}」不在白名单")


@dataclass
class EducationFilter:
    max_required: str
    name: str = "education"

    def check(self, job: Job) -> FilterResult:
        limit = education_rank(self.max_required)
        required = education_rank(job.education)
        if required < 0:  # 岗位没写学历要求，视作不限
            return FilterResult.ok()
        if required > limit:
            return FilterResult.reject(
                self.name, f"要求 {job.education} 高于上限 {self.max_required}"
            )
        return FilterResult.ok()


@dataclass
class CompanySizeFilter:
    allow: list[str]
    name: str = "company_size"

    def check(self, job: Job) -> FilterResult:
        if not self.allow:
            return FilterResult.ok()
        if not job.company_size:
            return FilterResult.reject(self.name, "未获取到公司规模")
        # 双向包含，容忍「1000-9999人」与「1000-9999」这类写法差异
        if any(a in job.company_size or job.company_size in a for a in self.allow):
            return FilterResult.ok()
        return FilterResult.reject(self.name, f"公司规模「{job.company_size}」不在白名单")


@dataclass
class KeywordFilter:
    """黑名单在指定字段上匹配；白名单非空时要求 JD 至少命中一个。"""

    blacklist: list[str]
    whitelist: list[str]
    fields: list[str]
    check_whitelist: bool = False
    name: str = "keyword"

    def check(self, job: Job) -> FilterResult:
        haystacks = {"title": job.title, "company": job.company, "jd": job.jd}
        for f in self.fields:
            text = haystacks.get(f, "")
            for word in self.blacklist:
                if word in text:
                    return FilterResult.reject(self.name, f"{f} 命中黑名单词「{word}」")
        if self.check_whitelist and self.whitelist:
            if not any(w in job.jd for w in self.whitelist):
                return FilterResult.reject(self.name, "JD 未命中任何白名单词")
        return FilterResult.ok()


# ---------------------------------------------------------------- 组链


def build_card_chain(cfg: Config, is_seen: Callable[[str], bool]) -> list[Filter]:
    """列表页阶段：只用卡片上的字段，尽量在进详情页前就刷掉。"""
    f = cfg.filters
    chain: list[Filter] = [DedupFilter(is_seen)]
    if f.job_source.enabled:
        chain.append(JobSourceFilter(
            f.job_source.exclude_headhunter, f.job_source.exclude_campus
        ))
    if f.rnd_only.enabled:
        chain.append(RndOnlyFilter(f.rnd_only.deny or NON_RND_TITLES))
    if f.topic.enabled:
        chain.append(TopicFilter(f.topic.allow))
    if f.activity.enabled:
        chain.append(ActivityFilter(f.activity.allow))
    if f.salary.enabled:
        chain.append(SalaryFilter(f.salary.min_k, f.salary.allow_negotiable))
    if f.city.enabled:
        chain.append(CityFilter(f.city.allow))
    if f.education.enabled:
        chain.append(EducationFilter(f.education.max_required))
    if f.company_size.enabled:
        chain.append(CompanySizeFilter(f.company_size.allow))
    if f.keyword.enabled:
        fields = [x for x in f.keyword.blacklist_fields if x in ("title", "company")]
        chain.append(KeywordFilter(f.keyword.blacklist, [], fields, check_whitelist=False))
    return chain


def build_jd_chain(cfg: Config) -> list[Filter]:
    """详情页阶段：JD 到手后才能跑的规则。"""
    f = cfg.filters
    if not f.keyword.enabled:
        return []
    fields = [x for x in f.keyword.blacklist_fields if x == "jd"]
    return [KeywordFilter(f.keyword.blacklist, f.keyword.whitelist, fields, check_whitelist=True)]


def run_chain(chain: list[Filter], job: Job) -> FilterResult:
    """依次执行，任一拒绝即短路。"""
    for flt in chain:
        result = flt.check(job)
        if not result.passed:
            return result
    return FilterResult.ok()
