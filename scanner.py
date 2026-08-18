import re
import sys
import json
import time
import argparse
import threading
import itertools
import subprocess
from pathlib import Path
from statistics import median
from aptlas import Swag, color
from collections import defaultdict

try:
    from scapy.all import sniff, Dot11Elt, RadioTap, Dot11, Dot11Beacon
except ImportError:
    print("Error: Scapy is not installed. Run: pip install scapy")
    sys.exit(1)

class Spinner:
    FRAMES = "|/-\\"

    def __init__(self, message, indent="      "):
        self.message = message
        self.indent = indent
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join()
        sys.stdout.write("\r\033[K") # clear the spinner line
        sys.stdout.flush()

    def _spin(self):
        for frame in itertools.cycle(self.FRAMES):
            if self._stop.is_set():
                break
            sys.stdout.write(f"\r{self.indent}{Swag.CYAN}{frame}{Swag.RESET} {self.message}")
            sys.stdout.flush()
            self._stop.wait(0.1)

SCAN_TIMEOUT    = 3     # seconds to listen per channel
SCAN_COUNT      = 30    # max packets per channel scan
NUM_READINGS    = 5     # number of readings to average per point
SUPPORTED_BANDS = True  # scan both 2.4 GHz and 5 GHz channels

# default Pi drivers hangs after monitor mode and needs a reload
RELOAD_DRIVERS = {"brcmfmac"}

NOISE_FLOOR_MIN = -100  # dBm - below the physical thermal floor
NOISE_FLOOR_MAX = -50   # dBm - a floor stronger than this is not credible
SNR_MAX_DB      = 70    # dB  - beyond this the SNR field is saturated/garbage

# band split by channel number
BAND_24_MAX_CHANNEL = 14


def band_of(channel):
    try:
        ch = int(channel)
    except (TypeError, ValueError):
        return None
    return "2.4" if ch <= BAND_24_MAX_CHANNEL else "5"


def filter_channels_by_band(channels, band):
    if band == "all":
        return list(channels)
    return [ch for ch in channels if band_of(ch) == band]

# MONITOR MODE (airmon-ng)
def interface_driver(interface):
    # get the kernel driver bound to an interface
    link = Path(f"/sys/class/net/{interface}/device/driver")
    
    try:
        if not link.is_symlink():
            return None
        return link.resolve().name
    except OSError:
        return None


def monitor_interfaces():
    try:
        output = subprocess.check_output(
            ["iw", "dev"], stderr=subprocess.DEVNULL
        ).decode()
    except Exception:
        return []

    monitors = []
    # parse monitor-mode interfaces from iw
    for block in re.split(r"\bInterface\s+", output)[1:]:
        name = block.split()[0]
        if re.search(r"^\s*type\s+monitor\s*$", block, re.M):
            monitors.append(name)

    return monitors


def start_monitor(interface):
    print(f"\n[*] Starting monitor mode on {interface}...")

    killed_procs = []
    before = monitor_interfaces() # track newly created monitor interface
    driver = interface_driver(interface) # save before airmon-ng renames it

    try:
        # stop processes that interfere with monitor mode
        proc_check = subprocess.check_output(
            ["airmon-ng", "check"], stderr=subprocess.DEVNULL
        ).decode()
        
        # de-duplicates while keeping order
        killed_procs = list(dict.fromkeys(re.findall(r"\d+\s+(\S+)", proc_check)[1:]))

        if killed_procs:
            subprocess.check_output(
                ["airmon-ng", "check", "kill"], stderr=subprocess.DEVNULL
            )
            print(f"    Killed {len(killed_procs)} interfering processes")

        start_output = subprocess.check_output(
            ["airmon-ng", "start", interface], stderr=subprocess.DEVNULL
        ).decode()

        # resolve monitor interface name
        after = monitor_interfaces()
        created = [m for m in after if m not in before]

        if created:
            # prefer the interface derived from the requested name
            mon_name = next((m for m in created if m.startswith(interface)), created[0])
        elif interface in after:
            mon_name = interface # driver kept the same name
        elif after:
            mon_name = after[0] # fall back to another monitor interface
        else:
            print(f"    Error: no interface is in monitor mode after "
                  f"'airmon-ng start {interface}'")
            print(f"    airmon-ng said:\n{start_output.strip()}")
            return {"status": 1, "name": None, "killed": killed_procs}

        print(f"    Monitor mode active: {mon_name}"
              + (f" (driver: {driver})" if driver else ""))
        return {"status": 0, "name": mon_name, "killed": killed_procs,
                "driver": driver}

    except FileNotFoundError:
        print("    Error: airmon-ng not found. Is it installed?")
        return {"status": 1, "name": None, "killed": []}
    except Exception as e:
        print(f"    Error starting monitor mode: {e}")
        return {"status": 1, "name": None, "killed": killed_procs}


def stop_monitor(monitor_info):
    if monitor_info["name"]:
        try:
            subprocess.check_output(
                ["airmon-ng", "stop", monitor_info["name"]],
                stderr=subprocess.DEVNULL
            )
            print(f"\n[*] Monitor mode stopped: {monitor_info['name']}")
        except Exception:
            print(f"\n[!] Failed to stop monitor mode")

    if monitor_info["killed"]:
        print("    Restarting processes:")
        
        for proc in monitor_info["killed"]:
            subprocess.run(
                ["systemctl", "start", proc],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            print(f"      {proc}")

    driver = monitor_info.get("driver")

    if driver in RELOAD_DRIVERS:
        print(f"    Restarting WLAN driver ({driver}): ", end="")
        
        try:
            subprocess.check_output(["modprobe", "-r", driver],
                                    stderr=subprocess.DEVNULL)
            subprocess.check_output(["modprobe", driver],
                                    stderr=subprocess.DEVNULL)
            print("OK")
        except Exception:
            print("FAILED (may need manual restart)")

    elif driver:
        print(f"    Driver reload not needed for {driver}")


# CHANNEL SCANNING
def get_supported_channels(interface):
    # prefer iw for monitor-mode channel detection (iwlist is unreliable)
    for source, fetch in (("iw", _channels_from_iw), ("iwlist", _channels_from_iwlist)):
        try:
            channels = fetch(interface)
        except Exception as e:
            print(f"    Warning: {source} channel lookup failed on {interface}: {e}")
            continue

        if channels:
            return channels

        print(f"    Warning: {source} reported no channels for {interface}")

    # fallback: standard 2.4 GHz channels
    print("    Warning: could not detect channels, using defaults (1-13)")
    
    return list(range(1, 14))


def _channels_from_iw(interface):
    info = subprocess.check_output(
        ["iw", "dev", interface, "info"], stderr=subprocess.DEVNULL
    ).decode()

    wiphy = re.search(r"wiphy\s+(\d+)", info)
    
    if not wiphy:
        return []

    phy_info = subprocess.check_output(
        ["iw", "phy", f"phy{wiphy.group(1)}", "info"], stderr=subprocess.DEVNULL
    ).decode()

    channels = []
    for _freq, channel, flags in re.findall(
        r"\*\s+(\d+)\s+MHz\s+\[(\d+)\]([^\n]*)", phy_info
    ):
        # skip disabled channels (regulatory domain)
        if "disabled" in flags.lower():
            continue
        channels.append(int(channel))

    return sorted(set(channels))


def _channels_from_iwlist(interface):
    output = subprocess.check_output(
        ["iwlist", interface, "channel"], stderr=subprocess.DEVNULL
    ).decode()
    
    return [int(ch) for ch in re.findall(r"Channel\s+(\d+)\s*:", output)]


def set_channel(interface, channel):
    # fall back to iwconfig if iw fails
    for cmd in (["iw", "dev", interface, "set", "channel", str(channel)],
                ["iwconfig", interface, "channel", str(channel)]):
        try:
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True
            )
            return True
        except Exception:
            # try the next method if channel setting fails
            continue

    return False


# BEACON FRAME CAPTURE
BEACON_BPF = "type mgt subtype beacon"
_use_bpf_filter = True  # disabled if libpcap cannot compile the filter


def plausible_noise(rssi, noise):
    # some adapters report invalid noise values, especially on CCK/weak OFDM frames
    # reject these so coverage falls back to RSSI instead of bogus SNR
    if rssi is None or noise is None:
        return None

    if not NOISE_FLOOR_MIN <= noise <= NOISE_FLOOR_MAX:
        return None

    snr = rssi - noise
    if snr <= 0 or snr > SNR_MAX_DB:
        return None

    return noise


def sniff_beacons(interface):
    # BPF beacon filtering requires an 802.11/radiotap datalink
    # fall back to Python filtering when the driver does not expose a valid one
    global _use_bpf_filter

    if _use_bpf_filter:
        try:
            return sniff(
                iface=interface,
                filter=BEACON_BPF,
                count=SCAN_COUNT,
                timeout=SCAN_TIMEOUT,
                store=True
            )
        except Exception as e:
            if "filter" not in str(e).lower():
                raise  # let the caller handle capture errors

            print("      Note: kernel BPF filter unsupported on this driver - "
                  "filtering beacons in Python instead")
            _use_bpf_filter = False

    return sniff(
        iface=interface,
        lfilter=lambda p: p.haslayer(Dot11Beacon),
        count=SCAN_COUNT,
        timeout=SCAN_TIMEOUT,
        store=True
    )


def capture_at_point(interface, channels):
    detected = {}
    channels_set = 0

    if not channels:
        print("      Error: no channels to scan - nothing will be captured")
        return detected

    for channel in channels:
        if set_channel(interface, channel):
            channels_set += 1
        time.sleep(0.1)  # brief settle time after channel change

        try:
            packets = sniff_beacons(interface)
        except Exception as e:
            print(f"      Error on channel {channel}: {e}")
            continue

        for pkt in packets:
            try:
                if not pkt.haslayer(Dot11Beacon):
                    continue

                bssid = pkt[Dot11].addr2
                if bssid is None:
                    continue

                # extract SSID
                ssid = ""
                try:
                    ssid = pkt[Dot11Elt].info.decode(errors="replace")
                except Exception:
                    ssid = ""

                # extract RSSI from RadioTap
                rssi = None
                noise = None
                
                if pkt.haslayer(RadioTap):
                    rt = pkt[RadioTap]
                    rssi = getattr(rt, 'dBm_AntSignal', None)
                    # discard invalid noise values
                    noise = plausible_noise(rssi, getattr(rt, 'dBm_AntNoise', None))

                # extract channel from Dot11Elt
                try:
                    ap_channel = None

                    # DS Parameter Set (ID 3) carries the channel;
                    # fall back to its raw byte if Scapy does not expose .channel
                    ds = pkt.getlayer(Dot11Elt, ID=3)
                    
                    if ds is not None:
                        ap_channel = getattr(ds, "channel", None)
                        if ap_channel is None and len(ds.info) == 1:
                            ap_channel = ds.info[0]

                    # 5 GHz beacons usually omit the DS set
                    # HT Operation (ID 61) starts with the primary channel number
                    if ap_channel is None:
                        ht = pkt.getlayer(Dot11Elt, ID=61)
                        if ht is not None and len(ht.info) >= 1:
                            ap_channel = ht.info[0]

                    # fall back to the scanned channel
                    if ap_channel is None:
                        ap_channel = channel

                except Exception:
                    ap_channel = channel  # fallback to scanned channel

                # keep the strongest observation per BSSID
                if bssid not in detected:
                    detected[bssid] = {
                        "bssid": bssid,
                        "ssid": ssid,
                        "channel": ap_channel,
                        "rssi": rssi,
                        "noise": noise,
                    }
                elif rssi is not None and detected[bssid]["rssi"] is not None:
                    if rssi > detected[bssid]["rssi"]:
                        detected[bssid]["rssi"] = rssi
                        detected[bssid]["noise"] = noise

            except Exception:
                continue  # skip malformed packets

    if channels_set == 0:
        print(f"      Warning: could not set any of the {len(channels)} channels - "
              f"captured on whatever channel the card was left on")

    return detected


# MEASUREMENT AT A SINGLE POINT
def measure_point(interface, channels, num_readings=NUM_READINGS, target_ssid=None):    
# capture multiple readings and average RSSI per AP
    all_readings = []
    
    for reading in range(num_readings):
        with Spinner(f"Reading {reading + 1}/{num_readings}..."):
            detected = capture_at_point(interface, channels)
        all_readings.append(detected)

    print(color(f"      Captured {num_readings} readings", Swag.GREEN))

    # collect readings per BSSID
    rssi_per_bssid = defaultdict(list)
    noise_per_bssid = defaultdict(list)
    info_per_bssid = {}

    for reading in all_readings:
        for bssid, ap_data in reading.items():
            if ap_data["rssi"] is not None:
                rssi_per_bssid[bssid].append(ap_data["rssi"])
            if ap_data["noise"] is not None:
                noise_per_bssid[bssid].append(ap_data["noise"])
            # keep the latest AP metadata
            info_per_bssid[bssid] = ap_data

    # build averaged AP resultss
    detected_aps = []
    for bssid, rssi_list in rssi_per_bssid.items():
        avg_rssi = round(sum(rssi_list) / len(rssi_list))
        avg_noise = None
        snr = None

        if bssid in noise_per_bssid and noise_per_bssid[bssid]:
            # use median to reduce noisy frame-level outliers
            avg_noise = round(median(noise_per_bssid[bssid]))
            snr = avg_rssi - avg_noise

        ap_info = info_per_bssid[bssid]
        detected_aps.append({
            "bssid": bssid,
            "ssid": ap_info.get("ssid", ""),
            "channel": ap_info.get("channel"),
            "rssi": avg_rssi,
            "noise": avg_noise,
            "snr": snr,
        })

    # estimate one noise floor per band;
    # pooling readings in each band gives a steadier estimate
    noise_by_band = defaultdict(list)
    for ap in detected_aps:
        if ap["noise"] is not None:
            band = band_of(ap["channel"])
            if band:
                noise_by_band[band].append(ap["noise"])

    noise_floor = {b: round(median(v)) for b, v in noise_by_band.items() if v}

    # use the strongest AP for point coverage, optionally limited to target SSID
    candidates = detected_aps
    
    if target_ssid:
        candidates = [a for a in detected_aps
                      if (a.get("ssid") or "").strip() == target_ssid]

    best_rssi = None # strongest averaged RSSI among the candidates
    best_snr = None # SNR of that AP, from its band's noise floor
    
    if candidates:
        best_ap = max(candidates, key=lambda a: a["rssi"])
        best_rssi = best_ap["rssi"]

        # use the pooled noise floor from the strongest AP's band;
        # missing 2.4 GHz CCK noise falls back to RSSI-only coverage
        floor = noise_floor.get(band_of(best_ap["channel"]))
        
        if floor is not None:
            best_snr = best_rssi - floor
        else:
            best_snr = best_ap.get("snr")

    return best_rssi, best_snr, detected_aps


def display_ssid(ssid):
    # label hidden SSIDs for display only
    cleaned = (ssid or "").replace("\x00", "").strip()
    return cleaned if cleaned else "<HIDDEN>"


# SURVEY RUNNER
def run_survey(gridmap_path, interface, num_readings=5, band="all", target_ssid=None):
# survey each grid point and write measurements back to the gridmap
    with open(gridmap_path, "r") as f:
        data = json.load(f)

    points = data["points"]
    print(f"\n[*] Loaded {len(points)} measurement points from {gridmap_path}")
    
    if target_ssid:
        print(f"    Coverage target SSID: '{target_ssid}' "
              f"(all APs still recorded for channel decisions)")

    monitor = start_monitor(interface)
    
    if monitor["status"] != 0:
        print("[!] Failed to start monitor mode. Exiting.")
        return

    channels = get_supported_channels(monitor["name"])
    
    if band != "all":
        available = channels
        channels = filter_channels_by_band(channels, band)
        print(f"    Band filter: {band} GHz only "
              f"({len(channels)}/{len(available)} channels)")
        if band == "2.4":
            print("    NOTICE: SNR is not readable on 2.4 GHz with adapters that "
                  "do not report\n            per-frame SNR for CCK beacons "
                  "(e.g. RTL8814AU) - SNR will be N/A.")
        if not channels:
            print(f"[!] Interface supports no {band} GHz channels. Exiting.")
            stop_monitor(monitor)
            return
        
    print(f"    Supported channels: {channels}")
    print(f"\n{'='*55}")
    print(f"  NETWORK SURVEY - {len(points)} points")
    print(f"  Press Enter at each point to start measurement")
    print(f"  Type 'skip' to skip a point")
    print(f"  Type 'quit' to stop early and save progress")
    print(f"{'='*55}\n")

    # measure each point in order
    for i, point in enumerate(points):
        point_id = point.get("id", f"P{i+1}")
        px = point["x"]
        py = point["y"]
        env = point.get("environment", "unknown")
        cond = point.get("condition", "unknown")

        print(f"\n  [{i+1}/{len(points)}] Move to {point_id} "
              f"({px}, {py}) [{env}/{cond}]")

        user_input = input("    Press Enter to measure, 'skip' or 'quit': ").strip().lower()

        if user_input == "quit":
            print("\n  Stopping survey - saving progress...")
            break
        elif user_input == "skip":
            print(f"    Skipped {point_id}")
            continue

        # measure point
        print(f"    Measuring {point_id}...")
        best_rssi, best_snr, detected_aps = measure_point(
            monitor["name"], channels, num_readings=num_readings,
            target_ssid=target_ssid)

        # store point measurements
        point["rssi"] = best_rssi
        point["snr"] = best_snr
        point["detected_aps"] = [
            {
                "bssid": ap["bssid"],
                "ssid": ap["ssid"],
                "channel": ap["channel"],
                "rssi": ap["rssi"],
                "snr": ap.get("snr"),
            }
            for ap in detected_aps
        ]

        # point summary
        num_aps = len(detected_aps)
        rssi_str = f"{best_rssi} dBm" if best_rssi is not None else "N/A"
        snr_str = f"{best_snr} dB" if best_snr is not None else "N/A"
        cov = f"RSSI={rssi_str}  SNR={snr_str}"
        
        if target_ssid and best_rssi is None:
            cov = f"target '{target_ssid}' NOT detected here (coverage gap)"

        print(f"    Result: {cov}  APs detected={num_aps}")

        # show the 3 strongest APs
        sorted_aps = sorted(detected_aps, key=lambda a: a["rssi"], reverse=True)
        
        for ap in sorted_aps[:3]:
            print(f"      {display_ssid(ap['ssid'])[:20]:20s}  ch={ap['channel']:3}  "
                  f"rssi={ap['rssi']} dBm  bssid={ap['bssid']}")

    # save survey results
    with open(gridmap_path, "w") as f:
        json.dump(data, f, indent=2)
    
    print(f"\n[*] Survey results saved to: {gridmap_path}")

    # survey summary
    measured = sum(1 for p in points if p.get("rssi") is not None)
    covered = sum(1 for p in points if p.get("rssi") is not None
                  and p["rssi"] >= -67)
    print(f"\n{'='*55}")
    print(f"  SURVEY COMPLETE")
    print(f"{'='*55}")
    print(f"  Points measured  : {measured}/{len(points)}")
    print(f"  Points covered   : {covered} (RSSI >= -67 dBm)")
    print(f"  Points uncovered : {measured - covered}")
    print(f"{'='*55}")

    # stop monitor mode
    stop_monitor(monitor)


def main():
    parser = argparse.ArgumentParser(
        description="APtlas Network Survey Scanner"
    )
    parser.add_argument(
        "-g", "--gridmap", required=True,
        help="Path to gridmap JSON file"
    )
    parser.add_argument(
        "-i", "--int", dest="interface", required=True,
        help="Wireless interface name (e.g., wlan1)"
    )
    parser.add_argument(
        "-r", "--readings", type=int, default=NUM_READINGS,
        help=f"Number of readings per point (default: {NUM_READINGS})"
    )
    parser.add_argument(
        "-b", "--band", choices=["all", "2.4", "5"], default="all",
        help="Restrict the survey to one band. '5' gives readable SNR on every "
             "point; '2.4' has no SNR on CCK-only adapters (default: all)."
    )
    parser.add_argument(
        "--ssid", default=None,
        help="Measure coverage (point rssi/snr) for this network only. Every AP is "
             "still recorded for channel/interference decisions (default: strongest AP)."
    )
    args = parser.parse_args()

    if not Path(args.gridmap).exists():
        print(f"Error: gridmap file not found: {args.gridmap}")
        sys.exit(1)

    print("\n" + "="*55)
    print("  APtlas - Network Survey Scanner")
    print("="*55)
    print(f"  Gridmap   : {args.gridmap}")
    print(f"  Interface : {args.interface}")
    print(f"  Readings  : {args.readings} per point")
    print(f"  Band      : {args.band}")
    print(f"  Target SSID: {args.ssid or 'none (strongest AP)'}")
    print("="*55)

    run_survey(args.gridmap, args.interface, args.readings, band=args.band, target_ssid=args.ssid)


if __name__ == "__main__":
    main()