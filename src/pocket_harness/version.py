#: Package version, kept in sync with ``pyproject.toml`` on a release.
__version__ = "0.1.0rc1"

#: Version of the deterministic transformation pipeline.
#:
#: Changing this value changes transformed-request hashes by design, so it must be
#: recorded in every run manifest (Plan.md §2 "Reproducibility", §8.3).
PIPELINE_VERSION = "pocket-harness/0.1.0"
