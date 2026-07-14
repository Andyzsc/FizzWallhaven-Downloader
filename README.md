[English](README.md) | [简体中文](README_CN.md)
<div align="center">

<img src="assets/FizzWallhaven.png" width="150" alt="FizzWallhaven Logo">

# FizzWallhaven 2.1

### A modern Wallhaven wallpaper browser and downloader for Windows

<p>
  <img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Platform-Windows-0078D6?logo=windows&logoColor=white" alt="Windows">
  <img src="https://img.shields.io/badge/Version-2.1-2ECC71" alt="Version">
  <img src="https://img.shields.io/badge/GUI-CustomTkinter-8E44AD" alt="CustomTkinter">
  <img src="https://img.shields.io/badge/API-Wallhaven-6C5CE7" alt="Wallhaven API">
</p>

<p>
  Search, preview, filter, and download wallpapers from Wallhaven through a clean desktop interface.
</p>

</div>

---

## Preview

<div align="center">

<img src="screenshots/main.png" width="900" alt="FizzWallhaven 2.1 Screenshot">

</div>

---

## Highlights

<table>
<tr>
<td width="50%" valign="top">

### Wallpaper browsing

- Search wallpapers by keyword
- Paginated thumbnail preview
- Responsive thumbnail grid
- Previous-page and next-page navigation
- Full-screen interface
- Double-click to open the original wallpaper

</td>
<td width="50%" valign="top">

### Flexible downloads

- Download a single wallpaper
- Download the current preview page
- Batch download a specified quantity
- Pause and stop active tasks
- Skip files that already exist
- Automatically retry temporary failures

</td>
</tr>
</table>

---

## Search filters

FizzWallhaven supports the following Wallhaven search options:

| Filter | Options |
|---|---|
| Sorting | Latest, Hot, Toplist, Random |
| Categories | General, Anime, People |
| Purity | SFW, Sketchy, NSFW |
| Aspect ratio | 16:9, 16:10, 21:9, 4:3, 1:1, All |
| Minimum resolution | All, 1920×1080, 2560×1440, 3840×2160, 5120×2880 |
| Keyword | Optional custom search term |

> NSFW results require a valid Wallhaven account and appropriate account permissions.

---

## Download strategy

FizzWallhaven uses a two-stage download process:

1. Wallpapers are downloaded concurrently at high speed.
2. Requests affected by HTTP `429` or temporary network errors enter an automatic retry queue.
3. Retry tasks continue automatically without restarting the download.
4. Existing files with the same filename are skipped.

This approach keeps the initial download fast while reducing the chance of missing wallpapers.

---

## Requirements

- Windows 10 or Windows 11
- Python 3.12 or later
- A personal Wallhaven API key

---

## Installation

Clone the repository:

```bash
git clone https://github.com/Andyzsc/FizzWallhaven-Downloader.git
cd FizzWallhaven-Downloader
```

Install the required packages:

```bash
python -m pip install -r requirements.txt
```

Run the application:

```bash
python FizzWallhaven2.1.py
```

---

## Quick start

1. Launch `FizzWallhaven2.1.exe`.
2. Enter your Wallhaven API key.
3. Select a folder for downloaded wallpapers.
4. Enter an optional keyword.
5. Choose sorting, category, purity, ratio, and resolution filters.
6. Click **Search and Preview**.
7. Browse wallpapers using the page controls.
8. Download one wallpaper, the current page, or a custom batch.

---

## Mouse and keyboard controls

| Action | Operation |
|---|---|
| Open original wallpaper | Double-click a thumbnail |
| Download one wallpaper | Right-click a thumbnail |
| Toggle full screen | Press `F11` |
| Exit full screen | Press `Esc` |
| Pause or resume download | Press `Space` or use the button |

---

## Wallhaven API key

FizzWallhaven requires your personal Wallhaven API key.

The key is entered inside the application and stored locally in:

```text
config.txt
```

For security:

- The source code does not contain a hard-coded API key.
- `config.txt` is excluded through `.gitignore`.
- Do not upload or share your local `config.txt`.

---

## Project structure

```text
FizzWallhaven-Downloader/
├── assets/
│   ├── FizzWallhaven.ico
│   └── FizzWallhaven.png
├── screenshots/
│   └── main.png
├── .gitignore
├── FizzWallhaven2.1.py
├── README.md
└── requirements.txt
```

---

## Dependencies

```text
requests
customtkinter
Pillow
```

Install them together with:

```bash
python -m pip install -r requirements.txt
```

---

## Known limitations

- Download speed depends on the network connection and Wallhaven rate limits.
- Some wallpapers may become unavailable after removal from Wallhaven.
- An API key is required for account-specific purity permissions.
- The application currently targets Windows.

---

## Roadmap

- [ ] Modularize the current single-file source code
- [x] Publish a packaged Windows executable
- [ ] Add more detailed download statistics
- [ ] Improve preview caching and memory management
- [ ] Create a mobile-friendly FizzWallhaven Web App

---

## Security

Please do not include any of the following files in public commits:

```text
config.txt
.env
.idea/
__pycache__/
build/
dist/
```

These files are already covered by the recommended `.gitignore`.

---

## License

No open-source license has been added yet.

Until a license is provided, the source code remains publicly viewable but is not automatically licensed for redistribution or modification.

---

<div align="center">

### FizzWallhaven 2.1

Built with Python, CustomTkinter, and the Wallhaven API.

</div>
