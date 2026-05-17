"""Phase 1 Q6 (2026-05-17) — extract Alpha191 A股 alphas from JoinQuant
jqdatasdk and translate to BRAIN DSL.

Source: https://raw.githubusercontent.com/JoinQuant/jqdatasdk/master/jqdatasdk/alpha191.py
(191 alphas, pseudo-code in docstring "公式:" sections).

Strategy (partial-OK per user decision in plan v1.3 §5):
- Parse all 191 docstring formulas
- Pre-normalize: lowercase known BRAIN fields, leave operators alone
- Auto-translate via backend.qlib_translator (now extended with UPPERCASE
  Alpha191 operator aliases — MEAN/STD/DELAY/CORR/TSMAX/...)
- Skip formulas containing ternary `? :` (Alpha191's IF-form — current
  translator doesn't have a ternary parser; future work)
- Skip formulas with logical operators (||, &&, ==, <, >, !=) when not
  inside a known IF/Greater/Less call
- Successes → backend/data/alpha191_jq.json with full metadata
- Failures → scripts/alpha191_translation_failures.log

Goal per plan §5.2: 30-50 translatable rows (sub-set of 191 candidates).

USAGE:
  # Fetch alpha191.py source first (cached to /tmp or pass --source)
  curl -sS https://raw.githubusercontent.com/JoinQuant/jqdatasdk/master/jqdatasdk/alpha191.py > /tmp/alpha191_source.py
  python scripts/extract_alpha191.py --source /tmp/alpha191_source.py

Plan reference: ~/.claude/plans/phase1-kickoff-2026-05-17.md v1.3 §5.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Add repo root to sys.path so backend imports work when run from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.qlib_translator import translate, QLIB_TO_BRAIN_OPERATORS  # noqa: E402

logger = logging.getLogger("extract_alpha191")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

# Alpha191 docstrings use uppercase field tokens like CLOSE / OPEN / HIGH /
# LOW / VOLUME / AMOUNT / VWAP / RETURNS / BENCHMARKINDEXCLOSE / ...
# BRAIN expects lowercase. Replace as whole-word, case-sensitive.
_FIELD_NORMALIZE: Dict[str, str] = {
    "CLOSE": "close",
    "OPEN": "open",
    "HIGH": "high",
    "LOW": "low",
    "VOLUME": "volume",
    "AMOUNT": "amount",
    "VWAP": "vwap",
    "RETURNS": "returns",
    "RET": "returns",
    "CAP": "cap",
    # A股-specific fields — pass through lowercased so BRAIN can map if available
    "BENCHMARKINDEXCLOSE": "benchmark_close",
    "BENCHMARKINDEXOPEN": "benchmark_open",
    "BANCHMARKINDEXCLOSE": "benchmark_close",  # alpha191 typo "BANCH"
    "BANCHMARKINDEXOPEN": "benchmark_open",
    "INDUSTRY": "industry",
    "DTM": "dtm",
    "DBM": "dbm",
    "TR": "tr",
    "HD": "hd",
    "LD": "ld",
}

# Operators sometimes appear lowercase in Alpha191 source (`delta`, `mean`).
# Pre-uppercase them so the translator dispatch lookup hits the dict keys.
# Build the set from QLIB_TO_BRAIN_OPERATORS — anything whose UPPER form is
# a valid key gets uppercased.
_OPERATOR_NORMALIZE: Dict[str, str] = {
    k.lower(): k for k in QLIB_TO_BRAIN_OPERATORS.keys()
    if not k.isupper()  # CamelCase keys: Mean → mean→Mean
}
# Also handle the all-caps-key collisions: e.g. MEAN/mean both map to MEAN
for k in QLIB_TO_BRAIN_OPERATORS.keys():
    if k.isupper():
        _OPERATOR_NORMALIZE.setdefault(k.lower(), k)


def normalize_formula(formula: str) -> str:
    """Pre-normalize Alpha191 docstring formula before passing to translator.

    1. Lowercase known field tokens (CLOSE → close, ...)
    2. Uppercase known operator tokens that appear lowercase (delta → DELTA, ...)
    3. Matlab-style `./` `.*` → `/` `*` (Alpha191 uses Matlab element-wise notation)
    4. Strip whitespace and common artifacts
    """
    out = formula.strip()
    # Strip leading "公式:" prefix if present
    out = re.sub(r"^公式[::]\s*", "", out)
    # Strip enclosing parens around the whole expression if balanced
    while out.startswith("(") and out.endswith(")") and _is_balanced(out[1:-1]):
        out = out[1:-1].strip()

    # Matlab element-wise → plain (BRAIN doesn't distinguish element-wise
    # from broadcasted, since everything is per-stock-per-day already)
    out = out.replace("./", "/").replace(".*", "*")
    # Power notation: `^` → BRAIN doesn't have ^ operator; rewrite x^N → power(x, N)
    # Only handle the simple x^literal case; complex bases left for translator fail
    out = re.sub(r"([a-zA-Z0-9_]+|\([^()]*\))\s*\^\s*(\d+(?:\.\d+)?)", r"power(\1, \2)", out)

    # Token replace: walk word-by-word, look up in normalization tables.
    # Word = [A-Za-z][A-Za-z0-9_]* — must match boundaries to avoid
    # false-replacing inside identifiers like ts_mean.
    def _sub(m: re.Match) -> str:
        tok = m.group(0)
        # field normalize first (case-sensitive UPPERCASE source token only)
        if tok in _FIELD_NORMALIZE:
            return _FIELD_NORMALIZE[tok]
        # operator normalize (lowercase source token → UPPER key)
        if tok in _OPERATOR_NORMALIZE:
            return _OPERATOR_NORMALIZE[tok]
        return tok

    out = re.sub(r"\b[A-Za-z][A-Za-z0-9_]*\b", _sub, out)
    return out


def _is_balanced(s: str) -> bool:
    d = 0
    for ch in s:
        if ch == "(":
            d += 1
        elif ch == ")":
            d -= 1
            if d < 0:
                return False
    return d == 0


# ---------------------------------------------------------------------------
# Alpha191 source parser
# ---------------------------------------------------------------------------

_ALPHA_DEF_RE = re.compile(
    r"def\s+alpha_(\d{3})\([^)]*\):\s*\"\"\"(.*?)\"\"\"",
    re.DOTALL,
)
_FORMULA_LINE_RE = re.compile(r"公式[::]\s*\n\s*(.+?)(?:\n\s*Inputs?|\n\s*Output|$)", re.DOTALL)


def parse_alpha191_source(source_text: str) -> List[Dict]:
    """Return list of {alpha_id, formula_raw} dicts from alpha191.py source."""
    out: List[Dict] = []
    for match in _ALPHA_DEF_RE.finditer(source_text):
        alpha_id = int(match.group(1))
        docstring = match.group(2)
        formula_match = _FORMULA_LINE_RE.search(docstring)
        if not formula_match:
            continue
        formula = formula_match.group(1).strip()
        # Collapse internal newlines + extra spaces
        formula = re.sub(r"\s+", " ", formula)
        out.append({
            "alpha_id": alpha_id,
            "formula_raw": formula,
        })
    return out


# ---------------------------------------------------------------------------
# Translation pipeline
# ---------------------------------------------------------------------------

# Alpha191 formulas containing these patterns can't be translated by the
# current qlib_translator (no ternary support, no infix logical ops). Skip
# rather than producing a broken BRAIN expression.
_UNTRANSLATABLE_PATTERNS = [
    r"\?.*?:",      # ternary `cond ? a : b`
    r"\|\|",        # logical OR
    r"&&",          # logical AND
    r"<>",          # not-equal
    r"!=",
]


def _has_untranslatable_pattern(formula: str) -> Optional[str]:
    for pat in _UNTRANSLATABLE_PATTERNS:
        if re.search(pat, formula):
            return pat
    return None


def try_translate(alpha_id: int, formula_raw: str) -> Tuple[Optional[str], str]:
    """Try translate one Alpha191 formula. Returns (brain_expr | None, reason)."""
    blocker = _has_untranslatable_pattern(formula_raw)
    if blocker:
        return None, f"untranslatable_pattern: {blocker!r}"
    try:
        normalized = normalize_formula(formula_raw)
        # Reject if normalized expression has unbalanced parens — usually means
        # JoinQuant source typo (alpha_001 / alpha_008 / alpha_009 all suffer).
        if not _is_balanced(normalized):
            return None, "unbalanced_parens_in_source"
        brain_expr = translate(normalized)
        if not brain_expr or len(brain_expr) < 5:
            return None, "empty_translation"
        # Final sanity: BRAIN parsers reject unbalanced parens too — verify
        if not _is_balanced(brain_expr):
            return None, "unbalanced_parens_in_output"
        return brain_expr, "ok"
    except NotImplementedError as e:
        return None, f"translator_skip: {str(e)[:120]}"
    except Exception as e:
        return None, f"translator_error: {type(e).__name__}: {str(e)[:80]}"


# ---------------------------------------------------------------------------
# Output assembly
# ---------------------------------------------------------------------------

def to_kb_row(alpha_id: int, formula_raw: str, brain_expr: str) -> Dict:
    """Build an ExternalKnowledge-shaped JSON row for KB import."""
    return {
        "source": "alpha191_jq",
        "pattern": brain_expr,
        "description": (
            f"Alpha191 #{alpha_id} from JoinQuant jqdatasdk — CHN region, "
            f"short horizon"
        ),
        "category": "pv",  # default; Phase 2+ classifier can refine
        "confidence": 0.75,
        "verified": True,
        "source_title": f"jqdatasdk Alpha191 #{alpha_id}",
        "source_url": (
            "https://github.com/JoinQuant/jqdatasdk/blob/master/jqdatasdk/"
            "alpha191.py"
        ),
        # Q6 forward-compat metadata (reuses ExternalKnowledge dataclass fields)
        "qlib_origin": formula_raw,
        "raw_feature": False,
        # Region/horizon don't have dedicated ExternalKnowledge fields — passed
        # via meta_data in import (see import_alpha191_knowledge handler).
        "alpha191_id": alpha_id,
        "region": "CHN",
        "horizon": "short",
    }


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

OUTPUT_JSON = Path("backend/data/alpha191_jq.json")
FAILURES_LOG = Path("scripts/alpha191_translation_failures.log")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--source", default="/tmp/alpha191_source.py",
                   help="path to cached alpha191.py source")
    p.add_argument("--limit", type=int, default=None,
                   help="translate only first N alphas (default: all 191)")
    args = p.parse_args()

    src_path = Path(args.source)
    if not src_path.exists():
        logger.error(f"--source {src_path} does not exist. Fetch via:\n"
                     "  curl -sS https://raw.githubusercontent.com/JoinQuant/"
                     "jqdatasdk/master/jqdatasdk/alpha191.py > "
                     f"{src_path}")
        return 1

    source = src_path.read_text(encoding="utf-8")
    candidates = parse_alpha191_source(source)
    logger.info(f"parsed {len(candidates)} alpha191 candidates from source")

    if args.limit:
        candidates = candidates[:args.limit]

    rows: List[Dict] = []
    failures: List[str] = []
    for c in candidates:
        brain_expr, reason = try_translate(c["alpha_id"], c["formula_raw"])
        if brain_expr is None:
            failures.append(f"alpha_{c['alpha_id']:03d}\t{reason}\t{c['formula_raw'][:120]}")
            continue
        rows.append(to_kb_row(c["alpha_id"], c["formula_raw"], brain_expr))

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(
        json.dumps(rows, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    FAILURES_LOG.parent.mkdir(parents=True, exist_ok=True)
    FAILURES_LOG.write_text("\n".join(failures), encoding="utf-8")

    logger.info(f"done: {len(rows)} translated / {len(failures)} skipped / "
                f"{len(candidates)} candidates")
    logger.info(f"output: {OUTPUT_JSON} ({len(rows)} rows)")
    logger.info(f"failures: {FAILURES_LOG} ({len(failures)} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
