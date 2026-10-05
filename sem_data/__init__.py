"""Carinthia-S SEM defect data preparation (post-hackathon development).

Safe archive extraction, image/mask pairing and validation, content hashing,
duplicate-grouped deterministic splits and a JSON manifest that follows the
shared ``schema_version: 1`` data interface. See SEM_DATA_GUIDE.md.
"""

DATASET_ID = "carinthia-s"
SCHEMA_VERSION = 1
DEFAULT_SEED = 2026100501
DEFAULT_FRACTIONS = (0.7, 0.15, 0.15)
SPLITS = ("train", "calibration", "test")

LICENSE = {
    "id": "CC-BY-4.0",
    "url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": (
        "Carinthia-S dataset by Corinna Kofler and Vahidin Hasic, Zenodo, 2025, "
        "doi:10.5281/zenodo.16895427, derived from the Carinthia dataset "
        "(doi:10.5281/zenodo.10696644). Licensed CC-BY-4.0."
    ),
    "source": "https://doi.org/10.5281/zenodo.16895427",
}
