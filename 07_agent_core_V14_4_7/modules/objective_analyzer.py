from __future__ import annotations

"""Small helper for transparent objective-component extraction."""

from typing import Any
import pandas as pd

from modules.v14_utils import as_float


def objective_components(ranking_row: pd.Series | None) -> dict[str, float]:
    if ranking_row is None:
        return {}
    result: dict[str, float] = {}
    for name, value in ranking_row.items():
        if str(name).upper().startswith(("RMSE_", "MAE_", "BIAS_")):
            number = as_float(value)
            if number is not None:
                result[str(name)] = number
    return result
