package main

import (
	"bufio"
	"context"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"syscall"
	"time"

	"net/http"
	"io"
)

// IconInfo 保存需要下载的图标信息
type IconInfo struct {
	AppID   string
	IconPath string
}

// getSteamInstallPath 获取 Steam 安装路径
// :return: Steam 安装路径或空字符串
func getSteamInstallPath() string {
	// 使用 reg 命令查询注册表，避免依赖外部 Go 模块
	cmd := exec.Command("reg", "query", `HKLM\SOFTWARE\WOW6432Node\Valve\Steam`, "/v", "InstallPath")
	output, err := cmd.Output()
	if err != nil {
		return ""
	}
	scanner := bufio.NewScanner(strings.NewReader(string(output)))
	for scanner.Scan() {
		line := scanner.Text()
		// 典型输出中包含 "InstallPath    REG_SZ    C:\Program Files (x86)\Steam"
		if strings.Contains(line, "InstallPath") {
			parts := strings.Fields(line)
			if len(parts) >= 3 {
				return strings.Join(parts[2:], " ")
			}
		}
	}
	return ""
}

// getGameInfosForDownload 获取目录中所有有效的 Steam 快捷方式
// :param searchDir:
// :return: 包含有效(AppID, IconPath)元组的列表
func getGameInfosForDownload(searchDir string) []IconInfo {
	entries, err := os.ReadDir(searchDir)
	if err != nil {
		return nil
	}
	var result []IconInfo
	urlRe := regexp.MustCompile(`^URL=(.*)`)
	iconRe := regexp.MustCompile(`^IconFile=(.*)`)

	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		if strings.ToLower(filepath.Ext(e.Name())) != ".url" {
			continue
		}
		fullPath := filepath.Join(searchDir, e.Name())
		f, err := os.Open(fullPath)
		if err != nil {
			continue
		}
		scanner := bufio.NewScanner(f)
		var urlVal string
		var iconVal string
		for scanner.Scan() {
			line := scanner.Text()
			if m := urlRe.FindStringSubmatch(line); m != nil {
				urlVal = strings.TrimSpace(m[1])
			}
			if m := iconRe.FindStringSubmatch(line); m != nil {
				iconVal = strings.TrimSpace(m[1])
			}
		}
		_ = f.Close()
		if urlVal == "" || iconVal == "" {
			continue
		}
		parsedIcon := iconVal
		// 判断 url 和 icon 是否符合要求
		if strings.HasPrefix(urlVal, "steam://rungameid/") && strings.ToLower(filepath.Ext(parsedIcon)) == ".ico" {
			info, err := os.Stat(parsedIcon)
			if err == nil && info.Size() > 0 {
				continue
			}
			appID := strings.TrimPrefix(urlVal, "steam://rungameid/")
			result = append(result, IconInfo{
				AppID:   appID,
				IconPath: parsedIcon,
			})
		}
	}
	return result
}

// flushIcon 刷新 Windows 图标缓存
func flushIcon() {
	homeDir, err := os.UserHomeDir()
	if err != nil {
		log.Println("Get user home failed")
		return
	}
	iconCache := filepath.Join(homeDir, "AppData", "Local", "IconCache.db")
	explorerCache := filepath.Join(homeDir, "AppData", "Local", "Microsoft", "Windows", "Explorer")

	commands := []string{
		`taskkill /f /im explorer.exe`,
		fmt.Sprintf(`attrib -h -s -r "%s"`, iconCache),
		fmt.Sprintf(`del /f "%s"`, iconCache),
		fmt.Sprintf(`attrib /s /d -h -s -r "%s\*"`, explorerCache),
		fmt.Sprintf(`del /f "%s\thumbcache_32.db"`, explorerCache),
		fmt.Sprintf(`del /f "%s\thumbcache_96.db"`, explorerCache),
		fmt.Sprintf(`del /f "%s\thumbcache_102.db"`, explorerCache),
		fmt.Sprintf(`del /f "%s\thumbcache_256.db"`, explorerCache),
		fmt.Sprintf(`del /f "%s\thumbcache_1024.db"`, explorerCache),
		fmt.Sprintf(`del /f "%s\thumbcache_idx.db"`, explorerCache),
		fmt.Sprintf(`del /f "%s\thumbcache_sr.db"`, explorerCache),
		`echo y | reg delete "HKEY_CLASSES_ROOT\Local Settings\Software\Microsoft\Windows\CurrentVersion\TrayNotify" /v IconStreams`,
		`echo y | reg delete "HKEY_CLASSES_ROOT\Local Settings\Software\Microsoft\Windows\CurrentVersion\TrayNotify" /v PastIconsStream`,
		`start explorer`,
	}

	for _, cmdStr := range commands {
		cmd := exec.Command("cmd.exe", "/C", cmdStr)
		cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
		_ = cmd.Run()
	}
}

// downloadIcon 下载图标
// :param client:
// :param info:
// :return:
func downloadIcon(client *http.Client, info IconInfo) bool {
	url := "http://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps/" + info.AppID + "/" + filepath.Base(info.IconPath)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		log.Println("Create request failed")
		return false
	}
	resp, err := client.Do(req)
	if err != nil {
		log.Println("Download icon failed")
		return false
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		log.Println("Download icon failed with status " + resp.Status)
		return false
	}

	if err := os.MkdirAll(filepath.Dir(info.IconPath), 0755); err != nil {
		log.Println("Create icon directory failed")
		return false
	}

	f, err := os.Create(info.IconPath)
	if err != nil {
		log.Println("Open icon file failed")
		return false
	}
	defer f.Close()

	_, err = io.Copy(f, resp.Body)
	if err != nil {
		log.Println("Write icon file failed")
		return false
	}
	log.Println("Download icon of <", info.AppID, "> success")
	return true
}

func main() {
	steamPath := getSteamInstallPath()
	if steamPath == "" {
		log.Println("Steam is not installed, exiting...")
		return
	}

	steamIconDir := filepath.Join(steamPath, "steam", "games")
	_ = os.MkdirAll(steamIconDir, 0755)

	homeDir, err := os.UserHomeDir()
	if err != nil {
		log.Println("Get user home failed")
		return
	}
	desktopDir := filepath.Join(homeDir, "Desktop")
	programData := os.Getenv("ProgramData")
	startMenuDir := filepath.Join(programData, "Microsoft", "Windows", "Start Menu", "Programs", "Steam")

	var allInfos []IconInfo
	for _, dir := range []string{desktopDir, startMenuDir} {
		if dir == "" {
			continue
		}
		if st, err := os.Stat(dir); err == nil && st.IsDir() {
			infos := getGameInfosForDownload(dir)
			allInfos = append(allInfos, infos...)
		}
	}

	if len(allInfos) == 0 {
		log.Println("No icons to fix")
		return
	}

	client := &http.Client{
		Timeout: 10 * time.Second,
	}

	var wg sync.WaitGroup
	// 限制同时下载的并发数为 5
	sem := make(chan struct{}, 5)

	for _, info := range allInfos {
		wg.Add(1)
		go func(info IconInfo) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			_ = downloadIcon(client, info)
		}(info)
	}

	wg.Wait()
	log.Println("Finish fixing, flushing icon cache...")
	flushIcon()
	log.Println("Done!")
}

