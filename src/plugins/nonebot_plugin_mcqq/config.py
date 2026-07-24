"""
配置文件
"""

import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from nonebot import logger
from nonebot.compat import PYDANTIC_V2
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    # 静态检查同时声明两个兼容装饰器，运行时仍按 Pydantic 版本选择。
    from pydantic import field_validator, validator

    from .utils.sensitive_words import SensitiveWordRuntimeSnapshot
elif PYDANTIC_V2:
    from pydantic import field_validator
else:
    from pydantic import validator

from .data_source import IGNORE_WORD_LIST, IGNORE_WORD_REPLACEMENTS

MAX_COMMAND_PRIORITY = 98
DEFAULT_IGNORE_WORD_MODE = "replace"
DEFAULT_IGNORE_WORD_REPLACEMENT = "***"
VALID_IGNORE_WORD_MODES = {"block", "replace"}


class Guild(BaseModel):
    """频道配置"""

    channel_id: str
    """子频道号"""
    adapter: str
    """适配器类型"""
    bot_id: str = ""
    """兼容旧配置的首选 Bot ID"""
    bot_ids: list[str] = Field(default_factory=list)
    """按顺序参与发送轮换的 Bot ID"""

    @property
    def candidate_bot_ids(self) -> list[str]:
        """合并新旧配置并保持 Bot 的配置顺序。"""
        return list(dict.fromkeys(filter(None, (self.bot_id, *self.bot_ids))))


class Group(BaseModel):
    """群配置"""

    group_id: str
    """群号"""
    adapter: str
    """适配器类型"""
    bot_id: str = ""
    """兼容旧配置的首选 Bot ID"""
    bot_ids: list[str] = Field(default_factory=list)
    """按顺序参与发送轮换的 Bot ID"""

    @property
    def candidate_bot_ids(self) -> list[str]:
        """合并新旧配置并保持 Bot 的配置顺序。"""
        return list(dict.fromkeys(filter(None, (self.bot_id, *self.bot_ids))))


class BotRateLimit(BaseModel):
    """单个 QQ Bot 的 MC→QQ 发送频率限制。"""

    rpm: int = Field(default=0, ge=0)
    """60 秒滑动窗口内的发送上限，0 表示不限。"""
    rph: int = Field(default=0, ge=0)
    """3600 秒滑动窗口内的发送上限，0 表示不限。"""


class Server(BaseModel):
    """服务器配置"""

    nickname: str = ""
    """服务器昵称"""
    group_list: list[Group] = Field(default_factory=list)
    """群列表"""
    guild_list: list[Guild] = Field(default_factory=list)
    """频道列表"""
    rcon_msg: bool = False
    """是否用Rcon发送消息"""
    forward_batch_header: str = ""
    """OneBot 合并转发消息的首个提示节点，空字符串表示关闭。"""


class MCQQConfig(BaseModel):
    """配置"""

    command_header: Any = {"mcc"}
    """命令头"""

    ignore_message_header: Any = {""}
    """忽略消息头"""

    ignore_word_file: str = "./src/mc_qq_ignore_word_list.json"
    """敏感词文件路径"""

    ignore_word_list: set[str] = Field(default_factory=set)
    """忽略的敏感词列表"""

    ignore_word_mode: str = DEFAULT_IGNORE_WORD_MODE
    """敏感词处理模式：block 屏蔽整条消息，replace 替换命中内容。"""

    ignore_word_replacement: str = DEFAULT_IGNORE_WORD_REPLACEMENT
    """没有逐词映射时使用的全局替换文本。"""

    ignore_word_replacements: dict[str, str] = Field(default_factory=dict)
    """敏感词到替换文本的逐词映射；映射键会自动加入敏感词列表。"""

    command_priority: int = 98
    """命令优先级，1-98，消息优先级=命令优先级 - 1"""

    command_block: bool = True
    """命令消息是否阻断后续消息"""

    notice_connected: bool = False
    """是否在服务器连接状态变化时发送通知"""

    rcon_result_to_image: bool = False
    """是否将 Rcon 命令执行结果转换为图片"""

    achievement_to_image: bool = True
    """是否将成就消息转换为图片"""

    ttf_path: Path = Path(__file__).parent / "resource" / "unifont-15.0.01.ttf"
    """字体路径"""

    send_group_name: bool = False
    """是否发送群聊名称"""

    display_server_name: bool = False
    """是否发送服务器名称"""

    say_way: str = " "
    """用户发言修饰"""

    username_way: list[str] = ["<", ">"]
    """用户名修饰"""

    server_dict: dict[str, Server] = Field(default_factory=dict)
    """服务器配置"""

    guild_admin_roles: list[str] = ["频道主", "超级管理员"]
    """频道管理员角色"""

    chat_image_enable: bool = False
    """是否启用 ChatImage MOD"""

    cmd_whitelist: set[str] = {"list", "tps", "banlist"}
    """命令白名单"""

    bot_rate_limits: dict[str, BotRateLimit] = Field(default_factory=dict)
    """每个 QQ Bot 的 RPM/RPH 限制；未配置的 Bot 不限流。"""

    send_batch_max_messages: int = Field(default=40, ge=1)
    """单批合并的原消息上限，不包含提示节点。"""

    send_route_queue_max_messages: int = Field(default=200, ge=1)
    """单个“服务器→目标”路由的积压消息上限。"""

    send_global_queue_max_messages: int = Field(default=1000, ge=1)
    """所有发送路由合计的积压消息上限。"""

    @classmethod
    def _get_common_set(
        cls,
        v: Any,
        configuration_name: str,
        default_config: set[Any] | None = None,
    ) -> set[Any]:
        default_config = default_config or set()
        if isinstance(v, str):
            logger.info(f"{configuration_name} is a string, use it as is.")
            return {v}
        if isinstance(v, list | set):
            logger.info(f"{configuration_name} is a list, loaded {len(v)} items.")
            return set(v)
        logger.warning(
            f"Invalid type for {configuration_name}: {type(v)}, use empty list."
        )
        return default_config

    @(
        field_validator("command_header", mode="before")
        if PYDANTIC_V2
        else validator("command_header", pre=True, always=True)
    )
    @classmethod
    def validate_command_header(cls, v: Any) -> set[str]:
        return cls._get_common_set(v, "command_header", {"mcqq"})

    @(
        field_validator("ignore_message_header", mode="before")
        if PYDANTIC_V2
        else validator("ignore_message_header", pre=True, always=True)
    )
    @classmethod
    def validate_ignore_message_header(cls, v: Any) -> set[str]:
        return cls._get_common_set(v, "ignore_message_header")

    @(
        field_validator("ignore_word_list", mode="before")
        if PYDANTIC_V2
        else validator("ignore_word_list", pre=True, always=True)
    )
    @classmethod
    def validate_ignore_word_list(cls, v: Any) -> set[str]:
        return cls._get_common_set(v, "ignore_word_list")

    @(
        field_validator("ignore_word_mode", mode="before")
        if PYDANTIC_V2
        else validator("ignore_word_mode", pre=True, always=True)
    )
    @classmethod
    def validate_ignore_word_mode(cls, v: Any) -> str:
        if isinstance(v, str) and v.strip().lower() in VALID_IGNORE_WORD_MODES:
            return v.strip().lower()
        logger.warning(
            f"Invalid ignore_word_mode: {v!r}, use {DEFAULT_IGNORE_WORD_MODE}."
        )
        return DEFAULT_IGNORE_WORD_MODE

    @(
        field_validator("ignore_word_replacement", mode="before")
        if PYDANTIC_V2
        else validator("ignore_word_replacement", pre=True, always=True)
    )
    @classmethod
    def validate_ignore_word_replacement(cls, v: Any) -> str:
        if isinstance(v, str):
            return v
        logger.warning(
            f"Invalid ignore_word_replacement, use {DEFAULT_IGNORE_WORD_REPLACEMENT!r}."
        )
        return DEFAULT_IGNORE_WORD_REPLACEMENT

    @(
        field_validator("ignore_word_replacements", mode="before")
        if PYDANTIC_V2
        else validator("ignore_word_replacements", pre=True, always=True)
    )
    @classmethod
    def validate_ignore_word_replacements(cls, v: Any) -> dict[str, str]:
        if not isinstance(v, dict):
            logger.warning("Invalid ignore_word_replacements, use empty mapping.")
            return {}

        replacements: dict[str, str] = {}
        for word, replacement in v.items():
            if not isinstance(word, str) or not word.strip():
                logger.warning("Ignore an empty or non-string sensitive word mapping.")
                continue
            replacement_value = replacement
            if not isinstance(replacement, str):
                logger.warning(
                    f"Invalid replacement for sensitive word {word!r}, "
                    f"use {DEFAULT_IGNORE_WORD_REPLACEMENT!r}."
                )
                replacement_value = DEFAULT_IGNORE_WORD_REPLACEMENT
            replacements[word] = replacement_value
        return replacements

    @(
        field_validator("command_priority", mode="before")
        if PYDANTIC_V2
        else validator("command_priority", pre=True, always=True)
    )
    @classmethod
    def validate_priority(cls, v: int) -> int:
        if 1 <= v <= MAX_COMMAND_PRIORITY:
            return v
        logger.warning("Invalid command_priority, use default 98.")
        return MAX_COMMAND_PRIORITY

    @(
        field_validator("rcon_result_to_image", mode="before")
        if PYDANTIC_V2
        else validator("rcon_result_to_image", pre=True, always=True)
    )
    @classmethod
    def validate_rcon_result_to_image(cls, v: bool) -> bool:  # noqa: FBT001
        is_pil_exists: bool = importlib.util.find_spec("PIL") is not None
        if v and not is_pil_exists:
            logger.warning(
                "Pillow not installed, please install it to use rcon result to image."
            )
            return False
        return v

    @(
        field_validator("ttf_path", mode="before")
        if PYDANTIC_V2
        else validator("ttf_path", pre=True, always=True)
    )
    @classmethod
    def validate_ttf_path(cls, v: str) -> Path:
        if v:
            if Path(v).exists():
                logger.info(f"ttf_path {v} exists, use it.")
                return Path(v)
            logger.warning(f"ttf_path {v} not exists, please check your config.")
        else:
            logger.warning("ttf_path not set, use default.")
        return Path(__file__).parent / "unifont-15.0.01.ttf"


class Config(BaseModel):
    """配置项"""

    mc_qq: MCQQConfig = MCQQConfig()


config_path = Path("config/mc_qq.yaml")
if not config_path.parent.exists():
    config_path.parent.mkdir(parents=True, exist_ok=True)

if not config_path.exists():
    alt_path = Path("mc_qq.yaml")
    if alt_path.exists():
        config_path = alt_path
    else:
        try:
            import json

            if PYDANTIC_V2:
                default_data = json.loads(MCQQConfig().model_dump_json())
            else:
                default_data = json.loads(MCQQConfig().json())
            with config_path.open("w", encoding="utf-8") as f:
                yaml.safe_dump(default_data, f, allow_unicode=True, sort_keys=False)
            logger.info(f"MCQQ 插件已在 {config_path} 自动生成默认配置文件")
        except Exception as e:  # noqa: BLE001
            logger.error(f"生成默认配置文件失败：{e}")

plugin_config = MCQQConfig()
loaded_from_nonebot = False
try:
    from nonebot import get_driver

    driver = get_driver()
    mc_qq_raw = getattr(driver.config, "mc_qq", None)
    if mc_qq_raw:
        if isinstance(mc_qq_raw, dict):
            if PYDANTIC_V2:
                plugin_config = MCQQConfig.model_validate(mc_qq_raw)
            else:
                plugin_config = MCQQConfig.parse_obj(mc_qq_raw)
            loaded_from_nonebot = True
        elif isinstance(mc_qq_raw, MCQQConfig):
            plugin_config = mc_qq_raw
            loaded_from_nonebot = True
        elif hasattr(mc_qq_raw, "model_dump"):
            plugin_config = MCQQConfig.model_validate(mc_qq_raw.model_dump())
            loaded_from_nonebot = True
        elif hasattr(mc_qq_raw, "dict"):
            plugin_config = MCQQConfig.parse_obj(mc_qq_raw.dict())
            loaded_from_nonebot = True
        if loaded_from_nonebot:
            logger.info("MCQQ 插件成功从 NoneBot 配置加载 mc_qq 项")
except Exception as e:  # noqa: BLE001
    logger.debug(f"从 NoneBot 配置加载 mc_qq 失败：{e}")

if not loaded_from_nonebot and config_path.exists():
    try:
        with config_path.open(encoding="utf-8") as f:
            yaml_data = yaml.safe_load(f) or {}
        if PYDANTIC_V2:
            plugin_config = MCQQConfig.model_validate(yaml_data)
        else:
            plugin_config = MCQQConfig.parse_obj(yaml_data)
        logger.info(f"MCQQ 插件成功加载配置文件：{config_path}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"加载配置文件 {config_path} 失败，已使用默认配置。错误信息：{e}")

@dataclass(frozen=True, slots=True)
class _ExternalSensitiveWords:
    """一个具体 JSON 路径最近一次成功解析出的不可变词库。"""

    path: Path
    words: tuple[str, ...]
    replacements: tuple[tuple[str, str], ...]


_last_valid_external_words: _ExternalSensitiveWords | None = None


@dataclass(frozen=True, slots=True)
class _SensitiveWordCandidate:
    """已完成解析和编译、尚未发布的敏感词运行态。"""

    runtime: "SensitiveWordRuntimeSnapshot"
    external_words: _ExternalSensitiveWords | None


def _collect_valid_words(raw_words: Any, source: str) -> list[str]:
    """校验外部词条，防止空词命中所有消息或非法类型破坏匹配器。"""
    if not isinstance(raw_words, list | set | tuple):
        if raw_words:
            logger.warning(f"{source} 的 words 必须是数组，已忽略")
        return []

    words: list[str] = []
    for word in raw_words:
        if isinstance(word, str) and word.strip():
            words.append(word)
        else:
            logger.warning(f"{source} 中存在空词或非字符串词条，已忽略")
    return words


def _collect_valid_replacements(raw_replacements: Any, source: str) -> dict[str, str]:
    """校验逐词替换映射；空字符串替换值是合法的删除操作。"""
    if not isinstance(raw_replacements, dict):
        if raw_replacements:
            logger.warning(f"{source} 的 replacements 必须是对象，已忽略")
        return {}

    replacements: dict[str, str] = {}
    for word, replacement in raw_replacements.items():
        if not isinstance(word, str) or not word.strip():
            logger.warning(f"{source} 中存在空词或非字符串映射键，已忽略")
            continue
        replacement_value = replacement
        if not isinstance(replacement, str):
            logger.warning(
                f"{source} 中 {word!r} 的替换值不是字符串，"
                f"已回退为 {DEFAULT_IGNORE_WORD_REPLACEMENT!r}"
            )
            replacement_value = DEFAULT_IGNORE_WORD_REPLACEMENT
        replacements[word] = replacement_value
    return replacements


def _normalized_sensitive_word_path(path: str) -> Path:
    return Path(path).resolve(strict=False)


def _read_external_sensitive_words(path: Path) -> _ExternalSensitiveWords:
    """读取一个完整 JSON 候选；格式错误由调用方决定是否回退。"""
    if not path.exists():
        logger.info(f"敏感词文件不存在，按空外部词库处理：{path}")
        return _ExternalSensitiveWords(path, (), ())

    with path.open(encoding="utf-8") as file:
        json_data = json.load(file)
    if not isinstance(json_data, dict):
        msg = "敏感词 JSON 根节点必须是对象"
        raise TypeError(msg)

    json_words = _collect_valid_words(json_data.get("words", []), "敏感词 JSON")
    json_replacements = _collect_valid_replacements(
        json_data.get("replacements", {}),
        "敏感词 JSON",
    )
    logger.info(
        "加载敏感词文件成功，"
        f"普通词数量为 {len(json_words)}，逐词映射数量为 {len(json_replacements)}"
    )
    return _ExternalSensitiveWords(
        path=path,
        words=tuple(json_words),
        replacements=tuple(json_replacements.items()),
    )


def _merge_sensitive_words(
    mcqq_config: MCQQConfig,
    external_words: _ExternalSensitiveWords | None,
) -> tuple[set[str], dict[str, str]]:
    """合并 YAML 与已校验 JSON；YAML 的逐词映射拥有更高优先级。"""
    words = set(_collect_valid_words(mcqq_config.ignore_word_list, "mc_qq.yaml"))
    replacements = dict(mcqq_config.ignore_word_replacements)
    words.update(replacements)

    if external_words is None:
        return words, replacements

    words.update(external_words.words)
    words.update(word for word, _replacement in external_words.replacements)
    # setdefault 保证 YAML 中同名映射覆盖 JSON，同时保留两边的声明顺序。
    for word, replacement in external_words.replacements:
        replacements.setdefault(word, replacement)
    return words, replacements


def _load_sensitive_words(
    mcqq_config: MCQQConfig,
) -> tuple[set[str], dict[str, str]]:
    """兼容的独立加载入口；失败时返回 YAML 词库且不改变运行时缓存。"""
    path = _normalized_sensitive_word_path(mcqq_config.ignore_word_file)
    try:
        external_words = _read_external_sensitive_words(path)
    except Exception as error:  # noqa: BLE001
        logger.error(f"加载敏感词文件失败，已仅使用 YAML 词库：{error}")
        external_words = None
    return _merge_sensitive_words(mcqq_config, external_words)


def _prepare_sensitive_words(
    mcqq_config: MCQQConfig,
    *,
    reload_external: bool,
    preserve_on_external_error: bool = False,
    prewarm_phonetic: bool,
) -> _SensitiveWordCandidate | None:
    """
    完整构建候选快照，但不改变当前运行态。

    JSON 文件事件解析失败时直接保留上一运行态；YAML 启动或切换到一个尚无
    有效快照的新路径时，则安全地退回 YAML 词库。
    """
    from .utils.sensitive_words import build_sensitive_runtime

    path = _normalized_sensitive_word_path(mcqq_config.ignore_word_file)
    cached_external = _last_valid_external_words
    should_read_external = (
        reload_external or cached_external is None or cached_external.path != path
    )
    external_words: _ExternalSensitiveWords | None

    if should_read_external:
        try:
            external_words = _read_external_sensitive_words(path)
        except Exception as error:  # noqa: BLE001
            if preserve_on_external_error:
                logger.error(f"敏感词 JSON 热重载失败，已保留当前词库和过滤器：{error}")
                return None
            logger.error(f"敏感词 JSON 无可用快照，已仅使用 YAML 词库：{error}")
            external_words = None
    else:
        external_words = cached_external

    words, replacements = _merge_sensitive_words(mcqq_config, external_words)
    try:
        runtime = build_sensitive_runtime(
            words,
            replacements,
            mode=mcqq_config.ignore_word_mode,
            default_replacement=mcqq_config.ignore_word_replacement,
            prewarm_phonetic=prewarm_phonetic,
        )
    except Exception:  # noqa: BLE001
        logger.exception("构建敏感词候选过滤器失败，已保留上一运行态")
        return None

    return _SensitiveWordCandidate(runtime, external_words)


def _commit_sensitive_words(candidate: _SensitiveWordCandidate) -> None:
    """以一个运行时引用为发布点，再同步只用于兼容的可变全局数据。"""
    global _last_valid_external_words  # noqa: PLW0603

    from .utils.sensitive_words import publish_sensitive_runtime

    runtime = candidate.runtime
    publish_sensitive_runtime(runtime)
    IGNORE_WORD_LIST.clear()
    IGNORE_WORD_LIST.update(runtime.words)
    IGNORE_WORD_REPLACEMENTS.clear()
    IGNORE_WORD_REPLACEMENTS.update(runtime.replacements)
    _last_valid_external_words = candidate.external_words

    if runtime.words:
        logger.info(f"加载敏感词成功，敏感词总数为 {len(runtime.words)}")
    else:
        logger.info("敏感词列表为空，不启用过滤")


def _publish_sensitive_words(
    mcqq_config: MCQQConfig,
    *,
    reload_external: bool,
    preserve_on_external_error: bool = False,
    prewarm_phonetic: bool,
) -> bool:
    candidate = _prepare_sensitive_words(
        mcqq_config,
        reload_external=reload_external,
        preserve_on_external_error=preserve_on_external_error,
        prewarm_phonetic=prewarm_phonetic,
    )
    if candidate is None:
        return False
    _commit_sensitive_words(candidate)
    return True


_publish_sensitive_words(
    plugin_config,
    reload_external=True,
    prewarm_phonetic=False,
)
