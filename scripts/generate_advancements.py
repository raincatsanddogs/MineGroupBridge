import os
import sys
import json
import zipfile
import urllib.request

VERSION = "26.2"
MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"

def process_zip_datapack(zip_path, advancements_map):
    print(f"Parsing local zip datapack: {zip_path}...")
    try:
        with zipfile.ZipFile(zip_path) as z:
            for file_info in z.infolist():
                filename = file_info.filename.replace("\\", "/")
                parts = filename.split("/")
                # Support both singular "advancement" and plural "advancements"
                if len(parts) >= 4 and parts[0] == "data" and parts[2] in ("advancement", "advancements") and filename.endswith(".json"):
                    namespace = parts[1]
                    rel_parts = parts[3:]
                    key_name = "/".join(rel_parts)
                    key_name = os.path.splitext(key_name)[0]
                    adv_key = f"{namespace}:{key_name}"
                    
                    try:
                        with z.open(file_info.filename) as f:
                            data = json.loads(f.read().decode("utf-8-sig"))
                        if "display" in data and "icon" in data["display"]:
                            advancements_map[adv_key] = data["display"]["icon"]
                    except Exception as e:
                        print(f"  Warning: Failed to parse {filename} in {zip_path}: {e}")
    except Exception as e:
        print(f"  Error reading zip datapack {zip_path}: {e}")

def process_folder_datapack(folder_path, advancements_map):
    data_dir = os.path.join(folder_path, "data")
    if not os.path.isdir(data_dir):
        return
    print(f"Parsing local folder datapack: {folder_path}...")
    for root, dirs, files in os.walk(data_dir):
        for file in files:
            if file.endswith(".json"):
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, data_dir).replace("\\", "/")
                parts = rel_path.split("/")
                if len(parts) >= 3 and parts[1] in ("advancement", "advancements"):
                    namespace = parts[0]
                    rel_parts = parts[2:]
                    key_name = "/".join(rel_parts)
                    key_name = os.path.splitext(key_name)[0]
                    adv_key = f"{namespace}:{key_name}"
                    
                    try:
                        with open(full_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        if "display" in data and "icon" in data["display"]:
                            advancements_map[adv_key] = data["display"]["icon"]
                    except Exception as e:
                        print(f"  Warning: Failed to parse {rel_path} in {folder_path}: {e}")

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    workspace_dir = os.path.dirname(script_dir)
    output_dir = os.path.join(workspace_dir, "src", "plugins", "minecraft_achievement_render", "templates")
    output_file = os.path.join(output_dir, "advancements.json")
    temp_jar = os.path.join(script_dir, "temp_client.jar")

    advancements_map = {}

    # 1. Download and parse official JAR
    try:
        print(f"[{VERSION}] Fetching version manifest from Mojang...")
        req = urllib.request.Request(MANIFEST_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as r:
            manifest = json.loads(r.read().decode("utf-8"))

        version_entry = next((v for v in manifest["versions"] if v["id"] == VERSION), None)
        if not version_entry:
            print(f"Error: Version {VERSION} not found in manifest.", file=sys.stderr)
            sys.exit(1)

        print(f"[{VERSION}] Fetching version package JSON...")
        req_ver = urllib.request.Request(version_entry["url"], headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req_ver) as r:
            package = json.loads(r.read().decode("utf-8"))

        client_jar_url = package["downloads"]["client"]["url"]
        print(f"[{VERSION}] Downloading client JAR from {client_jar_url}...")
        
        # Download client jar
        req_jar = urllib.request.Request(client_jar_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req_jar) as response, open(temp_jar, "wb") as out_file:
            while True:
                buf = response.read(16*1024)
                if not buf:
                    break
                out_file.write(buf)
            
        print(f"[{VERSION}] Extracting and parsing official advancements...")
        with zipfile.ZipFile(temp_jar) as z:
            for file_info in z.infolist():
                if file_info.filename.startswith("data/minecraft/advancement/") and file_info.filename.endswith(".json"):
                    rel_path = file_info.filename[len("data/minecraft/advancement/"):]
                    key_name = os.path.splitext(rel_path)[0].replace("\\", "/")
                    adv_key = f"minecraft:{key_name}"

                    with z.open(file_info.filename) as f:
                        data = json.loads(f.read().decode("utf-8"))

                    if "display" in data and "icon" in data["display"]:
                        advancements_map[adv_key] = data["display"]["icon"]

        print(f"[{VERSION}] Parsed {len(advancements_map)} official advancements.")

    except Exception as e:
        print(f"Error reading official advancements: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
    finally:
        if os.path.exists(temp_jar):
            print(f"Cleaning up temporary file {temp_jar}...")
            try:
                os.remove(temp_jar)
            except Exception as clean_err:
                print(f"Failed to delete {temp_jar}: {clean_err}", file=sys.stderr)

    # 2. Scan and parse local datapacks in scripts/ and templates/
    scan_dirs = [
        script_dir,
        output_dir
    ]
    for scan_dir in scan_dirs:
        if not os.path.isdir(scan_dir):
            continue
        print(f"Scanning directory for datapacks: {scan_dir}...")
        for item in os.listdir(scan_dir):
            item_path = os.path.join(scan_dir, item)
            # Process zip files
            if os.path.isfile(item_path) and item.endswith(".zip"):
                if item != "temp_client.jar":
                    process_zip_datapack(item_path, advancements_map)
            # Process folders
            elif os.path.isdir(item_path):
                if item not in ("__pycache__", "cache"):
                    process_folder_datapack(item_path, advancements_map)

    # 3. Sort and Save
    sorted_advancements = {k: advancements_map[k] for k in sorted(advancements_map.keys())}
    print(f"Total advancements in database: {len(sorted_advancements)}")

    try:
        os.makedirs(output_dir, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(sorted_advancements, f, indent=2, ensure_ascii=False)
        print(f"Successfully saved advancements mapping to: {output_file}")
    except Exception as e:
        print(f"Error saving to {output_file}: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
