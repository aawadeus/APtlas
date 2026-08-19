## APtlas - Wi-Fi Coverage Assessment and Heatmap Visualization

<img width="1275" height="640" alt="aptlas cover" src="https://github.com/user-attachments/assets/f88fc433-dfe1-447e-aac7-415d0088e959" />

## Setup

### Requirements

- **Python 3.10+** - `aptlas.py` uses `match`/`case`.
- **A graphical display** - the interactive matplotlib windows needed to visualize cannot run headless (e.g. over SSH without X forwarding). A headless mode is WIP.  
- **Wireless Interface With Monitor Mode Support** - needed for `scanner.py`, developed for Kali Linux and currently untested on any other OS.
- **Linux + Root For Surveying** - the scanner aside, building gridmaps, heatmaps and running the coverage algorithm should work on any OS as long as all python libraries are installed.
- **SNR Measurement Capable Adapter** - for SNR readings, the adapter used for surveys needs to expose noise data. For adapters that use the Realtek RTL8814AU driver, a patched driver can be found here: https://github.com/aawadeus/aptlas-8814au  

### Install

```bash
git clone https://github.com/aawadeus/APtlas
cd APtlas
pip install -r requirements.txt
```

The survey step also shells out to system tools that pip cannot install. They ship with Kali by default. On other Debian-based systems, they can be installed like so:

```bash
sudo apt install aircrack-ng wireless-tools
```

`aircrack-ng` provides `airmon-ng`, and `wireless-tools` provides `iwconfig` and `iwlist`.

## Usage

`aptlas.py` ties the pipeline together behind a menu. Each step feeds the next through a shared gridmap JSON, so run them in order:

```bash
# 1. Build a gridmap: place measurement points on a floor plan
python3 aptlas.py -f pages/survey/floorplan.png

# 2. Survey: Walk the points and capture signal data at each measurement point (needs root + monitor mode)
sudo python3 aptlas.py -g pages/survey/floorplan_gridmap.json -i wlan1

# 3/4. Heatmap and AP placement: Once the grid map has survey data
python3 aptlas.py -g pages/survey/floorplan_gridmap.json -f pages/survey/floorplan.png
```

The menu shows which gridmap is loaded and whether it has been surveyed yet:

| Option | Stage | Needs |
| --- | --- | --- |
| 1 | Build gridmap (`gridmap.py`) | `-f` floorplan, graphical display |
| 2 | Network survey (`scanner.py`) | `-g` gridmap, `-i` interface, root |
| 3 | Heatmap (`heatmap.py`) | surveyed `-g` gridmap, graphical display |
| 4 | AP Placement Algorithm (`echidna.py`) | `-g` gridmap (surveyed, unless baseline mode), `-f` floorplan, graphical display |

| Flag | Purpose |
| --- | --- |
| `-g`, `--gridmap` | Gridmap JSON. Not needed to build one (step 1); required for steps 2-4. |
| `-f`, `--floorplan` | Floorplan image. Overrides the path stored in the grid map JSON. |
| `-i`, `--int` | Wireless interface for the survey, e.g. `wlan1`. |
| `-r`, `--readings` | Readings averaged per measurement point (default: 3). |
| `-b`, `--band` | Default survey band: `all`, `2.4` or `5` (default: `all`). `5` gives readable SNR on every point; `2.4` has no SNR on CCK-only adapters. The survey menu can override it per run. |
| `--ssid` | Measure coverage for this network only. Every AP is still recorded, so channel and interference decisions are unaffected. If omitted, the survey and heatmap steps prompt for it. |
| `--baseline` | Default the optimizer to baseline mode: one suggested AP at the centre of the measured area, for a survey taken before the target network is deployed. Skips the RSSI requirement, and the optimizer menu can override it per run. |
| `-s`, `--scale` | Metres per pixel, used when building a gridmap (default: 0.05). |
| `--gridspacing` | Grid spacing in metres, used when building a gridmap (default: 1.0). |

The grid map builder exports its JSON and reference image next to the source floor plan, so a floor plan in `pages/survey/` keeps its grid map in `pages/survey/` too.

Place **at least 4 measurement points:** both the export and the heatmap interpolation require them.

### Running the modules directly

Each stage also works standalone, with its own flags:

```bash
python3 gridmap.py -f pages/survey/floorplan.png [-s 0.016942] [--gridspacing 1.0]
sudo python3 scanner.py -g pages/survey/floorplan_gridmap.json -i wlan1 [-r 3] [-b 5] [--ssid NAME]
python3 heatmap.py -g pages/survey/floorplan_gridmap.json [-s NAME]
python3 echidna.py -g pages/survey/floorplan_gridmap.json -f pages/survey/floorplan.png [-b 5] [--baseline]
```

Note the differences from the `aptlas.py` flags: `heatmap.py` uses `-s`/`--ssid` (it takes the floorplan from the gridmap JSON), and `echidna.py` requires `-f` and accepts only `2.4` or `5` for `-b/--band`.

## Repo Layout

| Path | Contents |
| --- | --- |
| `pages/` | Intended directory for floor plans, gridmaps and reference images. After all, an atlas is full of  pages that contain maps!! |
| `thesis/survey` | Gridmap JSON files belonging to the thesis project can be accessed here. The floor plans are omitted for confidentiality. |
