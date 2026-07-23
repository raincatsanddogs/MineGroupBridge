"""
配置文件
"""

import importlib.util
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from nonebot import logger
from nonebot.compat import PYDANTIC_V2
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    # 静态检查同时声明两个兼容装饰器，运行时仍按 Pydantic 版本选择。
    from pydantic import field_validator, validator
elif PYDANTIC_V2:
    from pydantic import field_validator
else:
    from pydantic import validator

from .data_source import IGNORE_WORD_LIST

MAX_COMMAND_PRIORITY = 98


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

    ignore_word_list: set[str] = set()
    """忽略的敏感词列表"""

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

if plugin_config.ignore_word_list:
    IGNORE_WORD_LIST.add(*plugin_config.ignore_word_list)
    logger.info("加载敏感词列表成功")
else:
    logger.info("敏感词列表为空，不加载")

ignore_word_path = Path(plugin_config.ignore_word_file)
if ignore_word_path.exists():
    try:
        with ignore_word_path.open(encoding="utf-8") as f:
            json_data = json.load(f)
            words = json_data.get("words", [])
            if words:
                IGNORE_WORD_LIST.update(words)
                logger.info(f"加载敏感词文件成功，敏感词数量为 {len(words)}")
            else:
                logger.warning("敏感词文件为空，不加载")
    except Exception as e:  # noqa: BLE001
        logger.error(f"加载敏感词文件失败，请检查文件格式，错误信息为：{e}")
else:
    logger.info("敏感词文件不存在，不加载")
