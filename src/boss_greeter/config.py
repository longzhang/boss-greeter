"""配置加载：config.yaml（用户策略）+ selectors.yaml（DOM 选择器）。"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path

import yaml
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
PROFILE_DIR = DATA_DIR / "profile"
# 接管模式下那个「专供自动化」的 Chrome 用户目录。
# Chrome 136 起不允许对默认用户目录开调试端口，所以必须单独给一个。
CHROME_PROFILE_DIR = DATA_DIR / "chrome-profile"
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "boss.db"


class SearchConfig(BaseModel):
    keywords: list[str]
    city: str = "北京"
    max_pages: int = 5


class TopicFilterConfig(BaseModel):
    enabled: bool = True
    #: 岗位名或技能标签命中任一即通过；留空表示不限方向
    allow: list[str] = Field(default_factory=list)


class JobSourceFilterConfig(BaseModel):
    enabled: bool = True
    #: 猎头/代招岗位（卡片上那个「猎头」角标）
    exclude_headhunter: bool = True
    #: 在校/应届岗位
    exclude_campus: bool = True


class RndOnlyFilterConfig(BaseModel):
    enabled: bool = True
    #: 留空则用 filters.NON_RND_TITLES 那份默认词表
    deny: list[str] = Field(default_factory=list)


class ActivityFilterConfig(BaseModel):
    enabled: bool = True
    allow: list[str] = Field(default_factory=list)


class SalaryFilterConfig(BaseModel):
    enabled: bool = True
    min_k: int = 0
    allow_negotiable: bool = False


class CityFilterConfig(BaseModel):
    enabled: bool = True
    allow: list[str] = Field(default_factory=list)


class EducationFilterConfig(BaseModel):
    enabled: bool = True
    max_required: str = "本科"


class CompanySizeFilterConfig(BaseModel):
    enabled: bool = True
    allow: list[str] = Field(default_factory=list)


class KeywordFilterConfig(BaseModel):
    enabled: bool = True
    blacklist: list[str] = Field(default_factory=list)
    whitelist: list[str] = Field(default_factory=list)
    blacklist_fields: list[str] = Field(default_factory=lambda: ["title", "company", "jd"])


class FiltersConfig(BaseModel):
    job_source: JobSourceFilterConfig = JobSourceFilterConfig()
    rnd_only: RndOnlyFilterConfig = RndOnlyFilterConfig()
    topic: TopicFilterConfig = TopicFilterConfig()
    activity: ActivityFilterConfig = ActivityFilterConfig()
    salary: SalaryFilterConfig = SalaryFilterConfig()
    city: CityFilterConfig = CityFilterConfig()
    education: EducationFilterConfig = EducationFilterConfig()
    company_size: CompanySizeFilterConfig = CompanySizeFilterConfig()
    keyword: KeywordFilterConfig = KeywordFilterConfig()


# 不写 model 时按 provider 取的默认值
DEFAULT_MODELS = {"anthropic": "claude-opus-5", "openai": "gpt-5.4"}


class GreetingConfig(BaseModel):
    resume_path: Path
    provider: Literal["anthropic", "openai"] = "anthropic"
    #: 留空则按 provider 取 DEFAULT_MODELS
    model: str = ""
    min_chars: int = 60
    max_chars: int = 200
    fallback_template: str = "您好，看到贵司的{title}岗位，我的背景与之比较契合，方便简单聊聊吗？"

    @model_validator(mode="after")
    def _model_matches_provider(self) -> "GreetingConfig":
        if not self.model:
            self.model = DEFAULT_MODELS[self.provider]
            return self
        # 只拦最常见的改了一半：换了 provider 忘了换 model
        wrong = {"anthropic": "gpt", "openai": "claude"}[self.provider]
        if self.model.lower().startswith(wrong):
            raise ValueError(
                f"greeting.provider 是 {self.provider}，model 却填了 {self.model!r}。"
                f"改成 {DEFAULT_MODELS[self.provider]!r} 之类的同家模型，或把 provider 改回去。"
            )
        return self

    @field_validator("resume_path")
    @classmethod
    def _resume_must_exist(cls, v: Path) -> Path:
        if not v.expanduser().is_file():
            raise ValueError(f"简历文件不存在：{v}")
        return v.expanduser()


class AiScreenConfig(BaseModel):
    """用模型读 JD，判定外包/销售岗。规则黑名单只能匹字面词，这一层补语义。"""

    enabled: bool = False
    #: 留空则跟 greeting.provider 走
    provider: Literal["anthropic", "openai", ""] = ""
    #: 留空则跟 greeting.model 走。判定是轻任务，建议单独指一个快模型。
    model: str = ""
    #: 判定哪些类别要跳过。可选 OUTSOURCE / SALES
    skip: list[str] = Field(default_factory=lambda: ["OUTSOURCE", "SALES"])
    #: 连续多少次判定失败（网关挂了之类）就停整轮。
    #: 失败是放行的，一直失败等于这层形同虚设，不如早点停下来告诉用户。
    max_consecutive_failures: int = 5


class PacingConfig(BaseModel):
    daily_limit: int = 50
    action_delay: tuple[float, float] = (3.0, 8.0)
    #: 两次「发送」之间的间隔，比 action_delay 长得多。
    #: 用模型写招呼语时每个岗位本来就要等十几秒到一分钟，无形中拉开了节奏；
    #: --default-greeting 不调模型，少了这段自然停顿，必须显式补上。
    send_delay: tuple[float, float] = (25.0, 70.0)
    batch_size: tuple[int, int] = (5, 8)
    batch_rest: tuple[float, float] = (300.0, 900.0)
    max_consecutive_failures: int = 3


class BrowserConfig(BaseModel):
    #: 接管一个你自己开着的 Chrome（CDP 地址，如 "http://127.0.0.1:9222"）。
    #: 填了就不再由本工具启浏览器——登录完全由你手动完成，工具只是连上去干活，
    #: 这是目前最不容易被 BOSS 识破的方式。留空则退回自启浏览器。
    attach_cdp: str = ""
    #: `boss chrome` 拿来启动 Chrome 的可执行文件与用户目录（仅接管模式用到）。
    chrome_path: str = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    #: 留空则用 data/chrome-profile
    chrome_user_data_dir: str = ""
    #: 自启模式下用本机真实 Chrome（"chrome"）还是 Playwright 自带的 Chromium（留空）。
    #: 实测 BOSS 登录页会把自带 Chromium 直接刷成空白，真实 Chrome 才渲染二维码。
    channel: str = "chrome"
    headless: bool = False
    slow_mo_ms: int = 80
    viewport: tuple[int, int] = (1440, 900)
    nav_timeout_ms: int = 30000

    @property
    def user_data_dir(self) -> Path:
        return Path(self.chrome_user_data_dir).expanduser() if self.chrome_user_data_dir \
            else CHROME_PROFILE_DIR

    @property
    def debug_port(self) -> int:
        """从 attach_cdp 里抠出端口号，给 `boss chrome` 的启动命令用。"""
        tail = self.attach_cdp.rsplit(":", 1)[-1].strip("/")
        return int(tail) if tail.isdigit() else 9222


class Config(BaseModel):
    search: SearchConfig
    filters: FiltersConfig = FiltersConfig()
    greeting: GreetingConfig
    ai_screen: AiScreenConfig = AiScreenConfig()
    pacing: PacingConfig = PacingConfig()
    browser: BrowserConfig = BrowserConfig()

    @model_validator(mode="after")
    def _screen_defaults_to_greeting(self) -> "Config":
        """ai_screen 不写 provider/model 就跟 greeting 走，省得两处都要配。"""
        if not self.ai_screen.provider:
            self.ai_screen.provider = self.greeting.provider
        if not self.ai_screen.model:
            self.ai_screen.model = self.greeting.model
        return self

    @cached_property
    def resume_text(self) -> str:
        return self.greeting.resume_path.read_text(encoding="utf-8")

    model_config = {"ignored_types": (cached_property,)}


class Selectors:
    """selectors.yaml 的薄封装。每个字段是候选列表，调用方按顺序尝试。"""

    def __init__(self, raw: dict):
        self._raw = raw

    def get(self, path: str) -> list[str]:
        """按 'list.title' 这样的点号路径取候选列表。"""
        node = self._raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                raise KeyError(f"selectors.yaml 缺少 {path!r}（断在 {part!r}）")
            node = node[part]
        if isinstance(node, str):
            return [node]
        if not isinstance(node, list):
            raise TypeError(f"selectors.yaml 的 {path!r} 应为字符串或列表，实际是 {type(node).__name__}")
        return node

    @property
    def risk_texts(self) -> list[str]:
        return self._raw.get("risk_texts", [])

    @property
    def sent_dialog_texts(self) -> list[str]:
        """点「立即沟通」后那个确认框的文案。命中即说明招呼语已经发出去了。"""
        return self._raw.get("chat", {}).get("sent_dialog_texts", [])

    #: 这几个字段存的是页面文案而不是选择器，doctor 别拿去当选择器验
    _TEXT_FIELDS = ("risk_texts", "chat.sent_dialog_texts")

    def iter_all(self):
        """遍历所有 (点号路径, 候选列表)，供 doctor 自检。"""

        def walk(node, prefix: str):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield from walk(v, f"{prefix}.{k}" if prefix else k)
            elif isinstance(node, list) and all(isinstance(x, str) for x in node):
                if prefix not in self._TEXT_FIELDS:
                    yield prefix, node

        yield from walk(self._raw, "")


def load_config(path: Path | None = None) -> Config:
    path = path or PROJECT_ROOT / "config.yaml"
    return Config(**yaml.safe_load(path.read_text(encoding="utf-8")))


def load_selectors(path: Path | None = None) -> Selectors:
    path = path or PROJECT_ROOT / "selectors.yaml"
    return Selectors(yaml.safe_load(path.read_text(encoding="utf-8")))
