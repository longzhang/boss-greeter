"""主流程编排：搜索 → 初筛 → 取 JD → 复筛 → 生成招呼语 → 发送。"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from patchright.sync_api import Error as PWError
from rich.console import Console

from .browser import browser_context, is_logged_in, page_of
from .config import DB_PATH, Config, Selectors
from .filters import build_card_chain, build_jd_chain, run_chain
from .greeter import Greeter
from .models import FilterResult, Greeting, Job
from .pacing import Circuit, Pacer, QuotaReached
from .scraper import fetch_jd, iter_jobs
from .screener import Screener
from .sender import send_default_greeting, send_greeting
from .store import Store

console = Console()


def _brief(exc: Exception) -> str:
    """异常信息取第一行——playwright 的报错常带一整屏调用栈。"""
    return str(exc).strip().splitlines()[0][:120]


class NotLoggedIn(Exception):
    pass


class ModelUnavailable(RuntimeError):
    """招呼语模型不可用。继续跑只会把配额发成通用模板，所以直接停。"""


#: 连续多少个岗位没能生成出招呼语就停。开跑前的 preflight 只能证明那一刻模型是通的，
#: 网关中途挂掉照样会让后面每个岗位都降级——与其磨完整张岗位表，不如早点停下来。
_MAX_MODEL_FAILURES = 3


@dataclass
class RunStats:
    scanned: int = 0
    rejected: int = 0
    sent: int = 0
    failed: int = 0
    dry_run: int = 0
    #: 招呼语降级成模板、因而没有发出去的岗位数
    skipped: int = 0
    stop_reason: str = "正常结束"
    #: 跑了几轮。非连续模式恒为 1。
    rounds: int = 1
    reject_by_rule: dict[str, int] = field(default_factory=dict)


def run(
    cfg: Config,
    sel: Selectors,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    allow_fallback: bool = False,
    default_greeting: bool = False,
    continuous: bool = False,
) -> RunStats:
    stats = RunStats()

    with Store(DB_PATH) as store:
        pacer = Pacer(cfg.pacing, sent_today=store.sent_today())
        run_id = store.start_run()
        card_chain = build_card_chain(cfg, store.is_greeted)
        jd_chain = build_jd_chain(cfg)

        mode = "[yellow]预演（不会真的发送）[/]" if dry_run else "[red]真实发送[/]"
        console.print(f"模式：{mode}　今日已发 {pacer.sent_today}/{cfg.pacing.daily_limit}")

        # 用平台默认招呼语时整条模型链路都用不上，连客户端都不建——
        # 没配 / 配错凭据也应该能照跑。
        greeter = None if default_greeting else Greeter(cfg.greeting, cfg.resume_text)
        screener = Screener(cfg.ai_screen.provider, cfg.ai_screen.model) \
            if cfg.ai_screen.enabled else None

        try:
            # AI 判定也先探活。它跟招呼语可能用不同的模型/网关，各探各的。
            if screener is not None and (err := screener.preflight()):
                raise ModelUnavailable(
                    f"AI 判定模型 {cfg.ai_screen.model} 不可用：{err}\n"
                    "  这层挡外包/销售岗，不通就等于没开。"
                    "确实不想用就把 config.yaml 的 ai_screen.enabled 置 false。"
                )
            # 先花一次极小的调用确认模型可用，再开浏览器——不可用就没必要抓了
            if greeter is not None and (err := greeter.preflight()):
                msg = f"招呼语模型 {cfg.greeting.model} 不可用：{err}"
                if not allow_fallback:
                    raise ModelUnavailable(msg)
                console.print(
                    f"[yellow]⚠ {msg}\n"
                    "  已指定 --allow-fallback，本轮全部岗位使用固定模板招呼语。[/]"
                )

            with browser_context(cfg.browser) as ctx:
                page = page_of(ctx, attach=bool(cfg.browser.attach_cdp))
                page.goto("https://www.zhipin.com/web/geek/job", wait_until="domcontentloaded")
                if not is_logged_in(page, sel):
                    raise NotLoggedIn("未检测到登录态，请先运行：uv run boss login 确认")

                empty_rounds = 0
                while True:
                    if continuous:
                        console.rule(
                            f"[bold]第 {stats.rounds} 轮[/]　"
                            f"今日已发 {pacer.sent_today}/{cfg.pacing.daily_limit}"
                        )
                    sent_before = stats.sent
                    _loop(ctx, page, cfg, sel, store, pacer, greeter,
                          card_chain, jd_chain, stats, run_id, dry_run, limit, allow_fallback,
                          default_greeting, screener)
                    if not continuous:
                        break

                    gained = stats.sent - sent_before
                    if pacer.remaining <= 0:
                        stats.stop_reason = f"已用完每日上限 {cfg.pacing.daily_limit} 条，收工"
                        break
                    # 一轮把所有关键词都翻完了还是零，说明候选耗尽——再跑也是
                    # 重扫同一批岗位，全被去重挡掉。等几轮新岗位没出来就收工。
                    if gained == 0:
                        empty_rounds += 1
                        console.print(
                            f"[dim]  本轮没有新岗位可投（连续 {empty_rounds} 轮）[/]"
                        )
                        if empty_rounds >= cfg.pacing.max_empty_rounds:
                            stats.stop_reason = (
                                f"连续 {empty_rounds} 轮没有新岗位可投，收工（"
                                f"今日已发 {pacer.sent_today}/{cfg.pacing.daily_limit}）"
                            )
                            break
                    else:
                        empty_rounds = 0

                    rest = random.uniform(*cfg.pacing.round_rest)
                    console.print(
                        f"[dim]  ⏸  本轮发出 {gained} 条，休息 {rest / 60:.0f} "
                        f"分钟后跑下一轮（剩余配额 {pacer.remaining}）…[/]"
                    )
                    time.sleep(rest)
                    stats.rounds += 1

        except QuotaReached as e:
            # 配额用完是正常收工，不是撞风控——别用熔断那套红色告警吓人
            stats.stop_reason = f"{e}，收工"
            console.print(f"\n[bold green]✓ {stats.stop_reason}[/]")
        except Circuit as e:
            stats.stop_reason = f"熔断：{e}"
            console.print(f"\n[bold red]⛔ {stats.stop_reason}[/]")
            if continuous:
                console.print(
                    "[yellow]  连续模式已停。这是风控信号，今天别再跑了——"
                    "先把 send_delay / hourly_limit 往回调。[/]"
                )
        except ModelUnavailable as e:
            stats.stop_reason = str(e)
            console.print(
                f"\n[bold red]{e}[/]\n"
                f"  请在项目根目录 .env 里配置可用的凭据：{greeter.env_hint}。\n"
                "  若确实只想用固定模板招呼语，加 --allow-fallback 重跑。"
            )
        except NotLoggedIn as e:
            stats.stop_reason = str(e)
            console.print(f"\n[bold red]{e}[/]")
        except KeyboardInterrupt:
            stats.stop_reason = "用户中断"
            console.print("\n[yellow]已中断[/]")
        except PWError as e:
            # 多半是窗口/标签页被关了（CDP 接管模式下用户自己关的，或被 BOSS 关的）。
            # 这类中断以前会带着一大屏 traceback 冒出来，而 runs 表里却记成「正常结束」。
            stats.stop_reason = f"浏览器中断：{_brief(e)}"
            console.print(
                f"\n[bold red]⛔ {stats.stop_reason}[/]\n"
                "  接管模式下请保持那个 Chrome 窗口和标签页开着；若是被 BOSS 关掉的，"
                "隔一会儿再跑。已抓到的岗位和招呼语都已入库。"
            )
        except Exception as e:
            # 记下真实原因再抛——否则 finally 会把默认的「正常结束」写进 runs 表。
            stats.stop_reason = f"异常中止：{type(e).__name__}: {_brief(e)}"
            raise
        finally:
            store.finish_run(
                run_id, scanned=stats.scanned, rejected=stats.rejected, sent=stats.sent,
                failed=stats.failed, dry_run=stats.dry_run, stop_reason=stats.stop_reason,
            )

    return stats


def _loop(ctx, page, cfg, sel, store, pacer, greeter, card_chain, jd_chain,
          stats, run_id, dry_run, limit, allow_fallback=False,
          default_greeting=False, screener=None) -> None:
    processed = 0
    model_failures = 0  # 连续降级计数，成功一次就清零
    screen_failures = 0  # AI 判定连续出错计数

    for keyword in cfg.search.keywords:
        console.rule(f"[bold]搜索：{keyword}[/]")

        for job in iter_jobs(page, sel, keyword, cfg.search.city, cfg.search.max_pages):
            if limit is not None and processed >= limit:
                stats.stop_reason = f"达到本次上限 {limit} 个"
                return
            stats.scanned += 1

            # 第一道：只用卡片字段，尽量别进详情页
            verdict = run_chain(card_chain, job)
            if not verdict.passed:
                _reject(store, stats, run_id, job, verdict)
                continue

            if not dry_run:
                pacer.check_quota()

            console.print(f"\n[cyan]▶[/] {job}")
            pacer.pause(scale=0.5)

            job.jd = fetch_jd(page, sel, job)
            store.save_job(job)
            if not job.jd and not default_greeting:
                console.print("  [dim]⚠ 未取到 JD，仍按岗位名生成招呼语[/]")

            # 第二道：JD 到手才能跑的规则
            verdict = run_chain(jd_chain, job)
            if not verdict.passed:
                _reject(store, stats, run_id, job, verdict)
                continue

            # 第三道：规则放行的，再花一次调用让模型读 JD 判外包/销售。
            # 顺序很重要——规则是免费的，先用规则刷掉一批再问模型。
            if screener is not None:
                sv = screener.check(job)
                if sv.errored:
                    screen_failures += 1
                    console.print(f"  [dim]⚠ AI 判定失败，放行：{sv.reason}[/]")
                    if screen_failures >= cfg.ai_screen.max_consecutive_failures:
                        stats.stop_reason = (
                            f"AI 判定连续 {screen_failures} 次失败，已停止"
                        )
                        console.print(
                            f"\n[bold red]⛔ {stats.stop_reason}[/]\n"
                            f"  最后一次：{sv.reason}\n"
                            "  这层不通等于没开，继续跑会把外包/销售岗一起发出去。"
                        )
                        return
                else:
                    screen_failures = 0
                    if not sv.ok and sv.label in cfg.ai_screen.skip:
                        _reject(store, stats, run_id, job,
                                FilterResult.reject("ai_screen", f"AI 判定：{sv.reason}"))
                        continue

            if default_greeting:
                # 不生成招呼语：点开沟通，由 BOSS 发它自己的默认那条
                message, model = "", "default"
            else:
                message, model = greeter.generate(job)
            if default_greeting or not model.startswith("fallback"):
                model_failures = 0
            elif not allow_fallback:
                console.print(f"  [yellow]↩ 招呼语降级到模板（{model}）[/]")
                model_failures += 1
                if model_failures >= _MAX_MODEL_FAILURES:
                    stats.stop_reason = f"招呼语模型连续 {model_failures} 次不可用，已停止"
                    console.print(
                        f"\n[bold red]⛔ {stats.stop_reason}[/]\n"
                        f"  最后一次：{model}\n"
                        "  等模型/网关恢复后重跑即可，已处理过的岗位不会重复打招呼。"
                    )
                    return
                # 模板招呼语对任何岗位都成立，发出去基本等于石沉大海，还白耗一格配额。
                # 真实发送时宁可跳过这个岗位，下轮再来。
                if not dry_run:
                    stats.skipped += 1
                    store.save_greeting(
                        Greeting(job.job_id, message, model, "skipped", "降级到模板，未发送")
                    )
                    console.print("  [dim]⇢ 跳过（加 --allow-fallback 可强行发模板）[/]")
                    pacer.pause(scale=0.3)
                    continue
            if default_greeting:
                console.print("  [green]✎[/] [dim]（平台默认招呼语）[/]")
            else:
                console.print(f"  [green]✎[/] {message}")

            processed += 1

            if dry_run:
                stats.dry_run += 1
                store.save_greeting(Greeting(job.job_id, message, model, "dry_run"))
                pacer.pause(scale=0.3)
                continue

            result = (
                send_default_greeting(ctx, page, sel, job)
                if default_greeting
                else send_greeting(ctx, page, sel, job, message)
            )
            if result.ok:
                stats.sent += 1
                store.save_greeting(Greeting(job.job_id, message, model, "sent"))
                console.print(f"  [bold green]✓ 已发送[/]　今日 {pacer.sent_today + 1}/{cfg.pacing.daily_limit}")
                pacer.after_send()
            elif result.already:
                # 早就打过招呼了，不是发送失败。计入熔断的话，连着遇上三个
                # 老岗位就会把整轮打断，而一条消息都没真的发失败过。
                stats.skipped += 1
                store.save_greeting(
                    Greeting(job.job_id, message, model, "already", result.reason)
                )
                console.print(f"  [dim]⇢ 跳过：{result.reason}[/]")
            else:
                stats.failed += 1
                store.save_greeting(Greeting(job.job_id, message, model, "failed", result.reason))
                console.print(f"  [red]✗ 发送失败：{result.reason}[/]")
                pacer.after_failure(result.reason)

            pacer.pause()


def _reject(store: Store, stats: RunStats, run_id: int, job: Job, verdict) -> None:
    stats.rejected += 1
    stats.reject_by_rule[verdict.rejected_by] = stats.reject_by_rule.get(verdict.rejected_by, 0) + 1
    store.record_rejection(run_id, job.job_id, verdict.rejected_by, verdict.reason)
    console.print(f"[dim]  ✗ {job.title} @ {job.company} — {verdict.reason}[/]")
