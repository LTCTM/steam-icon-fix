# Steam Icon Fix

Steam Icon Fix Tool - Automatically downloads and fixes Steam game icon display issues.

## Features

This tool addresses missing or non-displaying game icons in the Steam client. Key features include:

- Automatic detection of Steam installation path
- Retrieval of a list of games requiring icon fixes
- Concurrent downloading of game icons (limited to 5 concurrent downloads)
- Automatic refreshing of Steam icon cache

## System Requirements

- **Go Version**: Go 1.16+
- **Python Version**: Python 3.8+

## Installation

### Go Version

```bash
go build -o steam-icon-fix main.go
```

### Python Version

```bash
pip install -r requirements.txt
```

## Usage

### Go Version

```bash
./steam-icon-fix
```

### Python Version

```bash
python main.py
```

## How It Works

1. Automatically locates the Steam installation directory
2. Scans the game library to identify games with missing icons
3. Downloads the corresponding icons from Steam servers
4. Refreshes the local icon cache

## License

This project is open-source under the MIT License.