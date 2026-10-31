"""Compare completed API trials with user-provided manuscripts, locally only."""
from __future__ import annotations
import argparse
from datetime import datetime
import difflib
import hashlib
import html
import json
from pathlib import Path
import re


def digest(value):
    return hashlib.sha256(value).hexdigest()


def nonspace(text):
    return re.sub(r"\s", "", text)


def page(title, body):
    return """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>""" + html.escape(title) + """</title>
<style>body{font:16px/1.65 system-ui,'Microsoft YaHei',sans-serif;background:#f4f6fa;color:#172536;margin:0;padding:26px}
main{max-width:1500px;margin:auto}h1{font-size:28px}h2{font-size:21px}a{color:#075cc1}table{width:100%;border-collapse:collapse;background:white}
td,th{padding:12px;text-align:left;vertical-align:top;border-bottom:1px solid #dce2eb}th{background:#e8eef7;position:sticky;top:0}
.note{background:#fff7df;border-left:4px solid #ddb145;padding:14px 20px}.card,details{background:white;padding:18px;margin:16px 0;border-radius:8px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:18px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:15px/1.7 'Microsoft YaHei',sans-serif;background:#f7f9fc;padding:14px}
small{color:#52657a}summary{cursor:pointer;font-weight:600}.num{white-space:nowrap}@media(max-width:850px){body{padding:12px}.cols{grid-template-columns:1fr}table{font-size:14px}}</style><main>""" + body + "</main></html>"


def build_report(source, auto, human, split, output):
    source, auto, output = Path(source).resolve(strict=True), Path(auto).resolve(strict=True), Path(output).resolve()
    files = sorted(source.glob("*.txt"))
    summary = json.loads((auto / "summary.json").read_text(encoding="utf-8"))
    names = {p.name for p in files}
    if (not names or summary.get("complete") != len(files) or summary.get("technical_incomplete") != 0
            or {r.get("file") for r in summary.get("books", []) if r.get("complete")} != names):
        raise ValueError("自动审改尚未全部完成，不能读取留出人工稿进行最终评价")
    if output.exists():
        raise FileExistsError("报告目录已存在，拒绝覆盖")
    split_data = json.loads(Path(split).read_text(encoding="utf-8"))
    # First verify every auto artifact. Human files are opened only after this
    # complete-generation gate; they are never supplied to the generator.
    records = []
    for src in files:
        src_raw = src.read_bytes()
        auto_raw = (auto / src.name).read_bytes()
        report_raw = (auto / f"{src.stem}-review.json").read_bytes()
        report = json.loads(report_raw)
        if (digest(src_raw) != split_data["source_sha256"][src.name]
                or report.get("source_sha256") != digest(src_raw)
                or report.get("final_text_sha256") != digest(auto_raw)
                or report.get("automatic_review_complete") is not True
                or report.get("file") != src.name or report.get("version") != 2):
            raise ValueError(f"源文件/成稿/复核记录不一致：{src.name}")
        original, current = src_raw.decode("utf-8-sig"), auto_raw.decode("utf-8-sig")
        cursor = 0
        for mapping in report.get("source_mapping", []):
            if mapping["source_start"] != cursor or mapping["source_end"] <= cursor:
                raise ValueError(f"原文映射区间缺失或重叠：{src.name}")
            if not 0 <= mapping["output_start"] <= mapping["output_end"] <= len(current):
                raise ValueError(f"成稿映射超出实际文件：{src.name}")
            if mapping["kind"] == "keep" and original[cursor:mapping["source_end"]] != current[mapping["output_start"]:mapping["output_end"]]:
                raise ValueError(f"保留段落与成稿内容不符：{src.name}")
            cursor = mapping["source_end"]
        if cursor != len(original) or not report.get("core_facts"):
            raise ValueError(f"来源覆盖或主线元数据不完整：{src.name}")
        records.append(dict(name=src.name, original=original, current=current, auto_raw=auto_raw,
                            report_raw=report_raw, report=report))
    human = Path(human).resolve(strict=True)
    for record in records:
        raw = (human / record["name"]).read_bytes()
        if digest(raw) != split_data["human_sha256"][record["name"]]:
            raise ValueError(f"人工稿在对比后改变：{record['name']}")
        record["human"] = raw.decode("utf-8-sig")
    for input_dir in (source, auto, human):
        if output == input_dir or input_dir in output.parents or output in input_dir.parents:
            raise ValueError("报告必须保存在独立新目录")
    output.mkdir(parents=True, exist_ok=False)
    (output / "程序稿").mkdir()
    (output / "复核记录").mkdir()
    rows, stats = [], []
    status_names = dict(complete="完成", fidelity_repaired="修补后完成", fidelity_fallback="含保守回退")
    for number, record in enumerate(records, 1):
        name, original, current, manual, report = (record[k] for k in ("name", "original", "current", "human", "report"))
        before, after, human_chars = len(nonspace(original)), len(nonspace(current)), len(nonspace(manual))
        removed = added = 0
        for tag, a, b, c, d in difflib.SequenceMatcher(None, nonspace(original), nonspace(current), autojunk=False).get_opcodes():
            if tag != "equal":
                removed += b - a; added += d - c
        actions = report["edit_actions"]
        meaningful = [a for a in actions if nonspace(a["original"]) != nonspace(a["replacement"])]
        raw_flags = len(report["remaining_risks"])
        rule_flags=sum(a.get('outcome')=='residual_after_bounded_edits' and a.get('origin')=='rule_precheck'
                       for a in report['risk_actions'])
        stat = dict(file=name, split=report["split"], source_chars=before, human_chars=human_chars,
                    program_chars=after, net_shorter_percent=round((before-after)/before*100, 2),
                    removed_chars=removed, added_chars=added, scene_operations=len(actions),
                    operations_with_nonwhitespace_change=len(meaningful), residual_model_flags=raw_flags-rule_flags,
                    residual_rule_flags=rule_flags,residual_total_flags=raw_flags,
                    review_status=report["review_status"], source_sha256=report["source_sha256"], final_text_sha256=report["final_text_sha256"])
        stats.append(stat)
        (output / "程序稿" / name).write_bytes(record["auto_raw"])
        (output / "复核记录" / f"{Path(name).stem}-review.json").write_bytes(record["report_raw"])
        for label, text, slug in (("原文", original, "source"), ("人工稿", manual, "human")):
            diff = difflib.HtmlDiff(wrapcolumn=65).make_file(text.splitlines(), current.splitlines(),
                fromdesc=html.escape(label + " · " + name), todesc="程序稿", context=True, numlines=3, charset="utf-8")
            (output / f"{number:02d}-{slug}.html").write_text(diff, encoding="utf-8")
        body = f'<p><a href="index.html">返回总表</a> · <a href="{number:02d}-source.html">原文→程序稿差异</a> · <a href="{number:02d}-human.html">人工稿→程序稿差异</a></p><h1>{html.escape(name)}</h1>'
        body += '<p class="note">编辑理由与风险标记由模型给出，可能误报或漏报。以下字符变化和场景操作数不等于违规数量或过审率。</p>'
        body += '<h2>逐场景编辑</h2>'
        for i, action in enumerate(actions, 1):
            changed = "文字变化" if nonspace(action["original"]) != nonspace(action["replacement"]) else "仅排版或保留尝试"
            body += f'<details><summary>{i}. {html.escape(action["kind"])} · {changed} · 原文行 {html.escape(str(action["source_lines"]))}</summary><p>{html.escape(action["reason"])}</p><div class="cols"><section>原文<pre>{html.escape(action["original"])}</pre></section><section>程序稿<pre>{html.escape(action["replacement"])}</pre></section></div></details>'
        body += '<h2>结尾对照</h2><div class="cols"><section>原文结尾<pre>' + html.escape(original[-500:]) + '</pre></section><section>程序稿结尾<pre>' + html.escape(current[-500:]) + '</pre></section></div>'
        body += '<details><summary>程序锁定的主线事实</summary><ol>'
        for fact in report["core_facts"]:
            body += '<li>' + html.escape(fact["statement"]) + '<br><small>原文证据：' + html.escape(fact["evidence"]) + '</small></li>'
        body += '</ol></details><details><summary>主线与衔接核对、修补及回退记录</summary>'
        for action in report['risk_actions']:
            outcome=action.get('outcome')
            if outcome=='source_identity_verified':
                body += '<p>逐字保留证据证明主线未改：'+str(len(action['fact_ids']))+' 条已核实事实。</p>'
            elif outcome=='discarded_earlier_invention':
                body += '<p>后续发现旧稿已有新增事实，已将对应来源恢复原文，未沿用早期漏检结果。</p>'
            elif outcome=='proposal_rejected':
                body += '<div class="card"><b>自动拒绝的改稿提议：'+html.escape(action['verdict'])+'</b><p>'+html.escape(action['reason'])+'</p><div class="cols"><section>原文<pre>'+html.escape(action['original'])+'</pre></section><section>被拒绝提议（未交付）<pre>'+html.escape(action['proposal'])+'</pre></section></div></div>'
            elif outcome=='review_finding_not_confirmed':
                finding=action['finding'];proof=finding['verification']
                label='原文逐字对照证明的既有语病' if proof.get('method')=='source_lexical_identity' else '独立核实未支持的异议'
                body += '<div class="card"><b>'+label+'</b><p>原异议：'+html.escape(finding['reason'])+'</p><p>核实：'+html.escape(proof['reason'])+'</p><div class="cols"><pre>'+html.escape(proof['source_quote'])+'</pre><pre>'+html.escape(proof['output_quote'])+'</pre></div></div>'
            elif outcome in {'needs_repair','restored_core','restored_previous_valid'}:
                label={'needs_repair':'集中修补','restored_core':'保守恢复来源文本',
                       'restored_previous_valid':'撤回本轮，恢复上一完整有效版本'}[outcome]
                body += '<div class="card"><b>'+label+'</b><ol>'
                for problem in action.get('problems',[]):
                    body += '<li>'+html.escape(problem['reason'])+'</li>'
                body += '</ol></div>'
        body += '</details><h2>剩余风险线索（模型及规则预筛）</h2>'
        for risk in report["remaining_risks"]:
            body += '<div class="card"><b>' + html.escape(risk["category"]) + '</b><pre>' + html.escape(risk["span"]) + '</pre><p>' + html.escape(risk["reason"]) + '</p></div>'
        if not report["remaining_risks"]:
            body += '<p>本次未发现剩余线索；这不证明平台会批准。</p>'
        (output / f"{number:02d}-actions.html").write_text(page(name, body), encoding="utf-8")
        rows.append(f'<tr><td>{number}. {html.escape(name)}<br><small>{"留出验证" if report["split"]=="holdout" else "示例"}</small><br><a href="{number:02d}-actions.html">逐场景明细与结尾</a> · <a href="{number:02d}-source.html">原文对照</a> · <a href="{number:02d}-human.html">人工对照</a></td><td>{before} / {human_chars} / {after}<br><small>程序净缩短 {stat["net_shorter_percent"]}%</small></td><td>{len(actions)} / {len(meaningful)}</td><td>{raw_flags}</td><td>{status_names.get(report["review_status"],html.escape(report["review_status"]))}</td></tr>')
    now = datetime.now().isoformat(timespec="seconds")
    body = f'<h1>{len(records)} 本场景自动审改对照</h1><p>生成时间：{now}。{len(records)} 本均由程序调用接口审改；原文与人工稿均通过既定 SHA-256 核对，程序稿与复核记录哈希一致。</p>'
    body += '<p>实测模型：'+html.escape(str(summary.get('model','未记录')))+'；“单段最大字数”设置：'+html.escape(str(summary.get('max_request_chars','未记录')))+'；请求超时（秒）：'+html.escape(str(summary.get('request_timeout_seconds','未记录'))) +'。新程序使用相同参数可复现本次请求预算。</p>'
    body += '<p>本次实测范围：直接运行程序的场景审改模块；自定义补充规则为空，指定违禁词列表为空，未执行配音或发送。安装后的接口、补充规则、指定词和请求预算会影响实际结果；指定词的最终精确替换与遮盖另由处理器执行，并已做离线集成回归。</p>'
    body += '<p>为减少重复请求，程序会复用经过当前协议校验的真实接口检查点。JSON 中 audit_calls/rewrite_calls 仅统计当前成功尝试的新增请求，不含检查点、此前试跑及此前重试，不能当作全部调用或费用统计。</p>'
    body += '<p class="note">人工稿的“已通过”状态由用户提供。本次未向平台上传这些程序稿。保留主要事件、人物关系及结局，允许删细节、压缩与补衔接；人工稿中改事件、截结尾或谐音掩字的做法不作为目标。生成阶段没有读取留出集人工全文。</p>'
    body += '<p>留出仅指本次生成阶段未输入对应人工全文；这批资料此前用于差异分析，不等同完全未见过的外部测试集，不能据此推算其他书籍的过审率。</p>'
    body += f'<p><a href="程序稿/">打开 {len(records)} 本同名程序 TXT</a> · <a href="复核记录/">打开完整 JSON 复核记录</a></p><table><thead><tr><th>书籍与对照</th><th>原文 / 人工 / 程序字符数（去空白）</th><th>场景操作 / 有文字变化的操作</th><th>剩余风险线索（模型及规则）</th><th>程序复核状态</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table>'
    body += '<p>操作数不是修改行数：一次场景压缩可以同时处理多段文字。字符统计包含章节编号；模型标记与平台拒审理由不可直接等同。最终投放还涉及标题、视频、字幕及封面。</p>'
    (output / "index.html").write_text(page("场景自动审改对照", body), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(dict(created_at=now, books=stats, count=len(stats)), ensure_ascii=False, indent=2), encoding="utf-8")
    return dict(count=len(stats), output=str(output), books=stats)


def main():
    p=argparse.ArgumentParser()
    for flag in ("source", "auto", "human", "split", "output"):
        p.add_argument("--"+flag, type=Path, required=True)
    args=p.parse_args()
    result=build_report(args.source,args.auto,args.human,args.split,args.output)
    print(json.dumps({k:result[k] for k in ("count","output")},ensure_ascii=False))


if __name__=="__main__":
    main()
