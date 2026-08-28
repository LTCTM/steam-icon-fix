import asyncio
import logging
import os
import re
import struct
import urllib.error
import urllib.request
import winreg
from pathlib import Path


logging.basicConfig(level=logging.INFO, format="%(levelname)s\t %(message)s", force=True)
logger = logging.getLogger()

VDFToken = tuple[str, ...]


# ================ VDF/ACF 文本解析（libraryfolders.vdf / appmanifest_*.acf） ================


def _tokenize_vdf(text: str) -> list[VDFToken]:
    """把 VDF/ACF 文本拆成字符串、{、} 三种 token。"""
    tokens: list[VDFToken] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == '"':
            i += 1
            buf: list[str] = []
            while i < len(text):
                if text[i] == "\\" and i + 1 < len(text):
                    buf.append(text[i + 1])
                    i += 2
                elif text[i] == '"':
                    i += 1
                    break
                else:
                    buf.append(text[i])
                    i += 1
            tokens.append(("str", "".join(buf)))
            continue
        if ch == "{":
            tokens.append(("open",))
            i += 1
            continue
        if ch == "}":
            tokens.append(("close",))
            i += 1
            continue
        i += 1
    return tokens


def _parse_vdf(tokens: list[VDFToken], idx: int = 0) -> tuple[dict, int]:
    """递归解析 token 为嵌套字典。"""
    node: dict = {}
    while idx < len(tokens):
        typ = tokens[idx][0]
        if typ == "close":
            return node, idx + 1
        if typ != "str":
            idx += 1
            continue

        key = tokens[idx][1]
        idx += 1
        if idx >= len(tokens):
            break

        nxt = tokens[idx][0]
        if nxt == "str":
            node[key] = tokens[idx][1]
            idx += 1
        elif nxt == "open":
            child, idx = _parse_vdf(tokens, idx + 1)
            node[key] = child
        else:
            idx += 1
    return node, idx


def parse_vdf(text: str) -> dict:
    """解析 VDF/ACF 文本。"""
    if text.startswith("\ufeff"):
        text = text[1:]
    tokens = _tokenize_vdf(text)
    data, _ = _parse_vdf(tokens, 0)
    return data


# ================ Steam 安装路径 / 库 / 已安装游戏 ================


def get_steam_install_path() -> Path | None:
    """从注册表读取 Steam 安装路径，64 位系统优先 WOW6432Node。"""
    candidates = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
    ]
    for hkey, subkey in candidates:
        try:
            with winreg.OpenKey(hkey, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "InstallPath")
                if value:
                    return Path(value)
        except OSError:
            continue
    return None


def _collect_library_paths(node, found: set[Path]) -> None:
    """递归收集 VDF 中所有 steamapps 目录。"""
    if isinstance(node, dict):
        for value in node.values():
            if isinstance(value, str):
                p = Path(value)
                steamapps = p / "steamapps"
                if steamapps.is_dir():
                    found.add(steamapps)
                elif p.name.lower() == "steamapps" and p.is_dir():
                    found.add(p)
            else:
                _collect_library_paths(value, found)


def get_steam_libraries(steam_path: Path) -> list[Path]:
    """返回所有 Steam 库目录（包含默认库）。"""
    found: set[Path] = set()

    default = steam_path / "steamapps"
    if default.is_dir():
        found.add(default)

    vdf_candidates = [
        default / "libraryfolders.vdf",
        steam_path / "config" / "libraryfolders.vdf",
    ]
    for vdf_path in vdf_candidates:
        if not vdf_path.exists():
            continue
        try:
            data = parse_vdf(vdf_path.read_text(encoding="utf-8", errors="ignore"))
            _collect_library_paths(data, found)
        except Exception:
            logger.warning(f"读取库配置文件失败: {vdf_path}")

    return sorted(found)


def get_installed_appids(steam_path: Path) -> set[str]:
    """扫描所有库的 appmanifest_*.acf，返回已安装游戏的 appid 集合。"""
    appids: set[str] = set()
    appid_re = re.compile(r"^\d+$")

    for library in get_steam_libraries(steam_path):
        for manifest in sorted(library.glob("appmanifest_*.acf")):
            try:
                data = parse_vdf(manifest.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue

            state = data.get("AppState", data)
            if not isinstance(state, dict):
                continue

            appid = state.get("appid") or state.get("appID")
            if appid and appid_re.match(str(appid)):
                appids.add(str(appid))

    return appids


# ================ appinfo.vdf 二进制解析 ================


def _load_appinfo_strings(data: bytes, table_offset: int) -> list[str]:
    """读取 v41 的字符串表。"""
    count = struct.unpack("<I", data[table_offset : table_offset + 4])[0]
    strings: list[str] = []
    off = table_offset + 4
    for _ in range(count):
        end = data.find(b"\x00", off)
        if end == -1:
            break
        strings.append(data[off:end].decode("utf-8", errors="ignore"))
        off = end + 1
    return strings


def _make_key_reader(strings: list[str] | None):
    """返回根据格式读取 key 的函数。"""
    if strings is not None:

        def read_index(d: bytes, off: int) -> tuple[str, int]:
            idx = struct.unpack("<I", d[off : off + 4])[0]
            key = strings[idx] if idx < len(strings) else f"#{idx}"
            return key, off + 4

        return read_index

    def read_cstring(d: bytes, off: int) -> tuple[str, int]:
        end = d.find(b"\x00", off)
        key = d[off:end].decode("utf-8", errors="ignore")
        return key, end + 1

    return read_cstring


def _parse_binary_vdf(data: bytes, off: int, end: int, read_key) -> tuple[dict, int]:
    """解析 appinfo.vdf 里的二进制 VDF。"""
    stack: list[dict] = [{}]
    while off < end:
        t = data[off]
        off += 1
        if t in (0x08, 0x0B):
            if len(stack) > 1:
                stack.pop()
            else:
                break
            continue

        key, off = read_key(data, off)

        if t == 0x00:  # node
            child: dict = {}
            stack[-1][key] = child
            stack.append(child)
        elif t == 0x01:  # string
            s_end = data.find(b"\x00", off)
            stack[-1][key] = data[off:s_end].decode("utf-8", errors="ignore")
            off = s_end + 1
        elif t == 0x02:  # int32
            stack[-1][key] = struct.unpack("<i", data[off : off + 4])[0]
            off += 4
        elif t in (0x03, 0x04, 0x06):  # float / pointer / color
            off += 4
        elif t == 0x05:  # wide string
            s_end = off
            while not (data[s_end] == 0 and data[s_end + 1] == 0):
                s_end += 2
            stack[-1][key] = data[off:s_end].decode("utf-16-le", errors="ignore")
            off = s_end + 2
        elif t == 0x07:  # uint64
            stack[-1][key] = struct.unpack("<Q", data[off : off + 8])[0]
            off += 8
        elif t == 0x0A:  # int64
            stack[-1][key] = struct.unpack("<q", data[off : off + 8])[0]
            off += 8
        else:
            raise ValueError(f"未知的二进制 VDF 类型: {t} @ {off - 1}")
    return stack[0], off


def get_appinfo_icon_hashes(steam_path: Path, installed_appids: set[str]) -> dict[str, str]:
    """
    读取 appinfo.vdf，返回 {appid: icon_hash}。
    优先用 common.clienticon，没有则用 common.icon。
    """
    appinfo_path = steam_path / "appcache" / "appinfo.vdf"
    if not appinfo_path.exists():
        return {}

    try:
        data = appinfo_path.read_bytes()
    except Exception:
        return {}

    if len(data) < 16:
        return {}

    magic = struct.unpack("<I", data[:4])[0]
    if magic == 0x07564429:  # v41，含字符串表
        strings = _load_appinfo_strings(data, struct.unpack("<Q", data[8:16])[0])
        entry_off = 16
        read_key = _make_key_reader(strings)
    elif magic == 0x07564428:  # v40
        entry_off = 8
        read_key = _make_key_reader(None)
    else:
        logger.warning(f"不支持的 appinfo.vdf 格式: {hex(magic)}")
        return {}

    hashes: dict[str, str] = {}
    off = entry_off
    while off + 68 <= len(data):
        appid = struct.unpack("<I", data[off : off + 4])[0]
        if appid == 0:
            break
        size = struct.unpack("<I", data[off + 4 : off + 8])[0]
        if size < 60:
            break
        vdf_len = size - 60
        vdf_off = off + 68

        appid_str = str(appid)
        if appid_str in installed_appids and vdf_off + vdf_len <= len(data):
            try:
                root, _ = _parse_binary_vdf(data, vdf_off, vdf_off + vdf_len, read_key)
            except Exception:
                off += 8 + 60 + vdf_len
                continue

            appinfo = root.get("appinfo", root)
            if isinstance(appinfo, dict):
                common = appinfo.get("common", {})
                if isinstance(common, dict):
                    icon_hash = common.get("clienticon")
                    if isinstance(icon_hash, str) and icon_hash:
                        hashes[appid_str] = icon_hash

        off += 8 + 60 + vdf_len

    return hashes


# ================ 图标下载 ================


class IconDownloader:
    # 限制同时下载的并发数为 5
    sem = asyncio.Semaphore(5)
    base_url = "http://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps"

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout

    async def download_icon(self, app_id: str, icon_file: Path) -> bool:
        """下载图标到指定路径。"""
        url = f"{self.base_url}/{app_id}/{icon_file.name}"
        async with self.sem:
            return await asyncio.to_thread(self._download, app_id, url, icon_file)

    def _download(self, app_id: str, url: str, icon_file: Path) -> bool:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status != 200:
                    logger.error(f"Download icon of <{app_id}> failed with status {resp.status}")
                    return False
                data = resp.read()
            icon_file.parent.mkdir(parents=True, exist_ok=True)
            icon_file.write_bytes(data)
            logger.info(f"Download icon of <{app_id}> success")
            return True
        except urllib.error.HTTPError as e:
            logger.error(f"Download icon of <{app_id}> failed! {e.code}: {e.reason}")
            return False
        except Exception as e:
            logger.error(f"Download icon of <{app_id}> failed: {e}")
            return False


def flush_icon() -> None:
    """刷新 Windows 图标缓存。"""
    user_profile = Path.home()
    icon_cache = user_profile / "AppData" / "Local" / "IconCache.db"
    explorer_cache = user_profile / "AppData" / "Local" / "Microsoft" / "Windows" / "Explorer"

    commands = [
        "taskkill /f /im explorer.exe",
        f'attrib -h -s -r "{icon_cache}"',
        f'del /f "{icon_cache}"',
        f'attrib /s /d -h -s -r "{explorer_cache}\\*"',
        f'del /f "{explorer_cache}\\thumbcache_32.db"',
        f'del /f "{explorer_cache}\\thumbcache_96.db"',
        f'del /f "{explorer_cache}\\thumbcache_102.db"',
        f'del /f "{explorer_cache}\\thumbcache_256.db"',
        f'del /f "{explorer_cache}\\thumbcache_1024.db"',
        f'del /f "{explorer_cache}\\thumbcache_idx.db"',
        f'del /f "{explorer_cache}\\thumbcache_sr.db"',
        'echo y | reg delete "HKEY_CLASSES_ROOT\\Local Settings\\Software\\Microsoft\\Windows\\CurrentVersion\\TrayNotify" /v IconStreams',
        'echo y | reg delete "HKEY_CLASSES_ROOT\\Local Settings\\Software\\Microsoft\\Windows\\CurrentVersion\\TrayNotify" /v PastIconsStream',
        "start explorer",
    ]

    for cmd in commands:
        os.system(cmd)


def get_icon_infos_to_fix(steam_path: Path, installed_appids: set[str]) -> list[tuple[str, Path]]:
    """
    结合 appmanifest（已安装）和 appinfo.vdf（图标哈希），
    返回需要下载/修复的 (appid, icon_file) 列表。
    """
    icon_hashes = get_appinfo_icon_hashes(steam_path, installed_appids)
    if not icon_hashes:
        return []

    icon_dir = steam_path / "steam" / "games"
    icon_dir.mkdir(parents=True, exist_ok=True)

    infos: list[tuple[str, Path]] = []
    for appid, icon_hash in icon_hashes.items():
        icon_file = icon_dir / f"{icon_hash}.ico"
        if not icon_file.exists() or icon_file.stat().st_size == 0:
            infos.append((appid, icon_file))

    return infos


# ================ 入口 ================


async def main() -> None:
    steam_path = get_steam_install_path()
    if not steam_path:
        logger.error("无法从注册表找到 Steam 安装路径")
        return

    installed = get_installed_appids(steam_path)
    if not installed:
        logger.info("未找到已安装的 Steam 游戏")
        return
    logger.info(f"从 Steam 账本中读到 {len(installed)} 个已安装游戏")

    game_infos = get_icon_infos_to_fix(steam_path, installed)
    if not game_infos:
        logger.info("没有需要修复的图标")
        return

    downloader = IconDownloader()
    coroutines = [downloader.download_icon(appid, icon_file) for appid, icon_file in game_infos]
    await asyncio.gather(*coroutines)

    logger.info("Finish Fixing, Flushing icon cache...")
    flush_icon()
    logger.info("Done!")


if __name__ == "__main__":
    asyncio.run(main())
