import sys
import json
import argparse
import numpy as np
from PIL import Image
from pathlib import Path

# ANSI COLOR STYLING
class Swag:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RED     = "\033[91m"
    GREEN   = "\033[92m"
    BLUE    = "\033[94m"
    YELLOW  = "\033[93m"
    MAGENTA = "\033[95m"
    CYAN    = "\033[96m"
    GREY    = "\033[37m"


def color(text, *codes):
    return "".join(codes) + str(text) + Swag.RESET

import gridmap
import scanner
import heatmap
import echidna

BANNER = r"""
                    @@@@@  @@@@@@@@@@@@@@@
                  @@@@@@@@ @@@@@@@@@@@@@@@@@
                 @@@@@@@@@ @@@@@     @@@@@@@@
                @@@@@@@@@@ @@@@@         @@@@@
              @@@@@@ @@@@@ @@@@@          @@@@
             @@@@@@  @@@@@ @@@@@          @@@@
            @@@@@@   @@@@@ @@@@@         @@@@@
           @@@@@     @@@@@ @@@@@     @@@@@@@@@
         @@@@@@      @@@@@ @@@@@ @@@@@@@@@@@
        @@@@@@       @@@@@ @@@@@ @@@@@@@@@
       @@@@@@        @@@@@ @@@@@
     @@@@@@          @@@@@ @@@@@ @@@@@@@@@@@ @@@             @@@@@       @@@@@@@@@@@
    @@@@@@           @@@@@ @@@@@    @@@@     @@@            @@@@@@@     @@@@
   @@@@@@            @@@@@ @@@@@    @@@@     @@@          @@@@@ @@@@    @@@@@@@@@@@@@
  @@@@@@             @@@@@ @@@@@    @@@@     @@@         @@@@    @@@@@            @@@@
@@@@@@@              @@@@@ @@@@@    @@@@     @@@@@@@@@@ @@@@@@@@@@@@@@@ @@@@@@@@@@@@@
"""


def main(args):

    print(color(BANNER, Swag.BLUE))

    while True:
        match menu(args):

            case 1:
                run_gridmap(args)

            case 2:
                run_survey(args)

            case 3:
                run_heatmap(args)

            case 4:
                run_echidna(args)

            case 5:
                print(" ", end="")
                break


def menu(args):
    val = 0

    if not args.gridmap:
        gridmap_status = "none set (build one, or restart with -g)"
    elif has_rssi(args.gridmap):
        gridmap_status = f"{args.gridmap} (surveyed: yes)"
    else:
        gridmap_status = f"{args.gridmap} (surveyed: no)"

    while True:
        print(f"\n\n=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=--=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=\n\n"
              f"Gridmap : {gridmap_status}\n\n"
              f"1. Build Gridmap - GUI Required\n"
              f"2. Run Network Survey (scanner)\n"
              f"3. Generate Heatmap - GUI Required\n"
              f"4. Run AP placement Optimizer - GUI Required\n"
              f"5. Exit")

        try:
            val = int(input("\nChoice: "))

            if val not in range(1, 6):
                raise ValueError
            else:
                break
        except ValueError:
            print("\nEnter a number between 1-5")

    return val


def has_rssi(gridmap_path):  # check for existing measurement data (rssi)
    try:
        with open(gridmap_path, "r") as f:
            data = json.load(f)
        return any(p.get("rssi") is not None for p in data.get("points", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return False


def need_gridmap(args):  # validate gridmap param
    if not args.gridmap:
        print("\nNo gridmap set. Build one (option 1), then restart with "
              "-g <gridmap.json>.")
        return False

    if not Path(args.gridmap).exists():
        print(f"\nGridmap file not found: {args.gridmap}")
        return False

    return True


def run_gridmap(args):
    print("\nNOTICE: The grid map builder opens an interactive window and needs "
          "a graphical display.\n        It cannot run in a headless/CLI-only "
          "environment (e.g. over SSH without X forwarding).")

    if not args.floorplan:
        print("\nNo floor plan set. Restart with -f <image> to build a gridmap.")
        return

    if not Path(args.floorplan).exists():
        print(f"\nFloor plan not found: {args.floorplan}")
        return

    try:
        gridmap.GridMapBuilder(
            image_path=args.floorplan,
            scale=args.scale,
            grid_spacing_m=args.gridspacing
        )
        print("\nTip: export the gridmap from the builder, then restart aptlas "
              "with -g pointing at the new JSON.")

    except Exception as fail:
        print(f"\nGridmap builder error:\n{str(fail)}")


def choose_band(default):
    print(f"\nSurvey band  [Enter = {default}]"
          f"\n  1. all  - both bands"
          f"\n  2. 2.4  - 2.4 GHz only (no SNR on CCK-only adapters)"
          f"\n  3. 5    - 5 GHz only (readable SNR on every point)")

    while True:
        choice = input("Choice: ").strip()

        match choice:
            case "":
                return default
            case "1" | "all":
                return "all"
            case "2" | "2.4":
                return "2.4"
            case "3" | "5":
                return "5"
 
        print("Enter 1-3, a band name, or press Enter for the default.")


def choose_baseline():  # toggle between normal and pre-deployment (baseline) mode
    print("\nBaseline mode?  [Enter = no, normal coverage optimisation]"
          "\n  y = suggest ONE starting AP at the centre (target network not deployed yet)")
    return input("Baseline (y/N): ").strip().lower() in ("y", "yes")


def choose_ssid():  # optionally filter for specific SSID (network), unless already provided
    print("\nFocus on a single network's SSID?  [Enter = no filter]"
          "\n  Type a network name to restrict to it; other networks are still"
          "\n  recorded either way.")
    ssid = input("SSID: ").strip()
    return ssid or None


def run_survey(args):  # invoke scanner.py to process measurement data
    if not need_gridmap(args):
        return

    if not args.interface:
        print("\nNo interface set. Restart with -i <interface> to run a survey.")
        return

    band = choose_band(args.band)
    target_ssid = args.ssid if args.ssid is not None else choose_ssid() 

    try:
        scanner.run_survey(args.gridmap, args.interface, args.readings,
                           band=band, target_ssid=target_ssid)

    except PermissionError:
        print("\nMonitor mode requires root. Try: sudo python aptlas.py ...")

    except Exception as fail:
        print(f"\nSurvey error:\n{str(fail)}")


def run_heatmap(args):  # invoke heatmap.py with the surveyed gridmap
    if not need_gridmap(args):
        return

    if not has_rssi(args.gridmap):
        print("\nNo survey data in gridmap yet. Run a survey (option 2) first.")
        return

    target_ssid = args.ssid if args.ssid is not None else choose_ssid()

    try:
        heatmap.generate(gridmap_path=args.gridmap, floorplan_path=args.floorplan,
                         ssid=target_ssid)

    except Exception as fail:
        print(f"\nHeatmap error:\n{str(fail)}")


def run_echidna(args):  # run greedy set cover and visualize the results
    if not need_gridmap(args):
        return
    
    baseline = args.baseline or choose_baseline() # prompt for baseline if not provided

    # baseline mode needs no measured coverage, it suggests a centre AP
    # normal optimisation requires survey data
    if not baseline and not has_rssi(args.gridmap):
        print("\nNo survey data in gridmap yet. Run a survey (option 2) first, "
              "or choose baseline mode for a pre-deployment suggestion.")
        return

    try:
        # load measurement data and resolve the floorplan
        data, points = echidna.load_data(args.gridmap)
        floorplan = heatmap.resolve_floorplan(
            args.floorplan or data.get("floorplan"), args.gridmap
        )
        data["floorplan"] = floorplan

        scale_m_per_px  = data.get("scale_m_per_px", 0.05)
        grid_spacing_m  = data.get("grid_spacing_m", 1.0)
        grid_spacing_px = grid_spacing_m / scale_m_per_px

        # floorplan dimensions
        bg = Image.open(floorplan).convert("RGB")
        img_height, img_width = np.array(bg).shape[:2]

        # resolve user-placed AP channels from the survey, and passes it
        known_aps = heatmap.resolve_ap_channels(points, data.get("aps", []))
        placed_aps = echidna.run_algorithm(points, scale_m_per_px,
                                           grid_spacing_px, img_width, img_height,
                                           band=args.band, known_aps=known_aps,
                                           coverage_radius_m=args.coverage_radius,
                                           ap_offset=len(data.get("aps", [])))
        
        print("\nGenerating visualisation...")
        echidna.visualise(points, placed_aps, data, scale_m_per_px,
                          args.gridmap, floorplan, ssid=args.ssid)

    except Exception as fail:
        print(f"\nOptimization error:\n{str(fail)}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="APtlas - survey, heatmap, and AP placement pipeline"
    )
    parser.add_argument(
        "-g", "--gridmap", default=None,
        help="Path to gridmap JSON. Required for options 2-4."
    )
    parser.add_argument(
        "-i", "--interface", default=None,
        help="Wireless interface to use for surveying. Required for option 2."
    )
    parser.add_argument(
        "-f", "--floorplan", default=None,
        help="Floor plan image. Overrides the path stored in the gridmap JSON."
    )
    parser.add_argument(
        "-r", "--readings", type=int, default=scanner.NUM_READINGS,
        help=f"Readings per survey point (default: {scanner.NUM_READINGS})."
    )
    parser.add_argument(
        "-b", "--band", choices=["all", "2.4", "5"], default="all",
        help="Default survey band; can be changed from the survey menu (default: all)."
    )
    parser.add_argument(
        "--ssid", default=None,
        help="SSID to measure coverage for. If omitted, the survey menu prompts for one."
    )
    parser.add_argument(
        "--baseline", action="store_true",
        help="Use baseline optimisation by default. Can be changed from the optimisation menu."
    )
    parser.add_argument(
        "--coverage-radius", type=float, default=echidna.COVERAGE_RADIUS_M,
        help=f"Maximum AP coverage radius in metres (default: {echidna.COVERAGE_RADIUS_M})."
    )
    parser.add_argument(
        "-s", "--scale", type=float, default=0.05,
        help="Floor plan scale in metres per pixel (default: 0.05)."
    )
    parser.add_argument(
        "--gridspacing", type=float, default=2.0,
        help="Grid spacing in metres (default: 2.0)."
    )
    
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    # A gridmap isn't required up front, you can start APtlas just to build one.
    if args.gridmap and not Path(args.gridmap).exists():
        print(f"Error: gridmap file not found: {args.gridmap}")
        sys.exit(1)

    main(args)
