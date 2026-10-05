"""Upstream source pins copied from the methodology research ``source-pins.json``
(retrieved 2026-10-05T03:27:08Z). Pins identify what an optional adapter targets; they do
not mean the upstream code was installed, vendored, or executed here."""

from __future__ import annotations

PINS = {
    "modAL": {"repo": "modAL-python/modAL", "sha": "bba6f6fd00dbb862b1e09259b78caf6cffa2e755",
              "license": "MIT"},
    "apricot": {"repo": "jmschrei/apricot", "sha": "962f597a57fcb880a3b19befa7a3eebccc6b5228",
                "license": "MIT"},
    "MAPIE": {"repo": "scikit-learn-contrib/MAPIE", "sha": "3b84b8212db2bba452ef5a09ae06a0dd545869ae",
              "license": "BSD-3-Clause"},
    "detection_limits": {"repo": "usnistgov/detection_limits",
                         "sha": "1a674bbb0fff99c28539151668639b23e5b346e9",
                         "license": "NIST Software License notice (keep notice, acknowledge NIST, "
                                    "mark modifications)"},
    "artimagen": {"repo": "strec007/artimagen", "sha": "58ba160806ca6f7beec184f46f76e1d4049bf710",
                  "license": "US-government public-domain statement"},
    "alibi-detect": {"repo": "SeldonIO/alibi-detect", "sha": "c2fd0e05c648d353467bdb15fd6149a103b3a981",
                     "license": "BSL-1.1 (reviewed); NOT integrated, NO source copied"},
}


def pin(name: str) -> dict:
    p = PINS[name]
    return {**p, "url": f"https://github.com/{p['repo']}/tree/{p['sha']}"}
