#!/usr/bin/python3

import os
import shutil
import subprocess

import matplotlib.pyplot as plt
from astropy.io import fits

plt.rcParams["font.family"] = "Times New Roman"


class PipelineStageError(RuntimeError):
    """Report which processing stage failed for an observation."""

    def __init__(self, stage: str, detail: str):
        super().__init__(detail)
        self.stage = stage
        self.detail = detail


def is_numeric_folder(folder_name: str) -> bool:
    """Return True when a folder name has an ObsID-like numeric form."""
    return folder_name.isdigit()


def find_obsid_directories(base_dir: str):
    """Find numeric observation directories and sort them numerically."""
    directories = [
        name
        for name in os.listdir(base_dir)
        if os.path.isdir(os.path.join(base_dir, name)) and is_numeric_folder(name)
    ]
    return sorted(directories, key=int)


def run_command(command, stage: str, cwd: str):
    """Run one external task and raise a stage-specific error on failure."""
    print(f"[RUN] {' '.join(command)}\n   -> in folder: {cwd}")
    try:
        subprocess.run(command, cwd=cwd, check=True)
    except FileNotFoundError as exc:
        raise PipelineStageError(stage, f"command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise PipelineStageError(
            stage, f"command returned exit status {exc.returncode}"
        ) from exc


def run_nicerl2(obsid_dir: str):
    """Apply NICER Level-2 calibration and screening."""
    run_command(
        ["nicerl2", "indir=./", "clobber=yes"],
        stage="nicerl2",
        cwd=obsid_dir,
    )


def prepare_l2_products(obsid_dir: str):
    """Copy the MKF, cleaned event, and unfiltered event into l2_products."""
    l2_dir = os.path.join(obsid_dir, "l2_products")
    os.makedirs(l2_dir, exist_ok=True)

    mkf_files = []
    auxil_dir = os.path.join(obsid_dir, "auxil")
    if os.path.isdir(auxil_dir):
        for filename in sorted(os.listdir(auxil_dir)):
            if filename.endswith(".mkf"):
                destination = os.path.join(l2_dir, filename)
                shutil.copy2(os.path.join(auxil_dir, filename), destination)
                mkf_files.append(destination)

    clean_files = []
    event_cl_dir = os.path.join(obsid_dir, "xti", "event_cl")
    if os.path.isdir(event_cl_dir):
        for filename in sorted(os.listdir(event_cl_dir)):
            if filename.endswith("0mpu7_cl.evt") or filename.endswith("0mpu7_ufa.evt"):
                destination = os.path.join(l2_dir, filename)
                shutil.copy2(os.path.join(event_cl_dir, filename), destination)
                if filename.endswith("_cl.evt"):
                    clean_files.append(destination)

    if not mkf_files:
        raise PipelineStageError("prepare products", "no MKF file was found")
    if not clean_files:
        raise PipelineStageError(
            "prepare products", "no cleaned 0mpu7_cl.evt file was found"
        )

    return l2_dir, clean_files[0], mkf_files[0]


def run_detector_selection(l2_dir: str, event_path: str, mkf_path: str):
    """Exclude detectors 14 and 34 from the copied cleaned event in place."""
    event_name = os.path.basename(event_path)
    mkf_name = os.path.basename(mkf_path)
    run_command(
        [
            "nifpmsel",
            event_name,
            event_name,
            "detlist=launch,-14,-34",
            f"mkfile={mkf_name}",
            "clobber=yes",
        ],
        stage="nifpmsel",
        cwd=l2_dir,
    )


def run_nicerl3(obsid_dir: str):
    """Generate spectral products followed by a one-second light curve."""
    run_command(
        [
            "nicerl3-spect",
            "indir=l2_products/",
            "grouptype=optmin",
            "bkgformat=file",
            "bkgmodeltype=scorpeon",
            "clobber=yes",
            "syserrfile=NONE",
        ],
        stage="nicerl3-spect",
        cwd=obsid_dir,
    )
    run_command(
        [
            "nicerl3-lc",
            "indir=l2_products/",
            "timebin=1",
            "pirange=30:1200",
            "bkgmodeltype=scorpeon",
            "detnormtype=FPM",
            "clobber=yes",
        ],
        stage="nicerl3-lc",
        cwd=obsid_dir,
    )


def plot_mkf_data(file_path, axis):
    """Plot FPM_OVERONLY_COUNT against mission time."""
    with fits.open(file_path) as hdul:
        data = hdul[1].data
        axis.plot(data["TIME"], data["FPM_OVERONLY_COUNT"], label="FPM_OVERONLY_COUNT")


def plot_lc_data(file_path, axis):
    """Plot light-curve rate with its supplied uncertainty."""
    with fits.open(file_path) as hdul:
        data = hdul[1].data
        axis.errorbar(data["TIME"], data["RATE"], yerr=data["ERROR"], fmt=".", label="RATE")


def generate_plots(l2_dir: str):
    """Create a quick-look comparison of the MKF overshoot rate and source light curve."""
    mkf_files = sorted(
        os.path.join(l2_dir, name) for name in os.listdir(l2_dir) if name.endswith(".mkf")
    )
    light_curves = sorted(
        os.path.join(l2_dir, name)
        for name in os.listdir(l2_dir)
        if name.endswith("_sr.lc")
    )
    if not mkf_files:
        raise PipelineStageError("quick-look plot", "no MKF file is available")
    if not light_curves:
        raise PipelineStageError("quick-look plot", "no _sr.lc product is available")

    fig, axes = plt.subplots(2, 1, figsize=(10, 8))
    try:
        for file_path in mkf_files:
            plot_mkf_data(file_path, axes[0])
        axes[0].set_title("MKF–source comparison", fontsize=16)
        axes[0].set_ylabel("FPM_OVERONLY_COUNT", fontsize=14)
        axes[0].legend()

        for file_path in light_curves:
            plot_lc_data(file_path, axes[1])
        axes[1].set_xlabel("Time", fontsize=14)
        axes[1].set_ylabel("X-ray intensity (counts s$^{-1}$)", fontsize=14)
        axes[1].legend()

        fig.tight_layout()
        output_path = os.path.join(l2_dir, "mkf_source_comparison.png")
        fig.savefig(output_path, dpi=300)
    except (KeyError, OSError, ValueError) as exc:
        raise PipelineStageError("quick-look plot", str(exc)) from exc
    finally:
        plt.close(fig)
    print(f"[SAVED] Plot -> {output_path}")


def process_observation(obsid: str, working_dir: str):
    """Process one observation, allowing the caller to isolate failures."""
    obsid_path = os.path.join(working_dir, obsid)
    print(f"\n=== Processing ObsID: {obsid} ===")
    run_nicerl2(obsid_path)
    l2_dir, event_path, mkf_path = prepare_l2_products(obsid_path)
    run_detector_selection(l2_dir, event_path, mkf_path)
    run_nicerl3(obsid_path)
    generate_plots(l2_dir)


def main():
    working_dir = os.getcwd()
    obsids = find_obsid_directories(working_dir)
    if not obsids:
        print("No numeric observation directories were found.")
        return 1

    completed = []
    failed = []
    for obsid in obsids:
        try:
            process_observation(obsid, working_dir)
        except PipelineStageError as exc:
            failed.append((obsid, exc.stage, exc.detail))
            print(f"[FAILED] ObsID {obsid} | stage: {exc.stage} | {exc.detail}")
            print("[CONTINUE] Moving to the next observation.")
            continue
        except (OSError, KeyError, ValueError) as exc:
            failed.append((obsid, "unexpected data/file error", str(exc)))
            print(f"[FAILED] ObsID {obsid} | unexpected data/file error | {exc}")
            print("[CONTINUE] Moving to the next observation.")
            continue

        completed.append(obsid)
        print(f"[SUCCESS] ObsID {obsid}")

    print("\n=== NICER Mission Pipeline summary ===")
    print(f"Successful observations: {len(completed)}")
    print(f"Failed observations: {len(failed)}")
    if completed:
        print("Completed ObsIDs:", " ".join(completed))
    if failed:
        print("Failures:")
        for obsid, stage, detail in failed:
            print(f"  {obsid} | {stage} | {detail}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
