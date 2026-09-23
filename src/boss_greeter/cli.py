"""命令行入口：login / doctor / dump / run / stats。"""

from __future__ import annotations

import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from patchright.sync_api import Error as PlaywrightError
from rich.console import Console
from rich.table import Table

from .browser import (
    BOSS_HOME, CHAT_URL, LOGIN_URL, AttachFailed, attr_of, body_text, browser_context,
    chrome_launch_cmd, first_match, is_logged_in, page_of,
)
from .config import (
    DB_PATH, LOCAL_CONFIG, LOG_DIR, PROJECT_ROOT, config_path, load_config, load_selectors,
)
from .scraper import search_url
from .store import Store

app = typer.Typer(add_completion=False, help="BOSS 直聘自动打招呼")
console = Console()


# 每家 SDK 的凭据变量各算「一组」。
# Anthropic：按 ANTHROPIC_API_KEY → ANTHROPIC_AUTH_TOKEN 的顺序取凭据，
#            请求地址取 ANTHROPIC_BASE_URL（不设就是官方 api.anthropic.com）。
# OpenAI：  OPENAI_API_KEY + 可选的 OPENAI_BASE_URL（指向兼容网关）。
ENV_GROUPS = {
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"),
    "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
}


def _dotenv_keys() -> set[str]:
    """.env 里显式声明了哪些变量（不含被注释掉的行）。"""
    path = PROJECT_ROOT / ".env"
    if not path.exists():
        return set()
    keys = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.add(line.split("=", 1)[0].strip())
    return keys


def _apply_env_file() -> None:
    """让 .env 成为本工具模型凭据的唯一来源。

    要点是同一家的几个变量必须**整组**来自同一处，不能和 shell 环境各取一半：
    比如 .env 配了自建网关的 ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL，
    而 shell 里恰好有个 ANTHROPIC_API_KEY，SDK 会优先用那个 key 去打你的网关，
    认证直接失败。所以 .env 一旦声明了某组里的任意一个，同组没声明的就从环境里清掉。
    分组之间互不影响——只配了 OPENAI_* 不会动到继承来的 ANTHROPIC_*。

    典型的踩坑场景：从 Claude Code 之类的会话里启动本工具，环境里带着
    只认特定客户端的代理凭据（调用会返回 503）。
    """
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    declared = _dotenv_keys()
    for keys in ENV_GROUPS.values():
        if declared & set(keys):
            for key in keys:
                if key not in declared:
                    os.environ.pop(key, None)


_apply_env_file()


@contextmanager
def _browser(cfg):
    """所有命令统一从这里拿浏览器：接管不上时给出启动命令，而不是甩 traceback。"""
    try:
        with browser_context(cfg.browser) as ctx:
            yield ctx
    except AttachFailed as e:
        console.print("[bold red]无法接管 Chrome[/]")
        console.print(str(e))
        raise typer.Exit(1) from None


def _load():
    try:
        cfg = load_config()
    except Exception as e:
        console.print(f"[bold red]配置加载失败（{config_path().name}）：[/]{e}")
        raise typer.Exit(1)
    # 明说用的是哪份配置——两份并存时最容易「改了没生效」
    if config_path().name == LOCAL_CONFIG:
        console.print(f"[dim]配置：{LOCAL_CONFIG}[/]")
    try:
        return cfg, load_selectors()
    except Exception as e:
        console.print(f"[bold red]选择器加载失败：[/]{e}")
        raise typer.Exit(1)


@app.command()
def chrome(print_only: bool = typer.Option(False, "--print-only", help="只打印命令，不实际启动")):
    """启动一个开着调试端口的 Chrome，供接管模式使用。"""
    cfg, _ = _load()
    b = cfg.browser
    if not b.attach_cdp:
        console.print(
            "[yellow]当前不是接管模式[/]（config.yaml 的 browser.attach_cdp 是空的），"
            "这个命令没有意义。"
        )
        raise typer.Exit(1)

    console.print("启动命令：\n" + chrome_launch_cmd(b) + "\n", highlight=False)
    if print_only:
        return
    if not Path(b.chrome_path).exists():
        console.print(
            f"[bold red]找不到 Chrome：{b.chrome_path}[/]\n"
            "改 config.yaml 里的 browser.chrome_path 指向真实路径。"
        )
        raise typer.Exit(1)

    b.user_data_dir.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [b.chrome_path, f"--remote-debugging-port={b.debug_port}",
         f"--user-data-dir={b.user_data_dir}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    console.print(
        "[green]✓ Chrome 已启动[/]　用的是专用用户目录，所以第一次打开是全新的浏览器。\n"
        "请在里面[bold]手动[/]登录 BOSS（扫码或账号密码，本工具全程不碰登录页），\n"
        "登录完成后运行 [bold]uv run boss login[/] 确认，之后 doctor / run 都会接管这个窗口。"
    )


@app.command()
def login(timeout: int = typer.Option(300, help="等待登录完成的秒数")):
    """确认浏览器已登录 BOSS。

    接管模式（browser.attach_cdp 有值）下登录由你手动完成，本命令只负责确认；
    自启模式下才走扫码流程，登录态保存在 data/profile/。
    """
    cfg, sel = _load()
    cfg.browser.headless = False  # 登录必须有界面

    if cfg.browser.attach_cdp:
        _login_attached(cfg, sel, timeout)
        return

    with _browser(cfg) as ctx:
        page = page_of(ctx, attach=bool(cfg.browser.attach_cdp))
        page.goto(BOSS_HOME, wait_until="domcontentloaded")

        if is_logged_in(page, sel):
            console.print("[green]✓ 已是登录状态，无需重复登录[/]")
            return

        # 首页上没有二维码，得显式跳到登录页——否则界面上根本没有码可扫
        page.goto(LOGIN_URL, wait_until="domcontentloaded")

        if len(body_text(page)) < 50:
            console.print(
                f"[bold red]登录页是空白的[/]　当前地址：[dim]{page.url}[/]\n"
                "可能是网络不通或被反爬拦了。手动在这个窗口里刷新试试，或稍后重跑。"
            )
            raise typer.Exit(code=1)

        if first_match(page, sel.get("login.qr"), timeout=8000) is None:
            console.print(
                f"[bold red]登录页没渲染出二维码[/]　当前地址：[dim]{page.url}[/]\n"
                "多半是 BOSS 改版了，selectors.yaml 的 login.qr 候选需要更新。\n"
                "可以先用 [bold]uv run boss dump " + LOGIN_URL + "[/] 存一份 HTML 对照。"
            )
            raise typer.Exit(code=1)

        console.print("[yellow]二维码已出现，请用 BOSS 直聘 App 扫码…[/]（登录成功后本命令自动退出）")
        deadline = time.time() + timeout
        next_tick = time.time() + 30
        try:
            while time.time() < deadline:
                if is_logged_in(page, sel):
                    page.wait_for_timeout(2000)  # 等 cookie 落盘
                    console.print("[bold green]✓ 登录成功，登录态已保存[/]")
                    return
                if time.time() >= next_tick:
                    next_tick += 30
                    left = int(deadline - time.time())
                    # 码大约两分钟过期，过期了让用户自己点刷新——自动点容易和真人操作打架
                    expired = first_match(page, sel.get("login.qr_expired"), timeout=500)
                    tip = "　[red]二维码已失效，点一下页面上的刷新[/]" if expired else ""
                    console.print(f"[dim]等待扫码…剩余 {left}s[/]{tip}")
                page.wait_for_timeout(2000)
        except PlaywrightError as e:
            # 扫码途中直接关窗口是很自然的操作，不该甩一屏 traceback
            if "closed" not in str(e).lower():
                raise
            console.print(
                "[yellow]浏览器窗口被关闭，登录未完成。[/]\n"
                "重新运行 [bold]uv run boss login[/] 再扫一次即可。"
            )
            raise typer.Exit(1) from None

        console.print("[red]✗ 等待超时，未检测到登录[/]")
        raise typer.Exit(1)


def _login_attached(cfg, sel, timeout: int) -> None:
    """接管模式：不驱动任何登录动作，只盯着用户自己登完没有。"""
    with _browser(cfg) as ctx:
        page = page_of(ctx, attach=bool(cfg.browser.attach_cdp))
        if "zhipin.com" not in (page.url or ""):
            page.goto(BOSS_HOME, wait_until="domcontentloaded")

        if is_logged_in(page, sel):
            console.print("[green]✓ 已是登录状态，可以直接跑 doctor / run[/]")
            return

        console.print(
            "[yellow]这个 Chrome 还没登录 BOSS。[/]\n"
            "请在刚打开的那个标签页里自己点「登录」并完成扫码——全程手动，本工具不碰登录页。\n"
            "登录成功后本命令会自动确认并退出。"
        )
        deadline = time.time() + timeout
        next_tick = time.time() + 30
        try:
            while time.time() < deadline:
                if is_logged_in(page, sel):
                    console.print("[bold green]✓ 登录成功[/]")
                    return
                if time.time() >= next_tick:
                    next_tick += 30
                    console.print(f"[dim]等待手动登录…剩余 {int(deadline - time.time())}s[/]")
                page.wait_for_timeout(2000)
        except PlaywrightError as e:
            # 用户把标签页关了是很自然的操作，不该甩一屏 traceback
            if "closed" not in str(e).lower():
                raise
            console.print(
                "[yellow]标签页被关闭，登录未确认。[/]\n"
                "登录完成后重新运行 [bold]uv run boss login[/] 即可。"
            )
            raise typer.Exit(1) from None

        console.print("[red]✗ 等待超时，仍未检测到登录态[/]")
        raise typer.Exit(1)


@app.command()
def doctor(
    skip_detail: bool = typer.Option(False, "--skip-detail", help="只查列表页，不打开详情页和聊天页"),
):
    """选择器自检：逐个验证 selectors.yaml 能否命中真实页面元素。

    会依次走三个页面——列表页、随便一个岗位的详情页、聊天页，因为 detail.* 和 chat.*
    在列表页上本就不存在，只在列表页自检的话它们永远是一片红，等于没检。
    全程只读：详情页不点「立即沟通」，聊天页不发任何消息。
    """
    cfg, sel = _load()

    with _browser(cfg) as ctx:
        page = page_of(ctx, attach=bool(cfg.browser.attach_cdp))
        url = search_url(cfg.search.keywords[0], cfg.search.city, 1)
        console.print(f"打开列表页：[dim]{url}[/]")
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        # 未登录时 /web/geek/jobs 会被重定向到登录页，此时所有选择器都查不到，
        # 会误报成「全面改版失效」。所以先判登录，没登录就别往下跑了。
        if not is_logged_in(page, sel):
            console.print(
                f"[bold red]未登录[/]　当前页面：[dim]{page.url}[/]\n"
                "列表页需要登录态才能渲染，未登录时自检结果全是假阴性。\n"
                "请先运行：[bold]uv run boss login[/] 确认登录态，再回来跑 doctor。"
            )
            raise typer.Exit(code=1)

        console.print("登录态：[green]已登录[/]")

        rows: list[tuple[str, str, str]] = []
        # 卡片内部的字段要在卡片作用域里查，页面级的直接在 page 上查
        card_scope = None
        for candidate in sel.get("list.card"):
            if page.locator(candidate).count() > 0:
                card_scope = page.locator(candidate).first
                break

        if card_scope is None:
            console.print(
                "[yellow]⚠ list.card 的候选选择器一个都没命中——"
                "列表页结构可能变了，下面 list.* 的结果会退化成全页面查找。[/]"
            )

        def check(path: str, scope) -> None:
            candidates = sel.get(path)
            hit = next((c for c in candidates if _count(scope, c) > 0), None)
            if not hit:
                rows.append((path, "[red]❌ 全部失效[/]", "—"))
            elif hit != candidates[0]:
                # 首选失效，建议把 hit 提到第一位
                rows.append((path, "[yellow]⚠ 降级[/]", hit))
            else:
                rows.append((path, "[green]✅[/]", hit))

        check("login.logged_in", page)
        # login.qr / login_page / qr_expired 只在未登录的登录页上存在，
        # 已登录状态下查不到是理所当然的，报红只会干扰判断
        for path in ("login.login_page", "login.qr", "login.qr_expired"):
            rows.append((path, "[dim]— 跳过[/]", "仅未登录时存在"))

        for path, _ in sel.iter_all():
            if path.startswith("list.") and path != "list.card":
                check(path, card_scope if card_scope is not None else page)
            elif path == "list.card":
                check(path, page)

        if skip_detail:
            for path, _ in sel.iter_all():
                if path.startswith(("detail.", "chat.")):
                    rows.append((path, "[dim]— 跳过[/]", "--skip-detail"))
        else:
            _check_detail_and_chat(page, sel, card_scope, check, rows)

        table = Table("字段", "状态", "生效的选择器", title="选择器自检")
        for r in rows:
            table.add_row(*r)
        console.print(table)
        console.print(
            "\n[dim]❌ 的字段需要在 selectors.yaml 里补新选择器；"
            "⚠ 的建议把生效的那条挪到候选列表首位。[/]"
        )


def _check_detail_and_chat(page, sel, card_scope, check, rows) -> None:
    """打开一个真实详情页和聊天页，把 detail.* / chat.* 也检了。只读，不点沟通、不发消息。"""
    href = ""
    if card_scope is not None:
        href = attr_of(card_scope, sel.get("list.link"), "href")

    if not href:
        for path, _ in sel.iter_all():
            if path.startswith("detail."):
                rows.append((path, "[yellow]— 未检[/]", "列表页取不到岗位链接"))
    else:
        detail_url = href if href.startswith("http") else BOSS_HOME + href
        console.print(f"打开详情页：[dim]{detail_url}[/]")
        page.goto(detail_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        for path, _ in sel.iter_all():
            if path.startswith("detail."):
                check(path, page)

    console.print(f"打开聊天页：[dim]{CHAT_URL}[/]")
    page.goto(CHAT_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    # 输入框和发送按钮要选中一个会话才渲染出来；没有任何会话时只能跳过
    convs = page.locator(".user-list li, li.user-item")
    if convs.count():
        convs.first.click()
        page.wait_for_timeout(2500)
    # 这几个只在点完「立即沟通」弹出的确认框里存在，平时页面上没有，
    # 报红只会让人以为选择器挂了（doctor 是只读的，不会去点沟通把弹窗造出来）
    dialog_only = ("chat.stay_button", "chat.continue_button", "chat.already_chatted")
    for path, _ in sel.iter_all():
        if path.startswith("chat."):
            if path in dialog_only:
                rows.append((path, "[dim]— 跳过[/]", "仅发送确认框弹出时存在"))
                continue
            if path == "chat.sent_bubble" and not convs.count():
                rows.append((path, "[yellow]— 未检[/]", "还没有任何会话"))
                continue
            check(path, page)


def _count(scope, selector: str) -> int:
    try:
        return scope.locator(selector).count()
    except Exception:
        return 0


@app.command()
def dump(url: Optional[str] = typer.Argument(None, help="要抓的页面，默认用配置里的第一个搜索词")):
    """把页面 HTML 存到 data/logs/，用于校准选择器。"""
    cfg, sel = _load()
    target = url or search_url(cfg.search.keywords[0], cfg.search.city, 1)

    with _browser(cfg) as ctx:
        page = page_of(ctx, attach=bool(cfg.browser.attach_cdp))
        page.goto(target, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        out = LOG_DIR / f"dump-{int(time.time())}.html"
        out.write_text(page.content(), encoding="utf-8")
        shot = out.with_suffix(".png")
        page.screenshot(path=str(shot), full_page=True)

    console.print(f"[green]✓[/] HTML → {out}\n[green]✓[/] 截图 → {shot}")


@app.command()
def run(
    dry_run: bool = typer.Option(False, "--dry-run", help="只生成招呼语不发送"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="本次最多处理多少个岗位"),
    allow_fallback: bool = typer.Option(
        False, "--allow-fallback", help="模型不可用时也继续跑，全部改用固定模板招呼语"
    ),
    default_greeting: bool = typer.Option(
        False, "--default-greeting", help="不生成招呼语，只点沟通、用 BOSS 自带的默认招呼语"
    ),
    continuous: bool = typer.Option(
        False, "--continuous", "-c",
        help="一轮跑完不退出，休息一阵接着跑，直到用完每日上限、候选耗尽或撞上风控",
    ),
):
    """跑一轮：筛岗位 → 生成招呼语 → 打招呼。加 -c 则连续跑。"""
    from .runner import run as do_run  # 延迟导入，避免 stats/doctor 也去连 API

    cfg, sel = _load()
    if default_greeting and allow_fallback:
        console.print(
            "[yellow]--default-greeting already 不走模型，--allow-fallback 无意义，已忽略[/]"
        )
        allow_fallback = False
    if default_greeting:
        console.print("[dim]招呼语：平台默认（不调用模型）[/]")
    if continuous and dry_run:
        console.print(
            "[bold red]--continuous 不能跟 --dry-run 一起用。[/]\n"
            "  预演不写去重记录（只有真正发出去的才算打过招呼），"
            "所以下一轮会把同一批岗位重新跑一遍，无限循环。"
        )
        raise typer.Exit(1)
    if continuous and limit is not None:
        console.print(
            "[bold red]--continuous 不能跟 -n 一起用。[/]\n"
            "  -n 是「本次最多处理多少个」，连续模式下每轮都会重新计数，"
            "起不到限制总量的作用。要限总量请改 config 的 pacing.daily_limit。"
        )
        raise typer.Exit(1)
    if continuous:
        console.print(
            f"[dim]连续模式：跑到用完每日上限 {cfg.pacing.daily_limit} 条为止。"
            f"小时上限 {cfg.pacing.hourly_limit or '未设'} 条，"
            f"轮间休息 {cfg.pacing.round_rest[0] / 60:.0f}-{cfg.pacing.round_rest[1] / 60:.0f} 分钟。"
            f"　Ctrl+C 可随时中断[/]"
        )
    stats = do_run(
        cfg, sel, dry_run=dry_run, limit=limit,
        allow_fallback=allow_fallback, default_greeting=default_greeting,
        continuous=continuous,
    )

    console.rule("[bold]本轮结果[/]")
    table = Table("指标", "数量")
    table.add_row("扫描岗位", str(stats.scanned))
    table.add_row("被过滤", str(stats.rejected))
    table.add_row("已发送", f"[green]{stats.sent}[/]")
    table.add_row("发送失败", f"[red]{stats.failed}[/]" if stats.failed else "0")
    if stats.dry_run:
        table.add_row("预演生成", str(stats.dry_run))
    if stats.skipped:
        table.add_row("降级跳过", f"[yellow]{stats.skipped}[/]")
    if stats.rounds > 1:
        table.add_row("跑了几轮", str(stats.rounds))
    table.add_row("结束原因", stats.stop_reason)
    console.print(table)

    if stats.reject_by_rule:
        rt = Table("拦截规则", "数量", title="过滤明细")
        for rule, n in sorted(stats.reject_by_rule.items(), key=lambda x: -x[1]):
            rt.add_row(rule, str(n))
        console.print(rt)


@app.command()
def stats(days: int = typer.Option(7, help="统计最近多少天")):
    """查看打招呼统计与最近发出的消息。"""
    if not DB_PATH.exists():
        console.print("[yellow]还没有任何记录，先跑一次 boss run[/]")
        return

    with Store(DB_PATH) as store:
        console.print(f"[bold]今日已发送：[/]{store.sent_today()} 条\n")

        t = Table("状态", "数量", title=f"最近 {days} 天招呼语")
        for row in store.greeting_counts(days):
            t.add_row(row["status"], str(row["n"]))
        console.print(t)

        rt = Table("拦截规则", "数量", title=f"最近 {days} 天过滤明细")
        for row in store.rejection_counts(days):
            rt.add_row(row["rule"], str(row["n"]))
        console.print(rt)

        gt = Table("时间", "状态", "岗位", "招呼语", title="最近 10 条")
        for row in store.recent_greetings(10):
            msg = (row["message"] or "")[:40] + "…"
            gt.add_row(row["sent_at"][5:16], row["status"],
                       f"{row['title'] or '?'} @ {row['company'] or '?'}", msg)
        console.print(gt)


if __name__ == "__main__":
    app()
