package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

// IconInfo 保存需要下载的图标信息
type IconInfo struct {
	AppID    string
	IconPath string
}

// ================ VDF/ACF 文本解析 ================

type vdfToken struct {
	kind string // "str", "open", "close"
	val  string
}

// tokenizeVDF 把 VDF/ACF 文本拆成字符串、{、} 三种 token
func tokenizeVDF(s string) []vdfToken {
	var tokens []vdfToken
	for i := 0; i < len(s); {
		c := s[i]
		if c == ' ' || c == '\t' || c == '\n' || c == '\r' {
			i++
			continue
		}
		if c == '"' {
			i++
			var b strings.Builder
			for i < len(s) {
				if s[i] == '\\' && i+1 < len(s) {
					b.WriteByte(s[i+1])
					i += 2
				} else if s[i] == '"' {
					i++
					break
				} else {
					b.WriteByte(s[i])
					i++
				}
			}
			tokens = append(tokens, vdfToken{kind: "str", val: b.String()})
			continue
		}
		if c == '{' {
			tokens = append(tokens, vdfToken{kind: "open"})
			i++
			continue
		}
		if c == '}' {
			tokens = append(tokens, vdfToken{kind: "close"})
			i++
			continue
		}
		i++
	}
	return tokens
}

// parseVDF 递归解析 token 为嵌套 map
func parseVDF(tokens []vdfToken, idx int) (map[string]interface{}, int) {
	m := make(map[string]interface{})
	for idx < len(tokens) {
		t := tokens[idx]
		if t.kind == "close" {
			return m, idx + 1
		}
		if t.kind != "str" {
			idx++
			continue
		}

		key := t.val
		idx++
		if idx >= len(tokens) {
			break
		}

		nxt := tokens[idx]
		if nxt.kind == "str" {
			m[key] = nxt.val
			idx++
		} else if nxt.kind == "open" {
			child, nextIdx := parseVDF(tokens, idx+1)
			m[key] = child
			idx = nextIdx
		} else {
			idx++
		}
	}
	return m, idx
}

// ================ Steam 安装路径 / 库 / 已安装游戏 ================

// queryRegInstallPath 用 reg 命令读取 Steam 安装路径
func queryRegInstallPath(subkey string) string {
	cmd := exec.Command("reg", "query", subkey, "/v", "InstallPath")
	out, err := cmd.Output()
	if err != nil {
		return ""
	}
	lines := strings.Split(string(out), "\n")
	for _, line := range lines {
		if !strings.Contains(line, "InstallPath") {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) >= 3 {
			return strings.Join(fields[2:], " ")
		}
	}
	return ""
}

// getSteamInstallPath 获取 Steam 安装路径
func getSteamInstallPath() string {
	candidates := []string{
		`HKLM\SOFTWARE\WOW6432Node\Valve\Steam`,
		`HKLM\SOFTWARE\Valve\Steam`,
	}
	for _, subkey := range candidates {
		if path := queryRegInstallPath(subkey); path != "" {
			return path
		}
	}
	return ""
}

// collectLibraryPaths 递归收集 VDF 中所有 steamapps 目录
func collectLibraryPaths(v interface{}, found map[string]struct{}) {
	node, ok := v.(map[string]interface{})
	if !ok {
		return
	}
	for _, val := range node {
		s, ok := val.(string)
		if ok {
			if strings.EqualFold(filepath.Base(s), "steamapps") {
				if info, err := os.Stat(s); err == nil && info.IsDir() {
					found[s] = struct{}{}
				}
			}
			joined := filepath.Join(s, "steamapps")
			if info, err := os.Stat(joined); err == nil && info.IsDir() {
				found[joined] = struct{}{}
			}
		} else {
			collectLibraryPaths(val, found)
		}
	}
}

// getSteamLibraries 返回所有 Steam 库目录（包含默认库）
func getSteamLibraries(steamPath string) []string {
	found := make(map[string]struct{})

	defaultLib := filepath.Join(steamPath, "steamapps")
	if info, err := os.Stat(defaultLib); err == nil && info.IsDir() {
		found[defaultLib] = struct{}{}
	}

	candidates := []string{
		filepath.Join(defaultLib, "libraryfolders.vdf"),
		filepath.Join(steamPath, "config", "libraryfolders.vdf"),
	}
	for _, vdfPath := range candidates {
		data, err := os.ReadFile(vdfPath)
		if err != nil {
			continue
		}
		text := strings.TrimPrefix(string(data), "\ufeff")
		tokens := tokenizeVDF(text)
		root, _ := parseVDF(tokens, 0)
		collectLibraryPaths(root, found)
	}

	libs := make([]string, 0, len(found))
	for p := range found {
		libs = append(libs, p)
	}
	sort.Strings(libs)
	return libs
}

// getInstalledAppIDs 扫描所有库的 appmanifest_*.acf，返回已安装游戏的 appid 集合
func getInstalledAppIDs(steamPath string) map[string]struct{} {
	appIDs := make(map[string]struct{})
	appIDRe := regexp.MustCompile(`^\d+$`)

	for _, lib := range getSteamLibraries(steamPath) {
		pattern := filepath.Join(lib, "appmanifest_*.acf")
		matches, err := filepath.Glob(pattern)
		if err != nil {
			continue
		}
		for _, m := range matches {
			data, err := os.ReadFile(m)
			if err != nil {
				continue
			}
			text := strings.TrimPrefix(string(data), "\ufeff")
			tokens := tokenizeVDF(text)
			root, _ := parseVDF(tokens, 0)

			state, ok := root["AppState"].(map[string]interface{})
			if !ok {
				state = root
			}

			appid, _ := state["appid"].(string)
			if appid == "" {
				appid, _ = state["appID"].(string)
			}
			if appid != "" && appIDRe.MatchString(appid) {
				appIDs[appid] = struct{}{}
			}
		}
	}
	return appIDs
}

// ================ appinfo.vdf 二进制解析 ================

func loadAppInfoStrings(data []byte, tableOffset int) []string {
	count := int(binary.LittleEndian.Uint32(data[tableOffset : tableOffset+4]))
	strings := make([]string, 0, count)
	off := tableOffset + 4
	for i := 0; i < count && off < len(data); i++ {
		end := bytes.IndexByte(data[off:], 0)
		if end == -1 {
			break
		}
		strings = append(strings, string(data[off:off+end]))
		off += end + 1
	}
	return strings
}

type keyReaderFunc func([]byte, int) (string, int)

func makeKeyReader(strings []string) keyReaderFunc {
	if strings != nil {
		return func(d []byte, off int) (string, int) {
			idx := binary.LittleEndian.Uint32(d[off : off+4])
			if int(idx) < len(strings) {
				return strings[idx], off + 4
			}
			return fmt.Sprintf("#%d", idx), off + 4
		}
	}
	return func(d []byte, off int) (string, int) {
		end := bytes.IndexByte(d[off:], 0)
		if end == -1 {
			return "", len(d)
		}
		return string(d[off : off+end]), off + end + 1
	}
}

func parseBinaryVDF(data []byte, off, end int, readKey keyReaderFunc) (map[string]interface{}, int) {
	stack := []map[string]interface{}{make(map[string]interface{})}
	for off < end {
		t := data[off]
		off++
		if t == 0x08 || t == 0x0B {
			if len(stack) > 1 {
				stack = stack[:len(stack)-1]
			} else {
				break
			}
			continue
		}

		key, nextOff := readKey(data, off)
		off = nextOff

		switch t {
		case 0x00: // node
			child := make(map[string]interface{})
			stack[len(stack)-1][key] = child
			stack = append(stack, child)
		case 0x01: // string
			sEnd := off + bytes.IndexByte(data[off:], 0)
			stack[len(stack)-1][key] = string(data[off:sEnd])
			off = sEnd + 1
		case 0x02: // int32
			stack[len(stack)-1][key] = int32(binary.LittleEndian.Uint32(data[off : off+4]))
			off += 4
		case 0x03, 0x04, 0x06: // float / pointer / color
			off += 4
		case 0x05: // wide string
			sEnd := off
			for !(data[sEnd] == 0 && data[sEnd+1] == 0) {
				sEnd += 2
			}
			stack[len(stack)-1][key] = string(data[off:sEnd]) // 简单按字节存
			off = sEnd + 2
		case 0x07: // uint64
			stack[len(stack)-1][key] = binary.LittleEndian.Uint64(data[off : off+8])
			off += 8
		case 0x0A: // int64
			stack[len(stack)-1][key] = int64(binary.LittleEndian.Uint64(data[off : off+8]))
			off += 8
		default:
			// 未知类型，直接退出防止死循环
			return stack[0], off
		}
	}
	return stack[0], off
}

// getAppInfoIconHashes 读取 appinfo.vdf，返回 {appid: icon_hash}
func getAppInfoIconHashes(steamPath string, installed map[string]struct{}) map[string]string {
	hashes := make(map[string]string)

	appinfoPath := filepath.Join(steamPath, "appcache", "appinfo.vdf")
	data, err := os.ReadFile(appinfoPath)
	if err != nil {
		return hashes
	}
	if len(data) < 16 {
		return hashes
	}

	magic := binary.LittleEndian.Uint32(data[0:4])
	var entryOff int
	var readKey keyReaderFunc

	switch magic {
	case 0x07564429: // v41
		strings := loadAppInfoStrings(data, int(binary.LittleEndian.Uint64(data[8:16])))
		entryOff = 16
		readKey = makeKeyReader(strings)
	case 0x07564428: // v40
		entryOff = 8
		readKey = makeKeyReader(nil)
	default:
		log.Printf("不支持的 appinfo.vdf 格式: %x", magic)
		return hashes
	}

	appIDRe := regexp.MustCompile(`^\d+$`)

	off := entryOff
	for off+68 <= len(data) {
		appid := binary.LittleEndian.Uint32(data[off : off+4])
		if appid == 0 {
			break
		}
		size := binary.LittleEndian.Uint32(data[off+4 : off+8])
		if size < 60 {
			break
		}
		vdfLen := int(size - 60)
		vdfOff := off + 68

		appIDStr := strconv.FormatUint(uint64(appid), 10)
		if _, ok := installed[appIDStr]; ok && vdfOff+vdfLen <= len(data) && appIDRe.MatchString(appIDStr) {
			root, _ := parseBinaryVDF(data, vdfOff, vdfOff+vdfLen, readKey)
			appinfo, _ := root["appinfo"].(map[string]interface{})
			if appinfo == nil {
				appinfo = root
			}
			common, _ := appinfo["common"].(map[string]interface{})
			if common != nil {
				hash, _ := common["clienticon"].(string)
				if hash != "" {
					hashes[appIDStr] = hash
				}
			}
		}

		off += 8 + 60 + vdfLen
	}

	return hashes
}

// ================ 图标下载 ================

// getIconInfosToFix 返回需要下载/修复的 (appid, icon_file) 列表
func getIconInfosToFix(steamPath string, installed map[string]struct{}) []IconInfo {
	iconHashes := getAppInfoIconHashes(steamPath, installed)
	if len(iconHashes) == 0 {
		return nil
	}

	iconDir := filepath.Join(steamPath, "steam", "games")
	_ = os.MkdirAll(iconDir, 0755)

	var infos []IconInfo
	for appid, iconHash := range iconHashes {
		iconPath := filepath.Join(iconDir, iconHash+".ico")
		info, err := os.Stat(iconPath)
		if err != nil || info.Size() == 0 {
			infos = append(infos, IconInfo{
				AppID:    appid,
				IconPath: iconPath,
			})
		}
	}

	// 让输出顺序稳定
	sort.Slice(infos, func(i, j int) bool {
		return infos[i].AppID < infos[j].AppID
	})
	return infos
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

// ================ 入口 ================

func main() {
	steamPath := getSteamInstallPath()
	if steamPath == "" {
		log.Println("无法从注册表找到 Steam 安装路径")
		return
	}

	installed := getInstalledAppIDs(steamPath)
	if len(installed) == 0 {
		log.Println("未找到已安装的 Steam 游戏")
		return
	}
	log.Printf("从 Steam 账本中读到 %d 个已安装游戏", len(installed))

	allInfos := getIconInfosToFix(steamPath, installed)
	if len(allInfos) == 0 {
		log.Println("没有需要修复的图标")
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
		sem <- struct{}{}
		go func(info IconInfo) {
			defer wg.Done()
			defer func() { <-sem }()
			downloadIcon(client, info)
		}(info)
	}

	wg.Wait()
	log.Println("Finish fixing, flushing icon cache...")
	flushIcon()
	log.Println("Done!")
}
