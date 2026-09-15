# -*- coding: utf-8
"""query_compiler 单测：用探针实测的 mock 检索式与语法结论。"""
import unittest

from app.retrieval.query_compiler import (
    QueryCompileError, compile_groups, parse_boolean, sanitize_term,
)

MOCK_EXPR = ("(相变材料 OR PCM OR 相变储热) AND (液冷板 OR 液冷) "
             "AND (动力电池 OR 电池包 OR 储能电池) AND 热管理")


class TestSanitize(unittest.TestCase):
    def test_strip_unsupported_syntax(self):
        # 探针结论：括号导致 0 条、OR/AND 被当噪声，必须剔除
        self.assertEqual(sanitize_term('"复合相变材料"'), "复合相变材料")
        self.assertEqual(sanitize_term("(液冷板)"), "液冷板")
        self.assertEqual(sanitize_term("PCM OR"), "PCM")
        self.assertEqual(sanitize_term("AND OR NOT"), "")


class TestParseBoolean(unittest.TestCase):
    def test_mock_expression_cnf(self):
        groups = parse_boolean(MOCK_EXPR)
        self.assertEqual(len(groups), 4)
        self.assertEqual(groups[0], ["相变材料", "PCM", "相变储热"])
        self.assertEqual(groups[1], ["液冷板", "液冷"])
        self.assertEqual(groups[2], ["动力电池", "电池包", "储能电池"])
        self.assertEqual(groups[3], ["热管理"])

    def test_plain_wordbag(self):
        groups = parse_boolean("相变材料 液冷板 热管理")
        self.assertEqual(groups, [["相变材料 液冷板 热管理"]])


class TestCompile(unittest.TestCase):
    def test_cartesian_product_18(self):
        # 探针实测：该 CNF 恰好展开 18 个子查询
        subqueries, meta = compile_groups(parse_boolean(MOCK_EXPR))
        self.assertEqual(len(subqueries), 18)
        self.assertFalse(meta["truncated"])
        self.assertEqual(subqueries[0], "相变材料 液冷板 动力电池 热管理")
        self.assertIn("PCM 液冷 储能电池 热管理", subqueries)
        # 子查询内绝不允许出现括号或 OR/AND
        for sq in subqueries:
            self.assertNotIn("(", sq)
            self.assertNotIn("OR", sq)
            self.assertNotIn("AND", sq)

    def test_truncation(self):
        groups = [["a", "a2", "a3", "a4", "a5", "a6", "a7"],
                  ["b", "b2", "b3", "b4", "b5", "b6"]]
        subqueries, meta = compile_groups(groups, max_subqueries=36)
        self.assertLessEqual(len(subqueries), 36)
        self.assertTrue(meta["truncated"])
        self.assertTrue(meta["dropped_terms"])

    def test_empty_raises(self):
        with self.assertRaises(QueryCompileError):
            compile_groups([["("], []])


if __name__ == "__main__":
    unittest.main()
