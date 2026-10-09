from __future__ import annotations

import unittest

from backend.llm.paper_analysis import DeepSeekRequestError, _decode_response_json


class DeepSeekResponseJsonTests(unittest.TestCase):
    def test_accepts_dict_and_plain_json_object(self) -> None:
        value = {"headline": "Radar", "papers_to_read": []}
        self.assertIs(_decode_response_json(value), value)
        self.assertEqual(_decode_response_json('{"headline":"Radar"}'), {"headline": "Radar"})

    def test_accepts_prose_around_markdown_json_fence(self) -> None:
        content = "Here is the requested object:\n```json\n{\"headline\":\"Radar\",\"papers_to_read\":[]}\n```\nDone."
        self.assertEqual(
            _decode_response_json(content),
            {"headline": "Radar", "papers_to_read": []},
        )

    def test_accepts_one_json_object_with_short_leading_and_trailing_text(self) -> None:
        content = 'Result follows: {"headline":"Radar","caveats":[]} End of result.'
        self.assertEqual(
            _decode_response_json(content),
            {"headline": "Radar", "caveats": []},
        )

    def test_rejects_invalid_truncated_and_ambiguous_content(self) -> None:
        invalid_values = [
            "not JSON",
            'Result: {"headline":"truncated"',
            '```json\n{"headline":"missing fence"}',
            '{"first":1} and {"second":2}',
            '[{"headline":"array wrapper"}]',
        ]
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(DeepSeekRequestError):
                    _decode_response_json(value)


if __name__ == "__main__":
    unittest.main()
