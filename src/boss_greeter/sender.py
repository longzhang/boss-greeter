"""点「立即沟通」、填招呼语、发送、确认结果。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from patchright.sync_api import BrowserContext, Page

from .browser import detect_risk, first_match, human_type
from .config import LOG_DIR, Selectors
from .models import Job
from .pacing import Circuit


@dataclass
class SendResult:
    ok: bool
    reason: str = ""


def _screenshot(page: Page, job_id: str, tag: str) -> str:
    """出错时留证据，方便回头核对是选择器问题还是被风控了。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = LOG_DIR / f"{stamp}-{tag}-{job_id}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
        return str(path)
    except Exception:
        return ""


def _guard_risk(page: Page, sel: Selectors) -> None:
    """命中风控文案就熔断——继续点下去只会让账号更危险。"""
    if phrase := detect_risk(page, sel):
        _screenshot(page, "risk", "risk")
        raise Circuit(f"页面出现风控提示：「{phrase}」")


def _chat_page(ctx: BrowserContext, origin: Page, timeout_ms: int = 6000) -> Page:
    """点沟通后可能原地弹窗，也可能新开标签页。返回真正承载聊天的页面。"""
    before = set(ctx.pages)
    origin.wait_for_timeout(1500)
    new_pages = [p for p in ctx.pages if p not in before]
    if new_pages:
        target = new_pages[-1]
        try:
            target.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
        return target
    return origin


def _open_chat(ctx: BrowserContext, page: Page, sel: Selectors, job: Job):
    """点「立即沟通」，返回 (聊天页, 失败原因)。两条发送路径共用这一段。"""
    _guard_risk(page, sel)

    btn = first_match(page, sel.get("detail.chat_button"), timeout=5000)
    if btn is None:
        _screenshot(page, job.job_id, "no-chat-btn")
        return None, "找不到「立即沟通」按钮（可能已沟通过或选择器失效）"

    try:
        btn.scroll_into_view_if_needed(timeout=3000)
        btn.click(timeout=8000)
    except Exception as e:
        _screenshot(page, job.job_id, "click-failed")
        return None, f"点击沟通按钮失败：{type(e).__name__}"

    chat = _chat_page(ctx, page)
    _guard_risk(chat, sel)
    return chat, ""


#: 「立即沟通」点下去后，等平台确认框出现的总时长（毫秒）
_DIALOG_WAIT_MS = 6000


def send_default_greeting(
    ctx: BrowserContext, page: Page, sel: Selectors, job: Job
) -> SendResult:
    """只点「立即沟通」，招呼语用你在 BOSS 里设好的那条。

    实际行为跟「打开聊天再发消息」不一样：点下去平台**当场就把招呼语发出去了**，
    然后弹一个「已向BOSS发送消息」的确认框，页面并不会出现聊天面板。
    所以这里认的是那个确认框，而不是聊天气泡——找气泡的话永远等不到，
    会把已经发成功的岗位记成失败，攒够三次还会误触熔断。

    招呼语内容在 BOSS 的「消息通知-设置打招呼语」里改，本工具不参与。
    """
    if _already_chatted(page, sel):
        return SendResult(False, "这个岗位之前已经沟通过（按钮是「继续沟通」）")

    chat, err = _open_chat(ctx, page, sel, job)
    if chat is None:
        return SendResult(False, err)

    # 确认框是异步弹的，轮询着等——比固定 sleep 稳，也比一次性检查宽容
    deadline = _DIALOG_WAIT_MS
    while deadline > 0:
        _guard_risk(chat, sel)
        if _sent_dialog(chat, sel):
            _dismiss_dialog(chat, sel)
            _close_if_new_tab(chat, page)
            return SendResult(True)
        # 万一哪天真跳到了聊天页，气泡也算数
        if _has_sent_bubble(chat, sel):
            _close_if_new_tab(chat, page)
            return SendResult(True)
        chat.wait_for_timeout(500)
        deadline -= 500

    _screenshot(chat, job.job_id, "no-sent-dialog")
    return SendResult(False, "点了沟通但没等到「已发送」确认框")


def _already_chatted(page: Page, sel: Selectors) -> bool:
    """按钮变成「继续沟通」= 之前已经打过招呼，再点一次只是跳去聊天页。"""
    return first_match(page, sel.get("chat.already_chatted"), timeout=500) is not None


def _sent_dialog(page: Page, sel: Selectors) -> bool:
    """页面上有没有「已向BOSS发送消息」这类确认文案。"""
    texts = sel.sent_dialog_texts
    if not texts:
        return False
    try:
        body = page.inner_text("body", timeout=2000)
    except Exception:
        return False
    return any(t in body for t in texts)


def _dismiss_dialog(page: Page, sel: Selectors) -> None:
    """点掉确认框，免得它盖住下一个岗位的操作。

    只点「留在此页」——「继续沟通」会把页面带去聊天页，后面的流程就乱了。
    点不到也无所谓：下一个岗位会 goto 新的详情页，弹窗自然消失。
    """
    btn = first_match(page, sel.get("chat.stay_button"), timeout=1500)
    if btn is None:
        return
    try:
        btn.click(timeout=3000)
    except Exception:
        pass


def _enter_chat_from_dialog(ctx: BrowserContext, page: Page, sel: Selectors) -> Page | None:
    """顺着确认框里的「继续沟通」进聊天页。点不动就返回 None，让调用方照旧处理。"""
    btn = first_match(page, sel.get("chat.continue_button"), timeout=2000)
    if btn is None:
        return None
    try:
        btn.click(timeout=5000)
    except Exception:
        return None
    return _chat_page(ctx, page)


def _close_if_new_tab(chat: Page, page: Page) -> None:
    """新开的聊天标签页用完就关，否则跑几十个会开满标签。"""
    if chat is not page:
        try:
            chat.close()
        except Exception:
            pass


def send_greeting(
    ctx: BrowserContext, page: Page, sel: Selectors, job: Job, message: str
) -> SendResult:
    """在已打开的岗位详情页上完成一次打招呼。

    页面必须已经停在 job.url 对应的详情页（fetch_jd 会把页面留在那里）。
    """
    if _already_chatted(page, sel):
        return SendResult(False, "这个岗位之前已经沟通过（按钮是「继续沟通」）")

    chat, err = _open_chat(ctx, page, sel, job)
    if chat is None:
        return SendResult(False, err)

    # 点完「立即沟通」平台会先自动发一条你设好的招呼语，并弹确认框，页面停在详情页。
    # 要发自己写的这条，得顺着确认框里的「继续沟通」进聊天页，否则根本没有输入框。
    if _sent_dialog(chat, sel):
        chat = _enter_chat_from_dialog(ctx, chat, sel) or chat

    box = first_match(chat, sel.get("chat.input"), timeout=8000)
    if box is None:
        _screenshot(chat, job.job_id, "no-input")
        return SendResult(False, "找不到聊天输入框")

    try:
        box.click(timeout=5000)
        _clear(chat, box)
        human_type(box, message)
    except Exception as e:
        _screenshot(chat, job.job_id, "type-failed")
        return SendResult(False, f"输入招呼语失败：{type(e).__name__}")

    send_btn = first_match(chat, sel.get("chat.send_button"), timeout=2000)
    try:
        if send_btn is not None:
            send_btn.click(timeout=5000)
        else:
            chat.keyboard.press("Enter")
    except Exception as e:
        _screenshot(chat, job.job_id, "send-failed")
        return SendResult(False, f"点击发送失败：{type(e).__name__}")

    chat.wait_for_timeout(2000)
    _guard_risk(chat, sel)

    if not _confirm_sent(chat, sel, message):
        _screenshot(chat, job.job_id, "unconfirmed")
        return SendResult(False, "未在对话流中看到已发送的消息")

    # 新开的聊天标签页用完就关，否则跑几十个会开满标签
    if chat is not page:
        try:
            chat.close()
        except Exception:
            pass
    return SendResult(True)


def _clear(chat: Page, box) -> None:
    """清掉 BOSS 预置的默认文案。

    输入框可能是 textarea，也可能是 contenteditable div，两种清法都试一遍。
    """
    try:
        box.fill("")
        if not (box.input_value() if _is_fillable(box) else box.inner_text()).strip():
            return
    except Exception:
        pass
    modifier = "Meta" if chat.evaluate("navigator.platform").startswith("Mac") else "Control"
    chat.keyboard.press(f"{modifier}+A")
    chat.keyboard.press("Backspace")


def _is_fillable(box) -> bool:
    try:
        return box.evaluate("el => el.tagName") in ("TEXTAREA", "INPUT")
    except Exception:
        return False


def _has_sent_bubble(chat: Page, sel: Selectors) -> bool:
    """对话流里有没有「自己发出的」气泡。默认招呼语的正文由平台决定，
    这边不知道内容，只能确认气泡存在。"""
    for candidate in sel.get("chat.sent_bubble"):
        try:
            if chat.locator(candidate).count() > 0:
                return True
        except Exception:
            continue
    return False


def _confirm_sent(chat: Page, sel: Selectors, message: str) -> bool:
    """在自己发出的消息气泡里找招呼语片段，确认真的发出去了。"""
    probe = message[:12]
    for candidate in sel.get("chat.sent_bubble"):
        try:
            bubbles = chat.locator(candidate)
            if bubbles.count() == 0:
                continue
            if any(probe in t for t in bubbles.all_inner_texts()):
                return True
        except Exception:
            continue
    # 气泡选择器可能已失效，退一步在整页正文里找
    try:
        return probe in chat.inner_text("body", timeout=2000)
    except Exception:
        return False
