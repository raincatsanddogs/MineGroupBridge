import base64
import json
from pathlib import Path
import httpx
from nonebot import logger

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

file_path = Path(__file__).parent / "templates" / "advancements.json"
with open(file_path, encoding="utf-8") as f:
    advancements = json.load(f)

async def download_image(url: str, save_path: Path) -> bool:
    """Download image from url and save to save_path"""
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(url)
            if response.status_code == 200:
                save_path.write_bytes(response.content)
                return True
            else:
                logger.warning(f"Download image failed: {url}, status code: {response.status_code}")
                return False
    except Exception as e:
        logger.error(f"Error downloading image from {url}: {e}")
        return False

async def get_resource_path(key: str, prefix: str) -> str | None:
    """
    获取资源文件的路径 (返回 Base64 数据 URL)
    :param key: 资源文件的键
    :param prefix: 资源文件的前缀
    :return: 资源文件的 Base64 Data URL，或在线 URL 作为回退
    """
    if key is None:
        return None

    try:
        icon_obj = advancements[key]
        if isinstance(icon_obj, dict):
            target_key = icon_obj.get("id") or icon_obj.get("item") or "barrier"
        elif isinstance(icon_obj, str):
            target_key = icon_obj
        else:
            target_key = "barrier"
    except KeyError as e:
        target_key = "barrier"
        logger.warning(f"找不到对应的键: {e}")

    if ":" in target_key:
        target_key = target_key.split(":")[-1]

    target_key = target_key.upper()
    file_name = f"{target_key}.png"
    local_path = CACHE_DIR / file_name

    # If the file is not in cache, try downloading it
    if not local_path.exists():
        resource_dir = f"{prefix}" + "https://raw.githubusercontent.com/Owen1212055/mc-assets/main/item-assets/"
        download_url = resource_dir + file_name
        logger.info(f"Downloading advancement icon {file_name} from {download_url}...")
        success = await download_image(download_url, local_path)
        if not success:
            # Fallback to local BARRIER.png if downloading failed
            barrier_path = CACHE_DIR / "BARRIER.png"
            if not barrier_path.exists():
                barrier_url = (f"{prefix}" or "") + "https://raw.githubusercontent.com/Owen1212055/mc-assets/main/item-assets/BARRIER.png"
                await download_image(barrier_url, barrier_path)
            
            if barrier_path.exists():
                local_path = barrier_path
            else:
                # If everything fails, return the remote URL directly as a last-resort fallback
                return download_url

    # Convert local file to base64 Data URL
    try:
        img_bytes = local_path.read_bytes()
        base64_str = base64.b64encode(img_bytes).decode("utf-8")
        return f"data:image/png;base64,{base64_str}"
    except Exception as e:
        logger.error(f"Failed to read local file {local_path} as base64: {e}")
        # Last-resort fallback
        return f"{prefix}https://raw.githubusercontent.com/Owen1212055/mc-assets/main/item-assets/{target_key}.png"


