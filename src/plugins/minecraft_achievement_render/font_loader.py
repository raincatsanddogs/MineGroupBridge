import os
from pathlib import Path

from nonebot import logger

TEMPLATE_DIR_PATH = Path(__file__).parent / "templates"
FONT_PATH = TEMPLATE_DIR_PATH / "Minecraft_Font.ttf"

if FONT_PATH.exists():
    fonts_conf = TEMPLATE_DIR_PATH / "fonts.conf"
    windir = os.environ.get("SystemRoot", os.environ.get("windir", "C:\\Windows"))
    win_fonts = Path(windir) / "Fonts"

    fonts_conf_content = f"""<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
    <dir>{TEMPLATE_DIR_PATH.as_posix()}</dir>
    <dir>{win_fonts.as_posix()}</dir>
    <include ignore_missing="yes">/etc/fonts/fonts.conf</include>
    <include ignore_missing="yes">/usr/local/etc/fonts/fonts.conf</include>
    <include ignore_missing="yes">/opt/homebrew/etc/fonts/fonts.conf</include>
</fontconfig>
"""
    try:
        fonts_conf.write_text(fonts_conf_content, encoding="utf-8")
        os.environ["FONTCONFIG_FILE"] = str(fonts_conf.resolve())
        os.environ["FONTCONFIG_PATH"] = str(TEMPLATE_DIR_PATH.resolve())
        logger.info(f"minecraft_achievement_render: 已将 fontconfig 指向 {fonts_conf}")
    except Exception as e:
        logger.error(f"minecraft_achievement_render: 写入 fonts.conf 失败: {e}")
else:
    logger.warning(f"minecraft_achievement_render: 默认字体文件 {FONT_PATH} 不存在，将使用系统默认字体")
