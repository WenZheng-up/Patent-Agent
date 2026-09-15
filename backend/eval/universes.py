# -*- coding: utf-8 -*-
"""
M7 合成宇宙（两处用，明确标注 synthetic）：

用途：在没有真实 AMiner/LLM 凭证时，端到端验证 录制→回放→指标→消融→柱状图 全管线。
边界：全部专利/交底/标签均为人工构造的最小自洽宇宙，**只验证机制，不产出任何
"多 agent 有没有用"的统计结论**（run_eval 报告与图表均带合成数据水印）。

宇宙设计（刻意覆盖 critic 价值场景）：
- SYN-BATT-001：P1 实为 Y（热失控联动仅见背景技术），reader 判 X，critic 规则外
  内容审查降 X→Y —— E3/E7 相对 E1/E2 的分级修正收益来源；
- SYN-VAC-002：干净 X/Y/A 各一，critic 全票通过 —— 对照组，证明不是每件都被改判。

每个宇宙提供两个与真实依赖同形的后端：
- SyntheticAminer：search/info/detail（AND 词袋 + 显式排序权重）
- SyntheticLLM：chat_json/chat_text（按 system 角色确定性出 reader/critic/coverage/planner）
"""
import json
import re

# ---------------- 原始专利构造 ----------------

def _pat(pid, pub, app, title, *, year, seconds, ipc_l4, inventor, assignee,
         abstract, paras, claims):
    """paras: [(section, text), ...]（>=4 段，normalize 直接采用并赋章节）。"""
    l1, l2, l3 = ipc_l4[0], ipc_l4[:3], ipc_l4[:4]
    return {
        "id": pid, "country": "CN", "app_num": app, "pub_num": pub, "pub_kind": "A",
        "title": {"zh": [title]},
        "abstract": {"zh": [abstract]},
        "description": {"zh": [f"{sec} {text}" if sec else text for sec, text in paras]},
        "claims": {"zh": claims},
        "ipcr": [{"l1": l1, "l2": l2, "l3": l3, "l4": ipc_l4}],
        "inventor": [{"name": inventor}], "assignee": [{"name": assignee}],
        "pub_date": {"seconds": seconds},
        "_year": year, "_title": title, "_weight": 0,
        "_blob": " ".join([title, abstract] + claims
                          + [t for _, t in paras]),
    }


# =====================================================================
# 宇宙 1：电池热管理（H01M）
# =====================================================================

_B1_PARAS = [
    ("技术领域", "本发明涉及动力电池热管理技术，具体为一种相变材料与微通道液冷耦合的"
                  "电池组温控装置。"),
    ("背景技术", "现有动力电池热管理多依赖单一液冷板，快充时温差大；另有方案提到将温度"
                  "采样与冷却策略联动实现热失控预警，但结构复杂、可靠性不足。"),
    ("发明内容", "本发明以石墨烯复合相变材料包覆每一电芯，在电芯外侧构成被动均温层，"
                  "吸收快充瞬态热峰并沿模组均布。"),
    ("发明内容", "该相变均温层与贴附在模组底部的微通道液冷板耦合，微通道内冷却液带走"
                  "稳态热负荷，形成主被动结合的散热回路。"),
    ("发明内容", "模组内相邻电芯间隙布置分布式温度传感器，传感器信号线接入电池管理"
                  "单元，实时采集各区域温度。"),
    ("具体实施方式", "实施例中相变材料为石蜡/膨胀石墨复合体系，微通道液冷板采用铝制"
                    "扁管，温度传感器为 NTC 热敏电阻，沿模组长度方向等距布置八只。"),
]

_B2_PARAS = [
    ("技术领域", "本发明涉及电池热管理，具体为一种液冷与相变材料结合的电池包散热结构。"),
    ("背景技术", "单一相变材料热容有限，长期高倍率工况下热量累积无法及时移出。"),
    ("发明内容", "本发明在电芯之间填充复合相变材料形成均温层，并在电池包底部设置"
                  "微通道液冷板，相变层底面与液冷板表面导热贴合。"),
    ("发明内容", "液冷板微通道沿长度方向平行排布，进出口设置在同一侧，相变层将局部"
                  "热点拉平后由冷却液统一带走。"),
    ("具体实施方式", "实施例中每两只电芯间夹装一片相变材料板，液冷板入口设置流量"
                    "分配腔，整个装置不含电子采样部件。"),
]

_B3_PARAS = [
    ("技术领域", "本发明涉及电池组温控配件，具体为一种包裹电芯的相变材料套。"),
    ("背景技术", "电池组在高低温环境下性能衰减，需要被动保温与缓冲。"),
    ("发明内容", "本发明公开一种相变材料套，套装于圆柱电芯外部，相变材料层内外侧"
                  "分别设置绝缘膜与缓冲泡棉。"),
    ("具体实施方式", "该相变材料套仅提供被动吸热与缓冲，不涉及液冷部件及电子检测"
                    "元件，适用于小型电动工具电池包。"),
]

_UNIVERSE_BATT = {
    "case_id": "SYN-BATT-001",
    "title": "一种相变均温与微通道液冷耦合的电池热管理装置（合成案件）",
    "ipc": ["H01M 10/6566", "H01M 10/613"],
    "disclosure": {
        "problem": "单一液冷板快充温差大，被动相变长期热累积。",
        "solution": "石墨烯复合相变材料包覆电芯构成被动均温层；"
                    "相变均温层与微通道液冷板耦合形成主被动散热回路；"
                    "模组内布置分布式温度传感器；"
                    "温度采样与冷却策略联动实现热失控预警",
        "effect": "温差下降、循环寿命提升。",
        "terms": ["相变材料", "微通道液冷板", "温度传感器", "热失控预警", "动力电池"],
    },
    "groups": [["相变材料", "PCM", "相变储热"],
               ["液冷板", "微通道", "温控"],
               ["电池", "电芯", "动力电池"]],
    "patents": [
        # weight 字段注入见 build 处
        ("syn_p1", "1110001", "20241110001",
         "一种相变材料与微通道液冷耦合的电池热管理装置",
         _B1_PARAS,
         ["1. 一种电池热管理装置，其特征在于，电芯外包覆石墨烯复合相变材料均温层，"
          "模组底部设微通道液冷板，模组内布置温度传感器。"],
         "H01M10/6566", "2024", 1709251200, 100,
         {"grade": "X",
          "verdict": [("是", "对比文件以石墨烯复合相变材料包覆每一电芯，在电芯外侧"
                            "构成被动均温层，吸收快充瞬态热峰。", 2,
                       "石墨烯复合相变材料包覆每一电芯"),
                      ("是", "相变均温层与贴附在模组底部的微通道液冷板导热耦合，"
                            "形成主被动结合的散热回路。", 3,
                       "相变均温层与贴附在模组底部的微通道液冷板耦合"),
                      ("是", "模组内相邻电芯间隙布置分布式温度传感器，信号线接入"
                            "电池管理单元实时采集温度。", 4,
                       "相邻电芯间隙布置分布式温度传感器"),
                      ("是", "背景技术作为现有思路提及温度采样与冷却策略联动的"
                            "热失控预警，权利要求与实施例未记载该联动。", 1,
                       "温度采样与冷却策略联动实现热失控预警")],
          "conclusion": "该文件记载了相变均温与微通道液冷耦合结构及温度采样，"
                        "但其热失控联动仅在背景技术中作为现有思路提及，权利要求未明确记载。"}),
        ("syn_p2", "1110002", "20241110002",
         "一种相变材料与微通道液冷板结合的电池包散热结构",
         _B2_PARAS,
         ["1. 一种电池包散热结构，包括电芯间相变材料均温层和底部微通道液冷板。"],
         "H01M10/613", "2023", 1688169600, 90,
         {"grade": "Y",
          "verdict": [("是", "电芯之间填充复合相变材料形成均温层，提供被动吸热。", 2,
                       "电芯之间填充复合相变材料形成均温层"),
                      ("是", "相变层底面与微通道液冷板表面导热贴合，形成耦合散热。", 2,
                       "相变层底面与液冷板表面导热贴合"),
                      ("否", "未涉及温度采样与传感器布置。", None, None),
                      ("否", "未涉及热失控预警与冷却联动。", None, None)],
          "conclusion": "该文件公开相变层与微通道液冷板的组合，未公开温度采样与"
                        "热失控预警联动，需结合传感器检测类文件评价创造性。"}),
        ("syn_p3", "1110003", "20241110003",
         "一种电池组温控用相变材料套",
         _B3_PARAS,
         ["1. 一种相变材料套，套装于圆柱电芯外部，含相变材料层、绝缘膜与缓冲泡棉。"],
         "H01M10/625", "2022", 1648771200, 80,
         {"grade": "A",
          "verdict": [("部分", "公开一种套装电芯的被动相变材料套，与均温思路部分"
                              "相关但无主被动耦合。", 2,
                       "本发明公开一种相变材料套，套装于圆柱电芯外部"),
                      ("否", "未涉及微通道液冷板。", None, None),
                      ("否", "未涉及温度传感器。", None, None),
                      ("否", "未涉及热失控预警。", None, None)],
          "conclusion": "该文件为被动相变保温配件的背景技术，未涉及主动液冷与"
                        "电子检测，仅反映现有技术一般状况。"}),
        # ---- 噪声专利（粗检池内，不进精检 Top3）----
        ("syn_n1", "1110101", "20241110101", "一种服务器液冷机柜散热系统",
         [("技术领域", "本发明涉及数据中心散热设备。"),
          ("背景技术", "服务器机柜功率密度升高，风冷不足。"),
          ("发明内容", "本发明在机柜内布置微通道液冷板与CDU分配单元。"),
          ("具体实施方式", "液冷板贴合服务器主板，服务于数据中心温控场景。")],
         ["1. 一种服务器液冷机柜，含微通道液冷板和冷却液分配单元。"],
         "H05K7/20", "2024", 1709251200, 30, None),
        ("syn_n2", "1110102", "20241110102", "一种相变储热供暖装置",
         [("技术领域", "本发明涉及建筑供暖储能设备。"),
          ("背景技术", "夜间低谷电需要储存。"),
          ("发明内容", "本发明以相变材料储罐配合水盘管实现相变储热供暖。"),
          ("具体实施方式", "储罐内分层布置相变材料球，水盘管居中换热。")],
         ["1. 一种相变储热供暖装置，含相变材料储罐与水盘管。"],
         "F28D20/02", "2023", 1688169600, 25, None),
        ("syn_n3", "1110103", "20241110103", "一种锂电池正极材料及其制备方法",
         [("技术领域", "本发明涉及锂电池正极材料。"),
          ("背景技术", "高镍正极循环稳定性差。"),
          ("发明内容", "本发明提供一种包覆改性高镍三元正极材料。"),
          ("具体实施方式", "在正极颗粒表面包覆氧化铝薄层。")],
         ["1. 一种锂电池正极材料，包括高镍三元基体与氧化铝包覆层。"],
         "H01M4/505", "2024", 1709251200, 20, None),
        ("syn_n4", "1110104", "20241110104", "一种消费电子石墨散热片",
         [("技术领域", "本发明涉及电子设备散热。"),
          ("背景技术", "手机芯片局部热点需要均温。"),
          ("发明内容", "本发明为一种多层石墨复合散热片。"),
          ("具体实施方式", "石墨片两面贴合绝缘膜贴附于芯片屏蔽罩。")],
         ["1. 一种石墨复合散热片，含石墨层与双面绝缘膜。"],
         "H05K7/20", "2022", 1648771200, 15, None),
    ],
    "labels": {"CN1110001A": "Y", "CN1110002A": "Y", "CN1110003A": "A"},
    "critic_conflicts": [
        {"pub_no": "CN1110001A", "trigger": "热失控预警",
         "issue": "热失控预警联动仅在背景技术段出现，X 级单篇全要素不成立",
         "suggested_grade": "Y"}],
}


# =====================================================================
# 宇宙 2：气动机械手真空吸盘（B25J）
# =====================================================================

_V2_PARAS = [
    ("技术领域", "本发明涉及工业机器人末端执行器，具体为一种带检测的真空吸盘手爪。"),
    ("背景技术", "机械手搬运光滑板材时依赖真空吸附，掉件风险需要及时发现。"),
    ("发明内容", "本发明的真空吸盘经万向接头安装于机械臂末端法兰，吸盘工作面"
                  "均匀分布微孔，微孔汇至真空回路形成负压吸附区。"),
    ("发明内容", "真空回路串联负压传感器实时检测吸附状态，并在脱料时由电磁阀"
                  "破真空，同时经微孔吹气辅助快速释放工件。"),
    ("具体实施方式", "实施例中真空吸盘为丁腈橡胶波纹吸盘，万向接头允许 ±15 度"
                    "摆动，负压传感器采用压阻式，电磁阀两位三通。"),
]

_V1_PARAS = [
    ("技术领域", "本发明涉及机械手末端吸盘装置。"),
    ("背景技术", "真空吸盘抓取工件后释放缓慢，影响节拍。"),
    ("发明内容", "本发明真空吸盘经球头万向接头安装于机械臂末端，工作面分布"
                  "微孔并连通真空回路形成负压吸附。"),
    ("发明内容", "真空回路串联负压传感器，吸附不足时机器人停止抬升。"),
    ("具体实施方式", "实施例靠外部机械手移动与断气自然脱料，未设置吹气释放结构。"),
]

_V3_PARAS = [
    ("技术领域", "本发明涉及真空吸盘配件。"),
    ("背景技术", "吸盘唇部磨损影响密封。"),
    ("发明内容", "本发明公开一种带耐磨唇的真空吸盘，唇部局部加厚，依靠真空"
                  "负压吸附光滑工件表面。"),
    ("具体实施方式", "该吸盘为普通气动机械手通用配件，不附带检测与气路元件。"),
]

_UNIVERSE_VAC = {
    "case_id": "SYN-VAC-002",
    "title": "一种带负压检测与吹气脱料的真空吸盘机械手（合成案件）",
    "ipc": ["B25J 15/06", "B25J 15/00"],
    "disclosure": {
        "problem": "真空吸盘掉件无感知、脱料慢。",
        "solution": "真空吸盘经万向接头安装于机械臂末端；"
                    "吸盘工作面分布微孔并经真空回路形成负压吸附；"
                    "真空回路串联负压传感器实时检测吸附状态；"
                    "脱料时电磁阀破真空并经微孔吹气辅助释放",
        "effect": "掉件可检测、脱料节拍缩短。",
        "terms": ["真空吸盘", "机械臂", "负压传感器", "破真空", "吹气"],
    },
    "groups": [["真空吸盘", "负压吸盘"],
               ["机械臂", "机械手"],
               ["负压", "吸附", "负压传感器"]],
    "patents": [
        ("syn_q2", "2120002", "20242120002",
         "一种带负压检测与吹气脱料的真空吸盘手爪",
         _V2_PARAS,
         ["1. 一种真空吸盘机械手，吸盘经万向接头装于机械臂，工作面微孔接真空"
          "回路，回路串联负压传感器，电磁阀破真空并经微孔吹气。"],
         "B25J15/06", "2024", 1709251200, 100,
         {"grade": "X",
          "verdict": [("是", "真空吸盘经万向接头安装于机械臂末端法兰，提供角度"
                            "浮动安装。", 2,
                       "真空吸盘经万向接头安装于机械臂末端法兰"),
                      ("是", "吸盘工作面均匀分布微孔，微孔汇至真空回路形成负压"
                            "吸附区。", 2,
                       "微孔汇至真空回路形成负压吸附区"),
                      ("是", "真空回路串联负压传感器实时检测吸附状态，防止掉件。", 3,
                       "真空回路串联负压传感器实时检测吸附状态"),
                      ("是", "脱料时由电磁阀破真空，同时经微孔吹气辅助快速释放"
                            "工件。", 3,
                       "由电磁阀破真空，同时经微孔吹气辅助快速释放工件")],
          "conclusion": "该文件单篇完整记载万向吸盘、微孔负压、负压检测与"
                        "破真空吹气释放的全部要素，构成新颖性威胁。"}),
        ("syn_q1", "2120001", "20242120001",
         "一种带负压检测的真空吸盘机械手",
         _V1_PARAS,
         ["1. 一种真空吸盘机械手，含万向接头、微孔吸盘和负压传感器。"],
         "B25J15/00", "2023", 1688169600, 90,
         {"grade": "Y",
          "verdict": [("是", "真空吸盘经球头万向接头安装于机械臂末端，具备角度"
                            "浮动。", 2,
                       "真空吸盘经球头万向接头安装于机械臂末端"),
                      ("是", "工作面分布微孔并连通真空回路形成负压吸附。", 2,
                       "工作面分布微孔并连通真空回路形成负压吸附"),
                      ("是", "真空回路串联负压传感器，吸附不足时机器人停止抬升。", 3,
                       "真空回路串联负压传感器，吸附不足时机器人停止"),
                      ("否", "未涉及破真空吹气辅助释放。", None, None)],
          "conclusion": "该文件公开检测吸附部分，未公开破真空吹气脱料，需结合"
                        "气路控制类文件评价创造性。"}),
        ("syn_q3", "2120003", "20242120003",
         "一种带耐磨唇的真空吸盘",
         _V3_PARAS,
         ["1. 一种真空吸盘，唇部局部加厚耐磨。"],
         "B25J15/00", "2022", 1648771200, 80,
         {"grade": "A",
          "verdict": [("部分", "公开一种唇部局部加厚的耐磨真空吸盘，与吸附主题"
                              "部分相关但不含气路与检测结构。", 2,
                       "本发明公开一种带耐磨唇的真空吸盘"),
                      ("否", "未涉及微孔与真空回路结构。", None, None),
                      ("否", "未涉及负压传感器。", None, None),
                      ("否", "未涉及破真空吹气。", None, None)],
          "conclusion": "该文件为通用吸盘耐磨配件的背景技术，不涉及检测与"
                        "气路控制，仅反映现有技术一般状况。"}),
        ("syn_w1", "2120101", "20242120101", "一种电磁起重吸盘",
         [("技术领域", "本发明涉及起重电磁铁。"),
          ("背景技术", "钢铁车间搬运钢坯采用电磁吸盘。"),
          ("发明内容", "本发明为一种带停电保磁的电磁起重吸盘。"),
          ("具体实施方式", "线圈通电吸合钢坯，停电时保磁延时释放。")],
         ["1. 一种电磁起重吸盘，含励磁线圈与保磁电源。"],
         "B66C1/04", "2023", 1688169600, 30, None),
        ("syn_w2", "2120102", "20242120102", "一种玻璃搬运机械手",
         [("技术领域", "本发明涉及板材搬运机械手。"),
          ("背景技术", "玻璃板材表面光滑需要柔性抓取。"),
          ("发明内容", "本发明在机械臂末端安装真空吸盘夹具，靠行程开关防撞。"),
          ("具体实施方式", "吸盘为普通平口结构，仅靠通断气完成取放，不具备"
                          "吸附力检测与主动释放元件。")],
         ["1. 一种玻璃搬运机械手，机械臂末端设真空吸盘夹具与行程开关。"],
         "B25J15/06", "2024", 1688169600, 40, None),
        ("syn_w3", "2120103", "20242120103", "一种胶囊吸塑包装模具",
         [("技术领域", "本发明涉及药品包装设备。"),
          ("背景技术", "胶囊泡罩成型需要负压吸附。"),
          ("发明内容", "本发明在吸塑模具内设置负压气室成型泡罩。"),
          ("具体实施方式", "负压气室与真空站相连，与机械手无关。")],
         ["1. 一种胶囊吸塑包装模具，含负压气室与加热板。"],
         "B65B47/00", "2022", 1648771200, 20, None),
    ],
    "labels": {"CN2120002A": "X", "CN2120001A": "Y", "CN2120003A": "A"},
    "critic_conflicts": [],
}


# ---------------- 宇宙装配 ----------------

def build_universe(spec: dict) -> dict:
    """把专利元组列表展开成后端可用结构 + golden 定义。"""
    patents, profiles = [], {}
    for row in spec["patents"]:
        (pid, pub, app, title, paras, claims, ipc_l4,
         year, seconds, weight, profile) = row
        rec = _pat(pid, pub, app, title, year=year, seconds=seconds,
                   ipc_l4=ipc_l4, inventor="合成发明人", assignee="合成科技有限公司",
                   abstract=paras[1][1], paras=paras, claims=claims)
        rec["_weight"] = weight
        patents.append(rec)
        if profile:
            profiles[f"CN{pub}A"] = profile
    return {
        "golden": {
            "case_id": spec["case_id"],
            "title": spec["title"],
            "synthetic": True,
            "disclosure": spec["disclosure"],
            "query": {
                "topic": spec["title"],
                "keywords": spec["disclosure"]["terms"][:3],
                "synonyms": spec["disclosure"]["terms"][3:],
                "ipc": spec["ipc"],
                "dateFrom": "2018-01-01", "dateTo": "2026-12-31",
                "expr": "",
                "groups": spec["groups"],
            },
            "run": {"budget": 3, "min_score": 1, "size": 50},
            "labels": spec["labels"],
            "notes": "合成宇宙：仅验证评测管线，标签为构造真值，不代表真实检索结论。",
        },
        "patents": patents,
        "profiles": profiles,
        "critic_conflicts": spec["critic_conflicts"],
    }


UNIVERSES = {u["case_id"]: build_universe(u)
             for u in (_UNIVERSE_BATT, _UNIVERSE_VAC)}


# ---------------- 合成 AMiner 后端 ----------------

class SyntheticAminer:
    def __init__(self, universe: dict):
        self.u = universe
        self.min_interval = 0.0
        self.max_retry = 0

    def _by_id(self, pid: str):
        return next((p for p in self.u["patents"] if p["id"] == pid), None)

    def search(self, query: str, page: int = 0, size: int = 20):
        terms = [t for t in (query or "").split() if t]
        hits = []
        for p in self.u["patents"]:
            blob = p["_blob"]
            if all(t in blob for t in terms):
                hits.append(p)
        hits.sort(key=lambda p: (-p["_weight"], p["id"]))
        return [{"id": p["id"], "title_zh": p["_title"],
                 "pub_year": p["_year"], "inventor_name": "合成发明人"}
                for p in hits[:size]]

    def info(self, patent_id: str):
        p = self._by_id(patent_id)
        if not p:
            return None
        return {"id": p["id"], "country": p["country"], "app_num": p["app_num"],
                "pub_num": p["pub_num"], "pub_kind": p["pub_kind"]}

    def detail(self, patent_id: str):
        p = self._by_id(patent_id)
        if not p:
            return None
        return {k: v for k, v in p.items() if not k.startswith("_")}


# ---------------- 合成 LLM 后端 ----------------

_PUB_RE = re.compile(r"公开号：(\S+)")


class SyntheticLLM:
    """角色识别的确定性 LLM。token 数为字符估算（合成数据水印随报告明示）。"""

    def __init__(self, universe: dict, model: str = "synthetic-flash"):
        self.u = universe
        self.model = model
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0,
                      "cached_tokens": 0, "calls": 0}

    def _count(self, system: str, user: str, value) -> None:
        self.usage["calls"] += 1
        self.usage["prompt_tokens"] += (len(system) + len(user)) // 3 + 1
        self.usage["completion_tokens"] += len(json.dumps(value, ensure_ascii=False)) // 2 + 1

    def _reader(self, user: str) -> dict:
        m = _PUB_RE.search(user)
        pub = m.group(1) if m else ""
        if pub.isdigit():
            pub = f"CN{pub}A"
        prof = self.u["profiles"].get(pub)
        if not prof:
            return {"grade": "A", "features": [], "conclusion": "未知对比文件。"}
        features = []
        for level, theirs, pidx, quote in prof["verdict"]:
            ev = None
            if pidx is not None and quote:
                ev = {"paragraphIndex": pidx, "quote": quote}
            features.append({"disclosed": level, "theirs": theirs, "evidence": ev})
        return {"grade": prof["grade"], "features": features,
                "conclusion": prof["conclusion"]}

    def _critic(self, user: str) -> dict:
        gaps, conflicts = [], []
        for c in self.u["critic_conflicts"]:
            if c["pub_no"] in user and c["trigger"] in user:
                conflicts.append({"pub_no": c["pub_no"], "issue": c["issue"],
                                  "suggested_grade": c["suggested_grade"]})
        return {"passed": not gaps and not conflicts,
                "gaps": gaps, "conflicts": conflicts,
                "summary": "合成宇宙质控：发现 %d 项分级冲突。" % len(conflicts)
                           if conflicts else "合成宇宙质控：证据与分级自洽，通过。"}

    def chat_json(self, system: str, user: str, *, temperature: float = 0.1,
                  max_tokens: int = 2000, required_keys=None,
                  repair_hint=None) -> dict:
        if "资深专利审查员" in system:
            value = self._reader(user)
        elif "审查质控员" in system:
            value = self._critic(user)
        elif "检索质量评估器" in system:
            value = {"decision": "sufficient",
                     "reason": "合成宇宙：候选池覆盖全部检索角度",
                     "missing": [], "searches": []}
        elif "规划器" in system:
            value = {"decision": "advance", "reason": "合成宇宙：无需修订",
                     "open_gaps": [], "revisions": []}
        else:
            value = {"decision": "advance", "passed": True,
                     "gaps": [], "conflicts": [], "revisions": []}
        self._count(system, user, value)
        return value

    def chat_text(self, system: str, user: str, *, temperature: float = 0.3,
                  max_tokens: int = 1000) -> str:
        self._count(system, user, "ok")
        return "ok"
