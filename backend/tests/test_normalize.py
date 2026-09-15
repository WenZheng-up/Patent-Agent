# -*- coding: utf-8 -*-
"""normalize 单测：全部用探针实测的脏数据形态构造。"""
import unittest

from app.providers import normalize


class TestCleanIPC(unittest.TestCase):
    def test_observed_electric(self):
        # 探针实测：HH01H01M10/613 -> H01M 10/613
        raw = {"l1": "H", "l2": "HH01", "l3": "HH01H01M", "l4": "HH01H01M10/613"}
        self.assertEqual(normalize.clean_ipc(raw), "H01M 10/613")

    def test_observed_mechanical(self):
        # API 文档样本：BB25B25J9/16 -> B25J 9/16
        raw = {"l1": "B", "l2": "BB25", "l3": "BB25B25J", "l4": "BB25B25J9/16"}
        self.assertEqual(normalize.clean_ipc(raw), "B25J 9/16")

    def test_single_prefix_format(self):
        # 实测混存的单前缀格式：B60L58/26 -> B60L 58/26
        raw = {"l1": "B", "l2": "B60", "l3": "B60L", "l4": "B60L58/26"}
        self.assertEqual(normalize.clean_ipc(raw), "B60L 58/26")

    def test_fallback(self):
        self.assertEqual(normalize.clean_ipc({"l4": "H01M10/613"}), "H01M10/613")
        self.assertIsNone(normalize.clean_ipc({}))


class TestSplitDescription(unittest.TestCase):
    def test_normal_49_paragraphs(self):
        raws = ["技术领域", "本发明属于锂电池散热领域。", "背景技术",
                "锂离子电池能量密度高。", "目前常见 BTMS 包括风冷。"]
        out = normalize.split_description(raws)
        # 两个纯章节标题段被吸收为 section 标记，不占输出序号
        self.assertEqual([p["index"] for p in out], [0, 1, 2])
        self.assertEqual(out[0]["section"], "技术领域")
        self.assertEqual(out[1]["section"], "背景技术")
        self.assertEqual(out[2]["section"], "背景技术")

    def test_mashed_single_paragraph(self):
        # 探针实测：10013 字整篇挤压成 1 段，章节与正文用空格粘连
        blob = ("技术领域 本发明涉及电池模组散热。 背景技术 现有液冷温差异。"
                "发明内容 本发明提供相变储能液冷板。 具体实施方式 实施例一如下。"
                "传感器采集温度。 实施例二采用微通道。")
        out = normalize.split_description([blob])
        self.assertGreaterEqual(len(out), 4)
        sections = {p["section"] for p in out}
        self.assertIn("背景技术", sections)
        self.assertIn("具体实施方式", sections)

    def test_bracket_numbered(self):
        blob = "[0001] 本发明涉及散热。[0002] 背景如下。[0003] 方案为 PCM。"
        out = normalize.split_description([blob])
        self.assertEqual(len(out), 3)
        self.assertTrue(all(p["text"].startswith("[") for p in out))

    def test_empty(self):
        self.assertEqual(normalize.split_description([]), [])


class TestFamilyMerge(unittest.TestCase):
    def _raw(self, aminer_id, kind, desc, claims, assignee):
        return {
            "id": aminer_id, "country": "cn",
            "app_num": "201811132449", "pub_num": "109449528", "pub_kind": kind,
            "title": {"zh": ["一种相变储能液冷板、电池包主动热管理系统及控制方法"]},
            "app_date": {"seconds": 1537977600}, "pub_date": {"seconds": 1551974400},
            "inventor": [{"name": "张三"}],
            "assignee": [{"name": assignee}] if assignee else None,
            "abstract": {"zh": ["摘要"]},
            "description": {"zh": desc},
            "claims": {"zh": claims},
            "ipcr": [{"l1": "H", "l2": "HH01", "l3": "HH01H01M",
                      "l4": "HH01H01M10/613"}] if desc else [],
        }

    def test_ab_versions_merge(self):
        # 探针实测：同申请号 A/B 双 id，B 授权版全文/权项/IPC 全空；申请人中英不一致
        rich = self._raw("id-a", "A",
                         ["技术领域", "正文一。", "背景技术", "正文二。",
                          "发明内容", "正文三。", "正文四。"],
                         [], "UNIV JIANGSU")
        empty_b = self._raw("id-b", "B", [], [], "江苏大学")
        merged = normalize.merge_family([rich, empty_b])
        self.assertEqual(len(merged), 1)
        p = merged[0]
        self.assertEqual(p["kinds"], ["A", "B"])
        self.assertEqual(set(p["aminer_ids"]), {"id-a", "id-b"})
        self.assertGreaterEqual(len(p["paragraphs"]), 4)          # 取 A 的全文
        self.assertEqual(p["ipcs"], ["H01M 10/613"])
        self.assertEqual(set(p["assignees"]), {"江苏大学", "UNIV JIANGSU"})
        self.assertFalse(p["data_completeness"]["has_claims"])    # 权项缺失要被标记
        self.assertEqual(p["app_date"], "2018-09-26")

    def test_distinct_applications_not_merged(self):
        a = self._raw("id-a", "A", ["x"] * 4, ["c"], "X")
        a["app_num"] = "111"
        b = self._raw("id-b", "A", ["y"] * 4, ["c"], "Y")
        b["app_num"] = "222"
        self.assertEqual(len(normalize.merge_family([a, b])), 2)


class TestLocateEvidence(unittest.TestCase):
    def test_locate(self):
        patent = {"paragraphs": normalize.split_description(
            ["技术领域", "一种温差控制方法。", "背景技术", "快充时温差超过八摄氏度。"])}
        loc = normalize.locate_evidence(patent, "温差超过八摄氏度")
        self.assertIsNotNone(loc)
        self.assertEqual(loc["section"], "§背景技术")
        self.assertEqual(loc["paragraph_index"], 1)


if __name__ == "__main__":
    unittest.main()
