"""plan_summary() turns /v1/key/info into the audit-trail fields and decides
paid-vs-free from the credit limit, not a (sometimes-empty) plan-name string."""
from agent.cmc.client import CMCClient


def _client_with(info):
    c = CMCClient("dummy-key")
    c.key_info = lambda: info  # type: ignore[assignment]
    return c


def test_basic_free_tier_detected():
    c = _client_with({
        "plan": {"name": "Basic", "credit_limit_monthly": 10000,
                 "credit_limit_daily": None, "rate_limit_minute": 30},
        "usage": {"current_month": {"credits_left": 9500}},
    })
    s = c.plan_summary()
    assert s["is_paid"] is False
    assert s["credits_monthly"] == 10000
    assert s["credits_left"] == 9500


def test_pro_tier_detected_by_credit_limit():
    c = _client_with({
        "plan": {"name": "", "credit_limit_monthly": 333333,
                 "credit_limit_daily": 11111, "rate_limit_minute": 60},
        "usage": {"current_month": {"credits_left": 333000}},
    })
    s = c.plan_summary()
    assert s["is_paid"] is True
    # Falls back to "paid" when CMC leaves the plan name blank.
    assert s["tier"] == "paid"
    assert s["credits_daily"] == 11111


def test_paid_by_daily_allowance_even_if_monthly_small():
    # Any non-null daily allowance is an upgrade signal on its own.
    c = _client_with({"plan": {"credit_limit_monthly": 10000,
                               "credit_limit_daily": 500}})
    assert c.plan_summary()["is_paid"] is True


def test_missing_fields_do_not_crash():
    s = _client_with({}).plan_summary()
    assert s["is_paid"] is False
    assert s["credits_monthly"] is None
    assert s["tier"] == "Basic (free)"
