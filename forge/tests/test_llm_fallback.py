"""Regression tests for opt-in model failover."""
import unittest
from unittest.mock import patch

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
