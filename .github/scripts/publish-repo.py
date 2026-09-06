import gzip
import hashlib
import html
import json
import math
import sys
import time
from pathlib import Path

import index_pb2
from github_utils import REPO_NAME, run_gh
from google.protobuf import json_format

# Artifacts downloaded from the build jobs: one APK per extension plus the source metadata JSON
# emitted by each assembleRelease.
ARTIFACTS_DIR = Path.home() / "apk-artifacts"

# The checked-out `repo` branch we publish into (the working directory).
REPO_DIR = Path.cwd()

ICON_BASE_URL = "https://cdn.jsdelivr.net/gh/CahyaXyZp/extensions-source@main"
RELEASE_BASE_URL = f"https://github.com/{REPO_NAME}/releases/download"
ASSET_LIMIT = 495  # Actual limit is 1000 but we upload 2 items per extension.
UPLOAD_CHUNK_SIZE = 80
UPLOAD_CHUNK_INTERVAL = 30

to_delete: list[str] = json.loads(sys.argv[1])
current_sha = sys.argv[2]
current_sha_short = current_sha[:7]

with REPO_DIR.joinpath("index.json").open() as f:
    remote_proto = json_format.Parse(f.read(), index_pb2.Index())

remote_extensions = {
    ext.packageName: ext for ext in remote_proto.extensionList.extensions
}

release_assets_path = REPO_DIR / "release-assets.json"
if release_assets_path.exists():
    with release_assets_path.open() as f:
        release_assets = json.load(f)
else:
    release_assets = {}

updated_release_assets = {
    package_name: assets
    for package_name, assets in release_assets.items()
    if not any(package_name.endswith(f".{module}") for module in to_delete)
}

# Build index entries for the freshly built apks. Each extension's metadata comes from the
# source-info JSON emitted by its assembleRelease task (see GenerateSourceInfoTask); its APK is a
# sibling in the same build dir. aapt reads the icon out of the APK
new_extensions: list[tuple[index_pb2.Extension, Path, Path, bool, bool]] = []

SOURCE_DIR = Path(__file__).resolve().parents[2]
ICON_FILE = "res/mipmap-xhdpi/ic_launcher.png"


def get_icon_url(module: str, theme: str | None) -> str:
    module_icon = f"src/{module.replace('.', '/')}/{ICON_FILE}"
    if (SOURCE_DIR / module_icon).exists():
        return f"{ICON_BASE_URL}/{module_icon}"

    if theme:
        theme_icon = f"lib-multisrc/{theme}/{ICON_FILE}"
        if (SOURCE_DIR / theme_icon).exists():
            return f"{ICON_BASE_URL}/{theme_icon}"

    return f"{ICON_BASE_URL}/core/src/main/{ICON_FILE}"


for info_file in ARTIFACTS_DIR.glob("**/keiyoushi-source-info.json"):
    with info_file.open(encoding="utf-8") as f:
        info = json.load(f)
    package_name = info["packageName"]
    apk = next((info_file.parent / "outputs/apk/release").glob("*.apk"), None)
    if apk is None:
        raise FileNotFoundError(
            f"{package_name}: no release apk found under {info_file.parent}"
        )

    jar = next((info_file.parent / "outputs/jar/release").glob("*.jar"), None)
    if jar is None:
        raise FileNotFoundError(
            f"{package_name}: no release jar found under {info_file.parent}"
        )

    assets = {
        "apk": {
            "name": apk.name,
            "sha256": hashlib.sha256(apk.read_bytes()).hexdigest(),
        },
        "jar": {
            "name": jar.name,
            "sha256": hashlib.sha256(jar.read_bytes()).hexdigest(),
        },
    }
    old_assets = release_assets.get(package_name, {})
    apk_changed = (
        package_name not in remote_extensions
        or old_assets.get("apk") != assets["apk"]
    )
    jar_changed = (
        package_name not in remote_extensions
        or old_assets.get("jar") != assets["jar"]
    )

    updated_release_assets[package_name] = assets

    ext = index_pb2.Extension(
        name=info["name"],
        packageName=package_name,
        resources=index_pb2.Resources(
            iconUrl=get_icon_url(info["module"], info.get("theme")),
        ),
        extensionLib=info["extensionLib"],
        versionCode=info["versionCode"],
        versionName=info["versionName"],
        contentWarning=info["contentWarning"],
        sources=[
            index_pb2.Source(
                id=int(source["id"]),
                name=source["name"],
                language=source["lang"],
                homeUrl=source["baseUrl"],
                mirrorUrls=source.get("mirrorUrls", []),
            )
            for source in info["sources"]
        ],
    )
    new_extensions.append((ext, apk, jar, apk_changed, jar_changed))

new_extensions.sort(key=lambda item: item[0].packageName)

changed_extensions = [item for item in new_extensions if item[3] or item[4]]
total_changed_extensions = len(changed_extensions)
release_count = (
    math.ceil(total_changed_extensions / ASSET_LIMIT)
    if total_changed_extensions
    else 0
)
ext_per_release = (
    math.ceil(total_changed_extensions / release_count) if release_count else 0
)


def get_release_tag(batch_index: int) -> str:
    return (
        f"{current_sha_short}-{batch_index}" if release_count > 1 else current_sha_short
    )


changed_index = 0
for ext, apk, jar, apk_changed, jar_changed in new_extensions:
    if apk_changed or jar_changed:
        tag = get_release_tag(changed_index // ext_per_release)
        old_resources = remote_extensions.get(ext.packageName)
        ext.resources.apkUrl = (
            f"{RELEASE_BASE_URL}/{tag}/{apk.name}"
            if apk_changed
            else old_resources.resources.apkUrl
        )
        ext.resources.jarUrl = (
            f"{RELEASE_BASE_URL}/{tag}/{jar.name}"
            if jar_changed
            else old_resources.resources.jarUrl
        )
        changed_index += 1
    else:
        old_resources = remote_extensions[ext.packageName].resources
        ext.resources.apkUrl = old_resources.apkUrl
        ext.resources.jarUrl = old_resources.jarUrl

# Merge with the already-published index, dropping the deleted/rebuilt modules.
final_extensions = []
final_extensions.extend(
    ext
    for ext in remote_proto.extensionList.extensions
    if not any(ext.packageName.endswith(f".{module}") for module in to_delete)
)
final_extensions.extend(ext for ext, _, _, _, _ in new_extensions)

# Safety net: this is an Indonesian-only repo. Package names follow
# eu.kanade.tachiyomi.extension.<lang>.<module>, so any extension whose lang segment
# isn't "id" is stale (e.g. carried over from the original keiyoushi index when this
# repo was forked) and must never be published, regardless of what git-diff deletion
# detection did or didn't catch.
KEEP_LANG = "id"
final_extensions = [
    ext for ext in final_extensions if ext.packageName.split(".")[4:5] == [KEEP_LANG]
]

final_extensions.sort(key=lambda ext: ext.packageName)

kept_package_names = {ext.packageName for ext in final_extensions}
updated_release_assets = {
    package_name: assets
    for package_name, assets in updated_release_assets.items()
    if package_name in kept_package_names
}

# Safety net: apkUrl/jarUrl for extensions that weren't rebuilt this run (identical output,
# e.g. from reproducible builds) are carried over verbatim from the previously published index.
# If the repo owner ever changes (as it did: rotatsu-id -> CahyaXyZp), those stale URLs silently
# point at the old owner. GitHub's account-rename redirect doesn't reliably survive every client,
# and worse, cleanup-releases.py compares these URLs against the *current* owner's real asset
# URLs to decide what's "unreferenced" -- a stale owner here makes it think every release is
# orphaned and delete them all. So every URL is always rewritten to the current RELEASE_BASE_URL,
# keeping only the release tag and filename from whatever was there before.
def rehost(url: str) -> str:
    tag, filename = url.rsplit("/", 2)[-2:]
    return f"{RELEASE_BASE_URL}/{tag}/{filename}"


for ext in final_extensions:
    if ext.resources.apkUrl:
        ext.resources.apkUrl = rehost(ext.resources.apkUrl)
    if ext.resources.jarUrl:
        ext.resources.jarUrl = rehost(ext.resources.jarUrl)

index = index_pb2.Index(
    name="RoTatsu",
    badgeLabel="RTS",
    signingKey="1abce2f2fe4e905806d0a9d1e68f97b98cd255fe3253d6fce0c91992267878fd",
    contact=index_pb2.Contact(
        website="https://github.com/CahyaXyZp/rotatsu-extensions",
    ),
    extensionList=index_pb2.ExtensionList(extensions=final_extensions),
)

with REPO_DIR.joinpath("index.json").open("w", encoding="utf-8") as f:
    f.write(
        json_format.MessageToJson(
            index,
            always_print_fields_with_no_presence=False,
            preserving_proto_field_name=True,
        )
    )

with REPO_DIR.joinpath("index.pb").open("wb") as f:
    f.write(gzip.compress(index.SerializeToString(deterministic=True), mtime=0))

with release_assets_path.open("w", encoding="utf-8") as f:
    json.dump(updated_release_assets, f, indent=2, sort_keys=True)
    f.write("\n")

with REPO_DIR.joinpath("index.html").open("w", encoding="utf-8") as f:
    f.write(
        '<!DOCTYPE html>\n<html>\n<head>\n<meta charset="UTF-8">\n<title>apks</title>\n</head>\n<body>\n<pre>\n'
    )
    for ext in final_extensions:
        apk_escaped = html.escape(ext.resources.apkUrl)
        name_escaped = html.escape(f"Tachiyomi: {ext.name}")
        f.write(f'<a href="{apk_escaped}">{name_escaped}</a>\n')
    f.write("</pre>\n</body>\n</html>\n")

# --- Upload assets as release ---
if not changed_extensions:
    sys.exit(0)


def create_release(tag: str):
    if run_gh(
        "release",
        "view",
        tag,
        "--repo",
        REPO_NAME,
        "--json",
        "tagName",
        success_errors=("release not found",),
    ):
        print(f"Release {tag} already exists")
        return

    print(f"Creating release {tag}")
    run_gh(
        "release",
        "create",
        tag,
        "--repo",
        REPO_NAME,
        "--draft",
        "--title",
        f"Repository Update {tag}",
        "--notes",
        f"Automated update from CahyaXyZp/extensions-source@{current_sha}",
    )


def publish_release(tag: str):
    print(f"Publishing release {tag}")
    run_gh("release", "edit", tag, "--repo", REPO_NAME, "--draft=false")


def get_release_assets(tag: str) -> dict[str, str]:
    release = json.loads(
        run_gh(
            "release",
            "view",
            tag,
            "--repo",
            REPO_NAME,
            "--json",
            "assets",
        )
    )
    return {
        asset["name"]: (asset.get("digest") or "").removeprefix("sha256:")
        for asset in release["assets"]
    }


def upload_assets(tag: str, files: list[Path]):
    if not files:
        return

    existing_assets = get_release_assets(tag)
    files_to_upload = [
        file
        for file in files
        if existing_assets.get(file.name)
        != hashlib.sha256(file.read_bytes()).hexdigest()
    ]
    skipped = len(files) - len(files_to_upload)
    print(f"Uploading {len(files_to_upload)} assets to {tag}, skipping {skipped}")

    for i in range(0, len(files_to_upload), UPLOAD_CHUNK_SIZE):
        chunk = files_to_upload[i : i + UPLOAD_CHUNK_SIZE]
        if i:
            time.sleep(UPLOAD_CHUNK_INTERVAL)
        print(f"  assets {i + 1}-{i + len(chunk)} of {len(files_to_upload)}")
        run_gh(
            "release",
            "upload",
            tag,
            *[str(f) for f in chunk],
            "--repo",
            REPO_NAME,
            "--clobber",
        )
    publish_release(tag)


for i in range(0, total_changed_extensions, ext_per_release):
    batch = changed_extensions[i : i + ext_per_release]
    tag = get_release_tag(i // ext_per_release)
    files_to_upload = [
        file
        for _, apk, jar, apk_changed, jar_changed in batch
        for file, changed in ((apk, apk_changed), (jar, jar_changed))
        if changed
    ]

    create_release(tag)
    upload_assets(tag, files_to_upload)
