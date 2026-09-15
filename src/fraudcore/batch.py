"""Batch features for low-and-slow siphoning and destination risk (architecture sections 5.4, 10).

Siphoning is many transfers that are each unremarkable, so nothing inside one session can see it.
These functions look across a user's released transfers instead. They are pure: the aggregator
Lambda fetches the rows from the audit lake with Athena and calls them, and the siphoning simulator
drives that same deployed function, so the batch verdict and its evaluation are one implementation.

Per user, over the last 30 days: the amount quantiles, daily count and hour histogram that the
transaction channel reads.

Per user and recently added payee (an edge), four signals, each in [0, 1]:

    volume      cumulative amount sent to the payee, against three of the user's p95-sized transfers
    regularity  how clockwork the gaps between transfers are (coefficient of variation near zero)
    band        the share of amounts sitting just under the user's p95, where they stay quiet
    identity    how much worse the typing on this payee's confirmations scores than the same user's
                typing on every other payee: the signature of a second person using the account

Volume, regularity and band describe a new tutor's fees as well as theft. Only identity separates
the owner's own new habit from someone else's, so an edge is flagged only when the pattern is strong
and identity corroborates it. The graded score still raises the payee channel without a flag.

Per payee across users: distinct senders, and how many of them started paying it recently. Money
arriving from several unrelated customers who all began at once is the shape of a mule account.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from fraudcore.features import HOURS_PER_DAY, NEW_PAYEE_WINDOW_DAYS, UserAggregates

SECONDS_PER_DAY = 86_400

# How far back the aggregator reads. Longer than the new-payee window, so a payee first paid before
# the window is recognised as established rather than new.
LOOKBACK_DAYS = 90
AGGREGATE_WINDOW_DAYS = 30
RECENT_SENDER_DAYS = 7

# Regularity needs at least two gaps to have a dispersion at all.
MIN_EDGE_TRANSFERS = 3
# Gaps with a coefficient of variation at or above this are ordinary human timing; zero is a script.
REGULAR_CV = 0.5
# Cumulative amount to one new payee, in multiples of the user's p95 transfer, that scores 1.
VOLUME_SATURATION = 3.0
# The band just under the user's p95, as fractions of it.
BAND = (0.6, 1.0)
# Relative worsening of identity score. Below the floor is ordinary day-to-day variation in one
# person's typing; at the saturation the payee's sessions score twice as far from the profile.
IDENTITY_GAP_FLOOR = 0.3
IDENTITY_GAP_SATURATION = 1.0
MIN_IDENTITY_SAMPLES = 2
SIPHON_WEIGHTS: Mapping[str, float] = {
    "volume": 0.3,
    "regularity": 0.2,
    "band": 0.1,
    "identity": 0.4,
}
SIPHON_FLAG = 0.5

# New senders at which fan-in scores 1, and the fewest that can flag a payee on fan-in alone.
FAN_IN_SATURATION = 5
MIN_FLAG_SENDERS = 3
NEW_SENDER_MAJORITY = 0.5

AMOUNT_FLOOR = 1.0


@dataclass(frozen=True)
class TransferRecord:
    """One released transfer, read back from the lake. ``identity_score`` is the behaviour channel's
    raw score at its confirmation, or ``None`` when that decision is not in the lake."""

    uid: str
    payee_id: str
    amount: float
    at: float
    identity_score: float | None = None

    def __post_init__(self) -> None:
        if not self.uid or not self.payee_id:
            raise ValueError("uid and payee_id are required")
        if not (math.isfinite(self.amount) and self.amount > 0):
            raise ValueError("amount must be positive and finite")
        if not math.isfinite(self.at):
            raise ValueError("at must be finite")
        score = self.identity_score
        if score is not None and not (math.isfinite(score) and score >= 0):
            raise ValueError("identity_score must be non-negative and finite")


def quantile(values: Sequence[float], q: float) -> float:
    """Linear interpolation between the closest ranks, the definition numpy uses by default."""
    if not values:
        raise ValueError("quantile of no values")
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be in [0, 1]")
    ordered = sorted(values)
    position = q * (len(ordered) - 1)
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _in_window(at: float, now: float, days: float) -> bool:
    return now - days * SECONDS_PER_DAY < at <= now


def user_aggregates(records: Sequence[TransferRecord], now: float) -> UserAggregates | None:
    """The transaction channel's statistics over the last 30 days, or ``None`` with no transfers.

    The daily count's p95 is over every day of the window, including days with no transfer, so a
    user who pays once a week has a p95 near one rather than the count on their busiest day only.
    """
    window = [r for r in records if _in_window(r.at, now, AGGREGATE_WINDOW_DAYS)]
    if not window:
        return None
    daily = [0.0] * AGGREGATE_WINDOW_DAYS
    hours = [0.0] * HOURS_PER_DAY
    for record in window:
        daily[min(AGGREGATE_WINDOW_DAYS - 1, int((now - record.at) // SECONDS_PER_DAY))] += 1
        hours[datetime.fromtimestamp(record.at, UTC).hour] += 1
    amounts = [record.amount for record in window]
    return UserAggregates(
        amount_p50=quantile(amounts, 0.5),
        amount_p95=quantile(amounts, 0.95),
        daily_count_p95=quantile(daily, 0.95),
        hour_histogram=tuple(hours),
        history_count=len(window),
    )


@dataclass(frozen=True)
class EdgeSignals:
    uid: str
    payee_id: str
    first_seen: float
    transfers: int
    cumulative: float
    interval_cv: float | None
    band_fraction: float
    identity_gap: float | None
    volume: float
    regularity: float
    band: float
    identity: float

    @property
    def siphon_score(self) -> float:
        return (
            SIPHON_WEIGHTS["volume"] * self.volume
            + SIPHON_WEIGHTS["regularity"] * self.regularity
            + SIPHON_WEIGHTS["band"] * self.band
            + SIPHON_WEIGHTS["identity"] * self.identity
        )

    @property
    def flagged(self) -> bool:
        return (
            self.transfers >= MIN_EDGE_TRANSFERS
            and self.identity > 0.0
            and self.siphon_score >= SIPHON_FLAG
        )


def edge_signals(
    records: Sequence[TransferRecord], now: float, aggregates: UserAggregates | None
) -> list[EdgeSignals]:
    """Signals for every payee one user first paid within the new-payee window."""
    history = sorted((r for r in records if r.at <= now), key=lambda r: r.at)
    if len({r.uid for r in history}) > 1:
        raise ValueError("edge signals are computed for one user at a time")

    reference = max(aggregates.amount_p95 if aggregates is not None else 0.0, AMOUNT_FLOOR)
    by_payee: dict[str, list[TransferRecord]] = defaultdict(list)
    for record in history:
        by_payee[record.payee_id].append(record)

    signals = []
    for payee_id, sent in sorted(by_payee.items()):
        first_seen = sent[0].at
        if now - first_seen > NEW_PAYEE_WINDOW_DAYS * SECONDS_PER_DAY:
            continue
        counted = len(sent) >= MIN_EDGE_TRANSFERS

        interval_cv, regularity = None, 0.0
        gaps = [later.at - earlier.at for earlier, later in zip(sent, sent[1:], strict=False)]
        if counted and _mean(gaps) > 0:
            mean = _mean(gaps)
            interval_cv = math.sqrt(sum((gap - mean) ** 2 for gap in gaps) / len(gaps)) / mean
            regularity = _clamp((REGULAR_CV - interval_cv) / REGULAR_CV)

        low, high = BAND
        in_band = sum(low * reference <= r.amount < high * reference for r in sent)
        band_fraction = in_band / len(sent)

        on_edge = [r.identity_score for r in sent if r.identity_score is not None]
        elsewhere = [
            r.identity_score
            for r in history
            if r.payee_id != payee_id and r.identity_score is not None
        ]
        identity_gap, identity = None, 0.0
        if len(on_edge) >= MIN_IDENTITY_SAMPLES and len(elsewhere) >= MIN_IDENTITY_SAMPLES:
            baseline = max(_mean(elsewhere), 1e-6)
            identity_gap = (_mean(on_edge) - baseline) / baseline
            identity = _clamp(
                (identity_gap - IDENTITY_GAP_FLOOR)
                / (IDENTITY_GAP_SATURATION - IDENTITY_GAP_FLOOR)
            )

        cumulative = sum(r.amount for r in sent)
        signals.append(
            EdgeSignals(
                uid=sent[0].uid,
                payee_id=payee_id,
                first_seen=first_seen,
                transfers=len(sent),
                cumulative=cumulative,
                interval_cv=interval_cv,
                band_fraction=band_fraction,
                identity_gap=identity_gap,
                volume=_clamp(cumulative / (VOLUME_SATURATION * reference)),
                regularity=regularity,
                band=band_fraction if counted else 0.0,
                identity=identity,
            )
        )
    return signals


@dataclass(frozen=True)
class PayeeSignals:
    payee_id: str
    distinct_senders_7d: int
    distinct_senders_30d: int
    new_senders_30d: int
    edge_risk: float
    flagged_edges: int

    @property
    def new_sender_fraction(self) -> float:
        if not self.distinct_senders_30d:
            return 0.0
        return self.new_senders_30d / self.distinct_senders_30d

    @property
    def fan_in(self) -> float:
        return _clamp((self.new_senders_30d - 1) / (FAN_IN_SATURATION - 1))

    @property
    def risk_score(self) -> float:
        return max(self.fan_in * self.new_sender_fraction, self.edge_risk)

    @property
    def flagged(self) -> bool:
        many_new = (
            self.new_senders_30d >= MIN_FLAG_SENDERS
            and self.new_sender_fraction >= NEW_SENDER_MAJORITY
        )
        return self.flagged_edges > 0 or many_new


def payee_signals(
    records: Sequence[TransferRecord], now: float, edges: Sequence[EdgeSignals]
) -> list[PayeeSignals]:
    """Destination risk for every payee paid within the last 30 days, across all users."""
    first_paid: dict[tuple[str, str], float] = {}
    for record in records:
        if record.at <= now:
            key = (record.payee_id, record.uid)
            first_paid[key] = min(first_paid.get(key, record.at), record.at)

    senders_30d: dict[str, set[str]] = defaultdict(set)
    senders_7d: dict[str, set[str]] = defaultdict(set)
    for record in records:
        if _in_window(record.at, now, AGGREGATE_WINDOW_DAYS):
            senders_30d[record.payee_id].add(record.uid)
        if _in_window(record.at, now, RECENT_SENDER_DAYS):
            senders_7d[record.payee_id].add(record.uid)

    edge_scores: dict[str, list[EdgeSignals]] = defaultdict(list)
    for edge in edges:
        edge_scores[edge.payee_id].append(edge)

    signals = []
    for payee_id, senders in sorted(senders_30d.items()):
        new = [
            uid
            for uid in senders
            if now - first_paid[(payee_id, uid)] <= NEW_PAYEE_WINDOW_DAYS * SECONDS_PER_DAY
        ]
        payee_edges = edge_scores.get(payee_id, [])
        signals.append(
            PayeeSignals(
                payee_id=payee_id,
                distinct_senders_7d=len(senders_7d.get(payee_id, ())),
                distinct_senders_30d=len(senders),
                new_senders_30d=len(new),
                edge_risk=max((edge.siphon_score for edge in payee_edges), default=0.0),
                flagged_edges=sum(edge.flagged for edge in payee_edges),
            )
        )
    return signals


@dataclass(frozen=True)
class BatchResult:
    aggregates: Mapping[str, UserAggregates]
    edges: tuple[EdgeSignals, ...]
    payees: tuple[PayeeSignals, ...]


def compute(records: Sequence[TransferRecord], now: float) -> BatchResult:
    """Everything the aggregator writes, from the transfers released in the lookback window."""
    usable = [r for r in records if _in_window(r.at, now, LOOKBACK_DAYS)]
    by_user: dict[str, list[TransferRecord]] = defaultdict(list)
    for record in usable:
        by_user[record.uid].append(record)

    aggregates: dict[str, UserAggregates] = {}
    edges: list[EdgeSignals] = []
    for uid, owned in sorted(by_user.items()):
        user = user_aggregates(owned, now)
        if user is not None:
            aggregates[uid] = user
        edges += edge_signals(owned, now, user)

    return BatchResult(aggregates, tuple(edges), tuple(payee_signals(usable, now, edges)))
