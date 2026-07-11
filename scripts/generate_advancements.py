import os
import sys
import json
import zipfile
import urllib.request

VERSION = "26.2"
MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    workspace_dir = os.path.dirname(script_dir)
    output_dir = os.path.join(workspace_dir, "src", "plugins", "minecraft_achievement_render")
    output_file = os.path.join(output_dir, "advancements.json")
    temp_jar = os.path.join(script_dir, "temp_client.jar")

    try:
        print(f"[{VERSION}] Fetching version manifest from Mojang...")
        with urllib.request.urlopen(MANIFEST_URL) as r:
            manifest = json.loads(r.read().decode("utf-8"))

        version_entry = next((v for v in manifest["versions"] if v["id"] == VERSION), None)
        if not version_entry:
            print(f"Error: Version {VERSION} not found in manifest.", file=sys.stderr)
            sys.exit(1)

        print(f"[{VERSION}] Fetching version package JSON...")
        with urllib.request.urlopen(version_entry["url"]) as r:
            package = json.loads(r.read().decode("utf-8"))

        client_jar_url = package["downloads"]["client"]["url"]
        print(f"[{VERSION}] Downloading client JAR from {client_jar_url}...")
        
        # Download client jar block by block to temp_jar
        with urllib.request.urlopen(client_jar_url) as response, open(temp_jar, "wb") as out_file:
            shutil_copyfileobj(response, out_file)
            
        print(f"[{VERSION}] Extracting and parsing advancements...")
        advancements_map = {}
        with zipfile.ZipFile(temp_jar) as z:
            for file_info in z.infolist():
                if file_info.filename.startswith("data/minecraft/advancement/") and file_info.filename.endswith(".json"):
                    # Extract namespace path
                    rel_path = file_info.filename[len("data/minecraft/advancement/"):]
                    key_name = os.path.splitext(rel_path)[0].replace("\\", "/")
                    adv_key = f"minecraft:{key_name}"

                    # Read JSON content
                    with z.open(file_info.filename) as f:
                        data = json.loads(f.read().decode("utf-8"))

                    # Only process advancements with "display"
                    if "display" in data:
                        display = data["display"]
                        # Clean display object: keep only core fields, default frame to "task"
                        cleaned_display = {
                            "title": display.get("title"),
                            "description": display.get("description"),
                            "icon": display.get("icon"),
                            "frame": display.get("frame", "task")
                        }
                        advancements_map[adv_key] = cleaned_display

        # Sort advancements alphabetically by key
        sorted_advancements = {k: advancements_map[k] for k in sorted(advancements_map.keys())}
        
        print(f"[{VERSION}] Parsed {len(sorted_advancements)} advancements with display info.")

        # Ensure output directory exists
        os.makedirs(output_dir, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(sorted_advancements, f, indent=2, ensure_ascii=False)
        print(f"[{VERSION}] Successfully saved advancements mapping to: {output_file}")

    except Exception as e:
        print(f"Error occurred: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        # Clean up temp jar
        if os.path.exists(temp_jar):
            print(f"Cleaning up temporary file {temp_jar}...")
            try:
                os.remove(temp_jar)
            except Exception as clean_err:
                print(f"Failed to delete {temp_jar}: {clean_err}", file=sys.stderr)

def shutil_copyfileobj(fsrc, fdst, length=16*1024):
    while True:
        buf = fsrc.read(length)
        if not buf:
            break
        fdst.write(buf)

if __name__ == "__main__":
    main()
