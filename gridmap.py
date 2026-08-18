import sys
import json
import argparse
import numpy as np
from pathlib import Path
import matplotlib.pyplot as plt
from PIL import Image, ImageDraw, ImageFont
import matplotlib.gridspec as gridspec
from matplotlib.widgets import Button, RadioButtons, TextBox

def grid_snap(x_px, y_px, scale_m_per_px, spacing_m=5.0):
     # snap pixel coordinates to the grid
    spacing_px = spacing_m / scale_m_per_px
    snapped_x = round(x_px / spacing_px) * spacing_px
    snapped_y = round(y_px / spacing_px) * spacing_px
    return snapped_x, snapped_y
    
def export_json(points, scale, image_path, output_path="gridmap.json", aps=None,
                grid_spacing_m=1.0):
    data = {
        "scale_m_per_px": scale,
        "grid_spacing_m": grid_spacing_m,
        "floorplan": str(image_path),
    }
    # keep user-placed APs above the points; channels are resolved later
    if aps:
        data["aps"] = [
            {"id": f"AP{i+1}", "x": int(a["x"]), "y": int(a["y"]),
             "ssid": a.get("ssid", "")}
            for i, a in enumerate(aps)
        ]
    data["points"] = [
        {"id": f"P{i+1}", "x": int(p["x"]), "y": int(p["y"]),
         "environment": p.get("environment", "corridor"),
         "condition": p.get("condition", "LoS")}
        for i, p in enumerate(points)
    ]
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"Grid map saved to: {output_path}")
    
    return data

def label_font(size=18):
    try:
        from matplotlib import font_manager
        return ImageFont.truetype(font_manager.findfont("DejaVu Sans"), size)
    except Exception:
        return ImageFont.load_default()

def export_ref_img(points, image_array, output_path="gridmap_reference.png", aps=None):
    # export floorplan with measurement points as a survey reference
    img = Image.fromarray(image_array)
    draw = ImageDraw.Draw(img)
    font = label_font()

    for i, p in enumerate(points):
        px, py = int(p["x"]), int(p["y"])

        # measurement point
        r = 8
        draw.ellipse([px-r, py-r, px+r, py+r],
                     fill=(0, 212, 255),
                     outline=(255, 255, 255),
                     width=2)

        # outlined point label for readability
        draw.text((px + 12, py - 12), f"P{i+1}",
                  font=font,
                  fill=(26, 26, 46),
                  stroke_width=3,
                  stroke_fill=(255, 255, 255))

    # user-placed APs
    for i, a in enumerate(aps or []):
        px, py = int(a["x"]), int(a["y"])
        r = 10
        draw.polygon([(px, py - r), (px - r, py + r), (px + r, py + r)],
                     fill=(255, 61, 245), outline=(255, 255, 255))
        tag = a.get("ssid") or f"AP{i+1}"
        draw.text((px + 13, py - 6), tag,
                  font=font, fill=(26, 26, 46),
                  stroke_width=3, stroke_fill=(255, 255, 255))

    img.save(output_path)
    print(f"Reference image saved: {output_path}")

class GridMapBuilder:
    def __init__(self, image_path, scale, grid_spacing_m=5.0):
        self.image_path = image_path
        self.scale = scale                      # metres per pixel
        self.grid_spacing_m = grid_spacing_m    # grid spacing in metres
        self.grid_spacing_px = grid_spacing_m / scale

        img = Image.open(image_path).convert("RGB")
        self.image_array = np.array(img)
        self.img_height, self.img_width = self.image_array.shape[:2]
        self.points = []            # measurement points
        self.aps = []               # user-placed APs
        self.place_mode = "point"   # point or AP placement
        self.current_ssid = ""      # current AP SSID
        self.current_environment = "corridor"
        self.current_condition = "LoS"
        self._build_window()

    def _build_window(self):
        dpi = 100
        max_w, max_h = 16, 10  # maximum figure size in inches
        scale = min(max_w / (self.img_width / dpi), max_h / (self.img_height / dpi))
        fig_w = (self.img_width / dpi) * scale
        fig_h = (self.img_height / dpi) * scale
        self.fig = plt.figure(figsize=(fig_w, fig_h), facecolor="#1a1a2e")
        
        self.fig.suptitle(
            "Grid Map Builder  -  Left-click places (see Mode)  |  Right-click removes last  |  "
            "APs: type the SSID box, then click",
            color="#ff7b00", fontsize=11, fontfamily="monospace"
        )

        gs = gridspec.GridSpec(
            1, 1, figure=self.fig,
            left=0.03, right=0.97,
            top=0.91, bottom=0.08
        )

        self.ax = self.fig.add_subplot(gs[0])
        self.ax.set_facecolor("#1a1a2e")

        # mode toggle
        ax_mode = self.fig.add_axes([0.16, 0.01, 0.17, 0.05])
        self.btn_mode = Button(ax_mode,
                               "Mode: Points",
                               color="#0d1a2b",
                               hovercolor="#1a2d3d")
        self.btn_mode.label.set_color("#00d4ff")
        self.btn_mode.label.set_fontsize(9)
        self.btn_mode.label.set_fontfamily("monospace")
        self.btn_mode.on_clicked(self._toggle_mode)

        # export button
        ax_export = self.fig.add_axes([0.35, 0.01, 0.15, 0.05])
        self.btn_export = Button(ax_export,
                                 "✓  Export Gridmap",
                                 color="#0d1a2b",
                                 hovercolor="#1a2d3d")
        
        self.btn_export.label.set_color("#00d4ff")
        self.btn_export.label.set_fontsize(9)
        self.btn_export.label.set_fontfamily("monospace")
        self.btn_export.on_clicked(self._export)

        # undo button
        ax_undo = self.fig.add_axes([0.52, 0.01, 0.15, 0.05])
        self.btn_undo = Button(ax_undo,
                               "↩  Undo Last Point",
                               color="#1a0d0d",
                               hovercolor="#2d1a1a")
        
        self.btn_undo.label.set_color("#ff3d3d")
        self.btn_undo.label.set_fontsize(9)
        self.btn_undo.label.set_fontfamily("monospace")
        self.btn_undo.on_clicked(self._undo)

        # AP SSID input
        ax_ssid = self.fig.add_axes([0.78, 0.01, 0.14, 0.05], facecolor="#0d1117")
        self.txt_ssid = TextBox(ax_ssid, "AP SSID ", initial="", color="#0d1117",
                                hovercolor="#161d2b")
        self.txt_ssid.label.set_color("#ff8cf6")
        self.txt_ssid.label.set_fontsize(9)
        self.txt_ssid.label.set_fontfamily("monospace")
        self.txt_ssid.text_disp.set_color("white")
        self.txt_ssid.text_disp.set_fontsize(9)
        self.txt_ssid.text_disp.set_fontfamily("monospace")
        # keep the current SSID in sync
        self.txt_ssid.on_text_change(lambda text: setattr(self, "current_ssid", text)) 

        # environment selector
        self.fig.text(0.02, 0.28,
                      "Environment:",
                      color="#aaaaaa",
                      fontsize=8,
                      fontfamily="monospace")
        
        ax_env = self.fig.add_axes([0.02, 0.15, 0.08, 0.12],
                                   facecolor="#0d1117")
        
        self.radio_env = RadioButtons(ax_env, ("corridor", "office"),
                                      active=0, activecolor="#00d4ff")
        
        for t in self.radio_env.labels:
            t.set_color("white")
            t.set_fontsize(8)
            t.set_fontfamily("monospace")
        self.radio_env.on_clicked(self._set_environment)

        # condition selector
        self.fig.text(0.02, 0.13, "Condition:", color="#aaaaaa",
                      fontsize=8, fontfamily="monospace")
        
        ax_cond = self.fig.add_axes([0.02, 0.04, 0.08, 0.08],
                                     facecolor="#0d1117")
        
        self.radio_cond = RadioButtons(ax_cond, ("LoS", "NLoS"),
                                        active=0, activecolor="#ffd93d")
        for t in self.radio_cond.labels:
            t.set_color("white")
            t.set_fontsize(8)
            t.set_fontfamily("monospace")
        self.radio_cond.on_clicked(self._set_condition)

        self._redraw()

        # mouse click handler
        self.fig.canvas.mpl_connect("button_press_event", self._on_click)

        plt.show()

    def _redraw(self): 
        # redraw floorplan, grid, points and APs
        self.ax.cla()
        self.ax.set_facecolor("#1a1a2e")
        self.ax.imshow(self.image_array, origin="upper", extent=[0, self.img_width, self.img_height, 0], aspect="equal", zorder=1)
        self.ax.set_xlim(0, self.img_width)
        self.ax.set_ylim(self.img_height, 0)

        # grid lines
        x_lines = np.arange(0, self.img_width, self.grid_spacing_px)
        y_lines = np.arange(0, self.img_height, self.grid_spacing_px)

        for x in x_lines:
            self.ax.axvline(x, color="#ffffff", alpha=0.08,
                            linewidth=0.5, zorder=2)
        for y in y_lines:
            self.ax.axhline(y, color="#ffffff", alpha=0.08,
                            linewidth=0.5, zorder=2)

        # measurement points
        for i, p in enumerate(self.points):
            self.ax.scatter(p["x"], p["y"], s=100, c="#00d4ff",
                             marker="o", edgecolors="white",
                             linewidths=1.5, zorder=5)

            self.ax.text(p["x"] + 6, p["y"] - 6, f"P{i+1}",
                         color="white", fontsize=8, fontweight="bold",
                         fontfamily="monospace", zorder=6,
                         bbox=dict(fc="#1a1a2e", alpha=0.7,
                                   ec="none", pad=1))

        # user-placed APs (magenta triangles)
        for i, a in enumerate(self.aps):
            self.ax.scatter(a["x"], a["y"], s=140, c="#ff3df5",
                            marker="^", edgecolors="white",
                            linewidths=1.5, zorder=5)
            tag = a["ssid"] or f"AP{i+1}"
            self.ax.text(a["x"] + 8, a["y"] + 8, tag,
                         color="#ff8cf6", fontsize=8, fontweight="bold",
                         fontfamily="monospace", zorder=6,
                         bbox=dict(fc="#1a1a2e", alpha=0.7, ec="none", pad=1))

        self.ax.set_xlim(0, self.img_width)
        self.ax.set_ylim(self.img_height, 0)
        self.ax.axis("off")

        # figure title
        self.ax.set_title(
            f"Mode: {'APs' if self.place_mode == 'ap' else 'Points'}  |  "
            f"Points: {len(self.points)}  |  APs: {len(self.aps)}  |  "
            f"Next point: {self.current_environment}/{self.current_condition}  |  "
            f"Grid: {self.grid_spacing_m}m  |  Scale: {self.scale} m/px",
            color="#888888", fontsize=8, pad=4
        )

        self.fig.canvas.draw_idle()

    def _on_click(self, event):
        # handle clicks inside the floorplan
        if event.inaxes != self.ax:
            return
        
        if event.xdata is None or event.ydata is None:
            return

        if event.button == 1:
            if self.place_mode == "ap":
                self._place_ap(event.xdata, event.ydata)
            else:
                self._place_point(event.xdata, event.ydata)

        elif event.button == 3: # right click undo
            self._undo()

    def _place_point(self, x, y):
        # snap to nearest grid intersection
        snapped_x, snapped_y = grid_snap(x, y, self.scale, self.grid_spacing_m)

        # reject duplicate points
        for p in self.points:
            if abs(p["x"] - snapped_x) < 2 and abs(p["y"] - snapped_y) < 2:
                print(f"  Point already exists at ({snapped_x:.0f}, {snapped_y:.0f})")
                return

        # store the current environment and condition
        self.points.append({"x": snapped_x, "y": snapped_y,
                            "environment": self.current_environment,
                            "condition": self.current_condition})
        print(f"  P{len(self.points)} placed at ({snapped_x:.0f}, {snapped_y:.0f}) px  "
              f"[{self.current_environment}/{self.current_condition}]  "
              f"→  ({snapped_x * self.scale:.1f}, {snapped_y * self.scale:.1f}) m")
        self._redraw()

    def _place_ap(self, x, y):
        # APs are placed freely without grid snapping
        ssid = (self.current_ssid or "").strip()

        self.aps.append({"x": int(x), "y": int(y), "ssid": ssid})
        shown = ssid or f"AP{len(self.aps)}"
        note = "" if ssid else "  (no SSID - type one in the AP SSID box to enable channel resolution)"
        print(f"  AP{len(self.aps)} '{shown}' placed at ({x:.0f}, {y:.0f}) px{note}")
        self._redraw()

    def _undo(self, event=None):
        # remove the last item in the active mode
        if self.place_mode == "ap":
            if self.aps:
                removed = self.aps.pop()
                print(f"  Removed AP{len(self.aps) + 1} "
                      f"('{removed['ssid'] or 'unnamed'}')")
                self._redraw()
            else:
                print("  No APs to remove.")
        else:
            if self.points:
                removed = self.points.pop()
                print(f"  Removed P{len(self.points) + 1} "
                      f"at ({removed['x']:.0f}, {removed['y']:.0f})")
                self._redraw()
            else:
                print("  No points to remove.")

    def _toggle_mode(self, event=None):
        self.place_mode = "ap" if self.place_mode == "point" else "point"
        self.btn_mode.label.set_text(
            "Mode: APs" if self.place_mode == "ap" else "Mode: Points")
        
        if self.place_mode == "ap":
            print("\n  Placement mode: APs - type the network name in the AP SSID box "
                  "FIRST, then click to place.")
        else:
            print("\n  Placement mode: measurement points")

        self._redraw()

    def _set_environment(self, value):
        self.current_environment = value
        self._redraw()

    def _set_condition(self, value):
        self.current_condition = value
        self._redraw()

    def _export(self, event=None):
        if len(self.points) < 4:
            print(f"  Place at least 4 points before exporting "
                  f"(currently {len(self.points)}).")
            return

        # export next to the source floorplan
        src = Path(self.image_path)
        json_path = str(src.with_name(f"{src.stem}_gridmap.json"))
        img_path  = str(src.with_name(f"{src.stem}_reference.png"))

        export_json(self.points, self.scale, self.image_path, json_path,
                    aps=self.aps, grid_spacing_m=self.grid_spacing_m)
        export_ref_img(self.points, self.image_array, img_path, aps=self.aps)

        print(f"\n  Export complete:")
        print(f"  JSON:  {json_path}  ({len(self.points)} points, {len(self.aps)} APs)")
        print(f"  Image: {img_path}")

def main():
    parser = argparse.ArgumentParser(
        description="Grid Map Builder - Place measurement points on a floor plan"
    )
    parser.add_argument(
        "-f", "--floorplan", required=True,
        help="Path to floor plan image (PNG or JPG)"
    )
    parser.add_argument(
        "-s", "--scale", type=float, default=0.05,
        help="Metres per pixel (default: 0.05)"
    )
    parser.add_argument(
        "--gridspacing", type=float, default=2.0,
        help="Grid spacing in metres (default: 2.0)"
    )
    args = parser.parse_args()

    if not Path(args.floorplan).exists():
        print(f"Error: image not found: {args.floorplan}")
        sys.exit(1)

    print("\n" + "="*50)
    print("  Grid Map Builder")
    print("="*50)
    print(f"  Image  : {args.floorplan}")
    print(f"  Scale  : {args.scale} m/px")
    print(f"  Grid   : {args.gridspacing}m spacing")
    print("="*50 + "\n")

    GridMapBuilder(
        image_path=args.floorplan,
        scale=args.scale,
        grid_spacing_m=args.gridspacing
    )

if __name__ == "__main__":
    main()
