from __future__ import annotations

import unittest

from iot_ops_agent.web.auth import RedisSecurityStore


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.operations = []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self.operations.append((name, args, kwargs))
            return self

        return queue

    def execute(self):
        results = []
        for name, args, kwargs in self.operations:
            results.append(getattr(self.redis, name)(*args, **kwargs))
        return results


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.sets = {}
        self.expirations = {}

    def pipeline(self):
        return FakePipeline(self)

    def set(self, key, value, ex=None):
        self.values[key] = value
        if ex is not None:
            self.expirations[key] = ex
        return True

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        self.values.pop(key, None)
        self.sets.pop(key, None)
        return 1

    def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)
        return 1

    def srem(self, key, value):
        self.sets.setdefault(key, set()).discard(value)
        return 1

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def expire(self, key, seconds):
        self.expirations[key] = seconds
        return True

    def incr(self, key):
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]

    def ping(self):
        return True


class RedisSecurityStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.redis = FakeRedis()
        self.store = RedisSecurityStore.__new__(RedisSecurityStore)
        self.store.redis = self.redis

    def test_session_lifecycle_and_user_revocation(self) -> None:
        first_id, first = self.store.create_session("user-1", 2, 60)
        second_id, _ = self.store.create_session("user-1", 2, 60)
        loaded = self.store.get_session(first_id, 120)
        self.assertEqual(loaded, first)
        self.assertEqual(self.redis.expirations[self.store._session_key(first_id)], 120)

        self.store.delete_session(first_id)
        self.assertIsNone(self.store.get_session(first_id, 60))
        self.assertNotIn(first_id, self.redis.smembers(self.store._user_sessions_key("user-1")))

        self.store.revoke_user_sessions("user-1")
        self.assertIsNone(self.store.get_session(second_id, 60))
        self.assertEqual(self.redis.smembers(self.store._user_sessions_key("user-1")), set())

    def test_invalid_session_payload_is_deleted(self) -> None:
        self.redis.set(self.store._session_key("broken"), "not-json")
        self.assertIsNone(self.store.get_session("broken", 60))
        self.assertIsNone(self.redis.get(self.store._session_key("broken")))

        self.redis.set(self.store._session_key("delete-broken"), "[]")
        self.store.delete_session("delete-broken")
        self.assertIsNone(self.redis.get(self.store._session_key("delete-broken")))

    def test_rate_limit_and_ping(self) -> None:
        self.assertTrue(self.store.hit_rate_limit("login", 2, 60))
        self.assertTrue(self.store.hit_rate_limit("login", 2, 60))
        self.assertFalse(self.store.hit_rate_limit("login", 2, 60))
        self.assertTrue(self.store.ping())


if __name__ == "__main__":
    unittest.main()
