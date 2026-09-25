import unittest
from datetime import datetime, timezone
from abi_autopilot.availability import classify_provider_failure, parse_retry_after_seconds

class AvailabilityTests(unittest.TestCase):
    def test_quota(self):
        s=classify_provider_failure("You've hit your usage limit. Resets in 2 hours 15 minutes")
        self.assertEqual(s.kind,"quota")
        self.assertEqual(s.retry_after_seconds,8100)

    def test_rate_limit(self):
        self.assertEqual(classify_provider_failure("429 too many requests").kind,"transient")

    def test_clock_reset(self):
        now=datetime(2026,9,8,10,0,tzinfo=timezone.utc)
        self.assertEqual(parse_retry_after_seconds("resets at 11:30 AM",now),5400)
