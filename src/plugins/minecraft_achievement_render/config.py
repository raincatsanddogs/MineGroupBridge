import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from nonebot import logger  # pyright: ignore[reportMissingImports]
from nonebot.compat import PYDANTIC_V2  # pyright: ignore[reportMissingImports]
from pydantic import BaseModel, Field

from src.config_reload import config_reload_service, replace_model_state


class Config(BaseModel):
    """配置"""

    command_header: Any = Field(default_factory=lambda: {"render"})
    """命令头"""

    res_path_prefix: str = ""
    """资源路径前缀"""

    component_asset_version: str = Field(default="1.21.4", min_length=1)
    """Minecraft 原版组件纹理版本"""


def _validate_config(data: Any) -> Config:
    if PYDANTIC_V2:
        return Config.model_validate(data)
    return Config.parse_obj(data)


def _resolve_config_path() -> Path:
    primary = Path("config/render.yaml")
    primary.parent.mkdir(parents=True, exist_ok=True)
    if primary.exists():
        return primary

    alternative = Path("render.yaml")
    if alternative.exists():
        return alternative

    try:
        default_config = Config()
        default_data = (
            json.loads(default_config.model_dump_json())
            if PYDANTIC_V2
            else json.loads(default_config.json())
        )
        with primary.open("w", encoding="utf-8") as file:
            yaml.safe_dump(default_data, file, allow_unicode=True, sort_keys=False)
        logger.info(f"render插件已在 {primary} 自动生成默认配置文件")
    except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
        logger.error(f"生成默认配置文件失败：{error}")
    return primary


def _require_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    msg = "YAML 根节点必须是对象"
    raise TypeError(msg)


def _load_config(path: Path) -> Config:
    try:
        with path.open(encoding="utf-8") as file:
            yaml_data = yaml.safe_load(file) or {}
        config = _validate_config(_require_mapping(yaml_data))
    except (OSError, TypeError, ValueError, UnicodeError, yaml.YAMLError) as error:
        logger.error(
            f"render插件加载配置文件 {path} 失败，已使用默认配置。错误信息：{error}",
        )
        return Config()

    logger.info(f"render插件成功加载配置文件：{path}")
    return config


def _reload_render_config(_path: Path) -> None:
    candidate = _load_config(config_path)
    requested_command_header = candidate.command_header
    if requested_command_header != _startup_command_header:
        if (
            "command_header" not in _restart_warning_values
            or _restart_warning_values["command_header"] != requested_command_header
        ):
            _restart_warning_values["command_header"] = deepcopy(
                requested_command_header
            )
            logger.warning("render.command_header 需重启后生效，本次热重载已保留当前值")
        candidate.command_header = deepcopy(_startup_command_header)
    else:
        _restart_warning_values.pop("command_header", None)

    if candidate == plugin_config:
        logger.debug("render 配置内容未发生有效变化，跳过热重载")
        return
    replace_model_state(plugin_config, candidate)
    logger.info("render 配置热重载成功")


config_path = _resolve_config_path()
plugin_config = _load_config(config_path)
_startup_command_header = deepcopy(plugin_config.command_header)
_restart_warning_values: dict[str, Any] = {}
config_reload_service.register("render-yaml", config_path, _reload_render_config)
