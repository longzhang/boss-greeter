"""用模型读 JD，判断是不是外包/销售岗。

规则黑名单只能匹配字面词，而外包岗的写法千变万化——「入驻合作伙伴团队」
「为客户提供驻场技术支持」这类措辞一个关键词都不命中。这一层补的就是这个缺口：
把 JD 丢给模型做一次二分类，判定为外包或销售就跳过，不打招呼。

只读 JD、只回一个标签，token 用量很小，模型也该选快的那个（见 config 的 ai_screen.model）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .greeter import BACKENDS
from .models import Job

SYSTEM_PROMPT = """你是招聘信息审核员，判断一个岗位是否属于「外包」或「销售」。

判定标准：

**外包（OUTSOURCE）** —— 满足任一即是：
- 公司是人力外包/人才派遣/软件外包/咨询服务公司，招人是为了派到别的甲方公司干活
- JD 里出现驻场、入驻客户、常驻甲方、客户现场办公、项目制交付到客户方
- 公司介绍强调「为企业提供人才服务/技术外包/人力资源解决方案」
注意：自研产品公司里做「交付」「实施」「技术支持」的岗位**不算**外包；
甲方公司招人做自己的项目，即使提到客户，也不算。

**销售（SALES）** —— 满足任一即是：
- 岗位主要职责是卖产品、谈客户、拓展渠道、完成业绩指标
- 售前/售后/解决方案顾问/客户成功/商务拓展这类以客户沟通为主、不写代码的岗位
注意：研发岗里写「配合销售团队」「支持售前」**不算**销售。

**其他一律 OK**，包括普通研发、算法、架构、技术管理岗。

只输出一个词：OUTSOURCE、SALES 或 OK。不要解释，不要标点。"""

USER_PROMPT = """岗位名称：{title}
公司：{company}

岗位 JD：
<jd>
{jd}
</jd>

这个岗位属于 OUTSOURCE、SALES 还是 OK？"""

#: JD 截断长度。判定外包/销售的线索基本都在开头的公司介绍和职责里，
#: 全文丢进去只是多花钱多等待。
_JD_LIMIT = 1500

#: 判定用的 token 预算。只要一个词，给多了也用不上；
#: 但推理模型会先花掉一截，所以不能给太死。
_MAX_TOKENS = 512

LABELS = {"OUTSOURCE": "外包岗", "SALES": "销售岗"}


@dataclass
class Verdict:
    """判定结果。`ok=False` 表示该跳过这个岗位。"""

    ok: bool
    label: str = ""
    reason: str = ""
    #: 模型没能给出判定（网关挂了、超时……）。调用方据此决定放行还是计数报警。
    errored: bool = False


class Screener:
    """JD 外包/销售判定。一个岗位一次调用，失败放行（由调用方计数）。"""

    def __init__(self, provider: str, model: str):
        self.model = model
        backend_cls = BACKENDS[provider]
        self.env_hint = backend_cls.env_hint
        self.init_error: str | None = None
        self.backend = None
        try:
            # 复用 greeter 那套 backend：同样读 .env、同样的错误翻译
            self.backend = backend_cls(_ModelOnly(model))
        except RuntimeError as e:
            self.init_error = str(e)
            return
        self.backend.gen_max_tokens = _MAX_TOKENS
        self.backend.set_system(SYSTEM_PROMPT)

    @property
    def base_url(self) -> str:
        return self.backend.base_url if self.backend else ""

    def preflight(self) -> str | None:
        """开跑前确认判定模型可用，不可用返回原因。"""
        if self.init_error:
            return self.init_error
        try:
            self.backend.ping()
        except self.backend.api_errors as e:
            return self.backend.explain(e)
        except Exception as e:
            return f"{type(e).__name__}: {str(e)[:200]}"
        return None

    def check(self, job: Job) -> Verdict:
        if self.init_error:
            return Verdict(True, errored=True, reason=self.init_error[:80])
        if not job.jd:
            # 没 JD 就没得判，交给后面的规则；这不算模型出错
            return Verdict(True, reason="无 JD，未判定")

        user = USER_PROMPT.format(
            title=job.title, company=job.company, jd=job.jd[:_JD_LIMIT]
        )
        try:
            raw = self.backend.complete(user)
        except Exception as e:
            return Verdict(True, errored=True, reason=f"{type(e).__name__}: {str(e)[:60]}")

        return self._parse(raw)

    @staticmethod
    def _parse(raw: str) -> Verdict:
        """模型偶尔会带标点或多写一句，取第一个出现的标签。"""
        text = (raw or "").strip().upper()
        if not text:
            return Verdict(True, errored=True, reason="模型返回空")
        # 用「哪个标签先出现」判断，避免 "OK，不是 OUTSOURCE" 被误读成外包
        hits = [(text.find(k), k) for k in ("OUTSOURCE", "SALES", "OK") if k in text]
        if not hits:
            return Verdict(True, errored=True, reason=f"无法解析：{text[:40]!r}")
        _, label = min(hits)
        if label == "OK":
            return Verdict(True)
        return Verdict(False, label=label, reason=LABELS[label])


class _ModelOnly:
    """backend 只用到 cfg.model 这一个字段，不必拖一整个 GreetingConfig 进来。"""

    def __init__(self, model: str):
        self.model = model
