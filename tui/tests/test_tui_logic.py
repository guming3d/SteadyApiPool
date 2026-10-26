"""The rules ported from the web UI (pool-modes.ts, pool-metrics.ts, api-test.ts)."""

from __future__ import annotations

from pool_tui.logic import (
    DEFAULT_BREAKER,
    DEPLOYMENT_NAME_PATTERN,
    Member,
    append_members,
    bar,
    capacity_limit,
    clamp_capacity,
    compact,
    default_version,
    deployment_mode,
    diagnose,
    display_user,
    draft_issues,
    finished_state,
    follows_pool,
    members_for_mode,
    members_to_update,
    ms,
    percent,
    pool_breaker,
    read_only_members,
    remove_member,
    role,
    set_primary,
    suggest_api_path,
    suggest_deployment_name,
    suggest_pool_id,
    throttle_tone,
    traffic_shares,
)


def members(*spec: tuple[str, int, int]) -> list[Member]:
    return [Member(*item) for item in spec]


def test_modes_follow_priority_and_weight() -> None:
    assert deployment_mode(members(("a", 0, 1), ("b", 1, 1))) == "failover"
    assert deployment_mode(members(("a", 0, 1), ("b", 0, 1))) == "balanced"
    assert deployment_mode(members(("a", 0, 3), ("b", 0, 1))) == "weighted"
    assert deployment_mode(members(("a", 0, 1), ("b", 1, 1), ("c", 2, 1))) == "custom"


def test_switching_modes_keeps_the_primary() -> None:
    pool = members(("a", 0, 1), ("b", 0, 1), ("c", 0, 1))
    assert [m.priority for m in members_for_mode(pool, "failover")] == [0, 1, 1]
    assert [m.weight for m in members_for_mode(pool, "weighted")] == [2, 1, 1]
    custom = members(("a", 0, 5), ("b", 0, 3))
    assert [m.weight for m in members_for_mode(custom, "weighted")] == [5, 3]


def test_adding_removing_and_promoting() -> None:
    pool, added = append_members([], ["a", "b", "A"])
    assert added == ["a", "b"]
    assert [(m.backend_id, m.priority) for m in pool] == [("a", 0), ("b", 1)]
    # Removing the failover primary promotes the next member.
    assert [
        (m.backend_id, m.priority)
        for m in remove_member(pool + members(("c", 1, 1)), "a", "failover")
    ] == [
        ("b", 0),
        ("c", 1),
    ]
    promoted = set_primary(pool, "b", "failover")
    assert [(m.backend_id, m.priority) for m in promoted] == [("b", 0), ("a", 1)]
    assert role(promoted, 0) == "primary" and role(promoted, 1) == "standby"


def test_shares_and_validation() -> None:
    assert traffic_shares(members(("a", 0, 3), ("b", 0, 1), ("c", 1, 1))) == [0.75, 0.25, 0.0]
    assert draft_issues(members(("a", 0, 1)), "failover") == ["a pool needs 2 to 30 members"]
    assert draft_issues(members(("a", 0, 1), ("b", 0, 1)), "weighted") == [
        "weighted mode needs members with different weights"
    ]


def test_suggestions() -> None:
    assert suggest_api_path("GPT 4o (prod)", "pool-gpt-4o", "pool-") == "gpt-4o-prod"
    assert suggest_api_path(None, "pool-gpt-4o", "pool-") == "gpt-4o"
    assert suggest_pool_id("gpt-4.1-mini", "pool-") == "pool-gpt-4-1-mini"


def test_formatting_matches_the_web_ui() -> None:
    assert compact(1284) == "1,284"
    assert compact(12_900) == "12.9K"
    assert compact(None) == "—"
    assert ms(820.4) == "820 ms" and ms(13153) == "13.2 s"
    assert percent(0.99952, 2) == "99.95%"
    assert percent(0.99999, 2) == "99.99%"
    assert percent(1.0, 2) == "100%"
    assert bar(0.75) == "████████░░"
    assert throttle_tone(395, 735) == "bad"


def test_diagnosis() -> None:
    assert diagnose(200, "{}") == "ok"
    assert diagnose(401, '{"message": "Access denied due to invalid subscription key."}') == "key"
    assert diagnose(401, '{"error": {"code": "PermissionDenied"}}') == "member_access"
    assert diagnose(429, "") == "throttled"
    assert diagnose(504, "") == "timeout"
    assert diagnose(418, "") == "other"


def test_sparkline_scales_from_zero_and_fits_its_width() -> None:
    from pool_tui.logic import spark

    # A dip from 50 to 40 stays high: the scale starts at zero, not at the minimum.
    assert spark([50, 40, 50], 10) == "█▇█"
    assert spark([0, 10], 10) == " █"
    assert len(spark([float(v) for v in range(168)], 40)) == 40
    assert spark([], 10) == "" and spark([0, 0], 5) == "  "


def test_display_user_matches_the_web_ui() -> None:
    local = {"name": "local-developer", "can_write": True, "authenticated": False}
    azure = {"name": "dev@contoso.com", "object_id": "oid-1", "tenant_id": "tid-1"}
    signed_in = {"name": "ops@contoso.com", "can_write": False, "authenticated": True}
    assert display_user(signed_in, azure) == "ops@contoso.com"
    guest = {"name": "guest", "can_write": False, "authenticated": False, "guest": True}
    assert display_user(guest, azure) == "guest"
    assert display_user(local, azure) == "dev@contoso.com"
    assert display_user(local, {"name": None, "object_id": "oid-2", "tenant_id": None}) == "oid-2"
    assert display_user(local, None) is None


def _backend(backend_id: str, rule: dict[str, object] | None, **extra: object) -> dict[str, object]:
    return {"id": backend_id, "circuit_breaker": rule, "managed": True, "protected": False, **extra}


def test_pool_breaker_matches_the_web_ui() -> None:
    tight = {**DEFAULT_BREAKER, "failure_count": 5, "trip_duration_seconds": 120}
    backends = {
        "a": _backend("a", tight),
        "b": _backend("b", tight),
        "c": _backend("c", DEFAULT_BREAKER),
        "x": _backend("x", DEFAULT_BREAKER, managed=False),
        "n": _backend("n", None),
    }
    assert follows_pool(backends["a"]) and not follows_pool(backends["x"])
    assert not follows_pool(_backend("t", tight, protected=True)) and not follows_pool(None)
    assert pool_breaker(["a", "b", "c", "x"], backends) == (tight, False)
    # External members don't vote, and a tie goes to the primary.
    assert pool_breaker(["c", "a", "x"], backends) == (DEFAULT_BREAKER, False)
    assert pool_breaker(["a", "b"], backends) == (tight, True)
    assert pool_breaker(["n"], backends) == (DEFAULT_BREAKER, False)
    assert pool_breaker([], {}) == (DEFAULT_BREAKER, True)
    assert members_to_update(["a", "c", "n", "x", "gone"], backends, tight) == ["c", "n"]
    assert read_only_members(["a", "x", "gone"], backends) == ["x"]


def test_deploy_helpers_match_the_web_ui() -> None:
    models = [
        {"name": "gpt-4o", "version": "2024-11-20", "is_default_version": True},
        {"name": "gpt-4o", "version": "2024-08-06", "is_default_version": False},
        {"name": "gpt-5", "version": "2026-01-01", "is_default_version": False},
    ]
    assert (default_version(models, "gpt-4o") or {})["version"] == "2024-11-20"
    assert (default_version(models, "gpt-5") or {})["version"] == "2026-01-01"
    assert default_version(models, "missing") is None
    assert suggest_deployment_name("gpt-4o", []) == "gpt-4o"
    assert suggest_deployment_name("gpt-4o", ["GPT-4o", "gpt-4o-2"]) == "gpt-4o-3"
    long = suggest_deployment_name("m" * 80, ["m" * 64])
    assert len(long) == 64 and long.endswith("-2")
    assert DEPLOYMENT_NAME_PATTERN.fullmatch(suggest_deployment_name("o3 mini/preview", []))
    sku = {"default_capacity": 50, "min_capacity": 1, "max_capacity": 1000, "step": 1}
    assert capacity_limit(sku, None) == 1000 and capacity_limit(sku, 450) == 450
    assert clamp_capacity(5000, sku, 450) == 450
    assert clamp_capacity(0, sku) == 1
    assert clamp_capacity(float("nan"), sku) == 50
    assert clamp_capacity(26, {**sku, "step": 10}) == 30
    assert clamp_capacity(50, sku, 0) == 1
    assert finished_state("Succeeded") and finished_state("failed")
    assert not finished_state("Creating")
