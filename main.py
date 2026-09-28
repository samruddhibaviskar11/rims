"""RIMS automation: classify inputs and export four change reports."""

import logging
import re
import sys
from pathlib import Path

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"

INPUT_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

ENABLE_LLM_SUGGESTIONS = False
SUPPORTED_EXTENSIONS = (".xlsx", ".xls", ".csv")

RIMS_REQUIRED_COLS = {
    "Product SKU",
    "Product Model",
    "Upload Action",
    "Comments",
}

CHARGE_REQUIRED_COLS = {
    "SKU",
    "Product Name",
    "Rate Identifier",
    "End Date",
    "Rate Row Id",
}

CHARGE_OUTPUT_COLS = [
    "SKU",
    "Product Name",
    "Charge Sheet Model",
    "Rate Identifier",
    "End Date",
    "Price",
    "GST Price",
    "Rate Row Price",
    "Rate Row GST Price",
]

PRICE_COLS = [
    "Price",
    "GST Price",
    "Rate Row Price",
    "Rate Row GST Price",
]


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "automation.log", mode="w"),
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger(__name__)


def _sniff_format(path: Path) -> str:
    """Detect the file's real format, regardless of extension."""
    with path.open("rb") as fh:
        header = fh.read(8)

    if header.startswith(b"PK"):
        return "xlsx"

    if header.startswith(b"\xd0\xcf\x11\xe0"):
        return "xls"

    with path.open("r", encoding="utf-8", errors="ignore") as fh:
        preview = fh.read(1000).lstrip().lower()

    if preview.startswith(("<html", "<!doctype")) or "<table" in preview:
        return "html"

    return "text"


def read_any_file(path: Path, header: int = 0) -> pd.DataFrame:
    fmt = _sniff_format(path)

    if fmt == "xlsx":
        return pd.read_excel(
            path,
            header=header,
            engine="openpyxl",
        )

    if fmt == "xls":
        return pd.read_excel(
            path,
            header=header,
            engine="xlrd",
        )

    if fmt == "html":
        return pd.read_html(
            path,
            header=header,
        )[0]

    return pd.read_csv(
        path,
        header=header,
        sep=None,
        engine="python",
    )


def read_rims_file(path: Path) -> pd.DataFrame:
    return read_any_file(path, header=0)


def classify_input_files(input_dir: Path):
    """Identify Charge Sheet and RIMS files using their headers."""
    if not input_dir.exists():
        raise FileNotFoundError(
            f"Input directory not found: {input_dir}"
        )

    candidates = sorted(
        path
        for path in input_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )

    if not candidates:
        raise FileNotFoundError(
            f"No .xlsx, .xls, or .csv files in {input_dir}"
        )

    charge_df = None
    charge_path = None
    rims_paths = []

    for path in candidates:
        try:
            first = read_any_file(path, header=0)
        except Exception as exc:
            logger.warning(
                "Skipping unreadable file %s: %s",
                path.name,
                exc,
            )
            continue

        if RIMS_REQUIRED_COLS.issubset(first.columns):
            rims_paths.append(path)
            continue

        for header_row in (1, 0):
            try:
                candidate = (
                    first
                    if header_row == 0
                    else read_any_file(path, header=1)
                )
            except Exception:
                continue

            if CHARGE_REQUIRED_COLS.issubset(candidate.columns):
                if charge_df is None:
                    charge_df = candidate
                    charge_path = path

                    logger.info(
                        "Detected Charge Sheet: %s",
                        path.name,
                    )
                else:
                    logger.warning(
                        "Ignoring additional Charge Sheet: %s",
                        path.name,
                    )
                break
        else:
            logger.warning(
                "Skipping unclassified file: %s",
                path.name,
            )

    if charge_df is None:
        raise FileNotFoundError(
            "No Charge Sheet found. Check its column headers."
        )

    if not rims_paths:
        raise FileNotFoundError(
            "No RIMS daily files found. Check their column headers."
        )

    return charge_df, charge_path, rims_paths


def load_charge_sheet(charge_df: pd.DataFrame) -> pd.DataFrame:
    """
    Pick one Charge Sheet row per SKU.

    Highest numeric Rate Identifier wins.
    Rate Row Id breaks ties.
    """
    missing = CHARGE_REQUIRED_COLS - set(charge_df.columns)

    if missing:
        raise ValueError(
            f"Charge Sheet missing columns: {sorted(missing)}"
        )

    df = charge_df.copy()

    df["SKU"] = df["SKU"].astype("string").str.strip()

    df = df[
        df["SKU"].notna()
        & df["SKU"].ne("")
    ].copy()

    df["Rate Identifier"] = pd.to_numeric(
        df["Rate Identifier"],
        errors="coerce",
    )

    df["Rate Row Id"] = pd.to_numeric(
        df["Rate Row Id"],
        errors="coerce",
    )

    df["End Date"] = pd.to_datetime(
        df["End Date"],
        errors="coerce",
    )

    if df["Rate Identifier"].isna().any():
        logger.warning(
            "Some Rate Identifier values are invalid; "
            "those rows sort last."
        )

    # The Charge Sheet's "Model" becomes "Charge Sheet Model"
    # to distinguish it from the RIMS "Product Model".
    if "Model" in df.columns:
        df["Charge Sheet Model"] = df["Model"]
    else:
        df["Charge Sheet Model"] = pd.NA
        logger.warning(
            "Charge Sheet has no 'Model' column; "
            "model differences cannot be checked."
        )

    for column in CHARGE_OUTPUT_COLS:
        if column not in df.columns:
            df[column] = pd.NA

            logger.warning(
                "Charge Sheet column %r missing; "
                "leaving it blank in outputs.",
                column,
            )

    # Every returned value comes from this same selected row.
    picked = (
        df.sort_values(
            ["Rate Identifier", "Rate Row Id"],
            ascending=[False, False],
            na_position="last",
            kind="stable",
        )
        .drop_duplicates(
            "SKU",
            keep="first",
        )
    )

    logger.info(
        "Charge Sheet: %d rows, %d unique SKUs selected",
        len(df),
        len(picked),
    )

    return picked[
        CHARGE_OUTPUT_COLS
    ].reset_index(drop=True)


def load_rims_files(rims_paths: list) -> pd.DataFrame:
    frames = []

    for path in rims_paths:
        df = read_rims_file(path)

        missing = RIMS_REQUIRED_COLS - set(df.columns)

        if missing:
            raise ValueError(
                f"{path.name} missing columns: {sorted(missing)}"
            )

        df = df.copy()

        df["Product SKU"] = (
            df["Product SKU"]
            .astype("string")
            .str.strip()
        )

        df["__source_file"] = path.name

        frames.append(df)

    combined = pd.concat(
        frames,
        ignore_index=True,
    )

    if "Rims File_Date" in combined.columns:
        combined["Rims File_Date"] = pd.to_datetime(
            combined["Rims File_Date"],
            errors="coerce",
        )

        combined = combined.sort_values(
            "Rims File_Date",
            kind="stable",
        )
    else:
        combined = combined.sort_values(
            "__source_file",
            kind="stable",
        )

    return combined


def latest_snapshot(
    rims_all: pd.DataFrame,
) -> pd.DataFrame:
    return rims_all.drop_duplicates(
        "Product SKU",
        keep="last",
    )


def _only_technology_type(comment: str) -> bool:
    """True only if TechnologyType is the sole changed field."""
    text = str(comment).strip()

    text = re.sub(
        r"^changed\s*:\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()

    fields = [
        part.strip().strip(". \t")
        for part in re.split(r"[,;|\n]+", text)
    ]

    fields = [
        field
        for field in fields
        if field
    ]

    return (
        len(fields) == 1
        and re.fullmatch(
            r"Technology\s*Type",
            fields[0],
            re.IGNORECASE,
        ) is not None
    )


def filter_rims(rims_all: pd.DataFrame):
    comments = (
        rims_all["Comments"]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    action = (
        rims_all["Upload Action"]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    mask_rrp = comments.eq("Changed: RRP")
    mask_end = comments.eq("Changed: EndDate")

    mask_new = (
        comments.eq("")
        & action.eq("New")
    )

    # Exclude TechnologyType alone.
    # Retain TechnologyType + DeviceSpecCharValue,
    # or TechnologyType + any other changed field.
    mask_other = (
        comments.ne("")
        & ~comments.str.startswith("Nothing has changed")
        & ~mask_rrp
        & ~mask_end
        & ~comments.map(_only_technology_type)
    )

    def most_recent(mask):
        return (
            rims_all[mask]
            .drop_duplicates(
                "Product SKU",
                keep="last",
            )
            .copy()
        )

    return (
        most_recent(mask_rrp),
        most_recent(mask_end),
        most_recent(mask_new),
        most_recent(mask_other),
    )


def enrich_with_charge_sheet(
    df: pd.DataFrame,
    charge_lookup: pd.DataFrame,
) -> pd.DataFrame:
    merged = df.merge(
        charge_lookup,
        how="left",
        left_on="Product SKU",
        right_on="SKU",
        validate="many_to_one",
        suffixes=("", "_charge"),
        indicator=True,
    )

    merged["Charge Sheet Status"] = (
        merged["_merge"]
        .map(
            {
                "both": "Found",
                "left_only": "Not Present",
                "right_only": "Not Present",
            }
        )
        .astype(str)
    )

    return merged


def _model_difference(row: pd.Series) -> str:
    """
    Compare model strings after removing outer spaces and
    ignoring capitalization.
    """
    if row["Charge Sheet Status"] != "Found":
        return "Not Present"

    rims_model = row.get("Product Model")
    charge_model = row.get("Charge Sheet Model")

    if pd.isna(rims_model) or pd.isna(charge_model):
        return "Cannot Compare"

    rims_text = str(rims_model).strip().casefold()
    charge_text = str(charge_model).strip().casefold()

    if rims_text == charge_text:
        return "No Difference"

    return "Difference Present"


def _charge_output(
    df: pd.DataFrame,
    charge_lookup: pd.DataFrame,
    *,
    include_comments: bool = False,
    include_end_date: bool = False,
    include_model_difference: bool = False,
) -> pd.DataFrame:
    cols = [
        "Product SKU",
        "Product Model",
    ]

    if include_comments:
        cols.append("Comments")

    cols += [
        "Product Name",
    ]

    if include_model_difference:
        cols += [
            "Charge Sheet Model",
            "Model Difference",
        ]

    cols += [
        "Rate Identifier",
    ]

    if include_end_date:
        cols.append("End Date")

    cols += PRICE_COLS

    cols.append("Charge Sheet Status")

    if df.empty:
        return pd.DataFrame(columns=cols)

    merged = enrich_with_charge_sheet(
        df,
        charge_lookup,
    )

    if include_model_difference:
        merged["Model Difference"] = merged.apply(
            _model_difference,
            axis=1,
        )

    return merged[cols]


def build_rrp_output(
    rrp_df: pd.DataFrame,
    charge_lookup: pd.DataFrame,
) -> pd.DataFrame:
    return _charge_output(
        rrp_df,
        charge_lookup,
        include_end_date=True,
    )


def build_end_date_output(
    end_df: pd.DataFrame,
    charge_lookup: pd.DataFrame,
) -> pd.DataFrame:
    return _charge_output(
        end_df,
        charge_lookup,
        include_end_date=True,
    )


def build_new_items_output(
    new_df: pd.DataFrame,
) -> pd.DataFrame:
    cols = [
        "Product SKU",
        "Product Model",
    ]

    if new_df.empty:
        return pd.DataFrame(columns=cols)

    return new_df[cols].copy()


def build_other_changes_output(
    other_df: pd.DataFrame,
    charge_lookup: pd.DataFrame,
) -> pd.DataFrame:
    return _charge_output(
        other_df,
        charge_lookup,
        include_comments=True,
        include_model_difference=True,
    )


def main():
    charge_raw, charge_path, rims_paths = (
        classify_input_files(INPUT_DIR)
    )

    charge_lookup = load_charge_sheet(
        charge_raw
    )

    rims_all = load_rims_files(
        rims_paths
    )

    rrp_df, end_df, new_df, other_df = (
        filter_rims(rims_all)
    )

    outputs = {
        "RRP_Changes.csv": build_rrp_output(
            rrp_df,
            charge_lookup,
        ),
        "End_Date_Changes.csv": build_end_date_output(
            end_df,
            charge_lookup,
        ),
        "New_Items.csv": build_new_items_output(
            new_df,
        ),
        "Other_Changes.csv": build_other_changes_output(
            other_df,
            charge_lookup,
        ),
    }

    if ENABLE_LLM_SUGGESTIONS:
        from llm_match import add_llm_suggestions

        charge_full = charge_raw.copy()

        charge_full["SKU"] = (
            charge_full["SKU"]
            .astype("string")
            .str.strip()
        )

        if "Model" not in charge_full.columns:
            raise ValueError(
                "LLM matching requires a Model column "
                "in the Charge Sheet"
            )

        charge_full["Model"] = (
            charge_full["Model"]
            .astype("string")
            .str.strip()
        )

        for filename in (
            "RRP_Changes.csv",
            "End_Date_Changes.csv",
        ):
            if not outputs[filename].empty:
                outputs[filename] = (
                    add_llm_suggestions(
                        outputs[filename],
                        charge_full,
                    )
                )

    for filename, frame in outputs.items():
        frame.to_csv(
            OUTPUT_DIR / filename,
            index=False,
        )

        logger.info(
            "Wrote %s (%d rows)",
            filename,
            len(frame),
        )

    logger.info(
        "Processed Charge Sheet %s, %d RIMS files, "
        "%d unique products",
        charge_path.name,
        len(rims_paths),
        len(latest_snapshot(rims_all)),
    )

    return outputs


if __name__ == "__main__":
    main()