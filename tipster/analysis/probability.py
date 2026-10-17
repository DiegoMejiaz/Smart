"""Herramientas probabilísticas: eliminación de margen y modelo de Poisson."""
from __future__ import annotations

import math
from dataclasses import dataclass


def implied(odds: float) -> float:
    return 1.0 / odds


def devig_power(odds: list[float], tol: float = 1e-9) -> list[float]:
    """Elimina el margen con el método de potencia (corrige el sesgo favorito/sorpresa).

    Busca k tal que sum((1/o_i)^k) = 1.
    """
    raw = [1.0 / o for o in odds]
    if sum(raw) <= 1.0:  # sin margen (o arbitraje): normalización simple
        total = sum(raw)
        return [r / total for r in raw]
    lo, hi = 1.0, 10.0
    for _ in range(100):
        k = (lo + hi) / 2
        s = sum(r ** k for r in raw)
        if abs(s - 1.0) < tol:
            break
        if s > 1.0:
            lo = k
        else:
            hi = k
    probs = [r ** k for r in raw]
    total = sum(probs)
    return [p / total for p in probs]


def _poisson_pmf(k: int, lam: float) -> float:
    return math.exp(-lam) * lam ** k / math.factorial(k)


@dataclass
class ScoreMatrix:
    """Distribución de marcadores con corrección Dixon-Coles para marcadores bajos."""
    lam_home: float
    lam_away: float
    rho: float = -0.08
    max_goals: int = 10

    def __post_init__(self) -> None:
        n = self.max_goals + 1
        ph = [_poisson_pmf(i, self.lam_home) for i in range(n)]
        pa = [_poisson_pmf(j, self.lam_away) for j in range(n)]
        m = [[ph[i] * pa[j] * self._tau(i, j) for j in range(n)] for i in range(n)]
        total = sum(map(sum, m))
        self.matrix = [[v / total for v in row] for row in m]

    def _tau(self, i: int, j: int) -> float:
        lh, la, r = self.lam_home, self.lam_away, self.rho
        if i == 0 and j == 0:
            return 1 - lh * la * r
        if i == 0 and j == 1:
            return 1 + lh * r
        if i == 1 and j == 0:
            return 1 + la * r
        if i == 1 and j == 1:
            return 1 - r
        return 1.0

    def _sum(self, cond) -> float:
        return sum(p for i, row in enumerate(self.matrix) for j, p in enumerate(row) if cond(i, j))

    def p_home(self) -> float:
        return self._sum(lambda i, j: i > j)

    def p_draw(self) -> float:
        return self._sum(lambda i, j: i == j)

    def p_away(self) -> float:
        return self._sum(lambda i, j: i < j)

    def p_over(self, line: float) -> float:
        return self._sum(lambda i, j: i + j > line)

    def p_btts(self) -> float:
        return self._sum(lambda i, j: i > 0 and j > 0)

    def p_home_handicap(self, line: float) -> float:
        """P(local cubre el hándicap) para líneas .5 (sin push)."""
        return self._sum(lambda i, j: i + line > j)
