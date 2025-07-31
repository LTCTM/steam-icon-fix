import asyncio
import logging
import os
import re
import winreg
from pathlib import Path

import httpx

try:
    from rich.logging import RichHandler

    handler = RichHandler(show_path=False, show_time=False)
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(message)s", force=True)
except ImportError:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s\t %(message)s", force=True)

logger = logging.getLogger()
logging.getLogger("httpx").propagate = False
# ================


class IconDownloader:
    async_lock = asyncio.Semaphore(8)

    def __init__(self, timeout=5.0):
        self._time_out = timeout
        # base_url = "http://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps"
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            base_url="http://mirror-api.ltctm.net/steam-icon/steamcommunity/public/images/apps",
        )

    async def download_icon(self, app_id: str, icon_file: Path):
        """下载文件
        :param url: 下载URL
        :param path: 保存路径
        :param timeout: 下载超时时间(秒)，默认5秒
        :return: HTTP状态码或None(失败)
        """
        async with IconDownloader.async_lock:
            try:
                response = await self._client.get(f"{app_id}/{icon_file.name}")
                if response.is_success:
                    icon_file.write_bytes(response.content)
                    logger.info(f"Download icon of <{app_id}> success")
                    result = True
                else:
                    logger.error(f"Download icon of <{app_id}> failed! {response.status_code}: {response.text}")
                    result = False
            except httpx.TimeoutException:
                logger.error(f"Download icon of <{app_id}> timed out")
                result = False
        return result


def flush_icon() -> None:
    """刷新Windows图标缓存"""
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


def get_game_infos_for_download(search_dir: Path) -> list[tuple[str, str]]:
    """
    获取目录中所有有效的Steam快捷方式
    :param search_dir: 目录路径
    :return: 包含有效(URL:str, IconFile:Path)元组的列表
    """
    url_file_paths = [p for p in search_dir.glob("*") if p.suffix.lower() == ".url"]
    result = []
    for url_file_path in url_file_paths:
        content = url_file_path.read_text(encoding="utf-8")
        match_url = re.search(r"URL=(.*)", content)
        match_icon_file = re.search(r"IconFile=(.*)", content)
        if not (match_url and match_icon_file):
            continue
        url = match_url.group(1).strip()
        icon_file = match_icon_file.group(1).strip()
        try:
            icon_file = Path(icon_file)
            # 这里try的是防止icon_file不是合法的文件路径
            if (re.match(r"steam://rungameid/\d+", url) and icon_file.suffix.lower() == ".ico") and (
                not icon_file.exists() or icon_file.stat().st_size == 0
            ):
                result.append((url, icon_file))
        except:
            pass
    return result


def get_steam_install_path() -> Path:
    """获取Steam安装路径
    :return: Steam安装路径或None
    """
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\Valve\\Steam") as key:
            install_path = winreg.QueryValueEx(key, "InstallPath")[0]
            return Path(install_path)
    except WindowsError:
        return None


async def main():
    # 获取Steam安装路径
    steam_path = get_steam_install_path()
    if not steam_path:
        logger.error("Steam is not installed, exiting...")
        return
    # 找到steam_icon_dir
    steam_icon_dir = steam_path / "steam" / "games"
    if not steam_icon_dir.exists():
        steam_icon_dir.mkdir(parents=True, exist_ok=True)
    # 收集所有.url文件
    desktop_dir = Path.home() / "Desktop"
    start_menu_dir = Path(os.getenv("ProgramData", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Steam"
    target_dirs = [target_dir for target_dir in (desktop_dir, start_menu_dir) if target_dir.exists()]
    game_infos = [game_info for tdir in target_dirs for game_info in get_game_infos_for_download(tdir)]
    # ================main================
    downloader = IconDownloader()
    coroutines = [downloader.download_icon(url.split("/")[-1], icon_file) for url, icon_file in game_infos]
    await asyncio.gather(*coroutines)
    # ====
    logger.info("Finish Fixing, Flushing icon cache...")
    flush_icon()
    logger.info("Done!")


if __name__ == "__main__":
    asyncio.run(main())
