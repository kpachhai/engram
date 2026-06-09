"""Contradiction-band pair generation.

Contradiction candidates are pairs similar enough to discuss the same topic
but below the near-duplicate threshold: ``low <= similarity < high``. The
half-open interval keeps the two passes disjoint - a pair at the near-dup
threshold belongs to clustering, not contradiction judging.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Sequence

    from numpy.typing import NDArray


def contradiction_band_pairs(
    ids: Sequence[str],
    matrix: NDArray[np.float64],
    *,
    low: float,
    high: float,
    cap: int | None = None,
) -> list[tuple[str, str, float]]:
    """Pairs with ``low <= similarity < high``, descending, optionally capped.

    The cap keeps the LLM-judged pass within budget; callers report a capped
    pass as covering ``cap`` of the full candidate count.
    """
    count = len(ids)
    if count < 2:
        return []
    upper_i, upper_j = np.triu_indices(count, k=1)
    sims = matrix[upper_i, upper_j]
    in_band = (sims >= low) & (sims < high)
    band_sims = sims[in_band]
    band_pairs = np.column_stack((upper_i[in_band], upper_j[in_band]))
    order = np.lexsort((band_pairs[:, 1], band_pairs[:, 0], -band_sims))
    if cap is not None:
        order = order[:cap]
    return [
        (ids[int(band_pairs[k, 0])], ids[int(band_pairs[k, 1])], float(band_sims[k])) for k in order
    ]


__all__ = ["contradiction_band_pairs"]
