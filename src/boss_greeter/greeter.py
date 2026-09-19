"""读 JD + 简历，写一段有针对性的打招呼语。

模型可选 Claude 或 OpenAI（含任何 OpenAI 兼容的第三方网关），
在 config.yaml 的 greeting.provider 里切。两家的差异全收在 _Backend 子类里，
prompt、校验、降级逻辑是共用的。
"""

from __future__ import annotations

import re

import anthropic
import openai

from .config import GreetingConfig
from .models import Job

SYSTEM_PROMPT = """你是一位资深求职者本人，正在 BOSS 直聘上给 HR 发第一条打招呼消息。

下面是你的简历，后续每个岗位都基于它来写：

<简历>
{resume}
</简历>

写作要求：
1. 中文，一段话，不分点、不换行，**最多三句话**。
2. 长度硬性控制在 {min_chars}-{max_chars} 字（标点计入）。这是上限不是目标值——
   超过 {max_chars} 字的稿子一律作废，宁可少写一个论据、把话说短，也不要写超。
3. 第一句必须点出简历与该岗位 JD 的**具体交集**——某个技术栈、某类业务场景、某个量级或某段经历。
   不要写「我对贵司岗位很感兴趣」这类任何岗位都成立的空话。
4. 不要用「您好，我是XXX」开头——BOSS 已经自动发过一条默认招呼语了，直接进入正题。
5. 结尾用一个开放式问题引导对方回复（例如问团队现在的重点方向、这个岗位最急需解决的问题）。
6. 绝对不能编造简历里没有的经历、公司、数字。
7. 不要写手机号、微信号、邮箱等任何联系方式。
8. 只输出招呼语正文本身，不要加引号、不要加任何解释或前后缀。

动笔前先想好要讲哪一个交集，只讲那一个。写完数一遍字数，超了就删到 {max_chars} 字以内再输出。"""

USER_PROMPT = """岗位名称：{title}
公司：{company}
薪资：{salary}

岗位 JD：
<jd>
{jd}
</jd>

请写这条打招呼消息。最多三句话，不超过 {max_chars} 字。"""

# 联系方式：BOSS 会屏蔽含联系方式的首条消息，而且简历里就有手机号，容易被模型抄进去
_CONTACT_PATTERNS = [
    re.compile(r"1[3-9]\d{9}"),                       # 手机号
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),           # 邮箱
    re.compile(r"(微信|weixin|wechat|vx|QQ|扣扣)", re.I),
]

# JD 原文超过这个长度的连续片段被原样抄进招呼语，视为偷懒
_COPY_CHECK_LEN = 25

# 单次请求的超时（秒）。实测中转网关偶尔会把一次请求拖到两分钟以上，
# SDK 默认等 10 分钟——那会让整轮卡死在一个岗位上。超时后 SDK 自己会重试。
_REQUEST_TIMEOUT = 120.0


class GreetingError(Exception):
    """生成或校验失败，调用方应降级到模板。"""


class TooLong(GreetingError):
    """只是写超了。相比其他校验失败（编造、抄 JD、带联系方式），这一条是程度问题
    而非原则问题——实在改不短时，超一点的针对性招呼语也好过一条通用模板。"""


#: 重试仍超长时，最多容忍到 max_chars 的多少倍。再长就真的像小作文了。
_LEN_TOLERANCE = 1.15


def validate(message: str, job: Job, cfg: GreetingConfig) -> None:
    """校验不通过就抛错。三道：长度、联系方式、照抄 JD。"""
    text = message.strip()
    if not text:
        raise GreetingError("生成结果为空")

    n = len(text)
    if n > cfg.max_chars:
        raise TooLong(f"长度 {n} 超出上限 {cfg.max_chars}，至少还要删掉 {n - cfg.max_chars} 字")
    if n < cfg.min_chars:
        raise GreetingError(f"长度 {n} 不足下限 {cfg.min_chars}")

    for pat in _CONTACT_PATTERNS:
        if m := pat.search(text):
            raise GreetingError(f"包含联系方式：{m.group(0)!r}")

    if job.jd:
        for i in range(0, len(job.jd) - _COPY_CHECK_LEN, 10):
            chunk = job.jd[i : i + _COPY_CHECK_LEN].strip()
            if len(chunk) >= _COPY_CHECK_LEN and chunk in text:
                raise GreetingError(f"照抄了 JD 片段：{chunk[:20]!r}…")


def _explain_status(exc: Exception, base_url: str) -> str:
    """两家 SDK 的 APIStatusError 形状一致，错误说明也用同一套。"""
    try:
        detail = exc.response.json()["error"]["message"]
    except Exception:
        detail = str(exc)[:200]
    # 带上实际请求地址——配了网关时，多数认证问题一眼就能看出发错了地方
    return f"HTTP {exc.status_code}：{detail}（请求地址：{base_url}）"


class _Backend:
    """一家模型厂商的最小接口。子类负责建客户端、发请求、翻译错误。"""

    #: .env 里该配哪几个变量，报错时原样念给用户听
    env_hint = ""
    #: 正式生成时给的 token 预算（推理模型会先吃掉一部分）
    gen_max_tokens = 2000
    #: 重试也不会变好的那类错误（认证、模型不存在……），由子类填真实异常类
    status_error: type[Exception] = Exception

    def set_system(self, text: str) -> None: ...
    def complete(self, user: str) -> str: ...
    def ping(self) -> None: ...
    def explain(self, exc: Exception) -> str: ...


class _AnthropicBackend(_Backend):
    env_hint = "官方 API 填 ANTHROPIC_API_KEY，走网关填 ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN"

    def __init__(self, cfg: GreetingConfig):
        self.cfg = cfg
        try:
            self.client = anthropic.Anthropic(timeout=_REQUEST_TIMEOUT)
        except anthropic.AnthropicError as e:
            # 抓完几十个岗位才发现没配 key 太浪费，在起跑线上就拦住
            raise RuntimeError(
                f"无法初始化 Claude 客户端：{e}\n"
                f"请在项目根目录的 .env 里配置凭据：{self.env_hint}。"
            ) from e
        # 客户端已解析好的最终请求地址，出错时报给用户看
        self.base_url = str(self.client.base_url)
        self.api_errors = (anthropic.APIStatusError, anthropic.APIConnectionError)
        self.status_error = anthropic.APIStatusError

    def set_system(self, text: str) -> None:
        # 简历是每个岗位都一样的前缀，显式打上缓存点
        self.system = [
            {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}
        ]

    def complete(self, user: str) -> str:
        response = self.client.messages.create(
            model=self.cfg.model,
            max_tokens=self.gen_max_tokens,
            # 写一段话属于轻任务，低 effort 足够，且比关掉 thinking 更稳
            output_config={"effort": "low"},
            system=self.system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in response.content if b.type == "text").strip()

    def ping(self) -> None:
        self.client.messages.create(
            model=self.cfg.model,
            max_tokens=16,
            messages=[{"role": "user", "content": "ok"}],
        )

    def explain(self, exc: Exception) -> str:
        if isinstance(exc, anthropic.APIStatusError):
            return _explain_status(exc, self.base_url)
        return f"网络无法连接：{exc}（请求地址：{self.base_url}）"


class _OpenAIBackend(_Backend):
    env_hint = "OPENAI_API_KEY（走第三方兼容网关再加 OPENAI_BASE_URL）"
    # 推理模型会先花掉一截预算才开始写正文，比 Claude 侧留得宽一点
    gen_max_tokens = 4000

    def __init__(self, cfg: GreetingConfig):
        self.cfg = cfg
        try:
            self.client = openai.OpenAI(timeout=_REQUEST_TIMEOUT)
        except openai.OpenAIError as e:
            raise RuntimeError(
                f"无法初始化 OpenAI 客户端：{e}\n"
                f"请在项目根目录的 .env 里配置凭据：{self.env_hint}。"
            ) from e
        self.base_url = str(self.client.base_url)
        self.api_errors = (openai.APIStatusError, openai.APIConnectionError)
        self.status_error = openai.APIStatusError
        # gpt-5 一代只认 max_completion_tokens，老模型和多数第三方兼容网关只认
        # max_tokens。先按新名字发，被顶回来一次就记住换旧名字，省得让用户配。
        self._token_param = "max_completion_tokens"

    def set_system(self, text: str) -> None:
        # OpenAI 侧长前缀是自动缓存的，不需要像 Claude 那样显式标记
        self.system = text

    def _create(self, messages: list[dict], max_tokens: int):
        try:
            return self.client.chat.completions.create(
                model=self.cfg.model,
                messages=messages,
                **{self._token_param: max_tokens},
            )
        except openai.BadRequestError as e:
            if self._token_param == "max_tokens" or "token" not in str(e).lower():
                raise
            self._token_param = "max_tokens"
            return self.client.chat.completions.create(
                model=self.cfg.model,
                messages=messages,
                **{self._token_param: max_tokens},
            )

    def complete(self, user: str) -> str:
        response = self._create(
            [
                {"role": "system", "content": self.system},
                {"role": "user", "content": user},
            ],
            self.gen_max_tokens,
        )
        return (response.choices[0].message.content or "").strip()

    def ping(self) -> None:
        self._create([{"role": "user", "content": "ok"}], 16)

    def explain(self, exc: Exception) -> str:
        if isinstance(exc, openai.APIStatusError):
            msg = _explain_status(exc, self.base_url)
            if "model" in str(exc).lower():
                msg += self._model_hint()
            return msg
        return f"网络无法连接：{exc}（请求地址：{self.base_url}）"

    def _model_hint(self) -> str:
        """兼容网关认的模型名千奇百怪，报错涉及模型时顺手列出来，省得挨个猜。"""
        try:
            ids = sorted(m.id for m in self.client.models.list().data)
        except Exception:
            return ""
        return "\n  该地址可用的模型：" + "、".join(ids[:15]) + ("…" if len(ids) > 15 else "")


BACKENDS = {"anthropic": _AnthropicBackend, "openai": _OpenAIBackend}


class Greeter:
    """简历作为固定前缀走 prompt caching，批量跑时省下大部分输入成本。"""

    def __init__(self, cfg: GreetingConfig, resume_text: str):
        self.cfg = cfg
        backend_cls = BACKENDS[cfg.provider]
        self.env_hint = backend_cls.env_hint
        self.init_error: str | None = None
        self.backend: _Backend | None = None
        # 没配 key 的话客户端当场就建不起来。这里不抛：交给 preflight 统一报，
        # 用户若明确指定了 --allow-fallback，整轮走模板也应该能跑完。
        try:
            self.backend = backend_cls(cfg)
        except RuntimeError as e:
            self.init_error = str(e)
        else:
            self.backend.set_system(
                SYSTEM_PROMPT.format(
                    resume=resume_text,
                    min_chars=cfg.min_chars,
                    max_chars=cfg.max_chars,
                )
            )
        self.base_url = self.backend.base_url if self.backend else ""

    def _once(self, job: Job, extra: str = "") -> str:
        user = USER_PROMPT.format(
            title=job.title,
            company=job.company,
            salary=job.salary_text or "未注明",
            jd=job.jd or "（未能获取到 JD 正文，请仅根据岗位名称与公司名撰写）",
            max_chars=self.cfg.max_chars,
        )
        if extra:
            user += f"\n\n注意：上一次生成不合格（{extra}），请修正后重写。"
        return self.backend.complete(user)

    def preflight(self) -> str | None:
        """开跑前用一次极小的调用确认模型真的可用，不可用则返回原因。

        环境里可能存在无法给本工具用的凭据（例如只认 Claude Code 客户端的代理），
        那种情况下每个岗位都会静默降级成固定模板——等于白发一天的配额。
        """
        if self.init_error:
            return self.init_error
        try:
            self.backend.ping()
        except self.backend.api_errors as e:
            return self.backend.explain(e)
        except Exception as e:  # 凭据缺失等
            return f"{type(e).__name__}: {str(e)[:200]}"
        return None

    def generate(self, job: Job) -> tuple[str, str]:
        """返回 (招呼语, 使用的模型)。两次都不过才降级到模板。

        两处「不轻易放弃」：连接类错误（网关抖一下）值得再试一次，只有认证、
        模型不存在这类重试也没用的状态码才立刻停；写超了的稿子先留着，两次都
        没压进字数时，宁可发一条超一点的针对性招呼语，也不发通用模板。
        """
        if self.init_error:
            return self.fallback(job), "fallback(客户端未初始化)"

        last_err = ""
        overlong = ""  # 内容合格、只是超了字数的备选稿
        for attempt in range(2):
            try:
                text = self._once(job, extra=last_err if attempt else "")
                validate(text, job, self.cfg)
                return text, self.cfg.model
            except TooLong as e:
                last_err = str(e)
                if len(text) <= self.cfg.max_chars * _LEN_TOLERANCE:
                    overlong = text
            except GreetingError as e:
                last_err = str(e)
            except self.backend.api_errors as e:
                last_err = self.backend.explain(e)[:80]
                if isinstance(e, self.backend.status_error):
                    break  # 401 / 模型不存在之类，再试还是这个结果

        if overlong:
            return overlong, f"{self.cfg.model}(超长 {len(overlong)} 字)"
        return self.fallback(job), f"fallback({last_err})"

    def fallback(self, job: Job) -> str:
        return self.cfg.fallback_template.format(
            title=job.title, company=job.company, salary=job.salary_text
        )
