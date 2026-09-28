"""
price_mismatch_report.py
=========================

A separate, standalone check on top of the same Charge Sheet + RIMS files
main.py uses. It does NOT replace main.py's outputs - it runs the same file
discovery and enrichment logic (by importing main.py directly, so it never
drifts out of sync with it) and produces 2 focused reports containing ONLY
the rows where `GST Price` and `Rate Row GST Price` disagree.

Why this exists: GST Price and Rate Row GST Price are two different fields
in the Charge Sheet - GST Price looks like a general/list price, Rate Row
GST Price is tied to the specific rate row that was picked (highest
Rate Row Id among non-expired rows, same logic as main.py). They frequently
match, but not always - this script finds every case where they don't, so
it can be reviewed without having to scan the full pipeline output by hand.

Usage:
    python price_mismatch_report.py

Reads from the same input/ folder as main.py (any file names/formats -
same adaptive detection). Writes 2 files to output/:

    output/Charge_Sheet_Price_Mismatches.csv
        Every SKU in the Charge Sheet (regardless of whether it changed in
        RIMS today) whose picked rate row has GST Price != Rate Row GST
        Price.

    output/RIMS_Changes_Price_Mismatches.csv
        Of the SKUs that were actually flagged as changed in the RIMS files
        (RRP change, EndDate change, or Other change) AND matched in the
        Charge Sheet, only the ones where GST Price != Rate Row GST Price -
        i.e. today's flagged changes that also carry a price discrepancy
        worth a closer look.

See price_mismatch_report.log for a detailed run log.
"""

import logging
import sys
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
import main as pipeline  # noqa: E402  - reused so this script can't drift out of sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "price_mismatch_report.log", mode="w"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


def find_mismatches(df: pd.DataFrame) -> pd.DataFrame:
    """Rows where both price columns are present and numerically different.
    Rows missing either value (e.g. 'Not Present' SKUs, or a Charge Sheet
    without a Rate Row GST Price column) are excluded rather than treated
    as a mismatch."""
    if "Rate Row GST Price" not in df.columns:
        return df.iloc[0:0]
    gst = pd.to_numeric(df["GST Price"], errors="coerce")
    rate_row_gst = pd.to_numeric(df["Rate Row GST Price"], errors="coerce")
    mismatch = gst.notna() & rate_row_gst.notna() & (gst != rate_row_gst)
    out = df[mismatch].copy()
    out["Price Difference"] = gst[mismatch] - rate_row_gst[mismatch]
    return out


def main():
    logger.info("=" * 60)
    logger.info("Starting GST Price vs Rate Row GST Price mismatch report")
    logger.info("=" * 60)

    charge_df_raw, charge_path, rims_paths = pipeline.classify_input_files(pipeline.INPUT_DIR)
    logger.info(f"Charge Sheet: {charge_path.name}, {len(rims_paths)} RIMS file(s)")

    charge_lookup = pipeline.load_charge_sheet(charge_df_raw)

    # ------------------------------------------------------------
    # Report 1 - every SKU in the Charge Sheet with a price mismatch,
    # whether or not it changed in RIMS today.
    # ------------------------------------------------------------
    charge_mismatches = find_mismatches(charge_lookup)
    charge_mismatches = charge_mismatches.rename(columns={"SKU": "Product SKU"})
    logger.info(
        f"Charge Sheet: {len(charge_lookup)} SKUs total, "
        f"{len(charge_mismatches)} with GST Price != Rate Row GST Price"
    )

    # ------------------------------------------------------------
    # Report 2 - of today's flagged RIMS changes (RRP / EndDate / Other)
    # that matched the Charge Sheet, only the ones with a price mismatch.
    # ------------------------------------------------------------
    rims_all = pipeline.load_rims_files(rims_paths)
    rrp_df, end_df, new_df, other_df = pipeline.filter_rims(rims_all)

    rrp_out = pipeline.build_rrp_output(rrp_df, charge_lookup)
    end_out = pipeline.build_end_date_output(end_df, charge_lookup)
    other_out = pipeline.build_other_changes_output(other_df, charge_lookup)

    rrp_out.insert(0, "Change Type", "RRP Change") if not rrp_out.empty else None
    end_out.insert(0, "Change Type", "End Date Change") if not end_out.empty else None
    other_out.insert(0, "Change Type", "Other Change") if not other_out.empty else None

    combined_changes = pd.concat([rrp_out, end_out, other_out], ignore_index=True, sort=False)
    # Only Found rows can possibly have both price columns populated.
    combined_changes = combined_changes[combined_changes["Charge Sheet Status"] == "Found"]
    rims_mismatches = find_mismatches(combined_changes)

    logger.info(
        f"RIMS changes today: {len(combined_changes)} matched the Charge Sheet, "
        f"{len(rims_mismatches)} with GST Price != Rate Row GST Price"
    )

    # ------------------------------------------------------------
    # Write outputs
    # ------------------------------------------------------------
    pipeline.OUTPUT_DIR.mkdir(exist_ok=True)
    charge_path_out = pipeline.OUTPUT_DIR / "Charge_Sheet_Price_Mismatches.csv"
    rims_path_out = pipeline.OUTPUT_DIR / "RIMS_Changes_Price_Mismatches.csv"

    charge_cols = ["Product SKU", "Product Name", "Rate Identifier", "GST Price", "Rate Row GST Price", "Price Difference"]
    charge_cols = [c for c in charge_cols if c in charge_mismatches.columns]
    if charge_mismatches.empty:
        pd.DataFrame(columns=charge_cols).to_csv(charge_path_out, index=False)
    else:
        charge_mismatches[charge_cols].to_csv(charge_path_out, index=False)

    rims_cols = [
        "Change Type", "Product SKU", "Product Model", "Product Name", "Rate Identifier",
        "GST Price", "Rate Row GST Price", "Price Difference", "Charge Sheet Status",
    ]
    rims_cols = [c for c in rims_cols if c in rims_mismatches.columns]
    if rims_mismatches.empty:
        pd.DataFrame(columns=rims_cols).to_csv(rims_path_out, index=False)
    else:
        rims_mismatches[rims_cols].to_csv(rims_path_out, index=False)

    summary = (
        "\n" + "=" * 40 +
        "\nPrice Mismatch Report Complete" +
        "\n" + "=" * 40 +
        f"\nCharge Sheet SKUs checked      : {len(charge_lookup)}"
        f"\nCharge Sheet price mismatches  : {len(charge_mismatches)}"
        f"\nRIMS changes checked (Found)   : {len(combined_changes)}"
        f"\nRIMS changes price mismatches  : {len(rims_mismatches)}"
        f"\n\nOutputs written to             : {pipeline.OUTPUT_DIR}"
        f"\n  - {charge_path_out.name}"
        f"\n  - {rims_path_out.name}"
        + "\n" + "=" * 40
    )
    logger.info(summary)
    print(summary)


if __name__ == "__main__":
    main()