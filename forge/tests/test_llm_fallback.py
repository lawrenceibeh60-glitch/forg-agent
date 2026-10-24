"""Regression tests for opt-in model failover."""
import unittest
from unittest.mock import patch

import httpx
import llm


class ModelFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_falls_back_only_before_any_output(self):
        primary = {"id": "primary", "name": "primary", "kind": "openai", "base_url": "https://primary.invalid"}
        fallback = {"id": "env-fallback", "name": "fallback", "kind": "openai", "base_url": "https://fallback.invalid"}
        seen = []

        async def fake_openai(provider, messages, tools):
            seen.append(provider["id"])
            if provider["id"] == "primary":
                raise RuntimeError("primary HTTP 401")
            yield "text", "fallback answer"
            yield "message", {"role": "assistant", "content": "fallback answer"}

        with patch.object(llm, "active", return_value=primary), \
             patch.object(llm, "fallback_provider", return_value=fallback), \
             patch.object(llm, "_openai", fake_openai):
            output = [item async for item in llm.stream([{"role": "user", "content": "hello"}], [])]

        self.assertEqual(seen, ["primary", "env-fallback"])
        self.assertEqual(output[0], ("text", "fallback answer"))

    async def test_network_failures_retry_then_use_fallback(self):
        primary = {"id": "primary", "name": "primary", "kind": "openai", "base_url": "https://primary.invalid"}
        fallback = {"id": "env-fallback", "name": "fallback", "kind": "openai", "base_url": "https://fallback.invalid"}
        seen = []

        async def no_wait(_seconds):
            return None

        async def fake_openai(provider, messages, tools):
            seen.append(provider["id"])
            if provider["id"] == "primary":
                raise httpx.ConnectError("network unavailable")
            yield "text", "recovered via fallback"
            yield "message", {"role": "assistant", "content": "recovered via fallback"}

        with patch.object(llm, "active", return_value=primary), \
             patch.object(llm, "fallback_provider", return_value=fallback), \
             patch.object(llm, "asyncio") as asyncio_mock, \
             patch.object(llm, "_openai", fake_openai):
            asyncio_mock.sleep = no_wait
            output = [item async for item in llm.stream([{"role": "user", "content": "hello"}], [])]

        self.assertEqual(seen, ["primary", "primary", "primary", "env-fallback"])
        self.assertEqual(output[0], ("text", "recovered via fallback"))

    async def test_does_not_mix_fallback_after_partial_primary_output(self):
        primary = {"id": "primary", "name": "primary", "kind": "openai", "base_url": "https://primary.invalid"}
        fallback = {"id": "env-fallback", "name": "fallback", "kind": "openai", "base_url": "https://fallback.invalid"}
        seen = []

        async def fake_openai(provider, messages, tools):
            seen.append(provider["id"])
            if provider["id"] == "primary":
                yield "text", "partial"
                raise RuntimeError("connection dropped after output")
            yield "text", "must-not-appear"

        with patch.object(llm, "active", return_value=primary), \
             patch.object(llm, "fallback_provider", return_value=fallback), \
             patch.object(llm, "_openai", fake_openai):
            with self.assertRaisesRegex(RuntimeError, "after output"):
                _ = [item async for item in llm.stream([{"role": "user", "content": "hello"}], [])]

        self.assertEqual(seen, ["primary"])


if __name__ == "__main__":
    unittest.main()


class ModelRoutingTests(unittest.TestCase):
    def test_profile_requires_all_three_values(self):
        with patch.dict("os.environ", {"FORGE_REVIEW_API_KEY":"k", "FORGE_REVIEW_MODEL":"m"}, clear=True), \
             patch.object(llm, "active", return_value={"id":"primary"}):
            self.assertEqual(llm.routed_provider("review")["id"], "primary")

    def test_profile_selects_configured_route_without_exposing_secrets(self):
        env = {"FORGE_REVIEW_API_KEY":"secret-key", "FORGE_REVIEW_MODEL":"review-model",
               "FORGE_REVIEW_BASE_URL":"https://review.example/v1", "FORGE_REVIEW_KIND":"openai"}
        with patch.dict("os.environ", env, clear=True):
            provider = llm.routed_provider("review")
        self.assertEqual(provider["id"], "env-review")
        self.assertEqual(provider["model"], "review-model")
        self.assertEqual(provider["base_url"], "https://review.example/v1")
        self.assertNotIn("secret-key", repr({k:v for k,v in provider.items() if k not in {"key"}}))

    def test_invalid_profile_kind_is_rejected(self):
        env = {"FORGE_REVIEW_API_KEY":"secret-key", "FORGE_REVIEW_MODEL":"review-model",
               "FORGE_REVIEW_BASE_URL":"https://review.example/v1", "FORGE_REVIEW_KIND":"unknown"}
        with patch.dict("os.environ", env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "KIND"):
                llm.routed_provider("review")
