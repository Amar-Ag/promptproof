"""The probe corpus: loading and validation.

Probes are data, never code. They are read from YAML, validated, and then only
ever transmitted to a model — never evaluated, shell-interpolated, or templated
into SQL (AGENTS.md hard rule 3).

The corpus is loaded and validated once at startup. A malformed probe fails the
process rather than surfacing as a confusing scan error later.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

import yaml

Severity = Literal["low", "medium", "high", "critical"]
SEVERITIES: tuple[str, ...] = get_args(Severity)

REQUIRED_FIELDS = (
    "id",
    "suite",
    "severity",
    "title",
    "payload",
    "success_criteria",
    "remediation",
)


class CorpusError(Exception):
    """A probe file is invalid. Raised at startup, never swallowed."""


@dataclass(frozen=True, slots=True)
class Probe:
    id: str
    suite: str
    severity: str
    title: str
    payload: str
    success_criteria: str
    remediation: str
    source: str = ""


@dataclass(frozen=True, slots=True)
class Corpus:
    probes: tuple[Probe, ...]
    version: str

    def for_suites(self, suites: list[str]) -> tuple[Probe, ...]:
        """Probes for the requested suites, in a stable order.

        Ordering is deterministic (suite, then probe id) so the same request always
        runs the same probes in the same sequence — the structure of a scan is
        reproducible even though the verdicts are not.
        """
        wanted = set(suites)
        return tuple(
            sorted(
                (p for p in self.probes if p.suite in wanted),
                key=lambda p: (p.suite, p.id),
            )
        )

    def count_by_suite(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for probe in self.probes:
            counts[probe.suite] = counts.get(probe.suite, 0) + 1
        return counts


def _parse_probe(raw: Any, source_file: Path) -> Probe:
    if not isinstance(raw, dict):
        raise CorpusError(f"{source_file}: expected a mapping, got {type(raw).__name__}")

    missing = [f for f in REQUIRED_FIELDS if not raw.get(f)]
    if missing:
        raise CorpusError(f"{source_file}: missing required field(s): {', '.join(missing)}")

    severity = str(raw["severity"])
    if severity not in SEVERITIES:
        raise CorpusError(
            f"{source_file}: severity {severity!r} is not one of {', '.join(SEVERITIES)}"
        )

    expected_suite = source_file.parent.name
    suite = str(raw["suite"])
    if suite != expected_suite:
        raise CorpusError(
            f"{source_file}: suite {suite!r} does not match its directory {expected_suite!r}"
        )

    return Probe(
        id=str(raw["id"]),
        suite=suite,
        severity=severity,
        title=str(raw["title"]),
        payload=str(raw["payload"]),
        success_criteria=str(raw["success_criteria"]),
        remediation=str(raw["remediation"]),
        source=str(raw.get("source", "")),
    )


def load_corpus(root: Path) -> Corpus:
    """Load every probe under `root`, validate it, and version the result.

    The version is a digest of the probe files themselves, so it changes exactly
    when the corpus changes. Stamped onto every scan, it is what makes a report
    interpretable months later.
    """
    if not root.is_dir():
        raise CorpusError(f"Probe corpus directory not found: {root}")

    files = sorted(root.glob("*/*.yaml")) + sorted(root.glob("*/*.yml"))
    if not files:
        raise CorpusError(f"No probe files found under {root}")

    probes: list[Probe] = []
    seen: dict[str, Path] = {}
    digest = hashlib.sha256()

    for path in files:
        content = path.read_bytes()
        digest.update(path.name.encode())
        digest.update(content)

        try:
            raw = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise CorpusError(f"{path}: invalid YAML: {exc}") from exc

        entries = raw if isinstance(raw, list) else [raw]
        for entry in entries:
            probe = _parse_probe(entry, path)
            if probe.id in seen:
                raise CorpusError(
                    f"{path}: duplicate probe id {probe.id!r}, already defined in {seen[probe.id]}"
                )
            seen[probe.id] = path
            probes.append(probe)

    return Corpus(probes=tuple(probes), version=digest.hexdigest()[:12])


_cached: Corpus | None = None


def get_corpus(root: Path | None = None) -> Corpus:
    """The process-wide corpus, loaded once."""
    global _cached
    if _cached is None:
        _cached = load_corpus(root or default_corpus_root())
    return _cached


def default_corpus_root() -> Path:
    """`probes/` at the repository root, resolved relative to this file.

    Walks up looking for the directory rather than depending on the working
    directory, so the API, the worker and the tests all find the same corpus.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "probes"
        if candidate.is_dir():
            return candidate
    raise CorpusError("Could not locate a 'probes' directory above this module")
