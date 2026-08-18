import json
import argparse
import numpy as np
from PIL import Image
from pathlib import Path
import matplotlib.pyplot as plt
import matplotlib.colors as color
from matplotlib.widgets import Button
from scipy.spatial import ConvexHull
from scipy.interpolate import griddata
from matplotlib.cm import ScalarMappable
from matplotlib.colors import BoundaryNorm
from matplotlib.path import Path as MplPath
from mpl_toolkits.axes_grid1 import make_axes_locatable

def resolve_floorplan(floorplan, gridmap_path):
    # resolve floorplan relative to the gridmap if needed
    p = Path(floorplan)

    if p.exists():
        return str(p)

    beside_gridmap = Path(gridmap_path).parent / p.name
    
    if beside_gridmap.exists():
        return str(beside_gridmap)

    # return unresolved path and let the caller fail
    return str(p)


def resolve_ap_channels(points, aps):
    # resolve placed AP channels from nearby survey data
    resolved = []
    for ap in aps:
        out = dict(ap)
        out.setdefault("channel", None)
        out.setdefault("bssid", None)

        # keep explicitly assigned channels
        if out["channel"] is not None:
            resolved.append(out)
            continue

        ssid = (ap.get("ssid") or "").strip()

        if ssid and points:
            def strongest_match(point):
                seen = [d for d in point.get("detected_aps", [])
                        if (d.get("ssid") or "").strip() == ssid
                        and d.get("rssi") is not None]
                return max(seen, key=lambda d: d["rssi"]) if seen else None

            # use the nearest point that detected this SSID
            heard = [p for p in points if strongest_match(p) is not None]
            if heard:
                nearest = min(heard, key=lambda p: (p["x"] - ap["x"])**2
                                                    + (p["y"] - ap["y"])**2)
                best = strongest_match(nearest)
                out["channel"] = best.get("channel")
                out["bssid"] = best.get("bssid")

        resolved.append(out)
    return resolved


def draw_known_aps(ax, aps):
    if not aps:
        return
    ax.scatter([a["x"] for a in aps], [a["y"] for a in aps],
               s=180, marker="^", c="#ff3df5", edgecolors="white",
               linewidths=0.9, zorder=8, label="Known AP")
    for a in aps:
        label = (a.get("ssid") or "").strip() or "AP"
        
        if a.get("channel") is not None:
            label += f"  ch{a['channel']}"

        ax.annotate(label, xy=(a["x"], a["y"]), xytext=(a["x"] + 12, a["y"] + 16),
                    color="#ff8cf6", fontsize=7, fontfamily="monospace", zorder=9,
                    bbox=dict(fc="#1a1a2e", alpha=0.8, ec="#ff3df5", lw=0.7, pad=2))


def measured_rssi(point, ssid=None):
    # use point RSSI, or the strongest matching AP for a target SSID
    if not ssid:
        return point.get("rssi")
    
    target = ssid.strip()
    heard = [d.get("rssi") for d in point.get("detected_aps", [])
             if (d.get("ssid") or "").strip() == target and d.get("rssi") is not None]
    
    return max(heard) if heard else None


def _add_export_button(fig, gridmap_path):
    # export heatmap button
    ax_btn = fig.add_axes([0.01, 0.01, 0.14, 0.045])
    btn = Button(ax_btn, "Export Heatmap", color="#1a1a2e", hovercolor="#333333")
    btn.label.set_color("white")
    btn.label.set_fontsize(8)

    def _on_export(event):
        try:
            out = Path(gridmap_path).with_name(Path(gridmap_path).stem + "_heatmap.png")
            ax_btn.set_visible(False)   # keep the button out of the screenshot
            fig.savefig(out, dpi=150, facecolor=fig.get_facecolor())
            ax_btn.set_visible(True)
            print(f"\n[*] Heatmap exported: {out}")
            btn.label.set_text("Exported ✓")
        except Exception as e:
            print(f"\n[!] Heatmap export failed: {e}")
            btn.label.set_text("Export failed")
        fig.canvas.draw_idle()

    btn.on_clicked(_on_export)
    return btn


def generate(gridmap_path="gridmap.json", floorplan_path=None, show=True,
             return_fig=False, ssid=None):
    with open(gridmap_path, "r") as gridpoints:
        ps = json.load(gridpoints)

    # override stored floorplan if provided
    if floorplan_path:
        ps["floorplan"] = floorplan_path

    ps["floorplan"] = resolve_floorplan(ps["floorplan"], gridmap_path)

    background = Image.open(ps["floorplan"]).convert("RGB")
    bg_array = np.array(background)
    bg_height, bg_width = bg_array.shape[:2]

    # figure setup
    dpi = 100
    max_w, max_h = 16, 10 # max size of figure
    scale = min(max_w / (bg_width/dpi), max_h / (bg_height/dpi))
    fig_w = (bg_width / dpi) * scale
    fig_h = (bg_height / dpi) * scale
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    
    # only measured RSSI values are used for interpolation
    all_x = [p["x"] for p in ps["points"]]
    all_y = [p["y"] for p in ps["points"]]
    x = []
    y = []
    signal_strength = []

    for point in ps["points"]:
        rssi = measured_rssi(point, ssid)
        
        if rssi is None:
            continue

        x.append(point["x"])
        y.append(point["y"])
        signal_strength.append(rssi)

    if len(x) < 4:
        # show the floorplan alone if there are too few measurements
        print(f"  Not enough measured RSSI for a heatmap "
              f"({len(x)} of {len(ps['points'])} points measured, need 4) — "
              f"showing floor plan only")
        ax.set_title(f"APtlas Heatmap — {ssid}" if ssid else "APtlas Heatmap",
                 color="white", pad=5)
        ax.set_facecolor('none')
        fig.patch.set_facecolor('#1a1a2e')
        ax.axis("off")
        ax.imshow(bg_array, origin="upper",
                  extent=[0, bg_width, bg_height, 0],
                  aspect="equal", zorder=1)
        ax.scatter(all_x, all_y, s=20, c='white', edgecolors='black', zorder=3)

        known_aps = resolve_ap_channels(ps["points"], ps.get("aps", []))
        
        if known_aps:
            draw_known_aps(ax, known_aps)
            ax.legend(loc="lower right", fontsize=7, facecolor="#1a1a2e",
                      edgecolor="#333333", labelcolor="white")

        plt.tight_layout()
        
        if return_fig:
            return fig, ax
        if show:
            _export_btn = _add_export_button(fig, gridmap_path)
            plt.show()

        return fig, ax

    # interpolation
    xi = np.linspace(0, bg_width, 1000)
    yi = np.linspace(0, bg_height, 1000)
    xi, yi = np.meshgrid(xi, yi)
    zi = griddata((x, y), signal_strength, (xi,yi), method='cubic')

    # colormap and normalization
    bounds = [-90, -80, -70, -67, -60, -50, -40, -30, -15]
    cmap = color.ListedColormap([
    "#003060",   # dark blue  = weakest
    "#4ecdc4",   # teal
    "#6bcb77",   # green
    "#00FF00",   # bright green
    "#ffd93d",   # yellow
    "#ff9f43",   # orange
    "#ff6b6b",   # red
    "#c0392b",   # dark red   = strongest
    ])
    cmap.set_bad(alpha=0) 
    levels = np.linspace(min(bounds), max(bounds), 100) 
    norm = BoundaryNorm(bounds, ncolors=len(bounds) - 1) 

    # convex hull   
    hull = ConvexHull(list(zip(x, y)))
    hull_path = MplPath(np.array([(x[i], y[i]) for i in hull.vertices])) 
    
    # find interpolation cells inside the measured area
    grid_points = np.column_stack([xi.ravel(), yi.ravel()]) 
    inside = hull_path.contains_points(grid_points).reshape(xi.shape)
    
    # mask interpolation outside the convex hull
    zi_clipped = zi.copy()
    zi_clipped[~inside] = np.nan

    # heatmap
    ax.contourf(xi, yi, zi_clipped,
                norm = norm, levels=levels,
                cmap=cmap, alpha=0.3, zorder=2)
    
    ax.set_title(f"APtlas Heatmap — {ssid}" if ssid else "APtlas Heatmap",
                 color="white", pad=5)
    ax.set_facecolor('none')         
    fig.patch.set_facecolor('#1a1a2e') 
    ax.axis("off")                   
    
    ax.imshow(bg_array, origin="upper",
              extent=[0, bg_width, bg_height, 0],
              aspect="equal", zorder=1)
    
    # colorbar
    divider = make_axes_locatable(ax)
    cax = divider.append_axes("right", size="3%", pad=0.05)
    cax.set_facecolor('#1a1a2e')

    cb = plt.colorbar(ScalarMappable(norm = BoundaryNorm(bounds, ncolors=len(bounds) - 1), cmap=cmap), cax=cax, ticks=bounds)
    cb.set_label("Signal Strength (dBm)", color="white")
    cb.ax.set_yticklabels([str(b) for b in bounds])
    cb.ax.yaxis.set_tick_params(color="white", labelcolor="white")
    cb.outline.set_edgecolor("white")
    cb.update_ticks()

    # final plot
    ax.scatter(all_x, all_y, s=20, c='white', edgecolors='black', zorder=3)

    # user-placed APs
    known_aps = resolve_ap_channels(ps["points"], ps.get("aps", []))
    
    if known_aps:
        draw_known_aps(ax, known_aps)
        ax.legend(loc="lower right", fontsize=7, facecolor="#1a1a2e",
                  edgecolor="#333333", labelcolor="white")

    plt.tight_layout()
    
    if return_fig:
        return fig, ax
    if show:
        _export_btn = _add_export_button(fig, gridmap_path)
        plt.show()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="APtlas Heatmap Generator"
    )
    parser.add_argument(
        "-g", "--gridmap", required=True,
        help="Path to gridmap JSON file"
    )
    parser.add_argument(
        "-s", "--ssid", default=None,
        help="Visualise coverage for this network only, taken from each point's "
             "detected APs (default: the recorded point-level RSSI)."
    )
    args = parser.parse_args()
    generate(gridmap_path=args.gridmap, ssid=args.ssid)