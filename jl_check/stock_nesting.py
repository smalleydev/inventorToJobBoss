"""
Stock nesting: given a set of individual piece lengths that all need to
be cut from the same material, compute how many standard-length sticks
are required.

Uses First-Fit-Decreasing (FFD) bin packing — a simple, well-understood
heuristic: sort pieces largest-first, place each into the first stick
that still has room, opening a new stick only when none fit. FFD is not
always mathematically optimal but is close in practice and simple to
reason about/verify by hand, which matters more here than squeezing out
the last percent of material efficiency.

Kerf: every cut loses a sliver of material to the saw blade. We model
this by adding a fixed allowance to each piece's length before packing,
so the packing accounts for blade waste on every cut, not just gaps
between pieces.
"""

from dataclasses import dataclass, field

# Fixed per-cut kerf allowance, inches. Applied to every individual
# piece regardless of material — not a per-material or per-shape value.
KERF_ALLOWANCE_IN = 0.125

# Floating-point tolerance for length comparisons, inches. Lengths
# arriving from CAD software often carry tiny binary-representation
# noise (e.g. 0.1 * 3 == 0.30000000000000004, not exactly 0.3) — without
# this, a piece that should fit a stick perfectly can get spuriously
# rejected as "oversized" by a difference no measuring tool could ever
# detect. 1e-6 inch is far below any real machining tolerance but far
# above typical float noise, so it absorbs the noise without masking a
# genuine oversize.
FLOAT_TOLERANCE_IN = 1e-6


@dataclass
class NestingResult:
    sticks_needed: int
    total_piece_count: int
    stick_length_in: float
    total_stock_length_in: float  # sticks_needed * stick_length_in — what to ORDER
    total_material_used_in: float  # what's actually REQUIRED given how nesting
                                    # actually packed the pieces — see nest_pieces.


def nest_pieces(piece_lengths_in: list[float], stick_length_in: float,
                kerf_in: float = KERF_ALLOWANCE_IN) -> NestingResult:
    """
    Compute how many stick_length_in sticks are needed to cut every
    length in piece_lengths_in (one entry per individual piece — a row
    with Quantity=3 at 20" should contribute three separate 20.0
    entries, not one).

    Raises ValueError if any single piece (plus kerf) is longer than
    the stick itself — that piece can never be cut from this stock,
    which is a real data problem worth surfacing rather than silently
    mis-packing.
    """
    if not piece_lengths_in:
        return NestingResult(0, 0, stick_length_in, 0.0, 0.0)

    # Sort largest-first: placing big pieces first tends to pack tighter,
    # since small pieces are more flexible about which remaining gap
    # they fit into.
    pieces = sorted((p + kerf_in for p in piece_lengths_in), reverse=True)

    oversized = [p for p in pieces if p > stick_length_in + FLOAT_TOLERANCE_IN]
    if oversized:
        raise ValueError(
            f"Piece length {max(oversized):.3f}in (including {kerf_in}in kerf) "
            f"exceeds the stick length of {stick_length_in}in — this piece "
            f"cannot be cut from this stock."
        )

    remaining_capacity: list[float] = []  # one entry per stick opened so far

    for piece in pieces:
        placed = False
        for i, capacity in enumerate(remaining_capacity):
            if capacity >= piece - FLOAT_TOLERANCE_IN:
                remaining_capacity[i] -= piece
                placed = True
                break
        if not placed:
            remaining_capacity.append(stick_length_in - piece)

    sticks_needed = len(remaining_capacity)

    # Material actually required, given how nesting really packed these
    # pieces — NOT a flat sum(piece + kerf) computed independently of
    # the packing result (that number can't tell a perfectly-nested job
    # apart from a badly-fragmented one, since it never looks at
    # remaining_capacity at all).
    #
    # Every stick except the LAST one opened is treated as fully spent,
    # leftover capacity included: FFD checks every already-open stick,
    # in order, before ever opening a new one, so by the time a new
    # stick gets opened, every earlier stick's remaining capacity has
    # already proven too small for anything else left in this cut list
    # — otherwise FFD would have placed it there instead. That leftover
    # is real scrap, not usable stock, and has to be charged for.
    #
    # The LAST stick opened is different: whatever's left on it is only
    # there because the cut list ran out, not because it rejected
    # anything — that remainder is a genuine, still-usable length of
    # stock (a drop for the next job), so only what was actually cut
    # from that one stick gets charged.
    #
    # Concrete example: a 240" stick, one 235" piece and one 10" piece.
    # 235+10 doesn't fit on one stick, so nesting opens two. Stick 1's
    # ~4.875" leftover (240 - 235 - kerf) is unusable for the 10" piece
    # and gets charged in full (240"). Stick 2 only has the 10" piece on
    # it, so only its actual usage (10" + kerf) is charged, not the
    # whole second stick. Total required ≈ 250.125", not the naive
    # 245.25" you'd get from just summing the two piece+kerf lengths,
    # and not the full 480" of two whole sticks either.
    last_stick_used_in = stick_length_in - remaining_capacity[-1]
    total_material_used_in = (sticks_needed - 1) * stick_length_in + last_stick_used_in

    return NestingResult(
        sticks_needed=sticks_needed,
        total_piece_count=len(piece_lengths_in),
        stick_length_in=stick_length_in,
        total_stock_length_in=sticks_needed * stick_length_in,
        total_material_used_in=total_material_used_in,
    )


def expand_pieces(rows: list[dict]) -> list[float]:
    """
    Expand a list of BOM rows (each with CutLengthIn and Quantity) into
    a flat list of individual piece lengths — a row with Quantity=3 at
    CutLengthIn=20 becomes three 20.0 entries. This is the format
    nest_pieces expects.
    """
    pieces = []
    for row in rows:
        length = row.get("CutLengthIn")
        quantity = int(row.get("Quantity", 0) or 0)
        if length not in (None, 0, 0.0) and quantity > 0:
            pieces.extend([float(length)] * quantity)
    return pieces