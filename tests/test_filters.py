import pytest

from boss_greeter.filters import (
    NON_RND_TITLES, ActivityFilter, CompanySizeFilter, EducationFilter, JobSourceFilter,
    KeywordFilter, RndOnlyFilter, SalaryFilter, TopicFilter, education_rank, parse_salary,
    run_chain,
)
from boss_greeter.models import Job


def job(**kw) -> Job:
    base = dict(job_id="j1", title="Golang 架构师", url="/job_detail/j1.html",
                company="某科技", salary_text="30-60K·15薪", area_text="北京·海淀区",
                experience="10年以上", education="本科", company_size="1000-9999人",
                activity="在线", jd="", skills=["Golang"])
    base.update(kw)
    return Job(**base)


# ------------------------------------------------------------ 薪资解析

@pytest.mark.parametrize("text,lo,hi,months", [
    ("20-40K", 20, 40, 12),
    ("20-40K·15薪", 20, 40, 15),
    ("8-10K·13薪", 8, 10, 13),
    ("15-25K", 15, 25, 12),
    ("1.5-2万", 15, 20, 12),
    ("8千-1.2万", 8, 12, 12),
    ("10K-15K", 10, 15, 12),
    ("20K以上", 20, 20, 12),
    ("30-60K·15薪", 30, 60, 15),
])
def test_parse_salary_ranges(text, lo, hi, months):
    s = parse_salary(text)
    assert s is not None and not s.negotiable
    assert s.min_k == pytest.approx(lo)
    assert s.max_k == pytest.approx(hi)
    assert s.months == months


def test_parse_salary_per_day():
    s = parse_salary("300-500元/天")
    assert s is not None
    # 22 个工作日折算成月薪
    assert s.min_k == pytest.approx(6.6)
    assert s.max_k == pytest.approx(11.0)


def test_parse_salary_negotiable():
    s = parse_salary("面议")
    assert s is not None and s.negotiable


@pytest.mark.parametrize("text", ["", "薪资待定", "abc"])
def test_parse_salary_unparseable(text):
    assert parse_salary(text) is None


# ------------------------------------------------------------ 学历

def test_education_order():
    assert education_rank("大专") < education_rank("本科") < education_rank("硕士") < education_rank("博士")
    assert education_rank("学历不限") == education_rank("不限")
    assert education_rank("") == -1


def test_education_longest_match_wins():
    # 「中专/中技」不能被短词「中专」抢先匹配到不同的序号
    assert education_rank("中专/中技") == education_rank("中专/中技")
    assert education_rank("中专/中技") < education_rank("大专")


def test_education_filter():
    f = EducationFilter("本科")
    assert f.check(job(education="本科")).passed
    assert f.check(job(education="大专")).passed
    assert not f.check(job(education="硕士")).passed
    # 岗位没写学历要求时放行
    assert f.check(job(education="")).passed


# ------------------------------------------------------------ 各过滤器

def test_salary_filter_uses_upper_bound():
    f = SalaryFilter(min_k=40, allow_negotiable=False)
    assert f.check(job(salary_text="30-60K")).passed          # 上限够得着
    assert not f.check(job(salary_text="20-35K")).passed      # 上限也不够
    assert not f.check(job(salary_text="面议")).passed


def test_salary_filter_allows_negotiable_when_configured():
    assert SalaryFilter(min_k=40, allow_negotiable=True).check(job(salary_text="面议")).passed


def test_activity_filter():
    f = ActivityFilter(["刚刚活跃", "今日活跃"])
    assert f.check(job(activity="刚刚活跃")).passed
    assert not f.check(job(activity="半年前活跃")).passed
    assert not f.check(job(activity="")).passed


def test_company_size_filter_tolerates_format_drift():
    f = CompanySizeFilter(["1000-9999人"])
    assert f.check(job(company_size="1000-9999人")).passed
    assert f.check(job(company_size="1000-9999")).passed
    assert not f.check(job(company_size="0-20人")).passed


def test_keyword_blacklist_on_title():
    f = KeywordFilter(["外包", "驻场"], [], ["title", "company"])
    assert not f.check(job(title="Java 外包工程师")).passed
    assert f.check(job(title="Golang 架构师")).passed


def test_keyword_whitelist_only_checked_when_enabled():
    with_wl = KeywordFilter([], ["Golang"], ["jd"], check_whitelist=True)
    assert with_wl.check(job(jd="要求熟悉 Golang 微服务")).passed
    assert not with_wl.check(job(jd="要求熟悉 Java")).passed
    # 未开启白名单校验时，JD 不含白名单词也放行
    without_wl = KeywordFilter([], ["Golang"], ["jd"], check_whitelist=False)
    assert without_wl.check(job(jd="要求熟悉 Java")).passed


# ------------------------------------------------------------ 链

def test_chain_short_circuits_and_reports_rule():
    chain = [ActivityFilter(["在线"]), SalaryFilter(min_k=40, allow_negotiable=False)]
    r = run_chain(chain, job(activity="半年前活跃", salary_text="10-15K"))
    assert not r.passed
    assert r.rejected_by == "activity"       # 停在第一个拒绝的规则上
    assert "半年前活跃" in r.reason


def test_chain_passes_when_all_ok():
    chain = [ActivityFilter(["在线"]), SalaryFilter(min_k=40, allow_negotiable=False)]
    assert run_chain(chain, job()).passed


# ------------------------------------------------------------ 方向白名单

@pytest.mark.parametrize("title,expected", [
    ("Golang 架构师", True),
    ("AI 应用平台负责人", True),
    ("大模型推理工程师", True),
    ("Go 后端开发", True),
    # 下面这些是搜 Golang / AI 时真的会混进来的东西
    ("分布式文件存储-架构师", False),
    ("前端工程师（React）", False),
    # 英文词按独立单词匹配，不能被这些词里的 ai / go 误伤
    ("区块链 Blockchain 工程师", False),
    ("MongoDB DBA", False),
    ("Django 开发工程师", False),
])
def test_topic_filter_keeps_only_the_directions_we_want(title, expected):
    flt = TopicFilter(["golang", "go", "AI", "大模型"])
    assert flt.check(job(title=title, skills=[])).passed is expected


def test_topic_filter_also_looks_at_the_skill_tags():
    # 岗位名很泛，但技能标签暴露了它是 Go 岗
    j = job(title="服务端研发工程师", skills=["Golang", "MySQL"])
    assert TopicFilter(["golang"]).check(j).passed


def test_empty_topic_list_means_no_restriction():
    assert TopicFilter([]).check(job(title="前端工程师", skills=[])).passed


# ---------------------------------------------------------------- 猎头 / 研发岗

def test_job_source_rejects_headhunter():
    f = JobSourceFilter(exclude_headhunter=True, exclude_campus=True)
    job = Job(job_id="1", title="golang架构师", url="", is_headhunter=True)
    v = f.check(job)
    assert not v.passed and "猎头" in v.reason


def test_job_source_rejects_campus():
    f = JobSourceFilter(exclude_headhunter=True, exclude_campus=True)
    job = Job(job_id="1", title="Golang", url="", job_type=5)
    v = f.check(job)
    assert not v.passed and "应届" in v.reason


def test_job_source_passes_normal_job():
    f = JobSourceFilter(exclude_headhunter=True, exclude_campus=True)
    assert f.check(Job(job_id="1", title="golang架构师", url="")).passed


def test_job_source_can_allow_headhunter():
    f = JobSourceFilter(exclude_headhunter=False, exclude_campus=True)
    assert f.check(Job(job_id="1", title="x", url="", is_headhunter=True)).passed


def test_rnd_only_rejects_product_manager():
    """这类岗位 JD 里全是 AI/大模型，topic 白名单拦不住，得靠岗位名。"""
    f = RndOnlyFilter(NON_RND_TITLES)
    v = f.check(Job(job_id="1", title="AI产品经理", url=""))
    assert not v.passed and "产品经理" in v.reason


def test_rnd_only_rejects_presales_and_qa():
    f = RndOnlyFilter(NON_RND_TITLES)
    assert not f.check(Job(job_id="1", title="大模型解决方案专家", url="")).passed
    assert not f.check(Job(job_id="2", title="测试开发工程师", url="")).passed


def test_rnd_only_keeps_real_dev_titles():
    f = RndOnlyFilter(NON_RND_TITLES)
    for title in ("Golang 后端开发工程师", "Python 研发工程师",
                  "Node.js 全栈工程师", "AI 应用架构师", "大模型算法工程师"):
        assert f.check(Job(job_id="x", title=title, url="")).passed, title


def test_rnd_only_ascii_words_need_boundaries():
    """'UI' 不该把 'Building'、'Guide' 之类误伤。"""
    f = RndOnlyFilter(["UI"])
    assert f.check(Job(job_id="1", title="Guide 平台后端工程师", url="")).passed
    assert not f.check(Job(job_id="2", title="UI 设计", url="")).passed


# ---------------------------------------------------------------- 外包黑名单

def _kw(cfg_words, fields=("title", "company", "jd")):
    return KeywordFilter(list(cfg_words), [], list(fields), check_whitelist=False)


OUTSOURCE_WORDS = ["外包", "驻场", "外派", "人才外包", "人力外包", "外包事业",
                   "甲方", "客户现场", "常驻客户", "劳务派遣"]


def test_blacklist_catches_outsourcing_in_jd():
    """外包岗很少在标题里写「外包」，词都埋在 JD 的公司介绍里。"""
    f = _kw(OUTSOURCE_WORDS)
    v = f.check(job(title="Go 后端研发工程师", company="某软件",
                    jd="公司开展软件人才外派服务，拥有三大外包事业群，十六大城市外包事业部。"))
    assert not v.passed and v.rejected_by == "keyword"


def test_blacklist_catches_onsite_delivery():
    f = _kw(OUTSOURCE_WORDS)
    assert not f.check(job(jd="根据项目阶段需要，参与客户现场的业务调研与系统部署联调。")).passed


def test_blacklist_does_not_eat_normal_jd():
    """这几条真实 JD 曾被过宽的词表误伤，留作回归。"""
    f = _kw(OUTSOURCE_WORDS)
    # 「用工」曾卡在「熟练运用工作窃取」上
    assert f.check(job(jd="精通 Go 的 M:N 调度模型，能熟练运用工作窃取等机制优化调度效率。")).passed
    # 「入驻」曾卡在 ISV 产品语境上
    assert f.check(job(jd="完整了解 ISV 应用入驻、发布、企业授权、事件回调全链路。")).passed
