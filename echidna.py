import re
import sys
import math
import json
import gridmap
import heatmap
import argparse
import numpy as np
from PIL import Image
from pathlib import Path
from aptlas import Swag, color
import matplotlib.pyplot as plt
from matplotlib.widgets import Button
from scipy.spatial import ConvexHull
from matplotlib.path import Path as MplPath

# ITU-R P.1238-13 path loss coefficients
COEFFICIENTS = {
    ("corridor", "LoS"):  {"alpha": 1.57, "beta": 29.46, "gamma": 2.24},
    ("corridor", "NLoS"): {"alpha": 2.78, "beta": 28.62, "gamma": 2.54},
    ("office",   "LoS"):  {"alpha": 1.47, "beta": 34.17, "gamma": 2.08},
    ("office",   "NLoS"): {"alpha": 2.39, "beta": 30.13, "gamma": 2.40},
}

RSSI_THRESHOLD = -67    # dBm - minimum acceptable RSSI
SNR_THRESHOLD  = 25     # dB - placeholder if unavailable

# candidate AP settings for propagation model predictions
TX_POWER_DBM   = 20     # dBm - transmit power of candidate APs
ANTENNA_GAIN   = 2      # dBi - antenna gain (EIRP = TX_POWER + ANTENNA_GAIN)
FREQUENCY_GHZ  = 2.4    # GHz - default operating frequency, overriden by --band flag
COVERAGE_RADIUS_M = 15.0  # m - max radius one suggested AP is credited with covering,
                          # so a single AP cannot claim points spread across the whole
                          # floor via the optimistic path-loss reach (design §23)
# non-overlapping channels
CHANNELS_24 = [1, 6, 11]
CHANNELS_5 = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140]

def get_channels(band="2.4"):
    if band == "5":
        return CHANNELS_5
    return CHANNELS_24

def get_frequency(band):
    return 5.0 if band == "5" else 2.4

def load_data(json_path):
    with open(json_path, "r") as f:
        data = json.load(f)

    points = data["points"]
    print(f"Loaded {len(points)} measurement points from {json_path}")

    missing = sum(1 for p in points if p.get("rssi") is None)
    if missing > 0:
        print(f"  Warning: {missing} points have no RSSI value (None) - "
              f"these will be treated as uncovered.")

    return data, points

def path_loss(distance_m, environment, condition, frequency_ghz=FREQUENCY_GHZ):
    if distance_m < 0.1:
        # avoid log(0)
        distance_m = 0.1

    key = (environment.lower(), condition)
    if key not in COEFFICIENTS:
        # fall back to corridor LoS
        print(f"  Warning: unknown environment/condition '{key}' - using corridor LoS")
        key = ("corridor", "LoS")

    c = COEFFICIENTS[key]
    lb = (10 * c["alpha"] * math.log10(distance_m) +
          c["beta"] +
          10 * c["gamma"] * math.log10(frequency_ghz))
    return lb

def predicted_rssi(ap_x, ap_y, point,
                   tx_power=TX_POWER_DBM,
                   antenna_gain=ANTENNA_GAIN,
                   frequency_ghz=FREQUENCY_GHZ,
                   scale_m_per_px=0.05):
    # convert pixel distance to meters
    dx_px = point["x"] - ap_x
    dy_px = point["y"] - ap_y
    distance_px = math.sqrt(dx_px**2 + dy_px**2)
    distance_m = distance_px * scale_m_per_px

    lb = path_loss(distance_m,
                   point.get("environment", "corridor"),
                   point.get("condition", "LoS"),
                   frequency_ghz)
    
    eirp = tx_power + antenna_gain

    return eirp - lb

def check_snr(point):
    snr = point.get("snr")
    
    if snr is None:
        return True  # assume acceptable SNR when unavailable
    
    return snr >= SNR_THRESHOLD

def is_covered(point, rssi_at_point=None):
    # use predicted RSSI if provided, otherwise measured RSSI
    rssi = rssi_at_point if rssi_at_point is not None else point.get("rssi")

    # missing RSSI is treated as uncovered
    if rssi is None:
        return False

    rssi_ok = rssi >= RSSI_THRESHOLD
    snr_ok  = check_snr(point)

    return rssi_ok and snr_ok

def predicted_coverage(pred_rssi):
    # candidate coverage uses predicted RSSI only
    return pred_rssi >= RSSI_THRESHOLD

def extract_existing_aps(points):
    existing = []
    
    for p in points:
        detected = p.get("detected_aps", [])
        for ap in detected:
            # avoid duplicate BSSIDs
            if not any(e.get("bssid") == ap.get("bssid") for e in existing):
                existing.append({
                    "bssid": ap.get("bssid"),
                    "channel": ap.get("channel"),
                    "x": p["x"],  # approximate AP position
                    "y": p["y"],
                    "rssi": ap.get("rssi"),
                })
    
    print(f"  Existing APs detected    : {len(existing)}")
    return existing

def assign_channel(placed_aps, existing_aps, candidate_x, candidate_y,
                   scale_m_per_px, channels, coverage_radius_m=15.0):

    coverage_radius_px = coverage_radius_m / scale_m_per_px
    channel_usage = {ch: 0 for ch in channels}

    # include both suggested and existing APs
    all_aps = placed_aps + existing_aps

    if not all_aps:
        return channels[0]

    for ap in all_aps:
        dx = ap["x"] - candidate_x
        dy = ap["y"] - candidate_y
        distance_px = math.sqrt(dx**2 + dy**2)

        if distance_px < coverage_radius_px * 2:
            ch = ap.get("channel")
            if ch in channel_usage:
                channel_usage[ch] += 1

    return min(channel_usage, key=channel_usage.get)

def existing_infrastructure(points, known_aps):
    # prefer user-placed APs, then add scan-derived neighbors
    scan_aps = extract_existing_aps(points)
    known_bssids = {a.get("bssid") for a in known_aps if a.get("bssid")}
    return known_aps + [s for s in scan_aps if s.get("bssid") not in known_bssids]


def baseline_suggestion(points, scale_m_per_px, existing_aps, channels, ap_offset=0):
    # suggest one starting AP at the measurement centroid
    cx = round(sum(p["x"] for p in points) / len(points))
    cy = round(sum(p["y"] for p in points) / len(points))
    channel = assign_channel([], existing_aps, cx, cy, scale_m_per_px, channels)

    # number the suggestion after any APs already placed (ap_offset), so labels stay
    # continuous across survey iterations instead of resetting to AP-1 every run
    n = ap_offset + 1

    print("\n" + "="*55)
    print("  BASELINE - single starting AP at the centre of the measured area")
    print("="*55)
    print(f"  Measurement points       : {len(points)}")
    print(f"  Existing/neighbor APs   : {len(existing_aps)}")
    print(f"  Suggested AP-{n}           : ({cx}, {cy}) px  "
          f"({cx*scale_m_per_px:.1f}m, {cy*scale_m_per_px:.1f}m)  channel {channel}")
    print("="*55)
    
    return [{"id": f"AP-{n}", "x": cx, "y": cy, "channel": channel, "covers": 0}]


def run_algorithm(points, scale_m_per_px, grid_spacing_px,
                  img_width, img_height, band="2.4", known_aps=None,
                   baseline=False, coverage_radius_m=COVERAGE_RADIUS_M, ap_offset=0):

    # select channel plan and propagation frequency
    channels  = get_channels(band)
    frequency = get_frequency(band)
    known_aps = known_aps or []

    if baseline:
        existing_aps = existing_infrastructure(points, known_aps)
        return baseline_suggestion(points, scale_m_per_px, existing_aps, channels)

    print("\n" + "="*55)
    print("  GREEDY SET COVER ALGORITHM")
    print("="*55)

    # find initially uncovered points
    uncovered = []
    covered_by_existing = 0

    for p in points:
        if not is_covered(p):
            uncovered.append(p)
        else:
            covered_by_existing += 1

    print(f"  Total measurement points : {len(points)}")
    print(f"  Already covered          : {covered_by_existing}")
    print(f"  Uncovered (need APs)     : {len(uncovered)}")

    if not uncovered:
        print("\n  All points already meet coverage criteria.")
        print("  No additional APs required.")
        return []
    
    # load existing APs for channel assignment
    existing_aps = existing_infrastructure(points, known_aps)
    
    if known_aps:
        print(f"  User-placed APs          : {len(known_aps)}")

    # generate candidate AP positions
    candidate_xs = np.arange(0, img_width,  grid_spacing_px)
    candidate_ys = np.arange(0, img_height, grid_spacing_px)

    # limit candidate positions to within the measured area
    pt_x = [p["x"] for p in points]
    pt_y = [p["y"] for p in points]
    hull = ConvexHull(list(zip(pt_x, pt_y)))
    hull_path = MplPath(np.array([(pt_x[i], pt_y[i]) for i in hull.vertices]))

    # allow candidates slightly outside the hull
    buffer = grid_spacing_px * 1.5

    # filter candidates to the measured area
    filtered_candidates = []
    
    for cx in candidate_xs:
        for cy in candidate_ys:
            if hull_path.contains_point((cx, cy)):
                filtered_candidates.append((cx, cy))
            else:
                for i in hull.vertices:
                    px = pt_x[i]
                    py = pt_y[i]
                    if math.sqrt((cx - px)**2 + (cy - py)**2) < buffer:
                        filtered_candidates.append((cx, cy))
                        break

    total_candidates = len(filtered_candidates)
    print(f"  Candidate positions      : {total_candidates} (filtered from grid)")

    # greedy placement loop
    placed_aps = []
    iteration  = 0
    coverage_radius_px = coverage_radius_m / scale_m_per_px

    while uncovered:
        iteration += 1
        best_position  = None
        best_coverage  = []
        best_count     = 0
        best_margin    = None # weakest covered RSSI

        # evaluate every candidate position within measured area
        for cx, cy in filtered_candidates:

            # find uncovered points within range and above the RSSI threshold
            newly_covered = []
            weakest = None          # weakest predicted RSSI among covered points
            
            for p in uncovered:
                if math.hypot(cx - p["x"], cy - p["y"]) > coverage_radius_px:
                    continue
                pred = predicted_rssi(cx, cy, p,
                                          frequency_ghz=frequency,
                                          scale_m_per_px=scale_m_per_px)
                if predicted_coverage(pred):
                    newly_covered.append(p)
                    weakest = pred if weakest is None else min(weakest, pred)

            # prefer most coverage, then strongest worst-case RSSI
            count = len(newly_covered)
            if count > best_count or (count == best_count and count > 0
                                      and weakest > best_margin):
                best_count    = count
                best_position = (cx, cy)
                best_coverage = newly_covered
                best_margin   = weakest

        # stop if no candidate can cover the remaining points
        if best_position is None or best_count == 0:
            print(f"  Placement {iteration}: No candidate reaches the "
                  f"{RSSI_THRESHOLD} dBm RSSI threshold for the remaining "
                  f"{len(uncovered)} point(s) - stopping.")
            print("  These points are out of predicted range of every grid position "
                  "(too far / too much path loss).")
            for p in uncovered:
                print(f"    Uncovered: {p['id']} at ({p['x']}, {p['y']})")
            break

        channel = assign_channel(placed_aps, existing_aps, best_position[0], best_position[1], scale_m_per_px, channels)
        
        # record the placed AP (numbered after any already-placed APs, see ap_offset)
        ap = {
            "id":      f"AP-{ap_offset + iteration}",
            "x":       best_position[0],
            "y":       best_position[1],
            "channel": channel,
            "covers":  len(best_coverage),
        }
        placed_aps.append(ap)

        covered_ids = {id(p) for p in best_coverage}
        uncovered = [p for p in uncovered if id(p) not in covered_ids]

        print(f"  Placement {iteration}: Placed {ap['id']} at "
              f"({best_position[0]:.0f}, {best_position[1]:.0f}) px  "
              f"| Channel {channel}  "
              f"| Covers {best_count} points  "
              f"| Remaining uncovered: {len(uncovered)}")
        
    # print summary 
    print()
    print("="*55)
    print(f"  RESULT: {len(placed_aps)} AP(s) suggested")
    print("="*55)
    for ap in placed_aps:
        x_m = ap["x"] * scale_m_per_px
        y_m = ap["y"] * scale_m_per_px
        print(f"  {ap['id']:6s}  position: ({x_m:.1f}m, {y_m:.1f}m)  "
              f"channel: {ap['channel']}  covers: {ap['covers']} points")
    print("="*55)

    return placed_aps


def export_suggestion(gridmap_path, placed_aps, ssid=None):
    # place one AP per survey round
    with open(gridmap_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    aps = data.get("aps", [])
    
    if placed_aps:
        n = len(aps) + 1
        ap = placed_aps[0]
        aps.append({
            "id":      f"Iteration{n}",
            "x":       int(round(ap["x"])),
            "y":       int(round(ap["y"])),
            "ssid":    ssid or f"Iteration{n}",
            "channel": ap["channel"],
        })
    data["aps"] = aps
    total = len(aps)

    # keep APs above the measurement points in exported JSON
    front = {k: v for k, v in data.items() if k not in ("iteration", "aps", "points")}
    ordered = {**front, "aps": aps}
    
    if "points" in data:
        ordered["points"] = data["points"]
    data = ordered

    # replace any existing iteration suffix
    src = Path(gridmap_path)
    base = re.sub(r"_iteration\d+$", "", src.stem)
    out_json = src.with_name(f"{base}_iteration{total}.json")
    
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    # export placement reference image
    floorplan = heatmap.resolve_floorplan(data.get("floorplan"), gridmap_path)
    img_array = np.array(Image.open(floorplan).convert("RGB"))
    out_png = src.with_name(f"{base}_iteration{total}_reference.png")
    gridmap.export_ref_img(data["points"], img_array, str(out_png), aps=aps)

    return str(out_json), str(out_png)


def visualise(points, placed_aps, data, scale_m_per_px, gridmap_path, floorplan_path,
              ssid=None):
    # base heatmap includes user-placed APs
    fig, ax = heatmap.generate(gridmap_path=gridmap_path, floorplan_path=floorplan_path, show=False, return_fig=True)
    all_x = [p["x"] for p in points]
    all_y = [p["y"] for p in points]
    ax.scatter(all_x, all_y, s=30, c='#13FF00', edgecolors='black', zorder=5, label="Measurement points")

    uncovered_x = [p["x"] for p in points if not is_covered(p)]
    uncovered_y = [p["y"] for p in points if not is_covered(p)]
    
    if uncovered_x:
        ax.scatter(uncovered_x, uncovered_y, s=100, c='red', marker="X",
                   edgecolors='white', linewidths=0.8, zorder=6,
                   label=f"Below threshold ({RSSI_THRESHOLD} dBm)")

    # suggested AP positions (yellow stars)
    for ap in placed_aps:
        ax.scatter(ap["x"], ap["y"], s=300, c='yellow', marker="*",
                    edgecolors="black", linewidths=0.8, zorder=4)
        
        ax.annotate(f"{ap['id']}\nCh {ap['channel']}",
                    xy=(ap["x"], ap["y"]), xytext=(ap["x"] + 34, ap["y"] - 40),
                    color="yellow", fontsize=7, fontfamily="monospace", zorder=12,
                    bbox=dict(fc="#1a1a2e", alpha=0.85, ec="yellow", lw=0.7, pad=2),
                    arrowprops=dict(arrowstyle="->", color="yellow", lw=0.8))

    ax.legend(loc="lower right", fontsize=7, facecolor="#1a1a2e", edgecolor="#333333", labelcolor="white")

    ax.set_title(f"APtlas - Algorithm Results  |  "
                 f"{len(placed_aps)} AP(s) suggested  |  "
                 f"Threshold: {RSSI_THRESHOLD} dBm", color="#ff7b00", fontsize=9, fontfamily="monospace", pad=6)

    plt.tight_layout()

    # export suggestion button
    export_btn = None
    if placed_aps:
        ax_btn = fig.add_axes([0.01, 0.01, 0.16, 0.045])
        export_btn = Button(ax_btn, "Export Suggestion",
                            color="#1a1a2e", hovercolor="#333333")
        export_btn.label.set_color("yellow")
        export_btn.label.set_fontsize(8)

        def _on_export(event):
            try:
                out_json, out_png = export_suggestion(gridmap_path, placed_aps, ssid=ssid)
                
                # save the full result view without the export button
                optmap = Path(out_json).with_name(Path(out_json).stem + "_optmap.png")
                ax_btn.set_visible(False)
                fig.savefig(optmap, dpi=150, facecolor=fig.get_facecolor())
                ax_btn.set_visible(True)
                print(color(f"\n[*] Suggestion exported:", Swag.GREEN))
                print(f"    JSON   : {out_json}\n    Ref img: {out_png}\n    Optmap : {optmap}")
                export_btn.label.set_text("Exported ✓")
                export_btn.label.set_color("#13FF00")
            except Exception as e:
                print(color(f"\n[!] Export failed: {e}", Swag.RED))
                export_btn.label.set_text("Export failed")
            fig.canvas.draw_idle()

        export_btn.on_clicked(_on_export)

    plt.show()


def main():
    parser = argparse.ArgumentParser(
        description="WiFi AP Placement Optimization Algorithm"
        )
    parser.add_argument(
            "-g", "--gridmap", required=True,
            help="Path to gridmap JSON file produced by gridmap.py"
        )
    parser.add_argument(
            "-f", "--floorplan", required=True,
            help="Path to floor plan image"
        )
    parser.add_argument("-b", "--band", default="2.4", choices=["2.4", "5"],
            help="Frequency band (default: 2.4)"
        )
    parser.add_argument("--baseline", action="store_true",
            help="Baseline mode: ignore coverage and suggest one starting AP at the "
                 "centre of the measured area (for a survey taken before the target "
                 "network is deployed)."
        )
    parser.add_argument("--coverage-radius", type=float, default=COVERAGE_RADIUS_M,
            help=f"Max radius (m) one suggested AP is credited with covering, so a "
                 f"single AP is not placed centrally to 'cover' far-apart gaps "
                 f"(default: {COVERAGE_RADIUS_M})."
        )
    parser.add_argument("--ssid", default=None,
            help="SSID to tag an exported AP suggestion with (the 'Export Suggestion' "
                 "button), so a later survey can resolve its channel. Default: an "
                 "'Iteration<N>' label."
        )
    args = parser.parse_args()

    if not Path(args.gridmap).exists():
        print(f"Error: gridmap file not found: {args.gridmap}")
        sys.exit(1)

    print("\n" + "="*55)
    print("  APtlas - Optimization Algorithm")
    print("="*55)

    # load data
    data, points = load_data(args.gridmap)
    data["floorplan"] = heatmap.resolve_floorplan(args.floorplan, args.gridmap)
    scale_m_per_px  = data.get("scale_m_per_px", 0.05)
    grid_spacing_m  = data.get("grid_spacing_m", 1.0)
    grid_spacing_px = grid_spacing_m / scale_m_per_px

    # load floor plan dimensions
    bg = Image.open(data["floorplan"]).convert("RGB")
    bg_array = np.array(bg)
    img_height, img_width = bg_array.shape[:2]

    print(f"  Scale        : {scale_m_per_px} m/px")
    print(f"  Grid spacing : {grid_spacing_px:.0f} px ({grid_spacing_m}m)")
    print(f"  Floor plan   : {img_width}x{img_height} px  "
          f"({img_width*scale_m_per_px:.1f}x{img_height*scale_m_per_px:.1f}m)")
    print(f"  RSSI threshold: {RSSI_THRESHOLD} dBm")
    print(f"  SNR threshold : {SNR_THRESHOLD} dB (placeholder)")
    print(f"  TX power      : {TX_POWER_DBM} dBm")
    print(f"  Band          : {args.band} GHz  (channels {get_channels(args.band)})")
    print(f"  Frequency     : {get_frequency(args.band)} GHz")
    print(f"  Coverage radius: {args.coverage_radius} m per AP")
    print(f"  Mode          : {'baseline (1 AP at centre)' if args.baseline else 'coverage optimisation'}")

    # resolve user-placed AP channels from the survey (SSID -> nearest point -> BSSID)
    known_aps = heatmap.resolve_ap_channels(points, data.get("aps", []))

    # run algo
    placed_aps = run_algorithm(points, scale_m_per_px, grid_spacing_px,
                               img_width, img_height, band=args.band,
                               known_aps=known_aps, baseline=args.baseline,
                               coverage_radius_m=args.coverage_radius,
                               ap_offset=len(data.get("aps", [])))
    # visualise results
    print("\nGenerating visualisation...")
    visualise(points, placed_aps, data, scale_m_per_px, args.gridmap, data["floorplan"],
              ssid=args.ssid)


if __name__ == "__main__":
    main()