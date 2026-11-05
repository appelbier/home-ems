"""Electric vehicle charge planning.

The EV is planned before the house battery: it is a flexible load with a
deadline. Its slots are then fed to the battery optimiser as extra load (and
optionally marked as load the house battery must not supply).

Pure numpy, no Home Assistant imports.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

EV_OFF = "off"
EV_SOLAR = "solar"
EV_SMART = "smart"
EV_MAX = "max"
EV_MODES = (EV_OFF, EV_SOLAR, EV_SMART, EV_MAX)


@dataclass
class EvRequest:
    mode: str
    energy_kwh: float  # AC energy still needed from the grid / house
    deadline_index: int  # slot index the car must be ready by (exclusive)
    min_kw: float = 4.1
    max_kw: float = 11.0
    max_price: float | None = None  # smart mode without deadline: price ceiling


def plan_ev(
    req: EvRequest,
    duration_h: np.ndarray,
    surplus_kwh: np.ndarray,
    import_price: np.ndarray,
    export_price: np.ndarray,
) -> np.ndarray:
    """Return EV energy (kWh, AC) per slot."""
    slots = len(duration_h)
    plan = np.zeros(slots)
    need = max(0.0, req.energy_kwh)
    if req.mode == EV_OFF or need <= 0 or slots == 0:
        return plan
    cap = req.max_kw * duration_h
    floor = req.min_kw * duration_h

    if req.mode == EV_MAX:
        for t in range(slots):
            take = min(cap[t], need)
            plan[t] = take
            need -= take
            if need <= 1e-6:
                break
        return plan

    if req.mode == EV_SOLAR:
        for t in range(slots):
            available = min(cap[t], surplus_kwh[t])
            if available >= floor[t] and available > 0:
                take = min(available, need)
                plan[t] = take
                need -= take
                if need <= 1e-6:
                    break
        return plan

    # Smart: charge at full power in the cheapest slots before the deadline.
    # A slot's price is the average over its energy: the PV surplus part is
    # worth the export price, the rest comes from the grid / house battery.
    deadline = min(max(req.deadline_index, 0), slots)
    window = range(deadline) if deadline > 0 else range(slots)
    options: list[tuple[float, int]] = []
    for t in window:
        if cap[t] <= 0:
            continue
        solar = min(cap[t], max(0.0, surplus_kwh[t]))
        price = (solar * float(export_price[t]) + (cap[t] - solar) * float(import_price[t])) / cap[t]
        options.append((price, t))
    options.sort()
    for price, t in options:
        if need <= 1e-6:
            break
        if deadline <= 0 and req.max_price is not None and price > req.max_price:
            # No deadline: only charge when it is cheap enough.
            break
        take = min(cap[t], max(need, floor[t]))
        plan[t] = take
        need -= take

    if need > 1e-6 and deadline < slots:
        # Deadline cannot be met: keep charging at full power after it.
        for t in range(deadline, slots):
            take = min(cap[t], need)
            plan[t] = take
            need -= take
            if need <= 1e-6:
                break
    return plan
