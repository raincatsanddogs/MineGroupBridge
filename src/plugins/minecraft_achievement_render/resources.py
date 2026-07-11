import json
from pathlib import Path

file_path = str(Path(__file__).parent / "templates") + "/advancements.json"
with open(file_path, encoding="utf-8") as f:
    advancements = json.load(f)

async def get_resource_path(key: str, prefix: str) -> str:
    """
    获取资源文件的路径
    :param key: 资源文件的键
    :param prefix: 资源文件的前缀
    :return: 资源文件的路径
    """

    try:
        target_key = advancements[key]["icon"]["id"]
    except KeyError as e:
        target_key = "barrier"
        print(f"找不到对应的键: {e}")

    if ":" in target_key:
        target_key = target_key.split(":")[-1]

    target_key = target_key.upper()

    resource_dir = f"{prefix}" + "https://raw.githubusercontent.com/Owen1212055/mc-assets/main/item-assets/"
    resource_path = resource_dir + f"{target_key}.png"

    return str(resource_path)
