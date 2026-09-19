"""列表接口的解析。真实响应的字段名以 data/logs 里抓到的样本为准。"""

from boss_greeter.scraper import parse_api_job, parse_api_payload

# 一条真实响应记录（删掉了埋点字段）
RAW = {
    "encryptJobId": "7c6de0b595d4daf90nN-2N61E1tW",
    "jobName": "golang架构师",
    "salaryDesc": "45-75K",
    "jobLabels": ["5-10年", "大专"],
    "skills": ["Golang", "MySQL", "系统架构设计经验"],
    "jobExperience": "5-10年",
    "jobDegree": "大专",
    "cityName": "北京",
    "areaDistrict": "海淀区",
    "businessDistrict": "苏州桥",
    "bossOnline": True,
    "brandName": "小确信",
    "brandStageName": "",
    "brandScaleName": "20-99人",
}


def test_parses_the_fields_the_dom_cannot_give_us():
    job = parse_api_job(RAW, keyword="Golang 架构师")
    # 这三项正是换用接口的理由：页面上薪资是密文，公司规模和技能标签压根没有
    assert job.salary_text == "45-75K"
    assert job.company_size == "20-99人"
    assert job.skills == ["Golang", "MySQL", "系统架构设计经验"]


def test_builds_a_usable_detail_url_and_area():
    job = parse_api_job(RAW, keyword="k")
    assert job.url == "https://www.zhipin.com/job_detail/7c6de0b595d4daf90nN-2N61E1tW.html"
    assert job.area_text == "北京·海淀区·苏州桥"
    assert job.city == "北京"


def test_area_skips_missing_parts():
    job = parse_api_job({**RAW, "businessDistrict": ""}, keyword="k")
    assert job.area_text == "北京·海淀区"


def test_online_flag_becomes_the_activity_text():
    assert parse_api_job(RAW, "k").activity == "在线"
    assert parse_api_job({**RAW, "bossOnline": False}, "k").activity == "离线"


def test_records_without_an_id_or_name_are_dropped():
    assert parse_api_job({**RAW, "encryptJobId": ""}, "k") is None
    assert parse_api_job({**RAW, "jobName": ""}, "k") is None


def test_payload_reports_whether_more_pages_follow():
    jobs, has_more = parse_api_payload(
        {"zpData": {"jobList": [RAW, {**RAW, "encryptJobId": "x2"}], "hasMore": True}}, "k"
    )
    assert [j.job_id for j in jobs] == ["7c6de0b595d4daf90nN-2N61E1tW", "x2"]
    assert has_more is True


def test_payload_survives_an_empty_or_malformed_response():
    assert parse_api_payload({}, "k") == ([], False)
    assert parse_api_payload({"zpData": {"jobList": None}}, "k") == ([], False)
