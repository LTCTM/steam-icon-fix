import logging
import os
import re
import winreg
from itertools import chain
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


def download_file(url: str, path: Path, timeout: float = 5.0):
    """下载文件
    :param url: 下载URL
    :param path: 保存路径
    :param timeout: 下载超时时间(秒)，默认5秒
    :return: HTTP状态码或None(失败)
    """
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url)
            if response.is_success:
                path.write_bytes(response.content)
                return True
            else:
                logger.error(f"Download failed! {response.status_code}: {response.text}")
                return False
    except httpx.TimeoutException:
        logger.error(f"Download timed out after {timeout} seconds")
        return False


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


def get_valid_steam_shortcuts(path: Path) -> list[tuple[str, str]]:
    """
    获取目录中所有有效的Steam快捷方式

    :param path: 目录路径
    :return: 包含有效(URL, IconFile, FilePath)元组的列表
    """
    result = []
    for url_file_path in chain(path.glob("*.url"), path.glob("*.URL")):
        content = url_file_path.read_text(encoding="utf-8")
        url = re.search(r"URL=(.*)", content)
        icon_file = re.search(r"IconFile=(.*)", content)
        if not (url and icon_file):
            continue
        url = url.group(1).strip()
        icon_file = icon_file.group(1).strip()
        try:
            # 这里try的是防止icon_file不是合法的文件路径
            if re.match(r"steam://rungameid/\d+", url) and Path(icon_file).suffix.lower() == ".ico":
                result.append((url, icon_file, url_file_path))
            else:
                logger.error(f"Invalid url in {url_file_path.name}")
        except:
            logger.error(f"Invalid IconFile in {url_file_path.name}")
    return result


def get_steam_install_path() -> Path:
    """获取Steam安装路径
    :return: Steam安装路径或None
    """
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam") as key:
            install_path = winreg.QueryValueEx(key, "InstallPath")[0]
            return Path(install_path)
    except WindowsError:
        return None


def main():
    # 获取Steam安装路径
    steam_path = get_steam_install_path()
    if not steam_path:
        logger.error("Steam is not installed, exiting...")
        return
    # 找到steam_icon_dir
    steam_icon_dir = steam_path / "steam" / "games"
    if steam_icon_dir.exists():
        logger.info(f"Found Steam icon dir in {steam_icon_dir}")
    else:
        logger.warning("Cannot find steam icon dir, manual input> ")
        user_input = input().strip()
        steam_icon_dir = Path(user_input)
        if not steam_icon_dir.exists():
            logger.error("Path not exist. Exiting...")
            return
    # 获取桌面路径
    desktop_dir = Path.home() / "Desktop"
    if not desktop_dir.exists():
        logger.warning("Cannot find desktop dir, manual input> ")
        user_input = input().strip()
        desktop_dir = Path(user_input)
        if not desktop_dir.exists():
            logger.error("Path not exist. Exiting...")
            return
    # 收集所有.url文件
    url_files = get_valid_steam_shortcuts(desktop_dir)
    # 可选的开始菜单文件夹
    start_menu_dir = Path(os.getenv("ProgramData", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Steam"
    if start_menu_dir.exists():
        url_files.extend(get_valid_steam_shortcuts(start_menu_dir))
    # ====main====
    # icon_base_url = "http://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps/"
    icon_base_url = "http://mirror-api.ltctm.net/steam-icon/steamcommunity/public/images/apps/"
    for url, icon_file, url_file_path in url_files:
        app_id = url.split("/")[-1]
        icon_name = Path(icon_file).name
        download_url = f"{icon_base_url}{app_id}/{icon_name}"
        save_path = steam_icon_dir / icon_name
        if save_path.exists() and save_path.stat().st_size > 0:
            logger.info(f"Found Steam Shortcut {url_file_path.name} with appid {app_id}")
        else:
            logger.info(f"Try to download icon of appid {app_id} to {save_path.name}")
            download_file(download_url, save_path)

    logger.info("Finish Fixing, Flushing icon cache...")
    flush_icon()
    logger.info("Success!")


if __name__ == "__main__":
    main()
    os.system("pause")
