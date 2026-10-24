"""Regression test: recent question/answer context survives history compaction."""
import unittest
from unittest.mock import patch

import app


class ConversationCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_recent_user_turns_and_referents_survive_compaction(self):
        messages = [{"role": "system", "content": "system prompt"}]
        for i in range(8):
            messages.append({"role": "user", "content": f"Older request {i}. " + ("detail " * 500)})
            messages.append({"role": "assistant", "content": f"Answer {i}. " + ("context " * 500)})
        messages.append({"role": "user", "content": "Which theme did I choose?"})
        messages.append({"role": "assistant", "content": "I asked you to choose a theme."})
        messages.append({"role": "user", "content": "The dark blue and silver one."})
        messages.append({"role": "assistant", "content": "Got it, dark blue and silver."})
        original_recent = [m.copy() for m in messages[-12:]]

        async def fake_stream(_messages, _tools):
            yield "message", {"role": "assistant", "content": "Older history summary: prior requests and decisions."}

        with patch.object(app.llm, "stream", fake_stream):
            await app.compact(messages)

        self.assertIn("[Conversation continuity summary", messages[1]["content"])
        self.assertTrue(any(m.get("content") == "Which theme did I choose?" for m in messages))
        self.assertTrue(any(m.get("content") == "The dark blue and silver one." for m in messages))
        self.assertTrue(any(m.get("content") == "Got it, dark blue and silver." for m in messages))
        self.assertGreaterEqual(sum(1 for m in messages if m.get("role") == "user"), 6)


if __name__ == "__main__":
    unittest.main()
