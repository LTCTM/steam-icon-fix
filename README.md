

# Steam Icon Fix

Steam 图标修复工具 - 自动下载并修复 Steam 游戏图标显示问题。

## 功能介绍

本工具用于解决 Steam 客户端中游戏图标丢失或无法显示的问题。主要功能包括：

- 自动检测 Steam 安装路径
- 获取需要修复图标的游戏列表
- 并发下载游戏图标（限制 5 个并发）
- 自动刷新 Steam 图标缓存

## 环境要求

- **Go 版本**: Go 1.16+
- **Python 版本**: Python 3.8+

## 安装

### Go 版本

```bash
go build -o steam-icon-fix main.go
```

### Python 版本

```bash
pip install -r requirements.txt
```

## 使用方法

### Go 版本

```bash
./steam-icon-fix
```

### Python 版本

```bash
python main.py
```

## 工作原理

1. 自动定位 Steam 安装目录
2. 扫描游戏列表，识别缺失图标的游戏
3. 从 Steam 服务器下载对应图标
4. 刷新本地图标缓存

## 许可证

本项目遵循 MIT 许可证开源。