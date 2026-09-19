"""招呼语校验的单测。不打网络，只测 validate 这一段纯逻辑。"""

import pytest

from boss_greeter.config import GreetingConfig
from boss_greeter.greeter import GreetingError, validate
from boss_greeter.models import Job

@pytest.fixture
def resume(tmp_path) -> str:
    """GreetingConfig 要求简历文件存在，测里给个临时文件就行。"""
    f = tmp_path / "resume.md"
    f.write_text("简历正文", encoding="utf-8")
    return str(f)


@pytest.fixture
def cfg(tmp_path):
    # GreetingConfig 会校验简历文件存在，给个临时文件即可——
    # 这里测的是 validate，不读简历内容
    resume = tmp_path / "resume.md"
    resume.write_text("简历正文", encoding="utf-8")
    return GreetingConfig(resume_path=resume, min_chars=20, max_chars=100)


def a_job(jd="") -> Job:
    return Job(job_id="j1", title="架构师", url="u", company="某司", jd=jd)


def ok_msg(n=50) -> str:
    return "看到贵司在做 AI 应用平台，我这两年正好在搭 Agent 编排和 LLM 网关。" + "好" * (n - 33)


def test_accepts_normal_message(cfg):
    validate(ok_msg(), a_job(), cfg)


@pytest.mark.parametrize("bad", [
    "太短了",
    "长" * 200,
])
def test_rejects_bad_length(cfg, bad):
    with pytest.raises(GreetingError, match="长度"):
        validate(bad, a_job(), cfg)


def test_rejects_empty(cfg):
    with pytest.raises(GreetingError, match="为空"):
        validate("   ", a_job(), cfg)


@pytest.mark.parametrize("leak", [
    "我的手机号是 13800138000 欢迎联系，这段话要足够长才能过长度校验啊啊啊啊",
    "加我微信聊吧，这段话要足够长才能过长度校验啊啊啊啊啊啊啊啊啊啊啊啊",
    "邮箱 someone@example.com 联系我，这段话要足够长才能过长度校验啊啊啊啊啊啊",
])
def test_rejects_contact_info(cfg, leak):
    """简历里就有手机号和邮箱，模型很容易顺手抄进去，必须拦住。"""
    with pytest.raises(GreetingError, match="联系方式"):
        validate(leak, a_job(), cfg)


def test_rejects_verbatim_jd_copy(cfg):
    jd = "负责公司核心交易系统的架构设计与性能优化，主导微服务拆分和容量治理工作"
    msg = "我做过类似的事：" + jd[:30] + "，方便聊聊吗？"
    with pytest.raises(GreetingError, match="照抄"):
        validate(msg, a_job(jd), cfg)


# --- preflight：开跑前确认模型真的能用 -------------------------------------


class _Msgs:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        if self.exc:
            raise self.exc
        return object()


class _FakeClient:
    def __init__(self, exc=None):
        self.messages = _Msgs(exc)
        self.base_url = "https://api.anthropic.com"


def _greeter_with(monkeypatch, cfg, exc=None):
    from boss_greeter import greeter as mod

    fake = _FakeClient(exc)
    monkeypatch.setattr(mod.anthropic, "Anthropic", lambda *a, **k: fake)
    return mod.Greeter(cfg, "简历正文"), fake


def test_preflight_ok(monkeypatch, cfg):
    g, fake = _greeter_with(monkeypatch, cfg)
    assert g.preflight() is None
    assert fake.messages.calls == 1


def test_preflight_reports_http_error(monkeypatch, cfg):
    """环境里带着本工具用不了的凭据时，必须在开跑前就暴露出来。

    否则每个岗位都静默降级成固定模板，等于把一天的配额发成通用招呼语。
    """
    import anthropic

    # SDK 的异常构造要求真实的 httpx.Response，这里只关心 preflight 怎么读它，
    # 所以绕过 __init__ 直接摆出它要用到的两个属性。
    exc = anthropic.InternalServerError.__new__(anthropic.InternalServerError)
    exc.status_code = 503
    exc.response = type(
        "R", (), {"json": staticmethod(lambda: {"error": {"message": "No available accounts"}})}
    )()
    g, _ = _greeter_with(monkeypatch, cfg, exc)

    err = g.preflight()
    assert err is not None
    assert "503" in err and "No available accounts" in err
    # 配了网关时最常见的原因就是发错了地址，所以报错里必须带上它
    assert "https://api.anthropic.com" in err


def test_preflight_reports_missing_credentials(monkeypatch, cfg):
    g, _ = _greeter_with(monkeypatch, cfg, RuntimeError("api_key 未设置"))
    assert "api_key 未设置" in g.preflight()


def test_preflight_names_the_gateway_on_connection_error(monkeypatch, cfg):
    """配了自建网关又连不上时，把实际请求地址回显出来，省得对着报错猜。"""
    import anthropic

    exc = anthropic.APIConnectionError.__new__(anthropic.APIConnectionError)
    Exception.__init__(exc, "Connection refused")
    g, _ = _greeter_with(monkeypatch, cfg, exc)
    g.backend.base_url = "https://my-gateway.example.com"

    err = g.preflight()
    assert "my-gateway.example.com" in err


# ---------------------------------------------------------------- OpenAI 后端


class _Completions:
    """记下每次调用的 kwargs，用来断言 token 参数名。"""

    def __init__(self, exc=None):
        self.exc = exc
        self.calls: list[dict] = []

    def create(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        msg = type("M", (), {"content": "招呼语正文"})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()


class _FakeOpenAI:
    def __init__(self, exc=None):
        self.completions = _Completions(exc)
        self.chat = type("Chat", (), {"completions": self.completions})()
        self.base_url = "https://api.openai.com/v1"


def _openai_greeter(monkeypatch, tmp_path, exc=None):
    from boss_greeter import greeter as mod

    fake = _FakeOpenAI(exc)
    monkeypatch.setattr(mod.openai, "OpenAI", lambda *a, **k: fake)
    f = tmp_path / "resume.md"
    f.write_text("简历正文", encoding="utf-8")
    c = GreetingConfig(resume_path=f, provider="openai", min_chars=20, max_chars=100)
    return mod.Greeter(c, "简历正文"), fake


def test_openai_provider_uses_openai_client(monkeypatch, tmp_path):
    g, fake = _openai_greeter(monkeypatch, tmp_path)
    assert g.preflight() is None
    call = fake.completions.calls[0]
    assert call["model"] == "gpt-5.4"
    # 简历前缀走 system 消息（OpenAI 侧前缀缓存是自动的，不用显式标记）
    assert g.backend.system.startswith("你是一位资深求职者本人")


def test_openai_falls_back_to_legacy_token_param(monkeypatch, tmp_path):
    """第三方兼容网关多半只认 max_tokens，被顶回来一次后要自己换过去。"""
    import openai

    exc = openai.BadRequestError.__new__(openai.BadRequestError)
    Exception.__init__(exc, "Unsupported parameter: 'max_completion_tokens'")
    g, fake = _openai_greeter(monkeypatch, tmp_path, exc)
    # 第一次两种参数名都被拒（fake 对每次调用都抛），但换名字这件事要发生过
    g.preflight()

    assert list(fake.completions.calls[0]) [-1] == "max_completion_tokens"
    assert "max_tokens" in fake.completions.calls[1]
    assert g.backend._token_param == "max_tokens"


def test_openai_error_names_the_endpoint(monkeypatch, tmp_path):
    import openai

    exc = openai.InternalServerError.__new__(openai.InternalServerError)
    exc.status_code = 500
    exc.response = type(
        "R", (), {"json": staticmethod(lambda: {"error": {"message": "boom"}})}
    )()
    g, _ = _openai_greeter(monkeypatch, tmp_path, exc)

    err = g.preflight()
    assert "500" in err and "boom" in err and "api.openai.com" in err


def test_provider_model_mismatch_is_rejected(resume):
    """换了 provider 忘了换 model 是最容易犯的错，配置层就拦掉。"""
    with pytest.raises(ValueError, match="provider"):
        GreetingConfig(resume_path=resume, provider="openai", model="claude-opus-5")


def test_model_defaults_follow_provider(resume):
    assert GreetingConfig(resume_path=resume).model == "claude-opus-5"
    assert GreetingConfig(resume_path=resume, provider="openai").model == "gpt-5.4"


def test_missing_key_surfaces_in_preflight_not_at_construction(monkeypatch, cfg):
    """没配 key 时不该在构造处炸出 traceback，要走 preflight 那条带提示的路。"""
    from boss_greeter import greeter as mod

    def boom(*a, **k):
        raise mod.anthropic.AnthropicError("api_key 未设置")

    monkeypatch.setattr(mod.anthropic, "Anthropic", boom)
    g = mod.Greeter(cfg, "简历正文")

    assert "api_key 未设置" in g.preflight()
    # --allow-fallback 时整轮走模板也得能跑完
    text, model = g.generate(a_job())
    assert text and model.startswith("fallback")


def test_openai_unknown_model_lists_what_the_gateway_offers(monkeypatch, tmp_path):
    """兼容网关的模型名是自定义的，猜不出来——报错里直接把清单给出来。"""
    import openai

    exc = openai.NotFoundError.__new__(openai.NotFoundError)
    Exception.__init__(exc, 'Model "gpt-5" is not supported by this group')
    exc.status_code = 404
    exc.response = type(
        "R", (), {"json": staticmethod(lambda: {"error": {"message": 'Model "gpt-5" is not supported'}})}
    )()
    g, fake = _openai_greeter(monkeypatch, tmp_path, exc)
    fake.models = type(
        "M", (), {"list": staticmethod(lambda: type("L", (), {"data": [
            type("D", (), {"id": "gpt-5.4"})(), type("D", (), {"id": "gpt-5.2"})()]})())}
    )()

    err = g.preflight()
    assert "gpt-5.4" in err and "gpt-5.2" in err


# ------------------------------------------------- generate：什么时候才认输

def _scripted(monkeypatch, cfg, *replies):
    """让 backend.complete 按顺序吐出预设内容；元素是异常就抛出来。"""
    g, _ = _greeter_with(monkeypatch, cfg)
    calls = []

    def complete(user: str) -> str:
        r = replies[min(len(calls), len(replies) - 1)]
        calls.append(user)
        if isinstance(r, Exception):
            raise r
        return r

    g.backend.complete = complete
    return g, calls


def _conn_error(msg="Connection error."):
    import anthropic

    exc = anthropic.APIConnectionError.__new__(anthropic.APIConnectionError)
    Exception.__init__(exc, msg)
    return exc


def test_slightly_overlong_beats_a_generic_template(monkeypatch, cfg):
    """写超一点的针对性招呼语，也比「方向比较契合方便聊聊吗」的通用模板强。

    上限 100 字、容忍到 115 字：两次都压不进字数时收下 110 字那版，不降级。
    """
    text = "看到贵司在做 AI 应用平台，我这两年正好在搭 Agent 编排和 LLM 网关。" + "好" * 69
    assert len(text) == 110
    g, calls = _scripted(monkeypatch, cfg, text)

    msg, model = g.generate(a_job())

    assert msg == text
    assert "超长" in model and "fallback" not in model
    assert len(calls) == 2  # 收下之前确实让它改短过一次


def test_a_real_essay_still_falls_back(monkeypatch, cfg):
    """超出容忍线就该降级——收下 300 字的小作文不是「宽容」，是发错东西。"""
    g, _ = _scripted(monkeypatch, cfg, "长" * 300)

    _, model = g.generate(a_job())

    assert model.startswith("fallback")


def test_a_network_hiccup_gets_a_second_try(monkeypatch, cfg):
    """网关抖一下就把这个岗位判给模板，太亏了——连接类错误值得再试一次。"""
    g, calls = _scripted(monkeypatch, cfg, _conn_error(), ok_msg())

    msg, model = g.generate(a_job())

    assert msg == ok_msg()
    assert model == cfg.model
    assert len(calls) == 2


def test_auth_failure_does_not_get_a_second_try(monkeypatch, cfg):
    """401 / 模型不存在这类错误重试也是同一个结果，别白花一次请求。"""
    import anthropic

    exc = anthropic.AuthenticationError.__new__(anthropic.AuthenticationError)
    exc.status_code = 401
    exc.response = type("R", (), {"json": staticmethod(lambda: {})})()
    g, calls = _scripted(monkeypatch, cfg, exc)

    _, model = g.generate(a_job())

    assert model.startswith("fallback")
    assert len(calls) == 1
