"""Archive adjacency: rules/event interpretation and whole-case handoff."""

from .models import (  # noqa: F401
    ActiveHold,
    BlockReason,
    ConflictError,
    DocQualification,
    Document,
    Event,
    Hold,
    InvalidInterval,
    QualificationProduct,
    RejectError,
    Rule,
    RuleReason,
    UnknownReference,
)
from .rules_service import RuleEventStore  # noqa: F401
from .handoff_service import HandoffRepo  # noqa: F401

__all__ = [
    "RuleEventStore",
    "HandoffRepo",
    "QualificationProduct",
    "DocQualification",
    "RejectError",
    "ConflictError",
    "InvalidInterval",
    "UnknownReference",
]
from .deeds import (  # noqa: F401
    CadastralEventRevision,
    DeedRevision,
    DeedStore,
    evidence_as_json,
    import_deed_materials,
    index_as_json,
    load_deed_materials,
)
from .index_service import (  # noqa: F401
    DEFAULT_GRADE_ORDER,
    frac_str,
    parse_rational,
    report_as_json,
    solve_additive_index,
)
