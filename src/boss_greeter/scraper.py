"""列表页抓取与详情页 JD 提取。

列表数据走 BOSS 自己的 JSON 接口（`/wapi/zpgeek/search/joblist.json`），不再解析卡片 DOM：

- **薪资在 DOM 里是密文。** BOSS 用一套自定义字体把数字映射到 Unicode 私有区码点，
  `.job-salary` 肉眼看是 45-75K，`textContent` 取到的却是 U+E035 U+E036 这类无意义字符，
  没法解析。接口里则是明文 `"salaryDesc": "45-75K"`。
- **公司规模、技能标签卡片上压根没有**，接口里有 `brandScaleName` / `skills`。

接口是页面自己发的 POST，翻页靠往下滚触发（搜索 URL 里的 `page=` 参数对接口无效）。
所以这里的做法是：正常打开搜索页、正常往下滚，顺手把它自己的响应接下来——
不自己构造请求，省得对不上签名，也更像真人在浏览。

DOM 解析（`parse_card` / `scrape_page`）作为接口改版时的兜底保留。
"""

from __future__ import annotations

import re
from typing import Callable
from urllib.parse import urlencode

from patchright.sync_api import Locator, Page, TimeoutError as PWTimeout

from .browser import (
    BOSS_HOME, JOB_SEARCH_URL, attr_of, human_scroll, text_of, texts_of,
)
from .cities import city_code
from .config import Selectors
from .models import Job

# 经验/学历标签混在同一个 tag-list 里，靠关键词区分（仅 DOM 兜底路径需要）
_EDU_WORDS = ("不限", "初中", "中专", "中技", "高中", "大专", "本科", "硕士", "博士")
_EXP_WORDS = ("经验", "应届", "在校")

#: 列表接口。页面自己发，我们只旁听。
JOBLIST_API = "/wapi/zpgeek/search/joblist.json"
#: 等一次列表接口响应的上限。滚到底之后不会再有请求，超时即视为没有更多了。
_API_TIMEOUT_MS = 20000


def search_url(keyword: str, city: str, page_no: int = 1) -> str:
    params = {"query": keyword, "city": city_code(city), "page": page_no}
    return f"{JOB_SEARCH_URL}?{urlencode(params)}"


def extract_job_id(href: str) -> str:
    """从 /job_detail/abc123~.html?lid=... 里取出岗位 ID。"""
    m = re.search(r"job_detail/([^./?]+)", href)
    return m.group(1) if m else href


def _absolute(href: str) -> str:
    if href.startswith("http"):
        return href
    return BOSS_HOME + href if href.startswith("/") else f"{BOSS_HOME}/{href}"


# ---------------------------------------------------------------- 接口解析


def parse_api_job(raw: dict, keyword: str) -> Job | None:
    """把列表接口里的一条记录转成 Job。缺 id 或岗位名就丢掉。"""
    job_id = (raw.get("encryptJobId") or "").strip()
    title = (raw.get("jobName") or "").strip()
    if not job_id or not title:
        return None

    area = "·".join(
        p for p in (raw.get("cityName"), raw.get("areaDistrict"), raw.get("businessDistrict"))
        if p
    )
    return Job(
        job_id=job_id,
        title=title,
        url=f"{BOSS_HOME}/job_detail/{job_id}.html",
        company=(raw.get("brandName") or "").strip(),
        salary_text=(raw.get("salaryDesc") or "").strip(),
        area_text=area,
        experience=(raw.get("jobExperience") or "").strip(),
        education=(raw.get("jobDegree") or "").strip(),
        company_size=(raw.get("brandScaleName") or "").strip(),
        company_stage=(raw.get("brandStageName") or "").strip(),
        # 接口只给布尔量，没有「X 日内活跃」那种文案了
        activity="在线" if raw.get("bossOnline") else "离线",
        skills=[s for s in (raw.get("skills") or []) if s],
        # 三个字段是一起出现的，取其一为真即可（接口偶尔只给其中一个）
        is_headhunter=bool(
            raw.get("proxyJob") or raw.get("goldHunter") or raw.get("proxyType")
        ),
        job_type=int(raw.get("jobType") or 0),
        keyword=keyword,
    )


def parse_api_payload(data: dict, keyword: str) -> tuple[list[Job], bool]:
    """返回 (这一页的岗位, 还有没有下一页)。"""
    zp = data.get("zpData") or {}
    jobs = [j for j in (parse_api_job(r, keyword) for r in zp.get("jobList") or []) if j]
    return jobs, bool(zp.get("hasMore"))


def _capture_joblist(page: Page, action: Callable[[], object]) -> dict | None:
    """执行 action，并接住它触发的那次列表接口响应。没等到返回 None。"""
    try:
        with page.expect_response(
            lambda r: JOBLIST_API in r.url, timeout=_API_TIMEOUT_MS
        ) as info:
            action()
        return info.value.json()
    except (PWTimeout, ValueError):
        return None


# ---------------------------------------------------------------- DOM 兜底


def parse_card(card: Locator, sel: Selectors, keyword: str) -> Job | None:
    """把一张职位卡片解析成 Job。关键字段缺失则返回 None（多半是选择器失效）。

    只在接口抓不到时才走这条路。注意薪资在页面上是密文，这里拿到的
    `salary_text` 多半解析不出数字，薪资过滤会据此拒绝——宁可漏发不可错发。
    """
    href = attr_of(card, sel.get("list.link"), "href")
    title = text_of(card, sel.get("list.title"))
    if not href or not title:
        return None

    tags = texts_of(card, sel.get("list.tags"))
    experience = next((t for t in tags if any(w in t for w in _EXP_WORDS)), "")
    education = next((t for t in tags if any(w in t for w in _EDU_WORDS)), "")

    return Job(
        job_id=extract_job_id(href),
        title=title,
        url=_absolute(href),
        company=text_of(card, sel.get("list.company")),
        salary_text=text_of(card, sel.get("list.salary")),
        area_text=text_of(card, sel.get("list.area")),
        experience=experience,
        education=education,
        # 卡片上已经没有公司规模/融资阶段了，只有接口里有——
        # 走到这条兜底路径时这两项必然为空，company_size 过滤会据此拒绝。
        activity=text_of(card, sel.get("list.activity")),
        keyword=keyword,
    )


def scrape_page(page: Page, sel: Selectors, keyword: str) -> list[Job]:
    """抓当前列表页的所有卡片。"""
    card_sel = first_selector_that_matches(page, sel.get("list.card"))
    if card_sel is None:
        return []

    human_scroll(page, times=3)  # 触发懒加载
    cards = page.locator(card_sel)
    jobs: list[Job] = []
    for i in range(cards.count()):
        try:
            job = parse_card(cards.nth(i), sel, keyword)
        except Exception:
            continue
        if job:
            jobs.append(job)
    return jobs


def first_selector_that_matches(page: Page, candidates: list[str]) -> str | None:
    """返回第一个能命中元素的选择器字符串本身（而非 Locator）。"""
    for s in candidates:
        try:
            page.wait_for_selector(s, timeout=5000, state="attached")
            if page.locator(s).count() > 0:
                return s
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- 逐页产出


def iter_jobs(page: Page, sel: Selectors, keyword: str, city: str, max_pages: int):
    """逐页产出岗位。同一 job_id 重复出现时跳过。

    第一页由导航触发，后续页靠往下滚触发——BOSS 的列表是无限滚动，没有翻页链接。
    """
    seen: set[str] = set()

    data = _capture_joblist(
        page, lambda: page.goto(search_url(keyword, city, 1), wait_until="domcontentloaded")
    )
    if data is None:
        # 接口没抓到（改版/被拦），退回解析 DOM
        yield from _iter_jobs_dom(page, sel, keyword, city, max_pages, seen)
        return

    for _ in range(max_pages):
        jobs, has_more = parse_api_payload(data, keyword)
        fresh = [j for j in jobs if j.job_id not in seen]
        seen.update(j.job_id for j in fresh)
        yield from fresh

        if not has_more:
            break
        data = _capture_joblist(page, lambda: human_scroll(page, times=3))
        if data is None:
            break  # 滚到底了，或者接口不再响应


def _iter_jobs_dom(page: Page, sel: Selectors, keyword: str, city: str,
                   max_pages: int, seen: set[str]):
    """接口路径失效时的兜底：老老实实按 page= 翻页解析卡片。"""
    for page_no in range(1, max_pages + 1):
        if page_no > 1:
            page.goto(search_url(keyword, city, page_no), wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        jobs = scrape_page(page, sel, keyword)
        if not jobs:
            break  # 没有更多结果，或选择器失效
        fresh = [j for j in jobs if j.job_id not in seen]
        seen.update(j.job_id for j in fresh)
        if not fresh:
            break  # 翻页没翻动，避免空转
        yield from fresh


def fetch_jd(page: Page, sel: Selectors, job: Job) -> str:
    """打开详情页并提取 JD 正文。页面停在详情页，供后续点「立即沟通」复用。"""
    page.goto(job.url, wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    parts = texts_of(page, sel.get("detail.jd"))
    return "\n".join(parts).strip()
