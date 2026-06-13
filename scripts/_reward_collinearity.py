import asyncio
import sys
sys.path.insert(0, '.')
from sqlalchemy import text as t


async def main():
    from backend.database import AsyncSessionLocal
    from backend.field_selector import novelty, signal_quality, orthogonality_credible
    import statistics as st

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(t(
            "SELECT times_mined, signal_p90, band_pass_count, orthogonality, distinct_alphas "
            "FROM datafield_cell_stats WHERE times_mined > 0"
        ))).all()
    nv, sq, oc = [], [], []
    for tm, sp, bp, orth, da in rows:
        nv.append(novelty(tm or 0))
        sq.append(signal_quality(tm or 0, sp, bp))
        oc.append(orthogonality_credible(orth, da))

    def pearson(a, b):
        n = len(a)
        ma, mb = sum(a) / n, sum(b) / n
        cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
        va = sum((x - ma) ** 2 for x in a) ** 0.5
        vb = sum((y - mb) ** 2 for y in b) ** 0.5
        return cov / (va * vb) if va > 0 and vb > 0 else 0.0

    print("cells (times_mined>0):", len(rows))
    print("factor spread (std / mean):")
    for nm, v in [("novelty", nv), ("signal_quality", sq), ("orthogonality_credible", oc)]:
        print("  %-22s mean=%.3f std=%.3f min=%.2f max=%.2f" % (
            nm, st.mean(v), st.pstdev(v), min(v), max(v)))
    print("pairwise |pearson| (共线警戒 >0.8):")
    print("  novelty × signal     = %.3f" % abs(pearson(nv, sq)))
    print("  novelty × ortho      = %.3f" % abs(pearson(nv, oc)))
    print("  signal  × ortho      = %.3f" % abs(pearson(sq, oc)))
    # how often each factor is the binding (min) one — degenerate if one always dominates
    bind = {"novelty": 0, "signal": 0, "ortho": 0}
    for i in range(len(rows)):
        m = min(nv[i], sq[i], oc[i])
        if m == nv[i]: bind["novelty"] += 1
        elif m == sq[i]: bind["signal"] += 1
        else: bind["ortho"] += 1
    print("binding (min) factor share:", {k: "%.0f%%" % (100 * v / len(rows)) for k, v in bind.items()})


asyncio.run(main())
