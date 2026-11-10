"""Suite and model metadata, so the frontend never hard-codes either."""

from fastapi import APIRouter
from promptproof_core.catalog import TARGET_MODELS
from promptproof_core.probes import get_corpus
from promptproof_core.suites import SUITES

from ..schemas import ModelOut, SuiteOut

router = APIRouter(tags=["metadata"])


@router.get("/suites", response_model=list[SuiteOut], summary="List probe suites")
async def list_suites() -> list[SuiteOut]:
    counts = get_corpus().count_by_suite()
    return [
        SuiteOut(
            key=s.key,
            title=s.title,
            description=s.description,
            probe_count=counts.get(s.key, 0),
        )
        for s in SUITES
    ]


@router.get("/models", response_model=list[ModelOut], summary="List allowed target models")
async def list_models() -> list[ModelOut]:
    return [ModelOut(id=m.id, label=m.label) for m in TARGET_MODELS]


@router.get("/corpus", summary="Probe corpus version")
async def corpus_info() -> dict[str, object]:
    corpus = get_corpus()
    return {
        "version": corpus.version,
        "probe_count": len(corpus.probes),
        "by_suite": corpus.count_by_suite(),
    }
