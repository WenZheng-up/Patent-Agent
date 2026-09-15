# -*- coding: utf-8 -*-
"""
查新报告 v1：生成可打印 A4 HTML（契约 §9）。
引用渲染为 §章节 ¶序号；grade 未定时显示「待精读（v2 AI 分级）」。
v2 替换为 python-docx 的 .docx，URL 形态不变。
"""
import html
from pathlib import Path

FILES_DIR = Path(__file__).resolve().parents[2] / "data" / "files"

_CSS = """
body{background:#F5F4EF;color:#1c1e23;font-family:Georgia,'Songti SC',SimSun,serif;
     margin:0;padding:32px;}
.paper{max-width:800px;margin:0 auto;background:#fff;padding:56px 64px;
       box-shadow:0 1px 6px rgba(0,0,0,.08);}
h1{font-size:24px;text-align:center;letter-spacing:4px;margin:0 0 4px;}
.sub{text-align:center;color:#66707F;font-size:13px;margin-bottom:28px;}
.meta{width:100%;border-collapse:collapse;font-size:13px;margin:16px 0 28px;}
.meta td{border:1px solid #d8d5cc;padding:6px 10px;}
.meta td:first-child{background:#f7f6f2;width:110px;color:#66707F;}
h2{font-size:16px;border-left:4px solid #3450C8;padding-left:10px;margin:28px 0 12px;}
table.grid{width:100%;border-collapse:collapse;font-size:12.5px;}
table.grid th,table.grid td{border:1px solid #d8d5cc;padding:6px 8px;vertical-align:top;}
table.grid th{background:#f7f6f2;text-align:left;}
.mono{font-family:'JetBrains Mono',Consolas,monospace;font-size:12px;}
.tag-x{color:#C2402A;font-weight:bold;}
.tag-y{color:#B7791F;font-weight:bold;}
.tag-a{color:#66707F;font-weight:bold;}
.cite{font-family:'JetBrains Mono',Consolas,monospace;font-size:11.5px;color:#3450C8;}
.disclaimer{font-size:12px;color:#66707F;border-top:1px dashed #d8d5cc;
            margin-top:36px;padding-top:16px;line-height:1.8;}
.feat-no{color:#9aa0a8;}.feat-part{color:#B7791F;}.feat-yes{color:#C2402A;}
"""


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _grade_cell(g):
    if g == "X":
        return '<span class="tag-x">X 类</span>'
    if g == "Y":
        return '<span class="tag-y">Y 类</span>'
    if g == "A":
        return '<span class="tag-a">A 类</span>'
    return '<span style="color:#9aa0a8">待精读</span>'


def _disclosed_cell(d):
    cls = {"是": "feat-yes", "部分": "feat-part", "否": "feat-no"}.get(d, "")
    return f'<span class="{cls}">{_e(d)}</span>'


def _cite_cell(ev):
    if not ev:
        return '<span style="color:#9aa0a8">—</span>'
    return f'<span class="cite">{_e(ev["section"])} ¶{ev["paragraphIndex"]}</span>'


def render_report(case: dict, retrieval: dict, prior_art: list[dict]) -> str:
    s = retrieval["summary"]
    q = case["query"]
    meta = case.get("reportMeta") or {}
    rows = []
    for art in prior_art:
        feats = "".join(
            f"<tr><td>{_e(f['mine'])}</td><td>{_e(f['theirs'])}</td>"
            f"<td>{_disclosed_cell(f['disclosed'])}</td><td>{_cite_cell(f['evidence'])}</td></tr>"
            for f in art["features"])
        rows.append(f"""
        <h3 style="font-size:14px;margin:20px 0 6px;">
          {_grade_cell(art['grade'])}
          <span class="mono">{_e(art['pubNo'])}</span>
          {_e(art['title'])}
        </h3>
        <p style="font-size:12.5px;color:#66707F;margin:4px 0;">
          {_e(art['assignee'])} · {_e(art['date'])} · 相关度 {_e(art['score'])}
        </p>
        <table class="grid"><thead><tr>
          <th style="width:26%">本申请技术特征</th><th style="width:40%">对比文件公开内容</th>
          <th style="width:10%">公开程度</th><th style="width:24%">证据出处</th>
        </tr></thead><tbody>{feats}</tbody></table>
        <p style="font-size:12.5px;">结论：{_e(art['conclusion']) or
           '<span style="color:#9aa0a8">待 v2 AI 精读生成（当前为关键词骨架匹配）</span>'}</p>""")

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>查新报告 {_e(meta.get('no'))}</title><style>{_CSS}</style></head>
<body><div class="paper">
  <h1>专利查新报告</h1>
  <div class="sub">Patent-Agent · AI 初筛（代理师复核制）</div>
  <table class="meta">
    <tr><td>报告编号</td><td class="mono">{_e(meta.get('no'))}</td>
        <td>报告日期</td><td>{_e(meta.get('date'))}</td></tr>
    <tr><td>案件名称</td><td colspan="3">{_e(case['title'])}</td></tr>
    <tr><td>客户</td><td>{_e(case['client'])}</td>
        <td>案件编号</td><td class="mono">{_e(case['id'])}</td></tr>
    <tr><td>检索人</td><td>{_e(meta.get('searcher'))}</td>
        <td>复核人</td><td>{_e(meta.get('reviewer'))}</td></tr>
    <tr><td>检索数据库</td><td colspan="3">{_e(meta.get('db'))}</td></tr>
  </table>

  <h2>一、检索要素</h2>
  <table class="grid">
    <tr><th style="width:14%">技术问题</th><td>{_e(case['disclosure']['problem'])}</td></tr>
    <tr><th>技术方案</th><td>{_e(case['disclosure']['solution'])}</td></tr>
    <tr><th>技术效果</th><td>{_e(case['disclosure']['effect'])}</td></tr>
    <tr><th>IPC 分类</th><td class="mono">{_e(' / '.join(q.get('ipc', [])))}</td></tr>
    <tr><th>时间范围</th><td class="mono">{_e(q.get('dateFrom'))} ~ {_e(q.get('dateTo'))}</td></tr>
  </table>

  <h2>二、检索式（经代理师审批）</h2>
  <p class="mono" style="background:#f7f6f2;padding:10px 12px;font-size:12px;">
    {_e(q['expr'])}</p>
  <p style="font-size:12.5px;color:#66707F;">
    CNF 编译 {_e(s['subqueryCount'])} 个子查询 fan-out；粗检并集 {_e(s['coarseTotal'])} 条
    （{_e(s['coarseMultiHit'])} 条命中 ≥2 个子查询；日期过滤 {_e(s['dateFiltered'])} 条）；
    精检 {_e(s['finePatents'])} 篇全文，付费 detail {_e(s['paidDetailCalls'])} 次。
  </p>

  <h2>三、对比文件分析</h2>
  {''.join(rows) or '<p style="color:#9aa0a8">暂无对比文件。</p>'}

  <h2>四、查新结论</h2>
  <p style="font-size:13px;line-height:1.9;">
    本报告为 AI 基于检索结果的<b>初筛意见</b>，特征级比对与 X/Y/A 分级的
    法律结论以代理师复核为准；全部证据出处均标注至专利号与章节段落，过程可回放。
  </p>

  <div class="disclaimer">
    免责声明：本报告由「Patent-Agent」智能检索系统自动生成，检索结果受数据库覆盖范围、
    检索式表达与语义匹配精度限制，不构成法律意见。新颖性/创造性的最终判断应由
    执业专利代理师结合权利要求书与全部案卷材料作出。
  </div>
</div></body></html>"""


def generate_report_file(case: dict, retrieval: dict, prior_art: list[dict]) -> str:
    """写文件，返回可访问 URL 路径。"""
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    no = (case.get("reportMeta") or {}).get("no") or f"QX-{case['id'].replace('CN', '')}-01"
    path = FILES_DIR / f"{no}.html"
    path.write_text(render_report(case, retrieval, prior_art), encoding="utf-8")
    return f"/files/{no}.html"
