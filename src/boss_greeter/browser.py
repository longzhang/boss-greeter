"""Playwright 浏览器管理：持久化登录态、反自动化检测、容错的选择器查询。"""

from __future__ import annotations

import json
import random
from contextlib import contextmanager
from urllib.error import URLError
from urllib.request import urlopen

# 用 patchright 而不是 playwright：两者 API 完全一致，但 playwright 的 CDP 会话
# 带着 Runtime.enable 这类可被页面脚本探到的痕迹——实测 BOSS 的页面脚本一旦探到，
# 会在一秒内把整个标签页关掉（不管浏览器是不是用户自己开的）。patchright 修掉了这些泄漏。
from patchright.sync_api import (
    BrowserContext, Error as PlaywrightError, Locator, Page,
    TimeoutError as PWTimeout, sync_playwright,
)

from .config import PROFILE_DIR, BrowserConfig, Selectors

BOSS_HOME = "https://www.zhipin.com"
JOB_SEARCH_URL = BOSS_HOME + "/web/geek/jobs"
CHAT_URL = BOSS_HOME + "/web/geek/chat"
# 首页上没有二维码，扫码得去这个地址
LOGIN_URL = "https://login.zhipin.com/?ka=header-login"

# 真机 UA。跟随本地 Chrome 大版本即可，不必精确。
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

def chrome_launch_cmd(cfg: BrowserConfig) -> str:
    """接管模式下，用户该用哪条命令启动 Chrome。报错和 `boss chrome` 都念这一份。"""
    return (
        f'"{cfg.chrome_path}" \\\n'
        f"  --remote-debugging-port={cfg.debug_port} \\\n"
        f'  --user-data-dir="{cfg.user_data_dir}"'
    )


class AttachFailed(RuntimeError):
    """连不上那个开着调试端口的 Chrome。消息里带着完整的启动命令。"""


# webdriver 标志是最容易被检测的一处，页面脚本跑之前先抹掉。
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""


@contextmanager
def browser_context(cfg: BrowserConfig):
    """拿到一个可用的浏览器上下文。

    两种模式：
    - `browser.attach_cdp` 有值 → 连到你自己开着的 Chrome（推荐）。登录是你手动做的，
      浏览器带着真实历史与指纹，BOSS 的反自动化基本无从下手。
    - 留空 → 由本工具起一个持久化上下文，登录态存在 data/profile/。
    """
    with sync_playwright() as pw:
        if cfg.attach_cdp:
            yield from _attached(pw, cfg)
            return
        yield from _launched(pw, cfg)


def require_window(cdp_url: str) -> None:
    """接管前确认浏览器里确实有窗口。

    macOS 上把窗口全关掉，Chrome 进程还活着、调试端口也还通，但 CDP 里一个 page 目标
    都没有——connect_over_cdp 这时报的是「Browser context management is not supported」，
    光看这句话根本想不到是「没开窗口」。这里提前拦下，给条能照做的提示。

    （试过用 /json/new 自动补一个标签页：能连上，但那个标签页不属于任何窗口，
    导航完页面会回到 about:blank，反而更难查。还是让用户开个真窗口靠谱。）
    """
    base = cdp_url.rstrip("/")
    try:
        with urlopen(f"{base}/json/list", timeout=5) as r:
            targets = json.load(r)
    except (URLError, OSError, ValueError):
        return  # 端口不通之类的，交给 connect_over_cdp 去报更准确的错
    if not any(t.get("type") == "page" for t in targets):
        raise AttachFailed(
            f"{cdp_url} 上的 Chrome 还活着，但一个窗口都没有（窗口关了进程还在）。\n"
            "运行 uv run boss chrome 让它开一个新窗口，再重试。"
        )


def _attached(pw, cfg: BrowserConfig):
    require_window(cfg.attach_cdp)
    try:
        browser = pw.chromium.connect_over_cdp(cfg.attach_cdp, timeout=10000)
    except PlaywrightError as e:
        raise AttachFailed(
            f"连不上 {cfg.attach_cdp}：{str(e).splitlines()[0]}\n"
            "请先用下面这条命令启动 Chrome（一次启动，之后一直接管这一个）：\n\n"
            f"{chrome_launch_cmd(cfg)}\n\n"
            "或者直接运行 uv run boss chrome 由本工具代劳。"
        ) from None

    # 接管的是用户自己的浏览器，默认上下文里可能开着一堆无关标签，全程不动它们
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    ctx.set_default_timeout(cfg.nav_timeout_ms)
    try:
        yield ctx
    finally:
        # 不调用 close()：浏览器是用户开的，本工具退出时只断开连接，不该关人家窗口
        pass


def _launched(pw, cfg: BrowserConfig):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        # 自带 Chromium 会被 BOSS 登录页当场识破（页面 2 秒内自己刷成 about:blank），
        # 换本机真实 Chrome 才能正常渲染二维码。
        **({"channel": cfg.channel} if cfg.channel else {}),
        headless=cfg.headless,
        slow_mo=cfg.slow_mo_ms,
        viewport={"width": cfg.viewport[0], "height": cfg.viewport[1]},
        # 真实 Chrome 自带的 UA 就是真的，硬改反而制造矛盾特征；
        # 只有用自带 Chromium 时才需要伪装成 Chrome。
        **({} if cfg.channel else {"user_agent": USER_AGENT}),
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    )
    ctx.add_init_script(STEALTH_JS)
    ctx.set_default_timeout(cfg.nav_timeout_ms)
    try:
        yield ctx
    finally:
        try:
            ctx.close()
        except PlaywrightError:
            pass  # 用户已经把窗口关了，没什么可关的


def page_of(ctx: BrowserContext, attach: bool = False) -> Page:
    """挑一个标签页来干活。

    优先复用已经停在 BOSS 上的标签页。接管模式下浏览器是用户自己的，里面多半开着
    一堆无关标签，所以没有 BOSS 标签时宁可新开一个，也不去抢占人家正在看的页面；
    自启模式下上下文自带一个空白页，直接用它，免得每次多开一个标签。
    """
    pages = [p for p in ctx.pages if not p.is_closed()]
    for p in pages:
        if "zhipin.com" in (p.url or ""):
            return p
    if not attach:
        for p in pages:
            if (p.url or "about:blank") in ("about:blank", ""):
                return p
    return ctx.new_page()


# ---------------------------------------------------------------- 容错查询


def first_match(scope: Page | Locator, candidates: list[str], timeout: float = 3000) -> Locator | None:
    """按顺序尝试候选选择器，返回第一个真实命中的 Locator。

    BOSS 改版频繁，单一选择器很容易失效，所以每个字段都配了多个候选。
    """
    for sel in candidates:
        try:
            loc = scope.locator(sel).first
            loc.wait_for(state="attached", timeout=timeout)
            return loc
        except PWTimeout:
            continue
        except Exception:
            continue
    return None


def text_of(scope: Page | Locator, candidates: list[str], default: str = "") -> str:
    loc = first_match(scope, candidates, timeout=800)
    if loc is None:
        return default
    try:
        return (loc.inner_text() or "").strip()
    except Exception:
        return default


def texts_of(scope: Page | Locator, candidates: list[str]) -> list[str]:
    """取多个同类元素的文本，如标签列表。"""
    for sel in candidates:
        try:
            items = scope.locator(sel)
            if items.count() > 0:
                return [t.strip() for t in items.all_inner_texts() if t.strip()]
        except Exception:
            continue
    return []


def attr_of(scope: Page | Locator, candidates: list[str], name: str, default: str = "") -> str:
    loc = first_match(scope, candidates, timeout=800)
    if loc is None:
        return default
    try:
        return loc.get_attribute(name) or default
    except Exception:
        return default


# ---------------------------------------------------------------- 登录与风控


def is_logged_in(page: Page, sel: Selectors) -> bool:
    return first_match(page, sel.get("login.logged_in"), timeout=2500) is not None


def body_text(page: Page) -> str:
    """页面正文。取不到就当空——调用方据此判断是不是白板页。"""
    try:
        return page.inner_text("body", timeout=3000)
    except Exception:
        return ""


def detect_risk(page: Page, sel: Selectors) -> str | None:
    """扫一眼页面上有没有风控文案。命中则返回那句话，调用方应据此熔断。"""
    try:
        body = page.inner_text("body", timeout=2000)
    except Exception:
        return None
    for phrase in sel.risk_texts:
        if phrase in body:
            return phrase
    return None


def human_type(loc: Locator, text: str) -> None:
    """逐字符输入，字间随机停顿。整段 fill 的时序特征太机械。"""
    for ch in text:
        loc.type(ch, delay=random.uniform(30, 110))


def human_scroll(page: Page, times: int = 3, max_steps: int = 60) -> None:
    """往下滚到接近页底，触发列表页懒加载。

    这里必须滚「到底」而不是固定滚几次：列表每加载一页就长高约 2400px，而
    固定 3 次滚轮只覆盖 1200-2700px。第 1→2 页刚好够（距底 1651px），
    第 2→3 页就差一点点（距底 2384px，滚完还剩 241px），懒加载不触发，
    翻页于是静默断在第 2 页——接口明说 hasMore=True 却再也拿不到数据。
    结果是每个关键词只能抓到 30 个，max_pages 配 5 还是 10 都没区别。

    `times` 保留为「至少滚几次」，即使已经到底也会滚这么多下，免得页面
    没长高时一步都不动。max_steps 是防死循环的上限。
    """
    def left() -> float:
        try:
            return page.evaluate(
                "() => document.body.scrollHeight - window.scrollY - window.innerHeight"
            )
        except Exception:
            return 0.0

    for i in range(max_steps):
        page.mouse.wheel(0, random.randint(400, 900))
        page.wait_for_timeout(random.randint(150, 400))
        if i + 1 >= times and left() <= 80:
            break
