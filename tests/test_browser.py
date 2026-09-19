"""接管模式（CDP）相关的纯逻辑单测。不起浏览器、不打网络。"""

import pytest

from boss_greeter.browser import AttachFailed, chrome_launch_cmd, page_of, require_window
from boss_greeter.config import CHROME_PROFILE_DIR, BrowserConfig


class _Page:
    def __init__(self, url, closed=False):
        self.url = url
        self._closed = closed

    def is_closed(self):
        return self._closed


class _Ctx:
    def __init__(self, pages):
        self.pages = pages
        self.opened = 0

    def new_page(self):
        self.opened += 1
        return _Page("about:blank")


def test_page_of_prefers_an_existing_boss_tab():
    """接管用户自己的浏览器时，抢占第一个标签会把人家的页面导走。"""
    boss = _Page("https://www.zhipin.com/web/geek/jobs")
    ctx = _Ctx([_Page("https://mail.example.com"), boss])
    assert page_of(ctx, attach=True) is boss
    assert ctx.opened == 0


def test_page_of_reuses_blank_tab_when_it_launched_the_browser():
    blank = _Page("about:blank")
    ctx = _Ctx([_Page("https://news.example.com"), blank])
    assert page_of(ctx) is blank


def test_page_of_never_reuses_a_tab_in_attach_mode():
    """用户自己那个 about:blank 也可能是他正要用的，接管模式下一律新开。"""
    ctx = _Ctx([_Page("about:blank")])
    page_of(ctx, attach=True)
    assert ctx.opened == 1


def test_page_of_opens_a_tab_when_all_are_busy():
    ctx = _Ctx([_Page("https://news.example.com")])
    page_of(ctx)
    assert ctx.opened == 1


def test_page_of_skips_closed_tabs():
    ctx = _Ctx([_Page("https://www.zhipin.com/", closed=True)])
    page_of(ctx)
    assert ctx.opened == 1


def test_require_window_rejects_a_browser_with_no_tabs(monkeypatch):
    """窗口全关了但进程还在时，CDP 报的错跟真实原因八竿子打不着，要提前拦。"""
    import boss_greeter.browser as mod

    class _Resp:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod, "urlopen",
                        lambda *a, **k: _Resp(b'[{"type": "browser_ui"}]'))
    with pytest.raises(AttachFailed, match="窗口"):
        require_window("http://127.0.0.1:9222")

    monkeypatch.setattr(mod, "urlopen",
                        lambda *a, **k: _Resp(b'[{"type": "page"}]'))
    require_window("http://127.0.0.1:9222")  # 有窗口就放行


def test_debug_port_comes_from_the_cdp_url():
    assert BrowserConfig(attach_cdp="http://127.0.0.1:9333").debug_port == 9333
    # 写坏了也不要炸，退回默认端口
    assert BrowserConfig(attach_cdp="http://localhost").debug_port == 9222


def test_launch_cmd_uses_a_dedicated_profile_dir():
    """Chrome 136 起不允许对默认用户目录开调试端口，必须单独给一个。"""
    cmd = chrome_launch_cmd(BrowserConfig(attach_cdp="http://127.0.0.1:9222"))
    assert "--remote-debugging-port=9222" in cmd
    assert str(CHROME_PROFILE_DIR) in cmd


def test_launch_cmd_honours_custom_profile_dir():
    cfg = BrowserConfig(attach_cdp="http://127.0.0.1:9222", chrome_user_data_dir="~/my-chrome")
    assert "my-chrome" in chrome_launch_cmd(cfg)
