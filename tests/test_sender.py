"""默认招呼语路径的单测。不起浏览器、不打网络。"""

import pytest

from boss_greeter.config import Selectors
from boss_greeter.models import Job
from boss_greeter.pacing import Circuit
from boss_greeter.sender import send_default_greeting

SEL = Selectors({
    "detail": {"chat_button": ["a.btn-startchat"]},
    "chat": {
        "input": ["#chat-input"],
        "send_button": [".btn-send"],
        "sent_bubble": [".item-myself .text"],
        # 跟 selectors.yaml 保持一致：旧确认框文案 + 改版后聊天弹层的订阅提示
        "sent_dialog_texts": ["已向BOSS发送消息", "已开启消息订阅"],
        "stay_button": ["button:has-text('留在此页')"],
        "continue_button": [".dialog-container button:has-text('继续沟通')"],
        "already_chatted": ["a:has-text('继续沟通')"],
    },
    "risk_texts": ["操作过于频繁"],
})

JOB = Job(job_id="j1", title="Golang 后端", url="https://example.com/j1", company="某司")


class _Btn:
    """first_match 拿到的 Locator：要能 wait_for(attached)，也要能点。"""

    def __init__(self, exists=True):
        self.clicked = False
        self.exists = exists

    @property
    def first(self):
        return self

    def wait_for(self, state=None, timeout=0):
        if not self.exists:
            raise RuntimeError("not attached")

    def scroll_into_view_if_needed(self, timeout=0):
        pass

    def click(self, timeout=0):
        self.clicked = True


class _Bubbles:
    """_has_sent_bubble 只调 count()。"""

    def __init__(self, n=0):
        self._n = n

    @property
    def first(self):
        return self

    def wait_for(self, state=None, timeout=0):
        if self._n == 0:
            raise RuntimeError("not attached")

    def count(self):
        return self._n


class _Page:
    """只实现 sender 真正用到的那几个方法。"""

    def __init__(self, *, btn=True, bubbles=0, text="", already_chatted=False,
                 flips_after_click=False):
        self.btn = _Btn(exists=btn)
        self.stay = _Btn()
        self.bubbles = bubbles
        self.text = text
        self.already_chatted = already_chatted
        # 真实行为：点完「立即沟通」，详情页那个按钮会变成「继续沟通」
        self.flips_after_click = flips_after_click
        self.typed = []
        self.closed = False

    def inner_text(self, selector="body", timeout=0):
        return self.text

    def locator(self, selector):
        # 按选择器分派：沟通按钮 / 留在此页 / 已沟通过 / 消息气泡
        if "startchat" in selector:
            return self.btn
        if "留在此页" in selector:
            return self.stay
        if "继续沟通" in selector:
            flipped = self.flips_after_click and self.btn.clicked
            return _Btn(exists=self.already_chatted or flipped)
        return _Bubbles(self.bubbles)

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, path=None, full_page=False):
        pass

    def keyboard_press(self, key):
        self.typed.append(key)

    def close(self):
        self.closed = True


class _Ctx:
    def __init__(self, pages):
        self.pages = pages


def test_default_greeting_accepts_the_sent_dialog():
    """BOSS 点完沟通是弹「已向BOSS发送消息」确认框，不是开聊天面板。

    这正是最初把已发成功的岗位记成失败、攒三次误触熔断的原因。
    """
    page = _Page(text="已向BOSS发送消息")
    result = send_default_greeting(_Ctx([page]), page, SEL, JOB)
    assert result.ok
    assert page.btn.clicked
    assert page.typed == []
    assert page.stay.clicked, "确认框该点掉，否则会盖住下一个岗位"


def test_default_greeting_still_accepts_a_chat_bubble():
    """万一哪天真跳进了聊天页，气泡也算发送成功。"""
    page = _Page(bubbles=1)
    result = send_default_greeting(_Ctx([page]), page, SEL, JOB)
    assert result.ok
    assert page.typed == []


def test_default_greeting_skips_already_chatted_job():
    page = _Page(text="已向BOSS发送消息", already_chatted=True)
    result = send_default_greeting(_Ctx([page]), page, SEL, JOB)
    assert not result.ok
    assert "已经沟通过" in result.reason
    assert not page.btn.clicked, "已沟通过的岗位不该再点"


def test_default_greeting_fails_when_button_missing():
    page = _Page(btn=False, bubbles=1)
    result = send_default_greeting(_Ctx([page]), page, SEL, JOB)
    assert not result.ok
    assert "立即沟通" in result.reason


def test_default_greeting_fails_when_nothing_confirms():
    """既没确认框、没气泡、按钮也没翻转——只有这种才算真失败。"""
    page = _Page(bubbles=0)
    result = send_default_greeting(_Ctx([page]), page, SEL, JOB, wait_ms=50)
    assert not result.ok
    assert "确认框" in result.reason


def test_default_greeting_trips_circuit_on_risk_text():
    page = _Page(text="操作过于频繁，请稍后再试")
    with pytest.raises(Circuit):
        send_default_greeting(_Ctx([page]), page, SEL, JOB)


# ---------------------------------------------------------------- 2026-09-22 改版


def test_button_flip_counts_as_sent_even_without_any_dialog():
    """最关键的一条：BOSS 2026-09-22 改版后不再弹「已向BOSS发送消息」确认框，
    而是当场打开站内聊天弹层。三条旧文案一个都不出现，只认文案会把**全部**
    已发成功的岗位判成失败，攒三次就误触熔断（实测两轮各发出 0 条就被打断）。

    但详情页那个按钮点完必定从「立即沟通」变成「继续沟通」——认这个，
    改版打不掉。
    """
    page = _Page(bubbles=0, flips_after_click=True)
    result = send_default_greeting(_Ctx([page]), page, SEL, JOB, wait_ms=3000)
    assert result.ok, "按钮已翻转成「继续沟通」就该算发送成功"
    assert page.btn.clicked
    assert page.typed == []


def test_new_chat_panel_text_also_counts():
    """改版后那个弹层右侧的订阅提示，也是一条可用的判据。"""
    page = _Page(text="已开启消息订阅　小程序也能继续回复BOSS信息")
    result = send_default_greeting(_Ctx([page]), page, SEL, JOB, wait_ms=3000)
    assert result.ok


def test_already_chatted_is_not_a_send_failure():
    """「之前已经沟通过」不是发送失败：招呼语早就发出去了，平台没拒绝我们。

    标成 failure 的话，连着遇上三个老岗位就会触发「连续 3 次发送失败」熔断，
    而一条消息都没真的发失败过——这正是上一版误判留下的后遗症。
    """
    page = _Page(already_chatted=True)
    result = send_default_greeting(_Ctx([page]), page, SEL, JOB, wait_ms=50)
    assert not result.ok
    assert result.already, "得能跟真失败区分开，否则会计入熔断计数"
    assert not page.btn.clicked


def test_real_failure_is_not_marked_already():
    page = _Page(bubbles=0)
    result = send_default_greeting(_Ctx([page]), page, SEL, JOB, wait_ms=50)
    assert not result.ok
    assert not result.already, "真失败必须计入熔断，否则风控拦截时会一直撞下去"
