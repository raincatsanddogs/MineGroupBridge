import json
from pathlib import Path
from typing import Any

import yaml
from nonebot import logger  # pyright: ignore[reportMissingImports]
from nonebot.compat import PYDANTIC_V2  # pyright: ignore[reportMissingImports]
from pydantic import BaseModel, Field, ValidationError


class Config(BaseModel):
    """配置"""

    command_header: Any = {"render"}
    """命令头"""

    res_path_prefix: str = ""
    """资源路径前缀"""

    component_asset_version: str = Field(default="1.21.4", min_length=1)
    """Minecraft 原版组件纹理版本"""


config_path = Path("config/render.yaml")
if not config_path.parent.exists():
    config_path.parent.mkdir(parents=True, exist_ok=True)

if not config_path.exists():
    alt_path = Path("render.yaml")
    if alt_path.exists():
        config_path = alt_path
    else:
        try:
            if PYDANTIC_V2:
                default_data = json.loads(Config().model_dump_json())
            else:
                default_data = json.loads(Config().json())
            with config_path.open("w", encoding="utf-8") as f:
                yaml.safe_dump(default_data, f, allow_unicode=True, sort_keys=False)
            logger.info(f"render插件已在 {config_path} 自动生成默认配置文件")
        except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
            logger.error(f"生成默认配置文件失败：{error}")

plugin_config = Config()
if config_path.exists():
    try:
        with config_path.open(encoding="utf-8") as f:
            yaml_data = yaml.safe_load(f) or {}
        if PYDANTIC_V2:
            plugin_config = Config.model_validate(yaml_data)
        else:
            plugin_config = Config.parse_obj(yaml_data)
        logger.info(f"render插件成功加载配置文件：{config_path}")
    except (
        OSError,
        TypeError,
        ValueError,
        UnicodeError,
        ValidationError,
        yaml.YAMLError,
    ) as error:
        logger.error(
            f"render插件加载配置文件 {config_path} 失败，"
            f"已使用默认配置。错误信息：{error}",
        )
